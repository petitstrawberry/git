#!/usr/bin/env python3
"""Host-only release checks with tiny ELF fixtures; no builds or guests."""
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "scarlet_release_archive", Path(__file__).resolve().parents[1] / "release_archive.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

MANIFEST = "usr/share/git-core/scarlet-build.json"
CA = "etc/ssl/certs/ca-certificates.crt"
GIT = "usr/bin/git"
HTTP = "usr/libexec/git-core/git-remote-http"
FETCH = "usr/libexec/git-core/git-http-fetch"
HTTPS = "usr/libexec/git-core/git-remote-https"
LICENSES = "usr/share/licenses/git/"
# Representative names from the actual installed runtime and HTTP package.
NOTICES = (
    "COPYING", "musl-COPYRIGHT", "sha1dc-LICENSE", "zlib-LICENSE", "scarlet-sdk-LICENSE",
    "ca-bundle-MPL-2.0.txt", "ca-bundle-SOURCE.txt", "scarlet_runtime/LLVM/LICENSE.TXT",
    "scarlet_rust_toolchain/compiler-builtins/LICENSE.txt",
    "scarlet_rust_toolchain/compiler-builtins/libm/LICENSE.txt",
    "scarlet_rust_toolchain/backtrace/LICENSE-MIT",
    "scarlet_rust_toolchain/portable-simd/LICENSE-MIT",
    "scarlet_rust_toolchain/stdarch/LICENSE-MIT",
    "http/curl-sys/LICENSE", "http/curl-sys/libcurl-COPYING",
    "http/eb7e4c2/LICENSE-BoringSSL", "http/eb7e4c2/LICENSE-other-bits",
    "http/rustls-0.23.35/LICENSE-ISC", "http/rustls-ffi-0.15.0/LICENSE",
    "http/rustls-webpki-0.103.8/LICENSE", "http/subtle-2.6.1/LICENSE",
    "http/untrusted-0.9.0/LICENSE.txt", "http/cfg-if-1.0.4/LICENSE-MIT",
    "http/getrandom-0.2.16/LICENSE-MIT", "http/libc-0.2.177/LICENSE-MIT",
    "http/libz-sys-1.1.23/LICENSE-MIT", "http/log-0.4.28/LICENSE-MIT",
    "http/once_cell-1.21.3/LICENSE-MIT", "http/rustls-pki-types-1.13.1/LICENSE-MIT",
    "http/rustls-platform-verifier/LICENSE-MIT", "http/zeroize-1.8.2/LICENSE-MIT",
)


