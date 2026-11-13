#!/usr/bin/env python3
"""Upload a draft release, verify downloaded assets, publish and pin the bundle."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    if release.run("git", "rev-parse", "HEAD") != args.revision or release.run("git", "status", "--porcelain"):
        parser.error("Checkout must be clean and match the CI build revision")
    metadata = release.verify_release(args.artifacts)
    version = metadata["version"]
    if metadata["source_revision"] != args.revision:
        parser.error("Release was built from a different source revision")
    if release.run("git", "ls-remote", "--tags", "origin", "refs/tags/" + version):
        parser.error("Tag already exists; choose a new version")
    probe = subprocess.run(["gh", "release", "view", version, "--repo", release.REPOSITORY],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if probe.returncode == 0:
        parser.error("Release already exists; choose a new version")
    if "release not found" not in probe.stderr.lower():
        parser.error("Cannot determine release state: " + probe.stderr)
    assets = sorted(args.artifacts.iterdir())
    release.run("gh", "release", "create", version, "--repo", release.REPOSITORY,
                "--draft", "--prerelease", "--target", args.revision,
                "--title", "Git for Scarlet " + version,
                "--notes-file", str(args.artifacts / "README.md"), *map(str, assets))
    uploaded = json.loads(release.run("gh", "release", "view", version, "--repo", release.REPOSITORY,
                                      "--json", "assets"))["assets"]
    if {item["name"]: item["size"] for item in uploaded} != {p.name: p.stat().st_size for p in assets}:
        raise ValueError("Incomplete upload; draft retained")
    with tempfile.TemporaryDirectory(prefix="git-scarlet-publish-") as temporary:
        downloaded = Path(temporary)
        release.run("gh", "release", "download", version, "--repo", release.REPOSITORY, "--dir", str(downloaded))
        if any(release.sha256(downloaded / p.name) != release.sha256(p) for p in assets):
            raise ValueError("Downloaded checksum mismatch; draft retained")
        release.verify_release(downloaded)
        release.run("gh", "release", "edit", version, "--repo", release.REPOSITORY, "--draft=false")
        shutil.copyfile(downloaded / "bundle.toml", release.ROOT / "bundle.toml")
    release.run("git", "add", "--", "bundle.toml")
    release.run("git", "commit", "-m", "Pin native Git release " + version, "--", "bundle.toml")
    # Move only the generated manifest onto newer source commits, using normal
    # non-forced pushes. The release and manifest retain the actual build SHA.
    for _ in range(3):
        release.run("git", "fetch", "origin", "scarlet")
        subprocess.run(["git", "merge-base", "--is-ancestor", args.revision, "origin/scarlet"],
                       cwd=release.ROOT, check=True)
        release.run("git", "rebase", "-X", "theirs", "origin/scarlet")
        if subprocess.run(["git", "push", "origin", "HEAD:scarlet"], cwd=release.ROOT).returncode == 0:
            print(release.URL + "/releases/tag/" + version)
            return
    raise RuntimeError("Release published, but bundle pin could not be pushed after three attempts")


if __name__ == "__main__":
    main()
