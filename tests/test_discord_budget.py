"""D9 alert budget tests."""

from __future__ import annotations

from unittest.mock import patch

from hermes_core.notify.discord import send_trade_alert, take_alert_budget


def test_d9_allows_first_alert_in_window():
    allowed, _ = take_alert_budget("forex", "EUR/USD", "trade_close")
    assert allowed is True


def test_d9_suppresses_second_alert_same_window():
    assert take_alert_budget("forex", "EUR/USD", "trade_close")[0] is True
    assert take_alert_budget("forex", "EUR/USD", "trade_close")[0] is False


def test_d9_different_guard_not_shared():
    assert take_alert_budget("forex", "EUR/USD", "trade_close")[0] is True
    assert take_alert_budget("forex", "EUR/USD", "heartbeat")[0] is True


@patch("hermes_core.notify.discord.send_alert", return_value=True)
def test_send_trade_alert_respects_budget(mock_send):
    assert (
        send_trade_alert("forex", "EUR/USD", "sl", -1.0, webhook_url="https://example.invalid/hook")
        is True
    )
    assert (
        send_trade_alert("forex", "EUR/USD", "sl", -1.0, webhook_url="https://example.invalid/hook")
        is False
    )
    assert mock_send.call_count == 1
