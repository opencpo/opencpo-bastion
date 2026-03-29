"""
OpenCPO Gateway — Sensor Array

Plugin-architecture I2C/GPIO sensor framework.
Sensors are auto-detected on boot; only detected sensors are polled.

Supported:
  BME280   (I2C 0x76/0x77) — temperature, humidity, pressure
  ADS1115  (I2C 0x48)      — CT clamp (SCT-013) power measurement
  TSL2591  (I2C 0x29)      — ambient light
  Reed switch (GPIO)       — door/tamper detection
  Flood sensor (GPIO)      — water ingress detection
  SPH0645 I2S mic          — ambient sound level (dB only, no recording)

All sensors publish to Prometheus and a local 24-h ring buffer.
API: GET /sensors, GET /sensors/{id}/history
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Ring buffer: 24h at 1-min intervals = 1440 samples per sensor
HISTORY_SAMPLES = 1440
HISTORY_INTERVAL = 60  # seconds

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SensorReading:
    sensor_id: str
    name: str
    value: float
    unit: str
    timestamp: float = field(default_factory=time.time)
    alert: bool = False
    alert_reason: str = ""


@dataclass
class SensorInfo:
    sensor_id: str
    name: str
    sensor_type: str
    address: str        # I2C hex address or "gpio:<pin>"
    available: bool
    unit: str
    last_reading: Optional[SensorReading] = None
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_SAMPLES))


# ── Alert thresholds (loaded from config) ─────────────────────────────────────

@dataclass
class SensorAlerts:
    temperature_max: float = 55.0    # °C
    humidity_max: float = 85.0       # %
    power_deviation: float = 20.0    # %
    sound_db_max: float = 85.0       # dB
    flood: bool = True
    tamper: bool = True


# ── Base sensor plugin ────────────────────────────────────────────────────────

class SensorPlugin(ABC):
    sensor_id: str
    name: str
    sensor_type: str
    address: str
    unit: str

    @abstractmethod
    def detect(self) -> bool:
        """Return True if sensor is physically present."""
        ...

    @abstractmethod
    def read(self) -> float:
        """Return the primary sensor value."""
        ...

    def info_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "name": self.name,
            "type": self.sensor_type,
            "address": self.address,
            "unit": self.unit,
        }


# ── I2C helpers ───────────────────────────────────────────────────────────────

def _i2c_scan() -> list[int]:
    """Return list of detected I2C addresses (0x03–0x77)."""
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        found = []
        for addr in range(0x03, 0x78):
            try:
                bus.read_byte(addr)
                found.append(addr)
            except OSError:
                pass
        bus.close()
        logger.debug("I2C scan found addresses: %s", [hex(a) for a in found])
        return found
    except Exception as e:
        logger.warning("I2C scan failed (hardware not available?): %s", e)
        return []


# ── BME280 — Temperature / Humidity / Pressure ───────────────────────────────

class BME280Plugin(SensorPlugin):
    """Bosch BME280 environmental sensor."""

    ADDRESSES = [0x76, 0x77]

    def __init__(self, reading_key: str = "temperature"):
        self.reading_key = reading_key  # temperature | humidity | pressure
        self._addr: Optional[int] = None
        self._sensor = None

        if reading_key == "temperature":
            self.sensor_id = "bme280_temp"
            self.name = "Enclosure Temperature"
            self.unit = "°C"
        elif reading_key == "humidity":
            self.sensor_id = "bme280_hum"
            self.name = "Enclosure Humidity"
            self.unit = "%"
        else:
            self.sensor_id = "bme280_pres"
            self.name = "Barometric Pressure"
            self.unit = "hPa"

        self.sensor_type = "BME280"
        self.address = ""

    def detect(self) -> bool:
        try:
            import smbus2
            import bme280  # adafruit-circuitpython-bme280 or RPi.bme280
            bus = smbus2.SMBus(1)
            for addr in self.ADDRESSES:
                try:
                    params = bme280.load_calibration_params(bus, addr)
                    self._addr = addr
                    self._sensor = (bus, params)
                    self.address = hex(addr)
                    logger.info("BME280 detected at I2C %s", hex(addr))
                    return True
                except Exception:
                    pass
        except ImportError:
            logger.debug("BME280 library not installed")
        except Exception as e:
            logger.debug("BME280 detect error: %s", e)
        return False

    def read(self) -> float:
        if not self._sensor:
            raise RuntimeError("BME280 not initialized")
        import bme280
        bus, params = self._sensor
        data = bme280.sample(bus, self._addr, params)
        if self.reading_key == "temperature":
            return round(data.temperature, 2)
        elif self.reading_key == "humidity":
            return round(data.humidity, 2)
        else:
            return round(data.pressure, 2)


# ── ADS1115 + SCT-013 CT Clamp — Power Measurement ───────────────────────────

class CTClampPlugin(SensorPlugin):
    """SCT-013 current transformer via ADS1115 ADC."""

    # SCT-013-100 spec: 100A → 50mA, with 22Ω burden = 1.1V peak at 100A
    # Voltage RMS → current → power (assumes 230V grid)
    BURDEN_OHMS = 22.0
    CT_RATIO = 2000          # 100A / 50mA
    GRID_VOLTAGE = 230.0
    SAMPLES = 500            # samples for RMS calculation

    def __init__(self):
        self.sensor_id = "ct_power"
        self.name = "CT Clamp Power"
        self.sensor_type = "SCT-013 via ADS1115"
        self.address = "0x48"
        self.unit = "W"
        self._ads = None

    def detect(self) -> bool:
        try:
            import board
            import busio
            import adafruit_ads1x15.ads1115 as ADS
            from adafruit_ads1x15.analog_in import AnalogIn

            i2c = busio.I2C(board.SCL, board.SDA)
            ads = ADS.ADS1115(i2c, address=0x48)
            ads.gain = 1
            # Quick read to confirm hardware present
            chan = AnalogIn(ads, ADS.P0)
            _ = chan.voltage
            self._ads = ads
            logger.info("ADS1115 CT clamp detected at I2C 0x48")
            return True
        except Exception as e:
            logger.debug("ADS1115 detect error: %s", e)
            return False

    def read(self) -> float:
        """Return apparent power in Watts (RMS)."""
        if not self._ads:
            raise RuntimeError("ADS1115 not initialized")
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn

        chan = AnalogIn(self._ads, ADS.P0)

        # Collect samples for RMS
        sum_sq = 0.0
        for _ in range(self.SAMPLES):
            v = chan.voltage
            sum_sq += v * v
        vrms = math.sqrt(sum_sq / self.SAMPLES)

        # Convert voltage to current to power
        irms = (vrms / self.BURDEN_OHMS) * self.CT_RATIO
        power = irms * self.GRID_VOLTAGE
        return round(power, 1)


# ── TSL2591 — Ambient Light ───────────────────────────────────────────────────

class TSL2591Plugin(SensorPlugin):
    """AMS TSL2591 light sensor."""

    def __init__(self):
        self.sensor_id = "tsl2591_lux"
        self.name = "Ambient Light"
        self.sensor_type = "TSL2591"
        self.address = "0x29"
        self.unit = "lux"
        self._sensor = None

    def detect(self) -> bool:
        try:
            import board
            import busio
            import adafruit_tsl2591

            i2c = busio.I2C(board.SCL, board.SDA)
            self._sensor = adafruit_tsl2591.TSL2591(i2c)
            logger.info("TSL2591 light sensor detected at I2C 0x29")
            return True
        except Exception as e:
            logger.debug("TSL2591 detect error: %s", e)
            return False

    def read(self) -> float:
        if not self._sensor:
            raise RuntimeError("TSL2591 not initialized")
        lux = self._sensor.lux
        return round(lux if lux is not None else 0.0, 2)


# ── GPIO sensors — Reed switch & Flood ───────────────────────────────────────

class GPIOBinaryPlugin(SensorPlugin):
    """Generic binary GPIO sensor (reed switch, flood detector)."""

    def __init__(self, sensor_id: str, name: str, pin: int, active_high: bool = False):
        self.sensor_id = sensor_id
        self.name = name
        self.sensor_type = "GPIO Binary"
        self.address = f"gpio:{pin}"
        self.unit = "bool"
        self._pin = pin
        self._active_high = active_high
        self._gpio = None

    def detect(self) -> bool:
        try:
            from gpiozero import Button
            self._gpio = Button(self._pin, pull_up=not self._active_high)
            logger.info("%s sensor detected on GPIO %d", self.name, self._pin)
            return True
        except Exception as e:
            logger.debug("GPIO %d detect error: %s", self._pin, e)
            return False

    def read(self) -> float:
        if not self._gpio:
            raise RuntimeError(f"GPIO {self._pin} not initialized")
        pressed = self._gpio.is_pressed
        return 1.0 if pressed else 0.0


# ── SPH0645 I2S MEMS Microphone — Sound Level ─────────────────────────────────

class SoundLevelPlugin(SensorPlugin):
    """
    SPH0645 I2S MEMS microphone — reports dB SPL only.
    NOT recording audio. Captures ~100ms of PCM, computes RMS dB, discards.
    Requires: arecord (ALSA), I2S overlay in config.txt.
    """

    SAMPLE_DURATION_MS = 100
    SAMPLE_RATE = 16000

    def __init__(self):
        self.sensor_id = "sound_db"
        self.name = "Ambient Sound Level"
        self.sensor_type = "SPH0645 I2S"
        self.address = "i2s:0"
        self.unit = "dB"
        self._available = False

    def detect(self) -> bool:
        import subprocess
        try:
            result = subprocess.run(
                ["arecord", "-l"],
                capture_output=True, text=True, timeout=5
            )
            if "card" in result.stdout.lower():
                self._available = True
                logger.info("I2S microphone detected via ALSA")
                return True
        except Exception as e:
            logger.debug("Sound sensor detect error: %s", e)
        return False

    def read(self) -> float:
        """Capture a short burst, compute RMS dB, return level."""
        import subprocess
        import io

        samples = int(self.SAMPLE_RATE * self.SAMPLE_DURATION_MS / 1000)
        cmd = [
            "arecord",
            "-D", "plughw:0,0",
            "-f", "S16_LE",
            "-r", str(self.SAMPLE_RATE),
            "-c", "1",
            "--duration", "0",
            "-s", str(samples),
            "-t", "raw",
            "-q",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=2)
            raw = result.stdout
            if len(raw) < 2:
                return 0.0

            # Parse 16-bit signed PCM
            n = len(raw) // 2
            samples_data = struct.unpack(f"<{n}h", raw[: n * 2])

            # RMS
            rms = math.sqrt(sum(s * s for s in samples_data) / n)
            if rms < 1:
                return 0.0

            # Convert to dBFS, then approximate SPL (rough calibration)
            db_fs = 20 * math.log10(rms / 32768.0)
            db_spl = db_fs + 120  # rough conversion, site-calibrate for accuracy
            return round(max(0.0, db_spl), 1)
        except Exception as e:
            logger.warning("Sound level read error: %s", e)
            return 0.0


# ── Sensor Manager ────────────────────────────────────────────────────────────

class SensorManager:
    """
    Detects, polls, and exposes all connected sensors.
    Runs as a background asyncio task.
    """

    def __init__(self, config: dict, alerts: SensorAlerts):
        self.config = config
        self.alerts = alerts
        self.scan_interval: int = config.get("scan_interval", 30)
        self.sensors: dict[str, SensorInfo] = {}
        self._plugins: list[SensorPlugin] = []
        self._alert_callbacks: list = []
        self._last_history_ts: float = 0.0

        # Default GPIO pins (overridable in config)
        gpio_cfg = config.get("gpio", {})
        self._reed_pin: int = gpio_cfg.get("reed_switch_pin", 17)
        self._flood_pin: int = gpio_cfg.get("flood_sensor_pin", 27)

    def add_alert_callback(self, cb) -> None:
        self._alert_callbacks.append(cb)

    def _build_plugins(self) -> list[SensorPlugin]:
        """Instantiate all candidate sensor plugins."""
        return [
            BME280Plugin("temperature"),
            BME280Plugin("humidity"),
            BME280Plugin("pressure"),
            CTClampPlugin(),
            TSL2591Plugin(),
            GPIOBinaryPlugin("tamper", "Enclosure Tamper", self._reed_pin),
            GPIOBinaryPlugin("flood", "Flood Detector", self._flood_pin, active_high=True),
            SoundLevelPlugin(),
        ]

    def detect_all(self) -> None:
        """Probe hardware and build sensor registry. Run once on startup."""
        logger.info("Scanning for connected sensors...")
        candidates = self._build_plugins()

        # BME280 shares hardware — only scan once, all readings share detection
        bme280_detected = False
        for plugin in candidates:
            try:
                if plugin.sensor_type == "BME280":
                    if bme280_detected:
                        # Reuse result of first BME280 detect
                        if "bme280_temp" in self.sensors:
                            plugin._addr = candidates[0]._addr  # type: ignore[attr-defined]
                            plugin._sensor = candidates[0]._sensor  # type: ignore[attr-defined]
                            plugin.address = candidates[0].address
                            available = True
                        else:
                            available = False
                    else:
                        available = plugin.detect()
                        if available:
                            bme280_detected = True
                else:
                    available = plugin.detect()

                info = SensorInfo(
                    sensor_id=plugin.sensor_id,
                    name=plugin.name,
                    sensor_type=plugin.sensor_type,
                    address=plugin.address,
                    available=available,
                    unit=plugin.unit,
                )
                self.sensors[plugin.sensor_id] = info

                if available:
                    self._plugins.append(plugin)
                    logger.info("  ✓ %s (%s)", plugin.name, plugin.address)
                else:
                    logger.debug("  ✗ %s — not detected", plugin.name)

            except Exception as e:
                logger.warning("Error probing %s: %s", plugin.sensor_type, e)

        detected = sum(1 for s in self.sensors.values() if s.available)
        logger.info("Sensor scan complete: %d/%d available", detected, len(self.sensors))

    def _check_alerts(self, plugin: SensorPlugin, value: float) -> tuple[bool, str]:
        """Return (alert, reason) for a reading."""
        sid = plugin.sensor_id
        a = self.alerts

        if sid == "bme280_temp" and value > a.temperature_max:
            return True, f"Temperature {value}°C exceeds {a.temperature_max}°C"
        if sid == "bme280_hum" and value > a.humidity_max:
            return True, f"Humidity {value}% exceeds {a.humidity_max}%"
        if sid == "sound_db" and value > a.sound_db_max:
            return True, f"Sound level {value} dB exceeds {a.sound_db_max} dB"
        if sid == "tamper" and a.tamper and value > 0.5:
            return True, "Enclosure tamper detected"
        if sid == "flood" and a.flood and value > 0.5:
            return True, "Flood/water ingress detected"

        return False, ""

    async def _poll_once(self) -> None:
        """Poll all available sensors."""
        now = time.time()
        write_history = (now - self._last_history_ts) >= HISTORY_INTERVAL

        for plugin in self._plugins:
            try:
                value = await asyncio.get_event_loop().run_in_executor(None, plugin.read)
                alert, reason = self._check_alerts(plugin, value)

                reading = SensorReading(
                    sensor_id=plugin.sensor_id,
                    name=plugin.name,
                    value=value,
                    unit=plugin.unit,
                    timestamp=now,
                    alert=alert,
                    alert_reason=reason,
                )

                info = self.sensors[plugin.sensor_id]
                info.last_reading = reading

                if write_history:
                    info.history.append({"ts": now, "v": value})

                if alert:
                    logger.warning("SENSOR ALERT [%s]: %s", plugin.sensor_id, reason)
                    for cb in self._alert_callbacks:
                        try:
                            await cb(reading)
                        except Exception:
                            pass

            except Exception as e:
                logger.warning("Error reading %s: %s", plugin.sensor_id, e)

        if write_history:
            self._last_history_ts = now

    async def run(self) -> None:
        """Background polling loop."""
        self.detect_all()
        logger.info("Sensor polling started (interval: %ds)", self.scan_interval)

        while True:
            await self._poll_once()
            await asyncio.sleep(self.scan_interval)

    # ── API helpers ───────────────────────────────────────────────────────────

    def get_all_readings(self) -> list[dict]:
        out = []
        for info in self.sensors.values():
            r = info.last_reading
            out.append({
                "sensor_id": info.sensor_id,
                "name": info.name,
                "type": info.sensor_type,
                "address": info.address,
                "available": info.available,
                "unit": info.unit,
                "value": r.value if r else None,
                "alert": r.alert if r else False,
                "alert_reason": r.alert_reason if r else "",
                "timestamp": r.timestamp if r else None,
            })
        return out

    def get_history(self, sensor_id: str) -> Optional[list[dict]]:
        info = self.sensors.get(sensor_id)
        if info is None:
            return None
        return list(info.history)

    def get_scan_results(self) -> dict:
        """For /diag/sensors — raw scan results."""
        i2c_addresses = _i2c_scan()
        return {
            "i2c_addresses_found": [hex(a) for a in i2c_addresses],
            "sensors": [
                {
                    "sensor_id": info.sensor_id,
                    "name": info.name,
                    "type": info.sensor_type,
                    "address": info.address,
                    "available": info.available,
                }
                for info in self.sensors.values()
            ],
        }
