import datetime as dt
import hashlib
import io
import json
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import publish_feed as publisher
import verify_deployment


HOST = "XDial.app/Contents/Info.plist"
SETTINGS = "XDial.app/Contents/Helpers/XDial Settings UI.app/Contents/Info.plist"
EXTENSION = (
    "XDial.app/Contents/Library/SystemExtensions/"
    "com.kafeifei.xdial.app.transparent-proxy.systemextension/Contents/Info.plist"
)
EMPTY_LEDGER = {
    "schemaVersion": 1,
    "lastRevision": 1,
    "highestPublishedVersion": None,
    "releases": {},
    "withdrawnTags": [],
}


def version_of(tag):
    return re.match(r"v([0-9]+\.[0-9]+\.[0-9]+)", tag).group(1)


class ChannelFixture(unittest.TestCase):
    """A repository with the stable and beta placeholders and no publications."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "site").mkdir()
        (self.root / "state/history").mkdir(parents=True)
        (self.root / "state/beta/history").mkdir(parents=True)
        shutil.copy(ROOT / "state/history/1.json", self.root / "site/stable.json")
        shutil.copy(ROOT / "state/history/1.json", self.root / "state/history/1.json")
        (self.root / "state/ledger.json").write_text(json.dumps(EMPTY_LEDGER), encoding="utf-8")
        shutil.copy(ROOT / "site/beta.json", self.root / "site/beta.json")
        shutil.copy(ROOT / "state/beta/history/1.json", self.root / "state/beta/history/1.json")
        shutil.copy(ROOT / "state/beta/ledger.json", self.root / "state/beta/ledger.json")
        self.work = self.root / "fixtures"
        self.work.mkdir()
        self.clock = dt.datetime(2026, 10, 18, 1, 0, 0, tzinfo=dt.timezone.utc)
        self.run = 1000

    def tearDown(self):
        self.temporary.cleanup()

    def read(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def snapshot(self, *relatives):
        return {relative: (self.root / relative).read_bytes() for relative in relatives}

    def tree(self, relative):
        base = self.root / relative
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in sorted(base.rglob("*")) if path.is_file()
        }

    def stable_state(self):
        result = self.snapshot("site/stable.json", "state/ledger.json")
        result.update(self.tree("state/history"))
        if (self.root / "state/audit").exists():
            result.update(self.tree("state/audit"))
        return result

    def beta_state(self):
        result = self.snapshot("site/beta.json")
        result.update(self.tree("state/beta"))
        return result

    def write_archive(self, tag, *, build="1800000000", version=None, override=None):
        override = override or {}
        version = version or version_of(tag)
        archive = self.work / f"XDial-{tag}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path, identifier in publisher.BUNDLES.items():
                values = {
                    "CFBundleIdentifier": identifier,
                    "CFBundleShortVersionString": version,
                    "CFBundleVersion": build,
                    "LSMinimumSystemVersion": "15.0",
                }
                values.update(override.get(path, {}))
                info = zipfile.ZipInfo(path)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                output.writestr(info, plistlib.dumps(values))
        checksum = self.work / f"{archive.name}.sha256"
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        return archive, checksum

    def release(self, tag, archive, checksum, *, draft=False, prerelease=None):
        if prerelease is None:
            prerelease = "-beta." in tag
        assets = []
        for index, path in enumerate((archive, checksum), start=10):
            assets.append({
                "id": index,
                "name": path.name,
                "state": "uploaded",
                "size": path.stat().st_size,
                "url": f"https://api.github.com/repos/kafeifei/XDial/releases/assets/{index}",
                "browser_download_url": publisher.canonical_download_url(tag, path.name),
            })
        return {
            "tag_name": tag,
            "draft": draft,
            "prerelease": prerelease,
            "published_at": "2026-10-17T12:34:56Z",
            "body": "# XDial\n\n## 更新了什么\n\n- 测试通道。",
            "assets": assets,
        }

    def verified(self, tag, channel, *, build="1800000000", override=None, **release_options):
        archive, checksum = self.write_archive(tag, build=build, override=override)
        release = self.release(tag, archive, checksum, **release_options)
        record = publisher.verify_release(release, tag, archive, checksum, channel)
        archive.unlink()
        checksum.unlink()
        return record

    def apply(self, operation, tag, verified=None, *, channel, history_revision=None):
        self.clock += dt.timedelta(minutes=1)
        self.run += 1
        return publisher.apply_operation(
            self.root, operation, tag, verified, "caller-1", str(self.run), "1", self.clock,
            channel=channel, history_revision=history_revision,
        )

    def publish(self, tag, channel, build):
        record = self.verified(tag, channel, build=build)
        return self.apply("publish", tag, record, channel=channel)


class BetaReleaseVerificationTests(ChannelFixture):
    def test_accepts_public_beta_prerelease_with_formal_identity(self):
        record = self.verified("v1.4.0-beta.1", "beta", build="1800000001")
        self.assertEqual(record["tag"], "v1.4.0-beta.1")
        self.assertEqual(record["version"], "1.4.0")
        self.assertEqual(record["build"], "1800000001")
        self.assertEqual(
            record["archiveURL"],
            "https://github.com/kafeifei/XDial/releases/download/"
            "v1.4.0-beta.1/XDial-v1.4.0-beta.1.zip",
        )
        self.assertEqual(set(record), publisher.RELEASE_KEYS)

    def test_beta_requires_prerelease_and_rejects_draft(self):
        with self.assertRaisesRegex(publisher.PublishError, "GitHub prereleases"):
            self.verified("v1.4.0-beta.1", "beta", prerelease=False)
        with self.assertRaisesRegex(publisher.PublishError, "draft"):
            self.verified("v1.4.0-beta.1", "beta", draft=True)

    def test_beta_tag_grammar_is_strict(self):
        archive, checksum = self.write_archive("v1.4.0-beta.1")
        for tag in (
            "v1.4.0-beta.0",
            "v1.4.0-beta.01",
            "v1.4.0-beta.",
            "v1.4.0-beta",
            "v1.4.0-rc.1",
            "v1.4.0-Beta.1",
            "v01.4.0-beta.1",
            "v1.4-beta.1",
            "1.4.0-beta.1",
            "v1.4.0-beta.1\n",
            "v1.4.0-beta.1.2",
        ):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(publisher.PublishError, "canonical vMAJOR.MINOR.PATCH-beta.N"):
                    publisher.verify_release(
                        self.release(tag, archive, checksum, prerelease=True),
                        tag, archive, checksum, "beta",
                    )
        with self.assertRaisesRegex(publisher.PublishError, "stable tags cannot be published to the beta"):
            publisher.verify_release(
                self.release("v1.4.0", archive, checksum, prerelease=True),
                "v1.4.0", archive, checksum, "beta",
            )

    def test_beta_bundles_must_use_formal_identity_and_tag_version(self):
        cases = (
            ({SETTINGS: {"CFBundleIdentifier": "com.kafeifei.xdial.next.settings-ui"}}, "bundle identity"),
            ({HOST: {"CFBundleShortVersionString": "1.4.0-beta.1"}}, "release version"),
            ({EXTENSION: {"CFBundleShortVersionString": "1.3.9"}}, "release version"),
            ({EXTENSION: {"CFBundleVersion": "1800000002"}}, "build numbers differ"),
        )
        for override, message in cases:
            with self.subTest(message=message, override=override):
                with self.assertRaisesRegex(publisher.PublishError, message):
                    self.verified("v1.4.0-beta.1", "beta", override=override)

    def test_beta_release_tag_key_must_match_when_present(self):
        tag = "v1.4.0-beta.1"
        matching = {path: {"XDialReleaseTag": tag} for path in publisher.BUNDLES}
        self.assertEqual(self.verified(tag, "beta", override=matching)["tag"], tag)
        self.assertEqual(
            self.verified(tag, "beta", override={HOST: {"XDialReleaseTag": tag}})["tag"], tag
        )
        for path in publisher.BUNDLES:
            for value in ("v1.4.0-beta.2", "v1.4.0", ""):
                with self.subTest(path=path, value=value):
                    with self.assertRaisesRegex(publisher.PublishError, "XDialReleaseTag"):
                        self.verified(tag, "beta", override={path: {"XDialReleaseTag": value}})

    def test_beta_rejects_acceptance_archives(self):
        with self.assertRaisesRegex(publisher.PublishError, "acceptance update archives cannot be published to beta"):
            self.verified(
                "v1.4.0-beta.1", "beta",
                override={HOST: {"XDialUpdateAcceptanceID": "acceptance-1"}},
            )
        record = self.verified(
            "v1.4.0-beta.1", "beta", override={HOST: {"XDialUpdateAcceptanceID": ""}}
        )
        self.assertEqual(record["version"], "1.4.0")


class StableChannelGuardTests(ChannelFixture):
    def test_stable_rejects_beta_tags_before_anything_else(self):
        archive, checksum = self.write_archive("v1.4.0-beta.1")
        for prerelease in (False, True):
            with self.subTest(prerelease=prerelease):
                with self.assertRaisesRegex(publisher.PublishError, "beta tags cannot be published to the stable"):
                    publisher.verify_release(
                        self.release("v1.4.0-beta.1", archive, checksum, prerelease=prerelease),
                        "v1.4.0-beta.1", archive, checksum,
                    )
        beta_record = self.verified("v1.4.0-beta.1", "beta")
        before = self.stable_state()
        with self.assertRaisesRegex(publisher.PublishError, "beta tags cannot be published to the stable"):
            self.apply("publish", "v1.4.0-beta.1", beta_record, channel="stable")
        with self.assertRaisesRegex(publisher.PublishError, "beta tags cannot be published to the stable"):
            self.apply("withdraw", "v1.4.0-beta.1", channel="stable")
        self.assertEqual(self.stable_state(), before)

    def test_stable_still_rejects_prereleases(self):
        with self.assertRaisesRegex(publisher.PublishError, "prereleases cannot be published"):
            self.verified("v1.4.0", "stable", prerelease=True)

    def test_feeds_reject_records_from_the_other_channel(self):
        stable_record = self.verified("v1.4.0", "stable")
        beta_record = self.verified("v1.5.0-beta.1", "beta")
        with self.assertRaisesRegex(publisher.PublishError, "stable tags cannot be published to the beta"):
            self.apply("publish", "v1.4.0", stable_record, channel="beta")
        feed = self.read("site/beta.json")
        with self.assertRaisesRegex(publisher.PublishError, "feed channel must be beta"):
            publisher.validate_feed(dict(feed, channel="stable"), "beta")
        with self.assertRaisesRegex(publisher.PublishError, "beta tags cannot be published to the stable"):
            publisher.validate_feed(dict(self.read("site/stable.json"), release=beta_record))
        with self.assertRaisesRegex(publisher.PublishError, "unsupported channel"):
            publisher.validate_feed(feed, "next")


class ChannelIndependenceTests(ChannelFixture):
    def test_beta_publish_writes_only_beta_state_with_stable_structure(self):
        before = self.stable_state()
        self.assertEqual(self.publish("v1.4.0-beta.1", "beta", "1800000001"), "updated")
        self.assertEqual(self.stable_state(), before)

        feed = self.read("site/beta.json")
        stable_placeholder = self.read("site/stable.json")
        self.assertEqual(list(feed), list(stable_placeholder))
        self.assertEqual(feed["schemaVersion"], 1)
        self.assertEqual(feed["channel"], "beta")
        self.assertGreater(feed["revision"], 1)
        self.assertEqual(list(feed["release"]), [
            "tag", "version", "build", "minimumSystemVersion", "publishedAt",
            "releaseNotes", "archiveURL", "archiveSize", "archiveSHA256",
        ])
        self.assertEqual(self.read(f"state/beta/history/{feed['revision']}.json"), feed)
        ledger = self.read("state/beta/ledger.json")
        self.assertEqual(ledger["lastRevision"], feed["revision"])
        self.assertEqual(ledger["highestPublishedVersion"], "1.4.0")
        self.assertEqual(ledger["releases"]["v1.4.0-beta.1"]["status"], "published")
        audit = self.read(f"state/beta/audit/{self.run}-1.json")
        self.assertEqual(audit["channel"], "beta")
        self.assertEqual(audit["releaseTag"], "v1.4.0-beta.1")
        self.assertFalse((self.root / f"state/audit/{self.run}-1.json").exists())

    def test_stable_publish_keeps_original_feed_and_audit_format(self):
        before_beta = self.beta_state()
        self.publish("v1.3.0", "stable", "1800000000")
        self.assertEqual(self.beta_state(), before_beta)
        feed = self.read("site/stable.json")
        self.assertEqual(list(feed), ["schemaVersion", "revision", "channel", "generatedAt", "release"])
        self.assertEqual(feed["channel"], "stable")
        audit = self.read(f"state/audit/{self.run}-1.json")
        self.assertEqual(list(audit), [
            "schemaVersion", "operation", "releaseTag", "requestId", "runId", "runAttempt",
            "recordedAt", "result", "feedRevisionBefore", "feedRevisionAfter", "ledgerChanged",
        ])
        self.assertEqual(
            (self.root / "site/stable.json").read_bytes(),
            publisher.json_payload(feed),
        )

    def test_revision_chains_are_independent(self):
        self.publish("v1.3.0", "stable", "1800000000")
        stable_revision = self.read("site/stable.json")["revision"]
        self.assertEqual(self.read("site/beta.json")["revision"], 1)
        self.publish("v1.4.0-beta.1", "beta", "1800000001")
        beta_revision = self.read("site/beta.json")["revision"]
        self.assertEqual(self.read("site/stable.json")["revision"], stable_revision)
        self.assertEqual(self.read("state/ledger.json")["lastRevision"], stable_revision)
        self.assertEqual(self.read("state/beta/ledger.json")["lastRevision"], beta_revision)
        self.assertEqual(sorted(p.name for p in (self.root / "state/history").iterdir()),
                         sorted(["1.json", f"{stable_revision}.json"]))
        self.assertEqual(sorted(p.name for p in (self.root / "state/beta/history").iterdir()),
                         sorted(["1.json", f"{beta_revision}.json"]))

    def test_withdraw_is_per_channel(self):
        self.publish("v1.3.0", "stable", "1800000000")
        self.publish("v1.4.0-beta.1", "beta", "1800000001")
        stable_before = self.stable_state()
        with self.assertRaisesRegex(publisher.PublishError, "stable tags cannot be published to the beta"):
            self.apply("withdraw", "v1.3.0", channel="beta")
        self.assertEqual(self.apply("withdraw", "v1.4.0-beta.1", channel="beta"), "updated")
        self.assertEqual(self.stable_state(), stable_before)
        beta = self.read("site/beta.json")
        self.assertIsNone(beta["release"])
        ledger = self.read("state/beta/ledger.json")
        self.assertEqual(ledger["withdrawnTags"], ["v1.4.0-beta.1"])
        self.assertEqual(self.read("site/stable.json")["release"]["tag"], "v1.3.0")
        self.assertEqual(self.apply("withdraw", "v1.4.0-beta.1", channel="beta"), "noop")
        with self.assertRaisesRegex(publisher.PublishError, "cannot be republished"):
            self.publish("v1.4.0-beta.1", "beta", "1800000002")

    def test_beta_releases_are_ordered_by_version_then_beta_number(self):
        self.publish("v1.4.0-beta.1", "beta", "1800000001")
        self.publish("v1.4.0-beta.2", "beta", "1800000002")
        self.publish("v1.4.0-beta.10", "beta", "1800000003")
        self.assertEqual(self.read("site/beta.json")["release"]["tag"], "v1.4.0-beta.10")
        for tag, build in (("v1.4.0-beta.9", "1800000004"), ("v1.3.9-beta.20", "1800000005")):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(publisher.PublishError, "older or equal"):
                    self.publish(tag, "beta", build)
        self.publish("v1.5.0-beta.1", "beta", "1800000006")
        self.assertEqual(self.read("state/beta/ledger.json")["highestPublishedVersion"], "1.5.0")

    def test_beta_must_be_newer_than_every_published_stable_release(self):
        self.publish("v1.4.0", "stable", "1800000000")
        self.apply("withdraw", "v1.4.0", channel="stable")
        before = self.beta_state()
        for tag in ("v1.4.0-beta.1", "v1.3.0-beta.1"):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(publisher.PublishError, "newer than every published stable"):
                    self.publish(tag, "beta", "1800000009")
        self.assertEqual(self.beta_state(), before)
        self.assertEqual(self.publish("v1.4.1-beta.1", "beta", "1800000009"), "updated")


class BuildGateTests(ChannelFixture):
    def test_same_version_stable_must_outrank_withdrawn_beta(self):
        self.publish("v1.4.0-beta.1", "beta", "1800000200")
        self.apply("withdraw", "v1.4.0-beta.1", channel="beta")
        before = self.stable_state()
        with self.assertRaisesRegex(
            publisher.PublishError,
            "build 1800000150 of v1.4.0 must be greater than build 1800000200 of published beta v1.4.0-beta.1",
        ):
            self.publish("v1.4.0", "stable", "1800000150")
        self.assertEqual(self.stable_state(), before)
        with self.assertRaisesRegex(publisher.PublishError, "must be greater than build 1800000200"):
            self.publish("v1.4.0", "stable", "1800000200")
        self.assertEqual(self.publish("v1.4.0", "stable", "1800000201"), "updated")

    def test_stable_hotfix_below_open_beta_line_ignores_beta_build(self):
        self.publish("v1.3.0", "stable", "1800000100")
        self.publish("v1.4.0-beta.1", "beta", "1800000300")
        self.assertEqual(self.publish("v1.3.1", "stable", "1800000200"), "updated")
        with self.assertRaisesRegex(publisher.PublishError, "of published stable v1.3.0"):
            self.publish("v1.3.2", "stable", "1800000050")

    def test_next_beta_must_outrank_earlier_betas_and_stable_releases(self):
        self.publish("v1.3.0", "stable", "1800000100")
        self.publish("v1.4.0-beta.1", "beta", "1800000300")
        self.publish("v1.3.1", "stable", "1800000400")
        with self.assertRaisesRegex(publisher.PublishError, "of published beta v1.4.0-beta.1"):
            self.publish("v1.4.0-beta.2", "beta", "1800000250")
        with self.assertRaisesRegex(publisher.PublishError, "of published stable v1.3.1"):
            self.publish("v1.4.0-beta.2", "beta", "1800000350")
        self.assertEqual(self.publish("v1.4.0-beta.2", "beta", "1800000401"), "updated")

    def test_build_gate_counts_withdrawn_stable_releases(self):
        self.publish("v1.3.0", "stable", "1800000100")
        self.apply("withdraw", "v1.3.0", channel="stable")
        with self.assertRaisesRegex(publisher.PublishError, "of published stable v1.3.0"):
            self.publish("v1.3.1", "stable", "1800000099")

    def test_missing_history_fails_closed(self):
        self.publish("v1.4.0-beta.1", "beta", "1800000200")
        revision = self.read("state/beta/ledger.json")["releases"]["v1.4.0-beta.1"]["publishedRevision"]
        (self.root / f"state/beta/history/{revision}.json").unlink()
        with self.assertRaisesRegex(publisher.PublishError, "cannot prove the build of v1.4.0-beta.1"):
            self.publish("v1.4.0", "stable", "1800000300")

    def test_tampered_history_fails_closed(self):
        self.publish("v1.3.0", "stable", "1800000100")
        self.publish("v1.3.1", "stable", "1800000200")
        revision = self.read("state/ledger.json")["releases"]["v1.3.0"]["publishedRevision"]
        path = self.root / f"state/history/{revision}.json"
        tampered = json.loads(path.read_text())
        tampered["release"]["archiveSHA256"] = "0" * 64
        path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(publisher.PublishError, "cannot prove the build of v1.3.0"):
            self.publish("v1.3.2", "stable", "1800000300")

    def test_missing_beta_state_counts_as_no_beta_history(self):
        (self.root / "site/beta.json").unlink()
        shutil.rmtree(self.root / "state/beta")
        self.assertEqual(self.publish("v1.3.0", "stable", "1800000100"), "updated")

    def test_beta_ledger_without_feed_fails_closed(self):
        (self.root / "site/beta.json").unlink()
        with self.assertRaisesRegex(publisher.PublishError, "beta.json"):
            self.publish("v1.3.0", "stable", "1800000100")

    def test_builds_of_the_live_repository_are_derivable(self):
        live = Path(self.temporary.name) / "live"
        shutil.copytree(ROOT / "site", live / "site")
        shutil.copytree(ROOT / "state", live / "state")
        for channel in publisher.CHANNELS:
            with self.subTest(channel=channel):
                _, ledger = publisher.load_channel_state(live, channel)
                packages = publisher.published_packages(live, channel, ledger)
                self.assertEqual(len(packages), len(ledger["releases"]))
                self.assertTrue(all(package.build > 0 for package in packages))


class RestoreTests(ChannelFixture):
    def publish_two(self, channel="stable"):
        first, second = (("v1.3.0", "v1.3.1") if channel == "stable"
                         else ("v1.4.0-beta.1", "v1.4.0-beta.2"))
        self.publish(first, channel, "1800000100")
        first_revision = self.read(publisher.CHANNEL_PATHS[channel].feed)["revision"]
        self.publish(second, channel, "1800000200")
        return first, second, first_revision

    def test_restore_republishes_history_bytes_under_a_new_revision(self):
        first, second, first_revision = self.publish_two()
        current = self.read("site/stable.json")
        beta_before = self.beta_state()

        self.assertEqual(
            self.apply("restore", "", channel="stable", history_revision=first_revision),
            "updated",
        )
        feed = self.read("site/stable.json")
        self.assertGreater(feed["revision"], current["revision"])
        self.assertEqual(feed["generatedAt"], publisher.isoformat_utc(self.clock))
        original = self.read(f"state/history/{first_revision}.json")
        expected = dict(original, revision=feed["revision"], generatedAt=feed["generatedAt"])
        self.assertEqual((self.root / "site/stable.json").read_bytes(), publisher.json_payload(expected))
        self.assertEqual(self.read(f"state/history/{feed['revision']}.json"), feed)

        ledger = self.read("state/ledger.json")
        self.assertEqual(ledger["lastRevision"], feed["revision"])
        self.assertEqual(ledger["releases"][first]["status"], "published")
        self.assertEqual(ledger["releases"][first]["publishedRevision"], first_revision)
        self.assertEqual(ledger["releases"][second]["status"], "withdrawn")
        self.assertEqual(ledger["releases"][second]["withdrawnRevision"], feed["revision"])
        self.assertEqual(ledger["highestPublishedVersion"], "1.3.1")

        audit = self.read(f"state/audit/{self.run}-1.json")
        self.assertEqual(audit["operation"], "restore")
        self.assertEqual(audit["channel"], "stable")
        self.assertEqual(audit["releaseTag"], first)
        self.assertEqual(audit["historyRevision"], first_revision)
        self.assertEqual(audit["replacedReleaseTag"], second)
        self.assertEqual(audit["feedRevisionBefore"], current["revision"])
        self.assertEqual(audit["feedRevisionAfter"], feed["revision"])
        self.assertTrue(audit["ledgerChanged"])
        self.assertEqual(self.beta_state(), beta_before)

    def test_restore_rerun_is_an_audited_noop(self):
        _, _, first_revision = self.publish_two()
        self.apply("restore", "", channel="stable", history_revision=first_revision)
        before = self.snapshot("site/stable.json", "state/ledger.json")
        self.assertEqual(
            self.apply("restore", None, channel="stable", history_revision=first_revision),
            "noop",
        )
        self.assertEqual(self.snapshot("site/stable.json", "state/ledger.json"), before)
        audit = self.read(f"state/audit/{self.run}-1.json")
        self.assertEqual(audit["result"], "noop")
        self.assertIsNone(audit["replacedReleaseTag"])
        self.assertFalse(audit["ledgerChanged"])

    def test_after_restore_displaced_release_stays_retired(self):
        _, second, first_revision = self.publish_two()
        self.apply("restore", "", channel="stable", history_revision=first_revision)
        with self.assertRaisesRegex(publisher.PublishError, "cannot be republished"):
            self.publish(second, "stable", "1800000200")
        self.assertEqual(self.publish("v1.3.2", "stable", "1800000300"), "updated")

    def test_restore_rejects_withdrawn_release_and_bad_revisions(self):
        _, second, first_revision = self.publish_two()
        current_revision = self.read("site/stable.json")["revision"]
        self.apply("withdraw", second, channel="stable")
        self.apply("restore", "", channel="stable", history_revision=first_revision)
        self.publish("v1.3.2", "stable", "1800000300")
        self.apply("withdraw", "v1.3.2", channel="stable")
        before = self.stable_state()
        cases = (
            (current_revision, "withdrawn release cannot be restored"),
            (first_revision + 1, "cannot read valid JSON"),
            (10 ** 15, "newer than the current feed"),
            (0, "positive history revision"),
            (None, "positive history revision"),
        )
        for revision, message in cases:
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(publisher.PublishError, message):
                    self.apply("restore", "", channel="stable", history_revision=revision)
        with self.assertRaisesRegex(publisher.PublishError, "do not pass a release tag"):
            self.apply("restore", "v1.3.0", channel="stable", history_revision=first_revision)
        self.assertEqual(self.stable_state(), before)

    def test_restore_rejects_history_that_differs_from_original_publication(self):
        first, _, first_revision = self.publish_two()
        current = self.read("site/stable.json")
        forged = dict(current, revision=current["revision"] - 1)
        forged["release"] = dict(self.read(f"state/history/{first_revision}.json")["release"])
        forged["release"]["releaseNotes"] = "- changed later"
        (self.root / f"state/history/{forged['revision']}.json").write_text(
            json.dumps(forged), encoding="utf-8"
        )
        with self.assertRaisesRegex(publisher.PublishError, "differs from its original publication"):
            self.apply("restore", "", channel="stable", history_revision=forged["revision"])

    def test_restore_beta_to_placeholder_leaves_stable_untouched(self):
        self.publish("v1.3.0", "stable", "1800000050")
        _, second, _ = self.publish_two("beta")
        stable_before = self.stable_state()
        self.assertEqual(self.apply("restore", "", channel="beta", history_revision=1), "updated")
        beta = self.read("site/beta.json")
        self.assertIsNone(beta["release"])
        self.assertGreater(beta["revision"], 1)
        self.assertEqual(self.read("state/beta/ledger.json")["withdrawnTags"], [second])
        self.assertEqual(self.stable_state(), stable_before)
        audit = self.read(f"state/beta/audit/{self.run}-1.json")
        self.assertEqual((audit["channel"], audit["releaseTag"]), ("beta", None))

    def test_restore_cannot_cross_channels(self):
        _, _, stable_revision = self.publish_two()
        self.publish("v1.4.0-beta.1", "beta", "1800000300")
        self.assertGreater(self.read("site/beta.json")["revision"], stable_revision)
        with self.assertRaisesRegex(publisher.PublishError, "cannot read valid JSON"):
            self.apply("restore", "", channel="beta", history_revision=stable_revision)


class CommandLineTests(ChannelFixture):
    def test_operation_inputs(self):
        self.assertIsNone(publisher.validate_arguments("publish", "stable", "v1.3.0", ""))
        self.assertIsNone(publisher.validate_arguments("withdraw", "beta", "v1.4.0-beta.1", ""))
        self.assertEqual(
            publisher.validate_arguments("restore", "stable", "", "1791940209492"),
            1791940209492,
        )
        cases = (
            (("publish", "stable", "", ""), "publish requires --release-tag"),
            (("withdraw", "beta", "", ""), "withdraw requires --release-tag"),
            (("publish", "stable", "v1.3.0", "12"), "only valid for restore"),
            (("publish", "stable", "v1.4.0-beta.1", ""), "beta tags cannot be published to the stable"),
            (("publish", "beta", "v1.4.0", ""), "stable tags cannot be published to the beta"),
            (("restore", "stable", "", ""), "positive integer revision"),
            (("restore", "stable", "", "012"), "positive integer revision"),
            (("restore", "stable", "", "1.5"), "positive integer revision"),
            (("restore", "stable", "", " 12"), "positive integer revision"),
            (("restore", "stable", "v1.3.0", "12"), "do not pass a release tag"),
            (("restore", "next", "", "12"), "unsupported channel"),
            (("rollback", "stable", "v1.3.0", ""), "unsupported operation"),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(publisher.PublishError, message):
                    publisher.validate_arguments(*arguments)

    def test_parser_defaults_to_stable(self):
        args = publisher.parser().parse_args([
            "--operation", "publish", "--release-tag", "v1.3.0",
            "--run-id", "1", "--run-attempt", "1",
        ])
        self.assertEqual(args.channel, "stable")
        self.assertEqual(args.history_revision, "")

    def test_restore_command_runs_offline_against_a_checkout(self):
        self.publish("v1.4.0-beta.1", "beta", "1800000100")
        before = self.stable_state()
        command = [
            sys.executable, str(ROOT / "scripts/publish_feed.py"),
            "--operation", "restore", "--channel", "beta", "--history-revision", "1",
            "--repo-root", str(self.root), "--run-id", "local", "--run-attempt", "1",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("restore beta history revision 1: updated", completed.stdout)
        self.assertIsNone(self.read("site/beta.json")["release"])
        self.assertEqual(self.stable_state(), before)
        self.assertTrue((self.root / "state/beta/audit/local-1.json").exists())

        rejected = subprocess.run(
            command[:2] + ["--operation", "restore", "--channel", "beta",
                           "--repo-root", str(self.root), "--run-id", "local", "--run-attempt", "2"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("restore requires --history-revision", rejected.stderr)


class BetaPlaceholderTests(unittest.TestCase):
    def test_beta_placeholder_matches_the_original_stable_placeholder_format(self):
        # history/1.json is the permanent record of the placeholder; site/beta.json
        # only equals it until the first beta publication.
        beta = (ROOT / "state/beta/history/1.json").read_bytes()
        feed = json.loads(beta)
        publisher.validate_feed(feed, "beta")
        self.assertEqual(feed["revision"], 1)
        self.assertIsNone(feed["release"])
        self.assertEqual(beta, publisher.json_payload(feed))
        publisher.validate_feed(json.loads((ROOT / "site/beta.json").read_bytes()), "beta")
        stable_placeholder = json.loads((ROOT / "state/history/1.json").read_bytes())
        self.assertEqual(
            beta,
            publisher.json_payload(
                dict(stable_placeholder, channel="beta", generatedAt=feed["generatedAt"])
            ),
        )
        feed_now, ledger = publisher.load_channel_state(ROOT, "beta")
        self.assertEqual(json.loads((ROOT / "state/beta/history" / f"{feed_now['revision']}.json").read_bytes()), feed_now)
        self.assertEqual(json.loads((ROOT / "state/ledger.json").read_bytes())["schemaVersion"], ledger["schemaVersion"])


class PublishWorkflowTests(unittest.TestCase):
    """Static checks of publish.yml without a YAML dependency."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")

    def input_block(self, name):
        match = re.search(
            rf"^      {name}:\n((?:        .*\n|          .*\n)+)", self.text, re.MULTILINE
        )
        self.assertIsNotNone(match, f"missing workflow input {name}")
        return match.group(1)

    def test_inputs(self):
        channel = self.input_block("channel")
        self.assertIn("type: choice", channel)
        self.assertIn("default: stable", channel)
        self.assertIn("required: false", channel)
        self.assertEqual(re.findall(r"^          - (\S+)$", channel, re.MULTILINE), ["stable", "beta"])
        operation = self.input_block("operation")
        self.assertEqual(
            re.findall(r"^          - (\S+)$", operation, re.MULTILINE),
            ["publish", "withdraw", "restore"],
        )
        self.assertIn("default: publish", operation)
        history = self.input_block("history_revision")
        self.assertIn("type: string", history)
        self.assertIn("required: false", history)
        self.assertIn("required: false", self.input_block("release_tag"))
        self.assertIn("required: false", self.input_block("request_id"))

    def test_run_name_carries_channel_and_request_id(self):
        run_name = re.search(r"^run-name: (.*)$", self.text, re.MULTILINE).group(1)
        self.assertIn("inputs.channel", run_name)
        self.assertIn("[${{ inputs.request_id }}]", run_name)

    def test_concurrency_stays_one_global_group(self):
        self.assertEqual(re.findall(r"^  group: (.*)$", self.text, re.MULTILINE), ["stable-feed-publisher"])
        self.assertIn("cancel-in-progress: false", self.text)

    def test_publisher_receives_channel_and_history_revision(self):
        self.assertIn("CHANNEL: ${{ inputs.channel || 'stable' }}", self.text)
        self.assertIn("HISTORY_REVISION: ${{ inputs.history_revision }}", self.text)
        self.assertIn('--channel "$CHANNEL"', self.text)
        self.assertIn('--history-revision "$HISTORY_REVISION"', self.text)

    def test_commit_includes_both_channels(self):
        self.assertIn("git add -- site/stable.json state/ledger.json state/history state/audit", self.text)
        self.assertIn("git add -- site/beta.json", self.text)
        self.assertIn("git add -- state/beta", self.text)

    def test_deployment_verifies_both_canonical_feeds(self):
        verify = self.text[self.text.index("- name: Verify canonical deployed feeds"):]
        self.assertIn("--channel stable", verify)
        self.assertIn("--url https://saymiao.github.io/xdial-updates/stable.json", verify)
        self.assertIn("if [ -e site/beta.json ]", verify)
        self.assertIn("--channel beta", verify)
        self.assertIn("--url https://saymiao.github.io/xdial-updates/beta.json", verify)
        self.assertLess(verify.index("stable.json"), verify.index("beta.json"))


