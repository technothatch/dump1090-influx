"""Command-line interface for dump1090-influx."""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

from dump1090_influx import __version__
from dump1090_influx.batch import Batcher
from dump1090_influx.client import DEFAULT_URL, write_lines
from dump1090_influx.errors import InfluxError
from dump1090_influx.lineprotocol import aircraft_to_point
from dump1090_influx.ndjson import Record, read_ndjson
from dump1090_influx.record import (
    DEFAULT_HOST,
    fetch_aircraft,
    record,
    resolve_url,
    snapshot_record,
)
from dump1090_influx.udp import UdpSink, parse_endpoint

EXIT_OK = 0
EXIT_ERROR = 1
DEFAULT_BUCKET = "adsb"

__all__ = ["DEFAULT_BUCKET", "EXIT_ERROR", "EXIT_OK", "main"]


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"'{raw}' is not an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {raw}")
    return value


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"'{raw}' is not an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {raw}")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"'{raw}' is not a number") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {raw}")
    return value


def _non_negative_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"'{raw}' is not a number") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {raw}")
    return value


def _endpoint(raw: str) -> tuple[str, int]:
    try:
        return parse_endpoint(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dump1090-influx",
        description=(
            "Stream dump1090 / readsb aircraft positions into InfluxDB 2.x line protocol "
            "or a Telegraf socket_listener - batched, stdlib only."
        ),
        epilog="ADS-B data is public by design; nothing here contacts the network unless asked to.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="record a live decoder into an NDJSON file")
    rec.add_argument(
        "--url",
        metavar="URL",
        default=None,
        help=(f"decoder base URL or aircraft.json URL (env DUMP1090_URL, default {DEFAULT_HOST})"),
    )
    rec.add_argument(
        "--interval",
        type=_positive_float,
        default=1.0,
        metavar="SECS",
        help="seconds between polls (default: 1)",
    )
    rec.add_argument(
        "--out", metavar="PATH", required=True, help="NDJSON file to write (truncated)"
    )
    rec.add_argument(
        "--timeout", type=_positive_float, default=5.0, metavar="SECS", help="HTTP timeout"
    )
    rec.add_argument(
        "--max-polls",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="stop after N polls (0 = run until interrupted)",
    )

    write = sub.add_parser("write", help="batch aircraft points into InfluxDB or Telegraf")
    write.add_argument(
        "file",
        metavar="NDJSON",
        nargs="?",
        default=None,
        help="replay a recording instead of polling a decoder live",
    )
    write.add_argument(
        "--url",
        metavar="URL",
        default=None,
        help=f"InfluxDB base URL (env INFLUX_URL, default {DEFAULT_URL})",
    )
    write.add_argument(
        "--token", metavar="TOKEN", default=None, help="InfluxDB v2 API token (env INFLUX_TOKEN)"
    )
    write.add_argument(
        "--org", metavar="ORG", default=None, help="InfluxDB v2 organisation (env INFLUX_ORG)"
    )
    write.add_argument(
        "--bucket",
        metavar="BUCKET",
        default=None,
        help=f"InfluxDB v2 bucket (env INFLUX_BUCKET, default {DEFAULT_BUCKET})",
    )
    write.add_argument(
        "--decoder",
        metavar="URL",
        default=None,
        help=f"decoder base URL for live mode (env DUMP1090_URL, default {DEFAULT_HOST})",
    )
    write.add_argument(
        "--interval",
        type=_positive_float,
        default=1.0,
        metavar="SECS",
        help="seconds between live polls (default: 1)",
    )
    write.add_argument(
        "--batch-size",
        type=_positive_int,
        default=5000,
        metavar="N",
        help="flush after N lines (default: 5000)",
    )
    write.add_argument(
        "--flush-interval",
        type=_positive_float,
        default=5.0,
        metavar="SECS",
        help="flush pending lines after SECS (default: 5)",
    )
    sinks = write.add_mutually_exclusive_group()
    sinks.add_argument(
        "--dry-run",
        action="store_true",
        help="print line protocol to stdout instead of sending it",
    )
    sinks.add_argument(
        "--telegraf-udp",
        type=_endpoint,
        metavar="HOST:PORT",
        default=None,
        help="send UDP datagrams for a Telegraf socket_listener (env TELEGRAF_UDP)",
    )
    write.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        metavar="SECS",
        help="HTTP timeout per request (default: 10)",
    )
    write.add_argument(
        "--max-retries",
        type=_non_negative_int,
        default=4,
        metavar="N",
        help="retries on HTTP 429/5xx after the first attempt (default: 4)",
    )
    write.add_argument(
        "--backoff-base",
        type=_non_negative_float,
        default=0.5,
        metavar="SECS",
        help="first retry delay, doubles per retry (default: 0.5)",
    )
    write.add_argument(
        "--max-polls",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="stop live mode after N polls (0 = run until interrupted)",
    )
    return parser


def _queue(batcher: Batcher, record_: Record) -> None:
    for entry in record_.aircraft:
        point = aircraft_to_point(entry, record_.t)
        if point is not None:
            batcher.add(point)


