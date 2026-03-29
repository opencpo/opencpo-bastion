"""
OpenCPO Gateway — CCTV Proxy

Zero-trust camera integration. Cameras stay on the local LAN;
streams are proxied through the Tailscale tunnel to admin panel.

Provider abstraction supports multiple camera backends:
  - UniFi Protect (via uiprotect library) — primary target
  - ONVIF (generic fallback for any standards-compliant camera)

Management endpoints bind to Tailscale IP only.
Recording is local (circular buffer). No cloud video.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class Camera:
    camera_id: str
    name: str
    model: str
    state: str                      # connected | disconnected | unknown
    provider: str                   # unifi | onvif
    rtsp_url: str = ""              # primary RTSP URL
    snapshot_url: str = ""          # direct snapshot URL if available
    supports_ptz: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class CameraEvent:
    event_id: str
    camera_id: str
    event_type: str                 # motion | smartDetect | ring | lpr | face
    timestamp: float
    thumbnail: Optional[bytes] = None
    metadata: dict = field(default_factory=dict)


# ── Provider interface ─────────────────────────────────────────────────────────

class CameraProvider(ABC):
    """Abstract interface all camera backends must implement."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection / authenticate."""
        ...

    @abstractmethod
    async def discover(self) -> list[Camera]:
        """Return list of cameras from this provider."""
        ...

    @abstractmethod
    async def get_stream_url(self, camera_id: str, quality: str = "medium") -> str:
        """Return RTSP URL for the given camera and quality level."""
        ...

    @abstractmethod
    async def get_snapshot(self, camera_id: str) -> bytes:
        """Return JPEG snapshot bytes."""
        ...

    @abstractmethod
    async def get_events(self, camera_id: str, since: float) -> list[CameraEvent]:
        """Return events since the given Unix timestamp."""
        ...

    @abstractmethod
    async def ptz_command(self, camera_id: str, pan: float, tilt: float, zoom: float) -> None:
        """Send PTZ command (raise NotImplementedError if unsupported)."""
        ...

    async def disconnect(self) -> None:
        pass


# ── ONVIF provider ─────────────────────────────────────────────────────────────

class ONVIFProvider(CameraProvider):
    """
    Generic ONVIF camera backend.
    Discovers cameras via ONVIF WS-Discovery on the local subnet.
    Falls back to direct IP scan if multicast discovery fails.
    Requires: onvif-zeep (pip install onvif-zeep)
    """

    def __init__(self, config: dict):
        self.config = config
        self._cameras: dict[str, Camera] = {}
        self._onvif_devices: dict[str, Any] = {}

    async def connect(self) -> None:
        logger.info("ONVIF provider: ready (discovery on demand)")

    async def discover(self) -> list[Camera]:
        cameras = []
        try:
            from onvif import ONVIFCamera
            from wsdiscovery import WSDiscovery

            wsd = WSDiscovery()
            wsd.start()
            services = wsd.searchServices()
            wsd.stop()

            for svc in services:
                xaddrs = svc.getXAddrs()
                for addr in xaddrs:
                    if "onvif" in addr.lower():
                        try:
                            cam = await self._probe_onvif(addr)
                            if cam:
                                cameras.append(cam)
                                self._cameras[cam.camera_id] = cam
                        except Exception as e:
                            logger.debug("ONVIF probe failed for %s: %s", addr, e)

        except ImportError:
            logger.warning("onvif-zeep not installed; ONVIF discovery unavailable")
        except Exception as e:
            logger.warning("ONVIF discovery error: %s", e)

        logger.info("ONVIF: discovered %d camera(s)", len(cameras))
        return cameras

    async def _probe_onvif(self, addr: str) -> Optional[Camera]:
        from urllib.parse import urlparse
        from onvif import ONVIFCamera

        parsed = urlparse(addr)
        host = parsed.hostname or parsed.path
        port = parsed.port or 80

        cam = ONVIFCamera(host, port, "admin", "")
        await asyncio.get_event_loop().run_in_executor(None, cam.update_xaddrs)

        media = cam.create_media_service()
        profiles = await asyncio.get_event_loop().run_in_executor(None, media.GetProfiles)
        if not profiles:
            return None

        profile = profiles[0]
        uri_req = media.create_type("GetStreamUri")
        uri_req.ProfileToken = profile.token
        uri_req.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        uri_resp = await asyncio.get_event_loop().run_in_executor(None, media.GetStreamUri, uri_req)
        rtsp_url = uri_resp.Uri

        camera_id = f"onvif-{host.replace('.', '-')}"
        return Camera(
            camera_id=camera_id,
            name=f"ONVIF @ {host}",
            model="ONVIF",
            state="connected",
            provider="onvif",
            rtsp_url=rtsp_url,
        )

    async def get_stream_url(self, camera_id: str, quality: str = "medium") -> str:
        cam = self._cameras.get(camera_id)
        if not cam:
            raise ValueError(f"Unknown camera: {camera_id}")
        return cam.rtsp_url

    async def get_snapshot(self, camera_id: str) -> bytes:
        cam = self._cameras.get(camera_id)
        if not cam:
            raise ValueError(f"Unknown camera: {camera_id}")
        # Use ffmpeg to grab a single frame from the RTSP stream
        return await _ffmpeg_snapshot(cam.rtsp_url)

    async def get_events(self, camera_id: str, since: float) -> list[CameraEvent]:
        # Generic ONVIF doesn't push events — caller polls at interval
        return []

    async def ptz_command(self, camera_id: str, pan: float, tilt: float, zoom: float) -> None:
        raise NotImplementedError("PTZ not implemented for generic ONVIF yet")


