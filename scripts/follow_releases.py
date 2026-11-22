#!/usr/bin/env python3
"""Dispatch publish.yml for a newly public XDial release.

XDial's release workflow makes a verified release public; this repository picks
it up on a schedule with its own GITHUB_TOKEN, so XDial holds no credential for
this one. One run dispatches at most one publication, stable before beta, and
only while no publish.yml run is queued or running: every publish.yml run shares
one concurrency group, where a newer dispatch cancels the queued one.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import publish_feed as feed


# A failed publication is retried once; after that it waits for a person,
# instead of failing again on every scheduled run.
MAX_ATTEMPTS = 2


def newest_public_release(releases: list[dict[str, Any]], channel: str) -> str | None:
    """The highest tag of a public release in the channel's form."""
    pattern = feed.TAG_PATTERN if channel == feed.STABLE else feed.BETA_TAG_PATTERN
    tags = [
        release.get("tag_name") for release in releases
        if release.get("draft") is False
        and release.get("prerelease") is (channel == feed.BETA)
        and isinstance(release.get("tag_name"), str)
        and pattern.fullmatch(release["tag_name"])
    ]
    return max(tags, key=lambda tag: feed.tag_key(tag, channel), default=None)


def unpublished(tag: str | None, channel: str, ledger: dict[str, Any],
                stable_ledger: dict[str, Any]) -> bool:
    """Whether the publisher would take tag as this channel's next release.

    The ledger keeps withdrawn tags too, so a withdrawn release is never
    offered again; neither is one at or below what the channel has published.
    """
    if tag is None or tag in ledger["releases"]:
        return False
    published = [feed.tag_key(other, channel) for other in ledger["releases"]]
    if published and feed.tag_key(tag, channel) <= max(published):
        return False
    highest_stable = stable_ledger["highestPublishedVersion"]
    if channel == feed.BETA and highest_stable is not None:
        return feed.tag_version(tag, channel) > feed.parse_tag(f"v{highest_stable}")
    return True


def next_publication(root: Path, releases: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(channel, tag) to publish next, stable first, or None."""
    _, stable_ledger = feed.load_channel_state(root, feed.STABLE)
    for channel in feed.CHANNELS:
        if channel == feed.STABLE:
            ledger = stable_ledger
        elif (root / feed.CHANNEL_PATHS[feed.BETA].feed).exists():
            _, ledger = feed.load_channel_state(root, feed.BETA)
        else:
            continue
        tag = newest_public_release(releases, channel)
        if unpublished(tag, channel, ledger, stable_ledger):
            return channel, tag
    return None


def attempts(runs: list[dict[str, Any]], channel: str, tag: str) -> int:
    """Finished publish runs of this tag that did not succeed."""
    prefix = f"publish {channel} {tag} ["
    return sum(
        1 for run in runs
        if run.get("status") == "completed" and run.get("conclusion") != "success"
        and str(run.get("display_title", "")).startswith(prefix)
    )


def gh_json(*arguments: str) -> Any:
    return json.loads(subprocess.check_output(["gh", *arguments], text=True))


def main(argv: list[str] | None = None,
         gh: Callable[..., Any] = gh_json,
         run: Callable[..., Any] = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="report without dispatching")
    args = parser.parse_args(argv)
    feed.require(bool(args.repository), "--repository or GITHUB_REPOSITORY is required")

    runs = gh("api", f"repos/{args.repository}/actions/workflows/publish.yml/runs?per_page=50")
    runs = runs.get("workflow_runs", [])
    active = next((item for item in runs if item.get("status") != "completed"), None)
    if active is not None:
        print(f"A publish run is queued or running; next check takes over: {active.get('html_url')}")
        return 0

    releases = gh("api", f"repos/{feed.SOURCE_REPOSITORY}/releases?per_page=100")
    feed.require(isinstance(releases, list), "GitHub releases response is malformed")
    found = next_publication(args.repo_root, releases)
    if found is None:
        print("Both feeds already carry the newest public XDial releases.")
        return 0
    channel, tag = found
    tried = attempts(runs, channel, tag)
    if tried >= MAX_ATTEMPTS:
        print(f"::warning::publishing {channel} {tag} failed {tried} times; it is no longer "
              f"retried automatically. Fix the cause, then dispatch publish.yml by hand.")
        return 0
    command = [
        "gh", "workflow", "run", "publish.yml", "--repo", args.repository, "--ref", "main",
        "-f", "operation=publish", "-f", f"channel={channel}", "-f", f"release_tag={tag}",
        "-f", f"request_id={args.request_id}",
    ]
    print(f"Publishing {channel} {tag} (attempt {tried + 1} of {MAX_ATTEMPTS}).")
    if not args.dry_run:
        run(command, check=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (feed.PublishError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