def _print_sink(lines: list[str]) -> None:
    for line in lines:
        print(line)


def _cmd_record(
    args: argparse.Namespace,
    *,
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> int:
    url = args.url if args.url is not None else os.environ.get("DUMP1090_URL", DEFAULT_HOST)
    endpoint = resolve_url(url)
    print(f"# recording {endpoint} every {args.interval}s -> {args.out}", file=sys.stderr)
    try:
        written = record(
            endpoint,
            out=args.out,
            interval=args.interval,
            max_polls=args.max_polls,
            timeout=args.timeout,
            opener=opener,
            sleep=sleep,
            now=now,
        )
    except InfluxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"# wrote {written} snapshot(s) to {args.out}", file=sys.stderr)
    if not written:
        print("error: no snapshots captured", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _poll_live(
    args: argparse.Namespace,
    batcher: Batcher,
    *,
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> int:
    endpoint = resolve_url(
        args.decoder if args.decoder is not None else os.environ.get("DUMP1090_URL", DEFAULT_HOST)
    )
    print(
        f"# polling {endpoint} every {args.interval}s, "
        f"flush {args.batch_size} lines / {args.flush_interval}s",
        file=sys.stderr,
    )
    polls = 0
    successes = 0
    while True:
        polls += 1
        try:
            doc = fetch_aircraft(endpoint, timeout=args.timeout, opener=opener)
        except InfluxError as exc:
            print(f"warning: {exc}", file=sys.stderr)
        else:
            successes += 1
            _queue(batcher, snapshot_record(doc, wall=now()))
            batcher.flush_due()
        if args.max_polls and polls >= args.max_polls:
            break
        sleep(args.interval)
    if not successes:
        print(f"error: no successful poll of {endpoint}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _cmd_write(
    args: argparse.Namespace,
    *,
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
    mono: Callable[[], float],
    now: Callable[[], float],
) -> int:
    udp: UdpSink | None = None
    if args.dry_run:
        sink: Callable[[list[str]], None] = _print_sink
    else:
        udp_target = args.telegraf_udp
        if udp_target is None:
            env_target = os.environ.get("TELEGRAF_UDP")
            if env_target:
                try:
                    udp_target = parse_endpoint(env_target)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return EXIT_ERROR
        if udp_target is not None:
            udp = UdpSink(*udp_target)
            sink = udp
        else:
            token = args.token if args.token is not None else os.environ.get("INFLUX_TOKEN", "")
            org = args.org if args.org is not None else os.environ.get("INFLUX_ORG", "")
            if not token or not org:
                print(
                    "error: InfluxDB writes need --token and --org "
                    "(or INFLUX_TOKEN / INFLUX_ORG); use --dry-run to preview "
                    "line protocol or --telegraf-udp to hand off to Telegraf",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            bucket = (
                args.bucket
                if args.bucket is not None
                else os.environ.get("INFLUX_BUCKET", "") or DEFAULT_BUCKET
            )
            endpoint = args.url if args.url is not None else os.environ.get("INFLUX_URL", "")

            def sink(lines: list[str]) -> None:
                write_lines(
                    endpoint or DEFAULT_URL,
                    token=token,
                    org=org,
                    bucket=bucket,
                    lines=lines,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                    backoff_base=args.backoff_base,
                    sleep=sleep,
                )

    batcher = Batcher(
        sink,
        batch_size=args.batch_size,
        flush_interval=args.flush_interval,
        mono=mono,
    )
    exit_code = EXIT_OK
    try:
        if args.file is not None:
            try:
                records = read_ndjson(args.file)
            except InfluxError as exc:
                print(f"error: {exc}", file=sys.stderr)
                exit_code = EXIT_ERROR
            else:
                for record_ in records:
                    _queue(batcher, record_)
        else:
            exit_code = _poll_live(args, batcher, opener=opener, sleep=sleep, now=now)
    except KeyboardInterrupt:
        print("\n# interrupted", file=sys.stderr)
    except InfluxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = EXIT_ERROR
    finally:
        if exit_code == EXIT_OK:
            try:
                batcher.flush()
            except InfluxError as exc:
                print(f"error: {exc}", file=sys.stderr)
                exit_code = EXIT_ERROR
        if udp is not None:
            udp.close()

    print(
        f"# {batcher.lines_flushed} point(s) in {batcher.batches} batch(es)",
        file=sys.stderr,
    )
    return exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    mono: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
) -> int:
    """Entry point. Returns a process exit code.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).
        opener: Injectable HTTP opener for tests.
        sleep: Injectable sleep for tests (polling and retry backoff).
        mono: Injectable monotonic clock for flush-interval batching (tests).
        now: Injectable wall clock for fallback snapshot timestamps (tests).
    """
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    http_opener = opener if opener is not None else urllib.request.urlopen

    if args.command == "record":
        return _cmd_record(args, opener=http_opener, sleep=sleep, now=now)
    return _cmd_write(args, opener=http_opener, sleep=sleep, mono=mono, now=now)


if __name__ == "__main__":
    sys.exit(main())
