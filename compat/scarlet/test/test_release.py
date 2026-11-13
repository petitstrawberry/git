"""Release-level consistency checks in addition to the native archive tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.metadata = {
            "schema": 1, "version": "scarlet-v2.56.1-test", "repository": release.URL,
            "source_revision": "a" * 40, "required_kernel_revision": "b" * 40,
            "git_version": "2.56.1", "architecture": "aarch64", "binaries": {},
            "build_inputs_id": "c" * 64,
        }
        for key, filename in (("archive", "git-scarlet-aarch64-"), ("source_archive", "git-scarlet-source-")):
            self.metadata[key] = filename + self.metadata["version"] + ".tar.gz"
            (self.output / self.metadata[key]).write_bytes(key.encode())
        self.metadata["archive_sha256"] = release.sha256(self.output / self.metadata["archive"])
        self.metadata["source_sha256"] = release.sha256(self.output / self.metadata["source_archive"])
        (self.output / "kernel-prerequisites.patch").write_text("kernel patch\n")
        self.write_metadata()
        # Native ELF/archive corruption is independently covered in
        # test_release_archive; these fixtures isolate cross-file binding.
        mock = patch.object(release, "verify_archive", return_value=self.metadata.copy())
        mock.start()
        self.addCleanup(mock.stop)

    def sums(self):
        (self.output / "SHA256SUMS").write_text("".join(
            f"{release.sha256(p)}  {p.name}\n" for p in sorted(self.output.iterdir())
            if p.name != "SHA256SUMS"))

    def write_metadata(self):
        (self.output / "release.json").write_text(json.dumps(self.metadata))
        (self.output / "bundle.toml").write_text(release.bundle_text(self.metadata))
        (self.output / "README.md").write_text(release.release_notes(self.metadata))
        self.sums()

    def test_valid_release(self):
        self.assertEqual(release.verify_release(self.output), self.metadata)

    def test_corrupt_uploaded_bytes(self):
        (self.output / self.metadata["archive"]).write_bytes(b"truncated")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            release.verify_release(self.output)

    def test_bundle_cannot_pin_another_asset_even_with_fresh_sums(self):
        path = self.output / "bundle.toml"
        path.write_text(path.read_text().replace("sha256:" + self.metadata["archive_sha256"], "sha256:" + "0" * 64))
        self.sums()
        with self.assertRaisesRegex(ValueError, "Bundle does not pin"):
            release.verify_release(self.output)

    def test_source_revision_must_match_binary_manifest(self):
        self.metadata["source_revision"] = "d" * 40
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "metadata disagree: source_revision"):
            release.verify_release(self.output)

    def test_checksum_cannot_skip_a_file(self):
        path = self.output / "SHA256SUMS"
        path.write_text("\n".join(path.read_text().splitlines()[1:]) + "\n")
        with self.assertRaisesRegex(ValueError, "unexpected file set"):
            release.verify_release(self.output)

    def test_duplicate_checksum_entry(self):
        path = self.output / "SHA256SUMS"
        path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
        with self.assertRaisesRegex(ValueError, "duplicate checksum"):
            release.verify_release(self.output)

    def test_no_unlisted_extra_asset(self):
        (self.output / "extra").write_text("extra")
        with self.assertRaisesRegex(ValueError, "Unexpected or missing"):
            release.verify_release(self.output)

    def test_version_cannot_escape_path_or_collide_with_upstream(self):
        for value in ("../v1", "v2.56.1", "scarlet-", "scarlet-test/asset", "scarlet-test\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.valid_version(value)

    def test_only_aarch64_has_a_checksum(self):
        import tomllib
        layer, = tomllib.loads(release.bundle_text(self.metadata))["layers"]
        self.assertEqual(layer["sha256"], {"aarch64": "sha256:" + self.metadata["archive_sha256"]})
        self.assertIn("{arch}", layer["url"])


if __name__ == "__main__":
    unittest.main()
