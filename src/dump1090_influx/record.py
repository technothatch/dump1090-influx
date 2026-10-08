"""Poll a live decoder and append snapshots to the shared NDJSON format.

``dump1090-influx record`` fetches ``/data/aircraft.json`` from dump1090-fa or
readsb on an interval and writes one ``{"t": ..., "aircraft": [...]}`` line per
poll - consumable by dump1090-replay, dump1090-recorder and
``dump1090-influx write``.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from dump1090_influx.errors import InfluxError
from dump1090_influx.ndjson import Record, format_record

__all__ = [
    "AIRCRAFT_PATH",
    "DEFAULT_HOST",
    "fetch_aircraft",
    "record",
    "resolve_url",
    "snapshot_record",
]

DEFAULT_HOST = "http://localhost:8080"
AIRCRAFT_PATH = "/data/aircraft.json"


def resolve_url(url: str) -> str:
    """Accept a bare decoder base URL or a full ``*.json`` document URL.

    Anything that does not already point at a JSON document is treated as the
    decoder root and gets ``/data/aircraft.json`` appended, which is where both
    dump1090-fa and readsb publish their aircraft state.
    """
    cleaned = url.rstrip("/")
    if cleaned.endswith(".json"):
        return cleaned
    return cleaned + AIRCRAFT_PATH


def fetch_aircraft(
    url: str,
    *,
    timeout: float = 5.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Fetch one full ``aircraft.json`` document.

    Raises:
        InfluxError: Network failure, non-JSON body, or an unexpected shape.
    """
    try:
        with opener(url, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise InfluxError(f"cannot read {url}: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InfluxError(f"{url} did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InfluxError(f"{url} returned a JSON {type(payload).__name__}, expected an object")
    aircraft = payload.get("aircraft")
    if not isinstance(aircraft, list):
        raise InfluxError(f"{url} returned no 'aircraft' array")
    return payload


def snapshot_record(doc: dict[str, Any], *, wall: float) -> Record:
    """Turn a fetched document into one NDJSON record.

    ``t`` prefers the decoder's own ``now`` (so timestamps line up with what
    the decoder served); a document without a numeric ``now`` falls back to the
    wall clock at poll time.
    """
    decoder_now = doc.get("now")
    if isinstance(decoder_now, int | float) and not isinstance(decoder_now, bool):
        t = float(decoder_now)
    else:
        t = wall
    aircraft = doc.get("aircraft")
    entries: Sequence[Any] = aircraft if isinstance(aircraft, list) else []
    return Record(t=t, aircraft=[entry for entry in entries if isinstance(entry, dict)])


def record(
    url: str = DEFAULT_HOST,
    *,
    out: str,
    interval: float = 1.0,
    max_polls: int = 0,
    timeout: float = 5.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> int:
    """Poll a decoder and append every snapshot to ``out``.

    Args:
        url: Decoder base URL or full ``aircraft.json`` URL.
        out: Destination NDJSON path (truncated).
        interval: Seconds between polls.
        max_polls: Stop after this many polls (0 = run until interrupted).
        timeout: HTTP timeout per poll.
        opener: Injectable HTTP opener for tests.
        sleep: Injectable sleep for tests.
        now: Injectable wall clock, used when the document has no ``now``.

    Returns:
        Number of snapshots written. Failed polls warn on stderr and do not
        abort the recording (a decoder rebooting mid-recording is normal); they
        still count towards ``max_polls``.

    Raises:
        InfluxError: The output file cannot be written.
    """
    endpoint = resolve_url(url)
    target = Path(out)
    try:
        handle: TextIO = target.open("w", encoding="utf-8", newline="\n")
    except OSError as exc:
        raise InfluxError(f"cannot write {target}: {exc}") from exc

    written = 0
    polls = 0
    with handle:
        try:
            while True:
                polls += 1
                try:
                    doc = fetch_aircraft(endpoint, timeout=timeout, opener=opener)
                except InfluxError as exc:
                    print(f"warning: {exc}", file=sys.stderr)
                else:
                    line = format_record(snapshot_record(doc, wall=now()))
                    try:
                        handle.write(line + "\n")
                    except OSError as exc:
                        raise InfluxError(f"cannot write {target}: {exc}") from exc
                    written += 1
                if max_polls and polls >= max_polls:
                    break
                sleep(interval)
        except KeyboardInterrupt:  # Ctrl+C ends the recording, not crash it
            print("\n# interrupted", file=sys.stderr)
    return written
