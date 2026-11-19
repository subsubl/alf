"""Step 5 deployment deliverables, zero-trust security, exclusions, and firmware/cross-tier contract tests."""

from __future__ import annotations

import ast
import configparser
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._stubs import FakeAsyncOpenAI, FakeSerialPort, MockESP32, install_dependency_stubs

install_dependency_stubs()

import yaml
from server_agent.agent_router import AGENT_TOOLS
from server_agent.main_server import ServerCompanionService


STEP5_DELIVERABLES = [
    "deploy/DEPLOYMENT.md",
    "deploy/setup_pi.sh",
    "deploy/setup_server.sh",
    "deploy/ixian.cfg.pi.template",
    "deploy/ixian.cfg.server.template",
    "deploy/systemd/quixi-pi.service",
    "deploy/systemd/tamagci-pi-hub.service",
    "deploy/systemd/quixi-server.service",
    "deploy/systemd/tamagci-server-agent.service",
    "README.md",
]

ALLOWLIST_PINS = [15, 16, 17, 18, 21, 33, 34, 35, 36, 37]
EMOTIONS_9 = [
    "IDLE",
    "LISTENING",
    "THINKING",
    "SPEAKING",
    "HAPPY",
    "SAD",
    "SURPRISED",
    "SLEEPING",
    "ALERT",
]


