#!/usr/bin/env python3
"""Confirm that GitHub Pages serves the exact generated feed of a channel."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from publish_feed import (
    CHANNELS,
    MAX_FEED_BYTES,
    STABLE,
    PublishError,
    require_channel,
    validate_feed,
    validate_feed_size,
)


CANONICAL_FEED_URLS = {
    channel: f"https://saymiao.github.io/xdial-updates/{channel}.json" for channel in CHANNELS
}
CANONICAL_FEED_URL = CANONICAL_FEED_URLS[STABLE]


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
        url in CANONICAL_FEED_URLS.values()
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


def validate_payload(payload: bytes, channel: str = STABLE) -> None:
    try:
        feed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishError("deployed feed is not valid UTF-8 JSON") from error
    validate_feed(feed, channel)
    validate_feed_size(feed, channel)


def verify(expected_path: Path, url: str, attempts: int, delay_seconds: float,
           channel: str = STABLE) -> None:
    require_channel(channel)
    if url != CANONICAL_FEED_URLS[channel]:
        raise PublishError(f"deployment verification requires the canonical {channel} feed URL")
    expected = expected_path.read_bytes()
    if len(expected) > MAX_FEED_BYTES:
        raise PublishError("expected feed exceeds 512 KiB")
    validate_payload(expected, channel)
    opener = urllib.request.build_opener(NoRedirectHandler())
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            actual = fetch_once(opener, url)
            validate_payload(actual, channel)
            if actual != expected:
                raise PublishError(f"canonical feed does not yet match generated {channel}.json")
            print(f"canonical {channel} feed verified on attempt {attempt}")
            return
        except (OSError, urllib.error.URLError, PublishError) as error:
            last_error = error
            if attempt < attempts:
                print(f"verification attempt {attempt} failed: {error}", file=sys.stderr)
                time.sleep(delay_seconds)
    raise PublishError(f"canonical feed verification failed after {attempts} attempts: {last_error}")


def describe_pending(expected_path: Path, channel: str,
                     opener: urllib.request.OpenerDirector | None = None) -> str:
    """Say whether deploying site/ would change what a channel's URL serves now.

    Every run uploads the whole site/, so a feed that an earlier run committed
    but never deployed goes live with the next run of either channel. This only
    reports that before the deployment; it never fails the run.
    """
    require_channel(channel)
    url = CANONICAL_FEED_URLS[channel]
    try:
        expected = expected_path.read_bytes()
        revision = json.loads(expected).get("revision")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as error:
        return f"::warning::cannot read the committed {channel} feed {expected_path}: {error}"
    opener = opener or urllib.request.build_opener(NoRedirectHandler())
    try:
        live = fetch_once(opener, url)
    except (OSError, urllib.error.URLError, PublishError) as error:
        return (
            f"::warning::cannot read {url} ({error}); this deployment will serve the committed "
            f"{channel} feed revision {revision}"
        )
    if live == expected:
        return f"{url} already serves the committed {channel} feed revision {revision}"
    try:
        live_revision = json.loads(live).get("revision")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        live_revision = "unreadable"
    return (
        f"::warning::this deployment changes {url} from revision {live_revision} to the committed "
        f"revision {revision}. This run did not write the {channel} feed: an earlier run committed it "
        f"without deploying it, or the URL still serves a cached copy."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=CHANNELS, default=STABLE)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--url", default=None)
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--delay-seconds", type=float, default=5)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="before deploying: report whether the canonical URL would change; always exits 0",
    )
    args = parser.parse_args()
    if args.report_only:
        print(describe_pending(args.expected, args.channel))
        return 0
    try:
        if args.attempts < 1 or not 0 <= args.delay_seconds <= 30:
            raise PublishError("retry bounds are invalid")
        url = args.url if args.url is not None else CANONICAL_FEED_URLS[args.channel]
        verify(args.expected, url, args.attempts, args.delay_seconds, args.channel)
    except PublishError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
