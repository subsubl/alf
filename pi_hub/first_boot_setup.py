"""
Raspberry Pi Zero 2 W First-Boot AP Setup Portal (Tier 2):
- Runs once on first boot before `quixi-pi.service` / `tamagci-pi-hub.service` when
  `/etc/tamagci/firstboot.done` does not exist.
- Brings up a Wi-Fi Access Point (`Alf-Setup` @ 192.168.4.1) via NetworkManager (`nmcli`)
  and serves a zero-dependency mobile-friendly captive setup portal on port 80.
- Required fields:
    1. Linux User Account (`username`, `password`)
    2. Wi-Fi Network (`wifi_ssid`, optional `wifi_password` for open networks, `wifi_country`)
- Optional (skippable) fields:
    - Hostname, QuIXI `walletPassword`, `server_ixian_address`, SSH enable
- Zero-Trust compliant: zero AI provider API keys are stored or accepted on the Pi.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pi_hub.pi_system_config import (
    PiSystemConfigManager,
    RASPI_INTERFACE_KEYS,
    default_cmd_runner as _default_cmd_runner,
    parse_nmcli_wifi_scan,
)

logger = logging.getLogger("pi_hub.first_boot")

DEFAULT_AP_SSID = "Alf-Setup"
DEFAULT_AP_CON_NAME = "tamagci-setup-ap"
DEFAULT_AP_IP = "192.168.4.1"
DEFAULT_SENTINEL_PATH = Path("/etc/tamagci/firstboot.done")
DEFAULT_IXIAN_CFG_PATH = Path("/opt/quixi/ixian.cfg")
DEFAULT_HUB_CFG_PATH = Path("/opt/tamagci/pi_hub/config.yaml")
DEFAULT_CAPTIVE_DNSMASQ_PATH = Path("/etc/NetworkManager/dnsmasq-shared.d/tamagci-captive.conf")

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$")

CmdRunnerFn = Callable[[List[str], Optional[str]], Awaitable[Tuple[int, str, str]]]


async def _default_cmd_runner(
    cmd: List[str], stdin_text: Optional[str] = None
) -> Tuple[int, str, str]:
    """Run a system command asynchronously and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    in_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
    out_b, err_b = await proc.communicate(input=in_bytes)
    return (
        int(proc.returncode or 0),
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


def parse_nmcli_wifi_scan(raw_output: str) -> List[Dict[str, Any]]:
    """Parse `nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list` output into unique sorted networks."""
    best_by_ssid: Dict[str, Dict[str, Any]] = {}
    for raw_line in (raw_output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # nmcli escapes literal ':' as '\:' in terse (-t) mode
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 2:
            continue
        ssid = parts[0].replace("\\:", ":").strip()
        if not ssid:
            continue
        try:
            signal = int(parts[1].strip())
        except ValueError:
            signal = 0
        security = parts[2].replace("\\:", ":").strip() if len(parts) >= 3 else ""
        prev = best_by_ssid.get(ssid)
        if prev is None or signal > prev["signal"]:
            best_by_ssid[ssid] = {
                "ssid": ssid,
                "signal": max(0, min(100, signal)),
                "security": security or "OPEN",
            }
    return sorted(best_by_ssid.values(), key=lambda x: (-x["signal"], x["ssid"].lower()))


class FirstBootAPPortal:
    """
    Zero-dependency asyncio HTTP captive setup portal for Raspberry Pi Zero 2 W first boot.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 80,
        ap_ssid: str = DEFAULT_AP_SSID,
        ap_con_name: str = DEFAULT_AP_CON_NAME,
        ap_ip: str = DEFAULT_AP_IP,
        wifi_iface: str = "wlan0",
        sentinel_path: Path = DEFAULT_SENTINEL_PATH,
        ixian_cfg_path: Path = DEFAULT_IXIAN_CFG_PATH,
        hub_cfg_path: Path = DEFAULT_HUB_CFG_PATH,
        captive_dnsmasq_path: Optional[Path] = DEFAULT_CAPTIVE_DNSMASQ_PATH,
        esp_serial_port: str = "/dev/ttyACM0",
        recovery_mode: bool = False,
        cmd_runner: Optional[CmdRunnerFn] = None,
        dry_run: bool = False,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.ap_ssid = ap_ssid
        self.ap_con_name = ap_con_name
        self.ap_ip = ap_ip
        self.wifi_iface = wifi_iface
        self.sentinel_path = Path(sentinel_path)
        self.ixian_cfg_path = Path(ixian_cfg_path)
        self.hub_cfg_path = Path(hub_cfg_path)
        self.captive_dnsmasq_path: Optional[Path] = (
            Path(captive_dnsmasq_path) if captive_dnsmasq_path is not None else None
        )
        self.esp_serial_port = esp_serial_port
        self.recovery_mode = bool(recovery_mode)
        self._run_cmd: CmdRunnerFn = cmd_runner or _default_cmd_runner
        self.dry_run = bool(dry_run)
        self.sys_cfg = PiSystemConfigManager(
            wifi_iface=self.wifi_iface,
            cmd_runner=self._run_cmd,
            dry_run=self.dry_run,
            use_sudo=False,
        )

        self._server: Optional[asyncio.AbstractServer] = None
        self.bound_port: int = self.port
        self.setup_complete_event = asyncio.Event()
        self.last_setup_summary: Optional[Dict[str, Any]] = None
        self.ap_active: bool = False

    def is_first_boot_needed(self) -> bool:
        return not self.sentinel_path.is_file()

    async def has_active_wifi_connection(self, wait_s: float = 0.0) -> bool:
        """Return True if wifi_iface is connected to a non-AP Wi-Fi network (optionally waiting up to wait_s)."""
        if self.dry_run:
            return False
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            rc, out, _ = await self._run_cmd(
                ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], None
            )
            if rc == 0 and out:
                for line in out.splitlines():
                    parts = re.split(r"(?<!\\):", line.strip())
                    if len(parts) >= 4 and parts[0].strip() == self.wifi_iface:
                        state = parts[2].strip().lower()
                        con = parts[3].replace("\\:", ":").strip()
                        if state == "connected" and con and con != "--" and con != self.ap_con_name:
                            return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(2.0)

    def _notify_esp32_ap_ready(self) -> bool:
        """Best-effort one-shot NDJSON subtitle to ESP32-S3 LCD so the face shows AP setup instructions."""
        if self.dry_run or not self.esp_serial_port or not os.path.exists(self.esp_serial_port):
            return False
        try:
            import serial  # type: ignore

            msg = json.dumps(
                {
                    "t": "cmd",
                    "c": "state",
                    "emo": "ALERT",
                    "sub": f"Wi-Fi: {self.ap_ssid} -> {self.ap_ip}",
                }
            ) + "\n"
            with serial.Serial(self.esp_serial_port, 921600, timeout=0.5, write_timeout=1.0) as ser:
                ser.write(msg.encode("utf-8"))
                ser.flush()
            return True
        except Exception as exc:
            logger.debug("Could not push AP status to ESP32 (%s): %s", self.esp_serial_port, exc)
            return False

    # ------------------------------------------------------------------
    # NetworkManager AP + Wi-Fi Helpers
    # ------------------------------------------------------------------

    async def _ensure_regdom_for_ap(self) -> None:
        """Ensure cfg80211 is not stuck in world regdom '00' (which blocks AP beaconing on 2.4GHz)."""
        try:
            rc, out, _ = await self._run_cmd(["iw", "reg", "get"], None)
        except FileNotFoundError:
            return
        if rc == 0 and re.search(r"(?m)^country\s+00\b", out or ""):
            try:
                await self._run_cmd(["iw", "reg", "set", "US"], None)
            except FileNotFoundError:
                pass

    async def _wait_for_wifi_iface(self, timeout_s: float = 15.0) -> None:
        """Wait for NetworkManager to register the Wi-Fi interface and leave unavailable/unmanaged state."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            rc, out, _ = await self._run_cmd(
                ["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"], None
            )
            if rc != 0 or not (out or "").strip():
                return
            iface_state: Optional[str] = None
            for line in out.splitlines():
                parts = line.strip().split(":", 1)
                if len(parts) == 2 and parts[0].strip() == self.wifi_iface:
                    iface_state = parts[1].strip().lower()
                    break
            if iface_state is not None and iface_state not in ("unavailable", "unmanaged", ""):
                return
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(1.0)

    async def start_ap_mode(self) -> Tuple[bool, str]:
        """Bring up the temporary Wi-Fi setup hotspot via NetworkManager."""
        if self.dry_run:
            self.ap_active = True
            return True, f"AP {self.ap_ssid} simulated on {self.ap_ip}"

        await self._run_cmd(["rfkill", "unblock", "wifi"], None)
        await self._run_cmd(["nmcli", "radio", "wifi", "on"], None)
        await self._ensure_regdom_for_ap()
        await self._wait_for_wifi_iface(timeout_s=15.0)
        if self.captive_dnsmasq_path is not None:
            try:
                self.captive_dnsmasq_path.parent.mkdir(parents=True, exist_ok=True)
                self.captive_dnsmasq_path.write_text(f"address=/#/{self.ap_ip}\n", encoding="utf-8")
            except OSError as exc:
                logger.debug("Could not write captive dnsmasq rule %s: %s", self.captive_dnsmasq_path, exc)
        await self._run_cmd(["nmcli", "connection", "delete", self.ap_con_name], None)

        rc, out, err = await self._run_cmd(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                self.wifi_iface,
                "con-name",
                self.ap_con_name,
                "autoconnect",
                "no",
                "ssid",
                self.ap_ssid,
                "--",
                "802-11-wireless.mode",
                "ap",
                "802-11-wireless.band",
                "bg",
                "802-11-wireless.channel",
                "6",
                "ipv4.method",
                "shared",
                "ipv4.addresses",
                f"{self.ap_ip}/24",
                "ipv6.method",
                "ignore",
            ],
            None,
        )
        if rc != 0:
            return False, (err or out or "nmcli add ap failed").strip()

        rc2, out2, err2 = await self._run_cmd(["nmcli", "connection", "up", self.ap_con_name], None)
        if rc2 != 0:
            return False, (err2 or out2 or "nmcli up ap failed").strip()

        self.ap_active = True
        self._notify_esp32_ap_ready()
        logger.info("First-boot AP '%s' active at http://%s", self.ap_ssid, self.ap_ip)
        return True, f"AP {self.ap_ssid} active at {self.ap_ip}"

    async def stop_ap_mode(self) -> None:
        if not self.dry_run:
            await self._run_cmd(["nmcli", "connection", "down", self.ap_con_name], None)
            await self._run_cmd(["nmcli", "connection", "delete", self.ap_con_name], None)
            if self.captive_dnsmasq_path is not None:
                try:
                    self.captive_dnsmasq_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.ap_active = False

    async def scan_wifi_networks(self) -> List[Dict[str, Any]]:
        return await self.sys_cfg.scan_wifi_networks()

    async def scan_bluetooth_devices(self, scan_seconds: int = 5) -> List[Dict[str, str]]:
        return await self.sys_cfg.scan_bluetooth_devices(scan_seconds=scan_seconds)

    async def connect_bluetooth_device(self, mac: str) -> Tuple[bool, str]:
        return await self.sys_cfg.connect_bluetooth_device(mac)

    # ------------------------------------------------------------------
    # Validation & Provisioning Execution
    # ------------------------------------------------------------------

    @staticmethod
    def validate_setup_payload(
        payload: Dict[str, Any], recovery_mode: bool = False
    ) -> Tuple[bool, str]:
        """Validate required fields (username, password, wifi_ssid). In recovery_mode only wifi_ssid is required."""
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        wifi_ssid = str(payload.get("wifi_ssid") or "").strip()

        if not recovery_mode or username or password:
            if not username:
                return False, "Missing required 'username'"
            if not _USERNAME_RE.match(username) or username == "root":
                return False, (
                    "Invalid 'username': use 1-32 lowercase letters, digits, '_' or '-' (cannot be 'root')"
                )
            if not password or len(password) < 4:
                return False, "Missing or too short 'password' (minimum 4 characters)"
        if not wifi_ssid:
            return False, "Missing required 'wifi_ssid'"

        hostname = str(payload.get("hostname") or "").strip()
        if hostname and not _HOSTNAME_RE.match(hostname):
            return False, "Invalid optional 'hostname' (alphanumeric and '-' only)"
        return True, ""

    async def _provision_linux_user(self, username: str, password: str) -> Tuple[bool, str]:
        if self.dry_run:
            return True, f"user '{username}' provisioned (dry-run)"

        rc_id, _, _ = await self._run_cmd(["id", "-u", username], None)
        if rc_id != 0:
            rc_add, out_add, err_add = await self._run_cmd(
                [
                    "useradd",
                    "-m",
                    "-s",
                    "/bin/bash",
                    "-G",
                    "sudo,dialout,audio,bluetooth",
                    username,
                ],
                None,
            )
            if rc_add != 0:
                return False, f"useradd failed: {(err_add or out_add).strip()}"
        else:
            await self._run_cmd(
                ["usermod", "-aG", "sudo,dialout,audio,bluetooth", username], None
            )

        rc_pw, out_pw, err_pw = await self._run_cmd(["chpasswd"], f"{username}:{password}\n")
        if rc_pw != 0:
            return False, f"chpasswd failed: {(err_pw or out_pw).strip()}"
        return True, f"user '{username}' ready"

    async def _connect_wifi(
        self, ssid: str, wifi_password: str, wifi_country: str
    ) -> Tuple[bool, str]:
        if self.dry_run:
            return True, f"connected to '{ssid}' (dry-run)"

        if wifi_country and re.fullmatch(r"[A-Za-z]{2}", wifi_country):
            await self._run_cmd(
                ["raspi-config", "nonint", "do_wifi_country", wifi_country.upper()], None
            )

        await self.stop_ap_mode()
        await asyncio.sleep(1.0)

        cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", self.wifi_iface]
        if wifi_password:
            cmd.extend(["password", wifi_password])
        rc, out, err = await self._run_cmd(cmd, None)
        if rc != 0:
            return False, f"Wi-Fi connect failed: {(err or out).strip()}"
        return True, f"connected to '{ssid}'"

    def _apply_optional_quixi_wallet_password(self, wallet_pw: str) -> bool:
        pw = (wallet_pw or "").strip()
        if not pw:
            return False
        lines: List[str] = []
        found = False
        if self.ixian_cfg_path.is_file():
            for line in self.ixian_cfg_path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("walletPassword"):
                    lines.append(f"walletPassword = {pw}")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"walletPassword = {pw}")
        self.ixian_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.ixian_cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(self.ixian_cfg_path, 0o600)
        except Exception:
            pass
        return True

    def _apply_optional_hub_setting(self, section: str, key: str, value: Any) -> bool:
        if value in (None, "") or not self.hub_cfg_path.is_file():
            return False
        cfg = yaml.safe_load(self.hub_cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        sec_cfg = cfg.setdefault(section, {})
        if isinstance(sec_cfg, dict):
            sec_cfg[key] = value
        self.hub_cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        return True

    def _apply_optional_server_address(self, server_addr: str) -> bool:
        return self._apply_optional_hub_setting("quixi", "server_ixian_address", (server_addr or "").strip())

    async def apply_setup(self, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """Execute required (user + wifi, or wifi-only in recovery_mode) and optional first-boot setup steps."""
        ok, err = self.validate_setup_payload(payload, recovery_mode=self.recovery_mode)
        if not ok:
            return False, {"ok": False, "error": err}

        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        wifi_ssid = str(payload["wifi_ssid"]).strip()
        wifi_password = str(payload.get("wifi_password") or "")
        wifi_country = str(payload.get("wifi_country") or "").strip().upper()

        hostname = str(payload.get("hostname") or "").strip()
        wallet_pw = str(payload.get("quixi_wallet_password") or "").strip()
        server_addr = str(payload.get("server_ixian_address") or "").strip()
        web_ui_token = str(payload.get("web_ui_auth_token") or "").strip()

        # 1. Required (or optional in recovery_mode): Linux user
        if username and password:
            u_ok, u_msg = await self._provision_linux_user(username, password)
            if not u_ok:
                return False, {"ok": False, "error": u_msg}
        else:
            u_msg = "kept existing user (Wi-Fi recovery mode)"

        # 2. Optional (skippable): hostname, SSH, raspi-config options, QuIXI walletPassword, server_ixian_address, web_ui_auth_token, bluetooth_mac
        skipped: List[str] = []
        applied_optional: Dict[str, Any] = {}

        raspi_payload: Dict[str, Any] = {}
        if hostname:
            raspi_payload["hostname"] = hostname
        else:
            skipped.append("hostname")

        if "enable_ssh" in payload or "ssh" in payload:
            raspi_payload["ssh"] = bool(payload.get("ssh", payload.get("enable_ssh", True)))
        else:
            skipped.append("ssh")

        for opt_key in (
            "timezone",
            "locale",
            "keyboard",
            "boot_behaviour",
            "boot_wait_network",
            "gpu_mem",
            "expand_rootfs",
            "interfaces",
        ):
            if payload.get(opt_key) not in (None, "", False):
                raspi_payload[opt_key] = payload[opt_key]
        for iface_key in RASPI_INTERFACE_KEYS:
            if iface_key != "ssh" and iface_key in payload and payload[iface_key] is not None:
                raspi_payload[iface_key] = bool(payload[iface_key])

        if raspi_payload:
            rc_res = await self.sys_cfg.apply_raspi_config(raspi_payload)
            applied_optional["raspi_config"] = rc_res.get("applied", {})
            if rc_res.get("errors"):
                applied_optional["raspi_config_errors"] = rc_res["errors"]

        if wallet_pw:
            self._apply_optional_quixi_wallet_password(wallet_pw)
            applied_optional["quixi_wallet_password_set"] = True
        else:
            skipped.append("quixi_wallet_password")

        if server_addr:
            self._apply_optional_server_address(server_addr)
            applied_optional["server_ixian_address"] = server_addr
        else:
            skipped.append("server_ixian_address")

        if web_ui_token:
            self._apply_optional_hub_setting("web_ui", "auth_token", web_ui_token)
            applied_optional["web_ui_auth_token_set"] = True
        else:
            skipped.append("web_ui_auth_token")

        bt_mac = str(payload.get("bluetooth_mac") or "").strip().upper()
        if bt_mac:
            bt_ok, bt_msg = await self.connect_bluetooth_device(bt_mac)
            if bt_ok:
                self._apply_optional_hub_setting("bluetooth", "speaker_mac", bt_mac)
            applied_optional["bluetooth"] = {"mac": bt_mac, "ok": bt_ok, "detail": bt_msg}
        else:
            skipped.append("bluetooth_mac")

        # 3. Required: Wi-Fi connection (tears down AP) + optional Static IPv4
        w_ok, w_msg = await self._connect_wifi(wifi_ssid, wifi_password, wifi_country)
        if not w_ok:
            # Re-enable AP so the user is not locked out if Wi-Fi credentials were wrong
            await self.start_ap_mode()
            return False, {"ok": False, "error": w_msg}

        ipv4_mode = str(payload.get("ipv4_mode") or "").strip().lower()
        if ipv4_mode in ("static", "manual"):
            ip_ok, ip_msg = await self.sys_cfg.configure_ipv4(
                connection_name=wifi_ssid,
                mode="manual",
                ip_cidr=str(payload.get("ipv4_address") or ""),
                gateway=str(payload.get("ipv4_gateway") or ""),
                dns=str(payload.get("ipv4_dns") or ""),
            )
            applied_optional["ipv4"] = {"ok": ip_ok, "detail": ip_msg}
        else:
            skipped.append("static_ipv4")

        # 4. Write sentinel (no plaintext passwords or tokens!)
        self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_data = {
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "username": username or "(existing)",
            "wifi_ssid": wifi_ssid,
            "recovery_mode": self.recovery_mode,
            "skipped_optional": skipped,
        }
        self.sentinel_path.write_text(json.dumps(sentinel_data, indent=2), encoding="utf-8")

        summary = {
            "ok": True,
            "username": username or "(existing)",
            "wifi_ssid": wifi_ssid,
            "recovery_mode": self.recovery_mode,
            "user_status": u_msg,
            "wifi_status": w_msg,
            "applied_optional": applied_optional,
            "skipped_optional": skipped,
        }
        self.last_setup_summary = summary
        self.setup_complete_event.set()
        return True, summary

    # ------------------------------------------------------------------
    # HTTP Server & Captive Portal Routes
    # ------------------------------------------------------------------

    async def handle_api_request(
        self, method: str, path: str, body: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, Dict[str, Any]]:
        parsed = urllib.parse.urlsplit(path)
        route = parsed.path.rstrip("/") or "/"
        payload = body if isinstance(body, dict) else {}

        if method == "GET" and route == "/api/setup/status":
            return 200, {
                "ok": True,
                "first_boot_needed": self.is_first_boot_needed(),
                "recovery_mode": self.recovery_mode,
                "ap_active": self.ap_active,
                "ap_ssid": self.ap_ssid,
                "ap_ip": self.ap_ip,
                "required_fields": ["wifi_ssid"] if self.recovery_mode else ["username", "password", "wifi_ssid"],
                "skippable_fields": [
                    "bluetooth_mac",
                    "web_ui_auth_token",
                    "hostname",
                    "quixi_wallet_password",
                    "server_ixian_address",
                    "enable_ssh",
                ],
            }

        if method == "GET" and route == "/api/setup/wifi":
            nets = await self.scan_wifi_networks()
            return 200, {"ok": True, "networks": nets}

        if method == "GET" and route == "/api/setup/bluetooth":
            devices = await self.scan_bluetooth_devices()
            return 200, {"ok": True, "devices": devices}

        if method == "POST" and route == "/api/setup/bluetooth/connect":
            bt_ok, bt_msg = await self.connect_bluetooth_device(str(payload.get("mac") or ""))
            return (200 if bt_ok else 400), {"ok": bt_ok, "detail": bt_msg, "error": "" if bt_ok else bt_msg}

        if method == "POST" and route == "/api/setup/apply":
            ok, res = await self.apply_setup(payload)
            return (200 if ok else 400), res

        return 404, {"ok": False, "error": f"Not found: {method} {route}"}

    async def start(self) -> int:
        if self._server is not None:
            return self.bound_port
        self._server = await asyncio.start_server(
            self._handle_connection, host=self.host, port=self.port
        )
        sockets = getattr(self._server, "sockets", None) or []
        if sockets:
            sockname = sockets[0].getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                self.bound_port = int(sockname[1])
        logger.info("First-boot setup portal listening on http://%s:%d", self.host, self.bound_port)
        return self.bound_port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            req_line_b = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not req_line_b:
                writer.close()
                return
            parts = req_line_b.decode("latin-1", errors="replace").strip().split(" ")
            if len(parts) < 2:
                writer.close()
                return
            method, path = parts[0].upper(), parts[1]

            headers: Dict[str, str] = {}
            while True:
                line_b = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if not line_b or line_b in (b"\r\n", b"\n"):
                    break
                line_s = line_b.decode("latin-1", errors="replace").strip()
                if ":" in line_s:
                    k, v = line_s.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            parsed = urllib.parse.urlsplit(path)
            route = parsed.path.rstrip("/") or "/"

            # Captive portal detection probes -> serve setup page or redirect to portal root
            if method == "GET" and route in (
                "/",
                "/index.html",
                "/generate_204",
                "/hotspot-detect.html",
                "/ncsi.txt",
                "/connecttest.txt",
            ):
                html_b = FIRST_BOOT_HTML.encode("utf-8")
                await self._write_http_response(writer, 200, html_b, "text/html; charset=utf-8")
                return

            content_len = int(headers.get("content-length", "0") or "0")
            body_obj: Optional[Dict[str, Any]] = None
            if content_len > 0:
                raw_body = await asyncio.wait_for(reader.readexactly(content_len), timeout=15.0)
                try:
                    parsed_body = json.loads(raw_body.decode("utf-8", errors="replace"))
                    if isinstance(parsed_body, dict):
                        body_obj = parsed_body
                except Exception:
                    body_obj = None

            status_code, resp_dict = await self.handle_api_request(method, path, body=body_obj)
            resp_bytes = json.dumps(resp_dict, default=str).encode("utf-8")
            await self._write_http_response(writer, status_code, resp_bytes, "application/json")
        except Exception as exc:
            logger.debug("First-boot HTTP connection closed: %s", exc)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    async def _write_http_response(
        writer: asyncio.StreamWriter, status: int, body: bytes, content_type: str
    ) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        hdr = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(hdr + body)
        await writer.drain()


FIRST_BOOT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Alf Pi Setup · First-Boot AP</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #090b10; color: #f3f5fa; padding: 20px; max-width: 520px; margin: 0 auto;
  }
  h1 { font-size: 1.25rem; color: #38bdf8; margin-bottom: 6px; }
  p.sub { font-size: 0.84rem; color: #a8b3cf; margin-bottom: 16px; }
  .card {
    background: #111520; border: 1px solid #2b354d; border-radius: 12px;
    padding: 15px; margin-bottom: 14px; display: flex; flex-direction: column; gap: 10px;
  }
  .hdr { display: flex; justify-content: space-between; align-items: center; }
  .hdr h2 { font-size: 0.95rem; color: #f3f5fa; }
  .tag { font-size: 0.7rem; padding: 2px 8px; border-radius: 99px; font-weight: 600; }
  .req { background: rgba(56,189,248,0.18); color: #38bdf8; border: 1px solid #38bdf8; }
  .opt { background: rgba(168,179,207,0.12); color: #a8b3cf; border: 1px solid #2b354d; }
  label { font-size: 0.78rem; color: #a8b3cf; display: block; margin-bottom: 4px; }
  input, select {
    width: 100%; background: #181e2c; border: 1px solid #2b354d; color: #f3f5fa;
    border-radius: 8px; padding: 9px 10px; font-size: 0.88rem;
  }
  .row { display: flex; gap: 8px; }
  .btn {
    background: #222a3d; color: #f3f5fa; border: 1px solid #2b354d;
    border-radius: 8px; padding: 9px 14px; font-size: 0.85rem; cursor: pointer;
  }
  .btn-primary {
    background: linear-gradient(135deg, #38bdf8, #34d399); color: #060911;
    border: none; font-weight: 700; width: 100%; padding: 12px; font-size: 0.95rem;
  }
  #statusMsg { font-size: 0.83rem; margin-top: 10px; color: #34d399; white-space: pre-wrap; }
  .err { color: #f87171 !important; }
</style>
</head>
<body>
  <h1>Alf Pi Zero 2 W · First-Boot Setup</h1>
  <p class="sub">Create your Pi user and connect to Wi-Fi. All other settings can be skipped and configured later in the Pi Web GUI (:8082).</p>

  <div class="card">
    <div class="hdr"><h2>1 · Pi User Account</h2><span class="tag req">Required</span></div>
    <div><label>Username</label><input id="username" type="text" placeholder="e.g. pi or alex" autocomplete="username" /></div>
    <div><label>Password (min 4 chars)</label><input id="password" type="password" placeholder="Enter Linux user password" autocomplete="new-password" /></div>
  </div>

  <div class="card">
    <div class="hdr"><h2>2 · Wi-Fi Network</h2><span class="tag req">Required</span></div>
    <div class="row">
      <select id="wifiSelect" onchange="pickWifi()" style="flex:1;"><option value="">-- Tap Scan or type SSID below --</option></select>
      <button class="btn" onclick="scanWifi()">Scan</button>
    </div>
    <div><label>Wi-Fi SSID</label><input id="wifiSsid" type="text" placeholder="Network Name (SSID)" /></div>
    <div class="row">
      <div style="flex:2;"><label>Wi-Fi Password</label><input id="wifiPass" type="password" placeholder="Leave blank if open Wi-Fi" /></div>
      <div style="flex:1;"><label>Country</label><input id="wifiCountry" type="text" value="US" maxlength="2" /></div>
    </div>
  </div>

  <div class="card">
    <div class="hdr"><h2>3 · Bluetooth Speaker</h2><span class="tag opt">Optional · Skippable</span></div>
    <div class="row">
      <select id="btSelect" onchange="pickBt()" style="flex:1;"><option value="">-- Put speaker in pairing mode &amp; tap Scan --</option></select>
      <button class="btn" onclick="scanBt()">Scan</button>
    </div>
    <div class="row">
      <input id="btMac" type="text" placeholder="Speaker MAC (e.g. AA:BB:CC:DD:EE:FF)" style="flex:1;" />
      <button class="btn" onclick="connectBt()">Pair &amp; Connect</button>
    </div>
    <div id="btStatus" style="font-size:0.78rem;color:#a8b3cf;"></div>
  </div>

  <div class="card">
    <div class="hdr"><h2>4 · Network IPv4, raspi-config &amp; QuIXI</h2><span class="tag opt">Optional · Skippable</span></div>
    <div><label>Hostname (optional)</label><input id="hostname" type="text" placeholder="tamagci (skip to keep default)" /></div>
    <div><label>IPv4 Mode</label>
      <select id="ipv4Mode" onchange="toggleStaticIp()">
        <option value="auto">DHCP (Automatic)</option>
        <option value="manual">Static IPv4</option>
      </select>
    </div>
    <div id="staticIpFields" style="display:none;flex-direction:column;gap:8px;">
      <input id="ipv4Addr" type="text" placeholder="Static IP/CIDR (e.g. 192.168.1.50/24)" />
      <div class="row">
        <input id="ipv4Gw" type="text" placeholder="Gateway (192.168.1.1)" />
        <input id="ipv4Dns" type="text" placeholder="DNS (1.1.1.1 8.8.8.8)" />
      </div>
    </div>
    <details>
      <summary style="cursor:pointer;font-size:0.84rem;color:#38bdf8;">raspi-config Options (Interfaces, Localization, GPU, Boot)</summary>
      <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px;">
        <div class="row">
          <input id="rcTimezone" type="text" placeholder="Timezone (e.g. Europe/Berlin)" />
          <input id="rcLocale" type="text" placeholder="Locale (e.g. en_US.UTF-8)" />
        </div>
        <div class="row">
          <input id="rcKeyboard" type="text" placeholder="Keyboard (e.g. us, de)" />
          <select id="rcGpuMem">
            <option value="">GPU Mem (default 16M)</option>
            <option value="16">16 MB (Headless)</option>
            <option value="32">32 MB</option>
            <option value="64">64 MB</option>
            <option value="128">128 MB</option>
          </select>
        </div>
        <div class="row">
          <select id="rcBootBeh">
            <option value="">Boot Behaviour (skip)</option>
            <option value="B1">B1 · Console</option>
            <option value="B2">B2 · Console Autologin</option>
            <option value="B3">B3 · Desktop</option>
            <option value="B4">B4 · Desktop Autologin</option>
          </select>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:0.8rem;">
          <label><input type="checkbox" id="rcSsh" checked /> SSH</label>
          <label><input type="checkbox" id="rcVnc" /> VNC</label>
          <label><input type="checkbox" id="rcSpi" /> SPI</label>
          <label><input type="checkbox" id="rcI2c" /> I2C</label>
          <label><input type="checkbox" id="rcSerialHw" /> Serial HW</label>
          <label><input type="checkbox" id="rcSerialCons" /> Serial Console</label>
          <label><input type="checkbox" id="rcOnewire" /> 1-Wire</label>
          <label><input type="checkbox" id="rcRgpio" /> Remote GPIO</label>
          <label><input type="checkbox" id="rcBootWait" /> Wait for Network</label>
          <label><input type="checkbox" id="rcExpandFs" /> Expand Rootfs</label>
        </div>
      </div>
    </details>
    <div><label>Pi Web GUI Auth Token (optional, for LAN protection on :8082)</label>
      <div class="row">
        <input id="webUiToken" type="text" placeholder="Leave blank for open LAN access" style="flex:1;" />
        <button class="btn" onclick="genToken()">Generate</button>
      </div>
    </div>
    <div><label>QuIXI Wallet Password (optional)</label><input id="walletPw" type="password" placeholder="Skip to set later at :8082" /></div>
    <div><label>AI Server Ixian Address (optional)</label><input id="serverAddr" type="text" placeholder="Skip to pair later at :8082" /></div>
  </div>

  <button class="btn btn-primary" onclick="submitSetup(false)">Save &amp; Connect Wi-Fi</button>
  <button class="btn" style="width:100%;margin-top:8px;" onclick="submitSetup(true)">Skip Optional Settings &amp; Connect</button>
  <div id="statusMsg"></div>

<script>
  function genToken() {
    const arr = new Uint8Array(16);
    crypto.getRandomValues(arr);
    document.getElementById("webUiToken").value = Array.from(arr, b => b.toString(16).padStart(2, "0")).join("");
  }
  function toggleStaticIp() {
    document.getElementById("staticIpFields").style.display =
      document.getElementById("ipv4Mode").value === "manual" ? "flex" : "none";
  }
  async function scanWifi() {
    const sel = document.getElementById("wifiSelect");
    sel.innerHTML = "<option>Scanning...</option>";
    const r = await (await fetch("/api/setup/wifi")).json();
    sel.innerHTML = '<option value="">-- Select Wi-Fi --</option>';
    (r.networks || []).forEach(n => {
      const o = document.createElement("option");
      o.value = n.ssid;
      o.textContent = `${n.ssid} (${n.signal}% · ${n.security})`;
      sel.appendChild(o);
    });
  }
  function pickWifi() {
    const v = document.getElementById("wifiSelect").value;
    if (v) document.getElementById("wifiSsid").value = v;
  }
  async function scanBt() {
    const sel = document.getElementById("btSelect");
    const st = document.getElementById("btStatus");
    sel.innerHTML = "<option>Scanning (~5s)...</option>";
    st.textContent = "Scanning for Bluetooth speakers...";
    const r = await (await fetch("/api/setup/bluetooth")).json();
    sel.innerHTML = '<option value="">-- Select Bluetooth Speaker --</option>';
    (r.devices || []).forEach(d => {
      const o = document.createElement("option");
      o.value = d.mac;
      o.textContent = `${d.name} (${d.mac})`;
      sel.appendChild(o);
    });
    st.textContent = `Found ${(r.devices || []).length} Bluetooth device(s).`;
  }
  function pickBt() {
    const v = document.getElementById("btSelect").value;
    if (v) document.getElementById("btMac").value = v;
  }
  async function connectBt() {
    const mac = document.getElementById("btMac").value.trim();
    const st = document.getElementById("btStatus");
    st.style.color = "#a8b3cf";
    st.textContent = "Pairing & connecting Bluetooth speaker...";
    const res = await fetch("/api/setup/bluetooth/connect", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mac })
    });
    const data = await res.json();
    st.style.color = data.ok ? "#34d399" : "#f87171";
    st.textContent = data.ok ? ("Connected: " + (data.detail || mac)) : ("Error: " + (data.error || "Bluetooth connect failed"));
  }
  async function submitSetup(skipOptional) {
    const msg = document.getElementById("statusMsg");
    msg.className = "";
    msg.textContent = "Applying setup and connecting to Wi-Fi...";
    const body = {
      username: document.getElementById("username").value.trim(),
      password: document.getElementById("password").value,
      wifi_ssid: document.getElementById("wifiSsid").value.trim(),
      wifi_password: document.getElementById("wifiPass").value,
      wifi_country: document.getElementById("wifiCountry").value.trim(),
      bluetooth_mac: skipOptional ? "" : document.getElementById("btMac").value.trim(),
      web_ui_auth_token: skipOptional ? "" : document.getElementById("webUiToken").value.trim(),
      hostname: skipOptional ? "" : document.getElementById("hostname").value.trim(),
      ipv4_mode: skipOptional ? "auto" : document.getElementById("ipv4Mode").value,
      ipv4_address: skipOptional ? "" : document.getElementById("ipv4Addr").value.trim(),
      ipv4_gateway: skipOptional ? "" : document.getElementById("ipv4Gw").value.trim(),
      ipv4_dns: skipOptional ? "" : document.getElementById("ipv4Dns").value.trim(),
      timezone: skipOptional ? "" : document.getElementById("rcTimezone").value.trim(),
      locale: skipOptional ? "" : document.getElementById("rcLocale").value.trim(),
      keyboard: skipOptional ? "" : document.getElementById("rcKeyboard").value.trim(),
      gpu_mem: skipOptional ? "" : document.getElementById("rcGpuMem").value,
      boot_behaviour: skipOptional ? "" : document.getElementById("rcBootBeh").value,
      ssh: skipOptional ? true : document.getElementById("rcSsh").checked,
      vnc: skipOptional ? null : document.getElementById("rcVnc").checked,
      spi: skipOptional ? null : document.getElementById("rcSpi").checked,
      i2c: skipOptional ? null : document.getElementById("rcI2c").checked,
      serial_hw: skipOptional ? null : document.getElementById("rcSerialHw").checked,
      serial_cons: skipOptional ? null : document.getElementById("rcSerialCons").checked,
      onewire: skipOptional ? null : document.getElementById("rcOnewire").checked,
      rgpio: skipOptional ? null : document.getElementById("rcRgpio").checked,
      boot_wait_network: skipOptional ? null : document.getElementById("rcBootWait").checked,
      expand_rootfs: skipOptional ? false : document.getElementById("rcExpandFs").checked,
      quixi_wallet_password: skipOptional ? "" : document.getElementById("walletPw").value.trim(),
      server_ixian_address: skipOptional ? "" : document.getElementById("serverAddr").value.trim(),
      enable_ssh: true
    };
    const res = await fetch("/api/setup/apply", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!data.ok) {
      msg.className = "err";
      msg.textContent = "Error: " + (data.error || "Setup failed");
      return;
    }
    const tokSuffix = body.web_ui_auth_token ? `/?token=${encodeURIComponent(body.web_ui_auth_token)}` : "";
    msg.textContent = `Setup complete! Connecting to '${data.wifi_ssid}' for user '${data.username}'. You can reconnect to your normal Wi-Fi and open http://${body.hostname || "tamagci"}.local:8082${tokSuffix}`;
  }
  window.addEventListener("DOMContentLoaded", scanWifi);
</script>
</body>
</html>
"""


async def run_first_boot_portal() -> None:
    portal = FirstBootAPPortal()
    if not portal.is_first_boot_needed():
        if await portal.has_active_wifi_connection(wait_s=45.0):
            logger.info("First-boot sentinel %s exists and Wi-Fi is connected; skipping AP setup.", portal.sentinel_path)
            return
        logger.warning("First-boot sentinel exists but no active Wi-Fi after 45s; entering Wi-Fi recovery AP mode.")
        portal.recovery_mode = True

    ok, detail = await portal.start_ap_mode()
    if not ok:
        logger.warning("Could not start NetworkManager AP on initial attempt: %s", detail)

    async def _retry_ap_loop() -> None:
        while not portal.ap_active and not portal.setup_complete_event.is_set():
            await asyncio.sleep(5.0)
            if portal.ap_active or portal.setup_complete_event.is_set():
                break
            r_ok, r_detail = await portal.start_ap_mode()
            if not r_ok:
                logger.warning("Retrying NetworkManager AP failed: %s", r_detail)

    retry_task = asyncio.create_task(_retry_ap_loop()) if not ok else None
    await portal.start()
    try:
        await portal.setup_complete_event.wait()
        await asyncio.sleep(1.5)
    finally:
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()
        await portal.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run_first_boot_portal())
