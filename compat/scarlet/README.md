# Git for Scarlet

This fork ports Git 2.56.1 to Scarlet's Native ABI. The current build targets
**AArch64** and produces static native ELF executables with OSABI `0x53`.
Git's repository formats, object handling, merge algorithms and transports are
the upstream implementations. Child processes use Scarlet Environments; C and
Rust code share the SDK runtime. There is no Linux ABI or host Git proxy.

## Build

Clone the `scarlet` branch of <https://github.com/petitstrawberry/git>, then run:

```sh
nix develop --command compat/scarlet/build.sh -j8
nix develop --command python3 compat/scarlet/install.py \
  --destdir .scarlet-build/rootfs
```

`flake.lock` pins the published Scarlet SDK and its compiler/runtime inputs.
The process and HTTP crates have separate committed Cargo lockfiles. Build
scripts use `--locked`; the libc source patch is verified against its upstream
SHA-256 before extraction. The initial build needs network access for these
pinned dependencies. The tested host is Apple Silicon macOS.
Build inputs are fingerprinted from the immutable SDK/compiler paths and build
recipes. Changing them rebuilds zlib, the HTTP archive and Git's C objects;
unchanged inputs retain the cache. `.scarlet-build/build-inputs.json` records
the exact inputs used.

The three build outputs are `git`, `git-remote-http` and `git-http-fetch`.
The installer stages `/usr/bin/git`, built-in command links, HTTP/HTTPS helpers,
templates, dependency licenses and a CA bundle. It also records the executable
SHA-256 hashes in `/usr/share/git-core/scarlet-build.json`.
Copy the staged tree into a Scarlet distribution's rootfs during its image
build. `--destdir` must be a staging directory; the installer rejects `/`.

For an archive suitable for rootfs assembly:

```sh
tar -C .scarlet-build/rootfs -czf \
  .scarlet-build/git-2.56.1-scarlet-aarch64.tar.gz .
```

## Kernel and runtime requirements

Use the Scarlet branch
[`feat/native-git-dup2`](https://github.com/petitstrawberry/Scarlet/tree/feat/native-git-dup2)
with its matched native init, loader and filesystem services. Git needs atomic
descriptor replacement (`HandleDuplicateToWithStatus`, syscall 111) and pipe
metadata for receiving packfiles. Older kernels are insufficient even if they
can launch the executable.
The tested source revision is
[`cb70ea384d217b57966f10fbe1a199a14ae1e191`](https://github.com/petitstrawberry/Scarlet/commit/cb70ea384d217b57966f10fbe1a199a14ae1e191),
also recorded in `kernel-revision` and the staged build manifest.

The distribution must provide `/bin/sh`, a writable repository filesystem and
`/tmp`. Set `user.name` and `user.email` through normal Git configuration.
The installed defaults are `/usr/libexec/git-core` and
`/usr/share/git-core/templates`; `GIT_EXEC_PATH` and `GIT_TEMPLATE_DIR` can
override them.

HTTPS requires native IPv4 networking and DNS, a correct wall clock, a working
entropy source, and `/etc/ssl/certs/ca-certificates.crt`. A minimal bootstrap
must start `netcfgd` and `resolverd`; a normal distribution can use its service
manager. The installed CA bundle comes from the pinned Nix input. Git's usual
`http.sslCAInfo` and `GIT_SSL_CAINFO` overrides remain available. Certificate and
hostname verification stay enabled. See [HTTP build and validation](http/README.md).

## Platform differences

- Scarlet has no POSIX owner/group permission classes or chmod operation.
  Repository initialization detects that executable-bit changes are unavailable
  and sets `core.filemode=false`. Temporary directories use the supported native
  creation mode and inherit filesystem access from the Environment; they do not
  provide Unix `0700` isolation. `credential-store` refuses writes because it
  cannot enforce private credential-file permissions. Existing stores can be
  read, and native credential helpers and `GIT_ASKPASS` can be invoked.
- The existing Scarlet shell is not a complete POSIX shell. Native executable
  hooks work. Script hooks, shell aliases, custom transport commands and shell
  snippets require a shell that supports their syntax. Default local Git
  transports pass paths as structured arguments, including spaces and quotes.
- Native process termination works, but asynchronous POSIX signal handlers,
  process groups and timers are unavailable. Abrupt termination can leave lock
  files, as can a crash. Nonzero `upload-pack --timeout` is rejected explicitly.
- HTTP/1.1 and HTTPS over IPv4 are built in. SSH, signing programs, LFS and
  external credential helpers require separately ported native executables.
  Perl/Python/Tcl commands, gettext, iconv, Unix sockets and HTTP/2/3 are not
  included in this configuration. RISC-V Git is not yet built or validated.

## Validation

Native AArch64 acceptance on 2026-11-13:

| Suite | Result |
| --- | --- |
| tmpfs repository | 15/15 cases: descriptor redirection, version, init, status, add, commit, diff, checkout, local clone, fetch, push, divergent merge, accepting/rejecting native hook, fsck, cancellation |
| private ext2 disk | 21/21 cases: the same operations plus mounting, remounting, byte-for-byte file and ref comparisons, fsck after remount and clean unmount; host `e2fsck -fn` exit 0 |
| HTTPS | 6/6 cases: pipe metadata, trusted HTTPS, unrelated-CA rejection, Git ls-remote, shallow clone and strict fsck; HEAD and README match host Git |

The HTTPS fixture uses the public `octocat/Hello-World` repository. Authenticated
HTTPS push and SSH have not been exercised; the push acceptance uses a local bare
repository. Ext2 validation covers normal unmount/remount, not power-loss or
concurrent-writer recovery. Scarlet's ext2 implementation still has limitations
around atomic replacement failures, directory/device flushes and very large files.

[The native smoke runner](test/README.md) boots a private QEMU image and checks
real Git commands, resulting refs and file contents. It never modifies the
source distribution image. [The HTTPS runner](http/README.md) additionally
compares remote refs and cloned file contents with independently captured host
results and tests rejection of an unrelated CA.

The process adapter's host tests cover environment deltas, argument boundaries,
script interpretation and native exit-code conversion:

```sh
nix develop --command cargo test --locked \
  --manifest-path compat/scarlet/process/Cargo.toml
python3 -B compat/scarlet/test/test_runner.py
```

`strftime.c` adapts musl 1.2.5's C-locale formatter, with its copyright and license
in `musl-COPYRIGHT`. To compare it against the host C library across daily dates
from 1900 through 2100, including ISO week boundaries and buffer sizes:

```sh
mkdir -p .scarlet-build
cc -D_GNU_SOURCE -Dstrftime=scarlet_test_strftime -c \
  compat/scarlet/strftime.c -o .scarlet-build/strftime-host.o
cc compat/scarlet/strftime-test.c .scarlet-build/strftime-host.o \
  -o .scarlet-build/strftime-test
.scarlet-build/strftime-test
```

The upstream shell test suite requires a fuller POSIX userspace and has not been
run on Scarlet. Native acceptance results establish the tested operations, not
every Git command or filesystem's crash-durability guarantees.
