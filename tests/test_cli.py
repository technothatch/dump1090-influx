"""End-to-end CLI tests: parsing, exit codes, sinks and error messages."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeResponse, aircraft, aircraft_doc, influx_stub, json_body, make_records
from dump1090_influx.cli import main
from dump1090_influx.ndjson import write_ndjson

CLEAR_ENV = (
    "INFLUX_URL",
    "INFLUX_TOKEN",
    "INFLUX_ORG",
    "INFLUX_BUCKET",
    "DUMP1090_URL",
    "TELEGRAF_UDP",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLEAR_ENV:
        monkeypatch.delenv(name, raising=False)


def opener_returning(payload: bytes) -> Any:
    def opener(_url: str, timeout: float = 0.0) -> FakeResponse:
        return FakeResponse(payload)

    return opener


def test_version_prints_and_exits_zero(capsys: Any) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "dump1090-influx 0.1.0" in capsys.readouterr().out


def test_help_exits_zero(capsys: Any) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "record" in capsys.readouterr().out


def test_missing_subcommand_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_unknown_subcommand_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2


def test_unknown_flag_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["write", "--teleport"])
    assert excinfo.value.code == 2


def test_write_rejects_bad_batch_size() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["write", "--batch-size", "0"])
    assert excinfo.value.code == 2


def test_write_rejects_bad_interval() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["write", "--interval", "0"])
    assert excinfo.value.code == 2


def test_write_rejects_malformed_telegraf_endpoint() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["write", "--telegraf-udp", "no-port"])
    assert excinfo.value.code == 2


def test_write_rejects_conflicting_sinks() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["write", "--dry-run", "--telegraf-udp", "127.0.0.1:9"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["record", "--out", "x.ndjson", "--max-polls", "notanint"],
        ["record", "--out", "x.ndjson", "--interval", "notanumber"],
        ["record", "--out", "x.ndjson", "--timeout", "-1"],
        ["write", "--batch-size", "1.5"],
        ["write", "--flush-interval", "notanumber"],
        ["write", "--max-retries", "-1"],
        ["write", "--backoff-base", "notanumber"],
        ["write", "--backoff-base", "-0.1"],
        ["write", "--max-polls", "-2"],
        ["write", "--timeout", "0"],
    ],
)
def test_invalid_numeric_arguments_exit_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2


def test_write_without_credentials_exits_one(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "flight.ndjson"
    write_ndjson(path, make_records(1))
    assert main(["write", str(path)]) == 1
    assert "--token and --org" in capsys.readouterr().err


def test_write_dry_run_prints_line_protocol(ndjson_file: Path, capsys: Any) -> None:
    assert main(["write", str(ndjson_file), "--dry-run"]) == 0
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert len(lines) == 3
    assert all(line.startswith("aircraft,") for line in lines)
    assert all("hex=" in line for line in lines)
    assert "0 point(s)" not in out.err
    assert "3 point(s) in 1 batch(es)" in out.err


def test_write_dry_run_ignores_telegraf_env(
    monkeypatch: pytest.MonkeyPatch, ndjson_file: Path, capsys: Any
) -> None:
    monkeypatch.setenv("TELEGRAF_UDP", "127.0.0.1:9")
    assert main(["write", str(ndjson_file), "--dry-run"]) == 0
    assert "aircraft," in capsys.readouterr().out


def test_write_missing_file_exits_one(tmp_path: Path, capsys: Any) -> None:
    assert main(["write", str(tmp_path / "nope.ndjson"), "--dry-run"]) == 1
    assert "file not found" in capsys.readouterr().err


def test_write_skips_entries_without_a_hex(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "odd.ndjson"
    path.write_text(
        '{"t": 1.0, "aircraft": [{"hex": "abc123", "gs": 100}, {"flight": "NOHEX"}]}\n',
        encoding="utf-8",
    )
    assert main(["write", str(path), "--dry-run"]) == 0
    out = capsys.readouterr()
    assert out.out.count("\n") == 1
    assert "hex=abc123" in out.out
    assert "1 point(s) in 1 batch(es)" in out.err


def test_write_env_credentials_reach_influx(
    monkeypatch: pytest.MonkeyPatch, ndjson_file: Path
) -> None:
    with influx_stub([(204, {}, b"")]) as stub:
        monkeypatch.setenv("INFLUX_URL", stub.url)
        monkeypatch.setenv("INFLUX_TOKEN", "sekrit")
        monkeypatch.setenv("INFLUX_ORG", "techno")
        monkeypatch.setenv("INFLUX_BUCKET", "flights")
        assert main(["write", str(ndjson_file)]) == 0
    request = stub.requests[0]
    assert request["authorization"] == "Token sekrit"
    assert request["path"] == "/api/v2/write?org=techno&bucket=flights&precision=ns"
    assert request["body"].count("\n") == 2  # three points, newline-joined


def test_write_defaults_bucket_to_adsb(monkeypatch: pytest.MonkeyPatch, ndjson_file: Path) -> None:
    with influx_stub([(204, {}, b"")]) as stub:
        monkeypatch.setenv("INFLUX_URL", stub.url)
        monkeypatch.setenv("INFLUX_TOKEN", "sekrit")
        monkeypatch.setenv("INFLUX_ORG", "techno")
        assert main(["write", str(ndjson_file)]) == 0
    assert "bucket=adsb" in stub.requests[0]["path"]


def test_write_batches_by_size_before_hitting_influx(
    monkeypatch: pytest.MonkeyPatch, ndjson_file: Path
) -> None:
    with influx_stub([(204, {}, b"")]) as stub:
        monkeypatch.setenv("INFLUX_URL", stub.url)
        monkeypatch.setenv("INFLUX_TOKEN", "sekrit")
        monkeypatch.setenv("INFLUX_ORG", "techno")
        assert main(["write", str(ndjson_file), "--batch-size", "1"]) == 0
    assert len(stub.requests) == 3  # one request per point


def test_write_telegraf_udp_flag_sends_datagrams(
    monkeypatch: pytest.MonkeyPatch, ndjson_file: Path, capsys: Any
) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5.0)
    try:
        port = receiver.getsockname()[1]
        assert main(["write", str(ndjson_file), "--telegraf-udp", f"127.0.0.1:{port}"]) == 0
        data, _ = receiver.recvfrom(65535)
        assert data.decode("utf-8").startswith("aircraft,")
    finally:
        receiver.close()
    assert "3 point(s)" in capsys.readouterr().err


def test_write_telegraf_udp_env_target_is_used(
    monkeypatch: pytest.MonkeyPatch, ndjson_file: Path
) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5.0)
    try:
        port = receiver.getsockname()[1]
        monkeypatch.setenv("TELEGRAF_UDP", f"127.0.0.1:{port}")
        assert main(["write", str(ndjson_file)]) == 0
        data, _ = receiver.recvfrom(65535)
        assert b"hex=000000" in data
    finally:
        receiver.close()


def test_write_bad_telegraf_env_exits_one(
    monkeypatch: pytest.MonkeyPatch, ndjson_file: Path, capsys: Any
) -> None:
    monkeypatch.setenv("TELEGRAF_UDP", "missing-port")
    assert main(["write", str(ndjson_file)]) == 1
    assert "expected host:port" in capsys.readouterr().err


def test_write_live_polls_the_decoder(capsys: Any) -> None:
    doc = json_body(aircraft_doc(aircraft(), aircraft(hex="40621d", flight="BAW117 ")))
    assert main(["write", "--dry-run", "--max-polls", "1"], opener=opener_returning(doc)) == 0
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert len(lines) == 2
    assert any("hex=40621d" in line for line in lines)
    assert "2 point(s) in 1 batch(es)" in out.err


def test_write_live_banner_matches_readme(capsys: Any) -> None:
    doc = json_body(aircraft_doc(aircraft()))
    assert main(["write", "--dry-run", "--max-polls", "1"], opener=opener_returning(doc)) == 0
    assert (
        "# polling http://localhost:8080/data/aircraft.json every 1.0s, flush 5000 lines / 5.0s"
    ) in capsys.readouterr().err


def test_write_live_uses_decoder_env(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setenv("DUMP1090_URL", "http://decoder-env:8080")
    seen: list[str] = []

    def opener(url: str, timeout: float = 0.0) -> FakeResponse:
        seen.append(url)
        return FakeResponse(json_body(aircraft_doc(aircraft())))

    assert main(["write", "--dry-run", "--max-polls", "1"], opener=opener) == 0
    assert seen == ["http://decoder-env:8080/data/aircraft.json"]


def test_write_live_dead_decoder_exits_one(capsys: Any) -> None:
    import urllib.error

    def opener(_url: str, timeout: float = 0.0) -> Any:
        raise urllib.error.URLError("nothing listening")

    assert main(["write", "--dry-run", "--max-polls", "2"], opener=opener) == 1
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "no successful poll" in err


def test_write_live_keyboard_interrupt_still_flushes(capsys: Any) -> None:
    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    doc = json_body(aircraft_doc(aircraft()))
    code = main(["write", "--dry-run"], opener=opener_returning(doc), sleep=sleep)
    assert code == 0
    out = capsys.readouterr()
    assert "aircraft,flight=RYR22GK,hex=4ca918" in out.out
    assert "# interrupted" in out.err
    assert "1 point(s) in 1 batch(es)" in out.err


def test_write_live_retry_backoff_uses_injected_sleep(
    monkeypatch: pytest.MonkeyPatch, ndjson_file: Path
) -> None:
    sleeps: list[float] = []
    with influx_stub([(500, {}, b"boom"), (204, {}, b"")]) as stub:
        monkeypatch.setenv("INFLUX_URL", stub.url)
        monkeypatch.setenv("INFLUX_TOKEN", "sekrit")
        monkeypatch.setenv("INFLUX_ORG", "techno")
        assert main(["write", str(ndjson_file), "--backoff-base", "0.25"], sleep=sleeps.append) == 0
    assert sleeps == [0.25]


def test_write_live_size_flush_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    with influx_stub([(500, {}, b"boom")]) as stub:
        monkeypatch.setenv("INFLUX_URL", stub.url)
        monkeypatch.setenv("INFLUX_TOKEN", "sekrit")
        monkeypatch.setenv("INFLUX_ORG", "techno")
        doc = json_body(aircraft_doc(aircraft()))
        code = main(
            ["write", "--max-polls", "1", "--batch-size", "1", "--max-retries", "0"],
            opener=opener_returning(doc),
        )
    assert code == 1
    assert "HTTP 500" in capsys.readouterr().err


def test_write_final_flush_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, ndjson_file: Path, capsys: Any
) -> None:
    with influx_stub([(500, {}, b"boom")]) as stub:
        monkeypatch.setenv("INFLUX_URL", stub.url)
        monkeypatch.setenv("INFLUX_TOKEN", "sekrit")
        monkeypatch.setenv("INFLUX_ORG", "techno")
        assert main(["write", str(ndjson_file), "--max-retries", "0"]) == 1
    assert "HTTP 500" in capsys.readouterr().err


def test_record_happy_path(tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / "flight.ndjson"
    doc = json_body(aircraft_doc(aircraft()))
    code = main(
        ["record", "--out", str(out), "--max-polls", "2", "--interval", "0.5"],
        opener=opener_returning(doc),
        sleep=lambda _s: None,
    )
    assert code == 0
    assert out.read_text(encoding="utf-8").count("\n") == 2
    err = capsys.readouterr().err
    assert "# recording" in err
    assert "# wrote 2 snapshot(s)" in err


def test_record_uses_decoder_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DUMP1090_URL", "http://decoder-env:9090")
    seen: list[str] = []

    def opener(url: str, timeout: float = 0.0) -> FakeResponse:
        seen.append(url)
        return FakeResponse(json_body(aircraft_doc()))

    code = main(["record", "--out", str(tmp_path / "f.ndjson"), "--max-polls", "1"], opener=opener)
    assert code == 0
    assert seen == ["http://decoder-env:9090/data/aircraft.json"]


def test_record_dead_decoder_exits_one(tmp_path: Path, capsys: Any) -> None:
    import urllib.error

    def opener(_url: str, timeout: float = 0.0) -> Any:
        raise urllib.error.URLError("nothing listening")

    code = main(["record", "--out", str(tmp_path / "f.ndjson"), "--max-polls", "1"], opener=opener)
    assert code == 1
    assert "no snapshots captured" in capsys.readouterr().err


def test_record_unwritable_out_exits_one(tmp_path: Path, capsys: Any) -> None:
    code = main(
        ["record", "--out", str(tmp_path), "--max-polls", "1"],
        opener=opener_returning(b""),
    )
    assert code == 1
    assert "cannot write" in capsys.readouterr().err


def test_record_keyboard_interrupt_exits_zero(tmp_path: Path, capsys: Any) -> None:
    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    out = tmp_path / "flight.ndjson"
    code = main(
        ["record", "--out", str(out)],
        opener=opener_returning(json_body(aircraft_doc(aircraft()))),
        sleep=sleep,
    )
    assert code == 0
    assert out.read_text(encoding="utf-8").count("\n") == 1
    assert "# interrupted" in capsys.readouterr().err
