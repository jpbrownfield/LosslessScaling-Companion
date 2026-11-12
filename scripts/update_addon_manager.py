"""Stage and publish tested upstream updates from GitHub Actions.

Preparation only writes a candidate branch. The publisher advances main without
force only after the ordinary Windows build has produced verified artifacts.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from create_release import ReleaseError, git, latest_version, next_version, version_tuple

UPSTREAM = "FriedSnowman/ls-addon-manager"
LOCK = "native/addon_manager/upstream.lock.json"
HOST = "companion/services/addon_host.py"
HOST_VERSION = re.compile(r'^MANAGED_HOST_VERSION = "([^"]+)"$', re.MULTILINE)
SHA = re.compile(r"[0-9a-f]{40}")
ASSETS = ("LosslessCompanion-Setup-x64.exe", "LSAddonManager-Companion.zip")


class UpdateError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token=None):
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def api(self, path, data=None, *, method=None, missing_ok=False):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "LSCompanion-Upstream-Update",
                   "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request("https://api.github.com" + path, headers=headers,
            data=None if data is None else json.dumps(data).encode(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise UpdateError("GitHub response exceeded its size limit")
            return json.loads(payload)
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise UpdateError(f"GitHub {method or ('POST' if data is not None else 'GET')} {path}: HTTP {error.code}") from error


def build_number(tag):
    if not isinstance(tag, str) or not re.fullmatch(r"build-[1-9]\d*", tag):
        raise UpdateError(f"Unsupported upstream release tag: {tag!r}; review its release format manually")
    return int(tag.split("-")[1])


def checksum(url, *, expected=None, size=None, limit=1024 * 1024 * 1024):
    """Stream downloads without executing upstream Setup or forwarding credentials."""
    digest, count = hashlib.sha256(), 0
    request = urllib.request.Request(url, headers={"User-Agent": "LSCompanion-Upstream-Update"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if not response.geturl().startswith("https://"):
            raise UpdateError("Archive download redirected away from HTTPS")
        while block := response.read(1024 * 1024):
            count += len(block)
            if count > limit:
                raise UpdateError("Upstream archive exceeded its size limit")
            digest.update(block)
    if not count or (size is not None and count != size):
        raise UpdateError("Upstream archive is empty or its size does not match GitHub metadata")
    value = digest.hexdigest()
    if expected and value != expected:
        raise UpdateError("Upstream archive does not match GitHub's SHA-256 digest")
    return value


def updated_files(root, release, commit, download=checksum):
    pin = json.loads((root / LOCK).read_text(encoding="utf-8"))
    tag = release["tag_name"]
    build_number(tag)
    if release.get("draft") or release.get("prerelease") or not SHA.fullmatch(commit):
        raise UpdateError("Expected a stable upstream release and an immutable source commit")
    # Build 7 switched to a standalone extractable installer. Prefer that asset
    # when both formats exist, while retaining compatibility with older releases.
    names = [f"LSAddonManager-{tag}-x64{suffix}" for suffix in (".exe", ".zip")]
    name = next((name for name in names if any(asset.get("name") == name
                for asset in release.get("assets", []))), names[0])
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    if len(assets) != 1:
        raise UpdateError(f"Expected exactly one upstream {name} asset")
    asset = assets[0]
    url = f"https://github.com/{UPSTREAM}/releases/download/{tag}/{name}"
    if asset.get("browser_download_url") != url or asset.get("state") != "uploaded":
        raise UpdateError("Upstream archive URL or upload state is unexpected")
    digest = asset.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise UpdateError("Upstream archive has no GitHub SHA-256 digest; review this release manually")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= 1024 * 1024 * 1024:
        raise UpdateError("Upstream archive size is invalid")
    pin.update(tag=tag, commit=commit, companion_revision=1, version=f"{tag}-lsc.1", release_asset=name,
        source_sha256=download(f"https://codeload.github.com/{UPSTREAM}/zip/{commit}", limit=256 * 1024 * 1024),
        release_sha256=download(url, expected=digest.split(":")[1], size=size))
    source = (root / HOST).read_text(encoding="utf-8")
    if len(HOST_VERSION.findall(source)) != 1:
        raise UpdateError("Could not find the single managed host version; review the integration")
    source = HOST_VERSION.sub(f'MANAGED_HOST_VERSION = "{pin["version"]}"', source)
    return {LOCK: json.dumps(pin, indent=2) + "\n", HOST: source}


def marker(tag):
    return f"<!-- companion-upstream:{tag} -->"


def validate_plan(plan):
    if not plan.get("changed"):
        return
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", plan["repository"]):
        raise UpdateError("Invalid destination repository")
    build_number(plan["upstream_tag"])
    version_tuple(plan["version"])
    for key in ("base_sha", "source_sha", "upstream_commit"):
        if not SHA.fullmatch(plan[key]):
            raise UpdateError(f"Invalid {key}")
    if plan["release_tag"] != "v" + plan["version"] or plan["base_branch"] != "main":
        raise UpdateError("Unexpected release tag or destination branch")
    pin = plan["pin"]
    if pin["tag"] != plan["upstream_tag"] or pin["commit"] != plan["upstream_commit"]:
        raise UpdateError("Release metadata does not match its upstream pin")


def validate_candidate(api, repository, sha, pin):
    for path in (LOCK, HOST):
        data = api.api(f"/repos/{repository}/contents/{path}?ref={sha}")
        content = base64.b64decode(data["content"], validate=False).decode("utf-8")
        if path == LOCK:
            candidate = json.loads(content)
            if candidate != pin:
                raise UpdateError("Existing update branch has different pins; refusing to overwrite it")
        elif HOST_VERSION.findall(content) != [pin["version"]]:
            raise UpdateError("Existing update branch has an inconsistent managed host version")


def prepare(root, api, repository):
    pin = json.loads((root / LOCK).read_text(encoding="utf-8"))
    if pin["repository"] != UPSTREAM or git(root, "branch", "--show-current") != "main":
        raise UpdateError("Upstream automation must run on main with the expected upstream repository")
    base = git(root, "rev-parse", "HEAD")
    release = api.api(f"/repos/{UPSTREAM}/releases/latest")
    tag = release["tag_name"]
    number = build_number(tag)
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError("Latest upstream release is not stable")
    if number < build_number(pin["tag"]):
        return {"changed": False, "reason": "Pinned upstream release is newer"}
    own_releases = api.api(f"/repos/{repository}/releases?per_page=100")
    current = latest_version(git(root, "log", "--format=%B"), git(root, "tag", "--list"), "")
    # Recover publication after a successful promotion followed by an API/upload
    # failure. No second version is allocated for the same main commit.
    message = git(root, "log", "-1", "--format=%B")
    recovery = re.fullmatch(r"@(\d+\.\d+\.\d+) Update FriedSnowman addon manager to (build-\d+)\s*", message)
    if number == build_number(pin["tag"]):
        if api.api(f"/repos/{UPSTREAM}/commits/{tag}")["sha"] != pin["commit"]:
            raise UpdateError("The pinned upstream tag moved to another commit; review it manually")
        pending = next((item for item in own_releases if item.get("draft") and
                        item.get("target_commitish") == base and marker(tag) in (item.get("body") or "")), None)
        if recovery and recovery[2] == tag:
            version = recovery[1]
        elif pending:
            version = pending["tag_name"].removeprefix("v")
        else:
            return {"changed": False, "reason": "Upstream release is already pinned"}
        existing = next((item for item in own_releases if item["tag_name"] == "v" + version), None)
        if existing and not existing["draft"]:
            return {"changed": False, "reason": "Upstream update is already published"}
        plan = dict(changed=True, repository=repository, base_branch="main", base_sha=base, source_sha=base,
            upstream_tag=tag, upstream_commit=pin["commit"], version=version, release_tag="v" + version,
            pin=pin, recovery=True)
        validate_plan(plan)
        return plan
    commit = api.api(f"/repos/{UPSTREAM}/commits/{tag}")["sha"]
    files = updated_files(root, release, commit)
    new_pin = json.loads(files[LOCK])
    # A draft created before main was advanced can be resumed as well. Its exact
    # source and pins are checked before using it; unrelated drafts are ignored.
    pending = next((item for item in own_releases if item.get("draft") and
                    marker(tag) in (item.get("body") or "") and f"Base commit: `{base}`" in (item.get("body") or "")), None)
    version = pending["tag_name"].removeprefix("v") if pending else next_version(current)
    version_tuple(version)
    branch = f"automation/friedsnowman-{tag}-v{version}-{base[:12]}"
    ref = api.api(f"/repos/{repository}/git/ref/heads/{branch}", missing_ok=True)
    if ref:
        source_sha = ref["object"]["sha"]
        validate_candidate(api, repository, source_sha, new_pin)
    else:
        if pending:
            raise UpdateError("The pending release's candidate branch is missing; review it manually")
        tree = api.api(f"/repos/{repository}/git/commits/{base}")["tree"]["sha"]
        tree = api.api(f"/repos/{repository}/git/trees", {"base_tree": tree, "tree": [
            {"path": path, "mode": "100644", "type": "blob", "content": content} for path, content in files.items()]})["sha"]
        source_sha = api.api(f"/repos/{repository}/git/commits", {
            "message": f"@{version} Update FriedSnowman addon manager to {tag}", "tree": tree, "parents": [base]})["sha"]
        api.api(f"/repos/{repository}/git/refs", {"ref": "refs/heads/" + branch, "sha": source_sha})
    plan = dict(changed=True, repository=repository, base_branch="main", base_sha=base, source_sha=source_sha,
        upstream_tag=tag, upstream_commit=commit, version=version, release_tag="v" + version,
        pin=new_pin, recovery=False)
    validate_plan(plan)
    return plan


def verify_artifacts(directory):
    paths = []
    directory = directory.resolve(strict=True)
    for name in ASSETS:
        path, sums = directory / name, directory / (name + ".sha256")
        for item in (path, sums):
            if item.is_symlink() or item.resolve(strict=True).parent != directory:
                raise UpdateError("Release artifact escaped its directory")
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?" + re.escape(name), sums.read_text(encoding="utf-8-sig").strip())
        if not match or not path.stat().st_size:
            raise UpdateError(f"Missing or malformed checksum for {name}")
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != match[1].lower():
                raise UpdateError(f"Release checksum mismatch for {name}")
        paths.extend((path, sums))
    return paths


def publish(plan, api, directory, *, run=subprocess.run):
    validate_plan(plan)
    if not plan.get("changed"):
        raise UpdateError("There is no upstream update to publish")
    files = verify_artifacts(directory)
    try:
        with zipfile.ZipFile(directory / ASSETS[1]) as bundle:
            info = bundle.getinfo("lossless-scaling-deployment.json")
            if info.file_size > 1024 * 1024:
                raise UpdateError("Addon deployment manifest exceeded its size limit")
            manifest = json.loads(bundle.read(info))
        if manifest.get("version") != plan["pin"]["version"] or manifest.get("upstream_commit") != plan["upstream_commit"]:
            raise UpdateError("The built addon host does not match the validated upstream pins")
    except (zipfile.BadZipFile, KeyError, ValueError) as error:
        raise UpdateError("The built addon host has no valid deployment manifest") from error
    repo, tag, sha = plan["repository"], plan["release_tag"], plan["source_sha"]
    existing = api.api(f"/repos/{repo}/releases/tags/{tag}", missing_ok=True)
    if not existing:
        existing = next((item for item in api.api(f"/repos/{repo}/releases?per_page=100")
                         if item["tag_name"] == tag), None)
    if existing:
        # GitHub can defer creation of a draft release's tag until publication.
        # In that case require its exact immutable target, never a branch name.
        tagged = api.api(f"/repos/{repo}/commits/{tag}", missing_ok=True)
        actual_sha = tagged["sha"] if tagged else (existing.get("target_commitish") if existing["draft"] else None)
        if actual_sha != sha or marker(plan["upstream_tag"]) not in (existing.get("body") or ""):
            raise UpdateError("Release tag is already owned by a different source commit")
        if not existing["draft"]:
            return

    def require_unchanged_main():
        current = api.api(f"/repos/{repo}/git/ref/heads/main")["object"]["sha"]
        if current not in (plan["base_sha"], sha):
            raise UpdateError("Main changed during validation; rerun the updater to test the newer source")
        return current

    require_unchanged_main()
    latest = api.api(f"/repos/{repo}/releases/latest", missing_ok=True)
    if latest and re.fullmatch(r"v\d+\.\d+\.\d+", latest["tag_name"]):
        if version_tuple(latest["tag_name"][1:]) >= version_tuple(plan["version"]):
            raise UpdateError("A newer or equal Companion release was published during validation")
    notes = directory / "upstream-release-notes.md"
    notes.write_text(f"{marker(plan['upstream_tag'])}\n\n"
        f"Updates the Companion-built addon host to [FriedSnowman {plan['upstream_tag']}]"
        f"(https://github.com/{UPSTREAM}/releases/tag/{plan['upstream_tag']}).\n\n"
        f"Upstream commit: `{plan['upstream_commit']}`\n\nBase commit: `{plan['base_sha']}`\n\n"
        "Includes Companion's integration patches, unchanged upstream addon binaries and matching source/licences.\n\n"
        "The Windows application build, application tests, native host smoke tests and NR routing tests passed. "
        "Live GPU image quality and performance were not benchmarked by this workflow.\n",
        encoding="utf-8")

    def gh(*args):
        run(["gh", "release", *args, "--repo", repo], check=True)

    if not existing:
        gh("create", tag, "--target", sha, "--draft", "--title", tag, "--notes-file", str(notes))
    gh("upload", tag, *(str(path) for path in files), "--clobber")
    if require_unchanged_main() != sha:
        api.api(f"/repos/{repo}/git/refs/heads/main", {"sha": sha, "force": False}, method="PATCH")
    # The release remains a draft until every artifact is uploaded and main's
    # tested source pins are committed. Protected-branch rejection stops here.
    gh("edit", tag, "--draft=false", "--latest")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "publish"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--artifacts", type=Path, default=Path("release-artifacts"))
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
            raise UpdateError("Set GITHUB_REPOSITORY to the destination owner/repository")
        if args.command == "prepare":
            plan = prepare(args.root, GitHub(), args.repository)
            print(json.dumps(plan, indent=2))
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                with open(output, "a", encoding="utf-8") as stream:
                    stream.write(f"changed={str(plan['changed']).lower()}\n")
                    stream.write("metadata=" + json.dumps(plan, separators=(",", ":")) + "\n")
                    for key in ("source_sha", "version"):
                        stream.write(f"{key}={plan.get(key, '')}\n")
        else:
            plan = json.loads(os.environ["UPDATE_METADATA"])
            if plan["repository"] != args.repository:
                raise UpdateError("Update metadata targets another repository")
            publish(plan, GitHub(), args.artifacts)
    except (UpdateError, ReleaseError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Upstream update stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
