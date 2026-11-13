#!/usr/bin/env python3
"""Build and verify the native Scarlet release and checksum-pinned bundle."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from release_archive import verify_archive, write_archive

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "petitstrawberry/git"
URL = "https://github.com/" + REPOSITORY


def run(*args, **kwargs):
    return subprocess.check_output(list(args), cwd=ROOT, text=True, **kwargs).strip()


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid_version(version):
    if not re.fullmatch(r"scarlet-[A-Za-z0-9][A-Za-z0-9._-]{0,110}", version):
        raise ValueError("Use a fresh scarlet-* release tag containing letters, digits, dots, underscores or hyphens")
    return version


def choose_version(requested=""):
    upstream = re.search(r"^DEF_VER=(\S+)$", (ROOT / "GIT-VERSION-GEN").read_text(), re.M)[1]
    return valid_version(requested or f"scarlet-{upstream}-{os.environ['GITHUB_RUN_NUMBER']}.{os.environ['GITHUB_RUN_ATTEMPT']}")


def bundle_text(metadata):
    version = metadata["version"]
    return (
        "# Generated from a downloaded and verified CI release.\n"
        f"# Source revision: {metadata['source_revision']}\n"
        f"# Required Scarlet kernel: {metadata['required_kernel_revision']}\n"
        "# Native AArch64 only; no archive checksum is provided for other architectures.\n"
        "[[layers]]\n"
        'kind = "archive"\n'
        f'url = "{URL}/releases/download/{version}/git-scarlet-{{arch}}-{version}.tar.gz"\n'
        f'sha256 = {{ aarch64 = "sha256:{metadata["archive_sha256"]}" }}\n'
        'format = "tar-gz"\n'
        'to = "/"\n'
    )


def release_notes(metadata):
    return (
        f"# Native Scarlet Git {metadata['version']}\n\n"
        f"Git {metadata['git_version']}, built by GitHub Actions from "
        f"[{metadata['source_revision']}]({URL}/tree/{metadata['source_revision']}).\n\n"
        "The AArch64 archive installs Git, HTTP/HTTPS helpers, templates, a CA bundle "
        "and dependency licenses under `/usr` and `/etc`. Use the accompanying "
        "`bundle.toml` with Scarlet SDK rootfs assembly. Other architectures have no archive.\n\n"
        f"Requires Scarlet kernel [{metadata['required_kernel_revision']}]"
        f"(https://github.com/petitstrawberry/Scarlet/commit/{metadata['required_kernel_revision']}) "
        "or a kernel carrying its atomic dup2 and pipe metadata changes, with matching native userspace. "
        "`kernel-prerequisites.patch` contains those two commits; it does not install a kernel. "
        "Provide `/bin/sh`, a writable repository filesystem, `/tmp`, and native networking for HTTPS.\n\n"
        "CI checks host unit tests, the complete HTTP archive link, static native AArch64 ELF "
        "headers, safe archive paths/links, licenses, manifests and SHA-256 hashes. "
        "Guest execution is covered separately by the recorded tmpfs, ext2 and HTTPS acceptance "
        f"in [the port guide]({URL}/blob/{metadata['source_revision']}/compat/scarlet/README.md#validation). "
        "This release workflow does not rerun QEMU acceptance.\n\n"
        "SSH, LFS, GPG and external credential helpers require separate native ports. "
        "Authenticated HTTPS push is not yet guest-tested. See the port guide for shell and filesystem limits.\n\n"
        "`SHA256SUMS` covers every attached file except itself. The source archive contains "
        "this exact Git revision and the pinned Nix/Cargo dependency recipes; runtime licenses "
        "and dependency locks are also installed in the binary archive. Git is GPL-2.0; "
        "see `COPYING` and the bundled dependency notices.\n"
    )


def package(stage, output, version, revision):
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Release output directory must be empty")
    archive = output / f"git-scarlet-aarch64-{version}.tar.gz"
    write_archive(stage, archive)
    manifest = verify_archive(archive)
    if manifest["source_revision"] != revision:
        raise ValueError("Installed binaries have a different source revision")
    source = output / f"git-scarlet-source-{version}.tar.gz"
    with tempfile.TemporaryFile() as raw:
        subprocess.run(["git", "archive", "--format=tar", f"--prefix=git-{version}/", revision],
                       cwd=ROOT, stdout=raw, check=True)
        raw.seek(0)
        with source.open("wb") as target, gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
            shutil.copyfileobj(raw, compressed)
    patches = sorted((ROOT / "compat/scarlet/kernel-patches").glob("*.patch"))
    if len(patches) != 2:
        raise ValueError("Expected both kernel prerequisite patches")
    (output / "kernel-prerequisites.patch").write_bytes(b"".join(path.read_bytes() for path in patches))
    metadata = {
        "schema": 1,
        "version": version,
        "repository": URL,
        "source_revision": revision,
        "required_kernel_revision": manifest["required_kernel_revision"],
        "git_version": manifest["git_version"],
        "architecture": "aarch64",
        "archive": archive.name,
        "archive_sha256": sha256(archive),
        "source_archive": source.name,
        "source_sha256": sha256(source),
        "binaries": manifest["binaries"],
        "build_inputs_id": manifest["build_inputs_id"],
    }
    (output / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / "bundle.toml").write_text(bundle_text(metadata))
    (output / "README.md").write_text(release_notes(metadata))
    (output / "SHA256SUMS").write_text("".join(
        f"{sha256(path)}  {path.name}\n" for path in sorted(output.iterdir())))
    verify_release(output)


def verify_release(output):
    metadata = json.loads((output / "release.json").read_text())
    version = valid_version(metadata["version"])
    archive = f"git-scarlet-aarch64-{version}.tar.gz"
    source = f"git-scarlet-source-{version}.tar.gz"
    expected = {archive, source, "kernel-prerequisites.patch", "bundle.toml", "release.json", "README.md"}
    if {p.name for p in output.iterdir()} != expected | {"SHA256SUMS"}:
        raise ValueError("Unexpected or missing release files")
    checksums = {}
    for line in (output / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if not match or match[2] in checksums:
            raise ValueError("Invalid or duplicate checksum entry")
        checksums[match[2]] = match[1]
    if checksums.keys() != expected:
        raise ValueError("Checksum manifest has an unexpected file set")
    for name, digest in checksums.items():
        path = output / name
        if path.is_symlink() or not path.is_file() or sha256(path) != digest:
            raise ValueError(f"Release checksum mismatch: {name}")
    if (metadata["schema"] != 1 or metadata["repository"] != URL
            or metadata["archive"] != archive or metadata["source_archive"] != source
            or metadata["archive_sha256"] != checksums[archive]
            or metadata["source_sha256"] != checksums[source]):
        raise ValueError("Release metadata does not describe the attached archives")
    manifest = verify_archive(output / archive)
    for key in ("source_revision", "required_kernel_revision", "git_version", "architecture", "binaries", "build_inputs_id"):
        if metadata[key] != manifest[key]:
            raise ValueError(f"Installed and release metadata disagree: {key}")
    if (output / "bundle.toml").read_text() != bundle_text(metadata):
        raise ValueError("Bundle does not pin this archive")
    if (output / "README.md").read_text() != release_notes(metadata):
        raise ValueError("Release notes do not describe this build")
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version")
    version.add_argument("--requested", default="")
    build = commands.add_parser("build")
    build.add_argument("--version", required=True)
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    verify = commands.add_parser("verify")
    verify.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "version":
        print(choose_version(args.requested))
    elif args.command == "verify":
        print(json.dumps(verify_release(args.artifacts), indent=2))
    else:
        valid_version(args.version)
        if run("git", "status", "--porcelain"):
            raise ValueError("Build from a clean committed checkout")
        revision = run("git", "rev-parse", "HEAD")
        subprocess.run(["compat/scarlet/build.sh", f"-j{args.jobs}"], cwd=ROOT, check=True)
        subprocess.run(["compat/scarlet/http/verify.sh"], cwd=ROOT, check=True)
        with tempfile.TemporaryDirectory(prefix="git-scarlet-stage-") as temporary:
            stage = Path(temporary)
            subprocess.run(["python3", "compat/scarlet/install.py", "--destdir", str(stage)], cwd=ROOT, check=True)
            package(stage, args.output.resolve(), args.version, revision)


if __name__ == "__main__":
    main()
