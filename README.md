# OpenCPO Bastion

A flashable Raspberry Pi image that turns a Pi into a secure, zero-trust EV charger gateway — bridging local OCPP chargers into the OpenCPO mesh network without touching the chargers themselves.

---

## The Problem

EV chargers speak OCPP (WebSocket). They can't run Tailscale, WireGuard, or mTLS themselves — they just connect to a URL and talk. But you don't want charger WebSockets exposed raw to the internet.

OpenCPO Bastion solves this by sitting between your chargers and the cloud:

- Chargers connect to the Pi on the local network (just a WebSocket URL)
- The Pi holds the mTLS certificates, joins the zero-trust mesh, and forwards everything securely
- Your chargers never need to know any of this exists

---

## Architecture

```
                     LOCAL NETWORK                          MESH / INTERNET
                                                     
  ┌─────────────┐   OCPP WS    ┌──────────────────┐    WireGuard     ┌─────────────────┐
  │  EV Charger │ ───────────► │                  │ ──────────────►  │                 │
  │  (any OCPP) │              │  Pi Gateway      │    (Tailscale)   │  OpenCPO Core   │
  └─────────────┘              │                  │                  │                 │
                               │  • OCPP Proxy    │    mTLS cert     │  • OCPP Backend │
  ┌─────────────┐   OCPP WS    │  • Key Vault     │ ◄──────────────  │  • PKI          │
  │  EV Charger │ ───────────► │  • Discovery     │                  │  • API          │
  │  (any OCPP) │              │  • Monitor       │                  │                 │
  └─────────────┘              │  • Tap / Diag    │                  └─────────────────┘
                               └──────────────────┘
                                      │
                                      ▼
                               Tailscale-only:
                               :9090  Prometheus
                               :8085  Message tap / SSE
                               :8086  Troubleshoot API
```

**Data flow:**
1. Charger connects to `ws://gateway-ip:9100` (OCPP 1.6) or `:9201` (OCPP 2.0.1)
2. Gateway looks up the upstream Core URL from config
3. Opens a mTLS-authenticated WebSocket to Core via Tailscale IP
4. Proxies all frames bidirectionally, logging to ring buffer
5. Tap + troubleshoot endpoints available to admins over Tailscale only

---

## First Boot Flow

```
1. Flash opencpo-bastion.img.gz to SD card (Balena Etcher or rpi-imager)
2. Mount the boot partition (FAT32, visible on any OS)
3. Copy your opencpo.yaml onto the boot partition
4. Eject and insert SD card into Pi, power on
5. First-boot.sh runs automatically:
   - Reads opencpo.yaml
   - Joins Tailscale mesh (auth key from config)
   - Generates device keypair
   - Requests signed cert from Core PKI
   - Starts all gateway services
   - Removes auth key from config (security)
6. Pi appears in your Tailscale admin panel as "opencpo-gw-<hostname>"
7. Chargers on the local network can now connect
```

---

## Hardware Requirements

| Component | Minimum | Recommended | HA Pair |
|-----------|---------|-------------|---------|
| Board | Raspberry Pi Zero 2W | Raspberry Pi 4 (2GB+) | NanoPi R5S/R6S, Zimaboard, CM4 + dual-ETH carrier |
| RAM | 512MB | 2GB+ | 2GB+ each |
| Storage | SD card 8GB | SD card 16GB+ (A1/A2 rated) | 16GB+ each |
| Network | Wi-Fi (Zero 2W) | Ethernet (Pi 4) | **2x Ethernet** (dual-NIC SBC) |
| Power | 5V 2.5A | 5V 3A (USB-C on Pi 4) | 5V 3A each |

**Notes:**
- Ethernet strongly recommended for charger connectivity (reliability + latency)
- Pi 4 preferred for sites with many chargers (>4 simultaneous connections)
- Pi Zero 2W works well for small sites (1-3 chargers)
- Optional: Waveshare TPM HAT for hardware-backed key storage
- Read-only rootfs: SD card wear is minimal, but quality card still recommended

### Dual Ethernet (HA / Production)

For HA pairs and production sites, use an SBC with two Ethernet ports:

