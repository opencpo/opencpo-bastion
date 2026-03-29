#!/bin/bash
# OpenCPO Bastion — First Boot Provisioning
#
# Runs once on first power-on after flashing.
# Triggered by opencpo-first-boot.service when /boot/opencpo.yaml exists.
#
# Steps:
#   1. Read /boot/opencpo.yaml
#   2. Start Tailscale with auth key
#   3. Generate device keypair + request cert from Core PKI
#   4. Apply firewall rules
#   5. Enable and start all services
#   6. Remove auth key from config (security)
#   7. Mark first boot complete

set -euo pipefail

CONFIG_FILE=""
for path in /boot/opencpo.yaml /boot/firmware/opencpo.yaml; do
    [ -f "$path" ] && CONFIG_FILE="$path" && break
done

if [ -z "$CONFIG_FILE" ]; then
    echo "ERROR: No opencpo.yaml found on boot partition"
    echo "Mount the SD card and copy config/opencpo.yaml.example → /boot/opencpo.yaml"
    exit 1
fi

echo "╔══════════════════════════════════════════╗"
echo "║  OpenCPO Bastion — First Boot            ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Config: $CONFIG_FILE"

# ── Parse config ──────────────────────────────────────────────────────────────
_yaml_val() {
    grep "^${1}:" "$CONFIG_FILE" | head -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '"' | tr -d "'"
}

TAILSCALE_AUTH_KEY=$(_yaml_val tailscale_auth_key)
CORE_API_URL=$(_yaml_val core_api_url)
TAILSCALE_HOSTNAME=$(_yaml_val tailscale_hostname)

if [ -z "$TAILSCALE_AUTH_KEY" ] || [ "$TAILSCALE_AUTH_KEY" = "tskey-auth-XXXXXXXXXX" ]; then
    echo "ERROR: tailscale_auth_key is not set in $CONFIG_FILE"
    exit 1
fi

if [ -z "$CORE_API_URL" ]; then
    echo "ERROR: core_api_url is not set in $CONFIG_FILE"
    exit 1
fi

# Auto-generate hostname if not set
if [ -z "$TAILSCALE_HOSTNAME" ]; then
    MACHINE_HOSTNAME=$(hostname -s)
    TAILSCALE_HOSTNAME="opencpo-gw-${MACHINE_HOSTNAME}"
fi

echo "Tailscale hostname: $TAILSCALE_HOSTNAME"
echo "Core URL: $CORE_API_URL"
echo ""

# ── Step 1: Configure hostname ────────────────────────────────────────────────
echo "[1/7] Setting hostname..."
hostnamectl set-hostname "$TAILSCALE_HOSTNAME"
echo "      Hostname: $TAILSCALE_HOSTNAME"

# ── Step 2: Join Tailscale ────────────────────────────────────────────────────
echo "[2/7] Joining Tailscale mesh..."

systemctl enable --now tailscaled

tailscale up \
    --authkey="$TAILSCALE_AUTH_KEY" \
    --hostname="$TAILSCALE_HOSTNAME" \
    --accept-dns=false \
    --accept-routes=false \
    --ssh

# Wait for Tailscale IP
for i in $(seq 1 30); do
    TS_IP=$(tailscale ip -4 2>/dev/null || true)
    [ -n "$TS_IP" ] && break
    sleep 2
done

if [ -z "${TS_IP:-}" ]; then
    echo "ERROR: Tailscale did not get an IP after 60s"
    exit 1
fi
echo "      Tailscale IP: $TS_IP"

# ── Step 3: Cert provisioning ─────────────────────────────────────────────────
echo "[3/7] Provisioning device certificate from Core PKI..."

mkdir -p /etc/opencpo/certs
chmod 700 /etc/opencpo/certs

# Python handles the full PKI flow (CSR generation, submission, storage)
/opt/opencpo-venv/bin/python3 -c "
import asyncio, sys
sys.path.insert(0, '/opt/opencpo-bastion')

async def provision():
    from gateway.config import load_config
    from gateway.keyvault import KeyVault
    config = load_config()
    vault = KeyVault(config)
    await vault.start()
    print('Certificate provisioned:', vault.cert_info())

asyncio.run(provision())
"

echo "      Certificate provisioned ✓"

# ── Step 4: Apply firewall ────────────────────────────────────────────────────
echo "[4/7] Configuring firewall..."
bash /etc/opencpo/firewall.sh
echo "      Firewall configured ✓"

# ── Step 5: Apply kernel settings ─────────────────────────────────────────────
echo "[5/7] Applying kernel hardening..."
sysctl -p /etc/sysctl.d/99-opencpo.conf > /dev/null
echo "      Sysctl applied ✓"

# ── Step 6: Start services ────────────────────────────────────────────────────
echo "[6/7] Starting OpenCPO services..."

systemctl daemon-reload

for svc in opencpo-proxy opencpo-keyvault opencpo-monitor opencpo-tap opencpo-troubleshoot opencpo-sensors; do
    systemctl enable --now "$svc" 2>/dev/null || echo "  (skipped: $svc)"
done

systemctl enable --now opencpo-updater.timer 2>/dev/null || true
systemctl enable --now opencpo-discovery.timer 2>/dev/null || true

echo "      Services started ✓"

# ── Step 7: Remove auth key from config (security) ───────────────────────────
echo "[7/7] Removing auth key from config..."
# Replace the auth key line with a placeholder
sed -i "s|^tailscale_auth_key:.*|tailscale_auth_key: \"\"  # removed after provisioning|" "$CONFIG_FILE"
echo "      Auth key removed ✓"

# ── Mark complete ─────────────────────────────────────────────────────────────
touch /etc/opencpo/.first-boot-complete
echo ""
echo "════════════════════════════════════════════"
echo "  OpenCPO Bastion provisioned successfully!"
echo "  Tailscale IP: $TS_IP"
echo "  Metrics:      http://$TS_IP:9090/metrics"
echo "  Tap:          http://$TS_IP:8085/tap"
echo "  Diagnostics:  http://$TS_IP:8086/diag/system"
echo "════════════════════════════════════════════"
echo ""
echo "Point your OCPP chargers to:"
echo "  ws://$(hostname -I | awk '{print $1}'):9100/<charger-id>   (OCPP 1.6)"
echo "  ws://$(hostname -I | awk '{print $1}'):9201/<charger-id>   (OCPP 2.0.1)"
echo ""
