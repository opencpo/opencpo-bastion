"""
OpenCPO Bastion — Remote Troubleshooting API

FastAPI endpoints for remote diagnostics. Binds to Tailscale IP only.

GET /diag/network    — Tailscale status, ping to Core, local subnet scan
GET /diag/chargers   — connected chargers, last message times, state
GET /diag/speedtest  — download/upload speed through tunnel
GET /diag/capture    — trigger 60s tcpdump, return pcap file
GET /diag/journal    — last N lines of journald filtered by service
GET /diag/system     — CPU, memory, temp, disk, uptime, Pi model, firmware
GET /diag/sensors    — sensor hardware scan results, I2C addresses found
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="OpenCPO Diagnostics", docs_url=None, redoc_url=None)

# Injected at startup by main.py
_proxy = None
_keyvault = None
_sensor_manager = None
_config = None


def init(proxy, keyvault, sensor_manager, config) -> None:
    global _proxy, _keyvault, _sensor_manager, _config
    _proxy = proxy
    _keyvault = keyvault
    _sensor_manager = sensor_manager
    _config = config


# ── /diag/system ──────────────────────────────────────────────────────────────

def _pi_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00 ")
    except Exception:
        return "unknown"


def _uptime_seconds() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return 0.0


def _firmware_version() -> str:
    try:
        r = subprocess.run(["vcgencmd", "version"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return "unknown"


@app.get("/diag/system")
async def diag_system():
    from gateway.monitor import _read_pi_temp, _disk_usage_pct, _memory_usage_pct

    temp = _read_pi_temp()
    disk = _disk_usage_pct("/")
    mem_used, mem_total, mem_pct = _memory_usage_pct()

    # CPU usage (1s sample)
    try:
        with open("/proc/stat") as f:
            cpu_line = f.readline()
        fields = list(map(int, cpu_line.split()[1:]))
        idle = fields[3]
        total = sum(fields)
        await asyncio.sleep(1)
        with open("/proc/stat") as f:
            cpu_line2 = f.readline()
        fields2 = list(map(int, cpu_line2.split()[1:]))
        idle2 = fields2[3]
        total2 = sum(fields2)
        cpu_pct = round((1 - (idle2 - idle) / (total2 - total)) * 100, 1)
    except Exception:
        cpu_pct = 0.0

    cert_info = _keyvault.cert_info() if _keyvault else {}

    return JSONResponse({
        "pi_model": _pi_model(),
        "firmware": _firmware_version(),
        "uptime_seconds": _uptime_seconds(),
        "cpu_pct": cpu_pct,
        "cpu_temp_c": round(temp, 1),
        "memory": {"used_mb": mem_used, "total_mb": mem_total, "pct": mem_pct},
        "disk": {"pct": round(disk, 1)},
        "certificate": cert_info,
        "timestamp": time.time(),
    })


# ── /diag/network ─────────────────────────────────────────────────────────────

@app.get("/diag/network")
async def diag_network():
    from gateway.monitor import _tailscale_status

    ts = _tailscale_status()
    core_url = _config.core_api_base if _config else ""

    # Ping core
    core_reachable = False
    core_latency_ms = None
    if core_url:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                t0 = time.monotonic()
                r = await client.get(f"{core_url}/health")
                core_latency_ms = round((time.monotonic() - t0) * 1000, 1)
                core_reachable = r.status_code < 500
        except Exception:
            pass

    # Local interfaces
    try:
        ip_r = subprocess.run(["ip", "addr"], capture_output=True, text=True, timeout=3)
        interfaces = ip_r.stdout
    except Exception:
        interfaces = "unavailable"

    return JSONResponse({
        "tailscale": ts,
        "core_url": core_url,
        "core_reachable": core_reachable,
        "core_latency_ms": core_latency_ms,
        "interfaces": interfaces,
        "timestamp": time.time(),
    })


# ── /diag/chargers ────────────────────────────────────────────────────────────

@app.get("/diag/chargers")
async def diag_chargers():
    from gateway.proxy import get_message_buffer

    active = _proxy.active_chargers() if _proxy else []

    # Last message time per charger
    buf = list(get_message_buffer())
    last_msg: dict[str, float] = {}
    for m in buf:
        if m.charger_id not in last_msg or m.timestamp > last_msg[m.charger_id]:
            last_msg[m.charger_id] = m.timestamp

    chargers = []
    for cid in active:
        chargers.append({
            "charger_id": cid,
            "state": "connected",
            "last_message_ts": last_msg.get(cid),
            "last_message_age_s": round(time.time() - last_msg[cid], 1) if cid in last_msg else None,
        })

    # Also include chargers seen in buffer but no longer active
    for cid, ts in last_msg.items():
        if cid not in active:
            chargers.append({
                "charger_id": cid,
                "state": "disconnected",
                "last_message_ts": ts,
                "last_message_age_s": round(time.time() - ts, 1),
            })

    return JSONResponse({"chargers": chargers, "active_count": len(active)})


# ── /diag/sensors ─────────────────────────────────────────────────────────────

@app.get("/diag/sensors")
async def diag_sensors():
    if _sensor_manager is None:
        return JSONResponse({"error": "Sensor subsystem not initialized"}, status_code=503)

    scan = _sensor_manager.get_scan_results()
    return JSONResponse({
        **scan,
        "timestamp": time.time(),
    })


# ── /diag/speedtest ───────────────────────────────────────────────────────────

@app.get("/diag/speedtest")
async def diag_speedtest():
    """Download/upload speed through Tailscale tunnel to Core."""
    core_url = _config.core_api_base if _config else ""
    if not core_url:
        return JSONResponse({"error": "core_api_url not configured"}, status_code=503)

    results = {}

    # Download test: fetch 1MB test blob from Core
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            t0 = time.monotonic()
            r = await client.get(f"{core_url}/api/v1/gateway/speedtest/download?size=1048576")
            elapsed = time.monotonic() - t0
            if r.status_code == 200:
                size_bytes = len(r.content)
                results["download_mbps"] = round(size_bytes / elapsed / 1024 / 1024, 2)
                results["download_ms"] = round(elapsed * 1000, 1)
    except Exception as e:
        results["download_error"] = str(e)

    # Upload test: POST 512KB to Core
    try:
        payload = os.urandom(512 * 1024)
        async with httpx.AsyncClient(timeout=30) as client:
            t0 = time.monotonic()
            r = await client.post(
                f"{core_url}/api/v1/gateway/speedtest/upload",
                content=payload,
                headers={"Content-Type": "application/octet-stream"},
            )
            elapsed = time.monotonic() - t0
            if r.status_code in (200, 204):
                results["upload_mbps"] = round(len(payload) / elapsed / 1024 / 1024, 2)
                results["upload_ms"] = round(elapsed * 1000, 1)
    except Exception as e:
        results["upload_error"] = str(e)

    results["timestamp"] = time.time()
    return JSONResponse(results)


# ── /diag/capture ─────────────────────────────────────────────────────────────

@app.get("/diag/capture")
async def diag_capture(
    duration: int = Query(default=10, le=60),
    interface: str = Query(default="eth0"),
):
    """Capture network traffic and return pcap file."""
    with tempfile.NamedTemporaryFile(suffix=".pcap", delete=False) as f:
        tmpfile = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "tcpdump", "-i", interface, "-w", tmpfile,
            "-s", "0", "not", "port", "22",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(duration)
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)

        return FileResponse(
            tmpfile,
            media_type="application/vnd.tcpdump.pcap",
            filename=f"capture-{int(time.time())}.pcap",
            background=None,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── /diag/journal ─────────────────────────────────────────────────────────────

@app.get("/diag/journal")
async def diag_journal(
    service: str = Query(default="opencpo-proxy"),
    lines: int = Query(default=100, le=1000),
):
    try:
        r = subprocess.run(
            ["journalctl", f"-u{service}", f"-n{lines}", "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        return JSONResponse({
            "service": service,
            "lines": r.stdout.splitlines(),
            "returncode": r.returncode,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
