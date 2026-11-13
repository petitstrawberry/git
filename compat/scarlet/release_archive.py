"""Deterministic, host-only packaging of install.py's native Scarlet tree."""
import fnmatch
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import tarfile
import tempfile

MANIFEST = "usr/share/git-core/scarlet-build.json"
CA_BUNDLE = "etc/ssl/certs/ca-certificates.crt"
BINARIES = {"git": "usr/bin/git", **{
    name: "usr/libexec/git-core/" + name for name in ("git-remote-http", "git-http-fetch")}}
# Names come from install.py and http/package.py; Cargo versions may change.
NOTICES = (
    "COPYING", "musl-COPYRIGHT", "sha1dc-LICENSE", "zlib-LICENSE", "scarlet-sdk-LICENSE",
    "ca-bundle-MPL-2.0.txt", "ca-bundle-SOURCE.txt", "scarlet_runtime/LLVM/LICENSE.TXT",
    "scarlet_rust_toolchain/compiler-builtins/LICENSE.txt",
    "scarlet_rust_toolchain/compiler-builtins/libm/LICENSE.txt",
    "scarlet_rust_toolchain/backtrace/LICENSE-MIT",
    "scarlet_rust_toolchain/portable-simd/LICENSE-MIT",
    "scarlet_rust_toolchain/stdarch/LICENSE-MIT",
    "http/curl-sys/LICENSE", "http/curl-sys/libcurl-COPYING",
    "http/*/LICENSE-BoringSSL", "http/*/LICENSE-other-bits",
    "http/rustls-[0-9]*/LICENSE-ISC", "http/rustls-ffi-*/LICENSE",
    "http/rustls-webpki-*/LICENSE", "http/subtle-*/LICENSE", "http/untrusted-*/LICENSE.txt",
    *(f"http/{name}/LICENSE-MIT" for name in (
        "cfg-if-*", "getrandom-*", "libc-*", "libz-sys-*", "log-*", "once_cell-*",
        "rustls-pki-types-*", "rustls-platform-verifier", "zeroize-*")),
)


def _members(archive):
    members = {}
    for member in archive:
        parts = member.name.split("/")
        if (parts[0] not in ("usr", "etc") or any(p in ("", ".", "..") for p in parts)
                or "\\" in member.name or "\0" in member.name or member.name in members
                or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE)
                or member.sparse is not None or member.size < 0
                or (not member.isfile() and member.size != 0)
                or (len(parts) == 1 and not member.isdir())):
            raise ValueError(f"unsafe or duplicate archive member: {member.name}")
        members[member.name] = member
    directories = {"/".join(name.split("/")[:i]) for name in members
                   for i in range(1, len(name.split("/")))}
    directories.update(name for name, member in members.items() if member.isdir())
    for name, member in members.items():
        if name in directories and not member.isdir():
            raise ValueError(f"non-directory archive ancestor: {name}")
        seen = set()
        while member.issym():
            target = member.linkname
            if name in seen or not target or target.startswith("/") or "\\" in target or "\0" in target:
                raise ValueError(f"unsafe or cyclic symlink: {name}")
            seen.add(name)
            parts = name.split("/")[:-1]
            components = target.split("/")
            for i, part in enumerate(components):
                if part == "..":
                    if not parts:
                        raise ValueError(f"symlink escapes archive: {name}")
                    parts.pop()
                elif part not in ("", "."):
                    parts.append(part)
                if i < len(components) - 1 and parts and "/".join(parts) not in directories:
                    raise ValueError(f"non-directory symlink target component: {name}")
            name = "/".join(parts)
            member = members.get(name)
            if member is None or member.isdir():
                raise ValueError(f"symlink must resolve to an existing file: {name}")
    return members


