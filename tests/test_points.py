"""Tests for mapping aircraft.json entries onto ``aircraft`` points."""

from __future__ import annotations

from conftest import T0, aircraft
from dump1090_influx.lineprotocol import aircraft_to_point, to_line

FULL = {
    "hex": "4ca918",
    "flight": "RYR22GK ",
    "type": "B738",
    "squawk": "1200",
    "lat": 51.4712,
    "lon": -0.4543,
    "alt_baro": 34000,
    "gs": 458,
    "track": 210.5,
    "rssi": -4.2,
    "messages": 1204,
    "seen": 0.2,
    "category": "A3",
}


def test_full_aircraft_maps_to_spec_point() -> None:
    point = aircraft_to_point(FULL, t=T0)
    assert point is not None
    assert point.measurement == "aircraft"
    assert point.tags == {
        "hex": "4ca918",
        "flight": "RYR22GK",
        "type": "B738",
        "squawk": "1200",
    }
    assert point.fields == {
        "lat": 51.4712,
        "lon": -0.4543,
        "alt_baro": 34000.0,
        "gs": 458.0,
        "track": 210.5,
        "rssi": -4.2,
        "messages": 1204,
    }
    assert point.time_ns == 1791028800000000000


def test_field_order_on_the_wire_matches_spec() -> None:
    point = aircraft_to_point(FULL, t=T0)
    assert point is not None
    line = to_line(point)
    fields = line.split(" ")[1]
    assert fields.split(",") == [
        "lat=51.4712",
        "lon=-0.4543",
        "alt_baro=34000.0",
        "gs=458.0",
        "track=210.5",
        "rssi=-4.2",
        "messages=1204i",
    ]


def test_ground_altitude_is_coerced_to_zero() -> None:
    point = aircraft_to_point(aircraft(alt_baro="ground", gs=0), t=T0)
    assert point is not None
    assert point.fields["alt_baro"] == 0.0
    assert "alt_baro=0.0" in to_line(point)


def test_non_ground_altitude_string_is_skipped() -> None:
    point = aircraft_to_point(aircraft(alt_baro="high flight", gs=100), t=T0)
    assert point is not None
    assert "alt_baro" not in point.fields


def test_missing_hex_is_skipped() -> None:
    assert aircraft_to_point({"flight": "BAW117"}, t=T0) is None
    assert aircraft_to_point({"hex": ""}, t=T0) is None
    assert aircraft_to_point({"hex": "   "}, t=T0) is None
    assert aircraft_to_point({"hex": 123456}, t=T0) is None


def test_hex_is_stripped_and_tilde_addresses_survive() -> None:
    point = aircraft_to_point({"hex": " ~a1b2c3 ", "gs": 120}, t=T0)
    assert point is not None
    assert point.tags["hex"] == "~a1b2c3"
    assert "hex=~a1b2c3" in to_line(point)


def test_callsign_padding_is_stripped_and_empty_callsign_dropped() -> None:
    point = aircraft_to_point(aircraft(flight="  "), t=T0)
    assert point is not None
    assert "flight" not in point.tags
    padded = aircraft_to_point(aircraft(flight="BAW117  "), t=T0)
    assert padded is not None
    assert padded.tags["flight"] == "BAW117"


def test_type_falls_back_to_dump1090_fa_t_field() -> None:
    point = aircraft_to_point({"hex": "abc123", "t": "A320", "gs": 100}, t=T0)
    assert point is not None
    assert point.tags["type"] == "A320"
    explicit = aircraft_to_point({"hex": "abc123", "t": "A320", "type": "A321", "gs": 100}, t=T0)
    assert explicit is not None
    assert explicit.tags["type"] == "A321"


def test_aircraft_without_position_still_yields_a_point() -> None:
    entry = {"hex": "abc123", "messages": 12, "rssi": -7.5, "seen": 1.0}
    point = aircraft_to_point(entry, t=T0)
    assert point is not None
    assert point.fields == {"rssi": -7.5, "messages": 12}


def test_aircraft_with_no_writable_field_yields_no_point() -> None:
    assert aircraft_to_point({"hex": "abc123"}, t=T0) is None


def test_bad_numbers_are_skipped_not_poisoned() -> None:
    entry = aircraft(lat=float("nan"), lon=float("inf"), gs="fast", track=True)
    point = aircraft_to_point(entry, t=T0)
    assert point is not None
    assert set(point.fields) == {"alt_baro", "rssi", "messages"}


def test_messages_is_a_uint_field_and_bad_counts_dropped() -> None:
    point = aircraft_to_point(aircraft(messages=1204), t=T0)
    assert point is not None
    assert point.fields["messages"] == 1204
    assert "messages=1204i" in to_line(point)

    negative = aircraft_to_point(aircraft(messages=-1), t=T0)
    assert negative is not None
    assert "messages" not in negative.fields

    boolean = aircraft_to_point(aircraft(messages=True), t=T0)
    assert boolean is not None
    assert "messages" not in boolean.fields

    integral_float = aircraft_to_point(aircraft(messages=100.0), t=T0)
    assert integral_float is not None
    assert integral_float.fields["messages"] == 100


def test_integral_numeric_fields_are_written_as_floats() -> None:
    point = aircraft_to_point(aircraft(alt_baro=34000, gs=458, lat=52, lon=13), t=T0)
    assert point is not None
    line = to_line(point)
    assert "alt_baro=34000.0" in line
    assert "gs=458.0" in line
    assert "lat=52.0" in line
    assert "lon=13.0" in line


def test_emergency_and_extra_fields_never_become_tags_or_fields() -> None:
    point = aircraft_to_point(aircraft(emergency="major", category="A3", nic=8), t=T0)
    assert point is not None
    assert set(point.tags) <= {"hex", "flight", "type", "squawk"}
    assert set(point.fields) <= {"lat", "lon", "alt_baro", "gs", "track", "rssi", "messages"}
