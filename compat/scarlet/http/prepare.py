#!/usr/bin/env python3
"""Prepare the small, declaration-only libc patch without copying Cargo caches."""
import hashlib
import io
import os
from pathlib import Path
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
VERSION = "0.2.177"
DIGEST = "2874a2af47a2325c2001a6e6fad9b16a53b802102b528163885171cf92b15976"
destination = ROOT / "target/deps" / f"libc-{VERSION}"
if not destination.exists():
    cargo_cache = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    cached = list(cargo_cache.glob(f"registry/cache/*/libc-{VERSION}.crate"))
    if cached:
        data = cached[0].read_bytes()
    else:
        with urllib.request.urlopen(
            f"https://static.crates.io/crates/libc/libc-{VERSION}.crate"
        ) as response:
            data = response.read()
    if hashlib.sha256(data).hexdigest() != DIGEST:
        raise SystemExit("libc source checksum mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(destination.parent, filter="data")
    source = destination / "src/lib.rs"
    original = source.read_text()
    anchor = '    } else if #[cfg(unix)] {'
    if original.count(anchor) != 1:
        raise SystemExit("libc source no longer matches the Scarlet patch")
    source.write_text(original.replace(anchor, '''    } else if #[cfg(target_os = "scarlet")] {
        mod primitives;
        pub use crate::primitives::*;
        mod scarlet;
        pub use crate::scarlet::*;
        prelude!();
''' + anchor))
patch = (ROOT / "patches/libc-scarlet.rs").read_bytes()
module = destination / "src/scarlet.rs"
if not module.exists() or module.read_bytes() != patch:
    module.write_bytes(patch)
