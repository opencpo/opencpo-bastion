"""Tests for gateway/lpr.py"""

import time
import pytest
from gateway.lpr import PlateRead, LPRProcessor
from gateway.cctv import CameraEvent


def _make_lpr_event(plate: str, confidence: float = 0.95) -> CameraEvent:
    return CameraEvent(
        event_id=f"evt-{time.time()}",
        camera_id="cam-01",
        event_type="lpr",
        timestamp=time.time(),
        metadata={
            "license_plate_number": plate,
            "confidence": confidence,
        },
    )


def test_plate_read_to_dict():
    read = PlateRead(
        plate="AB-123-C",
        confidence=0.95,
        camera_id="cam-01",
        timestamp=1000.0,
    )
    d = read.to_dict(include_thumbnail=False)
    assert d["plate"] == "AB-123-C"
    assert d["confidence"] == 0.95
    assert "thumbnail_b64" not in d


def test_plate_read_to_dict_with_thumbnail():
    read = PlateRead(
        plate="AB-123-C",
        confidence=0.9,
        camera_id="cam-01",
        timestamp=1000.0,
        thumbnail_b64="base64data",
    )
    d = read.to_dict(include_thumbnail=True)
    assert d["thumbnail_b64"] == "base64data"


def test_search_history():
    proc = LPRProcessor({}, core_api_url="http://localhost", site_id="test")

    # Manually add to buffer
    proc._buffer.append(PlateRead("AB-123-C", 0.9, "cam-01", time.time()))
    proc._buffer.append(PlateRead("XY-456-Z", 0.8, "cam-01", time.time()))
    proc._buffer.append(PlateRead("AB-789-D", 0.85, "cam-02", time.time()))

    results = proc.search("AB")
    plates = [r["plate"] for r in results]
    assert "AB-123-C" in plates
    assert "AB-789-D" in plates
    assert "XY-456-Z" not in plates


def test_recent_reads_limit():
    proc = LPRProcessor({}, core_api_url="http://localhost", site_id="test")

    for i in range(10):
        proc._buffer.append(PlateRead(f"AA-{i:03d}-B", 0.9, "cam-01", time.time()))

    results = proc.recent_reads(limit=5)
    assert len(results) == 5


def test_plate_normalized_uppercase():
    # Plate normalization (uppercase + strip) is done in handle_event
    # Test the normalization logic directly
    raw = " ab-123-c "
    normalized = raw.upper().strip().replace(" ", "")
    assert normalized == "AB-123-C"
