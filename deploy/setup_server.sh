#!/usr/bin/env bash
set -euo pipefail

# Setup script for Tier 3: Remote AI Server (tamagci)
# Holds all AI provider credentials privately in server_agent/.env (chmod 600).

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUIXI_DIR="${QUIXI_DIR:-/opt/quixi}"
TARGET_USER="${TAMAGCI_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"

if [[ "${EUID}" -eq 0 ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

if ! id "${TARGET_USER}" >/dev/null 2>&1; then
    echo "Creating dedicated service user '${TARGET_USER}'..."
    ${SUDO} useradd --system --user-group --home-dir "${REPO_DIR}" --shell /usr/sbin/nologin "${TARGET_USER}"
fi

echo "[1/6] Installing Remote AI Server OS dependencies..."
${SUDO} apt-get update
${SUDO} apt-get install -y \
    mosquitto \
    mosquitto-clients \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    curl \
    jq

echo "[2/6] Enabling Mosquitto MQTT service..."
${SUDO} systemctl enable --now mosquitto

echo "[3/6] Creating server_agent virtual environment and installing Python requirements..."
if [[ ! -w "${REPO_DIR}" ]] || [[ ! -w "${REPO_DIR}/server_agent" ]]; then
    REPO_SUDO="${SUDO}"
elif [[ -e "${REPO_DIR}/server_agent/.venv" ]] && [[ ! -w "${REPO_DIR}/server_agent/.venv" ]]; then
    REPO_SUDO="${SUDO}"
else
    REPO_SUDO=""
fi
if [[ ! -d "${REPO_DIR}/server_agent/.venv" ]]; then
    ${REPO_SUDO} python3 -m venv "${REPO_DIR}/server_agent/.venv"
fi
${REPO_SUDO} "${REPO_DIR}/server_agent/.venv/bin/pip" install --upgrade pip
${REPO_SUDO} "${REPO_DIR}/server_agent/.venv/bin/pip" install -r "${REPO_DIR}/server_agent/requirements.txt"
if [[ "${TARGET_USER}" != "$(id -un)" ]] || [[ -n "${REPO_SUDO}" ]]; then
    ${SUDO} chown -R "${TARGET_USER}:" "${REPO_DIR}/server_agent/.venv"
fi

echo "[4/6] Preparing server_agent/.env secrets vault..."
if [[ -n "${REPO_SUDO}" ]]; then
    ENV_SUDO="${REPO_SUDO}"
elif [[ -e "${REPO_DIR}/server_agent/.env" ]] && [[ ! -O "${REPO_DIR}/server_agent/.env" ]]; then
    ENV_SUDO="${SUDO}"
else
    ENV_SUDO=""
fi
if [[ ! -f "${REPO_DIR}/server_agent/.env" ]]; then
    ${ENV_SUDO} cp "${REPO_DIR}/server_agent/.env.example" "${REPO_DIR}/server_agent/.env"
    echo "Created ${REPO_DIR}/server_agent/.env from .env.example."
else
    echo "Preserving existing ${REPO_DIR}/server_agent/.env."
fi
${ENV_SUDO} chmod 600 "${REPO_DIR}/server_agent/.env"
if [[ "${TARGET_USER}" != "$(id -un)" ]] || [[ -n "${ENV_SUDO}" ]]; then
    ${SUDO} chown "${TARGET_USER}:" "${REPO_DIR}/server_agent/.env"
fi

echo "[5/6] Staging QuIXI configuration template into ${QUIXI_DIR}/ixian.cfg..."
${SUDO} mkdir -p "${QUIXI_DIR}"
if [[ ! -f "${QUIXI_DIR}/ixian.cfg" ]]; then
    ${SUDO} cp "${REPO_DIR}/deploy/ixian.cfg.server.template" "${QUIXI_DIR}/ixian.cfg"
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

render_unit "${REPO_DIR}/deploy/systemd/quixi-server.service" "/etc/systemd/system/quixi-server.service"
render_unit "${REPO_DIR}/deploy/systemd/tamagci-server-agent.service" "/etc/systemd/system/tamagci-server-agent.service"
${SUDO} systemctl daemon-reload

echo ""
echo "Remote AI Server setup complete. Next steps:"
echo "  1. Place the QuIXI binary at ${QUIXI_DIR}/QuIXI and set walletPassword in ${QUIXI_DIR}/ixian.cfg."
echo "  2. Edit $REPO_DIR/server_agent/.env with your LLM/STT/TTS API keys and PI_IXIAN_ADDRESS (keep ALLOW_UNAUTHENTICATED_DEV=false)."
echo "  3. Pair the Pi and Server QuIXI wallets via /addContact and /acceptContact."
echo "  4. Start services: sudo systemctl enable --now quixi-server.service tamagci-server-agent.service"
