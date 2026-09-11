import datetime as dt
import hashlib
import io
import json
import plistlib
import shutil
import stat
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

import publish_feed as publisher


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "site").mkdir()
        (self.root / "state/history").mkdir(parents=True)
        (self.root / "state/audit").mkdir(parents=True)
        shutil.copy(ROOT / "site/stable.json", self.root / "site/stable.json")
        shutil.copy(ROOT / "state/ledger.json", self.root / "state/ledger.json")
        shutil.copy(ROOT / "state/history/1.json", self.root / "state/history/1.json")
        self.work = self.root / "fixtures"
        self.work.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write_archive(self, tag="v1.2.3", *, override=None, include_symlink=False):
        override = override or {}
        version = tag[1:]
        archive = self.work / f"XDial-{tag}.zip"
        bundle_paths = publisher.BUNDLES
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path, identifier in bundle_paths.items():
                values = {
                    "CFBundleIdentifier": identifier,
                    "CFBundleShortVersionString": version,
                    "CFBundleVersion": "1789130088",
                    "LSMinimumSystemVersion": "15.0",
                }
                values.update(override.get(path, {}))
                info = zipfile.ZipInfo(path)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                output.writestr(info, plistlib.dumps(values))
            if include_symlink:
                link = zipfile.ZipInfo(
                    "XDial.app/Contents/Frameworks/Libbox.framework/Versions/Current"
                )
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(link, "A")
        checksum = self.work / f"{archive.name}.sha256"
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        return archive, checksum, digest

    def release(self, tag, archive, checksum, *, draft=False, prerelease=False,
                body="# XDial\n\n## 更新了什么\n\n- 修复连接。", assets=True,
                digest=None):
        asset_values = []
        if assets:
            for index, path in enumerate((archive, checksum), start=10):
                value = {
                    "id": index,
                    "name": path.name,
                    "state": "uploaded",
                    "size": path.stat().st_size,
                    "url": (
                        "https://api.github.com/repos/kafeifei/XDial/"
                        f"releases/assets/{index}"
                    ),
                    "browser_download_url": publisher.canonical_download_url(tag, path.name),
                }
                if digest is not None and path == archive:
                    value["digest"] = f"sha256:{digest}"
                asset_values.append(value)
        return {
            "tag_name": tag,
            "draft": draft,
            "prerelease": prerelease,
            "published_at": "2026-09-10T12:34:56Z",
            "body": body,
            "assets": asset_values,
        }

    def verified(self, tag="v1.2.3", **release_options):
        archive, checksum, digest = self.write_archive(tag)
        release = self.release(tag, archive, checksum, digest=digest, **release_options)
        return publisher.verify_release(release, tag, archive, checksum)

    def apply(self, operation, tag, verified, run="100", attempt="1",
              now=dt.datetime(2026, 9, 11, 1, 2, 3, tzinfo=dt.timezone.utc)):
        return publisher.apply_operation(
            self.root,
            operation,
            tag,
            verified,
            "caller-123",
            run,
            attempt,
            now,
        )

    def test_extracts_same_preferred_release_notes_section(self):
        body = """# XDial

## 更新了什么

- 对用户可见。

## 完整记录

- 内部记录。
"""
        self.assertEqual(publisher.release_notes(body), "- 对用户可见。")

    def test_feed_timestamps_require_utc_whole_second_rfc3339(self):
        feed = json.loads((self.root / "site/stable.json").read_text())
        for timestamp in (
            "2026-09-11T00:00:00.123Z",
            "2026-09-11T08:00:00+08:00",
            "2026-09-11 00:00:00Z",
        ):
            with self.subTest(timestamp=timestamp):
                malformed = dict(feed, generatedAt=timestamp)
                with self.assertRaisesRegex(publisher.PublishError, "UTC RFC3339"):
                    publisher.validate_feed(malformed)

    def test_rejects_malformed_tag_draft_and_prerelease(self):
        archive, checksum, _ = self.write_archive()
        valid = self.release("v1.2.3", archive, checksum)
        with self.assertRaisesRegex(publisher.PublishError, "canonical"):
            publisher.verify_release(valid, "v1.2", archive, checksum)
        with self.assertRaisesRegex(publisher.PublishError, "draft"):
            publisher.verify_release(
                self.release("v1.2.3", archive, checksum, draft=True),
                "v1.2.3", archive, checksum,
            )
        with self.assertRaisesRegex(publisher.PublishError, "prerelease"):
            publisher.verify_release(
                self.release("v1.2.3", archive, checksum, prerelease=True),
                "v1.2.3", archive, checksum,
            )

    def test_rejects_missing_notes_and_wrong_asset(self):
        archive, checksum, _ = self.write_archive()
        with self.assertRaisesRegex(publisher.PublishError, "notes are missing"):
            publisher.verify_release(
                self.release("v1.2.3", archive, checksum, body=" \n"),
                "v1.2.3", archive, checksum,
            )
        wrong = self.release("v1.2.3", archive, checksum, assets=False)
        wrong["assets"] = [{
            "name": "some-other.zip",
            "size": 1,
            "url": "https://api.github.com/repos/kafeifei/XDial/releases/assets/10",
            "browser_download_url": publisher.canonical_download_url(
                "v1.2.3", "some-other.zip"
            ),
        }]
        with self.assertRaisesRegex(publisher.PublishError, "exactly one XDial-v1.2.3.zip"):
            publisher.verify_release(wrong, "v1.2.3", archive, checksum)

    def test_rejects_bad_checksum_and_api_digest(self):
        archive, checksum, digest = self.write_archive()
        release = self.release("v1.2.3", archive, checksum)
        checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")
        release["assets"][1]["size"] = checksum.stat().st_size
        with self.assertRaisesRegex(publisher.PublishError, "published checksum"):
            publisher.verify_release(release, "v1.2.3", archive, checksum)

        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        release = self.release("v1.2.3", archive, checksum, digest="1" * 64)
        with self.assertRaisesRegex(publisher.PublishError, "API digest"):
            publisher.verify_release(release, "v1.2.3", archive, checksum)

    def test_redirect_to_release_assets_strips_api_credentials(self):
        request = urllib.request.Request(
            "https://api.github.com/repos/kafeifei/XDial/releases/assets/10",
            headers={
                "Authorization": "Bearer secret",
                "X-GitHub-Api-Version": "2022-11-28",
                "Accept": "application/octet-stream",
            },
        )
        redirected = publisher.TrustedRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://release-assets.githubusercontent.com/github-production-release-asset/file?token=x",
        )
        headers = {name.lower(): value for name, value in redirected.header_items()}
        self.assertNotIn("authorization", headers)
        self.assertNotIn("x-github-api-version", headers)
        self.assertEqual(headers["accept"], "application/octet-stream")

    def test_stream_download_stops_at_bound_before_writing_extra_bytes(self):
        class Response(io.BytesIO):
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def geturl(self):
                return "https://api.github.com/repos/kafeifei/XDial/releases/assets/10"

        class Opener:
            def open(self, request, timeout):
                return Response(b"12345")

        client = publisher.GitHubClient(None)
        client.opener = Opener()
        asset = {"name": "too-large.zip", "size": 4, "url": "https://api.github.com/asset"}
        with self.assertRaisesRegex(publisher.PublishError, "download limit"):
            client.download(asset, self.work / "download.zip", 4)

    def test_rejects_bundle_version_identity_build_and_minimum_os_mismatch(self):
        settings = (
            "XDial.app/Contents/Helpers/XDial Settings UI.app/Contents/Info.plist"
        )
        cases = (
            ({settings: {"CFBundleShortVersionString": "1.2.2"}}, "release version"),
            ({settings: {"CFBundleIdentifier": "com.example.wrong"}}, "bundle identity"),
            ({settings: {"CFBundleVersion": "2"}}, "build numbers differ"),
            ({settings: {"LSMinimumSystemVersion": "14.0"}}, "minimum OS versions differ"),
        )
        for index, (override, message) in enumerate(cases):
            with self.subTest(message=message):
                archive, checksum, _ = self.write_archive(override=override)
                release = self.release("v1.2.3", archive, checksum)
                with self.assertRaisesRegex(publisher.PublishError, message):
                    publisher.verify_release(release, "v1.2.3", archive, checksum)
                archive.unlink()
                checksum.unlink()

    def test_allows_normal_bundle_symlinks_and_rejects_acceptance_archive(self):
        archive, checksum, _ = self.write_archive(include_symlink=True)
        release = self.release("v1.2.3", archive, checksum)
        record = publisher.verify_release(release, "v1.2.3", archive, checksum)
        self.assertEqual(record["version"], "1.2.3")

        archive.unlink()
        checksum.unlink()
        host = "XDial.app/Contents/Info.plist"
        archive, checksum, _ = self.write_archive(
            override={host: {"XDialUpdateAcceptanceID": "acceptance-123"}}
        )
        release = self.release("v1.2.3", archive, checksum)
        with self.assertRaisesRegex(publisher.PublishError, "acceptance"):
            publisher.verify_release(release, "v1.2.3", archive, checksum)

    def test_publish_is_atomic_monotonic_and_preserves_other_site_files(self):
        acceptance = self.root / "site/acceptance/case.json"
        acceptance.parent.mkdir()
        acceptance.write_text('{"fixture":true}\n', encoding="utf-8")
        verified = self.verified()
        self.assertEqual(self.apply("publish", "v1.2.3", verified), "updated")

        feed = json.loads((self.root / "site/stable.json").read_text())
        ledger = json.loads((self.root / "state/ledger.json").read_text())
        self.assertGreater(feed["revision"], 1)
        self.assertEqual(feed["release"], verified)
        self.assertEqual(ledger["lastRevision"], feed["revision"])
        self.assertTrue((self.root / f"state/history/{feed['revision']}.json").exists())
        self.assertTrue(acceptance.exists())

    def test_same_publish_is_noop_but_audited(self):
        verified = self.verified()
        self.apply("publish", "v1.2.3", verified, run="100")
        original = (self.root / "site/stable.json").read_bytes()
        self.assertEqual(self.apply("publish", "v1.2.3", verified, run="101"), "noop")
        self.assertEqual((self.root / "site/stable.json").read_bytes(), original)
        audit = json.loads((self.root / "state/audit/101-1.json").read_text())
        self.assertEqual(audit["result"], "noop")
        self.assertFalse(audit["ledgerChanged"])

    def test_oversized_feed_is_rejected_without_mutation(self):
        verified = self.verified()
        verified["releaseNotes"] = "x" * publisher.MAX_FEED_BYTES
        before = (self.root / "site/stable.json").read_bytes()
        with self.assertRaisesRegex(publisher.PublishError, "512 KiB"):
            self.apply("publish", "v1.2.3", verified)
        self.assertEqual((self.root / "site/stable.json").read_bytes(), before)

    def test_older_publish_cannot_overwrite_newer_release(self):
        newer = self.verified("v2.0.0")
        self.apply("publish", "v2.0.0", newer)
        before_feed = (self.root / "site/stable.json").read_bytes()
        older = self.verified("v1.9.9")
        with self.assertRaisesRegex(publisher.PublishError, "older or equal"):
            self.apply("publish", "v1.9.9", older, run="102")
        self.assertEqual((self.root / "site/stable.json").read_bytes(), before_feed)
        self.assertFalse((self.root / "state/audit/102-1.json").exists())

    def test_withdraw_current_release_and_block_accidental_republish(self):
        verified = self.verified()
        self.apply("publish", "v1.2.3", verified)
        published_revision = json.loads(
            (self.root / "site/stable.json").read_text()
        )["revision"]
        self.assertEqual(
            self.apply("withdraw", "v1.2.3", None, run="103"),
            "updated",
        )
        feed = json.loads((self.root / "site/stable.json").read_text())
        ledger = json.loads((self.root / "state/ledger.json").read_text())
        self.assertIsNone(feed["release"])
        self.assertGreater(feed["revision"], published_revision)
        self.assertIn("v1.2.3", ledger["withdrawnTags"])
        self.assertEqual(ledger["releases"]["v1.2.3"]["status"], "withdrawn")

        self.assertEqual(self.apply("withdraw", "v1.2.3", None, run="104"), "noop")
        with self.assertRaisesRegex(publisher.PublishError, "cannot be republished"):
            self.apply("publish", "v1.2.3", verified, run="105")

    def test_withdraw_rejects_noncurrent_tag_without_changing_feed(self):
        verified = self.verified()
        self.apply("publish", "v1.2.3", verified)
        before = (self.root / "site/stable.json").read_bytes()
        with self.assertRaisesRegex(publisher.PublishError, "only the current"):
            self.apply("withdraw", "v1.2.2", None, run="106")
        self.assertEqual((self.root / "site/stable.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
