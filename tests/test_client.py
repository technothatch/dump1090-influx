"""Tests for the InfluxDB v2 write client: headers, query, retry and backoff.

The retry tests run against a stub ``http.server`` on loopback that answers a
scripted sequence (429 then 200, ...); nothing needs InfluxDB to be running.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from conftest import influx_stub
from dump1090_influx.client import write_lines, write_url
from dump1090_influx.errors import InfluxError

LINES = ["aircraft,hex=4ca918 gs=458.0 1791028800000000000", "aircraft,hex=abc123 gs=0.0"]


def write(stub_url: str, **kwargs: Any) -> int:
    options: dict[str, Any] = {
        "token": "t0k3n",
        "org": "my org",
        "bucket": "adsb",
        "lines": LINES,
        "max_retries": 4,
        "backoff_base": 0.5,
    }
    options.update(kwargs)
    return write_lines(stub_url, **options)


def test_write_url_quotes_org_and_bucket() -> None:
    url = write_url("http://localhost:8086", "my org", "ads b")
    assert url == "http://localhost:8086/api/v2/write?org=my+org&bucket=ads+b&precision=ns"


def test_write_url_strips_trailing_slash() -> None:
    url = write_url("http://localhost:8086/", "o", "b")
    assert url.startswith("http://localhost:8086/api/v2/write?")


def test_successful_write_sends_auth_query_and_body() -> None:
    with influx_stub([(204, {}, b"")]) as stub:
        assert write(stub.url) == 2
    request = stub.requests[0]
    assert request["path"] == "/api/v2/write?org=my+org&bucket=adsb&precision=ns"
    assert request["authorization"] == "Token t0k3n"
    assert request["content_type"] == "text/plain; charset=utf-8"
    assert request["body"] == "\n".join(LINES)


def test_empty_batch_never_touches_the_network() -> None:
    with influx_stub([(204, {}, b"")]) as stub:
        assert write(stub.url, lines=[]) == 0
    assert stub.requests == []


def test_missing_credentials_are_rejected_before_any_request() -> None:
    with influx_stub([(204, {}, b"")]) as stub:
        with pytest.raises(InfluxError, match="required"):
            write(stub.url, token="")
        with pytest.raises(InfluxError, match="required"):
            write(stub.url, org="")
        with pytest.raises(InfluxError, match="required"):
            write(stub.url, bucket="")
    assert stub.requests == []


def test_invalid_retry_settings_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        write("http://127.0.0.1:1", max_retries=-1)
    with pytest.raises(ValueError, match="backoff_base"):
        write("http://127.0.0.1:1", backoff_base=-0.1)


def test_429_then_204_retries_once_with_base_backoff() -> None:
    sleeps: list[float] = []
    with influx_stub([(429, {}, b"slow down"), (204, {}, b"")]) as stub:
        assert write(stub.url, sleep=sleeps.append) == 2
    assert len(stub.requests) == 2
    assert sleeps == [0.5]


def test_backoff_doubles_on_each_retry() -> None:
    sleeps: list[float] = []
    script = [(429, {}, b""), (503, {}, b""), (500, {}, b""), (204, {}, b"")]
    with influx_stub(script) as stub:
        assert write(stub.url, sleep=sleeps.append, max_retries=3) == 2
    assert sleeps == [0.5, 1.0, 2.0]
    assert len(stub.requests) == 4


def test_retry_after_header_wins_when_larger_than_backoff() -> None:
    sleeps: list[float] = []
    with influx_stub([(429, {"Retry-After": "3"}, b""), (204, {}, b"")]) as stub:
        write(stub.url, sleep=sleeps.append)
    assert sleeps == [3.0]


def test_unparseable_retry_after_falls_back_to_backoff() -> None:
    sleeps: list[float] = []
    stale = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    with influx_stub([(429, stale, b""), (204, {}, b"")]) as stub:
        write(stub.url, sleep=sleeps.append)
    assert sleeps == [0.5]


def test_retries_exhausted_raises_after_max_attempts() -> None:
    sleeps: list[float] = []
    with (
        influx_stub([(500, {}, b"boom")]) as stub,
        pytest.raises(InfluxError, match="HTTP 500"),
    ):
        write(stub.url, sleep=sleeps.append, max_retries=2)
    assert len(stub.requests) == 3  # first attempt + 2 retries
    assert sleeps == [0.5, 1.0]


def test_non_retryable_4xx_fails_immediately() -> None:
    sleeps: list[float] = []
    with (
        influx_stub([(400, {}, b"bad line")]) as stub,
        pytest.raises(InfluxError, match="HTTP 400"),
    ):
        write(stub.url, sleep=sleeps.append)
    assert len(stub.requests) == 1
    assert sleeps == []


def test_connection_errors_are_retried_then_reported() -> None:
    sleeps: list[float] = []
    attempts: list[object] = []

    def refusing_opener(_request: object, timeout: float = 0.0) -> object:
        attempts.append(timeout)
        raise urllib.error.URLError("connection refused")

    with pytest.raises(InfluxError, match="cannot reach"):
        write(
            "http://127.0.0.1:9",
            sleep=sleeps.append,
            opener=refusing_opener,
            max_retries=2,
        )
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0]


def test_retry_then_connection_error_then_success() -> None:
    sleeps: list[float] = []
    with influx_stub([(503, {}, b""), (204, {}, b"")]) as stub:
        assert write(stub.url, sleep=sleeps.append, max_retries=4) == 2
    assert sleeps == [0.5]


def test_hand_built_http_error_without_headers_still_backs_off() -> None:
    """A hand-built 429 with no headers must not crash the backoff logic."""
    sleeps: list[float] = []
    attempts: list[int] = []

    def opener(_request: object, timeout: float = 0.0) -> object:
        attempts.append(1)
        raise urllib.error.HTTPError("http://x", 429, "Too Many", None, None)

    with pytest.raises(InfluxError, match="HTTP 429"):
        write("http://127.0.0.1:9", sleep=sleeps.append, opener=opener, max_retries=1)
    assert len(attempts) == 2
    assert sleeps == [0.5]
