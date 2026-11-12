import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch

from scripts import update_addon_manager as updater
from scripts import build_addon_manager as builder


BASE, SOURCE, COMMIT = "a" * 40, "b" * 40, "c" * 40
REPO = "example/Companion"


def pin():
    return dict(repository=updater.UPSTREAM, tag="build-5", commit="d" * 40,
                source_sha256="e" * 64, release_sha256="f" * 64,
                companion_revision=7, version="build-5-lsc.7", bridge_protocol=1,
                futureSetting="preserve")


def release(tag="build-6", suffix=".zip"):
    name = f"LSAddonManager-{tag}-x64{suffix}"
    return dict(tag_name=tag, draft=False, prerelease=False, assets=[dict(name=name,
        browser_download_url=f"https://github.com/{updater.UPSTREAM}/releases/download/{tag}/{name}",
        state="uploaded", digest="sha256:" + "1" * 64, size=42)])


class UpstreamUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for path in (updater.LOCK, updater.HOST):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / updater.LOCK).write_text(json.dumps(pin()))
        (self.root / updater.HOST).write_text('MANAGED_HOST_VERSION = "build-5-lsc.7"\nOTHER = "keep"\n')
        self.git = patch.object(updater, "git", side_effect=self.git_result).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def git_result(root, *args):
        return {("branch", "--show-current"): "main", ("rev-parse", "HEAD"): BASE,
                ("log", "--format=%B"): "@4.5.9 Last release", ("tag", "--list"): "v4.5.9",
                ("log", "-1", "--format=%B"): "Ordinary work"}[args]

    def api(self, *, upstream=None, own=None, existing_branch=False):
        def request(path, data=None, **kwargs):
            if path == f"/repos/{updater.UPSTREAM}/releases/latest":
                return upstream or release()
            if path == f"/repos/{REPO}/releases?per_page=100":
                return own or []
            if path == f"/repos/{updater.UPSTREAM}/commits/build-6":
                return {"sha": COMMIT}
            if path == f"/repos/{updater.UPSTREAM}/commits/build-5":
                return {"sha": pin()["commit"]}
            if "/git/ref/heads/automation/" in path:
                return {"object": {"sha": SOURCE}} if existing_branch else None
            if path == f"/repos/{REPO}/git/commits/{BASE}":
                return {"tree": {"sha": "e" * 40}}
            if path.endswith("/git/trees"):
                return {"sha": "f" * 40}
            if path.endswith("/git/commits"):
                return {"sha": SOURCE}
            if path.endswith("/git/refs"):
                return {}
            raise AssertionError(f"Unexpected API request: {path}")
        return Mock(api=Mock(side_effect=request))

    def test_refreshes_only_pins_and_host_version_preserving_protocol_and_other_code(self):
        download = Mock(side_effect=["2" * 64, "1" * 64])
        files = updater.updated_files(self.root, release(), COMMIT, download)
        self.assertEqual(set(files), {updater.LOCK, updater.HOST})
        result = json.loads(files[updater.LOCK])
        self.assertEqual(result["tag"], "build-6")
        self.assertEqual(result["version"], "build-6-lsc.1")
        self.assertEqual(result["companion_revision"], 1)
        self.assertEqual(result["bridge_protocol"], 1)
        self.assertEqual(result["futureSetting"], "preserve")
        self.assertEqual(result["source_sha256"], "2" * 64)
        self.assertEqual(result["release_asset"], "LSAddonManager-build-6-x64.zip")
        self.assertIn('OTHER = "keep"', files[updater.HOST])
        self.assertIn('"build-6-lsc.1"', files[updater.HOST])
        self.assertEqual(json.loads((self.root / updater.LOCK).read_text())["tag"], "build-5")
        self.assertEqual(download.call_args.kwargs, {"expected": "1" * 64, "size": 42})

    def test_build_7_installer_is_pinned_and_preferred_over_legacy_zip(self):
        sample = release("build-7", ".exe")
        sample["assets"].extend(release("build-7")["assets"])
        download = Mock(side_effect=["2" * 64, "1" * 64])
        result = json.loads(updater.updated_files(self.root, sample, COMMIT, download)[updater.LOCK])
        self.assertEqual(result["release_asset"], "LSAddonManager-build-7-x64.exe")
        self.assertEqual(builder.release_asset_name(result), "LSAddonManager-build-7-x64.exe")
        self.assertTrue(download.call_args.args[0].endswith("LSAddonManager-build-7-x64.exe"))

    def test_builder_extracts_current_installer_and_keeps_legacy_zip_support(self):
        self.assertEqual(builder.release_asset_name(pin()), "LSAddonManager-build-5-x64.zip")
        with self.assertRaises(RuntimeError):
            builder.release_asset_name(dict(pin(), release_asset="../../setup.exe"))
        for suffix in (".zip", ".exe"):
            archive, work = self.root / ("release" + suffix), self.root / suffix[1:]
            work.mkdir()
            if suffix == ".zip":
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr("LSAddonManagerSetup.exe", b"fixture")
                    bundle.writestr("NOTICE.md", b"notice")
            else:
                archive.write_bytes(b"fixture")
            with patch.object(builder.subprocess, "run") as run:
                payload, notices = builder.extract_release(archive, work)
            command = run.call_args.args[0]
            expected = work / "setup/LSAddonManagerSetup.exe" if suffix == ".zip" else archive
            self.assertEqual(command[0], str(expected.resolve()))
            self.assertEqual(command[1:4], ["--extract", str(payload.resolve()), "--no-remember"])
            self.assertEqual(notices, work / "setup" if suffix == ".zip" else payload)
            self.assertEqual(run.call_args.kwargs["timeout"], 180)

    def test_rejects_prerelease_missing_digest_asset_substitution_and_ambiguous_payloads(self):
        invalid = [dict(release(), prerelease=True), dict(release(), draft=True),
                   dict(release(), tag_name="v6.0.0")]
        for key, value in (("digest", None), ("digest", "sha512:bad"), ("size", 0),
                           ("browser_download_url", "https://example.invalid/anything.zip"), ("state", "open")):
            sample = release()
            sample["assets"][0][key] = value
            invalid.append(sample)
        duplicate = release()
        duplicate["assets"] *= 2
        invalid.append(duplicate)
        for sample in invalid:
            download = Mock()
            with self.subTest(sample=sample), self.assertRaises(updater.UpdateError):
                updater.updated_files(self.root, sample, COMMIT, download)
            download.assert_not_called()

    def test_download_checks_size_hash_limits_and_does_not_send_credentials(self):
        blob = b"trusted release payload"
        def response():
            stream = io.BytesIO(blob)
            stream.geturl = lambda: "https://release-assets.githubusercontent.com/asset"
            return stream
        for kwargs in ({"expected": "0" * 64}, {"size": 2}, {"limit": 2}):
            with patch.object(updater.urllib.request, "urlopen", return_value=response()), self.assertRaises(updater.UpdateError):
                updater.checksum("https://github.com/upstream.zip", **kwargs)
        with patch.object(updater.urllib.request, "urlopen", return_value=response()) as opened:
            result = updater.checksum("https://github.com/upstream.zip", size=len(blob))
        self.assertEqual(result, hashlib.sha256(blob).hexdigest())
        self.assertNotIn("Authorization", dict(opened.call_args.args[0].header_items()))

    def test_current_release_and_downgrade_are_noops_without_writes(self):
        for tag in ("build-4", "build-5"):
            api = self.api(upstream=release(tag))
            self.assertFalse(updater.prepare(self.root, api, REPO)["changed"])
            self.assertFalse(any(call.args[1:] for call in api.api.call_args_list))

    def test_stages_immutable_candidate_with_next_version_without_touching_main(self):
        api = self.api()
        with patch.object(updater, "checksum", return_value="1" * 64):
            # Default arguments bind at definition time; replace file preparation's
            # downloader explicitly rather than making network calls in tests.
            real = updater.updated_files
            with patch.object(updater, "updated_files", side_effect=lambda root, rel, commit:
                              real(root, rel, commit, Mock(return_value="1" * 64))):
                plan = updater.prepare(self.root, api, REPO)
        self.assertEqual(plan["version"], "4.6.0")
        self.assertEqual(plan["source_sha"], SOURCE)
        commits = [call.args[1] for call in api.api.call_args_list if call.args[0].endswith("/git/commits")]
        self.assertEqual(commits[0]["parents"], [BASE])
        self.assertEqual(commits[0]["message"], "@4.6.0 Update FriedSnowman addon manager to build-6")
        writes = [call for call in api.api.call_args_list if len(call.args) > 1]
        self.assertTrue(writes)
        self.assertFalse(any("heads/main" in call.args[0] for call in writes))

    def test_existing_candidate_is_validated_and_preserved(self):
        api = self.api(existing_branch=True)
        real = updater.updated_files
        with patch.object(updater, "updated_files", side_effect=lambda root, rel, commit:
                          real(root, rel, commit, Mock(return_value="1" * 64))), \
             patch.object(updater, "validate_candidate") as validate:
            plan = updater.prepare(self.root, api, REPO)
        validate.assert_called_once()
        self.assertEqual(plan["source_sha"], SOURCE)
        self.assertFalse(any(len(call.args) > 1 for call in api.api.call_args_list))

    def test_retry_before_promotion_reuses_draft_version_instead_of_allocating_again(self):
        pending = dict(draft=True, tag_name="v4.6.0", body=updater.marker("build-6") + f"\nBase commit: `{BASE}`")
        api = self.api(existing_branch=True, own=[pending])
        real = updater.updated_files
        with patch.object(updater, "updated_files", side_effect=lambda root, rel, commit:
                          real(root, rel, commit, Mock(return_value="1" * 64))), \
             patch.object(updater, "validate_candidate"):
            plan = updater.prepare(self.root, api, REPO)
        self.assertEqual(plan["version"], "4.6.0")
        self.assertEqual(plan["source_sha"], SOURCE)

    def test_moved_upstream_tag_fails_instead_of_silently_changing_pins(self):
        api = self.api(upstream=release("build-5"))
        original = api.api.side_effect
        api.api.side_effect = lambda path, *args, **kwargs: (
            {"sha": COMMIT} if path.endswith("/commits/build-5") else original(path, *args, **kwargs))
        with self.assertRaisesRegex(updater.UpdateError, "tag moved"):
            updater.prepare(self.root, api, REPO)

    def test_candidate_pin_mismatch_is_not_overwritten(self):
        data = base64.b64encode(json.dumps(pin()).encode()).decode()
        api = Mock(api=Mock(return_value={"content": data}))
        with self.assertRaisesRegex(updater.UpdateError, "different pins"):
            updater.validate_candidate(api, REPO, SOURCE, {**pin(), "tag": "build-6"})

    def test_retry_after_promotion_reuses_version_and_commit(self):
        self.git.side_effect = lambda root, *args: (
            "@4.6.0 Update FriedSnowman addon manager to build-5" if args == ("log", "-1", "--format=%B")
            else self.git_result(root, *args))
        api = self.api(upstream=release("build-5"))
        plan = updater.prepare(self.root, api, REPO)
        self.assertTrue(plan["recovery"])
        self.assertEqual(plan["source_sha"], BASE)
        self.assertEqual(plan["version"], "4.6.0")
        self.assertFalse(any(len(call.args) > 1 for call in api.api.call_args_list))

    def test_retry_after_manual_candidate_fix_finds_draft_by_its_exact_commit(self):
        pending = dict(draft=True, tag_name="v4.6.0", body=updater.marker("build-5"), target_commitish=BASE)
        plan = updater.prepare(self.root, self.api(upstream=release("build-5"), own=[pending]), REPO)
        self.assertTrue(plan["recovery"])
        self.assertEqual(plan["version"], "4.6.0")

    def test_published_update_is_not_rebuilt_or_announced_again(self):
        self.git.side_effect = lambda root, *args: (
            "@4.6.0 Update FriedSnowman addon manager to build-5" if args == ("log", "-1", "--format=%B")
            else self.git_result(root, *args))
        own = [dict(tag_name="v4.6.0", draft=False)]
        result = updater.prepare(self.root, self.api(upstream=release("build-5"), own=own), REPO)
        self.assertFalse(result["changed"])


class UpstreamPublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = dict(changed=True, repository=REPO, base_branch="main", base_sha=BASE,
                         source_sha=SOURCE, upstream_tag="build-6", upstream_commit=COMMIT,
                         version="4.6.0", release_tag="v4.6.0",
                         pin={**pin(), "tag": "build-6", "commit": COMMIT, "version": "build-6-lsc.1"})
        for name in updater.ASSETS:
            data = ("verified " + name).encode()
            (self.root / name).write_bytes(data)
            if name.endswith(".zip"):
                with zipfile.ZipFile(self.root / name, "w") as bundle:
                    bundle.writestr("lossless-scaling-deployment.json", json.dumps({
                        "version": "build-6-lsc.1", "upstream_commit": COMMIT}))
                data = (self.root / name).read_bytes()
            (self.root / (name + ".sha256")).write_text(hashlib.sha256(data).hexdigest() + "  " + name)
        self.events = []

    def api(self, *, main=BASE, existing=None, latest=None, fail_promotion=False, tag_exists=True, listed=None):
        def request(path, data=None, **kwargs):
            if path.endswith("/releases/tags/v4.6.0"):
                return existing
            if path.endswith("/releases?per_page=100"):
                return listed or []
            if path.endswith("/commits/v4.6.0"):
                return {"sha": SOURCE} if tag_exists else None
            if path.endswith("/git/ref/heads/main"):
                return {"object": {"sha": main}}
            if path.endswith("/releases/latest"):
                return latest
            if path.endswith("/git/refs/heads/main"):
                self.events.append(("promote", data))
                if fail_promotion:
                    raise updater.UpdateError("Protected branch")
                return {}
            raise AssertionError(path)
        return Mock(api=Mock(side_effect=request))

    def run_gh(self, command, **kwargs):
        self.events.append((command[2], command))

    def test_uploads_draft_then_fast_forwards_then_announces_release(self):
        updater.publish(self.plan, self.api(), self.root, run=self.run_gh)
        self.assertEqual([event[0] for event in self.events], ["create", "upload", "promote", "edit"])
        self.assertIn("--draft", self.events[0][1])
        self.assertEqual(self.events[2][1], {"sha": SOURCE, "force": False})
        self.assertIn("--draft=false", self.events[3][1])
        self.assertIn("--latest", self.events[3][1])
        notes = (self.root / "upstream-release-notes.md").read_text()
        self.assertIn(updater.marker("build-6"), notes)
        self.assertIn(COMMIT, notes)

    def test_corrupt_artifact_or_checksum_never_changes_repository_or_releases(self):
        for corrupt_checksum in (False, True):
            with self.subTest(checksum=corrupt_checksum):
                target = self.root / updater.ASSETS[0]
                if corrupt_checksum:
                    target = self.root / (updater.ASSETS[0] + ".sha256")
                target.write_bytes(b"corrupt")
                api = self.api()
                with self.assertRaises(updater.UpdateError):
                    updater.publish(self.plan, api, self.root, run=self.run_gh)
                api.api.assert_not_called()
                self.assertEqual(self.events, [])

    def test_changed_main_or_newer_release_is_not_overwritten(self):
        for api in (self.api(main="f" * 40), self.api(latest={"tag_name": "v4.6.1"})):
            with self.assertRaises(updater.UpdateError):
                updater.publish(self.plan, api, self.root, run=self.run_gh)
            self.assertEqual(self.events, [])

    def test_valid_checksums_for_wrong_host_version_do_not_publish(self):
        self.plan["pin"]["version"] = "build-6-lsc.2"
        api = self.api()
        with self.assertRaisesRegex(updater.UpdateError, "does not match"):
            updater.publish(self.plan, api, self.root, run=self.run_gh)
        api.api.assert_not_called()

    def test_protected_main_leaves_release_in_draft(self):
        with self.assertRaisesRegex(updater.UpdateError, "Protected branch"):
            updater.publish(self.plan, self.api(fail_promotion=True), self.root, run=self.run_gh)
        self.assertEqual([event[0] for event in self.events], ["create", "upload", "promote"])

    def test_upload_failure_does_not_promote_or_publish(self):
        def fail(command, **kwargs):
            self.run_gh(command)
            if command[2] == "upload":
                raise OSError("Network unavailable")
        with self.assertRaises(OSError):
            updater.publish(self.plan, self.api(), self.root, run=fail)
        self.assertEqual([event[0] for event in self.events], ["create", "upload"])

    def test_resumes_owned_draft_and_does_not_duplicate_published_release(self):
        existing = dict(draft=True, body=updater.marker("build-6"))
        updater.publish(self.plan, self.api(existing=existing, main=SOURCE), self.root, run=self.run_gh)
        self.assertEqual([event[0] for event in self.events], ["upload", "edit"])
        self.events.clear()
        existing["draft"] = False
        updater.publish(self.plan, self.api(existing=existing), self.root, run=self.run_gh)
        self.assertEqual(self.events, [])

    def test_unrelated_release_tag_is_never_clobbered(self):
        api = self.api(existing={"draft": True, "body": "A manually created release"})
        with self.assertRaisesRegex(updater.UpdateError, "different source"):
            updater.publish(self.plan, api, self.root, run=self.run_gh)
        self.assertEqual(self.events, [])

    def test_recovers_owned_draft_before_github_creates_its_tag(self):
        existing = dict(tag_name="v4.6.0", draft=True, target_commitish=SOURCE,
                        body=updater.marker("build-6"))
        updater.publish(self.plan, self.api(tag_exists=False, listed=[existing]), self.root, run=self.run_gh)
        self.assertEqual([event[0] for event in self.events], ["upload", "promote", "edit"])
        self.events.clear()
        existing["target_commitish"] = "main"
        with self.assertRaisesRegex(updater.UpdateError, "different source"):
            updater.publish(self.plan, self.api(tag_exists=False, listed=[existing]), self.root, run=self.run_gh)
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
