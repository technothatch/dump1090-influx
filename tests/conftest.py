"""Shared test fixtures - inline ADS-B data, a fake clock and a stub InfluxDB.

No test ever talks to a decoder or to InfluxDB: every snapshot is written
inline and the only sockets are loopback stubs bound to port 0.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from dump1090_influx.ndjson import Record, write_ndjson

#: 2026-10-03 12:00:00 UTC - same epoch family as the ecosystem demo files.
T0 = 1791028800.0

#: (status, headers, body) - what the stub InfluxDB answers per request.
Response = tuple[int, dict[str, str], bytes]


def aircraft(**fields: Any) -> dict[str, Any]:
    """One realistic aircraft object (SPEC.md field list subset)."""
    base: dict[str, Any] = {
        "hex": "4ca918",
        "flight": "RYR22GK ",
        "type": "B738",
        "alt_baro": 34000,
        "gs": 458,
        "track": 210.5,
        "squawk": "1200",
        "emergency": "none",
        "lat": 51.4712,
        "lon": -0.4543,
        "seen": 0.5,
        "seen_pos": 0.7,
        "messages": 100,
        "rssi": -4.2,
    }
    base.update(fields)
    return base


def aircraft_doc(*entries: dict[str, Any], now: float = T0, messages: int = 1611) -> dict[str, Any]:
    """A full ``aircraft.json`` document around the given entries."""
    return {"now": now, "messages": messages, "aircraft": list(entries)}


def json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def make_records(count: int = 3, *, t0: float = T0, gap: float = 1.0) -> list[Record]:
    """``count`` records ``gap`` seconds apart, one aircraft each."""
    return [
        Record(
            t=t0 + i * gap,
            aircraft=[aircraft(hex=f"{i:06x}", flight=f"TEST{i:03d}  ", messages=100 + i)],
        )
        for i in range(count)
    ]


class FakeClock:
    """A monotonic clock advanced only by its own ``sleep`` (fully deterministic)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start
        self.sleeps: list[float] = []

    def mono(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds

    @property
    def slept(self) -> float:
        return sum(self.sleeps)


class FakeResponse:
    """Minimal context-managed HTTP response (for injected openers)."""

    def __init__(self, body: bytes = b"") -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class StubInflux:
    """A scripted InfluxDB: one response per request, last response repeats."""

    def __init__(self, responses: list[Response]) -> None:
        if not responses:
            raise ValueError("stub needs at least one response")
        self._responses = list(responses)
        self.requests: list[dict[str, str]] = []
        self.url = ""

    def next_response(self) -> Response:
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        stub: StubInflux = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        stub.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "content_type": self.headers.get("Content-Type", ""),
                "body": body.decode("utf-8"),
            }
        )
        status, headers, payload = stub.next_response()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass  # keep test output quiet


@contextmanager
def influx_stub(responses: list[Response]) -> Iterator[StubInflux]:
    """Run a loopback HTTP server that answers ``responses`` in order."""
    stub = StubInflux(responses)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.stub = stub  # type: ignore[attr-defined]
    stub.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def records() -> list[Record]:
    return make_records()


@pytest.fixture
def ndjson_file(tmp_path: Path, records: list[Record]) -> Path:
    path = tmp_path / "flight.ndjson"
    write_ndjson(path, records)
    return path
