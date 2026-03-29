"""Tests for gateway/proxy.py"""

import pytest
from gateway.proxy import _extract_action, _extract_charger_id, get_message_buffer


def test_extract_charger_id_simple():
    assert _extract_charger_id("/ocpp/CHARGER-001") == "CHARGER-001"


def test_extract_charger_id_nested():
    assert _extract_charger_id("/ws/ocpp/SITE-A/CP-42") == "CP-42"


def test_extract_charger_id_root():
    assert _extract_charger_id("/") == "unknown"


def test_extract_charger_id_bare():
    assert _extract_charger_id("CHARGER-XYZ") == "CHARGER-XYZ"


def test_extract_action_call():
    payload = '[2,"abc123","BootNotification",{"chargePointModel":"Test"}]'
    assert _extract_action(payload) == "BootNotification"


def test_extract_action_result():
    # CALLRESULT has no action field at index 2
    payload = '[3,"abc123",{"status":"Accepted"}]'
    assert _extract_action(payload) == ""


def test_extract_action_invalid():
    assert _extract_action("not json") == ""
    assert _extract_action("{}") == ""
    assert _extract_action("") == ""


def test_message_buffer_is_deque():
    buf = get_message_buffer()
    from collections import deque
    assert isinstance(buf, deque)
