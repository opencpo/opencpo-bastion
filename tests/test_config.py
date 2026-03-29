"""Tests for gateway/config.py"""

import os
import pytest
from unittest.mock import patch


def _make_config(**overrides):
    from gateway.config import GatewayConfig
    cfg = GatewayConfig(
        tailscale_auth_key="tskey-test",
        core_api_url="https://core.example.com",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_valid_config_passes_validation():
    cfg = _make_config()
    errors = cfg.validate()
    assert errors == []


def test_missing_tailscale_key():
    cfg = _make_config(tailscale_auth_key="")
    errors = cfg.validate()
    assert any("tailscale_auth_key" in e for e in errors)


def test_missing_core_url():
    cfg = _make_config(core_api_url="")
    errors = cfg.validate()
    assert any("core_api_url" in e for e in errors)


def test_invalid_core_url():
    cfg = _make_config(core_api_url="ftp://bad")
    errors = cfg.validate()
    assert any("core_api_url" in e for e in errors)


def test_invalid_log_level():
    cfg = _make_config(log_level="verbose")
    errors = cfg.validate()
    assert any("log_level" in e for e in errors)


def test_core_api_base_strips_slash():
    cfg = _make_config(core_api_url="https://core.example.com/")
    assert cfg.core_api_base == "https://core.example.com"


def test_env_override(monkeypatch):
    monkeypatch.setenv("OPENCPO_LOG_LEVEL", "debug")
    monkeypatch.setenv("OPENCPO_METRICS_PORT", "9999")

    from gateway.config import GatewayConfig, _apply_env_overrides
    cfg = GatewayConfig(tailscale_auth_key="k", core_api_url="https://x.com")
    _apply_env_overrides(cfg)

    assert cfg.log_level == "debug"
    assert cfg.metrics_port == 9999