| Board | ETH ports | Notes |
|-------|-----------|-------|
| **NanoPi R5S** | 1× 2.5GbE + 2× 1GbE | Best all-around. RK3566, 4GB RAM. ~$60 |
| **NanoPi R6S** | 1× 2.5GbE + 2× 1GbE | Faster RK3588S, 8GB RAM. ~$80 |
| **Zimaboard 832** | 2× Intel i226 1GbE | x86-64, PCIe slot for 4G mPCIe card. ~$100 |
| **CM4 + dual-ETH carrier** | 2× 1GbE | Waveshare or similar CM4 IO board |
| **Raspberry Pi 5** | 1× GbE + USB-ETH | Works; USB-ETH is fine for charger LAN |

**Interface assignment (dual-NIC):**
```
ETH0 → WAN / uplink  (Tailscale, Core API, internet — metric 100)
ETH1 → Charger LAN  (isolated, DHCP server, VRRP virtual IP)
```

Single-ETH boards work fine in standalone mode. Dual-ETH is required for HA pairs.

---

## Components

| Module | Description |
|--------|-------------|
| `gateway/proxy.py` | OCPP WebSocket proxy — listens locally, forwards to Core via mTLS |
| `gateway/keyvault.py` | Certificate vault — device cert lifecycle, TPM support, auto-renewal |
| `gateway/discovery.py` | Charger discovery — mDNS, ARP scan, reports to Core API |
| `gateway/monitor.py` | Health monitoring — Prometheus metrics, heartbeat to Core, alerts |
| `gateway/tap.py` | OCPP message tap — ring buffer, SSE stream, query/export |
| `gateway/troubleshoot.py` | Remote diagnostics — network, chargers, speedtest, packet capture |
| `gateway/ha.py` | **High Availability** — peer discovery, VRRP/keepalived, state replication, failover |
| `gateway/connectivity.py` | **Multi-WAN** — 4G failover via ModemManager, bandwidth-aware mode |
| `gateway/config.py` | Config management — loads opencpo.yaml, env overrides, validation |
| `gateway/updater.py` | Auto-update — checks Core for updates, verifies, rolls back if broken |
| `gateway/main.py` | Entry point — asyncio orchestration, watchdog, graceful shutdown |
| `image/build.sh` | Pi image build script — pi-gen based, produces flashable .img.gz |
| `image/first-boot.sh` | First boot provisioning — Tailscale join, cert request, service start |
| `systemd/` | Systemd service + timer units for all components |
| `config/` | Example config, firewall rules, kernel hardening |

---

## Quick Start (Development)

```bash
# Clone and set up
git clone https://github.com/opencpo/opencpo-bastion
cd opencpo-bastion

# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp config/opencpo.yaml.example opencpo.yaml
# Edit: set core_api_url, tailscale_auth_key

# Run locally (no Pi hardware needed)
make dev

# Lint and test
make lint
make test
```

---

## Building the Pi Image

```bash
# Requires Docker
make build
# Output: image/opencpo-bastion-<date>.img.gz

# Flash with Balena Etcher or:
gunzip -c image/opencpo-bastion-*.img.gz | sudo dd of=/dev/sdX bs=4M status=progress
```

---

## Security Model

- **No SSH on LAN** — management is Tailscale-only
- **mTLS everywhere** — all Core communication uses device-specific client certs
- **Certs never leave the device** — private keys encrypted at rest, TPM-backed when available
- **Read-only rootfs** — overlayfs protects against SD corruption and tampering
- **OCPP proxy only** on 0.0.0.0 — all other endpoints bind to Tailscale IP only
- **Firewall** — iptables blocks everything except OCPP from local subnet, Tailscale for management
- **Auth key rotation** — Tailscale auth key removed from config after successful join

---

## Deployment Modes

### Standalone (default)

One gateway unit. No HA config needed. ETH0 for charger LAN or uplink, works on any Pi.

```yaml
# opencpo.yaml — minimum config
tailscale_auth_key: tskey-...
core_api_url: https://core.example.com
# ha.enabled defaults to "auto" — no peer found → standalone mode
```

