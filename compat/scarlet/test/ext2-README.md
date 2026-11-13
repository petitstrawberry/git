# Native Git acceptance on ext2

`ext2-run.py` reuses the core runner and all 15 Git cases, adds six mount and
persistence checks, and requires host `e2fsck -f -n` to exit **0**. It creates a
new private disk; it never formats an existing image or repairs a filesystem.
The validated architecture is AArch64 under QEMU TCG.

Enter the Git repository's `nix develop` shell. Put QEMU, the Scarlet Limine
image plugin, mtools (`mformat`, `mmd`, `mcopy`), and e2fsprogs (`mke2fs`,
`e2fsck`) on PATH. `--mke2fs` and `--e2fsck` can select executables explicitly.
The SDK shell provides `SCARLET_RUST_TOOLCHAIN` and the native C compiler inputs.

Provide these inputs as environment variables:

- `SCARLET_EXT2_KERNEL`: built native kernel with syscall 111 (native dup2).
- `SCARLET_EXT2_ROOTFS`: existing native staging directory containing `init`,
  `bin/scarlet-ld` and `bin/sh`; this directory is only read.
- `SCARLET_EFI_CODE_ARM64_EL2` and `SCARLET_EFI_VARS_ARM64_EL2`: the matching
  AArch64 TCG firmware pair.
- `SCARLET_LIMINE_CACHE`: existing cache containing
  `limine-12.4.0/BOOTAA64.EFI`. Only that EFI file is copied into the private
  cache, which is deleted after building the boot image.

From that shell, with a freshly built native `./git`:

```sh
python3 -B compat/scarlet/test/ext2-run.py \
  --git ./git \
  --kernel "$SCARLET_EXT2_KERNEL" \
  --rootfs "$SCARLET_EXT2_ROOTFS" \
  --toolchain "$SCARLET_RUST_TOOLCHAIN" \
  --qemu qemu-system-aarch64 \
  --limine-plugin cargo-scarlet-plugin-limine \
  --limine-cache "$SCARLET_LIMINE_CACHE" \
  --firmware-code "$SCARLET_EFI_CODE_ARM64_EL2" \
  --firmware-vars "$SCARLET_EFI_VARS_ARM64_EL2" \
  --mke2fs mke2fs --e2fsck e2fsck \
  --output .scarlet-build/ext2-acceptance --timeout 360
```

`--output` must not exist. `--prepare-only` builds without starting QEMU.
`--keep-image` retains the boot images and staging; normally those copies are
removed. The private `git-data.ext2` is retained for inspection with the logs
and manifests. Remove it when no longer needed; it is not a dependency cache.

The default disk is 256 MiB, with 4096-byte blocks, 128-byte inodes, and only
the ext2 `filetype`, `sparse_super` and `large_file` features. `--disk-mib`
changes the size. The host runs read-only fsck before and after the VM; a
nonzero status, including "errors corrected" (1), fails acceptance.

The data disk uses QEMU `-drive id=git-ext2,if=none,format=raw,file=...` and
`-device virtio-blk-device,drive=git-ext2,bus=virtio-mmio-bus.2`. Native device
names are `/dev/vblkN`. The guest matches the generated filesystem UUID,
rather than relying on discovery order. `rootfstype=git-smoke-no-root` prevents
init's automatic root-disk probe from selecting the blank test disk.

The normal init retains writable VfsView handles when it execs the supervisor
as PID 1. Those handles authorize native `FsMount` (500) and `FsUmount` (501).
No guest mount/unmount utility or POSIX shell is required. The supervisor
unmounts init's temporary `/tmp`, then mounts ext2 there with
`FsMount(device, "/tmp", "ext2", 0, "device=/dev/vblkN,rw")`. The device must
appear in the options string. It runs the unchanged core cases on that mount,
flushes and records every fixture file, and saves the refs from all three repos.

After `FsUmount("/tmp", 0)`, it verifies that the fixture disappeared, mounts
the disk again, compares every path and file byte, compares the refs, and runs
`git fsck --full --strict` in each repo. It flushes and unmounts once more before
the final guest PASS. The host stops and reaps its QEMU process before fsck.
This proves ordinary unmount/remount persistence for the fixture; it does not
test sudden power loss, disk-full recovery, or concurrent metadata repair.

Evidence is retained in `commands.json`, `ext2-commands.json`, `result.json`,
`serial.log`, `mke2fs.log`, and both `e2fsck-*.log` files. The manifests record
the deployed Git/kernel hashes and the private disk UUID; `result.json` records
the final disk hash and host fsck status. Host checks in `test_runner.py` reject
incomplete remount evidence, a failing checker or guest, and an existing disk.

```sh
python3 -B compat/scarlet/test/test_runner.py
```
