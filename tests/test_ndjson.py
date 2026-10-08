"""Tests for the shared NDJSON format: round-trip, validation, error paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import aircraft, make_records
from dump1090_influx.errors import InfluxError
from dump1090_influx.lineprotocol import aircraft_to_point, to_line
from dump1090_influx.ndjson import (
    Record,
    append_record,
    format_record,
    read_ndjson,
    write_ndjson,
)


def test_write_read_round_trip(tmp_path: Path) -> None:
    records = make_records(5)
    path = tmp_path / "flight.ndjson"
    write_ndjson(path, records)
    assert read_ndjson(path) == records


def test_written_lines_have_exactly_t_and_aircraft(tmp_path: Path) -> None:
    path = tmp_path / "flight.ndjson"
    write_ndjson(path, make_records(2))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        payload = json.loads(line)
        assert set(payload) == {"t", "aircraft"}
        assert isinstance(payload["t"], float)
        assert isinstance(payload["aircraft"], list)


def test_recorded_aircraft_become_line_protocol_points(tmp_path: Path) -> None:
    """The full round trip: record NDJSON -> read -> points -> lines."""
    path = tmp_path / "flight.ndjson"
    write_ndjson(path, make_records(3))
    lines = []
    for record in read_ndjson(path):
        for entry in record.aircraft:
            point = aircraft_to_point(entry, record.t)
            assert point is not None
            lines.append(to_line(point))
    assert len(lines) == 3
    assert lines[0].startswith("aircraft,flight=TEST000,hex=000000,")
    assert lines[0].endswith(str(1791028800000000000))


def test_append_record_adds_lines(tmp_path: Path) -> None:
    path = tmp_path / "flight.ndjson"
    records = make_records(3)
    append_record(path, records[0])
    append_record(path, records[1])
    append_record(path, records[2])
    assert read_ndjson(path) == records


def test_format_record_is_single_line() -> None:
    line = format_record(Record(t=1.0, aircraft=[aircraft()]))
    assert "\n" not in line
    assert json.loads(line)["aircraft"][0]["hex"] == "4ca918"


def test_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(InfluxError, match="file not found"):
        read_ndjson(tmp_path / "nope.ndjson")


def test_directory_instead_of_file_errors(tmp_path: Path) -> None:
    with pytest.raises(InfluxError, match="cannot read"):
        read_ndjson(tmp_path)


def test_empty_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.ndjson"
    path.write_text("", encoding="utf-8")
    with pytest.raises(InfluxError, match="no records found"):
        read_ndjson(path)


def test_blank_lines_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "flight.ndjson"
    records = make_records(2)
    body = "\n".join([format_record(records[0]), "", "  ", format_record(records[1])])
    path.write_text(body + "\n", encoding="utf-8")
    assert read_ndjson(path) == records


def test_invalid_json_reports_line_number(tmp_path: Path) -> None:
    path = tmp_path / "broken.ndjson"
    good = format_record(make_records(1)[0])
    path.write_text(good + "\n" + '{"t": 1.0, "aircraft": [\n', encoding="utf-8")
    with pytest.raises(InfluxError, match=r"broken\.ndjson:2: invalid JSON"):
        read_ndjson(path)


def test_non_object_line_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.ndjson"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(InfluxError, match="expected a JSON object, got list"):
        read_ndjson(path)


def test_missing_t_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.ndjson"
    path.write_text('{"aircraft": []}\n', encoding="utf-8")
    with pytest.raises(InfluxError, match="'t' must be a number"):
        read_ndjson(path)


def test_boolean_t_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.ndjson"
    path.write_text('{"t": true, "aircraft": []}\n', encoding="utf-8")
    with pytest.raises(InfluxError, match="'t' must be a number"):
        read_ndjson(path)


def test_missing_aircraft_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.ndjson"
    path.write_text('{"t": 1.0}\n', encoding="utf-8")
    with pytest.raises(InfluxError, match="'aircraft' must be a list"):
        read_ndjson(path)


def test_out_of_order_timestamps_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.ndjson"
    records = make_records(2)
    path.write_text(
        format_record(records[1]) + "\n" + format_record(records[0]) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(InfluxError, match="goes backwards"):
        read_ndjson(path)


def test_non_dict_aircraft_entries_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "mixed.ndjson"
    path.write_text(
        '{"t": 1.0, "aircraft": [{"hex": "abc123"}, "junk", null, 7]}\n',
        encoding="utf-8",
    )
    [record] = read_ndjson(path)
    assert [entry["hex"] for entry in record.aircraft] == ["abc123"]


def test_write_ndjson_to_unwritable_path_errors(tmp_path: Path) -> None:
    with pytest.raises(InfluxError, match="cannot write"):
        write_ndjson(tmp_path, make_records(1))  # tmp_path is a directory


def test_append_to_unwritable_path_errors(tmp_path: Path) -> None:
    with pytest.raises(InfluxError, match="cannot write"):
        append_record(tmp_path, make_records(1)[0])
