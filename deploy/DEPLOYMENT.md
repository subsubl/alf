# Tamagci Connected AI Companion — Zero-Trust Deployment Guide

This guide describes how to deploy the 3-tier **Tamagci Connected AI Companion** across a **Tier 3 Remote AI Server** and a **Tier 2 Raspberry Pi Zero 2 W** tethered to a **Tier 1 Waveshare ESP32-S3-Touch-LCD-1.28-B** co-processor.

---

## 1. Zero-Trust Edge Security Architecture

All AI provider credentials are isolated exclusively on **Tier 3**:

| Tier | Host / Component | Secrets Held | Network / Hardware Links |
|---|---|---|---|
| **Tier 1** | ESP32-S3 (`esp32_face_os/`) | **None** | USB-UART CH343 (`/dev/ttyACM0` @ `921600` baud 8N1 NDJSON) |
| **Tier 2** | Raspberry Pi Zero 2 W (`pi_hub/`) | **None** (zero API keys; stores only `server_ixian_address` in `pi_hub/config.yaml`) | USB serial (`921600`), BlueZ/PipeWire BT audio, local QuIXI REST (`:8001`) + Mosquitto (`:1883`) |
| **Tier 3** | Remote AI Server (`server_agent/`) | **API key vault** in `server_agent/.env` (`chmod 600`): `LLM_API_KEY`, `STT_API_KEY`, `TTS_API_KEY`, plus fail-closed `PI_IXIAN_ADDRESS` | Local QuIXI REST (`:8001`) + Mosquitto (`:1883`), OpenAI-compatible HTTPS APIs |

- **No API keys on Edge Tiers:** Never copy `.env` or `LLM_API_KEY`/`STT_API_KEY`/`TTS_API_KEY` to the Raspberry Pi or ESP32-S3.
- **Fail-Closed Authorization:** `server_agent/main_server.py` rejects all incoming Ixian addresses unless `PI_IXIAN_ADDRESS` matches the paired Pi wallet (or `ALLOW_UNAUTHENTICATED_DEV=true` is explicitly set for local offline development only).

---

## 2. Prerequisites & Repository Placement

1. **Tier 1 (ESP32-S3):** Flashed with `esp32_face_os/` (Zephyr v3.7.0 LTS, UART0 at `921600` baud).
2. **Tier 2 (Raspberry Pi Zero 2 W):** Raspberry Pi OS (Bookworm/Debian 12+ recommended) with BlueZ/PipeWire audio and USB connection to the ESP32-S3.
3. **Tier 3 (Remote AI Server):** Debian/Ubuntu Linux host with public/routable connectivity for the Ixian P2P network and HTTPS access to your OpenAI-compatible STT/LLM/TTS provider.
4. **QuIXI Binary (`ixian-platform/QuIXI`):** Build or download the `QuIXI` release binary for each host's architecture and place it at `/opt/quixi/QuIXI` (or set `QUIXI_DIR=/your/path` before running the setup scripts).
5. **Repository Directory:** Clone or copy this repository to `/opt/tamagci` on both the Remote AI Server and the Raspberry Pi Zero 2 W (the setup scripts automatically substitute your actual checkout path into the installed `systemd` unit files).

---

## 3. QuIXI & Mosquitto Configuration (`ixian.cfg`)

Both Tier 2 and Tier 3 run a local **Mosquitto** MQTT broker (`127.0.0.1:1883`) and a local **QuIXI** gateway (`http://127.0.0.1:8001/`). The templates in `deploy/` configure:

- `deploy/ixian.cfg.pi.template` (for Tier 2 Pi Zero 2 W)
- `deploy/ixian.cfg.server.template` (for Tier 3 Remote AI Server)

Key settings in both templates:

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

> **Important:** `ixian.cfg` and QuIXI wallet files (`*.wal`) are intentionally staged outside the git repository (in `/opt/quixi/`) with `chmod 600` so they are never committed.

---

## 4. Step 1 — Deploy Tier 3 Remote AI Server

On the Remote AI Server:

```bash
cd /opt/tamagci
TAMAGCI_USER=tamagci bash deploy/setup_server.sh
```

