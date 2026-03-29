"""Tests for gateway/sensors.py — mocked hardware"""

import pytest
from unittest.mock import MagicMock, patch
from gateway.sensors import SensorAlerts, SensorManager, SensorReading


def test_alert_temperature():
    alerts = SensorAlerts(temperature_max=55.0)
    manager = SensorManager({}, alerts)

    # Fake a temperature plugin
    plugin = MagicMock()
    plugin.sensor_id = "bme280_temp"

    alert, reason = manager._check_alerts(plugin, 60.0)
    assert alert is True
    assert "Temperature" in reason


def test_no_alert_temperature_below_threshold():
    alerts = SensorAlerts(temperature_max=55.0)
    manager = SensorManager({}, alerts)

    plugin = MagicMock()
    plugin.sensor_id = "bme280_temp"

    alert, reason = manager._check_alerts(plugin, 40.0)
    assert alert is False


def test_alert_flood():
    alerts = SensorAlerts(flood=True)
    manager = SensorManager({}, alerts)

    plugin = MagicMock()
    plugin.sensor_id = "flood"

    alert, reason = manager._check_alerts(plugin, 1.0)
    assert alert is True
    assert "Flood" in reason or "flood" in reason.lower()


def test_alert_tamper():
    alerts = SensorAlerts(tamper=True)
    manager = SensorManager({}, alerts)

    plugin = MagicMock()
    plugin.sensor_id = "tamper"

    alert, reason = manager._check_alerts(plugin, 1.0)
    assert alert is True


def test_no_alert_tamper_when_closed():
    alerts = SensorAlerts(tamper=True)
    manager = SensorManager({}, alerts)

    plugin = MagicMock()
    plugin.sensor_id = "tamper"

    alert, reason = manager._check_alerts(plugin, 0.0)
    assert alert is False


def test_alert_sound_level():
    alerts = SensorAlerts(sound_db_max=85.0)
    manager = SensorManager({}, alerts)

    plugin = MagicMock()
    plugin.sensor_id = "sound_db"

    alert, reason = manager._check_alerts(plugin, 90.0)
    assert alert is True


def test_get_all_readings_empty():
    manager = SensorManager({}, SensorAlerts())
    readings = manager.get_all_readings()
    assert isinstance(readings, list)
    # Empty before detect_all
    assert readings == []
