#!/bin/bash
# OpenCPO Gateway — Pi Image Build Script
#
# Produces a flashable Raspberry Pi OS Lite 64-bit image with:
#   - Python 3.11+, all gateway dependencies
#   - Tailscale pre-installed
#   - ttyd (web terminal, Tailscale-only)
#   - tcpdump, node_exporter
#   - Read-only rootfs with overlayfs
#   - Systemd services for all gateway components
#   - Hardware watchdog enabled
#   - Bluetooth/WiFi power management disabled
#   - Swap disabled
#
# Requires: Docker, ~5GB free disk space
# Output: image/dist/opencpo-gateway-YYYYMMDD.img.gz

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$SCRIPT_DIR/dist"
DATE=$(date +%Y%m%d)
OUTPUT_NAME="opencpo-gateway-$DATE.img"

echo "╔══════════════════════════════════════════╗"
echo "║  OpenCPO Gateway — Image Build           ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Output: $OUTPUT_DIR/$OUTPUT_NAME.gz"
echo ""

mkdir -p "$OUTPUT_DIR"

# ── Stage: Build gateway package ─────────────────────────────────────────────
echo "[1/5] Packaging gateway application..."
GATEWAY_TAR="$OUTPUT_DIR/gateway-$DATE.tar.gz"
tar -czf "$GATEWAY_TAR" \
    -C "$REPO_ROOT" \
    gateway/ \
    requirements.txt \
    config/

echo "      Package: $GATEWAY_TAR"

# ── Stage: Build Docker image for pi-gen ─────────────────────────────────────
echo "[2/5] Building pi-gen Docker environment..."

cat > "$SCRIPT_DIR/Dockerfile.pigen" << 'DOCKERFILE'
FROM debian:bookworm

RUN apt-get update && apt-get install -y \
    quilt parted qemu-user-static debootstrap zerofree zip \
    dosfstools libcap2-bin grep rsync xz-utils file git curl \
    bc gpg pigz xxd arch-test binfmt-support \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /pi-gen
RUN git clone --depth 1 https://github.com/RPi-Distro/pi-gen.git .
DOCKERFILE

docker build -f "$SCRIPT_DIR/Dockerfile.pigen" -t opencpo-pigen "$SCRIPT_DIR"

# ── Stage: Configure pi-gen stages ───────────────────────────────────────────
echo "[3/5] Configuring pi-gen stages..."

# Write pi-gen config
cat > "$SCRIPT_DIR/config.pigen" << EOF
IMG_NAME="opencpo-gateway"
RELEASE="bookworm"
DEPLOY_COMPRESSION="gz"
ENABLE_SSH=0
DISABLE_FIRST_BOOT_USER_RENAME=1
PI_GEN_RELEASE="bookworm"

# Skip desktop stages
SKIP_IMAGES=""
EOF

# Custom stage: install dependencies
mkdir -p "$SCRIPT_DIR/stage-opencpo/00-opencpo"

cat > "$SCRIPT_DIR/stage-opencpo/00-opencpo/00-run.sh" << 'STAGE'
#!/bin/bash -e

# ── System packages ───────────────────────────────────────────────────────────
on_chroot << EOF
apt-get update
apt-get install -y \
    python3 python3-pip python3-venv \
    tcpdump \
    ffmpeg \
    i2c-tools \
    libi2c-dev \
    iptables \
    iptables-persistent \
    procps \
    curl \
    git \
    jq
apt-get clean
EOF

# ── Tailscale ─────────────────────────────────────────────────────────────────
on_chroot << EOF
curl -fsSL https://tailscale.com/install.sh | sh
EOF

# ── node_exporter ─────────────────────────────────────────────────────────────
on_chroot << EOF
NODE_EXPORTER_VERSION="1.8.1"
ARCH=\$(dpkg --print-architecture)
[ "\$ARCH" = "arm64" ] && NE_ARCH="arm64" || NE_ARCH="armv7"
curl -fsSL "https://github.com/prometheus/node_exporter/releases/download/v\${NODE_EXPORTER_VERSION}/node_exporter-\${NODE_EXPORTER_VERSION}.linux-\${NE_ARCH}.tar.gz" \
    | tar -xzf - --strip-components=1 -C /usr/local/bin node_exporter-\${NODE_EXPORTER_VERSION}.linux-\${NE_ARCH}/node_exporter
chmod +x /usr/local/bin/node_exporter
EOF

# ── ttyd (web terminal, Tailscale-only) ───────────────────────────────────────
on_chroot << EOF
TTYD_VERSION="1.7.4"
ARCH=\$(dpkg --print-architecture)
curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/\${TTYD_VERSION}/ttyd.\${ARCH}" \
    -o /usr/local/bin/ttyd
chmod +x /usr/local/bin/ttyd
EOF

# ── Python virtual environment + gateway deps ─────────────────────────────────
on_chroot << EOF
python3 -m venv /opt/opencpo-venv
/opt/opencpo-venv/bin/pip install --upgrade pip wheel
/opt/opencpo-venv/bin/pip install -r /opt/opencpo-gateway/requirements.txt
EOF

# ── Hardware configuration ────────────────────────────────────────────────────
# Enable I2C
on_chroot << EOF
raspi-config nonint do_i2c 0
EOF