# ── UniFi Protect provider ─────────────────────────────────────────────────────

class UniFiProvider(CameraProvider):
    """
    UniFi Protect camera backend using the uiprotect library
    (same library used by Home Assistant).
    Handles auth, WebSocket, reconnection internally.
    """

    def __init__(self, config: dict):
        self.config = config
        self._protect = None
        self._cameras: dict[str, Camera] = {}
        self._unsub = None
        self._event_buffer: list[CameraEvent] = []
        self._quality: str = config.get("stream_quality", "medium")
        self._use_motion_events: bool = config.get("use_motion_events", True)

        # Event callbacks registered by smart_events / lpr / face_auth
        self._event_callbacks: list = []

    def add_event_callback(self, cb) -> None:
        self._event_callbacks.append(cb)

    async def _auto_discover_controller(self) -> Optional[str]:
        """Try to find UniFi Protect controller on local network."""
        import asyncio
        import httpx

        # Common Protect ports
        candidates = []

        # mDNS discovery — look for _unifi-protect._tcp
        try:
            from zeroconf import Zeroconf
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

            found_urls = []

            class ProtectListener:
                def add_service(self, zc, type_, name):
                    info = zc.get_service_info(type_, name)
                    if info:
                        host = ".".join(str(b) for b in info.addresses[0].split(b".")) \
                            if hasattr(info.addresses[0], "split") else info.parsed_addresses()[0]
                        found_urls.append(f"https://{host}:{info.port}")

                def remove_service(self, *_): pass
                def update_service(self, *_): pass

            azc = AsyncZeroconf()
            browser = AsyncServiceBrowser(azc.zeroconf, "_unifi-protect._tcp.local.", ProtectListener())
            await asyncio.sleep(3)
            await azc.async_close()

            if found_urls:
                logger.info("UniFi Protect found via mDNS: %s", found_urls[0])
                return found_urls[0]

        except Exception as e:
            logger.debug("mDNS discovery error: %s", e)

        # Fallback: scan common gateway IPs
        common_hosts = ["192.168.1.1", "10.0.0.1", "192.168.0.1", "172.16.0.1"]
        async with httpx.AsyncClient(verify=False, timeout=2) as client:
            for host in common_hosts:
                for port in (7443, 443):
                    try:
                        r = await client.get(f"https://{host}:{port}/proxy/protect/api/cameras")
                        if r.status_code in (200, 401, 403):
                            url = f"https://{host}:{port}"
                            logger.info("UniFi Protect found at %s (HTTP probe)", url)
                            return url
                    except Exception:
                        pass

        return None

    async def connect(self) -> None:
        try:
            from uiprotect import ProtectApiClient
            from uiprotect.data import WSSubscriptionMessage
        except ImportError:
            raise RuntimeError(
                "uiprotect library not installed. Run: pip install uiprotect"
            )

        controller_url = self.config.get("controller_url", "").strip()
        if not controller_url:
            logger.info("UniFi Protect: auto-discovering controller...")
            controller_url = await self._auto_discover_controller()
            if not controller_url:
                raise RuntimeError("UniFi Protect controller not found on local network")

        from urllib.parse import urlparse
        parsed = urlparse(controller_url)
        host = parsed.hostname
        port = parsed.port or (7443 if "7443" in controller_url else 443)
        username = self.config.get("username", "")
        password = self.config.get("password", "")

        if not username or not password:
            raise RuntimeError("UniFi Protect requires username and password in config")

        logger.info("Connecting to UniFi Protect at %s:%d ...", host, port)
        self._protect = ProtectApiClient(
            host, port, username, password, verify_ssl=False
        )

        await self._protect.update()
        logger.info(
            "UniFi Protect connected. %d camera(s) found.",
            len(self._protect.bootstrap.cameras),
        )

        # Subscribe to WebSocket events
        def _on_ws_message(msg: WSSubscriptionMessage) -> None:
            asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.ensure_future, self._handle_ws_event(msg)
            )

        self._unsub = self._protect.subscribe_websocket(_on_ws_message)

    async def _handle_ws_event(self, msg) -> None:
        """Process incoming WebSocket event from Protect."""
        try:
            from uiprotect.data import EventType

            event = getattr(msg, "data", None)
            if event is None:
                return

            event_type = getattr(event, "type", None)
            camera_id = str(getattr(event, "camera_id", "") or "")
            event_id = str(getattr(event, "id", f"evt-{time.time()}"))

            ce = CameraEvent(
                event_id=event_id,
                camera_id=camera_id,
                event_type=str(event_type) if event_type else "unknown",
                timestamp=time.time(),
                metadata={
                    "smart_detect_types": [
                        str(t) for t in (getattr(event, "smart_detect_types", None) or [])
                    ],
                    "score": getattr(event, "score", None),
                },
            )

            self._event_buffer.append(ce)
            # Keep buffer bounded
            if len(self._event_buffer) > 5000:
                self._event_buffer = self._event_buffer[-5000:]

            for cb in self._event_callbacks:
                try:
                    await cb(ce)
                except Exception as e:
                    logger.debug("Event callback error: %s", e)

        except Exception as e:
            logger.warning("WS event handling error: %s", e)

    async def discover(self) -> list[Camera]:
        if not self._protect:
            raise RuntimeError("Not connected")

        cameras = []
        for uid, cam in self._protect.bootstrap.cameras.items():
            # Enable RTSP on camera if not already enabled
            try:
                if not cam.is_recording:
                    await cam.set_recording_mode("always")
            except Exception:
                pass

            # Get RTSP URLs by quality
            rtsp = self._get_rtsp_url(cam, self._quality)

            camera = Camera(
                camera_id=str(uid),
                name=cam.name,
                model=cam.type,
                state="connected" if cam.is_connected else "disconnected",
                provider="unifi",
                rtsp_url=rtsp,
                supports_ptz=getattr(cam, "has_ptz", False),
                extra={
                    "mac": cam.mac,
                    "ip": str(cam.host),
                    "firmware": cam.firmware_version,
                },
            )
            cameras.append(camera)
            self._cameras[str(uid)] = camera

        return cameras

    def _get_rtsp_url(self, cam, quality: str) -> str:
        """Extract RTSP URL from UniFi camera at the requested quality."""
        channels = getattr(cam, "channels", [])
        quality_map = {"high": 0, "medium": 1, "low": 2}
        idx = quality_map.get(quality, 1)

        if channels and len(channels) > idx:
            ch = channels[idx]
            rtsp_alias = getattr(ch, "rtsp_alias", None)
            host = str(cam.host)
            if rtsp_alias:
                return f"rtsps://{host}:7441/{rtsp_alias}?enableSrtp"

        # Fallback
        if channels:
            ch = channels[0]
            rtsp_alias = getattr(ch, "rtsp_alias", None)
            if rtsp_alias:
                return f"rtsps://{str(cam.host)}:7441/{rtsp_alias}?enableSrtp"

        return ""

    async def get_stream_url(self, camera_id: str, quality: str = "medium") -> str:
        cam_obj = self._protect.bootstrap.cameras.get(camera_id)
        if not cam_obj:
            raise ValueError(f"Unknown camera: {camera_id}")
        return self._get_rtsp_url(cam_obj, quality)

    async def get_snapshot(self, camera_id: str) -> bytes:
        if not self._protect:
            raise RuntimeError("Not connected")
        try:
            data = await self._protect.get_camera_snapshot(camera_id)
            return data or b""
        except Exception as e:
            logger.warning("UniFi snapshot error for %s: %s", camera_id, e)
            return b""

    async def get_events(self, camera_id: str, since: float) -> list[CameraEvent]:
        return [e for e in self._event_buffer if e.camera_id == camera_id and e.timestamp >= since]

    async def ptz_command(self, camera_id: str, pan: float, tilt: float, zoom: float) -> None:
        cam_obj = self._protect.bootstrap.cameras.get(camera_id)
        if not cam_obj:
            raise ValueError(f"Unknown camera: {camera_id}")
        if not cam_obj.has_ptz:
            raise NotImplementedError(f"Camera {camera_id} does not support PTZ")
        try:
            await cam_obj.set_ptz_position(pan=pan, tilt=tilt, zoom=zoom)
        except Exception as e:
            raise RuntimeError(f"PTZ command failed: {e}") from e

    async def disconnect(self) -> None:
        if self._unsub:
            self._unsub()
        if self._protect:
            try:
                await self._protect.async_disconnect()
            except Exception:
                pass


