# dump1090-influx

[![CI](https://github.com/technothatch/dump1090-influx/actions/workflows/ci.yml/badge.svg)](https://github.com/technothatch/dump1090-influx/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/technothatch/dump1090-influx/blob/main/LICENSE)
[![Latest Release](https://img.shields.io/github/v/release/technothatch/dump1090-influx)](https://github.com/technothatch/dump1090-influx/releases)
[![Issues](https://img.shields.io/github/issues/technothatch/dump1090-influx)](https://github.com/technothatch/dump1090-influx/issues)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/technothatch/dump1090-influx/pulls)
[![Code size](https://img.shields.io/github/languages/code-size/technothatch/dump1090-influx)](https://github.com/technothatch/dump1090-influx)
[![Last commit](https://img.shields.io/github/last-commit/technothatch/dump1090-influx)](https://github.com/technothatch/dump1090-influx/commits/main)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen.svg)](https://github.com/technothatch/dump1090-influx)

Stream aircraft positions from [dump1090](https://github.com/flightaware/dump1090) /
[readsb](https://github.com/wiedehopf/readsb) into InfluxDB 2.x so every tail, callsign
and squawk you have ever seen becomes a queryable time series. `dump1090-influx` polls
`/data/aircraft.json`, encodes each aircraft as a properly escaped line-protocol point
(measurement `aircraft`, tags `hex`/`flight`/`type`/`squawk`, numeric fields plus a
uint `messages` counter), batches it and POSTs it to the v2 `/api/v2/write` API with
retry and exponential backoff — or ships the same batch as UDP datagrams to a Telegraf
`socket_listener` if Telegraf already owns your writes. It also records and replays the
shared `{"t": <epoch>, "aircraft": [...]}` NDJSON format used across this ecosystem.
One stdlib-only Python process: `urllib.request` for HTTP, `socket` for UDP, zero
runtime dependencies.

## Features

- **`record`** polls a live decoder and writes one NDJSON line per snapshot —
  `{"t": <epoch>, "aircraft": [...]}` — the exact convention shared with
  [dump1090-replay](https://github.com/technothatch/dump1090-replay) and
  [dump1090-recorder](https://github.com/technothatch/dump1090-recorder).
- **`write`** batches points into InfluxDB 2.x via `/api/v2/write` with
  `Authorization: Token ...`, organisation/bucket query parameters and nanosecond
  precision (recorded snapshots land at their original capture time).
- **Correct line protocol, on purpose**: measurement/tag/field-key escaping of
  spaces, commas, `=` and backslashes; quoted and escaped string fields; shortest
  round-trip float formatting (`34000.0`, `-4.2`); `messages` written as a uint
  (`messages=1204i`); non-ICAO `~a0b1c2` hex addresses pass through verbatim.
- **`alt_baro: "ground"` is coerced to `0.0`** (documented, consistent with
  dump1090-alerts) so a parked aircraft keeps its place in the altitude series
  instead of breaking the field's numeric type — any other non-numeric value is
  skipped, as are NaN/Infinity, so one bad sample never poisons a batch.
- **Batching by size and by time**: flush at `--batch-size` lines (default 5000)
  or every `--flush-interval` seconds (default 5), with the clock injected so the
  timing logic is deterministic under test.
- **Retry with exponential backoff** on HTTP 429 and 5xx (and connection errors):
  `backoff_base * 2**attempt`, raised to the server's `Retry-After` when it is
  larger; any other 4xx fails immediately with a clear message.
- **`--telegraf-udp HOST:PORT`** emits the same batch as newline-packed UDP
  datagrams (split before the 65507-byte limit) for a Telegraf `socket_listener`
  with `data_format = "influx"`.
- **`--dry-run`** prints the exact line protocol instead of sending it — usable
  without a decoder, without InfluxDB and without network access.
- **Zero runtime dependencies** — `urllib.request`, `socket`, `argparse`, `json`.
  Fully typed (`py.typed`), 149 tests at 99% coverage.

## Install

```bash
pip install dump1090-influx
```

Or from source:

```bash
git clone https://github.com/technothatch/dump1090-influx.git
cd dump1090-influx
pip install -e ".[dev]"
```

Requires Python 3.11+.

## Quickstart

A four-second capture ships with the repo, so the first two steps need neither
a decoder nor InfluxDB.

**Preview the line protocol:**

```console
$ dump1090-influx write examples/demo.ndjson --dry-run | head -3
aircraft,flight=RYR22GK,hex=4ca918,squawk=1200,type=B738 lat=51.4712,lon=-0.4543,alt_baro=34000.0,gs=458.0,track=210.5,rssi=-4.2,messages=1204i 1791028800000000000
aircraft,flight=BAW8214,hex=3c673a,squawk=1200,type=E190 lat=51.4705,lon=-0.4619,alt_baro=0.0,gs=0.0,track=90.0,rssi=-13.5,messages=302i 1791028800000000000
aircraft,hex=~a0b1c2,squawk=7700 alt_baro=15000.0,gs=320.0,rssi=-19.8,messages=47i 1791028800000000000
```

Tags are sorted, `alt_baro: "ground"` became `alt_baro=0.0`, `messages` carries the
`i` uint suffix, the third aircraft has no position so only its numeric fields are
written, and its `~` hex is untouched. The summary goes to stderr:

```console
$ dump1090-influx write examples/demo.ndjson --dry-run > /dev/null
# 12 point(s) in 1 batch(es)
```

**Write to InfluxDB 2.x** (this part needs a bucket and a token):

```console
$ export INFLUX_TOKEN=<token> INFLUX_ORG=<org>
$ dump1090-influx write --url http://localhost:8086 --bucket adsb
# polling http://localhost:8080/data/aircraft.json every 1.0s, flush 5000 lines / 5.0s
^C
# interrupted
# 3684 point(s) in 2 batch(es)
```

**Record now, write later** (also needs a decoder):

```console
$ dump1090-influx record --url http://readsb.lan:8080 --interval 1 --out flight.ndjson
# recording http://readsb.lan:8080/data/aircraft.json every 1.0s -> flight.ndjson
^C
# interrupted
# wrote 1842 snapshot(s) to flight.ndjson
$ dump1090-influx write flight.ndjson --bucket adsb
# 5526 point(s) in 2 batch(es)
```

**Hand the batch to Telegraf instead:**

```console
$ dump1090-influx write examples/demo.ndjson --telegraf-udp localhost:8094
# 12 point(s) in 1 batch(es)
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success (Ctrl+C stops cleanly after flushing what is pending) |
| `1` | Runtime failure: unwritable output, missing token/org, decoder never reachable, batch rejected after retries |
| `2` | Argument error (bad `--batch-size`, unknown flag, missing subcommand, malformed `--telegraf-udp`) |

## Configuration reference

Every flag is also settable from the environment; flags win over environment
variables, which win over the defaults.

### `dump1090-influx record`

| Flag | Environment | Meaning | Default |
| --- | --- | --- | --- |
| `--url URL` | `DUMP1090_URL` | Decoder base URL or full `aircraft.json` URL | `http://localhost:8080` |
| `--interval SECS` | — | Seconds between polls | `1.0` |
| `--out PATH` | — | NDJSON file to write (truncated) | required |
| `--timeout SECS` | — | HTTP timeout per poll | `5.0` |
| `--max-polls N` | — | Stop after N polls (`0` = until interrupted) | `0` |

### `dump1090-influx write`

| Flag | Environment | Meaning | Default |
| --- | --- | --- | --- |
| `FILE` | — | NDJSON recording to replay (positional); omit for live polling | live mode |
| `--url URL` | `INFLUX_URL` | **InfluxDB** base URL | `http://localhost:8086` |
| `--token TOKEN` | `INFLUX_TOKEN` | InfluxDB v2 API token (required unless `--dry-run`/`--telegraf-udp`) | — |
| `--org ORG` | `INFLUX_ORG` | InfluxDB v2 organisation (required unless `--dry-run`/`--telegraf-udp`) | — |
| `--bucket BUCKET` | `INFLUX_BUCKET` | InfluxDB v2 bucket | `adsb` |
| `--decoder URL` | `DUMP1090_URL` | **Decoder** base URL for live mode | `http://localhost:8080` |
| `--interval SECS` | — | Seconds between live polls | `1.0` |
| `--batch-size N` | — | Flush when N lines are pending | `5000` |
| `--flush-interval SECS` | — | Flush pending lines after SECS since the last flush | `5.0` |
| `--dry-run` | — | Print line protocol to stdout instead of sending | off |
| `--telegraf-udp HOST:PORT` | `TELEGRAF_UDP` | Send UDP datagrams for a Telegraf `socket_listener` | off |
| `--timeout SECS` | — | HTTP timeout per write request | `10.0` |
| `--max-retries N` | — | Retries after the first attempt on 429/5xx/connection errors | `4` |
| `--backoff-base SECS` | — | First retry delay; doubles each retry, honours `Retry-After` | `0.5` |
| `--max-polls N` | — | Stop live mode after N polls (`0` = until interrupted) | `0` |

`--dry-run` and `--telegraf-udp` are mutually exclusive; an explicit `--dry-run`
also overrides a `TELEGRAF_UDP` environment variable.

### The measurement

| Part | Keys | Notes |
| --- | --- | --- |
| measurement | `aircraft` | constant |
| tags | `hex`, `flight`, `type`, `squawk` | space-padded callsigns stripped, empty values dropped; `type` falls back to dump1090-fa's `t` |
| fields (float) | `lat`, `lon`, `alt_baro`, `gs`, `track`, `rssi` | always written with a decimal point so the field type never changes; missing/NaN/Inf skipped |
| field (uint) | `messages` | `i` suffix, e.g. `messages=1204i`; negative/boolean counts skipped |

Points are timestamped in nanoseconds from the snapshot's `now`, so replaying an
old recording writes history, not wall-clock garbage.

### The NDJSON format

One JSON object per line, UTF-8, LF-terminated — written by `record` and read by
`write` (and by dump1090-replay / dump1090-recorder):

```json
{"t": 1791028800.0, "aircraft": [{"hex": "4ca918", "flight": "RYR22GK ", "alt_baro": 34000, "seen": 0.5, "...": "..."}]}
```

`t` is unix epoch seconds (the decoder's own `now`); `aircraft` is the raw array
with every field preserved verbatim, including `seen`/`seen_pos` ages and
`alt_baro` values that may be the string `"ground"`.

### Telegraf `socket_listener`

Instead of writing to InfluxDB directly, let Telegraf batch, route and authorise
the writes:

```toml
# telegraf.conf
[[inputs.socket_listener]]
  service_address = "udp://:8094"
  data_format = "influx"
  read_buffer_size = 65536

[[outputs.influxdb_v2]]
  urls = ["http://localhost:8086"]
  token = "$INFLUX_TOKEN"
  organization = "my-org"
  bucket = "adsb"
```

```bash
telegraf --config telegraf.conf
dump1090-influx write --telegraf-udp localhost:8094   # live, or append a FILE
```

`socket_listener` with `data_format = "influx"` consumes the datagrams as-is:
newline-joined lines, one measurement per point, split before 65507 bytes so a
full batch never exceeds the UDP payload limit.

## How it works

```
  dump1090 / readsb                          InfluxDB 2.x
  /data/aircraft.json                             ▲
          │                                       │ POST /api/v2/write
          │  record      flight.ndjson            │   (Token auth, precision=ns,
          ├───────────▶ {"t", "aircraft"} ─┐      │    retry on 429/5xx with
          │                               ▼      │    exponential backoff)
          │  write (live)            ┌─────────────────┐
          └─────────────────────────▶│ cli.py          │
                                     │  ndjson.py ─────┼──▶ lineprotocol.py
                                     │  batch.py       │    aircraft_to_point()
                                     │  size + interval│    to_line() escaping
                                     └───┬─────────┬───┘
                                  HTTP   │         │  UDP
                              client.py ─┘         └─ udp.py ─▶ Telegraf
                                                          socket_listener :8094
```

- **`lineprotocol.py` owns correctness.** One function maps an `aircraft.json`
  entry to a `Point` (tags low-cardinality, floats for measurements, uint for
  `messages`), another renders it with the grammar's escaping rules: measurement,
  tag keys/values and field keys escape `\`, `,`, ` ` and `=`; string field
  values escape `\` and `"`. `alt_baro == "ground"` becomes `0.0` — InfluxDB
  refuses a field that changes type, and a gap-less 0 ft series is the most
  useful answer anyway.
- **`batch.py` is a dumb, testable buffer**: it renders points and hands complete
  batches to an injected sink; size flushes happen on `add`, time flushes on
  `flush_due` against an injected monotonic clock, and the CLI calls `flush_due`
  once per poll. When a batch is handed over it is considered delivered — if the
  sink fails after its retries, the error propagates and the process exits `1`
  instead of growing an unbounded buffer.
- **`client.py` speaks the v2 API**: `urlencode`d org/bucket, `precision=ns`,
  `Authorization: Token`, then 4 attempts by default on 429/5xx/connection
  errors with `0.5 * 2**attempt` seconds of sleep (capped below by
  `Retry-After`); other 4xx statuses abort immediately with the status in the
  message.
- **`udp.py`** packs lines into datagrams ≤ 65507 bytes (a single oversized line
  travels alone) and wraps OS errors in the package's `InfluxError`.
- **`ndjson.py` / `record.py`** implement the shared recording convention with
  strict validation: line-numbered errors, non-decreasing timestamps, decoder
  `now` preferred over wall clock.

## Development

```bash
git clone https://github.com/technothatch/dump1090-influx.git
cd dump1090-influx
pip install -e ".[dev]"

make lint     # ruff check + ruff format --check + mypy
make test     # pytest with coverage (fail-under 80)
make check    # everything, the same gate CI runs
make demo     # print examples/demo.ndjson as line protocol
```

No `make`? The underlying commands are:

```bash
ruff check .
ruff format --check .
mypy src
python -m pytest -q --cov --cov-report=term-missing --cov-fail-under=80
```

The suite — 149 tests at 99% line and branch coverage — covers line-protocol
escaping case by case, point/tag/field separation, batching by size and by flush
interval against a fake clock, retry and backoff against a stub `http.server`
that answers 429 then 200 (and persistent 500s), UDP payloads received on a
bound loopback socket, NDJSON round-trips and validation errors, and the CLI end
to end including exit codes. **No test contacts a decoder or InfluxDB**; fixtures
are written inline.

## Related projects

- [dump1090-fa](https://github.com/flightaware/dump1090) — FlightAware's decoder
- [readsb](https://github.com/wiedehopf/readsb) — high-performance fork, JSON docs in-repo
- [tar1090](https://github.com/wiedehopf/tar1090) — web UI for readsb
- [graphs1090](https://github.com/wiedehopf/graphs1090) — statistics graphs
- [dump1090-recorder](https://github.com/technothatch/dump1090-recorder) — long-term SQLite flight recorder (same NDJSON)
- [dump1090-replay](https://github.com/technothatch/dump1090-replay) — replay recorded traffic (same NDJSON)
- [dump1090-alerts](https://github.com/technothatch/dump1090-alerts) — rule engine over `aircraft.json`
- [dump1090-simulator](https://github.com/technothatch/dump1090-simulator) — synthetic traffic generator
- [InfluxDB 2.x](https://docs.influxdata.com/influxdb/v2/) — the write API target
- [Telegraf](https://docs.influxdata.com/telegraf/) — `socket_listener` UDP consumer

## License

MIT © technothatch. See [LICENSE](LICENSE).

---

## Related projects

Part of the [technothatch ADS-B toolkit](https://github.com/technothatch/technothatch#readme) - the full index with one-liners for every tool lives there.

- **Run & operate:** [dump1090-stack](https://github.com/technothatch/dump1090-stack) - full Compose stack; [dump1090-healthcheck](https://github.com/technothatch/dump1090-healthcheck) - decoder watchdog; [dump1090-alerts](https://github.com/technothatch/dump1090-alerts) - rule-based alerting
- **Metrics & data:** [dump1090-exporter](https://github.com/technothatch/dump1090-exporter) - Prometheus; [dump1090-influx](https://github.com/technothatch/dump1090-influx) - InfluxDB/Telegraf; [dump1090-grafana](https://github.com/technothatch/dump1090-grafana) - dashboards; [dump1090-recorder](https://github.com/technothatch/dump1090-recorder) - SQLite history; [dump1090-coverage](https://github.com/technothatch/dump1090-coverage) - heatmaps & reports
- **Feeds & networking:** [beast-relay](https://github.com/technothatch/beast-relay) - Beast fan-out; [dump1090-geofence](https://github.com/technothatch/dump1090-geofence) - region-filtered feeds; [dump1090-to-mqtt](https://github.com/technothatch/dump1090-to-mqtt) - MQTT/Home Assistant
- **Interfaces:** [dump1090-dashboard](https://github.com/technothatch/dump1090-dashboard) - web UI; [adsb-tui](https://github.com/technothatch/adsb-tui) - terminal radar; [dump1090-cli](https://github.com/technothatch/dump1090-cli) - aircraft.json CLI; [dump1090-sdk](https://github.com/technothatch/dump1090-sdk) - TypeScript client
- **Testing & decoding:** [dump1090-simulator](https://github.com/technothatch/dump1090-simulator) - synthetic traffic; [dump1090-replay](https://github.com/technothatch/dump1090-replay) - recorded history replay; [squawk-decoder](https://github.com/technothatch/squawk-decoder) - squawk/emitter/ICAO decoder
