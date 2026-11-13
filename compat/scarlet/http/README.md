# Native libcurl for Scarlet Git

This builds the existing Scarlet libcurl port as a C static library for Git's
normal `http.c`, `http-walker.c`, and `remote-curl.c` transport. The build targets
`aarch64-unknown-scarlet`.

From the Git checkout, build Git and its HTTP helpers, then verify the archive:

```sh
nix develop --command compat/scarlet/build.sh
nix develop --command compat/scarlet/http/verify.sh
```

`compat/scarlet/build.sh` builds zlib, the process bridge, libcurl, and Git with
HTTP enabled. To rebuild only this dependency after zlib exists, run
`nix develop --command compat/scarlet/http/build.sh`. The build supplies
`CURL_CONFIG="$PWD/compat/scarlet/http/target/install/bin/curl-config"` to Git.
Install `git-remote-http` and `git-http-fetch` under `/usr/libexec/git-core`, with
`git-remote-https -> git-remote-http` in the same directory. `NO_EXPAT` may remain
set: smart HTTP fetch/push does not need WebDAV's Expat dependency.

The dependency prefix is `compat/scarlet/http/target/install`:

| File | Use |
| --- | --- |
| `lib/libcurl.a` | libcurl, rustls-ffi, ring, and their Rust dependency objects |
| `include/curl/*.h` | Matching upstream libcurl C headers |
| `bin/curl-config` | Git's `CURL_CONFIG`; reports flags, version, TLS backend |
| `lib/pkgconfig/libcurl.pc` | Other native consumers; `pkg-config --static libcurl` |
| `build-info.txt` | Toolchain, runtime, and source provenance |
| `share/licenses/` | Bundled dependency license notices |

The archive requires Git's zlib (`-lz`) and the **matching** SDK native runtime.
The devshell supplies `SCARLET_RUNTIME`, `SCARLET_RUST_TOOLCHAIN`, `SCARLET_CC`,
`SCARLET_AR`, and `SCARLET_LD`. `SCARLET_ZLIB_PREFIX` can override the default
`.scarlet-build/zlib`. This build never creates another C runtime or zlib.

## Fixed dependency configuration

The pins reproduce Scarlet Cargo's working TLS configuration:

- `curl-rust`: `8bec16c29cb3b706e7711ec9ead45d58321d172e`, with `static-curl`
  and `rustls`, default OpenSSL features disabled.
- Its `petitstrawberry/curl` submodule:
  `99debd063090445c930aa83f713183cb4a4dae24` (reports libcurl 8.15.0-DEV).
- `rustls-ffi` 0.15.0, `rustls` 0.23.35, ring as the only crypto provider.
- Scarlet `ring`: `eb7e4c269ca35a2f3b5a782c831072562d2fb219`.
- Scarlet `rustls-platform-verifier`:
  `25d3b116f29e57fbe897b6c78a75941c90bee2b0`.
- `libz-sys` 1.1.23; Cargo's `links = "z"` override uses Git's native zlib.
- `libc` 0.2.177 with the scalar declarations from Scarlet Cargo's port. The
  upstream crate is SHA-256 checked before extraction; its MIT/Apache licenses
  remain in the extracted source. This patch exports no C runtime functions.
- `getrandom` 0.2.16 with its custom backend calling the SDK's C `getrandom`.
  SDK `flags=0` requires a registered entropy source and never uses the emergency
  PRNG. Failure propagates to ring/rustls; no insecure fallback is added.

All transitive Rust versions are locked. `build.sh` uses `--locked` and the normal
Cargo cache; it does not copy dependency caches or alter existing checkouts.

The observed TLS version string is
`rustls-ffi/0.15.0/rustls/0.23.25/ring`. In the published rustls-ffi 0.15.0 source,
`build.rs` hardcodes `RUSTLS_CRATE_VERSION = "0.23.25"` and uses it to generate
the string returned by `src/version.rs::rustls_version()`. It does not inspect
the resolved dependency version. This package pins rustls `=0.23.35`, and
`Cargo.lock` contains a single rustls package at **0.23.35**; use the lockfile
for dependency provenance rather than interpreting the static label as the
linked rustls version.

## Shared runtime and link verification

A Rust `staticlib` would normally carry its own standard library and allocator
shim. The SDK already supplies those inside `libscarlet_c.a`. `package.py`
instead combines the target dependency **rlib object members only**, excluding
metadata and all toolchain standard-library objects. It preserves libcurl's C
entry points, including the easy/multi APIs, without the Rust `curl::Easy`
wrapper or its application-level socket callback. Socket, DNS, polling,
threading, memory, errno, and FILE implementations come from the SDK.