def elf(kind=2, extra=()):
    """An ELF64 executable with a file-backed entry and optional program headers."""
    size = 64 + 56 * (1 + len(extra)) + 16
    header = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01\x53", kind,
                         183, 1, 0x400000 + size - 16, 64, 0, 0, 64, 56, 1 + len(extra), 0, 0, 0)
    load = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0, size, size, 4096)
    return header + load + b"".join(struct.pack("<IIQQQQQQ", *p) for p in extra) + bytes(16)


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="scarlet-release-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / "stage"
        for location in (GIT, HTTP, FETCH):
            self.put(location, elf(), 0o755)
        self.link(HTTPS, "git-remote-http")
        self.link("usr/bin/git-add", "git")
        self.link("usr/libexec/git-core/git-add", "../../bin/git")
        self.put(CA, b"fixture CA bundle\n")
        for notice in NOTICES:
            self.put(LICENSES + notice, b"fixture license notice\n")
        self.manifest = {
            "architecture": "aarch64", "native_abi": "Scarlet", "required_syscall": 111,
            "source_dirty": False, "source_revision": "a" * 40,
            "required_kernel_revision": "b" * 40, "git_version": "2.56.1",
            "binaries": {Path(p).name: hashlib.sha256(elf()).hexdigest() for p in (GIT, HTTP, FETCH)},
            "ca_bundle_sha256": hashlib.sha256((self.stage / CA).read_bytes()).hexdigest(),
        }
        self.save_manifest()

    def put(self, name, data, mode=0o644):
        path = self.stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def link(self, name, target):
        path = self.stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)

    def save_manifest(self):
        self.put(MANIFEST, (json.dumps(self.manifest) + "\n").encode())

    def unchecked_archive(self, change=None):
        """Construct attacks independently of the validating production writer."""
        path = self.root / "unchecked.tar.gz"
        with tarfile.open(path, "w:gz", dereference=False) as archive:
            for source in sorted(self.stage.rglob("*")):
                member = archive.gettarinfo(str(source), source.relative_to(self.stage).as_posix())
                data = source.read_bytes() if member.isfile() else None
                entries = [(member, data)] if change is None else change(member, data)
                for entry, contents in entries:
                    archive.addfile(entry, io.BytesIO(contents) if contents is not None else None)
        return path

    def reject(self, change=None, pattern=None):
        with self.assertRaisesRegex(ValueError, pattern or "."):
            release.verify_archive(self.unchecked_archive(change))

    def test_deterministic_metadata_modes_and_installer_links(self):
        (self.stage / GIT).chmod(0o751)
        first, second = self.root / "first.tar.gz", self.root / "different-name.tar.gz"
        release.write_archive(self.stage, first)
        before = {str(p.relative_to(self.stage)): (p.lstat().st_mode, p.readlink() if p.is_symlink()
                  else p.read_bytes() if p.is_file() else None) for p in self.stage.rglob("*")}
        for path in self.stage.rglob("*"):
            os.utime(path, (12345, 12345), follow_symlinks=False)
        release.write_archive(self.stage, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.read_bytes()[3:8], bytes(5))  # No filename flag; gzip timestamp zero.
        self.assertEqual(release.verify_archive(first), self.manifest)
        after = {str(p.relative_to(self.stage)): (p.lstat().st_mode, p.readlink() if p.is_symlink()
                 else p.read_bytes() if p.is_file() else None) for p in self.stage.rglob("*")}
        self.assertEqual(before, after)
        with tarfile.open(first) as archive:
            for member in archive:
                self.assertFalse(member.name.startswith("./"))
                self.assertEqual((member.uid, member.gid, member.mtime, member.uname, member.gname),
                                 (0, 0, 0, "", ""))
            self.assertEqual(archive.getmember(GIT).mode, 0o751)
            for name, target in ((HTTPS, "git-remote-http"), ("usr/bin/git-add", "git"),
                                 ("usr/libexec/git-core/git-add", "../../bin/git")):
                self.assertTrue(archive.getmember(name).issym())
                self.assertEqual(archive.getmember(name).linkname, target)

    def test_safe_symlink_chain_and_static_pie(self):
        self.link("usr/bin/git-stage", "git-add")
        self.link("usr/bin/git-absolute-parent", "../../usr/libexec/git-core/git-add")
        data = elf(kind=3, extra=((2, 4, 176, 0x4000B0, 0, 16, 16, 8),))
        self.put(GIT, data, 0o755)
        self.manifest["binaries"]["git"] = hashlib.sha256(data).hexdigest()
        self.save_manifest()
        output = self.root / "pie.tar.gz"
        release.write_archive(self.stage, output)
        self.assertEqual(release.verify_archive(output), self.manifest)

    def test_hardlinked_stage_files_are_archived_as_regular_files(self):
        os.link(self.stage / GIT, self.stage / "usr/bin/git-copy")
        output = self.root / "hardlinks.tar.gz"
        release.write_archive(self.stage, output)
        with tarfile.open(output) as archive:
            self.assertTrue(archive.getmember("usr/bin/git-copy").isfile())

    def test_rejects_unsafe_names_duplicates_and_special_members(self):
        for name in ("/usr/bin/git", "./usr/bin/git", "usr/../outside", "usr//bin/git",
                     "usr/./bin/git", "usr/bin/../../etc/escape", "usr\\evil", "bin/git", "bundle.toml", "usr"):
            with self.subTest(name=name):
                def change(member, data):
                    if member.name == GIT:
                        member.name = name
                    return [(member, data)]
                self.reject(change, "unsafe|duplicate")
        self.reject(lambda m, d: [(m, d), (copy.copy(m), d)] if m.name == GIT else [(m, d)], "duplicate")
        for kind in (tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, b"V"):
            with self.subTest(kind=kind):
                def change(member, data):
                    if member.name == "usr/bin/git-add":
                        member.type, member.linkname = kind, GIT
                    return [(member, data)]
                self.reject(change, "unsafe")

    def test_rejects_unsafe_broken_and_cyclic_symlinks(self):
        for target in ("/usr/bin/git", "../../../usr/bin/git", "../../../../etc/passwd", "missing",
                       "git-add", "../bin", "git/child", "git/../git", "missing/../git",
                       "git/", "..\\..\\etc\\passwd", ""):
            with self.subTest(target=target):
                def change(member, data):
                    if member.name == "usr/bin/git-add":
                        member.linkname = target
                    return [(member, data)]
                self.reject(change, "symlink")
        self.link("usr/bin/cycle-a", "cycle-b")
        self.link("usr/bin/cycle-b", "cycle-a")
        self.reject(pattern="cyclic")

    def test_rejects_files_below_symlinks_and_regular_files(self):
        for name in ("usr/bin/git-add/child", "usr/bin/git/child"):
            with self.subTest(name=name):
                def change(member, data):
                    if member.name == LICENSES + "COPYING":
                        member.name = name
                    return [(member, data)]
                self.reject(change, "non-directory archive ancestor")

    def test_rejects_corrupt_elf_even_with_matching_hash(self):
        changes = ((0, b"NOPE"), (4, b"\x01"), (5, b"\x02"), (6, b"\x00"), (7, b"\x00"),
                   (16, struct.pack("<H", 1)), (18, struct.pack("<H", 62)), (20, bytes(4)),
                   (24, bytes(8)), (32, struct.pack("<Q", 2**63)), (52, bytes(2)),
                   (54, bytes(2)), (56, bytes(2)), (56, b"\xff\xff"),
                   (64, struct.pack("<I", 3)), (68, struct.pack("<I", 4)),
                   (72, struct.pack("<Q", 2**63)), (104, bytes(8)))
        invalid = []
        for offset, value in changes:
            data = bytearray(elf())
            data[offset:offset + len(value)] = value
            invalid.append(bytes(data))
        invalid.extend((elf()[:20], elf()[:100], elf()[:-1]))
        data = bytearray(elf(kind=3, extra=((2, 4, 176, 0x4000B0, 0, 16, 16, 8),)))
        struct.pack_into("<qQ", data, 176, 1, 0)  # DT_NEEDED without an interpreter is still dynamic.
        invalid.append(bytes(data))
        for index, data in enumerate(invalid):
            with self.subTest(case=index):
                self.put(GIT, data, 0o755)
                self.manifest["binaries"]["git"] = hashlib.sha256(data).hexdigest()
                self.save_manifest()
                self.reject(pattern="ELF|executable")

    def test_rejects_missing_helpers_nonexecutables_and_hash_mismatches(self):
        for location in (GIT, HTTP, FETCH):
            with self.subTest(location=location):
                self.reject(lambda m, d: [] if m.name == location else [(m, d)])
                def change(member, data):
                    if member.name == location:
                        member.mode = 0o644
                    return [(member, data)]
                self.reject(change, "not executable")
                def corrupt(member, data):
                    if member.name == location:
                        data = data[:-1] + b"!"
                    return [(member, data)]
                self.reject(corrupt, "hash mismatch")
        self.put(CA, b"replaced certificate bundle\n")
        self.reject(pattern="CA bundle hash mismatch")

    def test_https_link_must_be_exact(self):
        for target in ("../../bin/git", "./git-remote-http", "git-http-fetch"):
            with self.subTest(target=target):
                def change(member, data):
                    if member.name == HTTPS:
                        member.linkname = target
                    return [(member, data)]
                self.reject(change, "git-remote-https must symlink")
        self.reject(lambda m, d: [] if m.name == HTTPS else [(m, d)], "git-remote-https")
        def regular_https(member, data):
            if member.name == HTTPS:
                member.type, member.linkname, member.size = tarfile.REGTYPE, "", 1
                data = b"!"
            return [(member, data)]
        self.reject(regular_https, "git-remote-https")

    def test_requires_manifest_evidence_and_real_json_object(self):
        original = copy.deepcopy(self.manifest)
        for key in original:
            with self.subTest(missing=key):
                if key == "git_version":
                    continue  # Informational metadata, not release-integrity evidence.
                self.manifest = copy.deepcopy(original)
                del self.manifest[key]
                self.save_manifest()
                self.reject()
        for key, value in (("source_dirty", True), ("source_dirty", 0), ("source_dirty", "false"),
                           ("architecture", "x86_64"), ("native_abi", "Linux"), ("required_syscall", 110),
                           ("source_revision", "a" * 39), ("required_kernel_revision", "g" * 40),
                           ("source_revision", 40), ("required_kernel_revision", "b" * 41),
                           ("binaries", []), ("binaries", {})):
            with self.subTest(key=key, value=value):
                self.manifest = {**original, key: value}
                self.save_manifest()
                self.reject()
        for data in (b"[]", b"null", b"not json", b"\xff", b"{}", b'{"binaries":{},"binaries":{}}'):
            with self.subTest(json=data):
                self.put(MANIFEST, data)
                self.reject()

    def test_requires_manifest_ca_and_each_license_notice_as_nonempty_regular_files(self):
        for name in (MANIFEST, CA, *(LICENSES + notice for notice in NOTICES)):
            with self.subTest(missing=name):
                self.reject(lambda m, d: [] if m.name == name else [(m, d)])
            with self.subTest(empty=name):
                def empty(member, data):
                    if member.name == name:
                        member.size, data = 0, b""
                    return [(member, data)]
                self.reject(empty)
            with self.subTest(symlink=name):
                def symlink(member, data):
                    if member.name == name:
                        member.type, member.size = tarfile.SYMTYPE, 0
                        member.linkname = "../" * (len(name.split("/")) - 1) + GIT
                        data = None
                    return [(member, data)]
                self.reject(symlink)

    def test_failed_write_preserves_previous_archive_and_cleans_temporaries(self):
        output = self.root / "release.tar.gz"
        release.write_archive(self.stage, output)
        original = output.read_bytes()
        self.put("bundle.toml", b"root metadata does not belong in the rootfs archive")
        with self.assertRaises(ValueError):
            release.write_archive(self.stage, output)
        self.assertEqual(output.read_bytes(), original)
        (self.stage / "bundle.toml").unlink()
        os.mkfifo(self.stage / "usr/bin/fifo")
        with self.assertRaisesRegex(ValueError, "unsupported staging"):
            release.write_archive(self.stage, output)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["release.tar.gz", "stage"])

    def test_cannot_write_inside_staging(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            release.write_archive(self.stage, self.stage / "usr/release.tar.gz")
        self.assertFalse((self.stage / "usr/release.tar.gz").exists())

    def test_truncated_archive_is_rejected(self):
        path = self.unchecked_archive()
        path.write_bytes(gzip.compress(gzip.decompress(path.read_bytes())[:1000]))
        with self.assertRaises(ValueError):
            release.verify_archive(path)


if __name__ == "__main__":
    unittest.main()
