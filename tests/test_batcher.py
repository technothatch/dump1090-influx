"""Tests for batching by size and by flush interval with a fake clock."""

from __future__ import annotations

import pytest

from conftest import FakeClock
from dump1090_influx.batch import Batcher
from dump1090_influx.lineprotocol import Point


class RecordingSink:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def __call__(self, lines: list[str]) -> None:
        self.batches.append(list(lines))


def point(i: int) -> Point:
    return Point(measurement="aircraft", tags={"hex": f"{i:06x}"}, fields={"gs": float(i)})


def test_no_flush_below_batch_size(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, batch_size=3, flush_interval=60.0, mono=fake_clock.mono)
    assert batcher.add(point(1)) is False
    assert batcher.add(point(2)) is False
    assert sink.batches == []
    assert batcher.pending == 2


def test_flush_when_batch_size_reached(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, batch_size=2, flush_interval=60.0, mono=fake_clock.mono)
    batcher.add(point(1))
    assert batcher.add(point(2)) is True
    assert sink.batches == [["aircraft,hex=000001 gs=1.0", "aircraft,hex=000002 gs=2.0"]]
    assert batcher.pending == 0
    assert batcher.batches == 1
    assert batcher.lines_flushed == 2


def test_flush_interval_not_elapsed_does_not_flush(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, batch_size=100, flush_interval=5.0, mono=fake_clock.mono)
    batcher.add(point(1))
    fake_clock.sleep(4.999)
    assert batcher.flush_due() is False
    assert sink.batches == []


def test_flush_interval_elapsed_flushes_pending(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, batch_size=100, flush_interval=5.0, mono=fake_clock.mono)
    batcher.add(point(1))
    fake_clock.sleep(5.0)
    assert batcher.flush_due() is True
    assert sink.batches == [["aircraft,hex=000001 gs=1.0"]]


def test_flush_due_without_pending_lines_never_flushes(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, batch_size=100, flush_interval=1.0, mono=fake_clock.mono)
    fake_clock.sleep(10.0)
    assert batcher.flush_due() is False
    assert sink.batches == []


def test_flush_resets_the_interval(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, batch_size=100, flush_interval=5.0, mono=fake_clock.mono)
    batcher.add(point(1))
    fake_clock.sleep(5.0)
    assert batcher.flush_due() is True
    batcher.add(point(2))
    fake_clock.sleep(4.999)
    assert batcher.flush_due() is False
    fake_clock.sleep(0.001)
    assert batcher.flush_due() is True
    assert len(sink.batches) == 2


def test_explicit_flush_with_empty_buffer_skips_sink(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, mono=fake_clock.mono)
    assert batcher.flush() == 0
    assert sink.batches == []
    assert batcher.batches == 0


def test_explicit_flush_sends_everything_pending(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, batch_size=1000, flush_interval=60.0, mono=fake_clock.mono)
    for i in range(4):
        batcher.add(point(i + 1))
    assert batcher.flush() == 4
    assert sink.batches == [
        [
            "aircraft,hex=000001 gs=1.0",
            "aircraft,hex=000002 gs=2.0",
            "aircraft,hex=000003 gs=3.0",
            "aircraft,hex=000004 gs=4.0",
        ]
    ]
    assert batcher.pending == 0
    assert batcher.flush() == 0


def test_size_flushes_interleave_with_time_flushes(fake_clock: FakeClock) -> None:
    sink = RecordingSink()
    batcher = Batcher(sink, batch_size=2, flush_interval=10.0, mono=fake_clock.mono)
    batcher.add(point(1))
    batcher.add(point(2))  # size flush
    batcher.add(point(3))
    fake_clock.sleep(10.0)
    batcher.flush_due()  # time flush
    batcher.add(point(4))
    batcher.flush()  # explicit flush
    assert [len(batch) for batch in sink.batches] == [2, 1, 1]
    assert batcher.batches == 3
    assert batcher.lines_flushed == 4


def test_sink_failure_propagates_and_buffer_is_not_stuck(fake_clock: FakeClock) -> None:
    def boom(_lines: list[str]) -> None:
        raise RuntimeError("sink exploded")

    batcher = Batcher(boom, batch_size=1, flush_interval=60.0, mono=fake_clock.mono)
    with pytest.raises(RuntimeError, match="sink exploded"):
        batcher.add(point(1))
    assert batcher.pending == 0
    assert batcher.batches == 0
    assert batcher.lines_flushed == 0


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        Batcher(lambda _lines: None, batch_size=0)
    with pytest.raises(ValueError, match="flush_interval"):
        Batcher(lambda _lines: None, flush_interval=0.0)
    with pytest.raises(ValueError, match="flush_interval"):
        Batcher(lambda _lines: None, flush_interval=-1.0)
