#!/usr/bin/env bash
set -euo pipefail

# Setup script for Tier 2: Raspberry Pi Zero 2 W Hub (tamagci)
# Zero-Trust Edge Security Model: no AI provider credentials are stored on the Pi.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUIXI_DIR="${QUIXI_DIR:-/opt/quixi}"
TARGET_USER="${TAMAGCI_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"

if [[ "${EUID}" -eq 0 ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

if ! id "${TARGET_USER}" >/dev/null 2>&1; then
    echo "Creating service user '${TARGET_USER}'..."
    ${SUDO} useradd --system --user-group --create-home --shell /usr/sbin/nologin "${TARGET_USER}"
fi

echo "[1/6] Installing Raspberry Pi OS dependencies..."
${SUDO} apt-get update
${SUDO} apt-get install -y \
    mosquitto \
    mosquitto-clients \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    build-essential \
    libffi-dev \
    portaudio19-dev \
    libportaudio2 \
    libasound2-dev \
    bluez \
    pipewire \
    pipewire-pulse \
    wireplumber \
    libspa-0.2-bluetooth \
    curl \
    jq

echo "[2/6] Enabling Mosquitto MQTT and Bluetooth services..."
${SUDO} systemctl enable --now mosquitto
${SUDO} systemctl enable --now bluetooth

echo "[3/6] Configuring hardware permissions for user '${TARGET_USER}'..."
${SUDO} usermod -aG dialout,audio,bluetooth "${TARGET_USER}"
if command -v loginctl >/dev/null 2>&1; then
    ${SUDO} loginctl enable-linger "${TARGET_USER}" || true
fi

echo "[4/6] Creating pi_hub virtual environment and installing Python requirements..."
if [[ ! -w "${REPO_DIR}" ]] || [[ ! -w "${REPO_DIR}/pi_hub" ]]; then
    REPO_SUDO="${SUDO}"
elif [[ -e "${REPO_DIR}/pi_hub/.venv" ]] && [[ ! -w "${REPO_DIR}/pi_hub/.venv" ]]; then
    REPO_SUDO="${SUDO}"
else
    REPO_SUDO=""
fi
if [[ ! -d "${REPO_DIR}/pi_hub/.venv" ]]; then
    ${REPO_SUDO} python3 -m venv "${REPO_DIR}/pi_hub/.venv"
fi
${REPO_SUDO} "${REPO_DIR}/pi_hub/.venv/bin/pip" install --upgrade pip
${REPO_SUDO} "${REPO_DIR}/pi_hub/.venv/bin/pip" install -r "${REPO_DIR}/pi_hub/requirements.txt"
if [[ "${TARGET_USER}" != "$(id -un)" ]] || [[ -n "${REPO_SUDO}" ]]; then
    ${SUDO} chown -R "${TARGET_USER}:" "${REPO_DIR}/pi_hub/.venv"
fi

echo "[5/6] Staging QuIXI configuration template into ${QUIXI_DIR}/ixian.cfg..."
${SUDO} mkdir -p "${QUIXI_DIR}"
if [[ ! -f "${QUIXI_DIR}/ixian.cfg" ]]; then
    ${SUDO} cp "${REPO_DIR}/deploy/ixian.cfg.pi.template" "${QUIXI_DIR}/ixian.cfg"
    ${SUDO} chmod 600 "${QUIXI_DIR}/ixian.cfg"
    echo "Staged ${QUIXI_DIR}/ixian.cfg (edit walletPassword before starting QuIXI)."
else
    echo "Preserving existing ${QUIXI_DIR}/ixian.cfg."
fi
${SUDO} chown -R "${TARGET_USER}:" "${QUIXI_DIR}"

echo "[6/6] Rendering and installing systemd units..."
render_unit() {
    local src="$1"
    local dst="$2"
    SRC_PATH="${src}" \
    TARGET_USER_VAL="${TARGET_USER}" \
    REPO_DIR_VAL="${REPO_DIR}" \
    QUIXI_DIR_VAL="${QUIXI_DIR}" \
    python3 -c '
import os, re
from pathlib import Path

text = Path(os.environ["SRC_PATH"]).read_text(encoding="utf-8")
repls = {
    "/opt/tamagci": os.environ["REPO_DIR_VAL"],
    "/opt/quixi": os.environ["QUIXI_DIR_VAL"],
}
pat = re.compile("|".join(re.escape(k) for k in sorted(repls, key=len, reverse=True)))
text = re.sub(r"(?m)^User=.*$", lambda _m: "User=" + os.environ["TARGET_USER_VAL"], text)
text = pat.sub(lambda m: repls[m.group(0)], text)
print(text, end="")
' | ${SUDO} tee "${dst}" >/dev/null
    ${SUDO} chmod 644 "${dst}"
}

render_unit "${REPO_DIR}/deploy/systemd/quixi-pi.service" "/etc/systemd/system/quixi-pi.service"
render_unit "${REPO_DIR}/deploy/systemd/tamagci-pi-hub.service" "/etc/systemd/system/tamagci-pi-hub.service"
${SUDO} systemctl daemon-reload

echo ""
echo "Raspberry Pi Zero 2 W setup complete. Next steps:"
echo "  1. Place the QuIXI binary at ${QUIXI_DIR}/QuIXI and set walletPassword in ${QUIXI_DIR}/ixian.cfg."
echo "  2. Connect the ESP32-S3 over USB (/dev/ttyACM0 @ 921600 baud) and pair your Bluetooth headset/speaker via bluetoothctl."
echo "  3. Set server_ixian_address in ${REPO_DIR}/pi_hub/config.yaml and pair contacts via QuIXI /addContact and /acceptContact."
echo "  4. Start services: sudo systemctl enable --now quixi-pi.service tamagci-pi-hub.service"
echo "  5. Re-login or reboot so dialout/audio/bluetooth group memberships take effect."
