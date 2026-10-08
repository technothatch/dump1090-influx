"""Batch line-protocol points by size and by flush interval.

The batcher is deliberately dumb about I/O: it renders points to lines and
hands complete batches to an injected sink (InfluxDB HTTP, UDP or stdout). All
time comes from an injected monotonic clock, so the flush-interval behaviour is
fully deterministic under test.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from dump1090_influx.lineprotocol import Point, to_line

__all__ = ["Batcher"]

Sink = Callable[[list[str]], None]


class Batcher:
    """Accumulate points and flush them to ``sink`` as line batches.

    A flush happens when either

    - the buffer reaches ``batch_size`` lines (checked on every ``add``), or
    - ``flush_interval`` seconds of the injected clock elapsed since the last
      flush and ``flush_due`` is called (the main loop calls it once per poll),
    - or ``flush`` is called explicitly (end of run, Ctrl+C).

    Once a batch is handed to the sink it is considered delivered: if the sink
    raises after its own retries were exhausted, the exception propagates and
    the CLI exits non-zero rather than silently growing an unbounded buffer.
    """

    def __init__(
        self,
        sink: Sink,
        *,
        batch_size: int = 5000,
        flush_interval: float = 5.0,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if flush_interval <= 0:
            raise ValueError(f"flush_interval must be > 0, got {flush_interval}")
        self._sink = sink
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._mono = mono
        self._lines: list[str] = []
        self._last_flush = mono()
        self.batches = 0
        self.lines_flushed = 0

    @property
    def pending(self) -> int:
        """Number of lines waiting to be sent."""
        return len(self._lines)

    def add(self, point: Point) -> bool:
        """Queue one point; flush immediately if the batch is full.

        Returns:
            Whether this addition triggered a size-based flush.
        """
        self._lines.append(to_line(point))
        if len(self._lines) >= self._batch_size:
            self.flush()
            return True
        return False

    def flush_due(self) -> bool:
        """Flush when the flush interval has elapsed and lines are pending.

        Returns:
            Whether a batch was sent.
        """
        if self._lines and (self._mono() - self._last_flush) >= self._flush_interval:
            self.flush()
            return True
        return False

    def flush(self) -> int:
        """Send whatever is pending now.

        Returns:
            Number of lines handed to the sink (0 when the buffer was empty,
            in which case the sink is not called at all).
        """
        lines, self._lines = self._lines, []
        self._last_flush = self._mono()
        if not lines:
            return 0
        self._sink(lines)
        self.batches += 1
        self.lines_flushed += len(lines)
        return len(lines)
