"""Notification adapters (Discord/webhook alerts)."""

from .discord import send_alert, send_text_alert, send_trade_alert

__all__ = ["send_alert", "send_text_alert", "send_trade_alert"]
