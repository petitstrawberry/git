#!/usr/bin/env python3
"""Content identity for native build recipes and immutable SDK/toolchain paths."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT = (
    "SCARLET_CC", "SCARLET_AR", "SCARLET_LD", "SCARLET_RUNTIME",
    "SCARLET_RUST_TOOLCHAIN", "SCARLET_ZLIB_SOURCE",
)
RECIPES = (
    "compat/scarlet/build_inputs.py", "compat/scarlet/build_zlib.py",
    "compat/scarlet/build.sh", "compat/scarlet/config.mak",
    "compat/scarlet/cc.sh", "compat/scarlet/mark-elf.py",
    "compat/scarlet/process/Cargo.toml", "compat/scarlet/process/Cargo.lock",
    "compat/scarlet/http/build.sh", "compat/scarlet/http/cc.sh",
    "compat/scarlet/http/package.py", "compat/scarlet/http/prepare.py",
    "compat/scarlet/http/Cargo.toml", "compat/scarlet/http/Cargo.lock",
    "compat/scarlet/http/curl-config", "compat/scarlet/http/patches/libc-scarlet.rs",
)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(root=ROOT, environment=None):
    environment = os.environ if environment is None else environment
    paths = {}
    for name in ENVIRONMENT:
        value = environment.get(name)
        if not value:
            raise ValueError(f"{name} is required; use nix develop")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"{name} must be an absolute immutable toolchain path")
        paths[name] = str(path.resolve(strict=True))
    inputs = {"schema": 1, "environment": paths,
              "recipes": {name: sha256(root / name) for name in RECIPES}}
    payload = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    return {"id": hashlib.sha256(payload).hexdigest(), **inputs}


def write_if_changed(path, data):
    path = Path(path)
    if not path.exists() or path.read_text() != data:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=Path, help="save input provenance, preserving mtime on a cache hit")
    args = parser.parse_args()
    inputs = fingerprint()
    if args.write:
        write_if_changed(args.write, json.dumps(inputs, sort_keys=True, indent=2) + "\n")
    print(inputs["id"])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"Scarlet build inputs: {error}", file=sys.stderr)
        sys.exit(1)
