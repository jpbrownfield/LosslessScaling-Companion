from contextlib import redirect_stdout
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import create_release
from create_release import ReleaseError, git, latest_version, next_version, release


class ReleaseVersionTests(unittest.TestCase):
    def test_sequential_rollovers(self):
        for current, expected in (("4.5.8", "4.5.9"), ("4.5.9", "4.6.0"),
                                  ("4.9.9", "5.0.0"), ("0.4.9", "0.5.0")):
            with self.subTest(current=current):
                self.assertEqual(next_version(current), expected)

    def test_invalid_or_non_digit_components_are_rejected(self):
        for value in ("v4.5.9", "4.5", "4.5.9-dev", "4.10.0", "4.0.10"):
            with self.subTest(value=value), self.assertRaises(ReleaseError):
                next_version(value)

    def test_versions_compared_numerically_across_history_and_remote_tags(self):
        self.assertEqual(latest_version("@0.4.9 notes\n@0.3.0 old", "v0.4.8\nv99.0.0-dev",
                                        "abc refs/tags/v0.5.0\nabc refs/tags/v0.5.0^{}\nabc refs/heads/main"), "0.5.0")
        self.assertEqual(latest_version("@9.9.9\n@10.0.0", "", ""), "10.0.0")

    def test_no_version_fails_instead_of_guessing(self):
        with self.assertRaises(ReleaseError):
            latest_version("ordinary commit", "nightly", "abc refs/heads/main")


@unittest.skipUnless(shutil.which("git"), "Git integration tests")
class ReleaseGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "work"
        self.remote = Path(self.temporary.name) / "remote.git"
        self.root.mkdir()
        git(self.root, "init", "--initial-branch=main")
        git(self.root, "config", "user.name", "Release Test")
        git(self.root, "config", "user.email", "release@example.invalid")
        git(self.root, "config", "commit.gpgsign", "false")
        git(self.root, "config", "core.hooksPath", str(self.root / "empty-hooks"))
        (self.root / "tracked.txt").write_text("original", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "@4.5.9 Previous release")
        git(self.root, "init", "--bare", str(self.remote))
        git(self.root, "remote", "add", "origin", str(self.remote))
        git(self.root, "push", "origin", "HEAD:main")
        (self.root / "new.txt").write_text("release work", encoding="utf-8")
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_stages_commits_and_pushes_release_using_existing_workflow_marker(self):
        (self.root / "tracked.txt").unlink()
        with patch("builtins.input", side_effect=AssertionError("Release must not prompt")):
            self.assertEqual(release(self.root, message="Cached runtimes"), "4.6.0")
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "@4.6.0 Cached runtimes")
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual(git(self.root, "ls-remote", "origin", "refs/heads/main").split()[0],
                         git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(git(self.root, "tag", "--list"), "")  # CI owns release tag creation.

    def test_dry_run_does_not_change_head_index_or_remote(self):
        before = git(self.root, "rev-parse", "HEAD")
        status = git(self.root, "status", "--porcelain")
        self.assertEqual(release(self.root, dry_run=True), "4.6.0")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.root, "status", "--porcelain"), status)
        self.assertEqual(git(self.root, "diff", "--cached"), "")
        self.assertEqual(git(self.root, "ls-remote", "origin", "refs/heads/main").split()[0], before)

    def test_non_main_branch_is_rejected(self):
        git(self.root, "switch", "-c", "feature")
        with self.assertRaisesRegex(ReleaseError, "Switch to main"):
            release(self.root)

    def test_remote_tag_reserves_version_even_before_fetch(self):
        git(self.root, "tag", "v4.6.0")
        git(self.root, "push", "origin", "v4.6.0")
        git(self.root, "tag", "--delete", "v4.6.0")
        self.assertEqual(release(self.root, dry_run=True), "4.6.1")

    def test_push_failure_keeps_local_release_for_manual_retry(self):
        real_git = create_release.git

        def failing_push(root, *args):
            if args[0] == "push":
                raise ReleaseError("simulated push rejection")
            return real_git(root, *args)

        with patch("create_release.git", side_effect=failing_push):
            with self.assertRaisesRegex(ReleaseError, "created locally, but push failed"):
                release(self.root)
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "@4.6.0 Release")
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertNotEqual(git(self.root, "ls-remote", "origin", "refs/heads/main").split()[0],
                            git(self.root, "rev-parse", "HEAD"))

    def test_diverged_remote_is_rejected_before_staging(self):
        git(self.root, "checkout", "--orphan", "other-history")
        git(self.root, "commit", "--allow-empty", "-m", "unrelated root")
        git(self.root, "push", "--force", "origin", "HEAD:main")  # Disposable local test remote only.
        git(self.root, "switch", "main")
        with self.assertRaisesRegex(ReleaseError, "reconcile main first"):
            release(self.root)
        self.assertEqual(git(self.root, "diff", "--cached"), "")

    def test_duplicate_message_marker_is_rejected(self):
        with self.assertRaisesRegex(ReleaseError, "another @version"):
            release(self.root, message="@9.9.9 unrelated marker")