# ── ffmpeg helpers ─────────────────────────────────────────────────────────────

async def _ffmpeg_snapshot(rtsp_url: str, resolution: str = "640x480") -> bytes:
    """Grab a single JPEG frame from an RTSP stream using ffmpeg."""
    import subprocess
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmpfile = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-vframes", "1",
            "-s", resolution,
            "-q:v", "2",
            tmpfile,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            return b""

        if os.path.exists(tmpfile):
            with open(tmpfile, "rb") as f:
                return f.read()
    finally:
        try:
            os.unlink(tmpfile)
        except Exception:
            pass
    return b""


async def _ffmpeg_to_hls(rtsp_url: str, output_dir: Path, segment_time: int = 2) -> asyncio.subprocess.Process:
    """Transcode RTSP → HLS segments in output_dir. Returns the running process."""
    output_dir.mkdir(parents=True, exist_ok=True)
    m3u8 = output_dir / "stream.m3u8"

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-c:v", "copy",
        "-c:a", "aac",
        "-f", "hls",
        "-hls_time", str(segment_time),
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments",
        str(m3u8),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return proc


# ── Motion detection (PIL/numpy, no OpenCV) ────────────────────────────────────

class MotionDetector:
    """
    Lightweight frame-diff motion detection.
    Uses Pillow + numpy only — no OpenCV.
    Designed for Pi Zero: low resolution, low FPS.
    """

    def __init__(self, sensitivity: int = 30, cooldown_sec: int = 60):
        self.sensitivity = sensitivity          # 0–100
        self.cooldown_sec = cooldown_sec
        self._prev_frame = None
        self._last_alert: float = 0.0
        self._threshold = (100 - sensitivity) * 2.55  # map 0-100 to 0-255 diff

    def check(self, jpeg_bytes: bytes) -> bool:
        """Return True if motion detected. Updates internal state."""
        if not jpeg_bytes:
            return False

        try:
            import numpy as np
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(jpeg_bytes)).convert("L")  # grayscale
            img = img.resize((160, 120))  # tiny for speed
            frame = np.array(img, dtype=np.float32)

            if self._prev_frame is None:
                self._prev_frame = frame
                return False

            diff = np.abs(frame - self._prev_frame)
            self._prev_frame = frame

            changed_pixels = np.sum(diff > self._threshold)
            total_pixels = frame.size
            change_ratio = changed_pixels / total_pixels

            now = time.time()
            if change_ratio > 0.02 and (now - self._last_alert) > self.cooldown_sec:
                self._last_alert = now
                logger.info("Motion detected (%.1f%% pixels changed)", change_ratio * 100)
                return True

        except Exception as e:
            logger.debug("Motion detection error: %s", e)

        return False


