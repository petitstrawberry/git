#!/usr/bin/env python3
"""Run all native Git cases on a fresh private ext2 disk, remount, then fsck.

Pass run.py's normal --kernel, --rootfs, --toolchain and QEMU/firmware options,
plus --mke2fs and --e2fsck if those tools are not on PATH. Use the same mtools
PATH and Limine/firmware arguments as README.md. Inside `nix develop`:

  python3 -B compat/scarlet/test/ext2-run.py \\
    --kernel /path/to/scarlet --rootfs /path/to/staged/rootfs \\
    --toolchain "$SCARLET_RUST_TOOLCHAIN" --output /new/acceptance-directory \\
    --mke2fs /path/to/e2fsprogs/sbin/mke2fs --e2fsck /path/to/e2fsprogs/sbin/e2fsck

The image is always newly created inside the run's private output directory.
It is retained with logs, hashes and e2fsck evidence; --keep-image also retains
boot/staging files. No existing disk is formatted, mounted by the host or
repaired. A PASS requires all guest cases and e2fsck -fn exit 0. This tests
unmount/remount persistence, not abrupt power-loss durability or concurrency.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

SOURCE = Path(__file__).resolve()
spec = importlib.util.spec_from_file_location("scarlet_core_smoke", SOURCE.with_name("run.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class Ext2Fixture:
    source = SOURCE.with_name("ext2-guest.rs")
    cases = ("ext2_mount", *core.CASES, "ext2_remount", "ext2_files", "ext2_refs", "ext2_fsck", "ext2_unmount")
    # Otherwise init tries /dev/vblk0 as an ext2 root, and discovery order could
    # accidentally pivot onto our blank disk. An unavailable rootfs driver
    # selects the normal diskless overlay without an explicit root= request.
    cmdline_extra = "rootfstype=git-smoke-no-root"

    def __init__(self, options):
        self.options = options
        self.mke2fs = core.executable(options.mke2fs)
        self.e2fsck = core.executable(options.e2fsck)

    def prepare(self, args, staging, output):
        disk = output / "git-data.ext2"
        identifier = uuid.uuid4()
        with disk.open("xb") as stream:
            stream.truncate(self.options.disk_mib * 1024 * 1024)
        disk.chmod(0o600)
        # Explicit ext2 features exclude journaling, extents, 64-bit descriptors
        # and metadata checksums. Scarlet's supported ext2 layout uses 128-byte
        # inodes; initialize the inode tables eagerly for offline fsck.
        mkfs = [self.mke2fs, "-F", "-t", "ext2", "-b", "4096", "-I", "128",
                "-O", "none,filetype,sparse_super,large_file", "-E", "lazy_itable_init=0",
                "-L", "GIT_ACCEPT", "-U", str(identifier), str(disk)]
        core.run_logged(mkfs, output, "mke2fs.log")
        core.run_logged([self.e2fsck, "-f", "-n", str(disk)], output, "e2fsck-before.log")
        (staging / "etc/git-ext2-uuid").write_text(identifier.hex + "\n")
        self.manifest = {"disk": str(disk), "disk_mib": self.options.disk_mib,
                         "uuid": str(identifier), "mkfs": mkfs,
                         "e2fsck": [self.e2fsck, "-f", "-n", str(disk)],
                         "initial_sha256": core.sha256(disk), "cases": self.cases,
                         "core_harness_sha256": core.sha256(SOURCE.with_name("guest.rs")),
                         "ext2_harness_sha256": core.sha256(self.source),
                         "runner_sha256": core.sha256(SOURCE),
                         "core_runner_sha256": core.sha256(SOURCE.with_name("run.py"))}
        (output / "ext2-commands.json").write_text(json.dumps(self.manifest, indent=2) + "\n")

    def qemu_options(self, args, output):
        # AArch64 base harness occupies buses 0 (boot), 3 (GPU), 6 (RNG).
        # RV64 uses PCI for boot and MMIO buses 1/5 for GPU/RNG. Bus 2 is free.
        return ["-drive", f"id=git-ext2,if=none,format=raw,file={core.qemu_path(output / 'git-data.ext2')}",
                "-device", "virtio-blk-device,drive=git-ext2,bus=virtio-mmio-bus.2"]

    def finish(self, result, args, output):
        # run_guest has stopped and reaped QEMU before this hook. Never fsck a
        # live VM's filesystem and never repair a failed acceptance image.
        with (output / "e2fsck-after.log").open("wb") as log:
            checked = subprocess.run(self.manifest["e2fsck"], stdout=log,
                                     stderr=subprocess.STDOUT, timeout=120)
        result["guest_result"] = result["result"]
        result["host_e2fsck_exit_code"] = checked.returncode
        result["disk"] = self.manifest["disk"]
        result["disk_sha256"] = core.sha256(Path(result["disk"]))
        if checked.returncode != 0:
            result["result"] = f"FAIL: host e2fsck exit {checked.returncode}; guest={result['guest_result']}"
        print((output / "e2fsck-after.log").read_text(errors="replace"), end="", flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mke2fs", default=os.environ.get("MKE2FS", "mke2fs"))
    parser.add_argument("--e2fsck", default=os.environ.get("E2FSCK", "e2fsck"))
    parser.add_argument("--disk-mib", type=int, default=256)
    options, rest = parser.parse_known_args()
    if "--help" in rest or "-h" in rest:
        parser.print_help()
        return core.main(rest)
    if not 32 <= options.disk_mib <= 4096:
        parser.error("--disk-mib must be between 32 and 4096")
    return core.main(rest, fixture=Ext2Fixture(options))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Scarlet Git ext2 acceptance: {error}", file=sys.stderr)
        sys.exit(1)
