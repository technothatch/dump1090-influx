"""InfluxDB 2.x write API client over ``urllib.request`` (stdlib only).

POSTs newline-delimited line protocol to ``/api/v2/write`` with the v2 token
auth scheme and nanosecond precision, retrying transient failures (HTTP 429
and 5xx, plus connection errors) with exponential backoff - optionally
honouring a ``Retry-After`` header when the server is kind enough to send one.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from dump1090_influx.errors import InfluxError

__all__ = ["DEFAULT_URL", "write_lines", "write_url"]

DEFAULT_URL = "http://localhost:8086"

_RETRYABLE_MIN = 429


def write_url(endpoint: str, org: str, bucket: str) -> str:
    """Build the v2 write endpoint for ``org``/``bucket`` at ns precision."""
    query = urllib.parse.urlencode({"org": org, "bucket": bucket, "precision": "ns"})
    return f"{endpoint.rstrip('/')}/api/v2/write?{query}"


def _retryable(status: int) -> bool:
    return status == _RETRYABLE_MIN or 500 <= status <= 599


def _retry_after(exc: urllib.error.HTTPError) -> float:
    try:
        raw = exc.headers.get("Retry-After")
    except AttributeError:  # defensively: hand-built HTTPErrors may carry no headers
        return 0.0
    if not isinstance(raw, str):
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, seconds)


def write_lines(
    endpoint: str,
    *,
    token: str,
    org: str,
    bucket: str,
    lines: list[str],
    timeout: float = 10.0,
    max_retries: int = 4,
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> int:
    """Write one batch of line-protocol lines, retrying transient failures.

    Args:
        endpoint: InfluxDB base URL (``http://localhost:8086``).
        token: v2 API token (``Authorization: Token ...``).
        org: Organisation name.
        bucket: Target bucket.
        lines: Line-protocol lines (joined with ``\\n``, no trailing newline).
        timeout: Per-request HTTP timeout.
        max_retries: Retries after the first attempt on 429/5xx/connection
            errors - so ``max_retries=4`` means at most 5 requests.
        backoff_base: First backoff delay; doubles each retry
            (``base * 2**attempt``), raised to ``Retry-After`` when larger.
        sleep: Injectable sleep for tests.
        opener: Injectable HTTP opener for tests.

    Returns:
        Number of lines accepted by InfluxDB (0 when ``lines`` is empty).

    Raises:
        InfluxError: Missing credentials, a non-retryable HTTP status (any
            other 4xx), or retries exhausted.
    """
    if not lines:
        return 0
    if not token or not org or not bucket:
        raise InfluxError("token, org and bucket are all required for an InfluxDB write")
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")
    if backoff_base < 0:
        raise ValueError(f"backoff_base must be >= 0, got {backoff_base}")

    request = urllib.request.Request(
        write_url(endpoint, org, bucket),
        data="\n".join(lines).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "text/plain; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "dump1090-influx",
        },
    )
    attempt = 0
    while True:
        try:
            with opener(request, timeout=timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            if not _retryable(exc.code) or attempt >= max_retries:
                raise InfluxError(
                    f"InfluxDB write to {endpoint} failed with HTTP {exc.code}: {exc.reason}"
                ) from exc
            delay = max(backoff_base * (2**attempt), _retry_after(exc))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt >= max_retries:
                raise InfluxError(f"cannot reach InfluxDB at {endpoint}: {exc}") from exc
            delay = backoff_base * (2**attempt)
        else:
            return len(lines)
        sleep(delay)
        attempt += 1
