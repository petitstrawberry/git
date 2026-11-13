#!/usr/bin/env python3
"""Rebuild zlib only when native inputs change, publishing after complete success."""
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
from build_inputs import ROOT, fingerprint

SOURCES = ("adler32", "compress", "crc32", "deflate", "infback", "inffast",
           "inflate", "inftrees", "trees", "uncompr", "zutil")
PRODUCTS = ("lib/libz.a", "include/zlib.h", "include/zconf.h", "LICENSE")


def build(root=ROOT):
    inputs = fingerprint(root)
    parent = root / ".scarlet-build"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / "zlib"
    # Serialize publishers; never reuse a half-extracted source directory.
    with (parent / ".zlib.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stamp = destination / "build-inputs.json"
        try:
            cached = json.loads(stamp.read_text())
        except (OSError, ValueError):
            cached = None
        if cached == inputs and all((destination / name).is_file() for name in PRODUCTS):
            print(f"Native zlib cached: {inputs['id']}")
            return
        with tempfile.TemporaryDirectory(prefix=".zlib-build-", dir=parent) as temporary:
            temporary = Path(temporary)
            source, staged = temporary / "source", temporary / "install"
            source.mkdir()
            (staged / "include").mkdir(parents=True)
            (staged / "lib").mkdir()
            subprocess.run(["tar", "xf", inputs["environment"]["SCARLET_ZLIB_SOURCE"],
                            "-C", str(source), "--strip-components=1"], check=True)
            for name in SOURCES:
                subprocess.run([str(root / "compat/scarlet/cc.sh"), "-O2",
                                "-ffunction-sections", "-fdata-sections", "-fno-stack-protector",
                                f"-DSCARLET_BUILD_ID=scarlet_{inputs['id']}",
                                "-DHAVE_UNISTD_H", "-I.", "-c", f"{name}.c", "-o", f"{name}.o"],
                               cwd=source, check=True)
            subprocess.run([inputs["environment"]["SCARLET_AR"], "rcs", str(staged / "lib/libz.a"),
                            *(str(source / f"{name}.o") for name in SOURCES)], check=True)
            for name in ("zlib.h", "zconf.h"):
                shutil.copy2(source / name, staged / "include" / name)
            shutil.copy2(source / "LICENSE", staged / "LICENSE")
            (staged / "build-inputs.json").write_text(json.dumps(inputs, sort_keys=True, indent=2) + "\n")
            # Reject a recipe/environment change that happened during compilation.
            if fingerprint(root) != inputs:
                raise RuntimeError("native inputs changed while building zlib; retry")
            previous = temporary / "previous"
            if destination.exists():
                destination.rename(previous)
            try:
                staged.rename(destination)
            except BaseException:
                if previous.exists():
                    previous.rename(destination)
                raise
            # TemporaryDirectory removes the extract, objects and old installation.
        print(f"Native zlib built: {inputs['id']}")


if __name__ == "__main__":
    try:
        build()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Scarlet zlib: {error}", file=sys.stderr)
        sys.exit(1)
