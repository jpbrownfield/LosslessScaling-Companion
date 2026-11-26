import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from scripts.resolve_release import resolve
from scripts.publish_release import ASSETS, publish


class ReleaseChannelTests(unittest.TestCase):
    def test_branch_and_tag_channels_are_consistent(self):
        for version, channel in (("0.9.10", "beta"), ("0.10.0", "full"), ("4.100.0", "full")):
            with self.subTest(version=version):
                branch = resolve(ref_type="branch", ref_name="main", message=f"@{version} [{channel}] Release")
                tag = resolve(ref_type="tag", ref_name="v"+version)
                self.assertEqual(branch["channel"], channel)
                self.assertEqual(tag["channel"], channel)
                self.assertEqual(branch["tag"], tag["tag"])
                self.assertEqual(branch["from_commit"], "true")

    def test_workflow_call_cannot_mislabel_beta_as_full(self):
        self.assertEqual(resolve(requested="0.9.10")["channel"], "beta")
        for kwargs in ({"requested": "0.10.0"}, {"requested": "0.9.10", "requested_channel": "full"},
                       {"requested": "0.10.0; echo bad", "requested_channel": "full"},
                       {"ref_type": "branch", "ref_name": "main", "message": "@0.10.0 [beta] mismatch"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                resolve(**kwargs)

    def test_ordinary_and_feature_commits_do_not_publish(self):
        self.assertEqual(resolve(ref_type="branch",ref_name="feature",message="@1.2.0 [full] test")["tag"], "")
        self.assertEqual(resolve(ref_type="branch",ref_name="main",message="Document @1.2.0 examples")["tag"], "")

    def artifacts(self, directory, *, signed=False):
        for name in ASSETS:
            blob = (name+" fixture").encode()
            (directory/name).write_bytes(blob)
            (directory/(name+".sha256")).write_text(hashlib.sha256(blob).hexdigest()+"  "+name)
        if signed:
            certificate = b"public test certificate"
            (directory/"LSCompanion-CodeSigning.cer").write_bytes(certificate)
            (directory/"release-signing.json").write_text(json.dumps({"channel": "full",
                "installerSha256": hashlib.sha256((directory/ASSETS[0]).read_bytes()).hexdigest(),
                "certificateSha256": hashlib.sha256(certificate).hexdigest()}))

    def test_beta_never_sets_latest_and_publishes_only_after_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary);self.artifacts(directory)
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=1 if "view" in args else 0, stdout="")
            publish(directory,tag="v0.9.10",channel="beta",source="a"*40,run=run)
            self.assertIn("--prerelease", calls[-1])
            self.assertIn("--latest=false", calls[-1])
            self.assertIn("--draft", calls[1])
            self.assertEqual(calls[-2][2], "upload")
            self.assertIn("--draft=false", calls[-1])

    def test_full_requires_signing_evidence_and_does_not_replace_newer_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary);self.artifacts(directory)
            with self.assertRaises(FileNotFoundError):
                publish(directory,tag="v0.10.0",channel="full",source="a"*40)
            self.artifacts(directory,signed=True)
            for latest, expected_flag in (("v0.9.5", "--latest"), ("v0.11.0", "--latest=false")):
                calls = []
                def run(args, **kwargs):
                    calls.append(args)
                    return SimpleNamespace(returncode=1 if args[1:4] == ["release","view","v0.10.0"] else 0,
                                           stdout=json.dumps({"tagName":latest}))
                publish(directory,tag="v0.10.0",channel="full",source="a"*40,run=run)
                self.assertIn("--prerelease=false", calls[-1])
                self.assertIn(expected_flag, calls[-1])
            (directory/ASSETS[0]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError,"checksum mismatch"):
                publish(directory,tag="v0.10.0",channel="full",source="a"*40)


if __name__ == "__main__":
    unittest.main()