# ── Local recording — circular buffer ─────────────────────────────────────────

class RecordingManager:
    """
    Manages local video recording with circular storage.
    Uses ffmpeg for RTSP → MP4 segment recording.
    """

    def __init__(self, config: dict):
        self.enabled: bool = config.get("enabled", True)
        self.retention_hours: int = config.get("retention_hours", 48)
        self.storage_path = Path(config.get("storage_path", "/var/lib/opencpo/recordings"))
        self.max_storage_gb: float = config.get("max_storage_gb", 16)
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def start_recording(self, camera: Camera) -> None:
        if not self.enabled:
            return

        cam_dir = self.storage_path / camera.camera_id
        cam_dir.mkdir(parents=True, exist_ok=True)

        # Segment into 10-minute files
        segment_pattern = str(cam_dir / "%Y%m%d_%H%M%S.mp4")

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", camera.rtsp_url,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", "600",
            "-segment_format", "mp4",
            "-strftime", "1",
            segment_pattern,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._processes[camera.camera_id] = proc
        logger.info("Recording started for camera %s → %s", camera.name, cam_dir)

    async def prune_old_recordings(self) -> None:
        """Delete recordings older than retention_hours and enforce max storage."""
        import os

        cutoff = time.time() - (self.retention_hours * 3600)
        total_bytes = 0

        all_files = []
        for cam_dir in self.storage_path.glob("*/"):
            for f in cam_dir.glob("*.mp4"):
                stat = f.stat()
                all_files.append((stat.st_mtime, stat.st_size, f))
                if stat.st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    logger.debug("Pruned old recording: %s", f)
                else:
                    total_bytes += stat.st_size

        # Enforce max storage
        max_bytes = self.max_storage_gb * 1024 ** 3
        if total_bytes > max_bytes:
            all_files.sort(key=lambda x: x[0])
            for mtime, size, path in all_files:
                if total_bytes <= max_bytes:
                    break
                try:
                    path.unlink(missing_ok=True)
                    total_bytes -= size
                    logger.info("Pruned for storage limit: %s", path)
                except Exception:
                    pass

    async def stop_recording(self, camera_id: str) -> None:
        proc = self._processes.pop(camera_id, None)
        if proc:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()


