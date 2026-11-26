"""Resolve one release identity for build metadata, signing and publication."""
from __future__ import annotations

import os
import re


def resolve(*, requested="", requested_channel="beta", ref_type="", ref_name="", message=""):
    version, channel, from_commit = "0.0.0-dev", "development", False
    marker = re.match(r"^@(\d+\.\d+\.\d+)(?:\s+\[(beta|full)\])?(?:\s|$)", message)
    if requested:
        version, channel = requested, requested_channel
    elif ref_type == "tag" and re.fullmatch(r"v\d+\.\d+\.\d+", ref_name):
        version = ref_name[1:]
        channel = "full" if int(version.split(".")[2]) == 0 else "beta"
    elif ref_type == "branch" and ref_name == "main" and marker:
        version = marker[1]
        channel = marker[2] or ("full" if int(version.split(".")[2]) == 0 else "beta")
        from_commit = True
    if channel != "development":
        if not re.fullmatch(r"\d+\.\d+\.\d+", version) or channel not in {"beta", "full"}:
            raise ValueError("Invalid release version/channel")
        if (int(version.split(".")[2]) == 0) != (channel == "full"):
            raise ValueError("Full releases require patch zero; betas require a positive patch")
    return {"version": version, "channel": channel, "tag": "v"+version if channel != "development" else "",
            "from_commit": str(from_commit).lower()}


def main():
    result = resolve(requested=os.environ.get("REQUESTED_VERSION", ""),
                     requested_channel=os.environ.get("REQUESTED_CHANNEL", "beta"),
                     ref_type=os.environ.get("REF_TYPE", ""), ref_name=os.environ.get("REF_NAME", ""),
                     message=os.environ.get("COMMIT_MESSAGE", ""))
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as output:
        output.write(f"LS_COMPANION_VERSION={result['version']}\nLS_COMPANION_RELEASE_CHANNEL={result['channel']}\n")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")
    print(f"Build: {result['version']} ({result['channel']})")


if __name__ == "__main__":
    main()
