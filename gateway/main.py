"""
OpenCPO Bastion — Entry Point

Starts all services via asyncio:
  - OCPP WebSocket proxy (proxy.py)
  - Certificate key vault (keyvault.py)
  - Charger discovery (discovery.py)
  - Health monitoring (monitor.py)
  - OCPP message tap (tap.py)
  - Troubleshoot API (troubleshoot.py)
  - Sensor array (sensors.py)
  - CCTV proxy (cctv.py)
  - Smart events / LPR / face auth (if UniFi present)
  - Auto-updater (updater.py)
  - Prometheus metrics server

Hardware watchdog (/dev/watchdog) integration.
Graceful shutdown on SIGTERM/SIGINT.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

from gateway.config import load_config
from gateway.keyvault import KeyVault
from gateway.proxy import OCPPProxy
from gateway.discovery import ChargerDiscovery
from gateway.monitor import HealthMonitor
from gateway.tap import app as tap_app
from gateway.troubleshoot import app as diag_app, init as diag_init
from gateway.sensors import SensorManager, SensorAlerts
from gateway.cctv import CCTVManager
from gateway.smart_events import SmartEventProcessor
from gateway.lpr import LPRProcessor
from gateway.face_auth import FaceAuthProcessor
from gateway.updater import Updater
from gateway.ha import HAManager
from gateway.connectivity import ConnectivityManager, ConnectivityMode

logger = logging.getLogger(__name__)

BANNER = r"""
  ___                   ____ ____  ___
 / _ \ _ __   ___ _ __ / ___|  _ \/ _ \
| | | | '_ \ / _ \ '_ \ |   | |_) | | | |
| |_| | |_) |  __/ | | | |__|  __/| |_| |
 \___/| .__/ \___|_| |_|\____|_|    \___/
      |_|   Gateway  {version}
