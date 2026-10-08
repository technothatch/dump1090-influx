"""Tests for line-protocol escaping, number formatting and point encoding."""

from __future__ import annotations

import pytest

from dump1090_influx.errors import InfluxError
from dump1090_influx.lineprotocol import (
    Point,
    escape_key,
    escape_string_value,
    format_field_value,
    format_float,
    timestamp_ns,
    to_line,
)


def test_escape_key_escapes_space_comma_equals_and_backslash() -> None:
    assert escape_key("a b,c=d\\e") == "a\\ b\\,c\\=d\\\\e"


def test_escape_key_leaves_tilde_hex_verbatim() -> None:
    assert escape_key("~a1b2c3") == "~a1b2c3"
    assert escape_key("4ca918") == "4ca918"


def test_escape_key_is_idempotent_on_plain_values() -> None:
    assert escape_key("RYR22GK") == "RYR22GK"
    assert escape_key("") == ""


def test_escape_string_value_escapes_quotes_and_backslashes() -> None:
    assert escape_string_value('say "hi"\\n') == 'say \\"hi\\"\\\\n'


def test_format_float_renders_integer_values_with_decimal_point() -> None:
    assert format_float(34000) == "34000.0"
    assert format_float(0) == "0.0"


def test_format_float_keeps_shortest_round_trip() -> None:
    assert format_float(-4.2) == "-4.2"
    assert format_float(0.1) == "0.1"
    assert format_float(51.4712) == "51.4712"


def test_format_float_rejects_nan_and_inf() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        format_float(float("nan"))
    with pytest.raises(ValueError, match="non-finite"):
        format_float(float("inf"))


def test_format_field_value_renders_every_type() -> None:
    assert format_field_value(7) == "7i"
    assert format_field_value(0) == "0i"
    assert format_field_value(True) == "true"
    assert format_field_value(False) == "false"
    assert format_field_value(1.5) == "1.5"
    assert format_field_value('a,b "c"') == '"a,b \\"c\\""'


def test_to_line_full_point_sorted_tags_and_spec_field_order() -> None:
    point = Point(
        measurement="aircraft",
        tags={"hex": "4ca918", "flight": "RYR22GK", "squawk": "1200", "type": "B738"},
        fields={
            "lat": 51.4712,
            "lon": -0.4543,
            "alt_baro": 34000.0,
            "gs": 458.0,
            "track": 210.5,
            "rssi": -4.2,
            "messages": 100,
        },
        time_ns=1791028800000000000,
    )
    assert to_line(point) == (
        "aircraft,flight=RYR22GK,hex=4ca918,squawk=1200,type=B738 "
        "lat=51.4712,lon=-0.4543,alt_baro=34000.0,gs=458.0,track=210.5,"
        "rssi=-4.2,messages=100i 1791028800000000000"
    )


def test_to_line_without_timestamp() -> None:
    point = Point(measurement="aircraft", tags={"hex": "abc123"}, fields={"gs": 1.0})
    assert to_line(point) == "aircraft,hex=abc123 gs=1.0"


def test_to_line_without_tags() -> None:
    point = Point(measurement="m", tags={}, fields={"v": 1.0})
    assert to_line(point) == "m v=1.0"


def test_to_line_drops_empty_tag_values() -> None:
    point = Point(measurement="m", tags={"hex": "abc123", "flight": ""}, fields={"v": 1.0})
    assert to_line(point) == "m,hex=abc123 v=1.0"


def test_to_line_escapes_hostile_tag_keys_and_values() -> None:
    point = Point(measurement="m", tags={"a=b": "c d,e"}, fields={"v": 1.0})
    assert to_line(point) == "m,a\\=b=c\\ d\\,e v=1.0"


def test_to_line_escapes_hostile_field_keys() -> None:
    point = Point(measurement="m", tags={}, fields={"a b": 1.0})
    assert to_line(point) == "m a\\ b=1.0"
    tagged = Point(measurement="m", tags={"hex": "abc"}, fields={"a b": 1.0})
    assert to_line(tagged) == "m,hex=abc a\\ b=1.0"


def test_to_line_quotes_string_fields_containing_commas_and_spaces() -> None:
    point = Point(measurement="m", tags={}, fields={"note": "a, b"})
    assert to_line(point) == 'm note="a, b"'


def test_to_line_keeps_tilde_hex_values_verbatim() -> None:
    point = Point(measurement="aircraft", tags={"hex": "~a1b2c3"}, fields={"gs": 0.0})
    assert to_line(point) == "aircraft,hex=~a1b2c3 gs=0.0"


def test_to_line_without_fields_raises() -> None:
    with pytest.raises(InfluxError, match="no fields"):
        to_line(Point(measurement="aircraft", tags={"hex": "abc123"}, fields={}))


def test_timestamp_ns_converts_epoch_seconds() -> None:
    assert timestamp_ns(1.5) == 1_500_000_000
    assert timestamp_ns(0.0) == 0
    assert timestamp_ns(1791028800.0) == 1791028800000000000