class BetaDeploymentVerifierTests(unittest.TestCase):
    class Response(io.BytesIO):
        status = 200

        def __init__(self, payload, url):
            super().__init__(payload)
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def geturl(self):
            return self.url

    class Opener:
        def __init__(self, payload, url):
            self.payload = payload
            self.url = url

        def open(self, request, timeout):
            return BetaDeploymentVerifierTests.Response(self.payload, self.url)

    def test_beta_canonical_url_and_channel_validation(self):
        url = verify_deployment.CANONICAL_FEED_URLS["beta"]
        self.assertEqual(url, "https://saymiao.github.io/xdial-updates/beta.json")
        payload = (ROOT / "site/beta.json").read_bytes()
        self.assertEqual(verify_deployment.fetch_once(self.Opener(payload, url), url), payload)
        verify_deployment.validate_payload(payload, "beta")
        with self.assertRaisesRegex(publisher.PublishError, "feed channel must be stable"):
            verify_deployment.validate_payload(payload)
        stable_payload = (ROOT / "site/stable.json").read_bytes()
        with self.assertRaisesRegex(publisher.PublishError, "feed channel must be beta"):
            verify_deployment.validate_payload(stable_payload, "beta")

    def test_verify_requires_the_url_of_its_channel(self):
        with self.assertRaisesRegex(publisher.PublishError, "canonical beta feed URL"):
            verify_deployment.verify(
                ROOT / "site/beta.json", verify_deployment.CANONICAL_FEED_URL, 1, 0, "beta"
            )
        with self.assertRaisesRegex(publisher.PublishError, "canonical stable feed URL"):
            verify_deployment.verify(
                ROOT / "site/stable.json", verify_deployment.CANONICAL_FEED_URLS["beta"], 1, 0
            )


if __name__ == "__main__":
    unittest.main()
