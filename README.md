# Connected AI Companion (Tamagci)

A 3-tier embodied AI companion system built around a **Waveshare ESP32-S3-Touch-LCD-1.28-B** face & sensor co-processor, tethered via USB to a **Raspberry Pi Zero 2 W** hub with Bluetooth speaker/microphone, communicating over the post-quantum encrypted **Ixian P2P network via QuIXI** (`ixian-platform/QuIXI`) using the **`Spixi/VoIP/VoIPManager.cs`** audio streaming protocol to a remote **AI Server**.

> **Zero-Trust Edge Security Model:** All OpenAI-compatible API keys (`LLM_API_KEY`, `STT_API_KEY`, `TTS_API_KEY`) live **exclusively** on Tier 3 in `server_agent/.env` (`chmod 600`). Tier 1 (`esp32_face_os/`) and Tier 2 (`pi_hub/`) store zero API keys or provider credentials.

---

## Architecture

1. **Tier 1 — `esp32_face_os/` (Zephyr RTOS v3.7.0 LTS + LVGL v8.4)**
   * **Animated Companion Face & 4-Step Orientation**: 240×240 round GC9A01 display with 4-step hardware MADCTL rotation (`0° / 90° / 180° / 270°` with IMU hysteresis + CST816S touch coordinate rotation), 9 procedural emotions (`IDLE`, `LISTENING`, `THINKING`, `SPEAKING`, `HAPPY`, `SAD`, `SURPRISED`, `SLEEPING`, `ALERT`), 20Hz viseme mouth lip-sync, and 2–3 line rolling live subtitles.
   * **3-Screen Carousel**: Touch L/R swipe or `<`/`>` navigation between:
     * **Face**: Tap center canvas for PTT (`ptt_toggle`) or barge-in (`stop_speech` while speaking).
     * **Settings**: GPIO 2 PWM backlight brightness (`&ledc0` bound exclusively to GPIO 2), BT volume, Mic mode (`vad` / `ptt` / `duplex`), Rotation mode (`Auto 4-Way` / `Auto Flip 0-180` / `Lock 180`), BLE observer toggle, and Sleep Timeout (`30s` / `2m` / `Never`).
     * **Debug/Telemetry**: Live `Q/V/BT/RTT` link status, IMU telemetry, BLE device count, GPIO state, Vbat (`GPIO 1` ADC), angle/uptime, `Free Heap`, plus `Test Call` and `BLE Refresh` buttons.
   * **QMI8658 6-Axis IMU (`50Hz` Thread)**: Dedicated 50Hz sensor thread on I2C1 (`0x6B`/`0x6A`, GPIO 6/7) detecting `shake`, `tilt`, `pickup`, and `double_tap` gestures.
   * **BLE Passive Observer**: 32-entry table tracking nearby BLE devices (`mac`, `addr_type`, `rssi`, sanitized `name`) with 30s stale pruning.
   * **Dynamic Agent GPIO & Battery ADC**: Allows the remote AI Agent to configure/read/write allowlisted header pins (`15, 16, 17, 18, 21, 33, 34, 35, 36, 37`) and read battery voltage on `GPIO 1` ADC (`* 3` mV). Protected internal pins (`0..14, 43, 44`) fail safely with `-EACCES`, and negative pins (`pin < 0`) return `-EINVAL`.
   * **Clean UART0 NDJSON Link**: Interrupt-driven UART0 (`&uart0`) at **`921600` baud** 8N1 with boot banner, UART console, and UART logging suppressed in `prj.conf` so logs never interleave with NDJSON frames.

2. **Tier 2 — `pi_hub/` (Raspberry Pi Zero 2 W)**
   * Connects via USB serial (`/dev/ttyACM0` @ `921600` baud NDJSON) to the ESP32-S3 (`esp_serial_bridge.py`).
   * Captures BlueZ/PipeWire Bluetooth microphone audio (`vad`, `ptt`, or `duplex` mode) with touch/voice barge-in (`stop_speech`) and streams frames via `SpixiVoIPSessionManager` (`spixi.voip`) over **QuIXI** (REST `:8001` + Mosquitto MQTT `:1883`).
   * Plays incoming TTS audio frames on the Bluetooth speaker while pacing 20Hz viseme mouth frames and `ChatStream` subtitles to the ESP32-S3 with configurable Bluetooth latency compensation (`bt_latency_ms`).
   * Executes `com.tamagci.agent` hardware RPCs (`tool_call` and `tool_batch`) against the ESP32-S3 and reports `tool_res` / `hw_event` upstream.

3. **Tier 3 — `server_agent/` (Remote AI Server — Sole API Key Vault)**
   * Enforces **fail-closed** `PI_IXIAN_ADDRESS` authorization (`ALLOW_UNAUTHENTICATED_DEV=false` by default) and auto-accepts authorized `spixi.voip` sessions.
   * Holds `LLM_API_KEY`, `STT_API_KEY`, and `TTS_API_KEY` in `server_agent/.env` (`chmod 600`).
   * Runs OpenAI-compatible STT (`/v1/audio/transcriptions`), Chat Completions LLM with 6 dynamic hardware tools and tool-call-safe history trimming (`/v1/chat/completions`), and TTS (`/v1/audio/speech`), returning synced `ChatStream` text, 20Hz viseme envelopes (`tts_meta` / `tts_end`), and `spixi.voip` audio frames.

4. **Shared Protocol Library — `common_ixian/`**
   * Exact Python port of `AppDataMessage` binary serialization (`common_ixian/spixi_voip.py`, importable standalone) and `Spixi/VoIP/VoIPManager.cs` session lifecycle (`SpixiMessageCode` `21, 22, 28, 29, 30, 31, 45, 53`, including `30` `APP_REQUEST_ERROR`), paired with async `QuIXIClient` (`common_ixian/quixi_client.py`) for REST (`:8001`) and MQTT (`:1883`).