# ── CCTV Manager ───────────────────────────────────────────────────────────────

class CCTVManager:
    """
    Orchestrates camera providers, recording, motion detection,
    and exposes the API surface consumed by troubleshoot.py / FastAPI.
    """

    def __init__(self, config: dict, tailscale_ip: str = ""):
        self.config = config
        self.enabled: bool = config.get("enabled", False)
        self.tailscale_ip = tailscale_ip

        if not self.enabled:
            logger.info("CCTV subsystem disabled (set cctv.enabled: true to enable)")
            self._provider: Optional[CameraProvider] = None
            self._cameras: dict[str, Camera] = {}
            self.recorder = None
            self.motion = None
            return

        # Select provider
        provider_name = config.get("provider", "auto")
        unifi_cfg = config.get("unifi", {})
        self._provider = self._build_provider(provider_name, unifi_cfg)

        rec_cfg = config.get("recording", {})
        self.recorder = RecordingManager(rec_cfg)

        motion_cfg = config.get("motion", {})
        self.motion = MotionDetector(
            sensitivity=motion_cfg.get("sensitivity", 30),
            cooldown_sec=motion_cfg.get("cooldown_sec", 60),
        )

        self._cameras: dict[str, Camera] = {}
        self._alert_callbacks: list = []
        self._discovery_interval: int = 300

        # Resolution/fps for proxy streams
        stream_cfg = config.get("streams", {})
        self.stream_resolution: str = stream_cfg.get("resolution", "640x480")
        self.stream_fps: int = stream_cfg.get("max_fps", 15)
        self.stream_format: str = stream_cfg.get("format", "mjpeg")

        # HLS output directory (Tailscale-only served)
        self._hls_dir = Path("/var/lib/opencpo/hls")
        self._hls_procs: dict[str, asyncio.subprocess.Process] = {}

    def _build_provider(self, name: str, unifi_cfg: dict) -> CameraProvider:
        if name == "unifi":
            return UniFiProvider(unifi_cfg)
        elif name == "onvif":
            return ONVIFProvider({})
        else:  # "auto" — try UniFi first
            if unifi_cfg.get("username"):
                logger.info("CCTV provider: auto → UniFi Protect")
                return UniFiProvider(unifi_cfg)
            else:
                logger.info("CCTV provider: auto → ONVIF (no UniFi credentials)")
                return ONVIFProvider({})

    def add_alert_callback(self, cb) -> None:
        self._alert_callbacks.append(cb)

    def register_event_callback(self, cb) -> None:
        """Register a callback for camera events (used by smart_events.py)."""
        if isinstance(self._provider, UniFiProvider):
            self._provider.add_event_callback(cb)

    async def run(self) -> None:
        if not self.enabled or not self._provider:
            return

        try:
            await self._provider.connect()
            cameras = await self._provider.discover()
            for cam in cameras:
                self._cameras[cam.camera_id] = cam
                logger.info("Camera: %s (%s) — %s", cam.name, cam.model, cam.state)

            # Start recording if enabled
            if self.recorder and self.recorder.enabled:
                for cam in cameras:
                    await self.recorder.start_recording(cam)

            # Periodic tasks
            await asyncio.gather(
                self._discovery_loop(),
                self._pruning_loop(),
            )

        except Exception as e:
            logger.error("CCTV manager error: %s", e)

    async def _discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self._discovery_interval)
            try:
                cameras = await self._provider.discover()
                for cam in cameras:
                    if cam.camera_id not in self._cameras:
                        logger.info("New camera discovered: %s", cam.name)
                        self._cameras[cam.camera_id] = cam
                        if self.recorder and self.recorder.enabled:
                            await self.recorder.start_recording(cam)
            except Exception as e:
                logger.warning("CCTV re-discovery error: %s", e)

    async def _pruning_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)  # hourly
            if self.recorder:
                await self.recorder.prune_old_recordings()

    # ── API helpers ────────────────────────────────────────────────────────────

    def list_cameras(self) -> list[dict]:
        return [
            {
                "camera_id": c.camera_id,
                "name": c.name,
                "model": c.model,
                "state": c.state,
                "provider": c.provider,
                "supports_ptz": c.supports_ptz,
                "has_stream": bool(c.rtsp_url),
                **c.extra,
            }
            for c in self._cameras.values()
        ]

    async def snapshot(self, camera_id: str) -> bytes:
        if not self._provider:
            return b""
        return await self._provider.get_snapshot(camera_id)

    async def stream_url(self, camera_id: str, quality: str = "medium") -> str:
        if not self._provider:
            return ""
        return await self._provider.get_stream_url(camera_id, quality)

    async def ptz(self, camera_id: str, pan: float, tilt: float, zoom: float) -> None:
        if not self._provider:
            raise RuntimeError("CCTV not enabled")
        await self._provider.ptz_command(camera_id, pan, tilt, zoom)

    async def events(self, camera_id: str, since: float) -> list[dict]:
        if not self._provider:
            return []
        evts = await self._provider.get_events(camera_id, since)
        return [
            {
                "event_id": e.event_id,
                "camera_id": e.camera_id,
                "type": e.event_type,
                "timestamp": e.timestamp,
                "metadata": e.metadata,
            }
            for e in evts
        ]
