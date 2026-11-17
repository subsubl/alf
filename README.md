# Connected AI Companion (ESP32-S3 Zephyr OS + Raspberry Pi Zero 2 W + Ixian QuIXI / Spixi VoIP)

A 3-tier embodied AI companion system built around a **Waveshare ESP32-S3-Touch-LCD-1.28-B** face & sensor co-processor, tethered via USB to a **Raspberry Pi Zero 2 W** hub with Bluetooth speaker/microphone, communicating over the post-quantum encrypted **Ixian P2P network via QuIXI** (`ixian-platform/QuIXI`) using the **`Spixi/VoIP/VoIPManager.cs`** audio streaming protocol to a remote **AI Server** holding all OpenAI-compatible API keys.

---

## Architecture

1. **Tier 1 — `esp32_face_os/` (Zephyr RTOS v3.7 LTS + LVGL v8.4)**
   * **Animated Companion Face**: Procedural eyes & viseme-driven mouth synchronized at 20Hz with voice playback, plus real-time rolling subtitles.
   * **3-Screen Carousel**: Touch swipe or `<`/`>` navigation between **Face**, **Settings** (brightness, BT volume, VAD/PTT mic mode, BLE toggle), and **Debug/Telemetry** (QuIXI/VoIP status, IMU, BLE count, GPIO state, Vbat).
   * **QMI8658 6-Axis IMU**: Polls acceleration/gyroscope/temperature at 25Hz and detects `shake` and `tilt` gestures.
   * **BLE Passive Observer**: Continuously tracks nearby BLE devices, names, and RSSI signal strengths.
   * **Dynamic Agent GPIO & Battery ADC**: Allows the remote AI Agent to dynamically configure and control free header pins (`15, 16, 17, 18, 21, 33, 34, 35, 36, 37`) while protecting internal display/I²C/UART buses.

2. **Tier 2 — `pi_hub/` (Raspberry Pi Zero 2 W)**
   * Connects via USB serial (`/dev/ttyACM0` @ `115200` baud NDJSON) to the ESP32-S3.
   * Captures Bluetooth microphone audio via **Auto VAD** or **Tap-to-Talk (PTT)** and streams frames using `SpixiVoIPSessionManager` (`spixi.voip`) over **QuIXI** (REST `/sendSpixiMessage` + Mosquitto MQTT `AppData/#`).
   * Plays incoming TTS audio frames (`VoIPManager.onData`) on the Bluetooth speaker while pacing 20Hz viseme mouth frames and `ChatStream` subtitles to the ESP32-S3 with configurable Bluetooth latency offset (`bt_latency_ms`).

3. **Tier 3 — `server_agent/` (Remote AI Server — Only Place with API Keys)**
   * Connects to **Server QuIXI** (REST + Mosquitto MQTT).
   * Runs STT (`/v1/audio/transcriptions`), LLM + dynamic hardware tool calls (`/v1/chat/completions`), and TTS (`/v1/audio/speech`), returning synced `ChatStream` text, 20Hz viseme envelopes, and `spixi.voip` audio frames.

4. **Shared Protocol Library — `common_ixian/`**
   * Exact Python port of `AppDataMessage` binary serialization and `Spixi/VoIP/VoIPManager.cs` session lifecycle (`SpixiMessageCode` 21, 22, 28, 29, 31, 45, 53) paired with QuIXI's HTTP REST API and MQTT topics.

---

## Quick Start

### 1. ESP32-S3 Firmware (`esp32_face_os`)
On Windows with `C:\zephyrproject` and `C:\zephyr-sdk-0.16.8`:
```cmd
esp32_face_os\flash.bat
```
Verify live hardware over USB (`COM3`):
```powershell
C:\zephyrproject\.venv\Scripts\python.exe tools\verify_esp32_serial.py COM3
C:\zephyrproject\.venv\Scripts\python.exe tools\pc_companion_tester.py COM3
```

### 2. QuIXI Configuration (Both Pi Zero 2 W and AI Server)
Ensure Mosquitto MQTT is running (`sudo apt install mosquitto`) and configure `ixian.cfg` on both QuIXI nodes:
```ini
apiPort = 8001
mqDriver = mqtt
mqHost = 127.0.0.1
mqPort = 1883
```
Add each node's Ixian wallet address as an accepted contact on the peer via `/addContact` and `/acceptContact`.

### 3. Remote AI Server (`server_agent`)
```bash
cd server_agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your OpenAI-compatible API keys & PI_IXIAN_ADDRESS
python main_server.py
```

### 4. Raspberry Pi Zero 2 W Hub (`pi_hub`)
```bash
cd pi_hub
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Edit config.yaml with your serial port (/dev/ttyACM0) & server_ixian_address
python main_hub.py
```