---

## Repository Layout

```text
Tamagci/
├── esp32_face_os/          # Tier 1 Zephyr v3.7.0 LTS + LVGL v8.4 firmware + flash.bat
├── common_ixian/           # Shared AppDataMessage codec, SpixiVoIPSessionManager, QuIXIClient
├── pi_hub/                 # Tier 2 Raspberry Pi Zero 2 W asyncio hub + config.yaml
├── server_agent/           # Tier 3 Remote AI Server + .env.example (API key vault)
├── deploy/                 # Step 5 setup scripts, ixian.cfg templates, systemd units, DEPLOYMENT.md
│   ├── DEPLOYMENT.md       # Step-by-step zero-trust Pi + Server deployment guide
│   ├── setup_pi.sh         # Pi Zero 2 W local setup script (zero API keys)
│   ├── setup_server.sh     # Remote server setup script (.env chmod 600)
│   ├── ixian.cfg.pi.template
│   ├── ixian.cfg.server.template
│   └── systemd/            # quixi-pi, tamagci-pi-hub, quixi-server, tamagci-server-agent
├── tools/                  # verify_esp32_serial.py, pc_companion_tester.py, test_ixian_voip.py
└── tests/                  # Offline unit, security/contract, and opaque-box E2E test suites
```

---

## Quick Start & Hardware Tools

### 1. ESP32-S3 Firmware (`esp32_face_os/`)
On Windows with Zephyr v3.7.0 LTS (`C:\zephyrproject` and `C:\zephyr-sdk-0.16.8`, board `esp32s3_touch_lcd_1_28/esp32s3/procpu`):
```cmd
esp32_face_os\flash.bat
```
For non-interactive / CI invocations (skips `pause` prompts):
```cmd
esp32_face_os\flash.bat --no-pause
```

Verify live ESP32-S3 hardware streams over USB (`COM3` @ `921600` baud):
```powershell
C:\zephyrproject\.venv\Scripts\python.exe tools\verify_esp32_serial.py COM3 --baud 921600
C:\zephyrproject\.venv\Scripts\python.exe tools\pc_companion_tester.py COM3 --baud 921600
```
(`pc_companion_tester.py` provides interactive commands: `speak`, `emo <NAME>`, `imu`, `ble`, `gpio <pin> <0|1>`, `gcfg <pin> <dir> [pull] [val]`, `gset <pin> <0|1>`, `gread <pin>`, `bright <10..100>`, `dbg <quixi> <voip> <ms> <bt>`, `ping`, `quit`.)

### 2. QuIXI P2P Configuration & Pairing
Both the Pi Zero 2 W and Remote AI Server run local Mosquitto (`127.0.0.1:1883`) and QuIXI (`http://127.0.0.1:8001/`). Starter templates are provided in `deploy/ixian.cfg.pi.template` and `deploy/ixian.cfg.server.template`:
```ini
apiPort = 8001
apiBinds = http://127.0.0.1:8001/
mqDriver = mqtt
mqHost = 127.0.0.1
mqPort = 1883
networkType = mainnet
# networkType = testnet
walletPassword = REPLACE_WITH_STRONG_WALLET_PASSWORD
```
Pair the Pi and Server wallets mutually via QuIXI REST `/addContact` and `/acceptContact` (and verify via `/contacts`), then set `PI_IXIAN_ADDRESS` in `server_agent/.env` and `server_ixian_address` in `pi_hub/config.yaml`.

### 3. Production / Step 5 Deployment (`deploy/`)
Follow **[`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md)** for complete zero-trust deployment instructions covering:
- `deploy/setup_server.sh` (installs `server_agent/.venv`, enforces `chmod 600` on `server_agent/.env`, stages `ixian.cfg.server.template`, installs `quixi-server.service` and `tamagci-server-agent.service`)
- `deploy/setup_pi.sh` (installs BlueZ/PipeWire/PortAudio/Mosquitto deps, `dialout`/`audio`/`bluetooth` groups, `pi_hub/.venv`, stages `ixian.cfg.pi.template`, installs `quixi-pi.service` and `tamagci-pi-hub.service`, zero API keys)
- QuIXI `/addContact` & `/acceptContact` pairing, `/dev/ttyACM0` @ `921600` baud, PipeWire Bluetooth audio & `bt_latency_ms`, and `systemd`/`journalctl` management.

### 4. Manual Local Development Run
**Tier 3 Remote AI Server (`server_agent/`):**
```bash
cd server_agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
# Edit .env with your API keys and PI_IXIAN_ADDRESS (keep ALLOW_UNAUTHENTICATED_DEV=false)
python main_server.py
```

**Tier 2 Raspberry Pi Zero 2 W Hub (`pi_hub/`):**
```bash
cd pi_hub
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Edit config.yaml with serial.port (/dev/ttyACM0), serial.baudrate (921600), and server_ixian_address
python main_hub.py
```

---

## Offline Verification & Testing

The `tests/` suite and `tools/test_ixian_voip.py` run **100% offline** using mocked ESP32-S3, QuIXI REST/MQTT, and OpenAI endpoints (no `COM3` hardware, live Raspberry Pi, live QuIXI node, or API keys required):

```bash
# Standard library unittest discovery across tests/
python -m unittest discover -s tests -v

# Standalone Spixi VoIP AppDataMessage & session loopback tests
python tools/test_ixian_voip.py -v

# Full pytest run (using Zephyr venv pytest)
C:\zephyrproject\.venv\Scripts\python.exe -m pytest tests tools/test_ixian_voip.py -v
```
