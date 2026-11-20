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
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import yaml

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

    # ------------------------------------------------------------------
    # NetworkManager AP + Wi-Fi Helpers
    # ------------------------------------------------------------------

    async def start_ap_mode(self) -> Tuple[bool, str]:
        """Bring up the temporary Wi-Fi setup hotspot via NetworkManager."""
        if self.dry_run:
            self.ap_active = True
            return True, f"AP {self.ap_ssid} simulated on {self.ap_ip}"

        await self._run_cmd(["rfkill", "unblock", "wifi"], None)
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
                "ipv4.method",
                "shared",
                "ipv4.addresses",
                f"{self.ap_ip}/24",
            ],
            None,
        )
        if rc != 0:
            return False, (err or out or "nmcli add ap failed").strip()

        rc2, out2, err2 = await self._run_cmd(["nmcli", "connection", "up", self.ap_con_name], None)
        if rc2 != 0:
            return False, (err2 or out2 or "nmcli up ap failed").strip()

        self.ap_active = True
        logger.info("First-boot AP '%s' active at http://%s", self.ap_ssid, self.ap_ip)
        return True, f"AP {self.ap_ssid} active at {self.ap_ip}"

    async def stop_ap_mode(self) -> None:
        if not self.dry_run:
            await self._run_cmd(["nmcli", "connection", "down", self.ap_con_name], None)
            await self._run_cmd(["nmcli", "connection", "delete", self.ap_con_name], None)
        self.ap_active = False

    async def scan_wifi_networks(self) -> List[Dict[str, Any]]:
        return await self.sys_cfg.scan_wifi_networks()

    # ------------------------------------------------------------------
    # Validation & Provisioning Execution
    # ------------------------------------------------------------------

    @staticmethod
    def validate_setup_payload(payload: Dict[str, Any]) -> Tuple[bool, str]:
        """Validate required fields (username, password, wifi_ssid). All other fields are optional/skippable."""
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        wifi_ssid = str(payload.get("wifi_ssid") or "").strip()

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

    def _apply_optional_server_address(self, server_addr: str) -> bool:
        addr = (server_addr or "").strip()
        if not addr or not self.hub_cfg_path.is_file():
            return False
        cfg = yaml.safe_load(self.hub_cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        q_cfg = cfg.setdefault("quixi", {})
        if isinstance(q_cfg, dict):
            q_cfg["server_ixian_address"] = addr
        self.hub_cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        return True

    async def apply_setup(self, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """Execute required (user + wifi) and optional (skippable) first-boot setup steps."""
        ok, err = self.validate_setup_payload(payload)
        if not ok:
            return False, {"ok": False, "error": err}

        username = str(payload["username"]).strip()
        password = str(payload["password"])
        wifi_ssid = str(payload["wifi_ssid"]).strip()
        wifi_password = str(payload.get("wifi_password") or "")
        wifi_country = str(payload.get("wifi_country") or "").strip().upper()

        hostname = str(payload.get("hostname") or "").strip()
        wallet_pw = str(payload.get("quixi_wallet_password") or "").strip()
        server_addr = str(payload.get("server_ixian_address") or "").strip()
        enable_ssh = bool(payload.get("enable_ssh", True))

        # 1. Required: Linux user
        u_ok, u_msg = await self._provision_linux_user(username, password)
        if not u_ok:
            return False, {"ok": False, "error": u_msg}

        # 2. Optional (skippable): hostname, SSH, raspi-config options, QuIXI walletPassword, server_ixian_address
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

        # 4. Write sentinel (no plaintext passwords!)
        self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_data = {
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "username": username,
            "wifi_ssid": wifi_ssid,
            "skipped_optional": skipped,
        }
        self.sentinel_path.write_text(json.dumps(sentinel_data, indent=2), encoding="utf-8")

        summary = {
            "ok": True,
            "username": username,
            "wifi_ssid": wifi_ssid,
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
                "ap_active": self.ap_active,
                "ap_ssid": self.ap_ssid,
                "ap_ip": self.ap_ip,
                "required_fields": ["username", "password", "wifi_ssid"],
                "skippable_fields": [
                    "hostname",
                    "quixi_wallet_password",
                    "server_ixian_address",
                    "enable_ssh",
                ],
            }

        if method == "GET" and route == "/api/setup/wifi":
            nets = await self.scan_wifi_networks()
            return 200, {"ok": True, "networks": nets}

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
    <div class="hdr"><h2>3 · Network IPv4, raspi-config &amp; QuIXI</h2><span class="tag opt">Optional · Skippable</span></div>
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
    <div><label>QuIXI Wallet Password (optional)</label><input id="walletPw" type="password" placeholder="Skip to set later at :8082" /></div>
    <div><label>AI Server Ixian Address (optional)</label><input id="serverAddr" type="text" placeholder="Skip to pair later at :8082" /></div>
  </div>

  <button class="btn btn-primary" onclick="submitSetup(false)">Save &amp; Connect Wi-Fi</button>
  <button class="btn" style="width:100%;margin-top:8px;" onclick="submitSetup(true)">Skip Optional Settings &amp; Connect</button>
  <div id="statusMsg"></div>

<script>
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
    msg.textContent = `Setup complete! Connecting to '${data.wifi_ssid}' for user '${data.username}'. You can reconnect to your normal Wi-Fi and open http://${body.hostname || "tamagci"}.local:8082`;
  }
  window.addEventListener("DOMContentLoaded", scanWifi);
</script>
</body>
</html>
"""


async def run_first_boot_portal() -> None:
    portal = FirstBootAPPortal()
    if not portal.is_first_boot_needed():
        logger.info("First-boot sentinel %s exists; skipping AP setup.", portal.sentinel_path)
        return

    ok, detail = await portal.start_ap_mode()
    if not ok:
        logger.warning("Could not start NetworkManager AP: %s", detail)
    await portal.start()
    try:
        await portal.setup_complete_event.wait()
        await asyncio.sleep(1.5)
    finally:
        await portal.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run_first_boot_portal())
