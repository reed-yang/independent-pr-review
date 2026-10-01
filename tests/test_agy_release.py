import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("agy_release", ROOT / "scripts/agy_release.py")
agy_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agy_release)

QUALIFIED = b"\x90" * 64 + b"\xc7\x00\x00\x40\x00\x00" + b"\x48" * 20 + b"\xc7\x00\x00\xfa\x00\x00" + b"\x90" * 64


def archive(content, name="antigravity"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as bundle:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        bundle.addfile(member, io.BytesIO(content))
    return data.getvalue()


class AgyReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "agy-release.json"
        self.path.write_text((ROOT / "independent_review/agy-release.json").read_text())
        self.pinned = json.loads(self.path.read_text())
        self.publish("9.9.9", {"linux_amd64": archive(QUALIFIED), "darwin_arm64": archive(b"arm64 binary")})

    def publish(self, version, archives, digests=None):
        # Local fixtures stand in for the official manifests and archives.
        self.files = {}
        for platform, data in archives.items():
            url = agy_release.ARCHIVE_PREFIX + version + "-1/" + platform.replace("_", "-") + "/cli_" + platform + ".tar.gz"
            digest = (digests or {}).get(platform, hashlib.sha512(data).hexdigest())
            self.files[agy_release.MANIFEST_URL.format(platform)] = json.dumps(
                {"version": version, "url": url, "sha512": digest}).encode()
            self.files[url] = data

    def fetch(self, url, limit):
        data = self.files[url]
        self.assertLessEqual(len(data), limit)
        return data

    def run_main(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            agy_release.main(list(argv), fetch=self.fetch, path=self.path)
        return json.loads(output.getvalue())

    def test_committed_pin_records_qualified_native_input_cap(self):
        text = (ROOT / "independent_review/agy-release.json").read_text()
        release = json.loads(text)
        self.assertEqual(text, json.dumps(release, indent=2) + "\n")
        self.assertEqual(set(release["platforms"]), set(agy_release.PLATFORMS))
        self.assertEqual(release["native_input"]["max_user_input_step_tokens"], 64000)
        self.assertEqual(release["native_input"]["bytes_per_token"], 3)
        self.assertIn("agy " + release["version"] + "'s default cascade config", release["native_input"]["basis"])

    def test_check_reports_newer_manifest_without_rewriting_pin(self):
        report = self.run_main("check")
        self.assertEqual((report["status"], report["pinned"], report["latest"]),
                         ("update_available", self.pinned["version"], "9.9.9"))
        self.assertEqual(json.loads(self.path.read_text()), self.pinned)

    def test_update_verifies_qualifies_and_rewrites_pin(self):
        report = self.run_main("update", "--version", "9.9.9")
        self.assertEqual(report["status"], "updated")
        self.assertEqual(report["native_step_cap_matches"], 1)
        release = json.loads(self.path.read_text())
        self.assertEqual(self.path.read_text(), json.dumps(release, indent=2) + "\n")
        self.assertEqual(release["version"], "9.9.9")
        for platform in agy_release.PLATFORMS:
            manifest = json.loads(self.files[agy_release.MANIFEST_URL.format(platform)])
            self.assertEqual(release["platforms"][platform], {"url": manifest["url"], "sha512": manifest["sha512"]})
        self.assertEqual(release["native_input"], {
            "max_user_input_step_tokens": 64000, "bytes_per_token": 3,
            "basis": "Static check: agy 9.9.9's default cascade config sets MaxTokensPerUserInputStep to 64000 (0xfa00)."})
        self.assertEqual(self.run_main("check")["status"], "current")
        self.assertEqual(self.run_main("update")["status"], "current")

    def test_unqualified_step_cap_or_archive_never_rewrites_pin(self):
        arm = archive(b"arm64 binary")
        cases = [
            ("native_step_cap_unqualified", {"linux_amd64": archive(b"\xc7\x00\x00\xfa\x00\x00" * 4), "darwin_arm64": arm}, None),
            ("release_checksum_mismatch", {"linux_amd64": archive(QUALIFIED), "darwin_arm64": arm}, {"darwin_arm64": "0" * 128}),
            ("invalid_release_archive", {"linux_amd64": archive(QUALIFIED, "other"), "darwin_arm64": arm}, None),
            ("invalid_release_archive", {"linux_amd64": b"not a tarball", "darwin_arm64": arm}, None),
        ]
        for error, archives, digests in cases:
            with self.subTest(error=error):
                self.publish("9.9.9", archives, digests)
                with self.assertRaises(SystemExit) as failure:
                    self.run_main("update")
                self.assertEqual(str(failure.exception.code), error)
                self.assertEqual(json.loads(self.path.read_text()), self.pinned)

    def test_manifest_must_be_consistent_and_newer(self):
        self.publish("9.9.9", {"linux_amd64": archive(QUALIFIED), "darwin_arm64": archive(b"arm")})
        with self.assertRaises(SystemExit) as failure:
            self.run_main("update", "--version", "9.9.8")
        self.assertEqual(failure.exception.code, "release_manifest_version_changed")
        self.files[agy_release.MANIFEST_URL.format("darwin_arm64")] = json.dumps(
            {"version": "9.9.8", "url": agy_release.ARCHIVE_PREFIX + "9.9.8-1/darwin-arm/cli.tar.gz", "sha512": "a" * 128}).encode()
        with self.assertRaises(SystemExit) as failure:
            self.run_main("check")
        self.assertEqual(failure.exception.code, "release_manifest_versions_differ")
        for manifest in [b"[]", b"{}", json.dumps({"version": "9.9.9", "url": "https://example.com/9.9.9-1/a/cli.tar.gz",
                                                    "sha512": "a" * 128}).encode(),
                         json.dumps({"version": "9.9.9", "url": agy_release.ARCHIVE_PREFIX + "9.9.9-1/a/cli.tar.gz",
                                     "sha512": "A" * 128}).encode()]:
            with self.subTest(manifest=manifest):
                self.files[agy_release.MANIFEST_URL.format("linux_amd64")] = manifest
                with self.assertRaises(SystemExit) as failure:
                    self.run_main("check")
                self.assertEqual(failure.exception.code, "invalid_release_manifest")
        # Same version with different bytes, or an older manifest, needs a human.
        for published in [self.pinned["version"], "0.0.0"]:
            with self.subTest(published=published):
                self.publish(published, {"linux_amd64": archive(QUALIFIED), "darwin_arm64": archive(b"arm")})
                with self.assertRaises(SystemExit) as failure:
                    self.run_main("check")
                self.assertEqual(failure.exception.code, 1)
                with self.assertRaises(SystemExit) as failure:
                    self.run_main("update")
                self.assertEqual(failure.exception.code, "release_manifest_not_newer")
        self.assertEqual(json.loads(self.path.read_text()), self.pinned)

    def test_network_reads_are_https_and_size_bounded(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        with patch.object(agy_release.urllib.request, "urlopen", return_value=Response(b"x" * 11)):
            self.assertEqual(agy_release.fetch("https://example.com/a", 11), b"x" * 11)
        with patch.object(agy_release.urllib.request, "urlopen", return_value=Response(b"x" * 11)):
            with self.assertRaises(SystemExit) as failure:
                agy_release.fetch("https://example.com/a", 10)
        self.assertEqual(failure.exception.code, "release_download_too_large")
        with patch.object(agy_release.urllib.request, "urlopen", side_effect=agy_release.urllib.error.URLError("private")):
            with self.assertRaises(SystemExit) as failure:
                agy_release.fetch("https://example.com/a", 10)
        self.assertEqual(failure.exception.code, "release_download_failed")
        with patch.object(agy_release.urllib.request, "urlopen") as opened:
            with self.assertRaises(SystemExit) as failure:
                agy_release.fetch("http://example.com/a", 10)
            opened.assert_not_called()
        self.assertEqual(failure.exception.code, "release_url_not_https")


if __name__ == "__main__":
    unittest.main()
