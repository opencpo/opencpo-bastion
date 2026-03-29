#!/bin/bash
# OpenCPO Bastion — iptables Firewall Rules
#
# Policy:
#   - OCPP proxy ports (9100, 9201) accept from local subnet only
#   - All management (metrics, tap, diag) on Tailscale interface only
#   - SSH disabled on LAN — Tailscale SSH only
#   - Everything else dropped
#
# Run as root. Called from first-boot.sh and opencpo-firewall.service.

set -euo pipefail

# Detect interfaces
LAN_IF="${LAN_IF:-eth0}"
TS_IF="${TS_IF:-tailscale0}"

# Detect local subnet (assumes /24)
LAN_IP=$(ip -4 addr show "$LAN_IF" 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
LAN_SUBNET="${LAN_IP%.*}.0/24"

echo "Configuring firewall:"
echo "  LAN interface : $LAN_IF ($LAN_SUBNET)"
echo "  Tailscale     : $TS_IF"

# ── Flush existing rules ──────────────────────────────────────────────────────
iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X

# ── Default policies ──────────────────────────────────────────────────────────
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

# ── Loopback ──────────────────────────────────────────────────────────────────
iptables -A INPUT -i lo -j ACCEPT

# ── Established/related connections ───────────────────────────────────────────
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# ── Tailscale — allow all management traffic through Tailscale interface ──────
iptables -A INPUT -i "$TS_IF" -j ACCEPT

# ── OCPP proxy — LAN subnet only ─────────────────────────────────────────────
# OCPP 1.6 WebSocket
iptables -A INPUT -i "$LAN_IF" -s "$LAN_SUBNET" -p tcp --dport 9100 -j ACCEPT
# OCPP 2.0.1 WebSocket
iptables -A INPUT -i "$LAN_IF" -s "$LAN_SUBNET" -p tcp --dport 9201 -j ACCEPT

# ── ICMP — allow ping from LAN ────────────────────────────────────────────────
iptables -A INPUT -i "$LAN_IF" -s "$LAN_SUBNET" -p icmp --icmp-type echo-request -j ACCEPT

# ── Block SSH from LAN (Tailscale SSH only) ───────────────────────────────────
iptables -A INPUT -i "$LAN_IF" -p tcp --dport 22 -j DROP

# ── DHCP client ───────────────────────────────────────────────────────────────
iptables -A INPUT -i "$LAN_IF" -p udp --sport 67 --dport 68 -j ACCEPT

# ── Log and drop everything else ─────────────────────────────────────────────
iptables -A INPUT -m limit --limit 5/min -j LOG --log-prefix "opencpo-drop: " --log-level 7
iptables -A INPUT -j DROP

# ── Save rules ────────────────────────────────────────────────────────────────
if command -v iptables-save &>/dev/null; then
    mkdir -p /etc/iptables
    iptables-save > /etc/iptables/rules.v4
    echo "Rules saved to /etc/iptables/rules.v4"
fi

echo "Firewall configured ✓"