`verify.sh` first checks for duplicate strong global symbols against all three
SDK runtime archives, then compiles a C API smoke program, links every member with
`--whole-archive` against the shared runtime and zlib, and produces
`target/link-check` with Scarlet's native ELF OSABI marker. Linker errors are
fatal; it does not permit unresolved strong symbols or duplicate definitions.
Successful linking is a build check, not a guest HTTPS test.

In the guest, run:

```sh
/bin/curl-link-check
/bin/curl-link-check https://www.kernel.org/
/bin/curl-link-check https://www.kernel.org/ /fixtures/wrong-ca.pem
```

The first command checks the C easy/multi API, TLS backend, wall clock and SDK
entropy source. URL requests require a writable `/tmp`; detailed C results are
also written to `/tmp/curl-link-check.result`. A successful transfer must return
HTTP 200 and a nonempty body. An optional second argument supplies `CURLOPT_CAINFO`;
the guest runner generates a valid but unrelated self-signed test CA for this
negative case. Both `SSL_VERIFYPEER=1` and `SSL_VERIFYHOST=2` stay enabled.
The fixture returns libcurl's error code on transfer failure.

## Guest requirements and supported configuration

HTTPS uses `/etc/ssl/certs/ca-certificates.crt` by default; Git's normal
`http.sslCAInfo`/`GIT_SSL_CAINFO` override still works. The guest needs a populated
CA bundle, working `resolverd`/IPv4 networking, accurate wall time, and an
entropy source. A missing trust store or entropy source is an error.

Use a kernel with native `dup2` (syscall 111) and pipe `FileMetadata` support,
plus compatible `init` and `scarlet-ld` binaries. Git's `index-pack` queries
metadata on pipe stdin. The pipe metadata must identify a FIFO with its shared
pipe ID and queued-byte count; the kernel preserves the metadata syscall's
legacy error sentinel for existing callers.

Deploy the distribution's maintained PEM CA bundle at that path when constructing
a minimal initramfs; neither this library nor rustls installs certificates at
runtime. Start `netcfgd` and then `resolverd` explicitly when bypassing `stemd`.
The minimal root must include `/mnt/newroot`, `/dev`, `/tmp`, `/root`, `/etc` and
`/bin` before bootstrap. The init process replaces `/tmp` with tmpfs, so staged
certificates and fixtures belong under `/etc` or `/fixtures`.

On QEMU, the tested clock is the guest RTC with `-rtc base=utc`. The fixture
requires guest time within one hour of host time; the successful run differed
by seven seconds including firmware boot. Real deployments need a correct RTC
or a trusted time-setting service before TLS validation. The test provides
`-object rng-random,id=entropy,filename=/dev/urandom` with a
`virtio-rng-device,rng=entropy` device. Merely having a `getrandom` symbol is not
enough: the SDK call must successfully obtain entropy from a registered source.

This pinned configuration supplies HTTP/1.1 and HTTPS over IPv4 with synchronous
Scarlet name resolution. HTTP/2/3, IPv6, NTLM, SPNEGO and FTP are not enabled.
Git's normal HTTP Basic credentials and HTTP proxy configuration remain libcurl
features. SSH transport and credential-helper execution use Git's process
integration.

## Reproducing guest acceptance

`guest-test.py` checks native libcurl and Git smart HTTPS in a private guest.
Enter `nix develop` from the Git checkout. In that shell, set these variables to
your local artifacts and ensure `mtools` and `openssl` are on `PATH`:

| Variable | Artifact |
| --- | --- |
| `SCARLET_SOURCE` | Scarlet source checkout with the kernel features described above |
| `SCARLET_QEMU` | `qemu-system-aarch64` executable with Scarlet support |
| `SCARLET_LIMINE_PLUGIN` | `cargo-scarlet-plugin-limine` executable |
| `SCARLET_FIRMWARE_CODE` | AArch64 OVMF `QEMU_EFI.fd` |
| `SCARLET_FIRMWARE_VARS` | Matching OVMF `QEMU_VARS.fd` template |

The devshell supplies `SCARLET_RUST_TOOLCHAIN` and the native C tools. Override
`SCARLET_KERNEL`, `SCARLET_ROOTFS`, or `SCARLET_LIMINE_CACHE` if your compatible
kernel, staged userspace, and Limine cache are built in separate locations.

