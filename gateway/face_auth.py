"""
OpenCPO Bastion — Face-Based Access Control

Listens for face recognition events from UniFi Protect and:
  - Classifies events as "known" (authorized personnel) or "unknown"
  - Alerts on unknown faces during configurable hours
  - Saves thumbnails locally
  - Forwards events to Core API
  - GET /faces/recent — recent events with status
  - GET /faces/stats  — counts by known/unknown/hour

Not performing any face recognition locally — UniFi Protect does that.
We just consume the results and act on them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Optional

import httpx

from gateway.cctv import CameraEvent

logger = logging.getLogger(__name__)

FACE_BUFFER_SIZE = 5_000


@dataclass
class FaceEvent:
    event_id: str
    camera_id: str
    timestamp: float
    known: bool                     # True if Protect identified the person
    name: str = ""                  # Protect's identity label if known
    confidence: float = 0.0
    thumbnail_b64: str = ""
    alerted: bool = False
    forwarded: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self, include_thumbnail: bool = True) -> dict:
        d = {
            "event_id": self.event_id,
            "camera_id": self.camera_id,
            "timestamp": self.timestamp,
            "known": self.known,
            "name": self.name,
            "confidence": self.confidence,
            "alerted": self.alerted,
            "forwarded": self.forwarded,
        }
        if include_thumbnail:
            d["thumbnail_b64"] = self.thumbnail_b64
        return d


class FaceAuthProcessor:
    """
    Processes face recognition events.
    Alerts on unknown faces. Logs known access.
    """

    def __init__(self, config: dict, core_api_url: str, site_id: str = ""):
        self.config = config
        self.enabled: bool = config.get("enabled", True)
        self.core_api_url = core_api_url.rstrip("/")
        self.site_id = site_id

        # Alert config
        alert_hours_str: str = config.get("alert_unknown_hours", "")
        self._alert_start, self._alert_end = self._parse_hours(alert_hours_str)
        self.alert_all_unknown: bool = config.get("alert_all_unknown", False)

        self._buffer: deque[FaceEvent] = deque(maxlen=FACE_BUFFER_SIZE)
        self._alert_callbacks: list = []
        self._http: Optional[httpx.AsyncClient] = None

        # Prometheus
        try:
            from prometheus_client import Counter
            self._known_counter = Counter(
                "opencpo_face_known_total", "Known face events by camera", ["camera_id"]
            )
            self._unknown_counter = Counter(
                "opencpo_face_unknown_total", "Unknown face events by camera", ["camera_id"]
            )
        except ImportError:
            self._known_counter = None
            self._unknown_counter = None

    def _parse_hours(self, hours_str: str) -> tuple[Optional[dt_time], Optional[dt_time]]:
        """Parse 'HH:MM-HH:MM' alert window string."""
        if not hours_str or "-" not in hours_str:
            return None, None
        try:
            start_str, end_str = hours_str.split("-")
            sh, sm = map(int, start_str.strip().split(":"))
            eh, em = map(int, end_str.strip().split(":"))
            return dt_time(sh, sm), dt_time(eh, em)
        except Exception:
            logger.warning("Invalid alert_unknown_hours format: %r (expected HH:MM-HH:MM)", hours_str)
            return None, None

    def _in_alert_window(self) -> bool:
        """Return True if current local time is within the alert window."""
        if self._alert_start is None:
            return False
        now = datetime.now().time()
        if self._alert_start <= self._alert_end:
            return self._alert_start <= now <= self._alert_end
        else:
            # Wraps midnight
            return now >= self._alert_start or now <= self._alert_end

    def add_alert_callback(self, cb) -> None:
        self._alert_callbacks.append(cb)

    async def start(self) -> None:
        if not self.enabled:
            return
        self._http = httpx.AsyncClient(timeout=10)
        logger.info(
            "Face auth processor started (alert_window=%s, alert_all=%s)",
            f"{self._alert_start}–{self._alert_end}" if self._alert_start else "disabled",
            self.alert_all_unknown,
        )

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()

    async def handle_event(self, event: CameraEvent) -> None:
        if not self.enabled:
            return

        # UniFi Protect sets identity name if face is recognized
        name = event.metadata.get("identity", {}).get("name", "")
        confidence = float(event.metadata.get("identity", {}).get("confidence", 0.0))
        known = bool(name)

        fe = FaceEvent(
            event_id=event.event_id,
            camera_id=event.camera_id,
            timestamp=event.timestamp,
            known=known,
            name=name,
            confidence=confidence,
            thumbnail_b64=event.metadata.get("thumbnail_b64", ""),
            metadata=event.metadata,
        )
        self._buffer.append(fe)

        if self._known_counter and known:
            self._known_counter.labels(camera_id=event.camera_id).inc()
        if self._unknown_counter and not known:
            self._unknown_counter.labels(camera_id=event.camera_id).inc()

        if known:
            logger.info("Face: known person '%s' (confidence=%.2f) at camera %s", name, confidence, event.camera_id)
        else:
            logger.info("Face: unknown person at camera %s", event.camera_id)

        # Determine if we should alert
        should_alert = not known and (self.alert_all_unknown or self._in_alert_window())
        if should_alert:
            fe.alerted = True
            logger.warning("FACE ALERT: unknown person detected at camera %s", event.camera_id)
            for cb in self._alert_callbacks:
                try:
                    await cb(fe)
                except Exception as e:
                    logger.debug("Face alert callback error: %s", e)

        # Forward to Core
        await self._forward(fe)

    async def _forward(self, fe: FaceEvent) -> None:
        if not self._http or not self.core_api_url:
            return

        payload = {
            "event_id": fe.event_id,
            "camera_id": fe.camera_id,
            "timestamp": fe.timestamp,
            "known": fe.known,
            "name": fe.name,
            "confidence": fe.confidence,
            "alerted": fe.alerted,
            "site_id": self.site_id,
        }

        try:
            r = await self._http.post(
                f"{self.core_api_url}/api/v1/gateway/face-events",
                json=payload,
            )
            if r.status_code in (200, 201, 204):
                fe.forwarded = True
            else:
                logger.warning("Face event forward failed: %d", r.status_code)
        except Exception as e:
            logger.debug("Face event forward error: %s", e)

    # ── API helpers ─────────────────────────────────────────────────────────────

    def recent_events(
        self,
        limit: int = 50,
        known_only: bool = False,
        unknown_only: bool = False,
        include_thumbnail: bool = False,
    ) -> list[dict]:
        events = list(self._buffer)
        if known_only:
            events = [e for e in events if e.known]
        if unknown_only:
            events = [e for e in events if not e.known]
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return [e.to_dict(include_thumbnail=include_thumbnail) for e in events[:limit]]

    def stats(self) -> dict:
        events = list(self._buffer)
        known = sum(1 for e in events if e.known)
        unknown = sum(1 for e in events if not e.known)
        alerted = sum(1 for e in events if e.alerted)
        return {
            "total": len(events),
            "known": known,
            "unknown": unknown,
            "alerted": alerted,
            "alert_window": (
                f"{self._alert_start}–{self._alert_end}"
                if self._alert_start else None
            ),
            "alert_all_unknown": self.alert_all_unknown,
            "in_alert_window": self._in_alert_window(),
        }