def _elf(data, name):
    if len(data) < 64 or data[:8] != b"\x7fELF\x02\x01\x01\x53":
        raise ValueError(f"not a native Scarlet ELF: {name}")
    (_, kind, machine, version, entry, phoff, _, _, ehsize, phsize, phnum,
     *_) = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    if (kind not in (2, 3) or machine != 183 or version != 1 or ehsize != 64
            or phsize != 56 or not 0 < phnum < 65535 or phoff < 64
            or phoff + phsize * phnum > len(data)):
        raise ValueError(f"invalid AArch64 executable header: {name}")
    executable_entry = False
    for i in range(phnum):
        kind, flags, offset, address, _, size, memory, _ = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + i * phsize)
        if kind == 3 or offset + size > len(data) or (kind == 1 and size > memory):
            raise ValueError(f"invalid or PT_INTERP ELF segment: {name}")
        if kind == 2 and (size % 16 or any(tag == 1 for tag, _ in
                                         struct.iter_unpack("<qQ", data[offset:offset + size]))):
            raise ValueError(f"ELF has dynamic dependencies: {name}")
        executable_entry |= kind == 1 and flags & 1 and address <= entry < address + size
    if not executable_entry:
        raise ValueError(f"ELF entry is outside executable load segments: {name}")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def verify_archive(path: Path) -> dict:
    """Return embedded provenance, or raise ValueError for invalid release contents."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = _members(archive)

            def read(name):
                member = members.get(name)
                if member is None or not member.isfile() or not member.size:
                    raise ValueError(f"missing or empty regular file: {name}")
                return archive.extractfile(member).read()

            manifest = json.loads(read(MANIFEST), object_pairs_hook=_unique)
            if not isinstance(manifest, dict) or not isinstance(manifest.get("binaries"), dict):
                raise ValueError("manifest must contain binary hashes")
            if (manifest.get("source_dirty") is not False or manifest.get("architecture") != "aarch64"
                    or manifest.get("native_abi") != "Scarlet" or manifest.get("required_syscall") != 111):
                raise ValueError("manifest must describe a clean native AArch64 Scarlet build (syscall 111)")
            for key in ("source_revision", "required_kernel_revision"):
                if not isinstance(manifest.get(key), str) or not re.fullmatch(r"[0-9a-fA-F]{40}", manifest[key]):
                    raise ValueError(f"manifest requires a full 40-hex {key}")
            for name, location in BINARIES.items():
                data = read(location)
                if not members[location].mode & 0o111:
                    raise ValueError(f"binary is not executable: {location}")
                _elf(data, location)
                if hashlib.sha256(data).hexdigest() != manifest["binaries"].get(name):
                    raise ValueError(f"binary hash mismatch: {name}")
            https = members.get("usr/libexec/git-core/git-remote-https")
            if https is None or not https.issym() or https.linkname != "git-remote-http":
                raise ValueError("git-remote-https must symlink to git-remote-http")
            if hashlib.sha256(read(CA_BUNDLE)).hexdigest() != manifest.get("ca_bundle_sha256"):
                raise ValueError("CA bundle hash mismatch")
            for notice in NOTICES:
                if not any(fnmatch.fnmatchcase(name, "usr/share/licenses/git/" + notice)
                           and member.isfile() and member.size for name, member in members.items()):
                    raise ValueError(f"missing license notice: {notice}")
            return manifest
    except (tarfile.TarError, EOFError, UnicodeError) as error:
        raise ValueError(f"invalid release archive: {error}") from error


def write_archive(stage: Path, archive_path: Path):
    """Validate and atomically write a reproducible tar.gz, preserving modes and links."""
    stage, archive_path = Path(stage).resolve(), Path(archive_path)
    if not stage.is_dir() or archive_path.resolve().is_relative_to(stage):
        raise ValueError("use a staging directory and an archive destination outside it")
    fd, temporary = tempfile.mkstemp(prefix=".scarlet-archive-", dir=archive_path.parent)
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(stage.rglob("*")):
                    status = path.lstat()
                    member = tarfile.TarInfo(path.relative_to(stage).as_posix())
                    member.mode = stat.S_IMODE(status.st_mode)
                    member.uid = member.gid = member.mtime = 0
                    if stat.S_ISREG(status.st_mode):
                        member.size = status.st_size
                        with path.open("rb") as source:
                            archive.addfile(member, source)
                        continue
                    if stat.S_ISDIR(status.st_mode):
                        member.type = tarfile.DIRTYPE
                    elif stat.S_ISLNK(status.st_mode):
                        member.type, member.linkname = tarfile.SYMTYPE, os.readlink(path)
                    else:
                        raise ValueError(f"unsupported staging file: {path}")
                    archive.addfile(member)
        verify_archive(Path(temporary))
        os.replace(temporary, archive_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
