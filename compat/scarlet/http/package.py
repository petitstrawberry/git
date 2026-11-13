#!/usr/bin/env python3
"""Publish C archives using only target dependency objects, never a second std."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_inputs import fingerprint, sha256

root = Path(__file__).resolve().parent
target = root / "target"
prefix = target / "install"
ar = os.environ["SCARLET_AR"]
artifacts = []
manifests = set()
curl_include = None
for line in (target / "artifacts.jsonl").read_text().splitlines():
    message = json.loads(line)
    if message.get("reason") == "compiler-artifact":
        libraries = [
            Path(name) for name in message["filenames"]
            if name.endswith(".rlib") and "/aarch64-unknown-scarlet/" in name
        ]
        artifacts.extend(libraries)
        if libraries:
            manifests.add(Path(message["manifest_path"]))
    if message.get("reason") == "build-script-executed" and "curl-sys" in message["package_id"]:
        curl_include = Path(message["out_dir"]) / "include/curl"
if not curl_include or not curl_include.is_dir():
    raise SystemExit("missing target curl headers in Cargo output")
if not artifacts:
    raise SystemExit("missing target Rust dependency archives")

# Cargo can report fresh artifacts without rebuilding them. Preserve the
# exported archive's mtime as well, or every cache hit would relink Git.
build_inputs = fingerprint()
if os.environ.get("SCARLET_BUILD_ID", build_inputs["id"]) != build_inputs["id"]:
    raise SystemExit("native build inputs changed during the HTTP build; retry")
input_files = set(artifacts) | {root / "curl-config"}
input_files.update(path for path in curl_include.rglob("*") if path.is_file())
for manifest in manifests:
    source = manifest.parent
    input_files.update(path for pattern in ("LICENSE*", "COPYING*")
                       for path in source.glob(pattern) if path.is_file())
    if (source / "curl/COPYING").is_file():
        input_files.add(source / "curl/COPYING")
package_inputs = {"build_id": build_inputs["id"],
                  "files": {str(path): sha256(path) for path in sorted(input_files)}}
stamp = prefix / "package-inputs.json"


def output_hashes():
    return {str(path.relative_to(prefix)): {"sha256": sha256(path), "mode": path.stat().st_mode & 0o777}
            for path in sorted(prefix.rglob("*")) if path.is_file() and path != stamp}


try:
    previous = json.loads(stamp.read_text())
except (OSError, ValueError):
    previous = {}
if (previous.get("inputs") == package_inputs and previous.get("outputs")
        and previous["outputs"] == output_hashes()):
    print(f"Native HTTP package cached: {build_inputs['id']}")
    sys.exit(0)

objects = target / "objects"
shutil.rmtree(objects, ignore_errors=True)
objects.mkdir()
inputs = []
for archive in sorted(set(artifacts)):
    members = subprocess.check_output([ar, "t", archive], text=True).splitlines()
    for index, member in enumerate(members):
        if not member.endswith((".o", ".obj")):
            continue
        name = objects / f"{archive.stem}-{index}.o"
        with name.open("wb") as output:
            subprocess.run([ar, "p", archive, member], stdout=output, check=True)
        inputs.append(name)
(prefix / "lib/pkgconfig").mkdir(parents=True, exist_ok=True)
(prefix / "include").mkdir(exist_ok=True)
shutil.copytree(curl_include, prefix / "include/curl", dirs_exist_ok=True)
licenses = prefix / "share/licenses"
licenses.mkdir(parents=True, exist_ok=True)
for manifest in sorted(manifests):
    source = manifest.parent
    if source == root:
        continue
    destination = licenses / source.name
    candidates = [*source.glob("LICENSE*"), *source.glob("COPYING*")]
    # curl-sys bundles libcurl, which has its own license.
    if (source / "curl/COPYING").is_file():
        candidates.append(source / "curl/COPYING")
    for candidate in candidates:
        if candidate.is_file():
            destination.mkdir(exist_ok=True)
            name = "libcurl-COPYING" if candidate.parent.name == "curl" else candidate.name
            shutil.copyfile(candidate, destination / name)
archive = prefix / "lib/libcurl.a"
archive.unlink(missing_ok=True)
subprocess.run([ar, "rcs", archive, *inputs], check=True)
shutil.rmtree(objects)
version = subprocess.check_output(
    [str(Path(os.environ["SCARLET_RUST_TOOLCHAIN"]) / "bin/rustc"), "-Vv"], text=True
)
(prefix / "build-info.txt").write_text(
    "Target: aarch64-unknown-scarlet\n"
    f"Build input ID: {build_inputs['id']}\n"
    f"Shared runtime: {os.environ['SCARLET_RUNTIME']}\n"
    f"{version}\n"
    "curl-rust: 8bec16c29cb3b706e7711ec9ead45d58321d172e\n"
    "libcurl: 99debd063090445c930aa83f713183cb4a4dae24 (8.15.0)\n"
    "TLS: rustls-ffi 0.15.0, ring; HTTP/1.1, IPv4\n"
    "CA bundle: /etc/ssl/certs/ca-certificates.crt\n"
    "Rust std/allocator and C runtime: supplied by libscarlet_c.a\n"
)
(prefix / "lib/pkgconfig/libcurl.pc").write_text('''prefix=${pcfiledir}/../..
libdir=${prefix}/lib
includedir=${prefix}/include

Name: libcurl
Description: Scarlet native libcurl with rustls (requires matching scarlet-sdk runtime)
Version: 8.15.0
Libs: -L${libdir} -lcurl
Libs.private: -lz
Cflags: -I${includedir} -DCURL_STATICLIB
''')
(prefix / "bin").mkdir(exist_ok=True)
shutil.copyfile(root / "curl-config", prefix / "bin/curl-config")
(prefix / "bin/curl-config").chmod(0o755)
stamp.write_text(json.dumps({"inputs": package_inputs, "outputs": output_hashes()},
                           sort_keys=True, indent=2) + "\n")
print(f"CURL_CONFIG={prefix / 'bin/curl-config'}")
print(f"CURL archive: {archive}")
print(f"CURL headers: {prefix / 'include'}")