def _parse_kv_cfg(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if "=" in stripped:
            k, v = stripped.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class TestStep5DeploymentArtifacts(unittest.TestCase):
    """Tier 1: Step 5 deployment scripts, ixian.cfg templates, systemd units, and docs."""

    def test_step5_deliverables_exist(self) -> None:
        for rel in STEP5_DELIVERABLES:
            with self.subTest(path=rel):
                p = _REPO_ROOT / rel
                self.assertTrue(p.is_file(), f"Missing Step 5 deliverable: {rel}")
                self.assertGreater(p.stat().st_size, 0, f"Empty Step 5 deliverable: {rel}")

    def test_shell_scripts_syntax_and_safety(self) -> None:
        bash_bin = shutil.which("bash")
        use_bash = False
        if bash_bin and "system32" not in bash_bin.lower():
            try:
                ver = subprocess.run([bash_bin, "--version"], capture_output=True, text=True, timeout=2)
                use_bash = ver.returncode == 0
            except Exception:
                use_bash = False

        for rel in ("deploy/setup_pi.sh", "deploy/setup_server.sh"):
            with self.subTest(script=rel):
                p = _REPO_ROOT / rel
                self.assertTrue(p.is_file(), f"Missing script: {rel}")
                if not p.is_file():
                    continue
                raw = p.read_bytes()
                self.assertNotIn(b"\r\n", raw, f"{rel} must use LF line endings, not CRLF")
                text = raw.decode("utf-8")
                first_line = text.splitlines()[0] if text.splitlines() else ""
                self.assertTrue(
                    first_line.startswith("#!/usr/bin/env bash") or first_line.startswith("#!/bin/bash"),
                    f"{rel} missing bash shebang",
                )
                self.assertTrue(
                    "set -euo pipefail" in text or ("set -e" in text and "set -u" in text),
                    f"{rel} must enable strict mode (set -euo pipefail)",
                )
                if use_bash and bash_bin:
                    res = subprocess.run([bash_bin, "-n", str(p)], capture_output=True, text=True, timeout=5)
                    self.assertEqual(res.returncode, 0, f"bash -n failed for {rel}: {res.stderr}")
                else:
                    if_cnt = len(re.findall(r"(?m)^\s*if\b", text))
                    fi_cnt = len(re.findall(r"(?m)^\s*fi\b", text))
                    self.assertEqual(if_cnt, fi_cnt, f"Unbalanced if/fi in {rel}")

        pi_script = _REPO_ROOT / "deploy/setup_pi.sh"
        if pi_script.is_file():
            pi_text = pi_script.read_text(encoding="utf-8")
            for token in ("pi_hub", "requirements.txt", "mosquitto", "quixi-pi.service", "tamagci-pi-hub.service"):
                self.assertIn(token, pi_text)
            for forbidden in ("OPENAI_API_KEY", "LLM_API_KEY", "STT_API_KEY", "TTS_API_KEY"):
                self.assertNotIn(forbidden, pi_text)

        srv_script = _REPO_ROOT / "deploy/setup_server.sh"
        if srv_script.is_file():
            srv_text = srv_script.read_text(encoding="utf-8")
            for token in ("server_agent", "requirements.txt", ".env.example", "chmod 600", "quixi-server.service", "tamagci-server-agent.service"):
                self.assertIn(token, srv_text)

    def test_ixian_cfg_templates_schema(self) -> None:
        for rel in ("deploy/ixian.cfg.pi.template", "deploy/ixian.cfg.server.template"):
            with self.subTest(template=rel):
                p = _REPO_ROOT / rel
                self.assertTrue(p.is_file(), f"Missing template: {rel}")
                cfg = _parse_kv_cfg(p)
                self.assertEqual(cfg.get("apiPort"), "8001")
                self.assertEqual(cfg.get("mqDriver"), "mqtt")
                self.assertIn(cfg.get("mqHost"), {"127.0.0.1", "localhost"})
                self.assertEqual(cfg.get("mqPort"), "1883")
                self.assertNotRegex(p.read_text(encoding="utf-8"), r"sk-[A-Za-z0-9_-]{8,}")

    def test_systemd_unit_files(self) -> None:
        units = [
            "deploy/systemd/quixi-pi.service",
            "deploy/systemd/tamagci-pi-hub.service",
            "deploy/systemd/quixi-server.service",
            "deploy/systemd/tamagci-server-agent.service",
        ]
        for rel in units:
            with self.subTest(unit=rel):
                p = _REPO_ROOT / rel
                self.assertTrue(p.is_file(), f"Missing systemd unit: {rel}")
                if not p.is_file():
                    continue
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                cp.read(p, encoding="utf-8")
                for sec in ("Unit", "Service", "Install"):
                    self.assertIn(sec, cp, f"{rel} missing [{sec}]")
                self.assertTrue(cp["Service"].get("ExecStart"), f"{rel} missing ExecStart")
                self.assertTrue(cp["Service"].get("Restart"), f"{rel} missing Restart")
                self.assertEqual(cp["Install"].get("WantedBy"), "multi-user.target")

        pi_unit = _REPO_ROOT / "deploy/systemd/tamagci-pi-hub.service"
        if pi_unit.is_file():
            pi_cp = configparser.ConfigParser(interpolation=None, strict=False)
            pi_cp.read(pi_unit, encoding="utf-8")
            self.assertIn("quixi-pi.service", pi_cp["Unit"].get("After", ""))
            self.assertIn("pi_hub/main_hub.py", pi_cp["Service"].get("ExecStart", ""))
            self.assertNotIn(".env", pi_cp["Service"].get("EnvironmentFile", ""))

        srv_unit = _REPO_ROOT / "deploy/systemd/tamagci-server-agent.service"
        if srv_unit.is_file():
            srv_cp = configparser.ConfigParser(interpolation=None, strict=False)
            srv_cp.read(srv_unit, encoding="utf-8")
            self.assertIn("quixi-server.service", srv_cp["Unit"].get("After", ""))
            self.assertIn("server_agent/main_server.py", srv_cp["Service"].get("ExecStart", ""))

    def test_docs_alignment(self) -> None:
        deprecated_115200 = re.compile(
            r"(?:\b(?:from|instead\s+of|not|rather\s+than|replaced|deprecated|legacy|previously|formerly)\b[^\n]*\b115200\b|\b115200\b[^\n]*\b(?:deprecated|legacy|replaced)\b)",
            re.IGNORECASE,
        )
        runtime_115200 = re.compile(
            r"(?:baud(?:rate)?\s*[:=]\s*`?115200\b|--baud(?:rate)?\s+`?115200\b|@\s*`?115200\b|`?115200`?\s*baud\b)",
            re.IGNORECASE,
        )
        for rel in ("deploy/DEPLOYMENT.md", "README.md"):
            with self.subTest(doc=rel):
                p = _REPO_ROOT / rel
                self.assertTrue(p.is_file(), f"Missing doc: {rel}")
                text = p.read_text(encoding="utf-8")
                self.assertIn("921600", text)
                for lineno, line in enumerate(text.splitlines(), 1):
                    if runtime_115200.search(line) and not deprecated_115200.search(line):
                        self.fail(f"{rel}:{lineno} configures legacy 115200 baud instead of 921600: {line.strip()}")
                self.assertIn("/addContact", text)
                self.assertIn("/acceptContact", text)
                self.assertRegex(text.lower(), r"api key|zero.?trust")


class TestZeroTrustAndExclusions(unittest.TestCase):
    """Tier 1: Zero-trust secret isolation and deferred exclusions compliance."""

    def test_no_api_keys_or_secrets_in_edge_and_shared_tiers(self) -> None:
        targets = [
            _REPO_ROOT / "esp32_face_os",
            _REPO_ROOT / "pi_hub",
            _REPO_ROOT / "common_ixian",
            _REPO_ROOT / "tools",
            _REPO_ROOT / "deploy" / "setup_pi.sh",
            _REPO_ROOT / "deploy" / "ixian.cfg.pi.template",
            _REPO_ROOT / "deploy" / "systemd" / "quixi-pi.service",
            _REPO_ROOT / "deploy" / "systemd" / "tamagci-pi-hub.service",
            _REPO_ROOT / "deploy" / "systemd" / "quixi-server.service",
        ]
        secret_patterns = [
            re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
            re.compile(r"Bearer\s+sk-"),
            re.compile(r"\b(?:OPENAI_API_KEY|LLM_API_KEY|STT_API_KEY|TTS_API_KEY)\b"),
        ]
        for target in targets:
            if not target.exists():
                continue
            files = [target] if target.is_file() else [f for f in target.rglob("*") if f.is_file() and "__pycache__" not in f.parts]
            for f in files:
                text = f.read_text(encoding="utf-8", errors="ignore")
                for pat in secret_patterns:
                    self.assertIsNone(pat.search(text), f"Secret pattern {pat.pattern} found in {f}")

    def test_no_live_dot_env_and_gitignore_protection(self) -> None:
        gi = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for item in (".env", ".venv/", "__pycache__/", "*.py[cod]", "build/"):
            self.assertIn(item, gi)

        tracked = subprocess.run(["git", "-C", str(_REPO_ROOT), "ls-files"], capture_output=True, text=True, check=True)
        tracked_files = set(tracked.stdout.splitlines())
        self.assertNotIn(".env", tracked_files)
        self.assertNotIn("server_agent/.env", tracked_files)

        env_ex = (_REPO_ROOT / "server_agent/.env.example").read_text(encoding="utf-8")
        self.assertIn("sk-your-llm-key-here", env_ex)
        self.assertIn("PI_IXIAN_ADDRESS", env_ex)
        self.assertIn("ALLOW_UNAUTHENTICATED_DEV", env_ex)

    def test_server_auth_is_fail_closed_by_default(self) -> None:
        fake_ai = FakeAsyncOpenAI()
        for pi_addr in ("", "1ReplaceWithYourPiQuIXIWalletAddress"):
            with (
                patch.dict(os.environ, {"PI_IXIAN_ADDRESS": pi_addr, "ALLOW_UNAUTHENTICATED_DEV": ""}, clear=False),
                patch("server_agent.main_server.load_dotenv", return_value=False),
                patch("server_agent.agent_router.AsyncOpenAI", return_value=fake_ai),
                patch("server_agent.speech_processor.AsyncOpenAI", return_value=fake_ai),
            ):
                svc = ServerCompanionService()
                self.assertFalse(svc._is_authorized("1SomeRandomAddress"))

    def test_exclusions_compliance(self) -> None:
        self.assertFalse((_REPO_ROOT / ".github" / "workflows").exists())
        github_dir = _REPO_ROOT / ".github"
        if github_dir.exists():
            self.assertEqual(list(github_dir.rglob("*.yml")) + list(github_dir.rglob("*.yaml")), [])

        for root, dirs, files in os.walk(_REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__"}]
            for d in dirs:
                self.assertNotIn(d.lower(), {"pi-gen", "packer", "genimage"})
            for fn in files:
                self.assertFalse(fn.endswith((".img", ".img.xz", ".iso")), f"Forbidden disk image: {fn}")

        remote = subprocess.run(["git", "-C", str(_REPO_ROOT), "remote", "-v"], capture_output=True, text=True, check=True)
        self.assertEqual(remote.stdout.strip(), "")


class TestFirmwareAndCrossTierContracts(unittest.TestCase):
    """Tier 1 & Tier 2 static firmware safety and cross-tier parity checks."""

    def test_app_overlay_hardware_contracts(self) -> None:
        overlay = (_REPO_ROOT / "esp32_face_os/app.overlay").read_text(encoding="utf-8")
        uart_m = re.search(r"&uart0\s*\{([^}]*)\}", overlay, re.DOTALL)
        self.assertIsNotNone(uart_m, "Missing &uart0 block in app.overlay")
        self.assertIn("current-speed = <921600>;", uart_m.group(1))

        ledc_m = re.search(r"&ledc0\s*\{([^}]*)\}", overlay, re.DOTALL)
        self.assertIsNotNone(ledc_m)
        self.assertIn('status = "okay";', ledc_m.group(1))
        # Ensure GPIO 4 and 5 are never bound inside any ledc/pwm pinctrl block
        for blk in re.findall(r"ledc[^{]*\{[^}]*\}", overlay, re.DOTALL):
            self.assertNotRegex(blk, r"LEDC_CH\d+_GPIO[45]\b|gpio\s+[45]\b")

        self.assertIn("zephyr,resolution = <12>;", overlay)
        self.assertIn("PANEL_PIXEL_FORMAT_RGB_565", overlay)

    def test_prj_conf_contracts(self) -> None:
        conf = (_REPO_ROOT / "esp32_face_os/prj.conf").read_text(encoding="utf-8")
        for sym in (
            "CONFIG_UART_INTERRUPT_DRIVEN=y",
            "CONFIG_BT=y",
            "CONFIG_BT_OBSERVER=y",
            "CONFIG_DISPLAY=y",
            "CONFIG_LVGL=y",
            "CONFIG_INPUT_CST816S=y",
            "CONFIG_ADC=y",
            "CONFIG_I2C=y",
        ):
            self.assertIn(sym, conf)
        self.assertIn("CONFIG_BOOT_BANNER=n", conf)
        self.assertTrue(
            "CONFIG_UART_CONSOLE=n" in conf
            or "CONFIG_LOG_BACKEND_UART=n" in conf
            or "CONFIG_LOG=n" in conf,
            "UART0 console/log output must be suppressed in prj.conf",
        )

    def test_gpio_agent_c_allowlist_and_safety(self) -> None:
        c_text = (_REPO_ROOT / "esp32_face_os/src/gpio_agent.c").read_text(encoding="utf-8")
        m = re.search(r"allowed_pins\[\]\s*=\s*\{([^}]+)\}", c_text)
        self.assertIsNotNone(m)
        pins = [int(x.strip()) for x in m.group(1).split(",") if x.strip()]
        self.assertEqual(pins, ALLOWLIST_PINS)
        for prot in (*range(0, 15), 43, 44):
            self.assertNotIn(prot, pins)
        self.assertIn("-EACCES", c_text)
        self.assertIn("mv * 3", c_text)
        self.assertIn("gpio_mutex", c_text)

    def test_serial_proto_c_contracts(self) -> None:
        c_text = (_REPO_ROOT / "esp32_face_os/src/serial_proto.c").read_text(encoding="utf-8")
        for cmd in ("ping", "state", "sync", "mouth", "dbg", "cfg", "gpio_cfg", "gpio_set", "gpio_get", "ble_get", "imu_get"):
            self.assertIn(f'"{cmd}"', c_text)
        self.assertNotIn("(pin >= 0) ? gpio_agent_get(pin, &val) : 0", c_text)
        self.assertIn("-EINVAL", c_text)

        # Ensure BLE response buffer is >= 1536B or dynamically formatted (not 768/896 truncation)
        self.assertNotIn("static char ble_arr[768]", c_text)
        self.assertNotIn("static char resp[896]", c_text)

    def test_ble_and_imu_c_contracts(self) -> None:
        ble_h = (_REPO_ROOT / "esp32_face_os/src/ble_scanner.h").read_text(encoding="utf-8")
        ble_c = (_REPO_ROOT / "esp32_face_os/src/ble_scanner.c").read_text(encoding="utf-8")
        self.assertIn("#define BLE_MAX_DEVICES 32", ble_h)
        self.assertIn("addr_type", ble_h)
        self.assertIn("BLE_STALE_MS 30000", ble_c)
        self.assertTrue('\\"type\\"' in ble_c or '"type"' in ble_c)

        imu_c = (_REPO_ROOT / "esp32_face_os/src/imu_qmi8658.c").read_text(encoding="utf-8")
        imu_h = (_REPO_ROOT / "esp32_face_os/src/imu_qmi8658.h").read_text(encoding="utf-8")
        self.assertIn("0x6B", imu_c)
        self.assertIn("0x6A", imu_c)
        self.assertTrue("K_THREAD_DEFINE" in imu_c or "k_thread_create" in imu_c, "Expected dedicated 50Hz IMU thread")
        for gest in ("shake", "tilt", "pickup", "double_tap"):
            self.assertIn(gest, (imu_c + imu_h).lower())

    def test_ui_screens_c_contracts(self) -> None:
        ui_h = (_REPO_ROOT / "esp32_face_os/src/ui_screens.h").read_text(encoding="utf-8")
        ui_c = (_REPO_ROOT / "esp32_face_os/src/ui_screens.c").read_text(encoding="utf-8")
        for scr in ("scr_face", "scr_settings", "scr_debug"):
            self.assertIn(scr, ui_c)
        self.assertIn("LV_EVENT_GESTURE", ui_c)
        for emo in EMOTIONS_9:
            self.assertIn(f"FACE_{emo}", ui_h)
        self.assertIn("display_set_orientation", ui_c)
        self.assertIn("stop_speech", (_REPO_ROOT / "esp32_face_os/src/main.c").read_text(encoding="utf-8") + ui_c)
        self.assertIn("ptt_toggle", (_REPO_ROOT / "esp32_face_os/src/main.c").read_text(encoding="utf-8") + ui_c)
        self.assertIn("pwm", ui_c.lower())
        self.assertIn("heap", ui_c.lower())

    def test_cross_tier_contract_parity(self) -> None:
        # 1. 921600 baudrate parity across all 6 files
        with open(_REPO_ROOT / "pi_hub/config.yaml", "r", encoding="utf-8") as f:
            pi_cfg = yaml.safe_load(f)
        self.assertEqual(int(pi_cfg["serial"]["baudrate"]), 921600)
        self.assertIn("921600", (_REPO_ROOT / "esp32_face_os/app.overlay").read_text(encoding="utf-8"))
        self.assertIn("921600", (_REPO_ROOT / "pi_hub/esp_serial_bridge.py").read_text(encoding="utf-8"))
        self.assertIn("921600", (_REPO_ROOT / "tools/verify_esp32_serial.py").read_text(encoding="utf-8"))
        self.assertIn("921600", (_REPO_ROOT / "tools/pc_companion_tester.py").read_text(encoding="utf-8"))
        self.assertIn("921600", (_REPO_ROOT / "README.md").read_text(encoding="utf-8"))

        # 2. GPIO allowlist parity
        by_name = {t["function"]["name"]: t["function"] for t in AGENT_TOOLS}
        self.assertEqual(by_name["gpio_configure_and_write"]["parameters"]["properties"]["pin"]["enum"], ALLOWLIST_PINS)
        self.assertEqual(by_name["gpio_read_pin"]["parameters"]["properties"]["pin"]["enum"], ALLOWLIST_PINS)

        # 3. Emotion enum parity
        self.assertEqual(by_name["set_companion_emotion"]["parameters"]["properties"]["emotion"]["enum"], EMOTIONS_9)

        # 4. All 6 AGENT_TOOLS implemented in pi_hub/main_hub.py
        hub_src = (_REPO_ROOT / "pi_hub/main_hub.py").read_text(encoding="utf-8")
        for tool_name in by_name:
            self.assertIn(f'"{tool_name}"', hub_src)

        # 5. QuIXI 8001 / 1883 parity
        self.assertEqual(int(pi_cfg["quixi"]["mqtt_port"]), 1883)
        self.assertIn("8001", pi_cfg["quixi"]["rest_url"])
        env_ex = (_REPO_ROOT / "server_agent/.env.example").read_text(encoding="utf-8")
        self.assertIn("8001", env_ex)
        self.assertIn("1883", env_ex)

    def test_render_unit_extraction_and_substitution(self) -> None:
        scenarios = [
            ("default", "/opt/tamagci", "/opt/quixi", "tamagci", ["quixi-pi.service", "tamagci-pi-hub.service"]),
            ("custom", "/home/alice/tamagci", "/srv/quixi", "alice", ["quixi-server.service", "tamagci-server-agent.service"]),
            ("spaces", "/opt/my companion/tamagci", "/var/lib/my quixi", "svc_user", ["quixi-pi.service", "tamagci-pi-hub.service"]),
            ("nested", "/opt/quixi/opt/tamagci/nested", "/var/quixi", "nested_user", ["quixi-server.service", "tamagci-server-agent.service"]),
        ]
        for script_rel in ("deploy/setup_pi.sh", "deploy/setup_server.sh"):
            script_text = (_REPO_ROOT / script_rel).read_text(encoding="utf-8")
            m = re.search(
                r"render_unit\(\)\s*\{.*?python3 -c '(.*?)'\s*\|\s*\$\{SUDO\} tee",
                script_text,
                re.DOTALL,
            )
            self.assertIsNotNone(m, f"Could not extract render_unit() python snippet from {script_rel}")
            py_snippet = m.group(1)

            for name, repo_dir, quixi_dir, user, unit_names in scenarios:
                for uname in unit_names:
                    unit_path = _REPO_ROOT / "deploy" / "systemd" / uname
                    with self.subTest(script=script_rel, scenario=name, unit=uname):
                        env_vars = {
                            "SRC_PATH": str(unit_path),
                            "TARGET_USER_VAL": user,
                            "REPO_DIR_VAL": repo_dir,
                            "QUIXI_DIR_VAL": quixi_dir,
                        }
                        stdout_buf = io.StringIO()
                        with (
                            patch.dict(os.environ, env_vars, clear=False),
                            patch("sys.stdout", stdout_buf),
                        ):
                            exec(compile(py_snippet, f"{script_rel}:render_unit", "exec"), {})
                        rendered = stdout_buf.getvalue()
                        self.assertIn(f"User={user}", rendered)
                        if user != "pi":
                            self.assertNotIn("User=pi\n", rendered)
                        if user != "tamagci":
                            self.assertNotIn("User=tamagci\n", rendered)
                        self.assertIn(quixi_dir if "/opt/quixi" in unit_path.read_text(encoding="utf-8") else repo_dir, rendered)
                        cp = configparser.ConfigParser(interpolation=None, strict=False)
                        cp.read_string(rendered)
                        for sec in ("Unit", "Service", "Install"):
                            self.assertIn(sec, cp)

    def test_env_example_key_parity_and_systemd_environment_file_syntax(self) -> None:
        main_srv_src = (_REPO_ROOT / "server_agent/main_server.py").read_text(encoding="utf-8")
        tree = ast.parse(main_srv_src)
        getenv_keys: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                getenv_keys.add(node.args[0].value)

        env_ex_text = (_REPO_ROOT / "server_agent/.env.example").read_text(encoding="utf-8")
        parsed_keys: dict[str, str] = {}
        for lineno, raw_line in enumerate(env_ex_text.splitlines(), 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertFalse(stripped.startswith("export "), f".env.example:{lineno} must not use 'export' for systemd EnvironmentFile")
            self.assertIn("=", stripped, f".env.example:{lineno} invalid line")
            k, v = stripped.split("=", 1)
            self.assertRegex(k, r"^[A-Z_][A-Z0-9_]*$", f".env.example:{lineno} invalid key {k!r}")
            self.assertNotIn(" #", v, f".env.example:{lineno} inline comment breaks systemd EnvironmentFile")
            parsed_keys[k] = v

        self.assertTrue(getenv_keys.issubset(parsed_keys.keys()), f"Missing keys in .env.example: {getenv_keys - parsed_keys.keys()}")

    def test_flash_bat_contracts(self) -> None:
        bat = (_REPO_ROOT / "esp32_face_os/flash.bat").read_text(encoding="utf-8")
        self.assertIn("set COM_PORT=COM3", bat)
        self.assertIn("--no-pause", bat)
        self.assertIn("--batch", bat)
        self.assertIn("west build -p auto -b esp32s3_touch_lcd_1_28/esp32s3/procpu", bat)
        self.assertIn("west flash --esp-device=%COM_PORT%", bat)
        self.assertIn("esptool.exe", bat)


class TestToolsBehavioralAndWireContracts(unittest.TestCase):
    """Tier 5: Behavioral and UTF-8 wire-level tests for tools/ using FakeSerialPort + MockESP32."""

    def test_verify_esp32_serial_full_run_and_utf8_wire_parity(self) -> None:
        from tools import verify_esp32_serial as ves

        esp = MockESP32()
        port = FakeSerialPort(esp=esp)

        # Verify _read_json_line ignores non-JSON and malformed lines
        port.open()
        port.push_rx_line("I (1) boot: noise")
        port.push_rx_line("{bad json")
        port.push_rx_line('{"t":"ping","id":"x"}')
        self.assertIsNone(ves._read_json_line(port))
        self.assertIsNone(ves._read_json_line(port))
        self.assertEqual(ves._read_json_line(port), {"t": "ping", "id": "x"})
        self.assertIsNone(ves._read_json_line(port))
        port.close()

        with (
            patch.object(ves.serial, "Serial", return_value=port),
            patch.object(ves.time, "sleep", return_value=None),
            patch.object(sys, "argv", ["verify_esp32_serial.py", "COM3", "--baud", "921600"]),
        ):
            rc = ves.main()

        self.assertEqual(rc, 0)
        wire_bytes = b"".join(port.written)
        self.assertIn("—".encode("utf-8"), wire_bytes)
        self.assertNotIn(b"\\u2014", wire_bytes)
        self.assertEqual(esp.dbg.get("srv_ms"), -1)
        self.assertEqual(esp.gpio_dirs.get(16), "in")

    def test_pc_companion_tester_utf8_wire_and_cli_loop(self) -> None:
        from tools import pc_companion_tester as pct

        self.assertTrue(pct.is_clean_text("Hello world! — OK"))
        self.assertFalse(pct.is_clean_text("bad\ufffdtext"))
        self.assertFalse(pct.is_clean_text("ctrl\x07char"))

        esp = MockESP32()
        port = FakeSerialPort(esp=esp)
        port.open()
        pct.send_json(port, {"t": "state", "emo": "HAPPY", "sub": "Hi — there"})
        wire = b"".join(port.written)
        self.assertIn("—".encode("utf-8"), wire)
        self.assertNotIn(b"\\u2014", wire)
        self.assertEqual(esp.sub, "Hi — there")

        with patch.object(pct.time, "sleep", return_value=None):
            pct.simulate_speech(port)
        self.assertEqual(esp.emo, "IDLE")

        # Exercise main() CLI command dispatch loop
        cli_cmds = iter(
            [
                "emo happy",
                "imu",
                "ble",
                "gpio 16 1",
                "gcfg 16 out none 1",
                "gset 16 0",
                "gread 16",
                "bright 50",
                "dbg ONLINE IN_CALL 25 OK",
                "ping",
                "gpio bad_int 1",
                "unknown_cmd",
                "quit",
            ]
        )
        port2 = FakeSerialPort(esp=esp)
        with (
            patch.object(pct.serial, "Serial", return_value=port2),
            patch.object(pct.time, "sleep", return_value=None),
            patch("builtins.input", side_effect=lambda _prompt="": next(cli_cmds)),
            patch.object(sys, "argv", ["pc_companion_tester.py", "COM3"]),
        ):
            pct.main()

        self.assertEqual(esp.bright, 50)
        self.assertEqual(esp.dbg["srv_ms"], 25)
        self.assertEqual(esp.gpio_vals[16], 0)


if __name__ == "__main__":
    unittest.main()
