"""UDP sink for a Telegraf ``socket_listener`` input.

Telegraf's ``socket_listener`` with ``data_format = "influx"`` consumes raw
line-protocol datagrams, so batches can be emitted as UDP packets instead of
talking to InfluxDB directly - useful when Telegraf already owns buffering,
routing and organisation of the writes.

Datagrams are packed line by line and split before ``MAX_DATAGRAM`` bytes so a
full batch never exceeds the UDP payload limit; a single oversized line is sent
alone (it is the caller's problem at that point).
"""

from __future__ import annotations

import socket

from dump1090_influx.errors import InfluxError

__all__ = ["MAX_DATAGRAM", "UdpSink", "parse_endpoint"]

MAX_DATAGRAM = 65507  # maximum theoretical UDP payload (65535 - IP/UDP headers)
DEFAULT_PORT = 8094


def parse_endpoint(raw: str) -> tuple[str, int]:
    """Parse ``host:port`` (or ``[v6-address]:port``) into a socket address.

    Raises:
        ValueError: The endpoint is missing a port or has an out-of-range port.
    """
    text = raw.strip()
    if not text:
        raise ValueError("endpoint is empty, expected host:port")
    if text.startswith("["):
        close = text.find("]")
        if close == -1 or text[close + 1 : close + 2] != ":":
            raise ValueError(f"{raw!r}: expected [ipv6]:port")
        host, port_text = text[1:close], text[close + 2 :]
    elif ":" not in text:
        raise ValueError(f"{raw!r}: expected host:port")
    else:
        host, _, port_text = text.rpartition(":")
    if not host:
        raise ValueError(f"{raw!r}: missing host")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"{raw!r}: invalid port {port_text!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{raw!r}: port must be 1-65535, got {port}")
    return host, port


def packetise(lines: list[str], max_datagram: int) -> list[bytes]:
    """Pack lines into newline-joined datagrams of at most ``max_datagram`` bytes."""
    if max_datagram < 1:
        raise ValueError(f"max_datagram must be >= 1, got {max_datagram}")
    packets: list[bytes] = []
    current: list[str] = []
    size = 0
    for line in lines:
        encoded = len(line.encode("utf-8"))
        extra = encoded + (1 if current else 0)
        if current and size + extra > max_datagram:
            packets.append("\n".join(current).encode("utf-8"))
            current, size = [line], encoded
        else:
            current.append(line)
            size += extra
    if current:
        packets.append("\n".join(current).encode("utf-8"))
    return packets


class UdpSink:
    """Callable sink sending batches as UDP line-protocol datagrams."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        max_datagram: int = MAX_DATAGRAM,
        sock: socket.socket | None = None,
    ) -> None:
        if not 1 <= port <= 65535:
            raise ValueError(f"port must be 1-65535, got {port}")
        self.host = host
        self.port = port
        self._max_datagram = max_datagram
        self._closed = False
        self._sock = sock if sock is not None else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.packets_sent = 0

    def send(self, lines: list[str]) -> int:
        """Send one batch, split across as many datagrams as needed.

        Returns:
            Number of datagrams sent.

        Raises:
            InfluxError: The socket was closed or the OS rejected the send.
        """
        if not lines:
            return 0
        if self._closed:
            raise InfluxError("UDP sink is closed")
        sent = 0
        try:
            for packet in packetise(lines, self._max_datagram):
                self._sock.sendto(packet, (self.host, self.port))
                sent += 1
        except OSError as exc:
            raise InfluxError(f"cannot send UDP to {self.host}:{self.port}: {exc}") from exc
        self.packets_sent += sent
        return sent

    def __call__(self, lines: list[str]) -> None:
        self.send(lines)

    def close(self) -> None:
        """Close the underlying socket (idempotent)."""
        if not self._closed:
            self._closed = True
            self._sock.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> UdpSink:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
