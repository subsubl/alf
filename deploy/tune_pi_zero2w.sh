#!/usr/bin/env bash
set -euo pipefail

# Raspberry Pi Zero 2 W (512 MB RAM, 4x Cortex-A53 @ 1 GHz, SD card, 2.4 GHz Wi-Fi) tuning for the Alf hub.
# Idempotent: safe to re-run. Invoked automatically by deploy/setup_pi.sh (set TAMAGCI_SKIP_TUNING=1 to skip).
# Zero-Trust: touches only OS performance settings; no credentials are read or written.

if [[ "${EUID}" -eq 0 ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

IMAGE_BUILD="${TAMAGCI_IMAGE_BUILD:-0}"

write_file() {
    # write_file <path> <content>: writes only when content differs (reduces SD card wear)
    local dst="$1"
    local content="$2"
    if [[ -f "${dst}" ]] && [[ "$(cat "${dst}")" == "${content}" ]]; then
        return 0
    fi
    ${SUDO} mkdir -p "$(dirname "${dst}")"
    printf '%s\n' "${content}" | ${SUDO} tee "${dst}" >/dev/null
    ${SUDO} chmod 644 "${dst}"
}

disable_unit() {
    local unit="$1"
    if [[ "${IMAGE_BUILD}" == "1" ]]; then
        ${SUDO} systemctl disable "${unit}" >/dev/null 2>&1 || true
    else
        ${SUDO} systemctl disable --now "${unit}" >/dev/null 2>&1 || true
    fi
}

echo "[tune 1/6] Compressed RAM swap (zram) instead of SD-card swap..."
${SUDO} apt-get install -y zram-tools >/dev/null 2>&1 || echo "  (zram-tools unavailable; skipping)"
if [[ -d /etc/default ]]; then
    write_file /etc/default/zramswap "# Managed by tamagci tune_pi_zero2w.sh
ALGO=lz4
PERCENT=50
PRIORITY=100"
fi
disable_unit dphys-swapfile.service
if [[ "${IMAGE_BUILD}" == "1" ]]; then
    ${SUDO} systemctl enable zramswap.service >/dev/null 2>&1 || true
else
    ${SUDO} systemctl enable --now zramswap.service >/dev/null 2>&1 || true
fi

echo "[tune 2/6] Kernel VM and network sysctls..."
write_file /etc/sysctl.d/99-tamagci-zero2w.conf "# Managed by tamagci tune_pi_zero2w.sh
vm.swappiness=100
vm.vfs_cache_pressure=80
vm.dirty_background_ratio=5
vm.dirty_ratio=15
vm.page-cluster=0
net.ipv4.tcp_fastopen=3"

echo "[tune 3/6] Volatile journald (less SD wear and RAM)..."
write_file /etc/systemd/journald.conf.d/99-tamagci.conf "# Managed by tamagci tune_pi_zero2w.sh
[Journal]
Storage=volatile
RuntimeMaxUse=24M
RuntimeMaxFileSize=4M
ForwardToSyslog=no"

echo "[tune 4/6] Wi-Fi power-save off (lower VoIP latency and jitter)..."
if [[ -d /etc/NetworkManager ]]; then
    write_file /etc/NetworkManager/conf.d/99-tamagci-wifi-powersave.conf "# Managed by tamagci tune_pi_zero2w.sh
[connection]
wifi.powersave = 2"
fi

echo "[tune 5/6] Disabling unused background services and CPU-heavy timers..."
for unit in triggerhappy.service ModemManager.service apt-daily.timer apt-daily-upgrade.timer man-db.timer; do
    disable_unit "${unit}"
done

echo "[tune 6/6] Boot firmware: minimal GPU memory, no unused onboard audio/camera/display probes..."
BOOT_CFG=""
for cand in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "${cand}" ]]; then
        BOOT_CFG="${cand}"
        break
    fi
done
if [[ -n "${BOOT_CFG}" ]]; then
    for setting in "gpu_mem=16" "dtparam=audio=off" "camera_auto_detect=0" "display_auto_detect=0"; do
        key="${setting%%=*}"
        if ! grep -Eq "^[[:space:]]*${key}(=|[[:space:]])" "${BOOT_CFG}"; then
            printf '%s\n' "${setting}" | ${SUDO} tee -a "${BOOT_CFG}" >/dev/null
        fi
    done
else
    echo "  (no boot config.txt found; skipping firmware tuning)"
fi

echo "Raspberry Pi Zero 2 W tuning complete. Reboot to apply boot firmware and zram changes."
