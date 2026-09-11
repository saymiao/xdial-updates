#!/usr/bin/env python3
"""Verify an XDial release and update the static stable feed."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


SOURCE_REPOSITORY = "kafeifei/XDial"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_CHECKSUM_BYTES = 4 * 1024
MAX_FEED_BYTES = 512 * 1024
TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
BUILD_PATTERN = re.compile(r"[1-9][0-9]*\Z")
SYSTEM_VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*)){1,2}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
RFC3339_UTC_SECONDS_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
ACCEPTED_NOTES_HEADINGS = {"更新了什么", "更新内容", "更新"}

FEED_KEYS = {"schemaVersion", "revision", "channel", "generatedAt", "release"}
RELEASE_KEYS = {
    "tag",
    "version",
    "build",
    "minimumSystemVersion",
    "publishedAt",
    "releaseNotes",
    "archiveURL",
    "archiveSize",
    "archiveSHA256",
}
LEDGER_KEYS = {
    "schemaVersion",
    "lastRevision",
    "highestPublishedVersion",
    "releases",
    "withdrawnTags",
}

BUNDLES = {
    "XDial.app/Contents/Info.plist": "com.kafeifei.xdial.app",
    "XDial.app/Contents/Helpers/XDial Settings UI.app/Contents/Info.plist":
        "com.kafeifei.xdial.app.settings-ui",
    (
        "XDial.app/Contents/Library/SystemExtensions/"
        "com.kafeifei.xdial.app.transparent-proxy.systemextension/Contents/Info.plist"
    ): "com.kafeifei.xdial.app.transparent-proxy",
}


class PublishError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PublishError(message)


def parse_iso8601(value: Any, field: str) -> dt.datetime:
    require(isinstance(value, str) and value != "", f"{field} must be an ISO-8601 string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as error:
        raise PublishError(f"{field} must be an ISO-8601 timestamp") from error
    require(parsed.tzinfo is not None, f"{field} must include a timezone")
    return parsed


def parse_feed_timestamp(value: Any, field: str) -> dt.datetime:
    require(
        isinstance(value, str) and RFC3339_UTC_SECONDS_PATTERN.fullmatch(value) is not None,
        f"{field} must be UTC RFC3339 with whole seconds",
    )
    return parse_iso8601(value, field)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def isoformat_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_tag(tag: Any) -> tuple[int, int, int]:
    require(isinstance(tag, str), "release tag must be a string")
    match = TAG_PATTERN.fullmatch(tag)
    require(match is not None, "release tag must be canonical vMAJOR.MINOR.PATCH")
    return tuple(int(component) for component in match.groups())  # type: ignore[return-value]


def release_notes(body: Any) -> str:
    require(isinstance(body, str) and body.strip() != "", "published release notes are missing")
    captures = False
    captured: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if captures:
                break
            captures = stripped[3:].strip() in ACCEPTED_NOTES_HEADINGS
            continue
        if captures:
            captured.append(line)
    section = "\n".join(captured).strip()
    notes = section if section else body.strip()
    require(notes != "", "published release notes are missing")
    return notes


def canonical_download_url(tag: str, asset_name: str) -> str:
    return f"https://github.com/{SOURCE_REPOSITORY}/releases/download/{tag}/{asset_name}"


def validate_asset(asset: Any, tag: str, expected_name: str) -> dict[str, Any]:
    require(isinstance(asset, dict), f"{expected_name} asset metadata is malformed")
    require(asset.get("name") == expected_name, f"wrong asset: expected {expected_name}")
    require(asset.get("state") in (None, "uploaded"), f"{expected_name} is not uploaded")
    size = asset.get("size")
    require(type(size) is int and size > 0, f"{expected_name} has invalid API size")
    require(
        asset.get("browser_download_url") == canonical_download_url(tag, expected_name),
        f"{expected_name} has a non-canonical download URL",
    )
    api_url = asset.get("url")
    require(isinstance(api_url, str), f"{expected_name} is missing its API URL")
    parsed = urllib.parse.urlsplit(api_url)
    expected_prefix = f"/repos/{SOURCE_REPOSITORY}/releases/assets/"
    require(
        parsed.scheme == "https"
        and parsed.hostname == "api.github.com"
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path.startswith(expected_prefix)
        and parsed.path[len(expected_prefix):].isdigit(),
        f"{expected_name} has an untrusted API URL",
    )
    return asset


def find_asset(release: dict[str, Any], tag: str, name: str) -> dict[str, Any]:
    assets = release.get("assets")
    require(isinstance(assets, list), "release assets are missing")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    require(len(matches) == 1, f"release must contain exactly one {name} asset")
    return validate_asset(matches[0], tag, name)


class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int,
                         msg: str, headers: Any, newurl: str) -> urllib.request.Request:
        parsed = urllib.parse.urlsplit(newurl)
        require(
            parsed.scheme == "https"
            and parsed.hostname == "release-assets.githubusercontent.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None,
            "asset download redirected to an untrusted host",
        )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        require(redirected is not None, "asset redirect could not be followed")
        private_headers = {"authorization", "x-github-api-version"}
        for collection in (redirected.headers, redirected.unredirected_hdrs):
            for header in list(collection):
                if header.lower() in private_headers:
                    collection.pop(header, None)
        return redirected


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.token = token
        self.opener = urllib.request.build_opener(TrustedRedirectHandler())

    def _request(self, url: str, accept: str) -> urllib.request.Request:
        headers = {
            "Accept": accept,
            "User-Agent": "xdial-updates-publisher/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers)

    def release(self, tag: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(tag, safe="")
        url = f"https://api.github.com/repos/{SOURCE_REPOSITORY}/releases/tags/{encoded}"
        try:
            with self.opener.open(self._request(url, "application/vnd.github+json"), timeout=30) as response:
                require(response.status == 200, f"GitHub returned HTTP {response.status}")
                payload = json.load(response)
        except (urllib.error.URLError, json.JSONDecodeError) as error:
            raise PublishError(f"unable to read the published release: {error}") from error
        require(isinstance(payload, dict), "GitHub release response is malformed")
        return payload

    def require_public_source_repository(self) -> None:
        url = f"https://api.github.com/repos/{SOURCE_REPOSITORY}"
        try:
            with self.opener.open(self._request(url, "application/vnd.github+json"), timeout=30) as response:
                require(response.status == 200, f"GitHub returned HTTP {response.status}")
                payload = json.load(response)
        except (urllib.error.URLError, json.JSONDecodeError) as error:
            raise PublishError(f"unable to verify the source repository: {error}") from error
        require(
            isinstance(payload, dict)
            and payload.get("private") is False
            and payload.get("visibility") == "public",
            f"{SOURCE_REPOSITORY} must be public before its feed is published",
        )

    def download(self, asset: dict[str, Any], destination: Path,
                 maximum_bytes: int) -> None:
        expected_size = asset["size"]
        require(expected_size <= maximum_bytes, f"{asset['name']} exceeds its download limit")
        try:
            with self.opener.open(
                self._request(asset["url"], "application/octet-stream"), timeout=120
            ) as response, destination.open("wb") as output:
                require(response.status == 200, f"asset download returned HTTP {response.status}")
                final = urllib.parse.urlsplit(response.geturl())
                require(
                    final.scheme == "https"
                    and final.hostname in {"api.github.com", "release-assets.githubusercontent.com"},
                    "asset download ended at an untrusted host",
                )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    require(content_length.isdigit(), "asset Content-Length is invalid")
                    require(int(content_length) == expected_size, "asset Content-Length differs from GitHub metadata")
                downloaded = 0
                while True:
                    chunk = response.read(min(1024 * 1024, maximum_bytes - downloaded + 1))
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    require(downloaded <= maximum_bytes, f"{asset['name']} exceeded its download limit")
                    output.write(chunk)
                require(downloaded == expected_size, f"{asset['name']} byte count differs from GitHub metadata")
        except urllib.error.URLError as error:
            raise PublishError(f"unable to download {asset['name']}: {error}") from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_api_digest(asset: dict[str, Any], actual: str) -> None:
    digest = asset.get("digest")
    if digest is None:
        return
    require(isinstance(digest, str) and digest.startswith("sha256:"), f"{asset['name']} has unsupported API digest")
    expected = digest.removeprefix("sha256:")
    require(SHA256_PATTERN.fullmatch(expected) is not None, f"{asset['name']} API digest is malformed")
    require(actual == expected, f"{asset['name']} does not match its API digest")


def checksum_from_file(path: Path, archive_name: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as error:
        raise PublishError("checksum asset is not UTF-8 text") from error
    match = re.fullmatch(r"([0-9a-f]{64})(?:[ \t]+\*?([^\r\n]+))?", text)
    require(match is not None, "checksum asset must contain one lowercase SHA-256")
    named_file = match.group(2)
    require(named_file in (None, archive_name), "checksum names the wrong archive")
    return match.group(1)


def plist_string(plist: dict[str, Any], key: str, path: str) -> str:
    value = plist.get(key)
    require(isinstance(value, str) and value != "", f"{path} has invalid {key}")
    return value


def validate_archive(path: Path, version: str) -> dict[str, str]:
    size = path.stat().st_size
    require(0 < size <= MAX_ARCHIVE_BYTES, "archive size must be between 1 byte and 512 MiB")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            require(infos, "archive is empty")
            roots: set[str] = set()
            for info in infos:
                parts = PurePosixPath(info.filename).parts
                require(parts and parts[0] not in {"", "/"}, "archive contains an absolute path")
                require(".." not in parts, "archive contains a parent traversal")
                roots.add(parts[0])
            require(roots == {"XDial.app"}, "archive must contain exactly one root XDial.app")

            found: dict[str, dict[str, Any]] = {}
            for plist_path in BUNDLES:
                matches = [info for info in infos if info.filename == plist_path]
                require(len(matches) == 1, f"archive must contain exactly one {plist_path}")
                mode = matches[0].external_attr >> 16
                require(stat.S_ISREG(mode), f"{plist_path} must be a regular file")
                require(matches[0].file_size <= 1024 * 1024, f"{plist_path} is unexpectedly large")
                try:
                    found[plist_path] = plistlib.loads(archive.read(plist_path))
                except KeyError as error:
                    raise PublishError(f"archive is missing {plist_path}") from error
                except plistlib.InvalidFileException as error:
                    raise PublishError(f"archive has invalid plist {plist_path}") from error
    except (zipfile.BadZipFile, OSError) as error:
        raise PublishError(f"archive is not a valid ZIP: {error}") from error

    builds: set[str] = set()
    minimum_versions: set[str] = set()
    for plist_path, expected_identifier in BUNDLES.items():
        plist = found[plist_path]
        require(
            plist_string(plist, "CFBundleIdentifier", plist_path) == expected_identifier,
            f"{plist_path} has the wrong bundle identity",
        )
        require(
            plist_string(plist, "CFBundleShortVersionString", plist_path) == version,
            f"{plist_path} does not match release version {version}",
        )
        build = plist_string(plist, "CFBundleVersion", plist_path)
        require(BUILD_PATTERN.fullmatch(build) is not None, f"{plist_path} has invalid build number")
        builds.add(build)
        minimum = plist_string(plist, "LSMinimumSystemVersion", plist_path)
        require(SYSTEM_VERSION_PATTERN.fullmatch(minimum) is not None, f"{plist_path} has invalid minimum OS")
        minimum_versions.add(minimum)
    host = found["XDial.app/Contents/Info.plist"]
    require(
        host.get("XDialUpdateAcceptanceID") in (None, ""),
        "acceptance update archives cannot be published to stable",
    )
    require(len(builds) == 1, "host, settings UI, and extension build numbers differ")
    require(len(minimum_versions) == 1, "host, settings UI, and extension minimum OS versions differ")
    return {"build": builds.pop(), "minimumSystemVersion": minimum_versions.pop()}


def verify_release(release: dict[str, Any], requested_tag: str, archive: Path,
                   checksum: Path) -> dict[str, Any]:
    version_tuple = parse_tag(requested_tag)
    require(release.get("tag_name") == requested_tag, "GitHub returned a different release tag")
    require(release.get("draft") is False, "draft releases cannot be published")
    require(release.get("prerelease") is False, "prereleases cannot be published")
    require(release.get("published_at") is not None, "release is not published")
    published_at = parse_iso8601(release["published_at"], "published_at")
    require(published_at <= utc_now() + dt.timedelta(minutes=5), "release published_at is in the future")

    archive_name = f"XDial-{requested_tag}.zip"
    checksum_name = f"{archive_name}.sha256"
    archive_asset = find_asset(release, requested_tag, archive_name)
    checksum_asset = find_asset(release, requested_tag, checksum_name)
    require(archive_asset["size"] <= MAX_ARCHIVE_BYTES, "archive exceeds 512 MiB")
    require(checksum_asset["size"] <= MAX_CHECKSUM_BYTES, "checksum asset is unexpectedly large")

    actual_size = archive.stat().st_size
    require(actual_size == archive_asset["size"], "downloaded archive size differs from GitHub metadata")
    require(checksum.stat().st_size == checksum_asset["size"], "downloaded checksum size differs from GitHub metadata")
    require(checksum.stat().st_size <= MAX_CHECKSUM_BYTES, "checksum asset is unexpectedly large")
    require(0 < actual_size <= MAX_ARCHIVE_BYTES, "archive size must be between 1 byte and 512 MiB")
    actual_sha = sha256_file(archive)
    expected_sha = checksum_from_file(checksum, archive_name)
    require(actual_sha == expected_sha, "archive does not match the published checksum")
    validate_api_digest(archive_asset, actual_sha)
    validate_api_digest(checksum_asset, sha256_file(checksum))

    bundle = validate_archive(archive, ".".join(str(value) for value in version_tuple))
    return {
        "tag": requested_tag,
        "version": ".".join(str(value) for value in version_tuple),
        "build": bundle["build"],
        "minimumSystemVersion": bundle["minimumSystemVersion"],
        "publishedAt": isoformat_utc(published_at),
        "releaseNotes": release_notes(release.get("body")),
        "archiveURL": canonical_download_url(requested_tag, archive_name),
        "archiveSize": actual_size,
        "archiveSHA256": actual_sha,
    }


def validate_release_record(record: Any) -> None:
    require(isinstance(record, dict) and set(record) == RELEASE_KEYS, "feed release fields are invalid")
    version = parse_tag(record["tag"])
    require(record["version"] == ".".join(str(value) for value in version), "feed tag and version differ")
    require(BUILD_PATTERN.fullmatch(record["build"]) is not None, "feed build is invalid")
    require(SYSTEM_VERSION_PATTERN.fullmatch(record["minimumSystemVersion"]) is not None, "feed minimum OS is invalid")
    parse_feed_timestamp(record["publishedAt"], "feed publishedAt")
    require(isinstance(record["releaseNotes"], str) and record["releaseNotes"].strip(), "feed notes are empty")
    require(record["archiveURL"] == canonical_download_url(record["tag"], f"XDial-{record['tag']}.zip"), "feed archive URL is invalid")
    require(type(record["archiveSize"]) is int and 0 < record["archiveSize"] <= MAX_ARCHIVE_BYTES, "feed archive size is invalid")
    require(isinstance(record["archiveSHA256"], str) and SHA256_PATTERN.fullmatch(record["archiveSHA256"]) is not None, "feed SHA-256 is invalid")


def validate_feed(feed: Any) -> None:
    require(isinstance(feed, dict) and set(feed) == FEED_KEYS, "stable feed fields are invalid")
    require(feed["schemaVersion"] == 1, "unsupported stable feed schema")
    require(type(feed["revision"]) is int and feed["revision"] > 0, "feed revision must be positive")
    require(feed["channel"] == "stable", "feed channel must be stable")
    parse_feed_timestamp(feed["generatedAt"], "feed generatedAt")
    if feed["release"] is not None:
        validate_release_record(feed["release"])


def json_payload(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def validate_feed_size(feed: Any) -> None:
    require(len(json_payload(feed)) <= MAX_FEED_BYTES, "stable feed exceeds 512 KiB")


def validate_ledger(ledger: Any, feed: dict[str, Any]) -> None:
    require(isinstance(ledger, dict) and set(ledger) == LEDGER_KEYS, "ledger fields are invalid")
    require(ledger["schemaVersion"] == 1, "unsupported ledger schema")
    require(type(ledger["lastRevision"]) is int and ledger["lastRevision"] == feed["revision"], "ledger and feed revisions differ")
    highest = ledger["highestPublishedVersion"]
    if highest is not None:
        parse_tag(f"v{highest}")
    require(isinstance(ledger["releases"], dict), "ledger releases must be an object")
    release_versions: list[tuple[int, int, int]] = []
    for tag, record in ledger["releases"].items():
        tag_version = parse_tag(tag)
        require(isinstance(record, dict), f"ledger record for {tag} is invalid")
        status = record.get("status")
        expected_keys = {"version", "archiveSHA256", "publishedRevision", "status"}
        if status == "withdrawn":
            expected_keys.add("withdrawnRevision")
        require(set(record) == expected_keys, f"ledger record fields for {tag} are invalid")
        require(status in {"published", "withdrawn"}, f"ledger status for {tag} is invalid")
        require(record["version"] == ".".join(str(value) for value in tag_version), f"ledger tag and version differ for {tag}")
        require(isinstance(record["archiveSHA256"], str) and SHA256_PATTERN.fullmatch(record["archiveSHA256"]) is not None, f"ledger SHA-256 for {tag} is invalid")
        published_revision = record["publishedRevision"]
        require(type(published_revision) is int and 0 < published_revision <= ledger["lastRevision"], f"ledger publish revision for {tag} is invalid")
        if status == "withdrawn":
            withdrawn_revision = record["withdrawnRevision"]
            require(type(withdrawn_revision) is int and published_revision < withdrawn_revision <= ledger["lastRevision"], f"ledger withdrawal revision for {tag} is invalid")
        release_versions.append(tag_version)
    withdrawn = ledger["withdrawnTags"]
    require(isinstance(withdrawn, list) and len(withdrawn) == len(set(withdrawn)), "withdrawn tags must be unique")
    for tag in withdrawn:
        parse_tag(tag)
        require(tag in ledger["releases"], "withdrawn tag is absent from release ledger")
        require(ledger["releases"][tag]["status"] == "withdrawn", "withdrawn tag has the wrong ledger status")
    if release_versions:
        require(highest is not None and parse_tag(f"v{highest}") == max(release_versions), "highest published version does not match ledger")
    else:
        require(highest is None, "empty ledger cannot have a highest published version")
    current = feed["release"]
    if current is not None:
        tag = current["tag"]
        require(tag in ledger["releases"], "current feed release is absent from ledger")
        record = ledger["releases"][tag]
        require(record["status"] == "published", "current feed release is not published in ledger")
        require(record["archiveSHA256"] == current["archiveSHA256"], "current feed and ledger checksums differ")


def next_revision(previous: int, now: dt.datetime) -> int:
    milliseconds = int(now.timestamp() * 1000)
    return max(previous + 1, milliseconds)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json_payload(value)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublishError(f"cannot read valid JSON from {path}") from error


def apply_operation(root: Path, operation: str, tag: str, verified: dict[str, Any] | None,
                    request_id: str, run_id: str, run_attempt: str,
                    now: dt.datetime | None = None) -> str:
    now = now or utc_now()
    parse_tag(tag)
    feed_path = root / "site/stable.json"
    ledger_path = root / "state/ledger.json"
    feed = load_json(feed_path)
    ledger = load_json(ledger_path)
    validate_feed(feed)
    require(feed_path.stat().st_size <= MAX_FEED_BYTES, "existing stable feed exceeds 512 KiB")
    validate_feed_size(feed)
    validate_ledger(ledger, feed)
    history_path = root / f"state/history/{feed['revision']}.json"
    require(load_json(history_path) == feed, "current feed is not preserved in history")
    original_feed = copy.deepcopy(feed)
    original_ledger = copy.deepcopy(ledger)
    result = "updated"

    if operation == "publish":
        require(verified is not None, "publish requires a verified release")
        validate_release_record(verified)
        require(verified["tag"] == tag, "verified release tag differs from request")
        require(tag not in ledger["withdrawnTags"], "withdrawn release cannot be republished")
        current = feed["release"]
        if current is not None and current["tag"] == tag:
            require(current == verified, "published release metadata changed after publication")
            result = "noop"
        else:
            highest = ledger["highestPublishedVersion"]
            if highest is not None:
                require(parse_tag(tag) > parse_tag(f"v{highest}"), "older or equal release cannot replace the feed")
            revision = next_revision(feed["revision"], now)
            feed = {
                "schemaVersion": 1,
                "revision": revision,
                "channel": "stable",
                "generatedAt": isoformat_utc(now),
                "release": verified,
            }
            ledger["lastRevision"] = revision
            ledger["highestPublishedVersion"] = verified["version"]
            ledger["releases"][tag] = {
                "version": verified["version"],
                "archiveSHA256": verified["archiveSHA256"],
                "publishedRevision": revision,
                "status": "published",
            }
    elif operation == "withdraw":
        require(verified is None, "withdraw does not accept release metadata")
        current = feed["release"]
        if current is None:
            require(tag in ledger["withdrawnTags"], "only the current release can be withdrawn")
            result = "noop"
        else:
            require(current["tag"] == tag, "only the current release can be withdrawn")
            revision = next_revision(feed["revision"], now)
            feed = {
                "schemaVersion": 1,
                "revision": revision,
                "channel": "stable",
                "generatedAt": isoformat_utc(now),
                "release": None,
            }
            ledger["lastRevision"] = revision
            ledger["withdrawnTags"].append(tag)
            ledger["releases"][tag]["status"] = "withdrawn"
            ledger["releases"][tag]["withdrawnRevision"] = revision
    else:
        raise PublishError(f"unsupported operation: {operation}")

    validate_feed(feed)
    validate_feed_size(feed)
    validate_ledger(ledger, feed)
    if result == "updated":
        atomic_json(feed_path, feed)
        atomic_json(ledger_path, ledger)
        atomic_json(root / f"state/history/{feed['revision']}.json", feed)

    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:100]
    safe_attempt = re.sub(r"[^A-Za-z0-9_.-]", "_", run_attempt)[:30]
    require(safe_run_id != "" and safe_attempt != "", "run identity is missing")
    audit = {
        "schemaVersion": 1,
        "operation": operation,
        "releaseTag": tag,
        "requestId": request_id,
        "runId": run_id,
        "runAttempt": run_attempt,
        "recordedAt": isoformat_utc(now),
        "result": result,
        "feedRevisionBefore": original_feed["revision"],
        "feedRevisionAfter": feed["revision"],
        "ledgerChanged": ledger != original_ledger,
    }
    atomic_json(root / f"state/audit/{safe_run_id}-{safe_attempt}.json", audit)
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--operation", required=True, choices=("publish", "withdraw"))
    result.add_argument("--release-tag", required=True)
    result.add_argument("--repo-root", type=Path, default=Path.cwd())
    result.add_argument("--request-id", default="")
    result.add_argument("--run-id", required=True)
    result.add_argument("--run-attempt", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    root = args.repo_root.resolve()
    verified = None
    try:
        if args.operation == "publish":
            client = GitHubClient(os.environ.get("GH_TOKEN"))
            client.require_public_source_repository()
            release = client.release(args.release_tag)
            with tempfile.TemporaryDirectory(prefix="xdial-release-") as temporary:
                archive_name = f"XDial-{args.release_tag}.zip"
                archive = Path(temporary) / archive_name
                checksum = Path(temporary) / f"{archive_name}.sha256"
                archive_asset = find_asset(release, args.release_tag, archive.name)
                checksum_asset = find_asset(release, args.release_tag, checksum.name)
                client.download(archive_asset, archive, MAX_ARCHIVE_BYTES)
                client.download(checksum_asset, checksum, MAX_CHECKSUM_BYTES)
                verified = verify_release(release, args.release_tag, archive, checksum)
        result = apply_operation(
            root=root,
            operation=args.operation,
            tag=args.release_tag,
            verified=verified,
            request_id=args.request_id,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
    except PublishError as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 1
    print(f"{args.operation} {args.release_tag}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
