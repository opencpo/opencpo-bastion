"""
OpenCPO Bastion — Auto-Update

Checks Core API for software updates on a configurable schedule.
Downloads, verifies (SHA256), applies, and rolls back if the new
version fails the health check.

Default schedule: 03:00 local time daily (driven by systemd timer).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from gateway.config import GatewayConfig

logger = logging.getLogger(__name__)

INSTALL_DIR = Path("/opt/opencpo-bastion")
BACKUP_DIR = Path("/opt/opencpo-bastion-prev")
VERSION_FILE = INSTALL_DIR / "VERSION"


def _current_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except Exception:
        return "unknown"


class Updater:
    def __init__(self, config: GatewayConfig):
        self.config = config
        self.enabled = config.auto_update
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Auto-update disabled")
            return
        self._http = httpx.AsyncClient(timeout=60)
        logger.info("Auto-updater started (schedule: %s daily)", self.config.update_time)
        await self._schedule_loop()

    async def _schedule_loop(self) -> None:
        while True:
            await self._wait_until_update_time()
            await self.check_and_update()

    async def _wait_until_update_time(self) -> None:
        import datetime
        h, m = map(int, self.config.update_time.split(":"))
        while True:
            now = datetime.datetime.now()
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target = target + datetime.timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.debug("Next update check in %.0f seconds", wait_seconds)
            await asyncio.sleep(wait_seconds)

    async def check_and_update(self) -> None:
        current = _current_version()
        logger.info("Checking for updates (current: %s)", current)

        try:
            r = await self._http.get(
                f"{self.config.core_api_base}/api/v1/gateway/version",
                params={"current": current},
                timeout=15,
            )
            r.raise_for_status()
            info = r.json()

        except Exception as e:
            logger.warning("Update check failed: %s", e)
            return

        latest = info.get("version", "")
        if not info.get("update_available") or not latest:
            logger.info("No update available (latest: %s)", latest or current)
            return

        download_url = info.get("download_url", "")
        checksum = info.get("sha256", "")

        if not download_url:
            logger.warning("Update available but no download_url provided")
            return

        logger.info("Update available: %s → %s", current, latest)
        await self._apply_update(latest, download_url, checksum)

    async def _apply_update(self, version: str, url: str, expected_sha256: str) -> None:
        logger.info("Downloading update %s from %s", version, url)

        # Download
        try:
            r = await self._http.get(url)
            r.raise_for_status()
            package = r.content
        except Exception as e:
            logger.error("Download failed: %s", e)
            return

        # Verify checksum
        if expected_sha256:
            actual = hashlib.sha256(package).hexdigest()
            if actual != expected_sha256:
                logger.error(
                    "Checksum mismatch! expected=%s actual=%s — aborting",
                    expected_sha256, actual,
                )
                return
            logger.info("Checksum verified ✓")

        # Backup current version
        try:
            if BACKUP_DIR.exists():
                subprocess.run(["rm", "-rf", str(BACKUP_DIR)], check=True)
            subprocess.run(["cp", "-a", str(INSTALL_DIR), str(BACKUP_DIR)], check=True)
            logger.info("Backed up current version to %s", BACKUP_DIR)
        except Exception as e:
            logger.error("Backup failed: %s — aborting update", e)
            return

        # Extract and install
        try:
            import tarfile
            import io
            with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as tar:
                tar.extractall(INSTALL_DIR)
            VERSION_FILE.write_text(version)
            logger.info("Update extracted to %s", INSTALL_DIR)
        except Exception as e:
            logger.error("Extraction failed: %s — rolling back", e)
            await self._rollback()
            return

        # Restart services
        logger.info("Restarting services...")
        try:
            subprocess.run(
                ["systemctl", "restart",
                 "opencpo-proxy", "opencpo-monitor",
                 "opencpo-tap", "opencpo-troubleshoot",
                 "opencpo-sensors", "opencpo-cctv"],
                check=True, timeout=30,
            )
        except Exception as e:
            logger.error("Service restart failed: %s — rolling back", e)
            await self._rollback()
            return

        # Health check after restart
        await asyncio.sleep(10)
        if not await self._health_check():
            logger.error("Health check failed after update — rolling back")
            await self._rollback()
        else:
            logger.info("Update to %s applied successfully ✓", version)

    async def _health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"http://localhost:{self._troubleshoot_port()}/diag/system")
                return r.status_code == 200
        except Exception:
            return False

    def _troubleshoot_port(self) -> int:
        return self.config.troubleshoot_port

    async def _rollback(self) -> None:
        logger.warning("Rolling back to previous version...")
        try:
            subprocess.run(["cp", "-a", str(BACKUP_DIR) + "/.", str(INSTALL_DIR)], check=True)
            subprocess.run(
                ["systemctl", "restart",
                 "opencpo-proxy", "opencpo-monitor",
                 "opencpo-tap", "opencpo-troubleshoot"],
                timeout=30,
            )
            logger.info("Rollback complete")
        except Exception as e:
            logger.error("Rollback failed: %s — manual intervention required", e)
