"""Stage ALL changes, commit and push a sequential release without confirmation."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


VERSION = re.compile(r"\d+\.\d+\.\d+")
MARKER = re.compile(r"@(\d+\.\d+\.\d+)(?![\d.])")



class ReleaseError(RuntimeError):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:
    if not VERSION.fullmatch(value):
        raise ReleaseError(f"Invalid version: {value}")
    return tuple(map(int, value.split(".")))


def next_version(value: str) -> str:
    major, minor, patch = version_tuple(value)
    if minor > 9 or patch > 9:
        raise ReleaseError("Sequential numbering requires minor and patch digits from 0 to 9.")
    number = major * 100 + minor * 10 + patch + 1
    return f"{number // 100}.{number // 10 % 10}.{number % 10}"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode:
        raise ReleaseError(f"git {' '.join(args)} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def latest_version(history: str, local_tags: str, remote_refs: str) -> str:
    versions = set(MARKER.findall(history))
    for tag in local_tags.splitlines():
        if re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
            versions.add(tag.removeprefix("v"))
    for line in remote_refs.splitlines():
        fields = line.split()
        if len(fields) == 2:
            match = re.fullmatch(r"refs/tags/v?(\d+\.\d+\.\d+)(?:\^\{\})?", fields[1])
            if match:
                versions.add(match[1])
    if not versions:
        raise ReleaseError("No release version found in commit messages or tags.")
    return max(versions, key=version_tuple)


def release(root: Path, *, remote="origin", message="Release", dry_run=False) -> str:
    root = Path(git(root, "rev-parse", "--show-toplevel"))
    if git(root, "branch", "--show-current") != "main":
        raise ReleaseError("Switch to main first; the release workflow only publishes branch releases from main.")
    if not remote or remote.startswith("-"):
        raise ReleaseError("Invalid remote name.")
    if MARKER.search(message) or not message.strip():
        raise ReleaseError("The message must be nonempty and must not contain another @version marker.")
    if git(root, "ls-files", "--unmerged"):
        raise ReleaseError("Resolve merge conflicts before releasing.")
    for state in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        path = Path(git(root, "rev-parse", "--git-path", state))
        if (path if path.is_absolute() else root / path).exists():
            raise ReleaseError("Finish the current merge/rebase/cherry-pick before releasing.")
    head = git(root, "rev-parse", "HEAD")
    fetch_urls = git(root, "remote", "get-url", "--all", remote).splitlines()
    push_urls = git(root, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(push_urls) != 1 or push_urls != fetch_urls:
        raise ReleaseError("The remote must have one matching fetch/push URL so version checks cover the push destination.")
    refs = git(root, "ls-remote", "--heads", "--tags", remote)
    remote_main = next((line.split()[0] for line in refs.splitlines()
                        if line.split()[1] == "refs/heads/main"), None)
    if not remote_main:
        raise ReleaseError(f"Remote {remote} has no main branch; refusing to publish to an unexpected destination.")
    try:
        git(root, "merge-base", "--is-ancestor", remote_main, head)
    except ReleaseError as error:
        raise ReleaseError("Remote main is ahead, diverged, or unknown locally. Fetch/pull and reconcile main first.") from error
    current = latest_version(git(root, "log", "--format=%B"), git(root, "tag", "--list"), refs)
    upcoming = next_version(current)
    commit_message = f"@{upcoming} {message.strip()}"
    status = git(root, "status", "--short", "--untracked-files=all")
    if not status:
        raise ReleaseError("No changes to release. Nothing was staged, committed or pushed.")
    print(f"Release {current} -> {upcoming}")
    print(f"Destination: {remote}/main ({push_urls[0]})")
    print(f"Commit: {commit_message}\n\nAll non-ignored changes will be staged, including deletions:\n{status}")
    if dry_run:
        print("Dry run: nothing staged, committed or pushed.")
        return upcoming
    if git(root, "rev-parse", "HEAD") != head:
        raise ReleaseError("HEAD changed during preflight. Run the script again.")
    if git(root, "status", "--short", "--untracked-files=all") != status:
        raise ReleaseError("The file list changed during preflight. Review it and run the script again.")
    git(root, "add", "--all", "--", ".")
    git(root, "commit", "-m", commit_message)
    commit = git(root, "rev-parse", "HEAD")
    try:
        git(root, "push", remote, "HEAD:refs/heads/main")
    except ReleaseError as error:
        raise ReleaseError(
            f"Release commit {commit} was created locally, but push failed. No reset or force-push was attempted.\n"
            f"After resolving the push problem, push this existing commit with: git push {remote} HEAD:main\n"
            "Do not rerun this script to retry the same release.\n" + str(error)
        ) from error
    print(f"Pushed {commit_message}. GitHub Actions will build and publish v{upcoming} if its checks pass.")
    return upcoming


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", default="Release", help="Commit description after the automatic @version marker")
    parser.add_argument("--remote", default="origin", help="Git remote (default: origin)")
    parser.add_argument("--dry-run", action="store_true", help="Read-only preview; still checks remote refs")
    args = parser.parse_args(argv)
    try:
        release(Path(__file__).resolve().parent, **vars(args))
    except (ReleaseError, OSError, EOFError, KeyboardInterrupt) as error:
        print(f"Release stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
