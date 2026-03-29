"""
OpenCPO Gateway — Configuration Management

Loads configuration from /boot/opencpo.yaml (dropped on SD card by user).
Supports environment variable overrides for all settings.
Validates on startup with clear error messages.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Config file locations (in priority order)
CONFIG_PATHS = [
    Path("/boot/opencpo.yaml"),
    Path("/boot/firmware/opencpo.yaml"),  # Ubuntu on Pi
    Path("opencpo.yaml"),                  # Dev/local
]


@dataclass
class ProxyPorts:
    ocpp16: int = 9100
    ocpp201: int = 9201


@dataclass
class GatewayConfig:
    # Required
    tailscale_auth_key: str = ""
    core_api_url: str = ""

    # Network
    proxy_ports: ProxyPorts = field(default_factory=ProxyPorts)
    tailscale_hostname: str = ""  # auto-generated if empty

    # Logging
    log_level: str = "info"

    # Metrics
    metrics_port: int = 9090

    # Tap
    tap_port: int = 8085
    tap_buffer_size: int = 10_000

    # Troubleshoot API
    troubleshoot_port: int = 8086

    # Auto-update
    auto_update: bool = True
    update_time: str = "03:00"  # local time HH:MM

    # PKI / cert renewal
    cert_renew_days_before: int = 30

    # Discovery
    discovery_interval_seconds: int = 300  # 5 min

    # Heartbeat
    heartbeat_interval_seconds: int = 60

    @property
    def core_api_base(self) -> str:
        return self.core_api_url.rstrip("/")

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = valid)."""
        errors = []

        if not self.tailscale_auth_key:
            errors.append(
                "Missing 'tailscale_auth_key'. "
                "Get one from https://login.tailscale.com/admin/settings/keys"
            )

        if not self.core_api_url:
            errors.append(
                "Missing 'core_api_url'. "
                "Set this to your OpenCPO Core URL, e.g. https://core.example.com"
            )
        elif not self.core_api_url.startswith(("http://", "https://")):
            errors.append(
                f"Invalid 'core_api_url': {self.core_api_url!r}. "
                "Must start with http:// or https://"
            )

        if not (1024 <= self.proxy_ports.ocpp16 <= 65535):
            errors.append(f"Invalid proxy_ports.ocpp16: {self.proxy_ports.ocpp16}")

        if not (1024 <= self.proxy_ports.ocpp201 <= 65535):
            errors.append(f"Invalid proxy_ports.ocpp201: {self.proxy_ports.ocpp201}")

        if self.log_level.lower() not in ("debug", "info", "warning", "error", "critical"):
            errors.append(f"Invalid log_level: {self.log_level!r}")

        return errors


def _find_config_file() -> Optional[Path]:
    """Find the first existing config file from the priority list."""
    for path in CONFIG_PATHS:
        if path.exists():
            logger.debug("Found config at %s", path)
            return path
    return None


def _apply_env_overrides(config: GatewayConfig) -> None:
    """Apply OPENCPO_* environment variable overrides."""
    overrides = {
        "OPENCPO_TAILSCALE_AUTH_KEY": ("tailscale_auth_key", str),
        "OPENCPO_CORE_API_URL": ("core_api_url", str),
        "OPENCPO_LOG_LEVEL": ("log_level", str),
        "OPENCPO_METRICS_PORT": ("metrics_port", int),
        "OPENCPO_TAP_PORT": ("tap_port", int),
        "OPENCPO_TAP_BUFFER_SIZE": ("tap_buffer_size", int),
        "OPENCPO_TROUBLESHOOT_PORT": ("troubleshoot_port", int),
        "OPENCPO_AUTO_UPDATE": ("auto_update", lambda v: v.lower() in ("1", "true", "yes")),
        "OPENCPO_UPDATE_TIME": ("update_time", str),
        "OPENCPO_HEARTBEAT_INTERVAL": ("heartbeat_interval_seconds", int),
        "OPENCPO_DISCOVERY_INTERVAL": ("discovery_interval_seconds", int),
    }

    for env_key, (attr, cast) in overrides.items():
        value = os.environ.get(env_key)
        if value is not None:
            try:
                setattr(config, attr, cast(value))
                logger.debug("Config override from env: %s=%r", env_key, value)
            except (ValueError, TypeError) as e:
                logger.warning("Invalid env override %s=%r: %s", env_key, value, e)

    # Port overrides
    ocpp16 = os.environ.get("OPENCPO_PORT_OCPP16")
    if ocpp16:
        config.proxy_ports.ocpp16 = int(ocpp16)

    ocpp201 = os.environ.get("OPENCPO_PORT_OCPP201")
    if ocpp201:
        config.proxy_ports.ocpp201 = int(ocpp201)


def load_config() -> GatewayConfig:
    """
    Load configuration from file + env overrides.
    Raises SystemExit with clear message if required fields are missing.
    """
    config = GatewayConfig()

    config_path = _find_config_file()
    if config_path is None:
        logger.warning(
            "No config file found. Checked: %s",
            ", ".join(str(p) for p in CONFIG_PATHS),
        )
        logger.warning("Falling back to environment variables only.")
    else:
        try:
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}

            # Required fields
            config.tailscale_auth_key = raw.get("tailscale_auth_key", "")
            config.core_api_url = raw.get("core_api_url", "")

            # Optional fields
            config.log_level = raw.get("log_level", config.log_level)
            config.metrics_port = raw.get("metrics_port", config.metrics_port)
            config.tap_port = raw.get("tap_port", config.tap_port)
            config.tap_buffer_size = raw.get("tap_buffer_size", config.tap_buffer_size)
            config.troubleshoot_port = raw.get("troubleshoot_port", config.troubleshoot_port)
            config.auto_update = raw.get("auto_update", config.auto_update)
            config.update_time = raw.get("update_time", config.update_time)
            config.tailscale_hostname = raw.get("tailscale_hostname", config.tailscale_hostname)
            config.heartbeat_interval_seconds = raw.get(
                "heartbeat_interval_seconds", config.heartbeat_interval_seconds
            )
            config.discovery_interval_seconds = raw.get(
                "discovery_interval_seconds", config.discovery_interval_seconds
            )

            # Proxy ports (nested)
            if "proxy_ports" in raw and isinstance(raw["proxy_ports"], dict):
                ports = raw["proxy_ports"]
                config.proxy_ports.ocpp16 = ports.get("ocpp16", config.proxy_ports.ocpp16)
                config.proxy_ports.ocpp201 = ports.get("ocpp201", config.proxy_ports.ocpp201)

            logger.info("Loaded config from %s", config_path)

        except yaml.YAMLError as e:
            logger.error("Failed to parse config file %s: %s", config_path, e)
            raise SystemExit(1) from e

    # Apply environment variable overrides
    _apply_env_overrides(config)

    # Validate
    errors = config.validate()
    if errors:
        print("\n❌ OpenCPO Gateway configuration errors:\n")
        for err in errors:
            print(f"  • {err}")
        print(
            f"\nEdit {config_path or 'opencpo.yaml'} to fix these issues.\n"
            "See config/opencpo.yaml.example for reference.\n"
        )
        raise SystemExit(1)

    # Set log level
    logging.getLogger().setLevel(config.log_level.upper())

    return config