```sh
SCARLET_DIST="$SCARLET_SOURCE/projects/aarch64-limine-full"
SCARLET_KERNEL="${SCARLET_KERNEL:-$SCARLET_DIST/bsp/target/aarch64-unknown-none-elf/release/scarlet}"
SCARLET_ROOTFS="${SCARLET_ROOTFS:-$SCARLET_DIST/.scarlet/staging/rootfs}"
SCARLET_LIMINE_CACHE="${SCARLET_LIMINE_CACHE:-$SCARLET_DIST/.scarlet/images/.cache}"
python3 -B compat/scarlet/http/guest-test.py \
  --format limine --accel tcg \
  --kernel "$SCARLET_KERNEL" \
  --bootstrap "$SCARLET_ROOTFS" \
  --rootfs "$SCARLET_ROOTFS" \
  --toolchain "$SCARLET_RUST_TOOLCHAIN" \
  --qemu "$SCARLET_QEMU" \
  --limine-plugin "$SCARLET_LIMINE_PLUGIN" \
  --limine-cache "$SCARLET_LIMINE_CACHE" \
  --firmware-code "$SCARLET_FIRMWARE_CODE" \
  --firmware-vars "$SCARLET_FIRMWARE_VARS" \
  --git "$PWD/git" \
  --pipe-metadata-source "$SCARLET_SOURCE/tests/native-pipe-metadata/main.c" \
  --timeout 240 \
  --output compat/scarlet/http/target/git-https-next
```

The Limine cache must contain `limine-12.4.0/BOOTAA64.EFI`. Each output path must
be new and under this component's `target/`. Run `verify.sh` first after changing
the C fixture. `--pipe-metadata-source` is optional; when supplied it compiles and
runs the kernel's C regression test before the network checks. This covers FIFO
type, endpoint/dup identity, queued-byte observation, peer closure, invalid and
closed handles, invalid output memory, negative-descriptor `EBADF`, and unchanged
regular-file metadata.

With `--git`, the runner loads `git-remote-http` and `git-http-fetch` from the
same directory as Git; `--git-remote-http` and `--git-http-fetch` can override
them. It installs Git as `/usr/bin/git`, the helpers under `/usr/libexec/git-core`,
and a relative `git-remote-https -> git-remote-http` symlink. The CPIO writer
preserves symlinks. Omit `--git` to run only the libcurl checks and optional
metadata probe.

At run time the host executes `git ls-remote --symref` against
`https://github.com/octocat/Hello-World.git`, records its HEAD, and downloads
`README` from GitHub's raw endpoint at that exact commit. The guest must pass:

1. `git ls-remote ... HEAD`, matching the host's captured object ID.
2. `git clone --depth=1 --single-branch`, matching the same HEAD, reporting a
   shallow repository, and containing byte-identical, nonempty `README`.
3. `git fsck --full --strict` in the resulting clone.

Each command has a bounded wait and captures stdout/stderr separately, then
reports escaped text through the Rust supervisor. These outputs cannot create
false case markers. `commands.json`, `host-ls-remote.txt`, and `host-README`
preserve the exact inputs and comparisons. Git is tested with its ordinary
certificate verification and the deployed CA bundle; the test does not add an
insecure transport override.

The runner removes its private images, staging tree, EFI variables, generated
private key, and copied Limine cache. It retains compact logs, results, and
manifests with kernel, Git/helper, fixture, and CA hashes. Input rootfs and
firmware files are unchanged.

## Observed acceptance

On 2026-11-13, `target/git-https-tcg-03` passed all six checks under AArch64 QEMU
TCG with Limine and OVMF in **19.994 seconds**:

| Check | Observed result |
| --- | --- |
| Native pipe metadata regression | Exit 0 |
| GET `https://www.kernel.org/` with deployed CA bundle | HTTP 200, 20,436 bytes, libcurl result 0 |
| Same URL with only the unrelated test CA | Result 60, rustls `UnknownIssuer`, HTTP 0, zero body bytes |
| Native `git ls-remote` | HEAD matched the host result |
| Native shallow clone | Same HEAD, shallow repository, matching 13-byte README |
| `git fsck --full --strict` | Exit 0 |

Both Git operations matched host HEAD
`7fd1a60b01f91b314f59955a4e4d4e80d8edf11d`. SDK entropy succeeded before both
libcurl transfers; guest time was seven seconds after the host staging timestamp.
Clone emitted a nonfatal `cannot get host name: Function not implemented` warning.

All three Git/helper hashes recorded in `commands.json` matched the frozen
binaries after the run. The tested kernel SHA-256 was
`140746d092588e1381a173261f856d602ad8e56f69b6ab522522c619b9dd5491`.
`target/git-https-tcg-03/{commands.json,result.json,serial.log,host-ls-remote.txt,host-README}`
retains the commands, input hashes, results, and host comparisons.

After preserving `target/install` and any fixture/results needed for guest
validation, the extracted temporary sources under `target/deps` and intermediate
Cargo output under `target/cargo` can be removed. `build.sh` recreates them.
