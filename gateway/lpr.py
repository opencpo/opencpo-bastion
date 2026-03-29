"""
OpenCPO Gateway — License Plate Recognition Integration

Listens for LPR events from UniFi Protect and:
  - Extracts plate number + confidence + camera + timestamp + thumbnail
  - Forwards to Core API: POST /api/v1/lpr/detect
  - Core matches plate → fleet vehicle → auto-authorize charging session
  - Maintains local plate cache for offline operation
  - Ring buffer of recent reads (10,000)

API:
  GET  /lpr/recent              — last N plate reads
  GET  /lpr/search?plate=XX-1   — search history by partial plate
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

from gateway.cctv import CameraEvent

logger = logging.getLogger(__name__)

LPR_BUFFER_SIZE = 10_000
CACHE_PATH = Path("/var/lib/opencpo/lpr_cache.json")


class PlateRead:
    __slots__ = ("plate", "confidence", "camera_id", "timestamp", "thumbnail_b64", "forwarded")

    def __init__(
        self,
        plate: str,
        confidence: float,
        camera_id: str,
        timestamp: float,
        thumbnail_b64: str = "",
        forwarded: bool = False,
    ):
        self.plate = plate
        self.confidence = confidence
        self.camera_id = camera_id
        self.timestamp = timestamp
        self.thumbnail_b64 = thumbnail_b64
        self.forwarded = forwarded

    def to_dict(self, include_thumbnail: bool = True) -> dict:
        d = {
            "plate": self.plate,
            "confidence": self.confidence,
            "camera_id": self.camera_id,
            "timestamp": self.timestamp,
            "forwarded": self.forwarded,
        }
        if include_thumbnail:
            d["thumbnail_b64"] = self.thumbnail_b64
        return d


class LPRProcessor:
    """
    Processes license plate events from UniFi Protect.
    Routes to Core API. Local cache for offline fallback.
    """

    def __init__(self, config: dict, core_api_url: str, site_id: str = ""):
        self.config = config
        self.enabled: bool = config.get("enabled", True)
        self.auto_authorize: bool = config.get("auto_authorize", False)
        self.core_api_url = core_api_url.rstrip("/")
        self.site_id = site_id

        # Ring buffer of recent reads
        self._buffer: deque[PlateRead] = deque(maxlen=LPR_BUFFER_SIZE)

        # Local plate cache: plate → {authorized: bool, vehicle_id: str, last_seen: float}
        self._cache: dict[str, dict] = {}
        self._load_cache()

        # Pending queue for offline forwarding
        self._pending: deque[PlateRead] = deque(maxlen=500)

        self._http: Optional[httpx.AsyncClient] = None
        self._flush_task: Optional[asyncio.Task] = None

        # Prometheus
        try:
            from prometheus_client import Counter, Histogram
            self._reads_counter = Counter(
                "opencpo_lpr_reads_total",
                "LPR reads by camera",
                ["camera_id"],
            )
            self._confidence_hist = Histogram(
                "opencpo_lpr_confidence",
                "LPR confidence scores",
                buckets=[0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0],
            )
        except ImportError:
            self._reads_counter = None
            self._confidence_hist = None

    def _load_cache(self) -> None:
        if CACHE_PATH.exists():
            try:
                self._cache = json.loads(CACHE_PATH.read_text())
                logger.info("LPR: loaded %d plates from local cache", len(self._cache))
            except Exception as e:
                logger.warning("LPR cache load error: %s", e)

    def _save_cache(self) -> None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(self._cache, indent=2))
        except Exception as e:
            logger.warning("LPR cache save error: %s", e)

    async def start(self) -> None:
        if not self.enabled:
            return
        self._http = httpx.AsyncClient(timeout=10)
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("LPR processor started (auto_authorize=%s)", self.auto_authorize)

    async def stop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
        if self._http:
            await self._http.aclose()
        self._save_cache()

    async def handle_event(self, event: CameraEvent) -> None:
        """Process an LPR event from UniFi Protect."""
        if not self.enabled:
            return

        # Extract plate data from event metadata
        plate = event.metadata.get("license_plate_number", "")
        confidence = float(event.metadata.get("confidence", 0.0))

        if not plate:
            logger.debug("LPR event with no plate number, skipping")
            return

        # Normalize plate: uppercase, strip spaces
        plate = plate.upper().strip().replace(" ", "")

        read = PlateRead(
            plate=plate,
            confidence=confidence,
            camera_id=event.camera_id,
            timestamp=event.timestamp,
            thumbnail_b64=event.metadata.get("thumbnail_b64", ""),
        )

        self._buffer.append(read)

        if self._reads_counter:
            self._reads_counter.labels(camera_id=event.camera_id).inc()
        if self._confidence_hist:
            self._confidence_hist.observe(confidence)

        logger.info(
            "LPR read: plate=%s confidence=%.2f camera=%s",
            plate, confidence, event.camera_id,
        )

        # Check local cache for known plate
        cached = self._cache.get(plate)
        if cached:
            logger.info("LPR: plate %s known (vehicle_id=%s)", plate, cached.get("vehicle_id"))
            cached["last_seen"] = event.timestamp
        else:
            self._cache[plate] = {"last_seen": event.timestamp, "authorized": None}

        # Forward to Core
        await self._forward(read)

        # Periodic cache save (every 50 reads)
        if len(self._buffer) % 50 == 0:
            self._save_cache()

    async def _forward(self, read: PlateRead) -> None:
        if not self._http:
            self._pending.append(read)
            return

        payload = {
            "plate": read.plate,
            "confidence": read.confidence,
            "camera_id": read.camera_id,
            "timestamp": read.timestamp,
            "site_id": self.site_id,
            "auto_authorize": self.auto_authorize,
        }

        try:
            r = await self._http.post(
                f"{self.core_api_url}/api/v1/lpr/detect",
                json=payload,
            )
            if r.status_code in (200, 201):
                read.forwarded = True
                # Update cache with Core's response
                resp = r.json()
                vehicle_id = resp.get("vehicle_id")
                authorized = resp.get("authorized", False)
                if vehicle_id:
                    self._cache[read.plate] = {
                        "vehicle_id": vehicle_id,
                        "authorized": authorized,
                        "last_seen": read.timestamp,
                    }
                    logger.info(
                        "LPR: Core matched plate %s → vehicle %s (authorized=%s)",
                        read.plate, vehicle_id, authorized,
                    )
            else:
                logger.warning("LPR forward failed: %d", r.status_code)
                self._pending.append(read)
        except Exception as e:
            logger.warning("LPR forward error: %s", e)
            self._pending.append(read)

    async def _flush_loop(self) -> None:
        """Retry pending events when Core becomes reachable."""
        while True:
            await asyncio.sleep(60)
            if not self._pending:
                continue
            logger.info("LPR: flushing %d pending reads", len(self._pending))
            while self._pending:
                read = self._pending[0]
                await self._forward(read)
                if read.forwarded:
                    self._pending.popleft()
                else:
                    break  # still offline, stop trying

    # ── API helpers ─────────────────────────────────────────────────────────────

    def recent_reads(self, limit: int = 50, include_thumbnail: bool = False) -> list[dict]:
        reads = list(self._buffer)
        reads.sort(key=lambda r: r.timestamp, reverse=True)
        return [r.to_dict(include_thumbnail=include_thumbnail) for r in reads[:limit]]

    def search(self, query: str, include_thumbnail: bool = False) -> list[dict]:
        """Search history by partial plate match."""
        query = query.upper().strip()
        matches = [
            r for r in self._buffer
            if query in r.plate
        ]
        matches.sort(key=lambda r: r.timestamp, reverse=True)
        return [r.to_dict(include_thumbnail=include_thumbnail) for r in matches[:100]]

    def known_plates(self) -> dict:
        return {
            plate: info
            for plate, info in self._cache.items()
            if info.get("authorized") is not None
        }
