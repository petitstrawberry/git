#!/usr/bin/env python3
"""Host-only checks of evidence handling and read-only rootfs packaging."""
import importlib.util
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("git_scarlet_runner", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
spec = importlib.util.spec_from_file_location("git_scarlet_ext2_runner", Path(__file__).with_name("ext2-run.py"))
ext2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ext2)


class MarkerTests(unittest.TestCase):
    def test_all_cases_required(self):
        markers = runner.Markers()
        markers.line(f"SCARLET_GIT_SMOKE PASS cases={len(runner.CASES)}".encode())
        self.assertEqual(markers.result, "incomplete case markers")

    def test_duplicate_case_rejected(self):
        markers = runner.Markers()
        for _ in range(2):
            markers.line(f"SCARLET_GIT_CASE {runner.CASES[0]} PASS".encode())
        self.assertEqual(markers.result, "unexpected or duplicate case marker")

    def test_full_sequence(self):
        markers = runner.Markers()
        for case in runner.CASES:
            markers.line(f"SCARLET_GIT_CASE {case} PASS\r\n".encode())
        markers.line(f"SCARLET_GIT_SMOKE PASS cases={len(runner.CASES)}".encode())
        self.assertEqual(markers.result, "PASS")

    def test_embedded_child_marker_ignored_and_failure_sticky(self):
        markers = runner.Markers()
        markers.line(b'SCARLET_GIT_EXIT 1 code=Some(0) stdout="SCARLET_GIT_SMOKE PASS cases=15\\n"')
        self.assertIsNone(markers.result)
        markers.line(b"SCARLET_GIT_SMOKE FAIL unavailable syscall")
        for case in runner.CASES:
            markers.line(f"SCARLET_GIT_CASE {case} PASS".encode())
        markers.line(f"SCARLET_GIT_SMOKE PASS cases={len(runner.CASES)}".encode())
        self.assertIn("unavailable syscall", markers.result)


class ArchiveTests(unittest.TestCase):
    def test_deterministic_modes_symlinks_and_unchanged_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            (staging / "bin").mkdir(parents=True)
            program = staging / "bin/program"
            program.write_bytes(b"fixture\n")
            program.chmod(0o751)
            (staging / "bin/link").symlink_to("program")
            before = program.stat()
            runner.create_initramfs(staging, root / "one.cpio")
            os.utime(program, (100, 100))
            runner.create_initramfs(staging, root / "two.cpio")
            self.assertEqual((root / "one.cpio").read_bytes(), (root / "two.cpio").read_bytes())
            self.assertEqual(program.read_bytes(), b"fixture\n")
            self.assertEqual(program.stat().st_mode, before.st_mode)
            data, position, entries = (root / "one.cpio").read_bytes(), 0, {}
            while True:
                self.assertEqual(data[position:position + 6], b"070701")
                fields = [int(data[position + 6 + i * 8:position + 14 + i * 8], 16) for i in range(13)]
                start = position + 110
                name = data[start:start + fields[11] - 1].decode()
                start = (start + fields[11] + 3) & ~3
                payload = data[start:start + fields[6]]
                position = (start + fields[6] + 3) & ~3
                if name == "TRAILER!!!":
                    break
                self.assertEqual(fields[2:4], [0, 0])
                self.assertEqual(fields[5], 0)
                entries[name] = (fields[1], payload)
            self.assertEqual(entries["bin/program"], (0o100751, b"fixture\n"))
            self.assertEqual(entries["bin/link"], ((staging / "bin/link").lstat().st_mode, b"program"))

    def test_foreign_elf_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "program"
            data = bytearray(64)
            data[:7] = b"\x7fELF\x02\x01\x01"
            data[16:20] = b"\x02\x00\xb7\x00"
            binary.write_bytes(data)
            with self.assertRaises(ValueError):
                runner.native_elf(binary, "aarch64")
            data[7] = 83
            binary.write_bytes(data)
            runner.native_elf(binary, "aarch64")
            with self.assertRaises(ValueError):
                runner.native_elf(binary, "riscv64")


class Ext2Tests(unittest.TestCase):
    def fixture(self):
        # Real host subprocesses below are controlled checkers, not e2fsck or
        # guest acceptance. They verify the host's fail-closed evidence gate.
        return ext2.Ext2Fixture(SimpleNamespace(
            mke2fs=sys.executable, e2fsck=sys.executable, disk_mib=32))

    def test_core_pass_does_not_skip_remount_or_final_unmount(self):
        for count in (len(runner.CASES) + 1, len(ext2.Ext2Fixture.cases) - 1,
                      len(ext2.Ext2Fixture.cases)):
            with self.subTest(cases=count):
                markers = runner.Markers(ext2.Ext2Fixture.cases)
                for case in ext2.Ext2Fixture.cases[:count]:
                    markers.line(f"SCARLET_GIT_CASE {case} PASS".encode())
                markers.line(f"SCARLET_GIT_SMOKE PASS cases={len(ext2.Ext2Fixture.cases)}".encode())
                self.assertEqual(markers.result == "PASS", count == len(ext2.Ext2Fixture.cases))

    def test_host_fsck_and_guest_must_both_pass(self):
        for guest, code in (("PASS", 0), ("PASS", 1), ("PASS", 4), ("PASS", 8),
                            ("guest timeout", 0)):
            with self.subTest(guest=guest, checker_exit=code), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                disk = output / "git-data.ext2"
                disk.write_bytes(b"host test fixture; not an ext2 image")
                fixture = self.fixture()
                fixture.manifest = {
                    "disk": str(disk),
                    "e2fsck": [sys.executable, "-c",
                               f"import sys; print('controlled checker'); sys.exit({code})"],
                }
                with contextlib.redirect_stdout(io.StringIO()):
                    result = fixture.finish({"result": guest}, None, output)
                self.assertEqual(result["result"] == "PASS", guest == "PASS" and code == 0)
                self.assertEqual(result["guest_result"], guest)
                self.assertEqual(result["host_e2fsck_exit_code"], code)
                self.assertIn("controlled checker", (output / "e2fsck-after.log").read_text())

    def test_existing_disk_is_never_formatted(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            disk = output / "git-data.ext2"
            disk.write_bytes(b"existing data")
            with self.assertRaises(FileExistsError):
                self.fixture().prepare(None, output / "staging", output)
            self.assertEqual(disk.read_bytes(), b"existing data")


class MonitorTests(unittest.TestCase):
    def test_clean_qemu_exit_without_markers_is_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = runner.run_guest([sys.executable, "-c", "pass"], Path(temporary), 3)
            self.assertEqual(result["qemu_exit_code"], 0)
            self.assertNotEqual(result["result"], "PASS")

    def test_timeout_stops_only_the_launched_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = runner.run_guest([sys.executable, "-c", "import time; time.sleep(30)"], Path(temporary), 0.1)
            self.assertEqual(result["result"], "guest timeout")
            self.assertLess(result["elapsed_seconds"], 6)


if __name__ == "__main__":
    unittest.main()
