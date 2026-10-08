"""Tests for the record command: polling, timestamps, error handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import T0, FakeResponse, aircraft, aircraft_doc, json_body
from dump1090_influx.errors import InfluxError
from dump1090_influx.ndjson import read_ndjson
from dump1090_influx.record import fetch_aircraft, record, resolve_url, snapshot_record


def opener_returning(payload: bytes) -> Any:
    def opener(_url: str, timeout: float = 0.0) -> FakeResponse:
        return FakeResponse(payload)

    return opener


def opener_failing() -> Any:
    import urllib.error

    def opener(_url: str, timeout: float = 0.0) -> Any:
        raise urllib.error.URLError("decoder is down")

    return opener


def test_resolve_url_appends_aircraft_path() -> None:
    assert resolve_url("http://localhost:8080") == "http://localhost:8080/data/aircraft.json"
    assert resolve_url("http://host:8080/") == "http://host:8080/data/aircraft.json"
    assert (
        resolve_url("http://host:8080/data/aircraft.json") == "http://host:8080/data/aircraft.json"
    )


def test_fetch_aircraft_returns_document() -> None:
    doc = fetch_aircraft(
        "http://x/data/aircraft.json",
        opener=opener_returning(json_body(aircraft_doc(aircraft()))),
    )
    assert doc["now"] == T0
    assert doc["aircraft"][0]["hex"] == "4ca918"


def test_fetch_aircraft_rejects_non_json() -> None:
    with pytest.raises(InfluxError, match="did not return JSON"):
        fetch_aircraft("http://x/a.json", opener=opener_returning(b"<html>nope</html>"))


def test_fetch_aircraft_rejects_non_object_json() -> None:
    with pytest.raises(InfluxError, match="expected an object"):
        fetch_aircraft("http://x/a.json", opener=opener_returning(b"[1, 2]"))


def test_fetch_aircraft_rejects_missing_aircraft_array() -> None:
    with pytest.raises(InfluxError, match="no 'aircraft' array"):
        fetch_aircraft("http://x/a.json", opener=opener_returning(b'{"now": 1.0}'))


def test_fetch_aircraft_reports_network_failures() -> None:
    with pytest.raises(InfluxError, match="cannot read"):
        fetch_aircraft("http://x/a.json", opener=opener_failing())


def test_snapshot_record_prefers_decoder_now() -> None:
    record_ = snapshot_record(aircraft_doc(aircraft(), now=1791028800.0), wall=99.0)
    assert record_.t == 1791028800.0


def test_snapshot_record_falls_back_to_wall_clock() -> None:
    record_ = snapshot_record({"aircraft": []}, wall=42.0)
    assert record_.t == 42.0
    record_ = snapshot_record({"now": "soon", "aircraft": []}, wall=43.0)
    assert record_.t == 43.0


def test_record_writes_one_line_per_poll(tmp_path: Path) -> None:
    out = tmp_path / "flight.ndjson"
    written = record(
        "http://decoder:8080",
        out=str(out),
        interval=0.0,
        max_polls=3,
        opener=opener_returning(json_body(aircraft_doc(aircraft()))),
        sleep=lambda _s: None,
        now=lambda: 0.0,
    )
    assert written == 3
    records = read_ndjson(out)
    assert len(records) == 3
    assert records[0].t == T0
    assert records[0].aircraft[0]["hex"] == "4ca918"


def test_record_survives_a_dead_decoder(tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / "flight.ndjson"
    written = record(
        "http://decoder:8080",
        out=str(out),
        interval=0.0,
        max_polls=2,
        opener=opener_failing(),
        sleep=lambda _s: None,
        now=lambda: 0.0,
    )
    assert written == 0
    assert "warning:" in capsys.readouterr().err


def test_record_to_an_unwritable_path_raises(tmp_path: Path) -> None:
    with pytest.raises(InfluxError, match="cannot write"):
        record("http://decoder:8080", out=str(tmp_path), max_polls=1, opener=opener_failing())


def test_record_interval_must_be_respected_by_caller(tmp_path: Path) -> None:
    """Sleep is injected, so the inter-poll cadence is observable."""
    sleeps: list[float] = []
    record(
        "http://decoder:8080",
        out=str(tmp_path / "f.ndjson"),
        interval=2.5,
        max_polls=3,
        opener=opener_returning(json_body(aircraft_doc(aircraft()))),
        sleep=sleeps.append,
        now=lambda: 0.0,
    )
    assert sleeps == [2.5, 2.5]
