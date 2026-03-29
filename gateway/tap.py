"""
OpenCPO Gateway — OCPP Message Tap

Reads from the shared ring buffer populated by proxy.py and exposes:
  - SSE stream at /tap — live OCPP message stream
  - Query endpoint: filter by charger_id, time range, action type
  - Export endpoint: download JSON

Ring buffer holds last 10,000 messages (configurable).
Binds to Tailscale IP only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.proxy import get_message_buffer, ProxiedMessage

logger = logging.getLogger(__name__)

app = FastAPI(title="OpenCPO Tap", docs_url=None, redoc_url=None)


def _msg_to_dict(m: ProxiedMessage) -> dict:
    return {
        "charger_id": m.charger_id,
        "direction": m.direction,
        "action": m.action,
        "payload": m.payload,
        "timestamp": m.timestamp,
    }


@app.get("/tap")
async def tap_stream(
    charger_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
):
    """Server-Sent Events stream of live OCPP messages."""
    buf = get_message_buffer()

    async def event_gen() -> AsyncGenerator[bytes, None]:
        last_len = len(buf)
        while True:
            current = list(buf)
            new_msgs = current[last_len:] if last_len < len(current) else []
            last_len = len(current)

            # Also catch wrap-around: if buffer wrapped, just stream everything new
            if last_len > len(current):
                new_msgs = current
                last_len = len(current)

            for msg in new_msgs:
                if charger_id and msg.charger_id != charger_id:
                    continue
                if action and msg.action != action:
                    continue
                data = json.dumps(_msg_to_dict(msg))
                yield f"data: {data}\n\n".encode()

            await asyncio.sleep(0.1)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/tap/query")
async def tap_query(
    charger_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    since: Optional[float] = Query(None),
    until: Optional[float] = Query(None),
    limit: int = Query(default=200, le=10_000),
):
    """Query the in-memory message ring buffer with filters."""
    messages = list(get_message_buffer())

    if charger_id:
        messages = [m for m in messages if m.charger_id == charger_id]
    if action:
        messages = [m for m in messages if m.action == action]
    if since:
        messages = [m for m in messages if m.timestamp >= since]
    if until:
        messages = [m for m in messages if m.timestamp <= until]

    messages = messages[-limit:]
    return JSONResponse([_msg_to_dict(m) for m in messages])


@app.get("/tap/export")
async def tap_export(
    charger_id: Optional[str] = Query(None),
    since: Optional[float] = Query(None),
):
    """Download buffered messages as newline-delimited JSON."""
    messages = list(get_message_buffer())

    if charger_id:
        messages = [m for m in messages if m.charger_id == charger_id]
    if since:
        messages = [m for m in messages if m.timestamp >= since]

    lines = "\n".join(json.dumps(_msg_to_dict(m)) for m in messages)
    filename = f"ocpp-tap-{int(time.time())}.ndjson"

    return StreamingResponse(
        iter([lines.encode()]),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/tap/chargers")
async def tap_chargers():
    """List charger IDs seen in the buffer."""
    ids = sorted({m.charger_id for m in get_message_buffer()})
    return JSONResponse(ids)
