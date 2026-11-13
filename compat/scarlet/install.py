#!/usr/bin/env python3
"""Stage the native Git binaries, helpers, templates and licenses in a rootfs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--destdir", required=True, type=Path)
parser.add_argument("--ca-bundle", type=Path, default=os.environ.get("SCARLET_CA_BUNDLE"))
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
dest = args.destdir.resolve()
if dest == Path("/"):
    parser.error("use a staging directory, not the host root")

def copy(source, target):
    target = dest / target
    target.parent.mkdir(parents=True, exist_ok=True)
    # Nix certificates and license files are read-only. Replace a previous
    # installation atomically instead of opening that read-only file in place.
    fd, temporary = tempfile.mkstemp(prefix=".git-install-", dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target

def link(value, target):
    target = dest / target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(value)

manifest = {}
for name, target in (("git", "usr/bin/git"),
                     ("git-remote-http", "usr/libexec/git-core/git-remote-http"),
                     ("git-http-fetch", "usr/libexec/git-core/git-http-fetch")):
    source = root / name
    header = source.read_bytes()[:20]
    if header[:8] != b"\x7fELF\x02\x01\x01\x53":
        parser.error(f"build the native Scarlet binary first: {source}")
    copy(source, target)
    manifest[name] = hashlib.sha256(source.read_bytes()).hexdigest()

link("git-remote-http", "usr/libexec/git-core/git-remote-https")
for name in re.findall(r'\{ "([a-z0-9-]+)", cmd_', (root / "git.c").read_text()):
    link("git", f"usr/bin/git-{name}")
    link("../../bin/git", f"usr/libexec/git-core/git-{name}")

for source in (root / "templates").rglob("*"):
    relative = source.relative_to(root / "templates")
    if source.is_file() and ((relative.parts[0] == "hooks" and source.suffix == ".sample") or relative.as_posix() in ("description", "info/exclude")):
        target = copy(source, Path("usr/share/git-core/templates") / relative)
        target.write_text(target.read_text().replace("@SHELL_PATH@", "/bin/sh").replace("@PERL_PATH@", "perl"))

copy(root / "COPYING", "usr/share/licenses/git/COPYING")
copy(root / "sha1dc/LICENSE.txt", "usr/share/licenses/git/sha1dc-LICENSE")
copy(root / "compat/scarlet/musl-COPYRIGHT", "usr/share/licenses/git/musl-COPYRIGHT")
copy(root / ".scarlet-build/zlib/LICENSE", "usr/share/licenses/git/zlib-LICENSE")
for variable, subdir in (("SCARLET_RUNTIME", "share/licenses"),
                         ("SCARLET_RUST_TOOLCHAIN", "lib/rustlib/src/rust/library")):
    if variable in os.environ:
        license_root = Path(os.environ[variable]) / subdir
        for source in license_root.rglob("LICENSE*"):
            if source.is_file():
                copy(source, Path("usr/share/licenses/git") / variable.lower() / source.relative_to(license_root))
if "SCARLET_SDK_SOURCE" in os.environ:
    copy(Path(os.environ["SCARLET_SDK_SOURCE"]) / "LICENSE", "usr/share/licenses/git/scarlet-sdk-LICENSE")
licenses = root / "compat/scarlet/http/target/install/share/licenses"
for source in licenses.rglob("*"):
    if source.is_file():
        copy(source, Path("usr/share/licenses/git/http") / source.relative_to(licenses))
if args.ca_bundle:
    copy(args.ca_bundle, "etc/ssl/certs/ca-certificates.crt")
    copy(root / "compat/scarlet/ca-bundle-MPL-2.0.txt", "usr/share/licenses/git/ca-bundle-MPL-2.0.txt")
    notice = dest / "usr/share/licenses/git/ca-bundle-SOURCE.txt"
    notice.write_text("The default certificate bundle is generated from Mozilla NSS root certificate data.\n"
                      "Source and build recipe: the pkgs.cacert derivation in the nixpkgs revision\n"
                      "recorded in the accompanying flake.lock. Mozilla NSS source:\n"
                      "https://hg.mozilla.org/projects/nss/\n"
                      "License text: https://www.mozilla.org/media/MPL/2.0/index.txt\n")
copy(root / "flake.lock", "usr/share/git-core/scarlet-sources/flake.lock")
copy(root / "compat/scarlet/process/Cargo.lock", "usr/share/git-core/scarlet-sources/process-Cargo.lock")
copy(root / "compat/scarlet/http/Cargo.lock", "usr/share/git-core/scarlet-sources/http-Cargo.lock")
info = dest / "usr/share/git-core/scarlet-build.json"
info.parent.mkdir(parents=True, exist_ok=True)
source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
source_dirty = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=root).returncode != 0
provenance = {
    "binaries": manifest,
    "native_abi": "Scarlet",
    "architecture": "aarch64",
    "required_syscall": 111,
    "source_repository": "https://github.com/petitstrawberry/git",
    "source_revision": source_revision,
    "source_dirty": source_dirty,
    "git_version": (root / "GIT-VERSION-FILE").read_text().partition("=")[2].strip(),
}
kernel_revision = root / "compat/scarlet/kernel-revision"
if kernel_revision.exists():
    provenance["required_kernel_revision"] = kernel_revision.read_text().strip()
build_inputs = root / ".scarlet-build/build-inputs.json"
if build_inputs.exists():
    provenance["build_inputs_id"] = json.loads(build_inputs.read_text())["id"]
    copy(build_inputs, "usr/share/git-core/scarlet-sources/build-inputs.json")
if args.ca_bundle:
    provenance["ca_bundle_sha256"] = hashlib.sha256(args.ca_bundle.read_bytes()).hexdigest()
info.write_text(json.dumps(provenance, indent=2) + "\n")
print(dest)
