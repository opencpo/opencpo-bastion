"""
OpenCPO Bastion — Multi-WAN Connectivity & 4G Failover

Ensures the gateway NEVER goes offline if any connectivity option is available.

Network priority:
  ETH0  (metric 100) — primary: LAN / fiber / uplink
  wwan0 (metric 500) — failover: 4G USB dongle or mPCIe modem (via ModemManager)
  wlan0 (metric 800) — tertiary: WiFi backup (opt-in)

How it works:
  1. Ping Core API on primary interface every check_interval seconds
  2. On N consecutive failures → activate failover via route metric manipulation
  3. When on 4G → signal bandwidth-aware mode to other modules
  4. Continuously retry primary — when it recovers, switch back
  5. Report connectivity status to Core API on every transition

Modem management:
  Uses ModemManager (mmcli) for 4G modem detection, connection, signal strength,
  carrier info, and data usage. No Python dependencies beyond subprocess.

Supported modems (tested):
  - Huawei E3372 (HiLink mode — appears as USB Ethernet)
  - Huawei E3372 (Stick mode — ModemManager)
  - Sierra Wireless MC7455 (mPCIe)
  - Quectel EC25 / EC21
  - Any ModemManager-compatible device

usb_modeswitch handles mode-switching for Huawei HiLink → Stick automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ── Connectivity modes ────────────────────────────────────────────────────────

class ConnectivityMode(str, Enum):
    PRIMARY   = "primary"    # ETH0, all good
    FAILOVER  = "failover"   # 4G active
    WIFI      = "wifi"       # WiFi tertiary
    DEGRADED  = "degraded"   # No working uplink
    UNKNOWN   = "unknown"    # Not yet determined


# ── Modem info ────────────────────────────────────────────────────────────────

@dataclass
class ModemInfo:
    present: bool = False
    index: str = ""
    state: str = ""
    signal_quality: int = 0
    carrier: str = ""
    technology: str = ""
    interface: str = ""
    ip_address: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    monthly_rx_mb: float = 0.0
    monthly_tx_mb: float = 0.0
    apn: str = ""
    imei: str = ""
    error: str = ""


# ── Main manager ──────────────────────────────────────────────────────────────

class ConnectivityManager:
    """
    Manages multi-WAN failover and bandwidth-aware mode signalling.

    Usage:
        conn = ConnectivityManager(config)
        await conn.start()                     # detect modem, set initial state
        asyncio.create_task(conn.run())        # background monitoring loop
        if conn.is_on_failover:
            # reduce camera quality, increase sensor interval, etc.
    """

    def __init__(self, config):
        self.config = config
        self.conn_cfg = config.connectivity

        self._mode: ConnectivityMode = ConnectivityMode.UNKNOWN
        self._modem: ModemInfo = ModemInfo()
        self._fail_count: int = 0
        self._last_failover: float = 0.0
        self._last_recovery: float = 0.0
        self._primary_ok: bool = True
        self._bandwidth_callbacks: list = []  # callables(mode)

        # Monthly usage tracking (reset on 1st of month)
        self._monthly_start: float = time.time()
        self._monthly_rx_bytes: int = 0
        self._monthly_tx_bytes: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def mode(self) -> ConnectivityMode:
        return self._mode

    @property
    def is_on_failover(self) -> bool:
        return self._mode in (ConnectivityMode.FAILOVER, ConnectivityMode.WIFI)

    @property
    def is_degraded(self) -> bool:
        return self._mode == ConnectivityMode.DEGRADED

    def register_bandwidth_callback(self, cb) -> None:
        """
        Register a callback to be called on mode changes.
        Signature: cb(mode: ConnectivityMode) -> None (or coroutine)
        """
        self._bandwidth_callbacks.append(cb)

    def get_status(self) -> dict:
        primary_cfg = self.conn_cfg.get("primary", {})
        failover_cfg = self.conn_cfg.get("failover", {})
        return {
            "mode": self._mode.value,
            "primary": {
                "interface": primary_cfg.get("interface", "eth0"),
                "ok": self._primary_ok,
                "fail_count": self._fail_count,
            },
            "failover": {
                "interface": failover_cfg.get("interface", "wwan0"),
                "enabled": failover_cfg.get("enabled", "auto") != "false",
                "modem": {
                    "present": self._modem.present,
                    "state": self._modem.state,
                    "carrier": self._modem.carrier,
                    "signal_quality": self._modem.signal_quality,
                    "technology": self._modem.technology,
                    "apn": self._modem.apn,
                    "rx_bytes": self._modem.rx_bytes,
                    "tx_bytes": self._modem.tx_bytes,
                    "monthly_rx_mb": round(self._modem.monthly_rx_mb, 1),
                    "monthly_tx_mb": round(self._modem.monthly_tx_mb, 1),
                } if self._modem.present else None,
            },
            "last_failover": self._last_failover or None,
            "last_recovery": self._last_recovery or None,
        }

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Detect modem and establish initial connectivity state."""
        failover_cfg = self.conn_cfg.get("failover", {})
        enabled = failover_cfg.get("enabled", "auto")

        if enabled != "false":
            modem = await self._detect_modem()
            if modem:
                self._modem = modem
                logger.info(
                    "4G modem detected: %s (index=%s, iface=%s)",
                    modem.carrier or "unknown carrier", modem.index, modem.interface,
                )
                # Ensure modem is connected
                await self._ensure_modem_connected()
            elif enabled == "true":
                logger.warning("4G failover forced enabled but no modem found")
            else:
                logger.info("No 4G modem detected — failover disabled")

        # Check primary
        ok = await self._check_primary()
        self._primary_ok = ok
        if ok:
            self._mode = ConnectivityMode.PRIMARY
        elif self._modem.present:
            logger.warning("Primary down at startup — activating 4G failover")
            await self._activate_failover()
        else:
            self._mode = ConnectivityMode.DEGRADED
            logger.error("No connectivity on startup")

        logger.info("Connectivity initialized: mode=%s", self._mode.value)

    async def run(self) -> None:
        """Main monitoring loop."""
        primary_cfg = self.conn_cfg.get("primary", {})
        interval = primary_cfg.get("check_interval", 30)

        while True:
            await asyncio.sleep(interval)
            await self._check_and_failover()

            # Update modem stats if active
            if self._modem.present:
                await self._update_modem_stats()

            # Monthly usage reset
            self._check_monthly_reset()

    # ── Connectivity checks ───────────────────────────────────────────────────

    async def _check_primary(self) -> bool:
        """Ping check target on primary interface."""
        primary_cfg = self.conn_cfg.get("primary", {})
        interface = primary_cfg.get("interface", "eth0")
        target = primary_cfg.get("check_target", "") or self._core_host()

        if not target:
            # No target — assume up
            return True

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["ping", "-c", "1", "-W", "3", "-I", interface, target],
                    capture_output=True, timeout=5,
                )
            )
            return result.returncode == 0
        except Exception:
            return False

    def _core_host(self) -> str:
        """Extract hostname/IP from core_api_url."""
        url = getattr(self.config, "core_api_url", "") or ""
        # Strip scheme, path
        host = url.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
        return host or "8.8.8.8"

    async def _check_and_failover(self) -> None:
        """Core monitoring logic — called every check_interval."""
        primary_cfg = self.conn_cfg.get("primary", {})
        threshold = primary_cfg.get("failure_threshold", 3)

        ok = await self._check_primary()

        if ok:
            if self._fail_count > 0:
                logger.info("Primary connectivity restored after %d failures", self._fail_count)

            self._fail_count = 0

            if self._mode != ConnectivityMode.PRIMARY:
                await self._restore_primary()
        else:
            self._fail_count += 1
            logger.debug("Primary check failed (%d/%d)", self._fail_count, threshold)

            if self._fail_count >= threshold and self._mode == ConnectivityMode.PRIMARY:
                logger.warning(
                    "Primary failed %d times — activating failover", self._fail_count
                )
                await self._activate_failover()

    # ── Failover activation / restoration ────────────────────────────────────

    async def _activate_failover(self) -> None:
        """Activate 4G failover."""
        failover_cfg = self.conn_cfg.get("failover", {})

        if not self._modem.present:
            logger.error("Cannot activate failover: no modem available")
            self._mode = ConnectivityMode.DEGRADED
            await self._notify_mode_change()
            return

        logger.warning("⚡ Activating 4G failover (mode: %s → failover)", self._mode.value)

        # Ensure modem is connected
        connected = await self._ensure_modem_connected()
        if not connected:
            logger.error("Modem present but cannot connect — degraded mode")
            self._mode = ConnectivityMode.DEGRADED
            await self._notify_mode_change()
            return

        # Lower primary interface metric, raise failover metric
        # (or just ensure failover route is preferred)
        iface = failover_cfg.get("interface", self._modem.interface or "wwan0")
        await self._set_route_metric(iface, 100)   # make failover preferred

        primary_iface = self.conn_cfg.get("primary", {}).get("interface", "eth0")
        await self._set_route_metric(primary_iface, 500)  # demote primary

        self._mode = ConnectivityMode.FAILOVER
        self._primary_ok = False
        self._last_failover = time.time()

        await self._notify_mode_change()
        await self._report_to_core()
        await self._alert_failover()

    async def _restore_primary(self) -> None:
        """Restore primary interface as default route."""
        primary_cfg = self.conn_cfg.get("primary", {})
        failover_cfg = self.conn_cfg.get("failover", {})
        primary_iface = primary_cfg.get("interface", "eth0")
        failover_iface = failover_cfg.get("interface", self._modem.interface or "wwan0")

        logger.info("✅ Restoring primary connectivity (%s)", primary_iface)

        await self._set_route_metric(primary_iface, 100)
        await self._set_route_metric(failover_iface, 500)

        prev_mode = self._mode
        self._mode = ConnectivityMode.PRIMARY
        self._primary_ok = True
        self._fail_count = 0
        self._last_recovery = time.time()

        await self._notify_mode_change()
        await self._report_to_core()

        if prev_mode == ConnectivityMode.FAILOVER:
            await self._alert_recovery()

    # ── Route management ──────────────────────────────────────────────────────

    async def _set_route_metric(self, interface: str, metric: int) -> None:
        """Adjust the default route metric for an interface via ip route."""
        try:
            # Get current default route via interface
            r = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["ip", "route", "show", "dev", interface, "default"],
                    capture_output=True, text=True,
                )
            )
            lines = r.stdout.strip().splitlines()

            for line in lines:
                if "default" in line or "0.0.0.0" in line:
                    # Delete old route, re-add with new metric
                    gateway_match = re.search(r"via (\S+)", line)
                    if gateway_match:
                        gw = gateway_match.group(1)
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: subprocess.run(
                                ["ip", "route", "del", "default", "via", gw, "dev", interface],
                                capture_output=True,
                            )
                        )
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: subprocess.run(
                                ["ip", "route", "add", "default", "via", gw,
                                 "dev", interface, "metric", str(metric)],
                                capture_output=True,
                            )
                        )
                        logger.debug("Route metric set: %s → %d via %s", interface, metric, gw)
        except Exception as e:
            logger.warning("Route metric update failed for %s: %s", interface, e)

    # ── Modem management ──────────────────────────────────────────────────────

    async def _detect_modem(self) -> Optional[ModemInfo]:
        """
        Use mmcli to detect any connected ModemManager modem.
        Returns ModemInfo if a modem is present, else None.
        """
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["mmcli", "-L", "--output-json"],
                    capture_output=True, text=True, timeout=10,
                )
            )
            if r.returncode != 0:
                # ModemManager not running or no modems
                return None

            data = json.loads(r.stdout)
            modems = data.get("modem-list", [])
            if not modems:
                return None

            # Use first modem
            modem_path = modems[0]
            index = modem_path.split("/")[-1]

            return await self._get_modem_info(index)

        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return None
        except subprocess.TimeoutExpired:
            logger.debug("mmcli timeout — no modem or ModemManager not ready")
            return None

    async def _get_modem_info(self, index: str) -> ModemInfo:
        """Query mmcli for detailed modem information."""
        info = ModemInfo(present=True, index=index)
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["mmcli", "-m", index, "--output-json"],
                    capture_output=True, text=True, timeout=10,
                )
            )
            if r.returncode != 0:
                info.error = r.stderr.strip()
                return info

            data = json.loads(r.stdout)
            modem = data.get("modem", {})

            generic = modem.get("generic", {})
            info.state = generic.get("state", "unknown")
            info.imei = generic.get("equipment-identifier", "")
            info.technology = generic.get("access-technologies", ["unknown"])
            if isinstance(info.technology, list):
                info.technology = ", ".join(info.technology)

            # Signal quality
            sq = generic.get("signal-quality", {})
            if isinstance(sq, dict):
                info.signal_quality = int(sq.get("value", 0))
            elif isinstance(sq, (int, str)):
                try:
                    info.signal_quality = int(sq)
                except ValueError:
                    pass

            # 3GPP info (carrier, etc.)
            threegpp = modem.get("3gpp", {})
            info.carrier = threegpp.get("operator-name", "")

            # Bearer (data interface + IP)
            bearers = modem.get("generic", {}).get("bearers", [])
            if bearers:
                bearer_index = bearers[0].split("/")[-1]
                bearer_info = await self._get_bearer_info(bearer_index)
                info.interface = bearer_info.get("interface", "wwan0")
                info.ip_address = bearer_info.get("ip", "")
                info.apn = bearer_info.get("apn", "")
                info.rx_bytes = bearer_info.get("rx-bytes", 0)
                info.tx_bytes = bearer_info.get("tx-bytes", 0)
            else:
                # Default interface name
                failover_cfg = self.conn_cfg.get("failover", {})
                info.interface = failover_cfg.get("interface", "wwan0")

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            info.error = str(e)

        return info

    async def _get_bearer_info(self, bearer_index: str) -> dict:
        """Get bearer (data connection) details."""
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["mmcli", "-b", bearer_index, "--output-json"],
                    capture_output=True, text=True, timeout=5,
                )
            )
            data = json.loads(r.stdout)
            bearer = data.get("bearer", {})

            props = bearer.get("properties", {})
            status = bearer.get("status", {})
            stats = bearer.get("stats", {})

            apn = props.get("apn", "")
            interface = status.get("interface", "wwan0")
            ip_address = ""

            ipv4_config = bearer.get("ipv4-config", {})
            if isinstance(ipv4_config, dict):
                ip_address = ipv4_config.get("address", "")

            return {
                "apn": apn,
                "interface": interface,
                "ip": ip_address,
                "rx-bytes": int(stats.get("rx-bytes", 0)),
                "tx-bytes": int(stats.get("tx-bytes", 0)),
            }
        except Exception:
            return {}

    async def _ensure_modem_connected(self) -> bool:
        """Connect modem if not already connected."""
        if not self._modem.present:
            return False

        # Re-fetch state
        updated = await self._get_modem_info(self._modem.index)
        self._modem = updated

        if self._modem.state in ("connected", "bearer"):
            return True

        if self._modem.state == "disabled":
            logger.info("Enabling modem %s...", self._modem.index)
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["mmcli", "-m", self._modem.index, "--enable"],
                    capture_output=True, timeout=15,
                )
            )
            await asyncio.sleep(3)

        # Connect with APN
        failover_cfg = self.conn_cfg.get("failover", {})
        apn = failover_cfg.get("apn", "internet")
        pin = failover_cfg.get("pin", "")

        # Enter PIN if needed
        if pin and self._modem.state == "locked":
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["mmcli", "-m", self._modem.index, f"--pin={pin}"],
                    capture_output=True, timeout=10,
                )
            )
            await asyncio.sleep(2)

        logger.info("Connecting modem (APN: %s)...", apn)
        r = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [
                    "mmcli", "-m", self._modem.index,
                    "--simple-connect", f"apn={apn}",
                ],
                capture_output=True, text=True, timeout=30,
            )
        )

        success = r.returncode == 0
        if success:
            await asyncio.sleep(5)  # wait for interface to come up
            updated = await self._get_modem_info(self._modem.index)
            self._modem = updated
            logger.info("Modem connected: %s @ %s", self._modem.carrier, self._modem.ip_address)
        else:
            logger.error("Modem connect failed: %s", r.stderr.strip())

        return success

    async def _update_modem_stats(self) -> None:
        """Refresh modem stats (signal, data usage)."""
        try:
            updated = await self._get_modem_info(self._modem.index)
            self._modem.signal_quality = updated.signal_quality
            self._modem.state = updated.state
            self._modem.rx_bytes = updated.rx_bytes
            self._modem.tx_bytes = updated.tx_bytes

            # Accumulate monthly usage
            self._modem.monthly_rx_mb = (
                self._monthly_rx_bytes + self._modem.rx_bytes
            ) / (1024 * 1024)
            self._modem.monthly_tx_mb = (
                self._monthly_tx_bytes + self._modem.tx_bytes
            ) / (1024 * 1024)

            # Warn on monthly limit approach
            failover_cfg = self.conn_cfg.get("failover", {})
            max_gb = failover_cfg.get("max_monthly_gb", 5)
            total_gb = (self._modem.monthly_rx_mb + self._modem.monthly_tx_mb) / 1024
            if total_gb > max_gb * 0.9:
                logger.warning(
                    "4G data usage: %.2f GB of %.0f GB monthly limit (90%%+)",
                    total_gb, max_gb,
                )
        except Exception as e:
            logger.debug("Modem stats update error: %s", e)

    def _check_monthly_reset(self) -> None:
        """Reset monthly counters on the 1st of each month."""
        from datetime import datetime
        now = datetime.now()
        if now.day == 1 and now.hour == 0:
            start = datetime(now.year, now.month, 1).timestamp()
            if self._monthly_start < start:
                logger.info("Monthly 4G usage counters reset")
                self._monthly_rx_bytes = 0
                self._monthly_tx_bytes = 0
                self._monthly_start = start

    # ── Bandwidth-aware mode signalling ──────────────────────────────────────

    async def _notify_mode_change(self) -> None:
        """Notify registered modules about connectivity mode change."""
        for cb in self._bandwidth_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(self._mode)
                else:
                    cb(self._mode)
            except Exception as e:
                logger.warning("Bandwidth callback error: %s", e)

    # ── Core API reporting ────────────────────────────────────────────────────

    async def _report_to_core(self) -> None:
        """POST connectivity status to Core API."""
        core_url = getattr(self.config, "core_api_url", "")
        if not core_url:
            return

        try:
            payload = {
                "event": "connectivity_change",
                "mode": self._mode.value,
                "modem": {
                    "present": self._modem.present,
                    "carrier": self._modem.carrier,
                    "signal_quality": self._modem.signal_quality,
                } if self._modem.present else None,
                "ts": time.time(),
            }
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{core_url.rstrip('/')}/api/gateway/connectivity",
                    json=payload,
                )
        except Exception as e:
            logger.debug("Core connectivity report failed: %s", e)

    async def _alert_failover(self) -> None:
        """Send instant alert when switching to 4G."""
        carrier = self._modem.carrier or "unknown"
        sig = self._modem.signal_quality
        logger.warning(
            "🔴 FAILOVER ACTIVE: Primary down — switched to 4G (%s, signal %d%%)",
            carrier, sig,
        )
        # Core API alert
        core_url = getattr(self.config, "core_api_url", "")
        if core_url:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"{core_url.rstrip('/')}/api/gateway/alerts",
                        json={
                            "type": "connectivity_failover",
                            "severity": "warning",
                            "message": f"Gateway switched to 4G failover ({carrier}, {sig}% signal)",
                            "ts": time.time(),
                        }
                    )
            except Exception:
                pass

    async def _alert_recovery(self) -> None:
        """Alert when primary recovers."""
        logger.info("✅ Primary connectivity recovered — 4G failover deactivated")
        core_url = getattr(self.config, "core_api_url", "")
        if core_url:
            try:
                downtime = time.time() - self._last_failover if self._last_failover else 0
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"{core_url.rstrip('/')}/api/gateway/alerts",
                        json={
                            "type": "connectivity_recovered",
                            "severity": "info",
                            "message": f"Primary connectivity restored (downtime: {downtime:.0f}s)",
                            "ts": time.time(),
                        }
                    )
            except Exception:
                pass
