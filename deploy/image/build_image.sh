#!/usr/bin/env bash
# Build a ready-to-flash Raspberry Pi Zero 2 W image for the Tamagci hub (Tier 2).
#
# Takes an official Raspberry Pi OS Lite (arm64) image, boots it in a qemu-user chroot and runs
# deploy/setup_pi.sh in image mode (TAMAGCI_IMAGE_BUILD=1). Zero-trust: NO API keys or wallet
# passwords are baked in; those are provisioned per device after first boot.
#
# Usage (as root, on an x86_64 or arm64 Debian/Ubuntu host with qemu-user-static, parted, e2fsprogs, xz):
#   sudo deploy/image/build_image.sh <raspios-lite-arm64.img> <quixi-linux-arm64-dir> [extra-grow-MiB]
set -euo pipefail

IMG="${1:?usage: $0 <base.img> <quixi-publish-dir> [extra-grow-MiB]}"
QUIXI_SRC="${2:?usage: $0 <base.img> <quixi-publish-dir> [extra-grow-MiB]}"
GROW_MIB="${3:-2048}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MNT="$(mktemp -d /tmp/tamagci-rootfs.XXXXXX)"
SVC_USER="tamagci"
LOOP=""

cleanup() {
    set +e
    for m in dev/pts dev proc sys boot/firmware boot ""; do
        mountpoint -q "${MNT}/${m}" && umount -l "${MNT}/${m}"
    done
    [[ -n "${LOOP}" ]] && losetup -d "${LOOP}"
    rmdir "${MNT}" 2>/dev/null
}
trap cleanup EXIT

[[ "${EUID}" -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f "${QUIXI_SRC}/QuIXI" ]] || { echo "Missing ${QUIXI_SRC}/QuIXI (linux-arm64 publish output)." >&2; exit 1; }

echo "==> Growing ${IMG} by ${GROW_MIB} MiB"
truncate -s "+${GROW_MIB}M" "${IMG}"
LOOP="$(losetup --find --show --partscan "${IMG}")"
parted -s "${LOOP}" resizepart 2 100%
e2fsck -fy "${LOOP}p2" || true
resize2fs "${LOOP}p2"

echo "==> Mounting rootfs and boot partitions"
mount "${LOOP}p2" "${MNT}"
mkdir -p "${MNT}/boot/firmware"
mount "${LOOP}p1" "${MNT}/boot/firmware" || mount "${LOOP}p1" "${MNT}/boot" || true
mount --bind /dev "${MNT}/dev"
mount --bind /dev/pts "${MNT}/dev/pts"
mount -t proc proc "${MNT}/proc"
mount -t sysfs sys "${MNT}/sys"

if [[ "$(uname -m)" != "aarch64" ]]; then
    install -m 0755 "$(command -v qemu-aarch64-static)" "${MNT}/usr/bin/qemu-aarch64-static"
fi
cp /etc/resolv.conf "${MNT}/etc/resolv.conf"

echo "==> Staging Tamagci sources and QuIXI"
mkdir -p "${MNT}/opt/tamagci" "${MNT}/opt/quixi"
tar -C "${REPO_DIR}" \
    --exclude=.git --exclude=.github --exclude=esp32_face_os --exclude=server_agent \
    --exclude='*.img' --exclude='*.img.xz' --exclude='*.iso' --exclude='*.sha256' \
    --exclude='quixi-linux-arm64' --exclude='QuIXI' --exclude='scratch' --exclude='build' \
    --exclude='__pycache__' --exclude='.venv' --exclude='.mypy_cache' --exclude='.pytest_cache' \
    -cf - . | tar -C "${MNT}/opt/tamagci" -xf -
mkdir -p "${MNT}/opt/tamagci/firmware" "${MNT}/opt/tamagci/esp32_face_os"
if [[ -d "${REPO_DIR}/firmware" ]]; then
    cp -a "${REPO_DIR}/firmware/." "${MNT}/opt/tamagci/firmware/"
fi
if [[ -f "${REPO_DIR}/esp32_face_os/flash_pi.sh" ]]; then
    install -m 0755 "${REPO_DIR}/esp32_face_os/flash_pi.sh" "${MNT}/opt/tamagci/esp32_face_os/flash_pi.sh"
fi
cp -a "${QUIXI_SRC}/." "${MNT}/opt/quixi/"
chmod 0755 "${MNT}/opt/quixi/QuIXI"

echo "==> Running setup_pi.sh inside the arm64 chroot"
chroot "${MNT}" /usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    DEBIAN_FRONTEND=noninteractive TAMAGCI_IMAGE_BUILD=1 TAMAGCI_USER="${SVC_USER}" \
    bash /opt/tamagci/deploy/setup_pi.sh

echo "==> Image identity and first-boot notes"
echo "tamagci" > "${MNT}/etc/hostname"
sed -i 's/127\.0\.1\.1.*/127.0.1.1\ttamagci/' "${MNT}/etc/hosts" || true
cat > "${MNT}/etc/motd" <<'EOF'

  Tamagci Pi hub image (Tier 2). No API keys live on this device by design.

  Finish provisioning:
    1. sudoedit /opt/quixi/ixian.cfg           # set walletPassword
    2. sudoedit /opt/tamagci/pi_hub/config.yaml # set server_ixian_address
    3. sudo systemctl enable --now quixi-pi tamagci-pi-hub
    4. Pair wallets via QuIXI /addContact and /acceptContact (see /opt/tamagci/deploy/DEPLOYMENT.md)

EOF

echo "==> Cleaning up"
chroot "${MNT}" apt-get clean
rm -rf "${MNT}/var/lib/apt/lists/"* "${MNT}/root/.cache" "${MNT}/tmp/"* || true
rm -f "${MNT}/usr/bin/qemu-aarch64-static"
: > "${MNT}/etc/resolv.conf"
sync

echo "==> Unmounting"
cleanup
trap - EXIT
echo "Done: ${IMG}"
