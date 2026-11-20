#!/usr/bin/env bash
# Flash Waveshare ESP32-S3-Touch-LCD-1.28-B from Raspberry Pi Zero 2 W (or any Linux host).
# Automatically coordinates with tamagci-pi-hub (via live API or systemctl stop/start) so
# /dev/ttyACM0 is cleanly released during flashing and restored at 921600 baud afterward.
#
# Usage:
#   bash esp32_face_os/flash_pi.sh [path/to/zephyr.bin] [/dev/ttyACM0]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW_BIN="${1:-${REPO_DIR}/firmware/zephyr.bin}"
SERIAL_PORT="${2:-/dev/ttyACM0}"
BAUD="921600"

if [[ ! -f "${FW_BIN}" ]]; then
    if [[ -f "/opt/tamagci/firmware/zephyr.bin" ]]; then
        FW_BIN="/opt/tamagci/firmware/zephyr.bin"
    else
        echo "Error: Firmware binary not found at ${FW_BIN}" >&2
        exit 1
    fi
fi

echo "================================================================"
echo "  Flashing ESP32-S3-Touch-LCD-1.28-B"
echo "  Firmware : ${FW_BIN}"
echo "  Port     : ${SERIAL_PORT} @ ${BAUD} baud"
echo "================================================================"

# If PiWebGUI is running on 127.0.0.1:8082, trigger a coordinated hot-flash that pauses & resumes EspSerialBridge
if command -v curl >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:8082/api/status" >/dev/null 2>&1; then
    echo "==> Coordinating live reflash via Pi Hub Web GUI (http://127.0.0.1:8082/api/esp32/firmware/flash)..."
    RESP="$(curl -fsS -X POST "http://127.0.0.1:8082/api/esp32/firmware/flash" \
        -H "Content-Type: application/json" \
        -d "{\"source\":\"${FW_BIN}\",\"baudrate\":${BAUD}}")"
    echo "${RESP}"
    exit 0
fi

# Otherwise, temporarily pause tamagci-pi-hub.service if running under systemd
HUB_WAS_ACTIVE=0
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet tamagci-pi-hub.service; then
    echo "==> Stopping tamagci-pi-hub.service to release ${SERIAL_PORT}..."
    sudo systemctl stop tamagci-pi-hub.service
    HUB_WAS_ACTIVE=1
fi

restore_hub() {
    if [[ "${HUB_WAS_ACTIVE}" -eq 1 ]]; then
        echo "==> Restarting tamagci-pi-hub.service..."
        sudo systemctl start tamagci-pi-hub.service || true
    fi
}
trap restore_hub EXIT

ESPTOOL_BIN="esptool.py"
if [[ -x "${REPO_DIR}/pi_hub/.venv/bin/python" ]]; then
    "${REPO_DIR}/pi_hub/.venv/bin/python" -m esptool \
        --chip esp32s3 --port "${SERIAL_PORT}" --baud "${BAUD}" \
        --before default_reset --after hard_reset \
        write_flash -z --flash_mode dio --flash_freq 80m --flash_size 16MB \
        0x0 "${FW_BIN}"
elif command -v "${ESPTOOL_BIN}" >/dev/null 2>&1; then
    "${ESPTOOL_BIN}" \
        --chip esp32s3 --port "${SERIAL_PORT}" --baud "${BAUD}" \
        --before default_reset --after hard_reset \
        write_flash -z --flash_mode dio --flash_freq 80m --flash_size 16MB \
        0x0 "${FW_BIN}"
else
    python3 -m esptool \
        --chip esp32s3 --port "${SERIAL_PORT}" --baud "${BAUD}" \
        --before default_reset --after hard_reset \
        write_flash -z --flash_mode dio --flash_freq 80m --flash_size 16MB \
        0x0 "${FW_BIN}"
fi

echo "==> Flash complete!"
