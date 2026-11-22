import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import follow_releases as follow
from test_channels import seed_placeholder_repository


def release(tag, *, draft=False, prerelease=None):
    return {"tag_name": tag, "draft": draft,
            "prerelease": "-beta." in tag if prerelease is None else prerelease}


def ledger(*tags, withdrawn=()):
    releases = {tag: {"status": "withdrawn" if tag in withdrawn else "published"} for tag in tags}
    versions = [re.match(r"v([0-9.]+?)(?:-beta\.[0-9]+)?$", tag).group(1) for tag in tags]
    highest = max(versions, key=lambda value: tuple(map(int, value.split("."))), default=None)
    return {"highestPublishedVersion": highest, "releases": releases,
            "withdrawnTags": list(withdrawn)}


class SelectionTests(unittest.TestCase):
    def test_only_public_releases_of_the_channel_form_count(self):
        releases = [
            release("v1.2.4", draft=True),
            release("v1.2.3"),
            release("v1.3.0-beta.2", draft=True),
            release("v1.3.0-beta.1"),
            release("v9.9.9", prerelease=True),
            release("v1.2.9-rc.1"),
            release("nightly"),
        ]
        self.assertEqual(follow.newest_public_release(releases, "stable"), "v1.2.3")
        self.assertEqual(follow.newest_public_release(releases, "beta"), "v1.3.0-beta.1")
        self.assertIsNone(follow.newest_public_release([release("v1.2.3", draft=True)], "stable"))

    def test_tags_compare_numerically(self):
        releases = [release("v1.2.9"), release("v1.2.10"), release("v1.3.0-beta.9"),
                    release("v1.3.0-beta.10")]
        self.assertEqual(follow.newest_public_release(releases, "stable"), "v1.2.10")
        self.assertEqual(follow.newest_public_release(releases, "beta"), "v1.3.0-beta.10")

    def test_a_tag_is_offered_only_once_and_only_moving_forward(self):
        stable = ledger("v1.2.2", "v1.2.3", withdrawn=("v1.2.3",))
        self.assertTrue(follow.unpublished("v1.2.4", "stable", stable, stable))
        self.assertFalse(follow.unpublished("v1.2.3", "stable", stable, stable))
        self.assertFalse(follow.unpublished("v1.2.1", "stable", stable, stable))
        self.assertFalse(follow.unpublished(None, "stable", stable, stable))
        self.assertTrue(follow.unpublished("v1.0.0", "stable", ledger(), ledger()))

    def test_beta_must_be_above_every_published_stable(self):
        stable = ledger("v1.3.0")
        beta = ledger("v1.3.0-beta.1")
        self.assertFalse(follow.unpublished("v1.3.0-beta.2", "beta", beta, stable))
        self.assertTrue(follow.unpublished("v1.4.0-beta.1", "beta", beta, stable))
        self.assertFalse(follow.unpublished("v1.4.0-beta.1", "beta", ledger("v1.4.0-beta.1"), stable))
        self.assertTrue(follow.unpublished("v1.4.0-beta.2", "beta", ledger("v1.4.0-beta.1"), stable))

    def test_failed_attempts_are_counted_per_channel_and_tag(self):
        runs = [
            {"status": "completed", "conclusion": "failure", "display_title": "publish stable v1.2.3 [a]"},
            {"status": "completed", "conclusion": "cancelled", "display_title": "publish stable v1.2.3 [b]"},
            {"status": "completed", "conclusion": "success", "display_title": "publish stable v1.2.3 [c]"},
            {"status": "completed", "conclusion": "failure", "display_title": "publish stable v1.2.30 [d]"},
            {"status": "completed", "conclusion": "failure", "display_title": "publish beta v1.2.3 [e]"},
            {"status": "completed", "conclusion": "failure", "display_title": "withdraw stable v1.2.3 [f]"},
        ]
        self.assertEqual(follow.attempts(runs, "stable", "v1.2.3"), 2)
        self.assertEqual(follow.attempts(runs, "beta", "v1.3.0-beta.1"), 0)


class CommandTests(unittest.TestCase):
    """main() against a placeholder repository: nothing published in either channel."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        seed_placeholder_repository(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def follow(self, runs, releases):
        commands = []

        def gh(*arguments):
            if arguments[1].startswith("repos/saymiao/xdial-updates/actions/workflows/publish.yml/runs"):
                return {"workflow_runs": runs}
            self.assertEqual(arguments[1], "repos/kafeifei/XDial/releases?per_page=100")
            return releases

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = follow.main(
                ["--repo-root", str(self.root), "--repository", "saymiao/xdial-updates",
                 "--request-id", "follow-1"],
                gh=gh, run=lambda command, **kwargs: commands.append((command, kwargs)),
            )
        self.assertEqual(result, 0)
        return commands, output.getvalue()

    def test_dispatches_stable_before_beta(self):
        commands, _ = self.follow([], [release("v1.2.3"), release("v1.3.0-beta.1")])
        self.assertEqual(commands, [([
            "gh", "workflow", "run", "publish.yml", "--repo", "saymiao/xdial-updates",
            "--ref", "main", "-f", "operation=publish", "-f", "channel=stable",
            "-f", "release_tag=v1.2.3", "-f", "request_id=follow-1",
        ], {"check": True})])

    def test_dispatches_beta_when_stable_is_current(self):
        commands, _ = self.follow([], [release("v1.3.0-beta.1")])
        self.assertIn("channel=beta", commands[0][0])
        self.assertIn("release_tag=v1.3.0-beta.1", commands[0][0])

    def test_waits_while_any_publish_run_is_queued_or_running(self):
        for status in ("queued", "in_progress", "waiting"):
            with self.subTest(status=status):
                commands, output = self.follow(
                    [{"status": status, "html_url": "https://example.invalid/1"}],
                    [release("v1.2.3")],
                )
                self.assertEqual(commands, [])
                self.assertIn("queued or running", output)

    def test_stops_retrying_after_the_attempt_limit(self):
        failed = {"status": "completed", "conclusion": "failure",
                  "display_title": "publish stable v1.2.3 [x]"}
        commands, _ = self.follow([failed], [release("v1.2.3")])
        self.assertEqual(len(commands), 1)
        commands, output = self.follow([failed, failed], [release("v1.2.3")])
        self.assertEqual(commands, [])
        self.assertIn("::warning::", output)

    def test_nothing_to_do(self):
        commands, output = self.follow([], [release("v1.2.3", draft=True)])
        self.assertEqual(commands, [])
        self.assertIn("already carry", output)


class FollowWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github/workflows/follow.yml").read_text(encoding="utf-8")

    def test_dispatches_with_its_own_token_only(self):
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.text)
        self.assertNotIn("secrets.", self.text)
        self.assertIn("  actions: write\n", self.text)

    def test_keepalive_runs_only_on_its_own_schedule(self):
        crons = re.findall(r'^    - cron: "(.*)"$', self.text, re.MULTILINE)
        self.assertEqual(len(crons), 2)
        self.assertIn(f"if: github.event.schedule == '{crons[1]}'", self.text)
        self.assertIn("actions/workflows/follow.yml/enable", self.text)


if __name__ == "__main__":
    unittest.main()
