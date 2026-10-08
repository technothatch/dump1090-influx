"""Errors raised by dump1090-influx."""

from __future__ import annotations

__all__ = ["InfluxError"]


class InfluxError(Exception):
    """An unrecoverable error: unreadable input, unreachable decoder, rejected write."""
