"""InfluxDB line protocol encoding for dump1090 / readsb aircraft objects.

One measurement, ``aircraft``:

- tags: ``hex``, ``flight``, ``type``, ``squawk`` (low cardinality, indexed)
- fields: ``lat``, ``lon``, ``alt_baro``, ``gs``, ``track``, ``rssi``
  (always written as floats so a field never mixes integer and float types)
- one uint field: ``messages`` (written with the ``i`` suffix)

Escaping follows the line protocol grammar: measurement, tag keys/values and
field keys escape backslash, comma, space and ``=``; string field values escape
backslash and double quote. Non-ICAO hex addresses (readsb ``~a1b2c3`` style)
need no escaping - ``~`` is an ordinary character and passes through verbatim.

``alt_baro`` may be the string ``"ground"`` in ``aircraft.json``. The chosen
behaviour is **numeric coercion to 0.0**: a parked aircraft keeps its place in
the altitude time series instead of creating a gap or a second, string-typed
series (InfluxDB rejects a field that is sometimes a string and sometimes a
number). Any other non-numeric ``alt_baro`` is skipped.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from dump1090_influx.errors import InfluxError

__all__ = [
    "FLOAT_FIELDS",
    "MEASUREMENT",
    "TAG_KEYS",
    "UINT_FIELD",
    "Point",
    "aircraft_to_point",
    "escape_key",
    "escape_string_value",
    "format_field_value",
    "format_float",
    "timestamp_ns",
    "to_line",
]

MEASUREMENT = "aircraft"
TAG_KEYS = ("hex", "flight", "type", "squawk")
FLOAT_FIELDS = ("lat", "lon", "alt_baro", "gs", "track", "rssi")
UINT_FIELD = "messages"

#: A field value: floats for measurements, ints for uint fields, str/bool allowed
#: for generically-built points (aircraft points never emit string fields).
FieldValue = str | int | float | bool

_KEY_ESCAPES = (("\\", "\\\\"), (",", "\\,"), (" ", "\\ "), ("=", "\\="))


@dataclass(frozen=True, slots=True)
class Point:
    """One line-protocol point: measurement + tags + fields + optional ns timestamp."""

    measurement: str
    tags: dict[str, str]
    fields: dict[str, FieldValue]
    time_ns: int | None = None


def escape_key(value: str) -> str:
    """Escape a measurement name, tag key/value or field key."""
    escaped = value
    for raw, replacement in _KEY_ESCAPES:
        escaped = escaped.replace(raw, replacement)
    return escaped


def escape_string_value(value: str) -> str:
    """Escape a string field value (backslash first, then double quotes)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def format_float(value: float) -> str:
    """Render a float the way line protocol wants it: shortest round-trip, always decimal."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite float cannot be written: {value!r}")
    return repr(number)


def format_field_value(value: FieldValue) -> str:
    """Render one field value: ``true``/``false``, ``12i``, ``1.5`` or ``"text"``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return format_float(value)
    return f'"{escape_string_value(value)}"'


def to_line(point: Point) -> str:
    """Encode a :class:`Point` as one line of line protocol.

    Tags are emitted sorted by key (the canonical order InfluxDB stores them
    in); fields keep insertion order. Empty tag values are dropped - an empty
    tag value is invalid and callsign-less aircraft are common.

    Raises:
        InfluxError: The point has no fields (line protocol requires at least one).
    """
    if not point.fields:
        raise InfluxError(f"measurement {point.measurement!r} has no fields to write")
    head = escape_key(point.measurement)
    tags = ",".join(
        f"{escape_key(key)}={escape_key(value)}"
        for key, value in sorted(point.tags.items())
        if value != ""
    )
    if tags:
        head += f",{tags}"
    fields = ",".join(
        f"{escape_key(key)}={format_field_value(value)}" for key, value in point.fields.items()
    )
    line = f"{head} {fields}"
    if point.time_ns is not None:
        line += f" {point.time_ns}"
    return line


def timestamp_ns(t: float) -> int:
    """Convert unix epoch seconds to an integer nanosecond timestamp."""
    return round(float(t) * 1e9)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _altitude(value: Any) -> float | None:
    if isinstance(value, str) and value.strip().lower() == "ground":
        return 0.0
    return _finite_float(value)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def aircraft_to_point(entry: Mapping[str, Any], t: float) -> Point | None:
    """Map one ``aircraft.json`` entry to an ``aircraft`` point.

    ``t`` (the snapshot's ``now``) becomes the nanosecond timestamp, so replays
    of recorded NDJSON land at their original capture time.

    Returns:
        The point, or ``None`` when the entry has no usable hex address or no
        writable field (line protocol cannot represent an empty point).

    Non-numeric or non-finite field values are skipped rather than written:
    a bad latitude must not poison a whole batch.
    """
    hex_value = entry.get("hex")
    if not isinstance(hex_value, str) or not hex_value.strip():
        return None

    tags: dict[str, str] = {"hex": hex_value.strip()}
    for key in ("flight", "type", "squawk"):
        raw = entry.get(key)
        if key == "type" and (not isinstance(raw, str) or not raw.strip()):
            raw = entry.get("t")  # dump1090-fa spells the ICAO type code "t"
        if isinstance(raw, str) and raw.strip():
            tags[key] = raw.strip()

    fields: dict[str, FieldValue] = {}
    for key in ("lat", "lon"):
        number = _finite_float(entry.get(key))
        if number is not None:
            fields[key] = number
    altitude = _altitude(entry.get("alt_baro"))
    if altitude is not None:
        fields["alt_baro"] = altitude
    for key in ("gs", "track", "rssi"):
        number = _finite_float(entry.get(key))
        if number is not None:
            fields[key] = number
    messages = _count(entry.get(UINT_FIELD))
    if messages is not None:
        fields[UINT_FIELD] = messages

    if not fields:
        return None
    return Point(
        measurement=MEASUREMENT,
        tags=tags,
        fields=fields,
        time_ns=timestamp_ns(t),
    )
