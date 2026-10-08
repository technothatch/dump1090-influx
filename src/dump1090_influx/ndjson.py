"""The NDJSON recording format shared across the dump1090-influx ecosystem.

Every line is one JSON object::

    {"t": <epoch seconds>, "aircraft": [ ... ]}

``t`` is the decoder's ``now`` at capture time (wall clock as a fallback) and
``aircraft`` is the raw ``aircraft`` array from ``aircraft.json`` with every
field preserved verbatim - including ``seen`` / ``seen_pos`` (ages relative to
``t``) and ``alt_baro`` which may be the string ``"ground"``.

This is the exact convention written by ``dump1090-replay record`` and
``dump1090-recorder``, so recordings interchange freely between the tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dump1090_influx.errors import InfluxError

__all__ = ["Record", "append_record", "format_record", "read_ndjson", "write_ndjson"]


@dataclass(frozen=True, slots=True)
class Record:
    """One captured ``aircraft.json`` snapshot."""

    t: float
    aircraft: list[dict[str, Any]]


def format_record(record: Record) -> str:
    """Serialise one record as a single NDJSON line (without the newline)."""
    payload: dict[str, Any] = {"t": record.t, "aircraft": record.aircraft}
    return json.dumps(payload, ensure_ascii=False, separators=(", ", ": "))


def append_record(path: str | Path, record: Record) -> None:
    """Append one record to ``path``, creating the file if needed."""
    target = Path(path)
    try:
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(format_record(record) + "\n")
    except OSError as exc:
        raise InfluxError(f"cannot write {target}: {exc}") from exc


def write_ndjson(path: str | Path, records: list[Record]) -> None:
    """Write a whole recording to ``path`` (truncating any existing file)."""
    target = Path(path)
    body = "".join(format_record(record) + "\n" for record in records)
    try:
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    except OSError as exc:
        raise InfluxError(f"cannot write {target}: {exc}") from exc


def read_ndjson(path: str | Path) -> list[Record]:
    """Read an NDJSON recording, validating shape and timestamp ordering.

    Raises:
        InfluxError: The file is missing/unreadable, empty, or any line is not
            a ``{"t": <number>, "aircraft": [<object>, ...]}`` object with
            non-decreasing timestamps.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InfluxError(f"{target}: file not found") from exc
    except OSError as exc:
        raise InfluxError(f"cannot read {target}: {exc}") from exc

    records: list[Record] = []
    previous_t: float | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        record = _parse_line(target, lineno, line)
        if previous_t is not None and record.t < previous_t:
            raise InfluxError(
                f"{target}:{lineno}: timestamp {record.t} goes backwards (previous {previous_t})"
            )
        previous_t = record.t
        records.append(record)

    if not records:
        raise InfluxError(f"{target}: no records found")
    return records


def _parse_line(target: Path, lineno: int, line: str) -> Record:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise InfluxError(f"{target}:{lineno}: invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise InfluxError(
            f"{target}:{lineno}: expected a JSON object, got {type(payload).__name__}"
        )

    t = payload.get("t")
    if isinstance(t, bool) or not isinstance(t, int | float):
        raise InfluxError(f"{target}:{lineno}: 't' must be a number (unix epoch seconds)")
    aircraft = payload.get("aircraft")
    if not isinstance(aircraft, list):
        raise InfluxError(f"{target}:{lineno}: 'aircraft' must be a list")

    return Record(
        t=float(t),
        aircraft=[entry for entry in aircraft if isinstance(entry, dict)],
    )