### HA Pair (zero-config auto-discovery)

Two identical units. Flash the same image to both. They find each other automatically.

```yaml
# opencpo.yaml — same on both units (truly identical)
tailscale_auth_key: tskey-...
core_api_url: https://core.example.com

ha:
  enabled: auto          # default — find peer, negotiate roles
  interface: eth1        # charger LAN interface
  virtual_ip: ""         # auto-derived from eth1 subnet
```

Boot both units. Within 10 seconds they discover each other via UDP broadcast, negotiate
active/standby roles (tunnel health → VRRP priority → hostname tiebreak), start keepalived,
and chargers connect to the shared VIP. Failover happens in <3 seconds.

### HA Pair (explicit roles)

Use when you want deterministic role assignment (e.g. unit A is always primary):

```yaml
# Unit A — opencpo-A.yaml
ha:
  enabled: true
  role: primary          # always wants to be active
  priority: 150
  interface: eth1
  virtual_ip: 192.168.10.100

# Unit B — opencpo-B.yaml
ha:
  enabled: true
  role: secondary        # always standby unless A is down
  priority: 100
  interface: eth1
  virtual_ip: 192.168.10.100
```

### API endpoints (HA)

- `GET /ha/status` — role, peer status, VIP owner, last sync, replication lag
- `POST /ha/failover` — graceful handoff (for planned maintenance / updates)

---

## 4G Failover

Plug in any supported USB 4G dongle or mPCIe modem. It's auto-detected on boot via ModemManager.
No config needed for most carriers (APN `internet` is the universal default).

```
Primary down?  →  3 ping failures  →  switch to 4G  →  alert Core  →  bandwidth-aware mode
Primary back?  →  3 ping successes →  switch back   →  alert Core  →  normal mode
```

**Bandwidth-aware mode** (automatic when on 4G):
- CCTV: snapshot-only or reduced quality (no continuous MJPEG)
- Sensor sync: interval increased from 30s → 120s
- OCPP tap: only critical events forwarded
- HA state replication: continues normally (small payloads — <1KB per sync)

### Supported modems

| Modem | Interface | Notes |
|-------|-----------|-------|
| Huawei E3372 (HiLink) | `usb0` / `eth2` | Appears as USB Ethernet — no mmcli needed |
| Huawei E3372 (Stick) | `wwan0` | usb_modeswitch handles HiLink→Stick |
| Sierra Wireless MC7455 | `wwan0` | mPCIe — ideal for Zimaboard |
| Quectel EC25 / EC21 | `wwan0` | USB — widely available |
| Any ModemManager device | `wwan0` | If `mmcli -L` shows it, it works |

### SIM setup

1. Insert SIM into modem before powering on
2. If SIM has a PIN: set `connectivity.failover.pin` in config
3. Set APN if carrier doesn't use `internet`: `connectivity.failover.apn: your.apn`
4. That's it — ModemManager handles the rest

### Config

```yaml
connectivity:
  primary:
    interface: eth0
    check_interval: 30       # ping Core every N seconds
    check_target: ""         # auto: Core API hostname. Or set explicit IP/host
    failure_threshold: 3     # failures before failover activates
  failover:
    enabled: auto            # "auto" (detect modem), "true", "false"
    interface: wwan0         # or "usb0" for HiLink-mode Huawei
    apn: internet
    pin: ""                  # SIM PIN (leave blank if none)
    bandwidth_mode: true     # reduce non-essential traffic on 4G
    max_monthly_gb: 5        # alert at 90% of this limit
  wifi:
    enabled: false           # tertiary option — WiFi as backup
    ssid: ""
    password: ""
```

### API

- `GET /diag/connectivity` — mode, primary status, modem info, signal, carrier, data usage, last failover time

---

## Configuration Reference

See `config/opencpo.yaml.example` for full documentation.

**Required:**
- `tailscale_auth_key` — one-time Tailscale auth key (removed after first boot)
- `core_api_url` — your OpenCPO Core URL (e.g. `https://core.example.com`)

