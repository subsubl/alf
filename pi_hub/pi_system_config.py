"""
Shared Raspberry Pi Network & `raspi-config` System Configuration Manager (Tier 2).

Used by BOTH:
  1. `pi_hub/first_boot_setup.py` (First-Boot AP Setup Portal on :80)
  2. `pi_hub/pi_web_gui.py` (Live Raspberry Pi Hub Web GUI on :8082)

Supports:
  - Wi-Fi scanning, connecting, saved connection listing/forgetting, active IP/gateway/DNS status
  - IPv4 DHCP (`auto`) vs Static IP (`manual` with CIDR, gateway, DNS)
  - All core `raspi-config nonint` options:
      * System/Localization: hostname, timezone, locale, keyboard, wifi_country, boot_behaviour,
        boot_wait_network, expand_rootfs
      * Interfaces: ssh, vnc, spi, i2c, serial_hw, serial_cons, onewire, rgpio
      * Performance: gpu_mem (16/32/64/128)
  - Power actions: reboot, shutdown
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("pi_hub.system_config")

CmdRunnerFn = Callable[[List[str], Optional[str]], Awaitable[Tuple[int, str, str]]]

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$")
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
_CIDR_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./+-]{1,64}$")

# raspi-config nonint interface toggles: 0 = enabled, 1 = disabled
RASPI_INTERFACE_KEYS = (
    "ssh",
    "vnc",
    "spi",
    "i2c",
    "serial_hw",
    "serial_cons",
    "onewire",
    "rgpio",
)
_RASPI_INTERFACE_CMD = {
    "ssh": "do_ssh",
    "vnc": "do_vnc",
    "spi": "do_spi",
    "i2c": "do_i2c",
    "serial_hw": "do_serial_hw",
    "serial_cons": "do_serial_cons",
    "onewire": "do_onewire",
    "rgpio": "do_rgpio",
}
_RASPI_INTERFACE_GET = {
    "ssh": "get_ssh",
    "vnc": "get_vnc",
    "spi": "get_spi",
    "i2c": "get_i2c",
    "serial_hw": "get_serial_hw",
    "serial_cons": "get_serial_cons",
    "onewire": "get_onewire",
    "rgpio": "get_rgpio",
}


async def default_cmd_runner(
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


def parse_nmcli_connections(raw_output: str) -> List[Dict[str, str]]:
    """Parse `nmcli -t -f NAME,TYPE,DEVICE connection show`."""
    conns: List[Dict[str, str]] = []
    for raw_line in (raw_output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = re.split(r"(?<!\\):", line)
        if len(parts) >= 2:
            conns.append(
                {
                    "name": parts[0].replace("\\:", ":").strip(),
                    "type": parts[1].strip(),
                    "device": parts[2].strip() if len(parts) >= 3 else "",
                }
            )
    return conns


class PiSystemConfigManager:
    """Unified Network + `raspi-config` controller for Raspberry Pi Zero 2 W."""

    def __init__(
        self,
        wifi_iface: str = "wlan0",
        cmd_runner: Optional[CmdRunnerFn] = None,
        dry_run: bool = False,
        use_sudo: bool = False,
    ) -> None:
        self.wifi_iface = wifi_iface
        self._raw_runner: CmdRunnerFn = cmd_runner or default_cmd_runner
        self.dry_run = bool(dry_run)
        self.use_sudo = bool(use_sudo)

    async def run_cmd(
        self, cmd: List[str], stdin_text: Optional[str] = None, privileged: bool = False
    ) -> Tuple[int, str, str]:
        actual = list(cmd)
        if (
            privileged
            and self.use_sudo
            and hasattr(os, "geteuid")
            and os.geteuid() != 0
            and actual
            and actual[0] != "sudo"
        ):
            actual = ["sudo", "-n", *actual]
        return await self._raw_runner(actual, stdin_text)

    # ------------------------------------------------------------------
    # Network Status, Wi-Fi Scan/Connect, DHCP vs Static IP
    # ------------------------------------------------------------------

    async def scan_wifi_networks(self) -> List[Dict[str, Any]]:
        if self.dry_run:
            return [
                {"ssid": "HomeWiFi", "signal": 88, "security": "WPA2"},
                {"ssid": "Studio-2.4G", "signal": 62, "security": "WPA2"},
            ]
        await self.run_cmd(
            ["nmcli", "device", "wifi", "rescan", "ifname", self.wifi_iface],
            privileged=True,
        )
        rc, out, _ = await self.run_cmd(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        )
        if rc != 0:
            return []
        return parse_nmcli_wifi_scan(out)

    async def get_network_status(self) -> Dict[str, Any]:
        if self.dry_run:
            return {
                "ok": True,
                "iface": self.wifi_iface,
                "active_connection": "HomeWiFi",
                "ipv4_address": "192.168.1.42/24",
                "gateway": "192.168.1.1",
                "dns": "1.1.1.1",
                "connections": [{"name": "HomeWiFi", "type": "802-11-wireless", "device": self.wifi_iface}],
            }
        rc, dev_out, _ = await self.run_cmd(
            [
                "nmcli",
                "-t",
                "-f",
                "GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
                "device",
                "show",
                self.wifi_iface,
            ]
        )
        info: Dict[str, Any] = {
            "ok": rc == 0,
            "iface": self.wifi_iface,
            "active_connection": "",
            "ipv4_address": "",
            "gateway": "",
            "dns": "",
            "connections": [],
        }
        if rc == 0:
            for line in dev_out.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                v = v.strip()
                if k == "GENERAL.CONNECTION" and v != "--":
                    info["active_connection"] = v
                elif k.startswith("IP4.ADDRESS") and not info["ipv4_address"]:
                    info["ipv4_address"] = v
                elif k == "IP4.GATEWAY" and v != "--":
                    info["gateway"] = v
                elif k.startswith("IP4.DNS") and not info["dns"]:
                    info["dns"] = v

        rc_c, con_out, _ = await self.run_cmd(
            ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show"]
        )
        if rc_c == 0:
            info["connections"] = parse_nmcli_connections(con_out)
        return info

    async def configure_ipv4(
        self,
        connection_name: str,
        mode: str = "auto",
        ip_cidr: str = "",
        gateway: str = "",
        dns: str = "",
    ) -> Tuple[bool, str]:
        con = (connection_name or "").strip()
        if not con:
            net = await self.get_network_status()
            con = str(net.get("active_connection") or "").strip()
        if not con:
            return False, "No active or specified NetworkManager connection"

        m = (mode or "auto").strip().lower()
        if m in ("dhcp", "auto"):
            if self.dry_run:
                return True, f"IPv4 set to DHCP on '{con}' (dry-run)"
            rc, out, err = await self.run_cmd(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    con,
                    "ipv4.method",
                    "auto",
                    "ipv4.addresses",
                    "",
                    "ipv4.gateway",
                    "",
                    "ipv4.dns",
                    "",
                ],
                privileged=True,
            )
            if rc != 0:
                return False, (err or out or "nmcli modify dhcp failed").strip()
            await self.run_cmd(["nmcli", "connection", "up", con], privileged=True)
            return True, f"IPv4 set to DHCP on '{con}'"

        if m in ("static", "manual"):
            cidr = (ip_cidr or "").strip()
            gw = (gateway or "").strip()
            dns_s = (dns or "").strip()
            if not _CIDR_RE.match(cidr):
                return False, "Static IP must be in CIDR format, e.g. '192.168.1.50/24'"
            if gw and not _IPV4_RE.match(gw):
                return False, "Invalid IPv4 gateway address"
            if self.dry_run:
                return True, f"Static IP {cidr} set on '{con}' (dry-run)"
            cmd = [
                "nmcli",
                "connection",
                "modify",
                con,
                "ipv4.method",
                "manual",
                "ipv4.addresses",
                cidr,
            ]
            if gw:
                cmd.extend(["ipv4.gateway", gw])
            if dns_s:
                cmd.extend(["ipv4.dns", dns_s])
            rc, out, err = await self.run_cmd(cmd, privileged=True)
            if rc != 0:
                return False, (err or out or "nmcli modify static failed").strip()
            await self.run_cmd(["nmcli", "connection", "up", con], privileged=True)
            return True, f"Static IP {cidr} applied on '{con}'"

        return False, f"Unknown IPv4 mode '{mode}' (use 'auto' or 'manual')"

    async def connect_wifi(
        self, ssid: str, password: str = "", country: str = ""
    ) -> Tuple[bool, str]:
        ssid_s = (ssid or "").strip()
        if not ssid_s:
            return False, "Missing Wi-Fi SSID"
        if self.dry_run:
            return True, f"Connected to '{ssid_s}' (dry-run)"
        if country and _COUNTRY_RE.match(country.strip()):
            await self.run_cmd(
                ["raspi-config", "nonint", "do_wifi_country", country.strip().upper()],
                privileged=True,
            )
        cmd = ["nmcli", "device", "wifi", "connect", ssid_s, "ifname", self.wifi_iface]
        if password:
            cmd.extend(["password", password])
        rc, out, err = await self.run_cmd(cmd, privileged=True)
        if rc != 0:
            return False, (err or out or "nmcli wifi connect failed").strip()
        return True, f"Connected to '{ssid_s}'"

    async def forget_connection(self, name: str) -> Tuple[bool, str]:
        con = (name or "").strip()
        if not con:
            return False, "Missing connection name"
        if self.dry_run:
            return True, f"Removed connection '{con}' (dry-run)"
        rc, out, err = await self.run_cmd(
            ["nmcli", "connection", "delete", con], privileged=True
        )
        if rc != 0:
            return False, (err or out or "nmcli delete failed").strip()
        return True, f"Removed connection '{con}'"

    # ------------------------------------------------------------------
    # `raspi-config nonint` Read & Apply
    # ------------------------------------------------------------------

    async def get_raspi_config_state(self) -> Dict[str, Any]:
        """Query current `raspi-config nonint` settings (interfaces, hostname, wifi country, gpu_mem)."""
        state: Dict[str, Any] = {
            "hostname": "",
            "wifi_country": "",
            "timezone": "",
            "interfaces": {},
        }
        if self.dry_run:
            state["hostname"] = "tamagci"
            state["wifi_country"] = "US"
            state["timezone"] = "UTC"
            state["interfaces"] = {k: (k == "ssh") for k in RASPI_INTERFACE_KEYS}
            return state

        rc_h, out_h, _ = await self.run_cmd(["hostname"])
        if rc_h == 0:
            state["hostname"] = out_h.strip()

        rc_wc, out_wc, _ = await self.run_cmd(["raspi-config", "nonint", "get_wifi_country"])
        if rc_wc == 0:
            state["wifi_country"] = out_wc.strip()

        rc_tz, out_tz, _ = await self.run_cmd(["timedatectl", "show", "-p", "Timezone", "--value"])
        if rc_tz == 0:
            state["timezone"] = out_tz.strip()

        for key, getter in _RASPI_INTERFACE_GET.items():
            rc_i, out_i, _ = await self.run_cmd(["raspi-config", "nonint", getter])
            if rc_i == 0 and out_i.strip() in ("0", "1"):
                # raspi-config nonint returns 0 when enabled, 1 when disabled
                state["interfaces"][key] = out_i.strip() == "0"
        return state

    async def apply_raspi_config(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Apply any subset of `raspi-config nonint` options."""
        applied: Dict[str, Any] = {}
        errors: Dict[str, str] = {}

        if not isinstance(params, dict):
            return {"ok": False, "error": "Expected JSON object"}

        # 1. Hostname
        hostname = str(params.get("hostname") or "").strip()
        if hostname:
            if not _HOSTNAME_RE.match(hostname):
                errors["hostname"] = "Invalid hostname (alphanumeric and '-' only)"
            elif self.dry_run:
                applied["hostname"] = hostname
            else:
                rc, out, err = await self.run_cmd(
                    ["raspi-config", "nonint", "do_hostname", hostname], privileged=True
                )
                if rc != 0:
                    rc, out, err = await self.run_cmd(
                        ["hostnamectl", "set-hostname", hostname], privileged=True
                    )
                if rc == 0:
                    applied["hostname"] = hostname
                else:
                    errors["hostname"] = (err or out or "failed").strip()

        # 2. Wi-Fi Country
        wifi_country = str(params.get("wifi_country") or "").strip().upper()
        if wifi_country:
            if not _COUNTRY_RE.match(wifi_country):
                errors["wifi_country"] = "Use 2-letter ISO country code (e.g. US, DE, GB)"
            elif self.dry_run:
                applied["wifi_country"] = wifi_country
            else:
                rc, out, err = await self.run_cmd(
                    ["raspi-config", "nonint", "do_wifi_country", wifi_country], privileged=True
                )
                if rc == 0:
                    applied["wifi_country"] = wifi_country
                else:
                    errors["wifi_country"] = (err or out or "failed").strip()

        # 3. Timezone / Locale / Keyboard
        for field, rc_cmd in (
            ("timezone", "do_change_timezone"),
            ("locale", "do_change_locale"),
            ("keyboard", "do_configure_keyboard"),
        ):
            val = str(params.get(field) or "").strip()
            if val:
                if not _SAFE_TOKEN_RE.match(val):
                    errors[field] = f"Invalid {field} format"
                elif self.dry_run:
                    applied[field] = val
                else:
                    rc, out, err = await self.run_cmd(
                        ["raspi-config", "nonint", rc_cmd, val], privileged=True
                    )
                    if rc == 0:
                        applied[field] = val
                    else:
                        errors[field] = (err or out or "failed").strip()

        # 4. Boot behaviour & Boot wait for network
        boot_beh = str(params.get("boot_behaviour") or "").strip().upper()
        if boot_beh:
            if boot_beh not in ("B1", "B2", "B3", "B4"):
                errors["boot_behaviour"] = "Must be B1 (Console), B2 (Console Autologin), B3 (Desktop), or B4 (Desktop Autologin)"
            elif self.dry_run:
                applied["boot_behaviour"] = boot_beh
            else:
                rc, out, err = await self.run_cmd(
                    ["raspi-config", "nonint", "do_boot_behaviour", boot_beh], privileged=True
                )
                if rc == 0:
                    applied["boot_behaviour"] = boot_beh
                else:
                    errors["boot_behaviour"] = (err or out or "failed").strip()

        if "boot_wait_network" in params and params["boot_wait_network"] is not None:
            wait_flag = 0 if bool(params["boot_wait_network"]) else 1
            if self.dry_run:
                applied["boot_wait_network"] = bool(params["boot_wait_network"])
            else:
                rc, out, err = await self.run_cmd(
                    ["raspi-config", "nonint", "do_boot_wait", str(wait_flag)], privileged=True
                )
                if rc == 0:
                    applied["boot_wait_network"] = bool(params["boot_wait_network"])
                else:
                    errors["boot_wait_network"] = (err or out or "failed").strip()

        # 5. Hardware Interfaces (SSH, VNC, SPI, I2C, Serial HW, Serial Console, 1-Wire, Remote GPIO)
        ifaces_in = params.get("interfaces") if isinstance(params.get("interfaces"), dict) else params
        for key in RASPI_INTERFACE_KEYS:
            if key in ifaces_in and ifaces_in[key] is not None:
                enabled = bool(ifaces_in[key])
                rc_arg = "0" if enabled else "1"
                if self.dry_run:
                    applied[key] = enabled
                else:
                    rc, out, err = await self.run_cmd(
                        ["raspi-config", "nonint", _RASPI_INTERFACE_CMD[key], rc_arg],
                        privileged=True,
                    )
                    if rc == 0:
                        applied[key] = enabled
                    else:
                        errors[key] = (err or out or "failed").strip()

        # 6. GPU Memory Split (16 / 32 / 64 / 128 MB)
        if "gpu_mem" in params and params["gpu_mem"] not in (None, ""):
            try:
                gpu_mb = int(params["gpu_mem"])
            except (TypeError, ValueError):
                gpu_mb = -1
            if gpu_mb not in (16, 32, 64, 128, 256):
                errors["gpu_mem"] = "gpu_mem must be 16, 32, 64, 128, or 256"
            elif self.dry_run:
                applied["gpu_mem"] = gpu_mb
            else:
                rc, out, err = await self.run_cmd(
                    ["raspi-config", "nonint", "do_memory_split", str(gpu_mb)], privileged=True
                )
                if rc == 0:
                    applied["gpu_mem"] = gpu_mb
                else:
                    errors["gpu_mem"] = (err or out or "failed").strip()

        # 7. Expand Root Filesystem
        if params.get("expand_rootfs"):
            if self.dry_run:
                applied["expand_rootfs"] = True
            else:
                rc, out, err = await self.run_cmd(
                    ["raspi-config", "nonint", "do_expand_rootfs"], privileged=True
                )
                if rc == 0:
                    applied["expand_rootfs"] = True
                else:
                    errors["expand_rootfs"] = (err or out or "failed").strip()

        return {
            "ok": len(errors) == 0,
            "applied": applied,
            "errors": errors,
        }

    async def power_action(self, action: str) -> Tuple[bool, str]:
        act = (action or "").strip().lower()
        if act not in ("reboot", "shutdown", "poweroff"):
            return False, f"Unsupported power action '{action}'"
        if self.dry_run:
            return True, f"{act} scheduled (dry-run)"
        cmd = ["systemctl", "reboot"] if act == "reboot" else ["systemctl", "poweroff"]
        rc, out, err = await self.run_cmd(cmd, privileged=True)
        if rc != 0:
            return False, (err or out or f"{act} failed").strip()
        return True, f"{act} initiated"