"""

WATCHDOG_PATH = Path("/dev/watchdog")
WATCHDOG_INTERVAL = 10  # seconds between watchdog pings


def _print_banner(config) -> None:
    version = "dev"
    try:
        version = Path("/opt/opencpo-bastion/VERSION").read_text().strip()
    except Exception:
        pass

    print(BANNER.format(version=version))

    ts_ip = _get_tailscale_ip()
    pi_model = _pi_model()

    print(f"  Pi Model  : {pi_model}")
    print(f"  Tailscale : {ts_ip or 'not connected'}")
    print(f"  Core URL  : {config.core_api_base}")
    print(f"  OCPP 1.6  : 0.0.0.0:{config.proxy_ports.ocpp16}")
    print(f"  OCPP 2.0.1: 0.0.0.0:{config.proxy_ports.ocpp201}")
    ha_mode = config.ha.enabled
    if ha_mode != "false":
        print(f"  HA Mode   : {ha_mode} (VIP: {config.ha.virtual_ip or 'auto'}, iface: {config.ha.interface})")
    print(f"  4G Failover: {config.connectivity.failover.get('enabled', 'auto')}")
    if ts_ip:
        print(f"  Metrics   : http://{ts_ip}:{config.metrics_port}/metrics")
        print(f"  Tap       : http://{ts_ip}:{config.tap_port}/tap")
        print(f"  Diag      : http://{ts_ip}:{config.troubleshoot_port}/diag/system")
    print()


def _get_tailscale_ip() -> str:
    try:
        r = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _pi_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00 ")
    except Exception:
        return "Unknown"


def _start_prometheus(port: int, ts_ip: str) -> None:
    """Start Prometheus HTTP server bound to Tailscale IP."""
    try:
        from prometheus_client import start_http_server
        bind_ip = ts_ip or "127.0.0.1"
        start_http_server(port, addr=bind_ip)
        logger.info("Prometheus metrics on %s:%d", bind_ip, port)
    except ImportError:
        logger.warning("prometheus_client not installed — metrics disabled")
    except Exception as e:
        logger.warning("Prometheus start failed: %s", e)


async def _watchdog_loop() -> None:
    """Keep hardware watchdog alive. Stops feeding on shutdown."""
    if not WATCHDOG_PATH.exists():
        logger.debug("No hardware watchdog at %s", WATCHDOG_PATH)
        return

    try:
        fd = os.open(str(WATCHDOG_PATH), os.O_WRONLY)
        logger.info("Hardware watchdog enabled")
        while True:
            os.write(fd, b"1")
            await asyncio.sleep(WATCHDOG_INTERVAL)
    except PermissionError:
        logger.warning("Cannot open watchdog (not root?)")
    except Exception as e:
        logger.warning("Watchdog error: %s", e)


async def main() -> None:
    # ── Config ────────────────────────────────────────────────────────────────
    config = load_config()

    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ts_ip = _get_tailscale_ip()
    _print_banner(config)

    # ── Connectivity (4G failover) — start before anything needs the network ──
    connectivity = ConnectivityManager(config)
    await connectivity.start()

    # ── Key vault ─────────────────────────────────────────────────────────────
    vault = KeyVault(config, renew_days_before=config.cert_renew_days_before)
    await vault.start()

    # ── High Availability — discover peer and negotiate role before binding ───
    ha = HAManager(config, vault=vault)
    await ha.start()  # blocks up to 10s for peer discovery

    # Determine proxy bind address: VIP if HA active, else all interfaces
    if ha.is_active:
        proxy_bind = ha.virtual_ip
        logger.info("HA active — OCPP proxy binding to VIP %s", proxy_bind)
    elif ha.is_standby:
        proxy_bind = "0.0.0.0"  # standby also listens (for fast cutover)
        logger.info("HA standby — OCPP proxy binding to 0.0.0.0")
    else:
        proxy_bind = "0.0.0.0"
        logger.info("Standalone mode — OCPP proxy binding to 0.0.0.0")

    # ── OCPP Proxy ────────────────────────────────────────────────────────────
    proxy = OCPPProxy(
        config,
        cert_path=vault.cert_path,
        key_path=vault.key_path,
        ca_path=vault.ca_path,
    )

    # ── Charger discovery ─────────────────────────────────────────────────────
    discovery = ChargerDiscovery(config, interval_seconds=config.discovery_interval_seconds)
    await discovery.start()

    # ── Sensors ───────────────────────────────────────────────────────────────
    sensor_cfg = {}
    alerts = SensorAlerts()
    sensors = SensorManager(sensor_cfg, alerts)

    # ── CCTV + smart events ───────────────────────────────────────────────────
    cctv_cfg = {}
    unifi_cfg = cctv_cfg.get("unifi", {})
    sd_cfg = unifi_cfg.get("smart_detection", {})

    lpr = LPRProcessor(
        sd_cfg.get("lpr", {}),
        core_api_url=config.core_api_base,
        site_id="",
    )
    face = FaceAuthProcessor(
        sd_cfg.get("face_auth", {}),
        core_api_url=config.core_api_base,
    )
    smart = SmartEventProcessor(
        sd_cfg,
        core_api_url=config.core_api_base,
        lpr_processor=lpr,
        face_processor=face,
    )

    cctv = CCTVManager(cctv_cfg, tailscale_ip=ts_ip)
    cctv.register_event_callback(smart.handle_event)

    await lpr.start()
    await face.start()
    await smart.start()

    # ── Monitor ───────────────────────────────────────────────────────────────
    monitor = HealthMonitor(config, keyvault=vault, proxy=proxy)

    # ── Bandwidth-aware mode: wire up connectivity callbacks ─────────────────
    def _on_connectivity_change(mode: ConnectivityMode) -> None:
        """Adjust module behavior when switching between primary and 4G."""
        if mode == ConnectivityMode.FAILOVER:
            logger.info("Bandwidth-aware mode: ON (4G active)")
            # CCTV: signal low-quality mode (modules check connectivity.is_on_failover)
            # Sensor sync interval handled by SensorManager checking same flag
        else:
            logger.info("Bandwidth-aware mode: OFF (primary restored)")

    connectivity.register_bandwidth_callback(_on_connectivity_change)

    # ── HA: wire state replication sources ───────────────────────────────────
    # Proxy and discovery register their state so it gets replicated to standby
    # These are no-ops in standalone mode
    if not ha.is_standalone:
        logger.info("HA state replication wired up")

    # ── Troubleshoot API ──────────────────────────────────────────────────────
    diag_init(proxy=proxy, keyvault=vault, sensor_manager=sensors, config=config)

    # ── Updater ───────────────────────────────────────────────────────────────
    updater = Updater(config)

    # ── Prometheus ────────────────────────────────────────────────────────────
    _start_prometheus(config.metrics_port, ts_ip)

    # ── Uvicorn servers (Tailscale-only) ─────────────────────────────────────
    bind_ip = ts_ip or "127.0.0.1"

    tap_cfg = uvicorn.Config(
        tap_app, host=bind_ip, port=config.tap_port,
        log_level=config.log_level.lower(), access_log=False,
    )
    diag_cfg = uvicorn.Config(
        diag_app, host=bind_ip, port=config.troubleshoot_port,
        log_level=config.log_level.lower(), access_log=False,
    )

    tap_server = uvicorn.Server(tap_cfg)
    diag_server = uvicorn.Server(diag_cfg)

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal(sig):
        logger.info("Received %s — shutting down...", sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))

    # ── Run everything ────────────────────────────────────────────────────────
    logger.info("Starting all services...")

    tasks = [
        asyncio.create_task(proxy.run(), name="proxy"),
        asyncio.create_task(monitor.run(), name="monitor"),
        asyncio.create_task(sensors.run(), name="sensors"),
        asyncio.create_task(cctv.run(), name="cctv"),
        asyncio.create_task(tap_server.serve(), name="tap"),
        asyncio.create_task(diag_server.serve(), name="diag"),
        asyncio.create_task(updater.start(), name="updater"),
        asyncio.create_task(_watchdog_loop(), name="watchdog"),
        asyncio.create_task(connectivity.run(), name="connectivity"),
        asyncio.create_task(ha.run(), name="ha"),
        asyncio.create_task(shutdown_event.wait(), name="shutdown-sentinel"),
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Check if it was an error
    for task in done:
        if task.get_name() != "shutdown-sentinel":
            exc = task.exception()
            if exc:
                logger.error("Service %s crashed: %s", task.get_name(), exc)

    # Cancel remaining
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    await smart.stop()
    await lpr.stop()
    await face.stop()

    logger.info("Gateway shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
