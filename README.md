# OpenCPO Gateway

A flashable Raspberry Pi image that turns a Pi into a secure, zero-trust EV charger gateway — bridging local OCPP chargers into the OpenCPO mesh network without touching the chargers themselves.

---

## The Problem

EV chargers speak OCPP (WebSocket). They can't run Tailscale, WireGuard, or mTLS themselves — they just connect to a URL and talk. But you don't want charger WebSockets exposed raw to the internet.

OpenCPO Gateway solves this by sitting between your chargers and the cloud:

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
1. Flash opencpo-gateway.img.gz to SD card (Balena Etcher or rpi-imager)
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

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| Board | Raspberry Pi Zero 2W | Raspberry Pi 4 (2GB+) |
| RAM | 512MB | 2GB+ |
| Storage | SD card 8GB | SD card 16GB+ (A1/A2 rated) |
| Network | Wi-Fi (Zero 2W) | Ethernet (Pi 4) |
| Power | 5V 2.5A | 5V 3A (USB-C on Pi 4) |

**Notes:**
- Ethernet strongly recommended for charger connectivity (reliability + latency)
- Pi 4 preferred for sites with many chargers (>4 simultaneous connections)
- Pi Zero 2W works well for small sites (1-3 chargers)
- Optional: Waveshare TPM HAT for hardware-backed key storage
- Read-only rootfs: SD card wear is minimal, but quality card still recommended

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
git clone https://github.com/opencpo/opencpo-gateway
cd opencpo-gateway

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
# Output: image/opencpo-gateway-<date>.img.gz

# Flash with Balena Etcher or:
gunzip -c image/opencpo-gateway-*.img.gz | sudo dd of=/dev/sdX bs=4M status=progress
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

---

## Site Intelligence

One Pi does more than networking. The gateway turns each charging site into a fully-instrumented, monitored location — all data flowing through the same zero-trust tunnel.

```
                     ONE PI — ONE TUNNEL

  ┌─────────────────────────────────────────────────────────┐
  │                  OpenCPO Gateway                        │
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
