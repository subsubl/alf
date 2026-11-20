"""
ESP32-S3 LCD Firmware Updater & Safe Live Reflash Manager for Raspberry Pi Zero 2 W (Tier 2).

Allows the LLM (via update_companion_settings / flash_esp32_firmware), the Pi Web GUI,
the Server Web GUI, or CLI scripts to:
1. Inspect staged Zephyr firmware (`zephyr.bin`) and verify Espressif 0xE9 magic & SHA-256.
2. Upload or fetch new `zephyr.bin` firmware builds into the Pi staging directory.
3. Safely pause `EspSerialBridge` (releasing `/dev/ttyACM0` / `COM3`), invoke `esptool`
   for `esp32s3` at `0x0`, resume the 921600-baud serial bridge, and verify post-flash boot via `ping`.
4. Trigger a non-destructive hardware reset of the ESP32-S3 via CH343 USB DTR/RTS.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("pi_hub.firmware_updater")

ESP_IMAGE_MAGIC_BYTE = 0xE9

FlashRunnerFn = Callable[[List[str]], Awaitable[Tuple[int, str]]]


async def _default_subproc_runner(cmd: List[str]) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    output = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    return int(proc.returncode or 0), output


class EspFirmwareUpdater:
    """Manages staged ESP32-S3 Zephyr firmware binaries and safe live USB reflashing."""

    def __init__(
        self,
        esp_bridge: Any,
        firmware_dir: Optional[Path] = None,
        runner_fn: Optional[FlashRunnerFn] = None,
    ) -> None:
        self.esp = esp_bridge
        repo_root = Path(__file__).resolve().parent.parent
        default_fw_dir = Path(os.getenv("TAMAGCI_FIRMWARE_DIR", str(repo_root / "firmware")))
        self.firmware_dir = Path(firmware_dir) if firmware_dir is not None else default_fw_dir
        self._runner: FlashRunnerFn = runner_fn or _default_subproc_runner
        self._flash_lock = asyncio.Lock()
        self.last_flash_result: Optional[Dict[str, Any]] = None

        self._candidate_paths: List[Path] = [
            self.firmware_dir / "zephyr.bin",
            Path("/opt/tamagci/firmware/zephyr.bin"),
            repo_root / "pi_hub" / "firmware" / "zephyr.bin",
            repo_root / "esp32_face_os" / "build" / "zephyr" / "zephyr.bin",
            Path("C:/zephyrproject/build_esp32_face_os/zephyr/zephyr.bin"),
        ]

    @staticmethod
    def resolve_esptool_command() -> Tuple[bool, List[str]]:
        """Locate esptool module or executable in the current Python environment or PATH."""
        try:
            import importlib.util

            if importlib.util.find_spec("esptool") is not None:
                return True, [sys.executable, "-m", "esptool"]
        except Exception:
            pass

        for exe_name in ("esptool.py", "esptool", "esptool.exe"):
            found = shutil.which(exe_name)
            if found:
                return True, [found]

        win_zephyr_esptool = Path("C:/zephyrproject/.venv/Scripts/esptool.exe")
        if win_zephyr_esptool.is_file():
            return True, [str(win_zephyr_esptool)]

        return False, [sys.executable, "-m", "esptool"]

    def _find_firmware_file(self, source: Optional[str | Path] = None) -> Optional[Path]:
        if source is not None:
            s = str(source).strip()
            if s and s.lower() not in ("latest", "default", "staged", "auto"):
                p = Path(s)
                if p.is_file():
                    return p
                cand_in_dir = self.firmware_dir / s
                if cand_in_dir.is_file():
                    return cand_in_dir
                return p
        for cand in self._candidate_paths:
            if cand.is_file():
                return cand
        return None

    @staticmethod
    def inspect_binary(path: Path) -> Dict[str, Any]:
        if not path.is_file():
            return {
                "exists": False,
                "path": str(path),
                "size_bytes": 0,
                "sha256": "",
                "valid_esp_magic": False,
            }
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        valid_magic = len(data) >= 16 and data[0] == ESP_IMAGE_MAGIC_BYTE
        return {
            "exists": True,
            "path": str(path),
            "size_bytes": len(data),
            "sha256": sha,
            "valid_esp_magic": valid_magic,
            "mtime": path.stat().st_mtime,
        }

    def check_firmware(self, source: Optional[str | Path] = None) -> Dict[str, Any]:
        """Inspect staged firmware binary, SHA-256, Espressif magic byte, and esptool readiness."""
        fw_path = self._find_firmware_file(source)
        info = (
            self.inspect_binary(fw_path)
            if fw_path is not None
            else {
                "exists": False,
                "path": str(self.firmware_dir / "zephyr.bin"),
                "size_bytes": 0,
                "sha256": "",
                "valid_esp_magic": False,
            }
        )
        esptool_ok, esptool_cmd = self.resolve_esptool_command()
        return {
            "ok": True,
            "action": "check",
            "port": getattr(self.esp, "port", "/dev/ttyACM0"),
            "baudrate": getattr(self.esp, "baudrate", 921600),
            "firmware": info,
            "esptool_available": esptool_ok,
            "esptool_cmd": esptool_cmd,
            "ready_to_flash": bool(info["exists"] and info["valid_esp_magic"]),
            "last_flash_result": self.last_flash_result,
        }

    def stage_firmware(
        self,
        data: bytes,
        expected_sha256: Optional[str] = None,
        filename: str = "zephyr.bin",
    ) -> Dict[str, Any]:
        """Validate and atomically stage a new zephyr.bin firmware image on disk."""
        if not data or len(data) < 16:
            return {"ok": False, "error": "Firmware image is empty or smaller than 16 bytes"}
        if data[0] != ESP_IMAGE_MAGIC_BYTE:
            return {
                "ok": False,
                "error": f"Invalid ESP32 image magic byte 0x{data[0]:02X} (expected 0xE9)",
            }

        actual_sha = hashlib.sha256(data).hexdigest()
        if expected_sha256:
            exp = expected_sha256.strip().lower()
            if exp and actual_sha.lower() != exp:
                return {
                    "ok": False,
                    "error": f"SHA-256 mismatch: expected {exp}, got {actual_sha}",
                }

        safe_name = Path(filename).name or "zephyr.bin"
        if not safe_name.endswith(".bin"):
            safe_name += ".bin"

        self.firmware_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.firmware_dir / safe_name
        tmp_path = self.firmware_dir / f".{safe_name}.tmp"
        tmp_path.write_bytes(data)
        tmp_path.replace(target_path)

        sha_path = self.firmware_dir / f"{safe_name}.sha256"
        sha_path.write_text(f"{actual_sha}  {safe_name}\n", encoding="utf-8")

        if target_path not in self._candidate_paths:
            self._candidate_paths.insert(0, target_path)

        return {
            "ok": True,
            "action": "stage",
            "firmware": self.inspect_binary(target_path),
        }

    async def reset_esp32(self) -> Dict[str, Any]:
        """Perform a hardware reset of the ESP32-S3 over USB DTR/RTS and verify post-boot ping."""
        async with self._flash_lock:
            return await self.esp.hardware_reset()

    async def flash_firmware(
        self,
        source: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        baudrate: int = 921600,
        flash_offset: str = "0x0",
    ) -> Dict[str, Any]:
        """
        Safely flash `zephyr.bin` onto the Waveshare ESP32-S3-Touch-LCD-1.28-B:
        1. Optionally fetch URL source into local staging.
        2. Verify file existence, 0xE9 magic header, and optional SHA-256 digest.
        3. Pause EspSerialBridge so `/dev/ttyACM0` (or `COM3`) is unlocked.
        4. Run esptool write_flash for esp32s3.
        5. Resume EspSerialBridge at 921600 baud and verify live `ping` response.
        """
        async with self._flash_lock:
            t0 = time.monotonic()

            # 1. If source is an HTTP(S) URL, download and stage it first
            if isinstance(source, str) and source.strip().lower().startswith(("http://", "https://")):
                url = source.strip()
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Alf-PiHub-Updater/1.0"})
                    with urllib.request.urlopen(req, timeout=30.0) as resp:
                        dl_bytes = resp.read()
                    staged = self.stage_firmware(dl_bytes, expected_sha256=expected_sha256)
                    if not staged.get("ok"):
                        self.last_flash_result = staged
                        return staged
                    fw_path: Optional[Path] = Path(staged["firmware"]["path"])
                except Exception as exc:
                    err_res = {"ok": False, "action": "flash", "error": f"Failed to download firmware: {exc}"}
                    self.last_flash_result = err_res
                    return err_res
            else:
                fw_path = self._find_firmware_file(source)

            if fw_path is None or not fw_path.is_file():
                err_res = {
                    "ok": False,
                    "action": "flash",
                    "error": f"Firmware binary not found (searched {source or self.firmware_dir / 'zephyr.bin'})",
                }
                self.last_flash_result = err_res
                return err_res

            info = self.inspect_binary(fw_path)
            if not info["valid_esp_magic"]:
                err_res = {
                    "ok": False,
                    "action": "flash",
                    "error": f"Invalid ESP32-S3 firmware image (missing 0xE9 magic header): {fw_path}",
                    "firmware": info,
                }
                self.last_flash_result = err_res
                return err_res

            if expected_sha256:
                exp = expected_sha256.strip().lower()
                if exp and info["sha256"].lower() != exp:
                    err_res = {
                        "ok": False,
                        "action": "flash",
                        "error": f"SHA-256 mismatch: expected {exp}, got {info['sha256']}",
                        "firmware": info,
                    }
                    self.last_flash_result = err_res
                    return err_res

            _, esptool_prefix = self.resolve_esptool_command()
            port = str(getattr(self.esp, "port", "/dev/ttyACM0"))
            cmd = [
                *esptool_prefix,
                "--chip",
                "esp32s3",
                "--port",
                port,
                "--baud",
                str(int(baudrate)),
                "--before",
                "default_reset",
                "--after",
                "hard_reset",
                "write_flash",
                "-z",
                "--flash_mode",
                "dio",
                "--flash_freq",
                "80m",
                "--flash_size",
                "16MB",
                str(flash_offset),
                str(fw_path),
            ]

            logger.info("Pausing ESP32 serial bridge on %s for firmware flash...", port)
            was_running = await self.esp.pause_for_flash()
            exit_code = -1
            output = ""
            try:
                exit_code, output = await self._runner(cmd)
            except Exception as exc:
                output = f"esptool execution error: {exc}"
            finally:
                logger.info("Resuming ESP32 serial bridge on %s @ 921600...", port)
                await self.esp.resume_after_flash(was_running)

            ping_res = await self.esp.request({"t": "ping"}, timeout=3.0) if was_running else {"ok": 1}
            duration_s = round(time.monotonic() - t0, 2)
            ok = exit_code == 0

            result = {
                "ok": ok,
                "action": "flash",
                "port": port,
                "baudrate": int(baudrate),
                "flash_offset": str(flash_offset),
                "firmware": info,
                "esptool_exit_code": exit_code,
                "esptool_output": output[-2000:] if output else "",
                "post_flash_ping": ping_res,
                "duration_s": duration_s,
                "ts": time.time(),
            }
            if not ok:
                result["error"] = f"esptool exited with code {exit_code}"
            self.last_flash_result = result
            return result