What `deploy/setup_server.sh` does:
1. Installs OS dependencies (`mosquitto`, `mosquitto-clients`, `python3`, `python3-venv`, `python3-pip`, `python3-dev`, `curl`, `jq`).
2. Enables and starts `mosquitto`.
3. Creates `server_agent/.venv` and installs `server_agent/requirements.txt`.
4. Copies `server_agent/.env.example` to `server_agent/.env` (if absent) and enforces `chmod 600 "$REPO_DIR/server_agent/.env"`.
5. Stages `deploy/ixian.cfg.server.template` to `/opt/quixi/ixian.cfg` (`chmod 600`, if absent).
6. Renders and installs `deploy/systemd/quixi-server.service` and `deploy/systemd/tamagci-server-agent.service` into `/etc/systemd/system/` and runs `systemctl daemon-reload`.

Next, configure the server files:
1. Set a strong `walletPassword` in `/opt/quixi/ixian.cfg` (and choose `mainnet` or `testnet`).
2. Edit `/opt/tamagci/server_agent/.env` (`chmod 600`):
   - Fill in `LLM_API_KEY`, `STT_API_KEY`, `TTS_API_KEY` (and `LLM_BASE_URL`/`LLM_MODEL`/`TTS_VOICE` if using custom endpoints).
   - Keep `ALLOW_UNAUTHENTICATED_DEV=false`.
   - Leave `PI_IXIAN_ADDRESS` to be filled in Step 3 below once QuIXI wallets are generated.

---

## 5. Step 2 — Deploy Tier 2 Raspberry Pi Zero 2 W

On the Raspberry Pi Zero 2 W:

```bash
cd /opt/tamagci
TAMAGCI_USER=pi bash deploy/setup_pi.sh
```

What `deploy/setup_pi.sh` does:
1. Installs OS dependencies (`mosquitto`, `mosquitto-clients`, Python build/venv tools, `portaudio19-dev`, `libportaudio2`, `libasound2-dev`, `bluez`, `pipewire`, `pipewire-pulse`, `wireplumber`, `libspa-0.2-bluetooth`, `curl`, `jq`).
2. Enables `mosquitto` and `bluetooth`.
3. Adds the target user to `dialout`, `audio`, and `bluetooth` groups and enables `loginctl enable-linger` so headless `systemd` sessions can access user PipeWire Bluetooth audio (`XDG_RUNTIME_DIR=/run/user/%U`).
4. Creates `pi_hub/.venv` and installs `pi_hub/requirements.txt`.
5. Stages `deploy/ixian.cfg.pi.template` to `/opt/quixi/ixian.cfg` (`chmod 600`, if absent).
6. Renders and installs `deploy/systemd/quixi-pi.service` and `deploy/systemd/tamagci-pi-hub.service` into `/etc/systemd/system/` and runs `systemctl daemon-reload`.

### Hardware & Bluetooth Audio Setup on Pi
1. **ESP32-S3 USB Serial:** Plug the Waveshare ESP32-S3 into the Pi's USB OTG data port. Verify `/dev/ttyACM0` appears and `pi_hub/config.yaml` is set to:
   ```yaml
   serial:
     port: /dev/ttyACM0
     baudrate: 921600
   ```
   (Re-login or reboot after `setup_pi.sh` so `dialout`, `audio`, and `bluetooth` group memberships take effect.)
2. **BlueZ / PipeWire Bluetooth Pairing:** Pair and trust your Bluetooth speaker/microphone headset:
   ```bash
   bluetoothctl
   # Inside bluetoothctl:
   power on
   agent on
   scan on
   pair <BT_MAC>
   trust <BT_MAC>
   connect <BT_MAC>
   ```
   Confirm PipeWire routes the default sink/source (`wpctl status` or `pactl info`) and tune `audio.bt_latency_ms` in `pi_hub/config.yaml` (default `150` ms) so the 20Hz viseme mouth animation and subtitles align with Bluetooth speaker playback.
3. **QuIXI Password:** Set a strong `walletPassword` in `/opt/quixi/ixian.cfg` (matching the `mainnet`/`testnet` choice of the server).

---

## 6. Step 3 — Mutual QuIXI P2P Wallet Pairing (`/addContact` & `/acceptContact`)

Start the QuIXI services first on both hosts so each node initializes its Ixian wallet:

```bash
# On Remote AI Server:
sudo systemctl enable --now quixi-server.service

# On Raspberry Pi Zero 2 W:
sudo systemctl enable --now quixi-pi.service
```

