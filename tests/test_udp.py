"""Tests for the Telegraf UDP sink: payload format, packetising, endpoints."""

from __future__ import annotations

import socket

import pytest

from dump1090_influx.errors import InfluxError
from dump1090_influx.udp import UdpSink, packetise, parse_endpoint


def receiver() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5.0)
    return sock


def recv(sock: socket.socket) -> bytes:
    data, _addr = sock.recvfrom(65535)
    return data


def test_udp_payload_is_newline_joined_line_protocol() -> None:
    sock = receiver()
    try:
        port = sock.getsockname()[1]
        with UdpSink("127.0.0.1", port) as sink:
            sink(["aircraft,hex=4ca918 gs=458.0 1", "aircraft,hex=abc123 gs=0.0 2"])
        assert recv(sock) == b"aircraft,hex=4ca918 gs=458.0 1\naircraft,hex=abc123 gs=0.0 2"
    finally:
        sock.close()


def test_sink_is_callable_and_counts_packets() -> None:
    sock = receiver()
    try:
        port = sock.getsockname()[1]
        sink = UdpSink("127.0.0.1", port)
        sink(["m,hex=a v=1.0"])
        assert sink.packets_sent == 1
        sink.close()
    finally:
        sock.close()


def test_oversized_batch_is_split_across_datagrams() -> None:
    sock = receiver()
    try:
        port = sock.getsockname()[1]
        lines = [f"aircraft,hex={i:06x} gs={i}.0 1791028800000000000" for i in range(10)]
        with UdpSink("127.0.0.1", port, max_datagram=90) as sink:
            sent = sink.send(lines)
        assert sent >= 2
        received = []
        for _ in range(sent):
            received.append(recv(sock).decode("utf-8"))
        assert "\n".join(received) == "\n".join(lines)
    finally:
        sock.close()


def test_empty_batch_sends_nothing() -> None:
    sock = receiver()
    try:
        port = sock.getsockname()[1]
        with UdpSink("127.0.0.1", port) as sink:
            assert sink.send([]) == 0
        assert sink.packets_sent == 0
        sock.settimeout(0.2)
        with pytest.raises(TimeoutError):
            recv(sock)
    finally:
        sock.close()


def test_single_line_larger_than_limit_is_sent_alone() -> None:
    lines = ["x" * 200, "y" * 10]
    packets = packetise(lines, max_datagram=50)
    assert [len(packet) for packet in packets] == [200, 10]


def test_packetise_rejects_nonsense_limit() -> None:
    with pytest.raises(ValueError, match="max_datagram"):
        packetise(["x"], max_datagram=0)


def test_closed_sink_refuses_further_sends() -> None:
    sink = UdpSink("127.0.0.1", 9)
    sink.close()
    assert sink.closed is True
    sink.close()  # idempotent
    with pytest.raises(InfluxError, match="closed"):
        sink.send(["m v=1.0"])


def test_os_send_errors_are_wrapped_as_influx_errors() -> None:
    sink = UdpSink("127.0.0.1", 9)
    sink._sock.close()  # underlying socket dies behind the sink's back
    with pytest.raises(InfluxError, match="cannot send UDP"):
        sink.send(["m v=1.0"])


def test_invalid_port_rejected_on_construction() -> None:
    with pytest.raises(ValueError, match="port"):
        UdpSink("127.0.0.1", 0)
    with pytest.raises(ValueError, match="port"):
        UdpSink("127.0.0.1", 70000)


def test_parse_endpoint_accepts_host_and_bracketed_ipv6() -> None:
    assert parse_endpoint("localhost:8094") == ("localhost", 8094)
    assert parse_endpoint("  10.0.0.5:1234 ") == ("10.0.0.5", 1234)
    assert parse_endpoint("[::1]:9999") == ("::1", 9999)


@pytest.mark.parametrize(
    "raw",
    ["", "nohost", ":8094", "host:", "host:notaport", "host:0", "host:70000", "[::1]8094"],
)
def test_parse_endpoint_rejects_malformed_values(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_endpoint(raw)
