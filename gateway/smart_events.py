"""
OpenCPO Bastion — Smart Detection Event Processor

Subscribes to UniFi Protect's real-time WebSocket events and:
  - Logs to a local ring buffer
  - Forwards to OpenCPO Core (POST /api/v1/gateway/events)
  - SSE stream at /events for admin dashboard
  - Prometheus counters by event type

Relies on cctv.py registering the event callback on the UniFiProvider.
This module is the consumer; cctv.py drives the event feed.

Event types handled:
  smartDetectZone — person, vehicle, animal, package
  ring            — doorbell ring
  motion          — basic motion
  fingerprint     — fingerprint auth event
  face            — face recognition (name + confidence if known)
  lpr             — license plate read (routed to lpr.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Optional

import httpx

from gateway.cctv import CameraEvent

logger = logging.getLogger(__name__)

# Ring buffer: 10,000 events (same pattern as OCPP tap)
EVENT_BUFFER_SIZE = 10_000

# Event types that get forwarded to Core
FORWARD_TYPES = {
    "smartDetectZone",
    "smartDetect",
    "ring",
    "motion",
    "fingerprint",
    "face",
}


class SmartEventProcessor:
    """
    Receives CameraEvent objects from the CCTV provider's WebSocket feed,
    processes them, and routes to Core API + SSE subscribers.
    """

    def __init__(
        self,
        config: dict,
        core_api_url: str,
        site_id: str = "",
        lpr_processor=None,
        face_processor=None,
    ):
        self.config = config
        self.core_api_url = core_api_url.rstrip("/")
        self.site_id = site_id
        self.enabled: bool = config.get("enabled", True)
        self.forward_to_core: bool = config.get("forward_to_core", True)

        # Sub-processors (injected to avoid circular imports)
        self._lpr = lpr_processor
        self._face = face_processor

        # Ring buffer
        self._buffer: deque = deque(maxlen=EVENT_BUFFER_SIZE)

        # SSE subscribers: list of asyncio.Queue
        self._sse_subscribers: list[asyncio.Queue] = []

        # Prometheus counters (lazy import)
        self._counters: dict[str, Any] = {}
        self._setup_metrics()

        # HTTP client for Core forwarding
        self._http: Optional[httpx.AsyncClient] = None

    def _setup_metrics(self) -> None:
        try:
            from prometheus_client import Counter
            self._event_counter = Counter(
                "opencpo_camera_events_total",
                "Camera events by type",
                ["event_type", "camera_id"],
            )
        except ImportError:
            self._event_counter = None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Smart event processor disabled")
            return
        self._http = httpx.AsyncClient(timeout=10)
        logger.info("Smart event processor started (forward_to_core=%s)", self.forward_to_core)

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()

    async def handle_event(self, event: CameraEvent) -> None:
        """Main entry point — called by cctv.py for each incoming event."""
        if not self.enabled:
            return

        # Enrich event with site context
        event.metadata["site_id"] = self.site_id

        # Buffer
        self._buffer.append(event)

        # Metrics
        if self._event_counter:
            self._event_counter.labels(
                event_type=event.event_type,
                camera_id=event.camera_id,
            ).inc()

        # Route to sub-processors
        smart_types = event.metadata.get("smart_detect_types", [])

        if "licensePlate" in smart_types or event.event_type == "lpr":
            if self._lpr:
                await self._lpr.handle_event(event)

        if event.event_type in ("face", "fingerprint"):
            if self._face:
                await self._face.handle_event(event)

        # SSE broadcast
        await self._broadcast_sse(event)

        # Forward to Core
        if self.forward_to_core and event.event_type in FORWARD_TYPES:
            await self._forward_to_core(event)

        logger.debug(
            "Event: type=%s camera=%s meta=%s",
            event.event_type, event.camera_id, event.metadata,
        )

    async def _forward_to_core(self, event: CameraEvent) -> None:
        if not self._http or not self.core_api_url:
            return

        payload = {
            "event_id": event.event_id,
            "camera_id": event.camera_id,
            "event_type": event.event_type,
            "timestamp": event.timestamp,
            "site_id": self.site_id,
            "metadata": event.metadata,
        }

        try:
            r = await self._http.post(
                f"{self.core_api_url}/api/v1/gateway/events",
                json=payload,
            )
            if r.status_code not in (200, 201, 204):
                logger.warning("Core event forward failed: %d %s", r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("Core event forward error: %s", e)

    async def _broadcast_sse(self, event: CameraEvent) -> None:
        if not self._sse_subscribers:
            return

        data = json.dumps({
            "event_id": event.event_id,
            "camera_id": event.camera_id,
            "type": event.event_type,
            "timestamp": event.timestamp,
            "metadata": event.metadata,
        })
        dead = []
        for q in self._sse_subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._sse_subscribers.remove(q)

    def subscribe_sse(self) -> asyncio.Queue:
        """Create a new SSE subscriber queue. Caller reads from it."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._sse_subscribers.append(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue) -> None:
        try:
            self._sse_subscribers.remove(q)
        except ValueError:
            pass

    def recent_events(
        self,
        limit: int = 100,
        event_type: Optional[str] = None,
        camera_id: Optional[str] = None,
    ) -> list[dict]:
        events = list(self._buffer)
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if camera_id:
            events = [e for e in events if e.camera_id == camera_id]
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return [
            {
                "event_id": e.event_id,
                "camera_id": e.camera_id,
                "type": e.event_type,
                "timestamp": e.timestamp,
                "metadata": e.metadata,
            }
            for e in events[:limit]
        ]
