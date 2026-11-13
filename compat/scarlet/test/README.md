# Native Scarlet Git acceptance

`run.py` compiles the native Rust supervisor and hook plus the C `dup2` probe,
stages the supplied Git executable, and boots an existing kernel in a private
QEMU image. It never builds Git or Scarlet, modifies the source rootfs, or opens
an existing distribution disk image for writing. The validated Git target is
AArch64.

## Run

Enter this Git checkout's `nix develop` shell after building Git. The shell
provides `SCARLET_RUST_TOOLCHAIN` and the C compiler/runtime inputs. Put QEMU,
the Scarlet Limine image plugin and mtools on PATH, or pass their full paths.
Provide the kernel, an existing native rootfs staging directory, the matching
TCG firmware pair, and a Limine cache containing `limine-12.4.0/BOOTAA64.EFI`:

```sh
python3 -B compat/scarlet/test/run.py \
  --git ./git \
  --kernel "$SCARLET_TEST_KERNEL" \
  --rootfs "$SCARLET_TEST_ROOTFS" \
  --toolchain "$SCARLET_RUST_TOOLCHAIN" \
  --qemu qemu-system-aarch64 \
  --limine-plugin cargo-scarlet-plugin-limine \
  --limine-cache "$SCARLET_LIMINE_CACHE" \
  --firmware-code "$SCARLET_EFI_CODE_ARM64_EL2" \
  --firmware-vars "$SCARLET_EFI_VARS_ARM64_EL2" \
  --accel tcg --timeout 360 --output .scarlet-build/native-acceptance
```

Use the kernel revision recorded in `../kernel-revision`, including native
atomic descriptor replacement (syscall 111). `--rootfs` is a directory, not an
ext2 image. Only its `/init`, `/bin/scarlet-ld` and `/bin/sh` are copied. The
native distribution init establishes the Environment, writable root overlay and
`/tmp`, then execs `/bin/git-smoke`. Fixtures live under `/fixtures` because init
mounts a new filesystem over `/tmp`. The runner supplies `/mnt/newroot`, `/dev`,
`/tmp`, `/root`, `/etc` and `/bin` before bootstrap.

A supplied `--output` must not exist. `--prepare-only` builds artifacts without
starting QEMU. `--keep-image` retains boot images and staging; otherwise those
copies, EFI variables and the private Limine cache are removed. Logs, commands,
input/deployed ELF hashes and `result.json` remain. Each run terminates only its
own QEMU process. Success requires all 15 ordered case markers and the final
PASS, not just QEMU exit zero. Child output is escaped so it cannot forge a
supervisor marker.

TCG uses `-cpu max`; `--accel hvf` uses `-cpu host` and requires its matching
firmware pair. Both default to two CPUs and 2 GiB and include a real virtio RNG.
`--smp`, `--memory`, `--timeout` and `--command-timeout` are explicit overrides.
The harness has RISC-V support for future native Git/rootfs/kernel inputs, but
the complete RISC-V Git port has not been built or accepted.

## Checks

The 15 cases cover descriptor redirection, version, init, clean/untracked status,
staging, commit, diff contents and exit 1, branch checkout, local clone, fetch,
push, divergent merge, native hooks, fsck and cancellation. Every command has a
bounded wait and an expected exit code; refs and file contents are checked after
changes. Repository and remote paths include spaces and an apostrophe, and a
tracked pathname includes a space.

`dup2.c` calls syscall 111 and checks atomic replacement, shared file offsets,
cleared close-on-exec on the new descriptor, same-fd flag preservation, invalid
source/same-fd failures without target mutation, and real stdout redirection.
The Rust supervisor independently checks the redirected bytes and success marker.

Local clones use `--no-hardlinks`; this does not establish hardlink support.
Fetch/push use real upload-pack/receive-pack subprocesses with a local bare repo.
The merge case creates divergent histories and checks both parents and merged
contents. The staged native `hook.rs` executable is selected with `core.hooksPath`;
acceptance, rejection without changing HEAD/staged content, and retry are all
verified. This tests native executable hooks, not POSIX shell-script compatibility.

Cancellation keeps `git hash-object --stdin` blocked on an open pipe, sends native
`Kill(pid, 9)` (syscall 6), requires Scarlet exit code 137, then checks Git still
works. The shipped Rust toolchain returns Unsupported from `Child::kill()`, so
this is explicitly native syscall cancellation, not acceptance of that Rust API.

The default repositories use tmpfs. Run [the ext2 suite](ext2-README.md) for all
these cases plus unmount/remount persistence, file/ref comparisons and offline
host filesystem checking. [The HTTPS suite](../http/README.md) separately tests
certificate rejection and Git smart HTTPS clone against host-captured refs/files.
SSH, authenticated remote push, signatures, LFS, and the upstream POSIX shell
test suite are outside these fixtures.

## Host checks

```sh
python3 -B compat/scarlet/test/test_runner.py
```

These check archive preservation, native ELF identity, strict evidence parsing,
ext2 results and refusal to reuse an existing disk. They do not execute Git on
Scarlet or substitute for guest acceptance.
