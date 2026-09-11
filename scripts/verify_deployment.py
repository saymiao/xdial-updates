#!/usr/bin/env python3
"""Confirm that GitHub Pages serves the exact generated stable feed."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from publish_feed import MAX_FEED_BYTES, PublishError, validate_feed, validate_feed_size


CANONICAL_FEED_URL = "https://saymiao.github.io/xdial-updates/stable.json"


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_bounded(response, maximum: int) -> bytes:
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise PublishError("deployed feed exceeds 512 KiB")
    return payload


def fetch_once(opener: urllib.request.OpenerDirector, url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if not (
        url == CANONICAL_FEED_URL
        and parsed.scheme == "https"
        and parsed.hostname == "saymiao.github.io"
        and parsed.port is None
        and parsed.query == ""
        and parsed.fragment == ""
    ):
        raise PublishError("deployment verification requires the canonical HTTPS feed URL")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache, max-age=0",
            "Pragma": "no-cache",
            "User-Agent": "xdial-updates-deployment-verifier/1",
        },
    )
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise PublishError(f"canonical feed returned HTTP {response.status}")
        if response.geturl() != url:
            raise PublishError("canonical feed redirected")
        return read_bounded(response, MAX_FEED_BYTES)


def validate_payload(payload: bytes) -> None:
    try:
        feed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishError("deployed feed is not valid UTF-8 JSON") from error
    validate_feed(feed)
    validate_feed_size(feed)


def verify(expected_path: Path, url: str, attempts: int, delay_seconds: float) -> None:
    expected = expected_path.read_bytes()
    if len(expected) > MAX_FEED_BYTES:
        raise PublishError("expected feed exceeds 512 KiB")
    validate_payload(expected)
    opener = urllib.request.build_opener(NoRedirectHandler())
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            actual = fetch_once(opener, url)
            validate_payload(actual)
            if actual != expected:
                raise PublishError("canonical feed does not yet match generated stable.json")
            print(f"canonical feed verified on attempt {attempt}")
            return
        except (OSError, urllib.error.URLError, PublishError) as error:
            last_error = error
            if attempt < attempts:
                print(f"verification attempt {attempt} failed: {error}", file=sys.stderr)
                time.sleep(delay_seconds)
    raise PublishError(f"canonical feed verification failed after {attempts} attempts: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--url", default=CANONICAL_FEED_URL)
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--delay-seconds", type=float, default=5)
    args = parser.parse_args()
    try:
        if args.attempts < 1 or not 0 <= args.delay_seconds <= 30:
            raise PublishError("retry bounds are invalid")
        verify(args.expected, args.url, args.attempts, args.delay_seconds)
    except PublishError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
