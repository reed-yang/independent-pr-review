import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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


FAKE_GH = """#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
option = lambda name: args[args.index(name) + 1] if name in args else None
prs = json.load(open(os.environ["FAKE_PRS"]))
if args[:2] == ["pr", "list"]:
    data = [pr for pr in prs if pr["state"] == option("--state")]
elif args[:2] == ["pr", "view"]:
    data = next(pr for pr in prs if str(pr["number"]) == args[2])
else:
    sys.exit(0)
fields = option("--json").split(",")
project = lambda pr: {{key: pr[key] for key in fields}}
data = [project(pr) for pr in data] if isinstance(data, list) else project(data)
# The real jq evaluates the workflow's --jq expression, as gh does.
sys.stdout.write(subprocess.run(["jq", "-r", option("--jq")], input=json.dumps(data),
                                capture_output=True, text=True, check=True).stdout)
"""

FAKE_GIT = """#!{python}
import os, sys
args = sys.argv[1:]
while args[:1] == ["-c"]:
    args = args[2:]
tip = os.environ["FAKE_TIP"]
if args[0] == "ls-remote":
    if not os.path.exists(tip):
        sys.exit(2)
    ref = args[-1] if args[-1].startswith("refs/") else "refs/heads/" + args[-1]
    print(open(tip).read().strip() + "\\t" + ref)
elif args[0] == "push":
    open(tip, "w").write("1" * 40)
"""


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "jq and bash are required")
class ReleaseWatchWorkflowTests(unittest.TestCase):
    def step_script(self, name):
        lines = (ROOT / ".github/workflows/agy-release-watch.yml").read_text().splitlines()
        start = lines.index("      - name: " + name)
        run = next(index for index in range(start, len(lines)) if lines[index].strip() == "run: |")
        indent = len(lines[run]) - len(lines[run].lstrip()) + 2
        body = []
        for line in lines[run + 1:]:
            if line.strip() and len(line) - len(line.lstrip()) < indent:
                break
            body.append(line[indent:])
        return "\n".join(body) + "\n"

    def run_step(self, prs, branch_tip=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        temp = Path(directory.name)
        bin_dir = temp / "bin"
        bin_dir.mkdir()
        for name, source in (("gh", FAKE_GH), ("git", FAKE_GIT), ("base64", "#!/bin/sh\ncat >/dev/null\necho Zml4dHVyZQ==\n")):
            (bin_dir / name).write_text(source.format(python=sys.executable) if name != "base64" else source)
            (bin_dir / name).chmod(0o755)
        (temp / "prs.json").write_text(json.dumps(prs))
        if branch_tip:
            (temp / "tip").write_text(branch_tip)
        (temp / "agy-update.json").write_text(json.dumps({
            "latest": "9.9.9", "pinned": "1.2.14", "native_step_cap_matches": 1,
            "platforms": {"linux_amd64": {"version": "9.9.9", "sha512": "0" * 128, "archive_bytes": 1}}}))
        (temp / "agy-tests.log").write_text("Ran 1 test in 0.1s\n\nOK\n")
        (temp / "step.sh").write_text(self.step_script("Create or update the release PR"))
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "RUNNER_TEMP": str(temp),
               "GH_TOKEN": "fixture", "GH_REPO": "owner/engine", "AGY_VERSION": "9.9.9", "BASE_BRANCH": "main",
               "RUN_URL": "https://github.com/owner/engine/actions/runs/1", "GITHUB_SERVER_URL": "https://github.com",
               "GITHUB_SHA": "a" * 40, "FAKE_LOG": str(temp / "calls.log"), "FAKE_PRS": str(temp / "prs.json"),
               "FAKE_TIP": str(temp / "tip")}
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(temp / "step.sh")],
                                env=env, capture_output=True, text=True, timeout=30)
        log = temp / "calls.log"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, [call[:3] for call in calls if call[:2] in (["pr", "create"], ["pr", "edit"])]

    def test_release_pr_lookup_ignores_fork_heads_and_stale_pr_heads(self):
        fork = {"isCrossRepository": True, "headRefOid": "f" * 40}
        own = {"isCrossRepository": False}
        cases = [
            # A closed fork PR with the same branch name must not suppress the official PR.
            ("closed_fork", [{"number": 5, "state": "closed", **fork}], None, 0, [["pr", "create", "--base"]]),
            # An open fork PR must never receive the bot's evidence body.
            ("open_fork", [{"number": 6, "state": "open", **fork}], None, 0, [["pr", "create", "--base"]]),
            ("stale_head", [{"number": 7, "state": "open", "headRefOid": "e" * 40, **own}], "1" * 40, 1, []),
            ("current_head", [{"number": 7, "state": "open", "headRefOid": "1" * 40, **own}], "1" * 40, 0, [["pr", "edit", "7"]]),
        ]
        for name, prs, tip, code, writes in cases:
            with self.subTest(case=name):
                result, calls = self.run_step(prs, tip)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(calls, writes)


if __name__ == "__main__":
    unittest.main()
