"""
OpenCPO Bastion — Charger Discovery

Finds OCPP-capable EV chargers on the local network via:
  - mDNS/Avahi (OCPP devices sometimes advertise via _ocpp._tcp)
  - ARP scan of local subnet (finds any IP-connected device)

Discovered chargers are reported to Core API and auto-configured
as proxy routes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from gateway.config import GatewayConfig

logger = logging.getLogger(__name__)

# Well-known OCPP WebSocket ports
OCPP_PORTS = [80, 443, 8080, 8443, 9100, 9201]


@dataclass
class DiscoveredCharger:
    ip: str
    port: int
    charger_id: str
    protocol: str = "ws"
    hostname: str = ""
    mac: str = ""
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class ChargerDiscovery:
    def __init__(self, config: GatewayConfig, interval_seconds: int = 300):
        self.config = config
        self.interval = interval_seconds
        self._chargers: dict[str, DiscoveredCharger] = {}
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=10)
        asyncio.create_task(self._discovery_loop())
        logger.info("Charger discovery started (interval: %ds)", self.interval)

    async def _discovery_loop(self) -> None:
        while True:
            await self._run_discovery()
            await asyncio.sleep(self.interval)

    async def _run_discovery(self) -> None:
        logger.debug("Running charger discovery scan...")
        found = []

        # mDNS scan
        mdns_results = await self._mdns_scan()
        found.extend(mdns_results)

        # ARP + port scan
        arp_results = await self._arp_scan()
        found.extend(arp_results)

        new_count = 0
        for charger in found:
            key = f"{charger.ip}:{charger.port}"
            if key not in self._chargers:
                self._chargers[key] = charger
                new_count += 1
                await self._report_to_core(charger)
                logger.info("New charger discovered: %s (port %d)", charger.ip, charger.port)
            else:
                self._chargers[key].last_seen = time.time()

        if new_count:
            logger.info("Discovery: %d new charger(s) found", new_count)

    async def _mdns_scan(self) -> list[DiscoveredCharger]:
        """Scan for OCPP services via mDNS."""
        results = []
        try:
            from zeroconf import ServiceBrowser, Zeroconf

            zc = Zeroconf()
            found_services = []

            class Listener:
                def add_service(self, zc, type_, name):
                    info = zc.get_service_info(type_, name)
                    if info:
                        found_services.append(info)
                def remove_service(self, *_): pass
                def update_service(self, *_): pass

            for svc_type in ("_ocpp._tcp.local.", "_ws._tcp.local."):
                ServiceBrowser(zc, svc_type, Listener())

            await asyncio.sleep(3)
            zc.close()

            for info in found_services:
                try:
                    ip = info.parsed_addresses()[0]
                    charger_id = info.name.split(".")[0]
                    results.append(DiscoveredCharger(
                        ip=ip,
                        port=info.port,
                        charger_id=charger_id,
                        hostname=info.server,
                    ))
                except Exception:
                    pass

        except ImportError:
            logger.debug("zeroconf not installed; mDNS discovery skipped")
        except Exception as e:
            logger.debug("mDNS scan error: %s", e)

        return results

    async def _arp_scan(self) -> list[DiscoveredCharger]:
        """ARP scan local subnet + probe OCPP ports."""
        results = []

        # Determine local subnet
        subnet = self._local_subnet()
        if not subnet:
            return results

        # Scan in batches to avoid overwhelming a Pi Zero
        hosts = list(ipaddress.ip_network(subnet, strict=False).hosts())
        semaphore = asyncio.Semaphore(20)  # max 20 concurrent probes

        async def probe(host_ip: str) -> Optional[DiscoveredCharger]:
            async with semaphore:
                for port in OCPP_PORTS:
                    try:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_connection(host_ip, port),
                            timeout=0.5,
                        )
                        writer.close()
                        await writer.wait_closed()
                        return DiscoveredCharger(
                            ip=host_ip,
                            port=port,
                            charger_id=host_ip.replace(".", "-"),
                        )
                    except Exception:
                        pass
            return None

        tasks = [probe(str(h)) for h in hosts[:254]]  # limit to /24 equivalent
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result:
                results.append(result)

        return results

    def _local_subnet(self) -> Optional[str]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            # Assume /24 — good enough for EV charging sites
            parts = local_ip.rsplit(".", 1)
            return f"{parts[0]}.0/24"
        except Exception:
            return None

    async def _report_to_core(self, charger: DiscoveredCharger) -> None:
        if not self._http:
            return
        try:
            payload = {
                "ip": charger.ip,
                "port": charger.port,
                "charger_id": charger.charger_id,
                "gateway_proxy_url": f"ws://{charger.ip}:{self.config.proxy_ports.ocpp16}/{charger.charger_id}",
                "first_seen": charger.first_seen,
            }
            await self._http.post(
                f"{self.config.core_api_base}/api/v1/gateway/chargers/discovered",
                json=payload,
            )
        except Exception as e:
            logger.debug("Failed to report charger to core: %s", e)

    def discovered_chargers(self) -> list[dict]:
        return [
            {
                "ip": c.ip,
                "port": c.port,
                "charger_id": c.charger_id,
                "hostname": c.hostname,
                "first_seen": c.first_seen,
                "last_seen": c.last_seen,
            }
            for c in self._chargers.values()
        ]
