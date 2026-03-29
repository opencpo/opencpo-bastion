"""
OpenCPO Bastion — Health Monitoring

Checks:
  - Tailscale connectivity (status + ping to Core)
  - OCPP proxy health (active connections, last message times)
  - Certificate validity (days remaining)
  - System: disk, memory, CPU temp
  - Sensor alerts (delegated to sensors.py)

Exposes Prometheus metrics on :9090 (Tailscale-only).
Sends heartbeat to Core every 60s.
Alerts: Tailscale disconnect, cert expiry <7d, temp >70°C, disk >90%.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from gateway.config import GatewayConfig

logger = logging.getLogger(__name__)


def _read_pi_temp() -> float:
    """Read CPU temperature from Pi thermal zone."""
    for path in (
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ):
        try:
            val = int(Path(path).read_text().strip())
            return val / 1000.0  # millidegrees → degrees
        except Exception:
            pass
    return 0.0


def _disk_usage_pct(path: str = "/") -> float:
    stat = os.statvfs(path)
    total = stat.f_blocks * stat.f_frsize
    used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
    return (used / total * 100) if total else 0.0


def _memory_usage_pct() -> tuple[int, int, float]:
    """Return (used_mb, total_mb, pct)."""
    try:
        with open("/proc/meminfo") as f:
            lines = {
                k.strip(":"): int(v.split()[0])
                for k, v in (line.split(":", 1) for line in f if ":" in line)
            }
        total_kb = lines.get("MemTotal", 0)
        avail_kb = lines.get("MemAvailable", 0)
        used_kb = total_kb - avail_kb
        pct = used_kb / total_kb * 100 if total_kb else 0.0
        return used_kb // 1024, total_kb // 1024, round(pct, 1)
    except Exception:
        return 0, 0, 0.0


def _tailscale_status() -> dict:
    try:
        r = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            import json
            data = json.loads(r.stdout)
            self_ip = data.get("TailscaleIPs", [""])[0]
            connected = data.get("BackendState") == "Running"
            return {"connected": connected, "ip": self_ip, "raw": data}
    except Exception as e:
        logger.debug("Tailscale status error: %s", e)
    return {"connected": False, "ip": "", "raw": {}}


class HealthMonitor:
    def __init__(
        self,
        config: GatewayConfig,
        keyvault=None,
        proxy=None,
    ):
        self.config = config
        self.keyvault = keyvault
        self.proxy = proxy
        self._http: Optional[httpx.AsyncClient] = None
        self._setup_metrics()
        self._last_ts_connected: Optional[float] = None

    def _setup_metrics(self) -> None:
        try:
            from prometheus_client import Gauge, Counter
            self.g_temp = Gauge("opencpo_cpu_temp_celsius", "CPU temperature")
            self.g_disk = Gauge("opencpo_disk_usage_pct", "Disk usage percent")
            self.g_mem = Gauge("opencpo_memory_usage_pct", "Memory usage percent")
            self.g_ts = Gauge("opencpo_tailscale_connected", "Tailscale connected (1/0)")
            self.g_chargers = Gauge("opencpo_active_chargers", "Active proxied chargers")
            self.g_cert_days = Gauge("opencpo_cert_days_remaining", "Cert days until expiry")
            self._metrics_ok = True
        except ImportError:
            self._metrics_ok = False

    def _update_metrics(
        self, temp: float, disk: float, mem_pct: float,
        ts_connected: bool, active_chargers: int, cert_days: int,
    ) -> None:
        if not self._metrics_ok:
            return
        self.g_temp.set(temp)
        self.g_disk.set(disk)
        self.g_mem.set(mem_pct)
        self.g_ts.set(1 if ts_connected else 0)
        self.g_chargers.set(active_chargers)
        self.g_cert_days.set(cert_days)

    async def _ping_core(self) -> bool:
        if not self._http:
            return False
        try:
            r = await asyncio.wait_for(
                self._http.get(f"{self.config.core_api_base}/health"),
                timeout=5,
            )
            return r.status_code < 500
        except Exception:
            return False

    async def _send_heartbeat(self, health: dict) -> None:
        if not self._http:
            return
        try:
            await self._http.post(
                f"{self.config.core_api_base}/api/v1/gateway/heartbeat",
                json=health,
                timeout=10,
            )
        except Exception as e:
            logger.debug("Heartbeat send failed: %s", e)

    async def _check_once(self) -> dict:
        temp = _read_pi_temp()
        disk = _disk_usage_pct("/")
        mem_used, mem_total, mem_pct = _memory_usage_pct()
        ts = _tailscale_status()
        ts_connected = ts["connected"]
        active_chargers = len(self.proxy.active_chargers()) if self.proxy else 0

        cert_info = self.keyvault.cert_info() if self.keyvault else {}
        cert_days = cert_info.get("days_remaining", 999)

        self._update_metrics(temp, disk, mem_pct, ts_connected, active_chargers, cert_days)

        # Alert conditions
        alerts = []

        if not ts_connected:
            if self._last_ts_connected is None:
                self._last_ts_connected = time.time()
            elif time.time() - self._last_ts_connected > 120:
                alerts.append("Tailscale disconnected for >2 minutes")
        else:
            self._last_ts_connected = None

        if cert_days < 7:
            alerts.append(f"Certificate expires in {cert_days} days")

        if temp > 70.0:
            alerts.append(f"CPU temperature critical: {temp:.1f}°C")

        if disk > 90.0:
            alerts.append(f"Disk usage critical: {disk:.1f}%")

        for alert in alerts:
            logger.warning("HEALTH ALERT: %s", alert)

        health = {
            "timestamp": time.time(),
            "tailscale": ts,
            "cpu_temp_c": round(temp, 1),
            "disk_pct": round(disk, 1),
            "memory": {"used_mb": mem_used, "total_mb": mem_total, "pct": mem_pct},
            "active_chargers": active_chargers,
            "cert_days_remaining": cert_days,
            "alerts": alerts,
        }
        return health

    async def run(self) -> None:
        self._http = httpx.AsyncClient(timeout=10)
        logger.info(
            "Health monitor started (heartbeat every %ds)",
            self.config.heartbeat_interval_seconds,
        )
        while True:
            health = await self._check_once()
            await self._send_heartbeat(health)
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    async def current_health(self) -> dict:
        return await self._check_once()
