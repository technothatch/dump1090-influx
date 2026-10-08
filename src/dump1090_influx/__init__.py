"""Package exports for dump1090-influx."""

from __future__ import annotations

from dump1090_influx.errors import InfluxError
from dump1090_influx.lineprotocol import Point, aircraft_to_point, to_line
from dump1090_influx.ndjson import Record, read_ndjson

__all__ = [
    "InfluxError",
    "Point",
    "Record",
    "__version__",
    "aircraft_to_point",
    "read_ndjson",
    "to_line",
]

__version__ = "0.1.0"