# Enable hardware watchdog
echo "dtparam=watchdog=on" >> "${ROOTFS_DIR}/boot/config.txt"

# Enable SPI (for TPM HAT if present)
echo "dtparam=spi=on" >> "${ROOTFS_DIR}/boot/config.txt"

# I2S microphone overlay (SPH0645)
echo "#dtoverlay=i2s-mmap" >> "${ROOTFS_DIR}/boot/config.txt"
echo "# Uncomment above and add: dtoverlay=googlevoicehat-soundcard for I2S mic" \
    >> "${ROOTFS_DIR}/boot/config.txt"

# Disable Bluetooth (saves resources)
echo "dtoverlay=disable-bt" >> "${ROOTFS_DIR}/boot/config.txt"

# Disable WiFi power management
mkdir -p "${ROOTFS_DIR}/etc/NetworkManager/conf.d"
cat > "${ROOTFS_DIR}/etc/NetworkManager/conf.d/wifi-powersave.conf" << WIFICFG
[connection]
wifi.powersave = 2
WIFICFG

# Disable swap
on_chroot << EOF
systemctl disable dphys-swapfile || true
dphys-swapfile swapoff || true
dphys-swapfile uninstall || true
EOF

# ── Read-only rootfs with overlayfs ──────────────────────────────────────────
# Uses overlayroot package for Pi OS
on_chroot << EOF
apt-get install -y overlayroot
EOF

cat > "${ROOTFS_DIR}/etc/overlayroot.conf" << 'OVERLAYCFG'
overlayroot="tmpfs:swap=1,recurse=0"
overlayroot_cfgdisk="disabled"
OVERLAYCFG

# ── Kernel hardening ──────────────────────────────────────────────────────────
install -m 644 /tmp/sysctl.conf "${ROOTFS_DIR}/etc/sysctl.d/99-opencpo.conf"

# ── Systemd services ──────────────────────────────────────────────────────────
for service in /tmp/systemd/*.service /tmp/systemd/*.timer; do
    [ -f "$service" ] && install -m 644 "$service" "${ROOTFS_DIR}/etc/systemd/system/"
done

on_chroot << EOF
systemctl enable \
    opencpo-proxy \
    opencpo-keyvault \
    opencpo-monitor \
    opencpo-tap \
    opencpo-troubleshoot \
    opencpo-sensors \
    opencpo-cctv \
    opencpo-updater.timer \
    opencpo-discovery.timer
EOF

# ── First boot service ────────────────────────────────────────────────────────
install -m 755 /tmp/first-boot.sh "${ROOTFS_DIR}/usr/local/bin/opencpo-first-boot.sh"

cat > "${ROOTFS_DIR}/etc/systemd/system/opencpo-first-boot.service" << 'FIRSTBOOT'
[Unit]
Description=OpenCPO Gateway First Boot Provisioning
ConditionPathExists=/boot/opencpo.yaml
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/opencpo-first-boot.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
FIRSTBOOT

on_chroot << EOF
systemctl enable opencpo-first-boot
EOF

STAGE

chmod +x "$SCRIPT_DIR/stage-opencpo/00-opencpo/00-run.sh"

# ── Stage: Run pi-gen ─────────────────────────────────────────────────────────
echo "[4/5] Running pi-gen (this takes 20-40 minutes)..."

docker run --rm --privileged \
    -v "$SCRIPT_DIR:/workspace" \
    -v "$OUTPUT_DIR:/deploy" \
    -v "$REPO_ROOT/gateway:/opt/opencpo-gateway/gateway" \
    -v "$REPO_ROOT/requirements.txt:/opt/opencpo-gateway/requirements.txt" \
    -v "$REPO_ROOT/config:/tmp/config" \
    -v "$REPO_ROOT/systemd:/tmp/systemd" \
    -v "$SCRIPT_DIR/first-boot.sh:/tmp/first-boot.sh" \
    -v "$REPO_ROOT/config/sysctl.conf:/tmp/sysctl.conf" \
    opencpo-pigen \
    bash -c "
        cd /pi-gen
        cp /workspace/config.pigen config
        cp -r /workspace/stage-opencpo stage5/
        bash build.sh
        cp deploy/*.img.gz /deploy/
    "

# ── Stage: Finalize ───────────────────────────────────────────────────────────
echo "[5/5] Finalizing..."

IMAGE=$(ls "$OUTPUT_DIR"/*.img.gz 2>/dev/null | head -1)
if [ -n "$IMAGE" ]; then
    FINAL="$OUTPUT_DIR/$OUTPUT_NAME.gz"
    mv "$IMAGE" "$FINAL"
    SIZE=$(du -sh "$FINAL" | cut -f1)
    echo ""
    echo "✓ Image built: $FINAL ($SIZE)"
    echo ""
    echo "Flash with:"
    echo "  Balena Etcher: open $FINAL"
    echo "  CLI: gunzip -c $FINAL | sudo dd of=/dev/sdX bs=4M status=progress"
    echo ""
    echo "Then drop your opencpo.yaml onto the boot partition and power on."
else
    echo "✗ Build failed — check pi-gen output above"
    exit 1
fi