**Optional:**
- `proxy_ports.ocpp16` — OCPP 1.6 listen port (default: 9100)
- `proxy_ports.ocpp201` — OCPP 2.0.1 listen port (default: 9201)
- `log_level` — debug/info/warning/error (default: info)
- `metrics_port` — Prometheus port (default: 9090)
- `auto_update` — enable/disable auto-update (default: true)
- `update_time` — cron-style update schedule (default: "03:00")
- `ha.*` — see [HA Pair](#ha-pair-zero-config-auto-discovery) section above
- `connectivity.*` — see [4G Failover](#4g-failover) section above

---

## Site Intelligence

One Pi does more than networking. The gateway turns each charging site into a fully-instrumented, monitored location — all data flowing through the same zero-trust tunnel.

```
                     ONE PI — ONE TUNNEL

  ┌─────────────────────────────────────────────────────────┐
  │                  OpenCPO Bastion                        │
  │                                                         │
  │  🔌 OCPP Proxy      — chargers ↔ core                  │
  │  🌡  BME280          — enclosure temp, humidity, pressure│
  │  ⚡ CT Clamp         — real power draw (independent)     │
  │  🚪 Reed Switch      — enclosure tamper detection        │
  │  💧 Flood Sensor     — water ingress                     │
  │  🔊 MEMS Mic         — ambient dB (fan failure, arcing)  │
  │  💡 TSL2591          — ambient light / site lighting     │
  │  📷 UniFi Cameras    — video, motion, LPR, face events  │
  │                                                         │
  │  All management over Tailscale — nothing on WAN          │
  └─────────────────────────────────────────────────────────┘
```

### Sensor Array

Plug-and-play I2C/GPIO sensors auto-detected on boot. No configuration needed if using default I2C addresses and GPIO pins.

| Sensor | Interface | What it tells you |
|--------|-----------|-------------------|
| BME280 | I2C 0x76/0x77 | Enclosure temperature, humidity, pressure |
| ADS1115 + SCT-013 | I2C 0x48 | True power draw at supply — independent check on charger reports |
| Reed switch | GPIO 17 | Enclosure door open / tamper detected |
| Flood sensor | GPIO 27 | Water ingress — critical for outdoor/underground installs |
| SPH0645 (I2S mic) | I2S | Ambient dB level only — detects fan failure, arcing, abnormal noise. **No audio recording.** |
| TSL2591 | I2C 0x29 | Ambient light — site lighting status, day/night detection |

All sensors publish to Prometheus and a 24-hour ring buffer (1-min resolution).

**API:**
- `GET /sensors` — current readings for all detected sensors
- `GET /sensors/{id}/history` — 24h time series for a single sensor
- `GET /diag/sensors` — raw hardware scan: I2C addresses found, which sensors detected

### CCTV Integration

Opt-in (`cctv.enabled: true`). Cameras stay on the local LAN — only the admin-panel-bound stream proxies through the tunnel.

**UniFi Protect** is the primary target. The `uiprotect` library (same as Home Assistant) handles auth, WebSocket, and reconnection.

**Smart detection events** from Protect AI are the real value:

| Feature | What it does |
|---------|-------------|
| **License Plate Recognition** | Reads plates from Protect → sends to Core → Core matches plate to fleet vehicle → auto-authorize charging session |
| **Motion / Person / Vehicle** | Real-time events forwarded to Core API + SSE stream |
| **Face Recognition** | Known faces logged as authorized access; unknown faces alert admin during configured hours |
| **ONVIF fallback** | Any standards-compliant camera works if UniFi isn't present |

**API:**
- `GET /cctv` — list discovered cameras with status
- `GET /cctv/{id}/snapshot` — JPEG frame on demand
- `GET /cctv/{id}/stream` — MJPEG stream for dashboard embedding
- `POST /cctv/{id}/ptz` — pan/tilt/zoom (if camera supports it)
- `GET /events` — SSE stream of smart detection events
- `GET /lpr/recent` — recent plate reads with thumbnails
- `GET /lpr/search?plate=XX-1` — search plate history
- `GET /faces/recent` — recent face events with known/unknown status

Local recording uses a circular buffer on the SD card or USB drive (configurable retention and size cap).

---

## License

Apache 2.0 — see LICENSE