1. **Retrieve both wallet addresses** via the local QuIXI REST API (`http://127.0.0.1:8001`):
   ```bash
   curl -s http://127.0.0.1:8001/myWallet
   ```
   Record:
   - `<SERVER_IXIAN_ADDRESS>` (from Tier 3 Server)
   - `<PI_IXIAN_ADDRESS>` (from Tier 2 Pi)

2. **Pair contacts mutually** using QuIXI REST `/addContact` and `/acceptContact`:
   - **On the Pi** (send contact request to the Server):
     ```bash
     curl -s -X POST http://127.0.0.1:8001/addContact \
       -H "Content-Type: application/json" \
       -d '{"method":"addContact","params":{"address":"<SERVER_IXIAN_ADDRESS>"}}'
     ```
   - **On the Server** (accept the Pi contact request):
     ```bash
     curl -s -X POST http://127.0.0.1:8001/acceptContact \
       -H "Content-Type: application/json" \
       -d '{"method":"acceptContact","params":{"address":"<PI_IXIAN_ADDRESS>"}}'
     ```
   - **Verify accepted contact state** on both nodes via `/contacts`:
     ```bash
     curl -s -X POST http://127.0.0.1:8001/contacts \
       -H "Content-Type: application/json" \
       -d '{"method":"contacts","params":{}}'
     ```

3. **Bind Zero-Trust Peer Addresses:**
   - On **Tier 3 Server** (`/opt/tamagci/server_agent/.env`):
     ```ini
     PI_IXIAN_ADDRESS=<PI_IXIAN_ADDRESS>
     ALLOW_UNAUTHENTICATED_DEV=false
     ```
   - On **Tier 2 Pi** (`/opt/tamagci/pi_hub/config.yaml`):
     ```yaml
     quixi:
       rest_url: http://127.0.0.1:8001
       mqtt_host: 127.0.0.1
       mqtt_port: 1883
       server_ixian_address: <SERVER_IXIAN_ADDRESS>
     ```

---

## 7. Step 4 — Enable & Manage `systemd` Services

Start and enable the companion services after wallet pairing is complete:

```bash
# On Remote AI Server:
sudo systemctl enable --now quixi-server.service tamagci-server-agent.service

# On Raspberry Pi Zero 2 W:
sudo systemctl enable --now quixi-pi.service tamagci-pi-hub.service
```

Service unit dependency chain:
- `quixi-server.service` -> `After=network-online.target mosquitto.service`
- `tamagci-server-agent.service` -> `After=network-online.target mosquitto.service quixi-server.service`, loads `EnvironmentFile=.../server_agent/.env`
- `quixi-pi.service` -> `After=network-online.target mosquitto.service`
- `tamagci-pi-hub.service` -> `After=network-online.target mosquitto.service quixi-pi.service bluetooth.target`, runs without any `.env` file (`SupplementaryGroups=dialout audio bluetooth`, `Environment=XDG_RUNTIME_DIR=/run/user/%U`)

---

## 8. Verification & `journalctl` Troubleshooting

- **Check service status & live logs:**
  ```bash
  # Tier 3 Server
  sudo systemctl status quixi-server tamagci-server-agent
  journalctl -u quixi-server -u tamagci-server-agent -f

  # Tier 2 Pi
  sudo systemctl status quixi-pi tamagci-pi-hub
  journalctl -u quixi-pi -u tamagci-pi-hub -f
  ```
- **USB Serial (`/dev/ttyACM0` @ `921600` baud):**
  - Confirm `ls -l /dev/ttyACM0` shows group `dialout` and your Pi user is in `dialout` (`groups`).
  - Confirm `pi_hub/config.yaml` specifies `baudrate: 921600` matching `esp32_face_os/app.overlay`.
- **Mosquitto & QuIXI Health:**
  - Verify MQTT messages on localhost: `mosquitto_sub -h 127.0.0.1 -p 1883 -t '#' -v`.
  - Verify QuIXI REST: `curl -s http://127.0.0.1:8001/contacts`.
- **Bluetooth / PipeWire Audio in Headless `systemd`:**
  - Confirm `loginctl show-user pi -p Linger` reports `Linger=yes` and `XDG_RUNTIME_DIR=/run/user/%U` points to an active PipeWire socket.
- **Offline Regression Tests (No Live Hardware/API Keys Required):**
  ```bash
  python3 -m unittest discover -s tests -v
  python3 tools/test_ixian_voip.py -v
  ```
