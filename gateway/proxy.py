"""
OpenCPO Gateway — OCPP WebSocket Proxy

Listens on the local network for OCPP charger connections and forwards
them to the upstream OpenCPO Core via a mTLS-authenticated WebSocket
over the Tailscale tunnel.

Ports:
  0.0.0.0:9100  — OCPP 1.6
  0.0.0.0:9201  — OCPP 2.0.1

Features:
  - Transparent bidirectional WebSocket proxy
  - mTLS client cert from keyvault
  - Reconnection with exponential backoff (cap: 60s)
  - All frames logged to shared ring buffer (tap.py reads it)
  - Charger identity extracted from WebSocket path (/ocpp/<charger_id>)
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import websockets
import websockets.exceptions

from gateway.config import GatewayConfig

logger = logging.getLogger(__name__)

# Shared ring buffer consumed by tap.py
# deque is thread-safe for single-producer append
_message_buffer: deque = deque(maxlen=10_000)


@dataclass
class ProxiedMessage:
    charger_id: str
    direction: str      # "up" (charger→core) | "down" (core→charger)
    action: str         # extracted OCPP action if parseable
    payload: str
    timestamp: float = field(default_factory=time.time)


def get_message_buffer() -> deque:
    return _message_buffer


def _extract_action(payload: str) -> str:
    """Best-effort extract of OCPP action name from JSON frame."""
    try:
        import json
        msg = json.loads(payload)
        if isinstance(msg, list) and len(msg) >= 3:
            return str(msg[2]) if len(msg) == 4 else ""
    except Exception:
        pass
    return ""


def _extract_charger_id(path: str) -> str:
    """Extract charger ID from WebSocket path: /ocpp/CHARGER-001 → CHARGER-001"""
    parts = [p for p in path.strip("/").split("/") if p]
    return parts[-1] if parts else "unknown"


def _build_ssl_context(cert_path: str, key_path: str, ca_path: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if ca_path:
        ctx.load_verify_locations(ca_path)
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if cert_path and key_path:
        ctx.load_cert_chain(cert_path, key_path)
    return ctx


class OCPPProxyConnection:
    """
    Handles a single charger ↔ core proxy session.
    Lives as long as the charger WebSocket is open.
    """

    def __init__(
        self,
        charger_ws,
        charger_id: str,
        core_url: str,
        ssl_ctx: Optional[ssl.SSLContext],
        ocpp_version: str,
    ):
        self.charger_ws = charger_ws
        self.charger_id = charger_id
        self.core_url = core_url
        self.ssl_ctx = ssl_ctx
        self.ocpp_version = ocpp_version

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                upstream_url = f"{self.core_url}/{self.charger_id}"
                logger.info(
                    "[%s] Connecting to core: %s", self.charger_id, upstream_url
                )
                async with websockets.connect(
                    upstream_url,
                    ssl=self.ssl_ctx,
                    subprotocols=[f"ocpp{self.ocpp_version.replace('.', '')}"],
                    ping_interval=30,
                    ping_timeout=10,
                ) as core_ws:
                    logger.info("[%s] Upstream connected", self.charger_id)
                    backoff = 1.0  # reset on success

                    await asyncio.gather(
                        self._pipe(self.charger_ws, core_ws, direction="up"),
                        self._pipe(core_ws, self.charger_ws, direction="down"),
                    )

            except websockets.exceptions.ConnectionClosed:
                logger.info("[%s] Upstream connection closed", self.charger_id)
                break
            except Exception as e:
                logger.warning("[%s] Upstream error: %s — retrying in %.1fs", self.charger_id, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _pipe(self, src, dst, direction: str) -> None:
        try:
            async for message in src:
                payload = message if isinstance(message, str) else message.decode()

                _message_buffer.append(ProxiedMessage(
                    charger_id=self.charger_id,
                    direction=direction,
                    action=_extract_action(payload),
                    payload=payload,
                ))

                await dst.send(message)

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.warning("[%s] Pipe error (%s): %s", self.charger_id, direction, e)


class OCPPProxy:
    """Manages listener servers for OCPP 1.6 and 2.0.1."""

    def __init__(self, config: GatewayConfig, cert_path: str = "", key_path: str = "", ca_path: str = ""):
        self.config = config
        self.ssl_ctx = _build_ssl_context(cert_path, key_path, ca_path) if cert_path else None
        self._active: dict[str, asyncio.Task] = {}

    async def _handle_charger(self, websocket, path: str, ocpp_version: str) -> None:
        charger_id = _extract_charger_id(path)
        logger.info("Charger connected: %s (OCPP %s)", charger_id, ocpp_version)

        core_base = self.config.core_api_base
        conn = OCPPProxyConnection(
            charger_ws=websocket,
            charger_id=charger_id,
            core_url=f"{core_base.replace('http', 'ws')}/ocpp/{ocpp_version}",
            ssl_ctx=self.ssl_ctx,
            ocpp_version=ocpp_version,
        )

        task = asyncio.current_task()
        self._active[charger_id] = task
        try:
            await conn.run()
        finally:
            self._active.pop(charger_id, None)
            logger.info("Charger disconnected: %s", charger_id)

    async def run(self) -> None:
        p16 = self.config.proxy_ports.ocpp16
        p201 = self.config.proxy_ports.ocpp201

        server16 = await websockets.serve(
            lambda ws, path: self._handle_charger(ws, path, "1.6"),
            "0.0.0.0", p16,
            subprotocols=["ocpp1.6"],
        )
        server201 = await websockets.serve(
            lambda ws, path: self._handle_charger(ws, path, "2.0.1"),
            "0.0.0.0", p201,
            subprotocols=["ocpp2.0.1", "ocpp2.0"],
        )

        logger.info("OCPP proxy listening: 0.0.0.0:%d (1.6), 0.0.0.0:%d (2.0.1)", p16, p201)
        await asyncio.gather(server16.wait_closed(), server201.wait_closed())

    def active_chargers(self) -> list[str]:
        return list(self._active.keys())
