"""Compare or update the pinned official agy release against Google's manifests."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import urllib.request


MANIFEST_URL = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/{}.json"
ARCHIVE_PREFIX = "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
PLATFORMS = ("linux_amd64", "darwin_arm64")
RELEASE = Path(__file__).resolve().parent.parent / "independent_review/agy-release.json"
STEP_CAP_TOKENS = 64000
# x86-64 stores in agy's default conversational cascade config: 0x400000, then
# MaxTokensPerUserInputStep = 0xfa00. A miss needs human qualification.
STEP_CAP = re.compile(rb"\xc7\x00\x00\x40\x00\x00.{14,40}?\xc7\x00\x00\xfa\x00\x00", re.S)


def fetch(url, limit):
    """Read an HTTPS resource, rejecting bodies larger than limit bytes."""
    if not url.startswith("https://"):
        raise SystemExit("release_url_not_https")
    try:
        with urllib.request.urlopen(url, timeout=90) as response:
            data = response.read(limit + 1)
    except OSError:
        raise SystemExit("release_download_failed") from None
    if len(data) > limit:
        raise SystemExit("release_download_too_large")
    return data


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def manifests(fetch=fetch):
    found = {}
    for platform in PLATFORMS:
        try:
            manifest = json.loads(fetch(MANIFEST_URL.format(platform), 65536))
            version, url, sha512 = manifest["version"], manifest["url"], manifest["sha512"]
        except (ValueError, TypeError, KeyError):
            raise SystemExit("invalid_release_manifest") from None
        if not (isinstance(version, str) and re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version)
                and isinstance(url, str) and re.fullmatch(re.escape(ARCHIVE_PREFIX + version + "-") +
                                                          r"\d+/[a-z0-9-]+/[a-z0-9_]+\.tar\.gz", url)
                and isinstance(sha512, str) and re.fullmatch(r"[0-9a-f]{128}", sha512)):
            raise SystemExit("invalid_release_manifest")
        found[platform] = {"version": version, "url": url, "sha512": sha512}
    if len({item["version"] for item in found.values()}) != 1:
        raise SystemExit("release_manifest_versions_differ")
    return found


def compare(release, found):
    pinned, latest = release["version"], found[PLATFORMS[0]]["version"]
    same = all(release["platforms"].get(platform, {}).get(key) == found[platform][key]
               for platform in PLATFORMS for key in ("url", "sha512"))
    if version_key(latest) > version_key(pinned):
        status = "update_available"
    elif latest == pinned and same:
        status = "current"
    else:
        # A republished or withdrawn version needs a human decision.
        status = "manifest_differs"
    return {"status": status, "pinned": pinned, "latest": latest, "platforms": found}


def binary(archive):
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            member = bundle.getmember("antigravity")
            if not member.isfile() or member.size > 400_000_000:
                raise SystemExit("invalid_release_archive")
            return bundle.extractfile(member).read()
    except (KeyError, tarfile.TarError, OSError, EOFError):
        raise SystemExit("invalid_release_archive") from None


def update(path=RELEASE, version="auto", fetch=fetch):
    """Rewrite the pin only after checksum and native step-cap qualification."""
    release = json.loads(path.read_text())
    report = compare(release, manifests(fetch))
    if version not in ("auto", report["latest"]):
        raise SystemExit("release_manifest_version_changed")
    if report["status"] == "current":
        return report
    if report["status"] != "update_available":
        raise SystemExit("release_manifest_not_newer")
    for platform in PLATFORMS:
        asset = report["platforms"][platform]
        archive = fetch(asset["url"], 200_000_000)
        if hashlib.sha512(archive).hexdigest() != asset["sha512"]:
            raise SystemExit("release_checksum_mismatch")
        asset["archive_bytes"] = len(archive)
        executable = binary(archive)
        if platform == "linux_amd64":
            report["native_step_cap_matches"] = len(STEP_CAP.findall(executable))
    if not report["native_step_cap_matches"]:
        raise SystemExit("native_step_cap_unqualified")
    latest = report["latest"]
    release["version"] = latest
    for platform in PLATFORMS:
        release["platforms"][platform] = {key: report["platforms"][platform][key] for key in ("url", "sha512")}
    release["native_input"] = {
        "max_user_input_step_tokens": STEP_CAP_TOKENS, "bytes_per_token": 3,
        "basis": f"Static check: agy {latest}'s default cascade config sets MaxTokensPerUserInputStep to 64000 (0xfa00)."}
    path.write_text(json.dumps(release, indent=2) + "\n")
    report["status"] = "updated"
    return report


def main(argv=None, fetch=fetch, path=RELEASE):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="compare the pin with the official manifests")
    updating = commands.add_parser("update", help="download, verify, qualify and rewrite the pin")
    updating.add_argument("--version", default="auto", help="expected manifest version, or auto")
    args = parser.parse_args(argv)
    if args.command == "check":
        report = compare(json.loads(path.read_text()), manifests(fetch))
    else:
        report = update(path, args.version, fetch)
    print(json.dumps(report, indent=2))
    if report["status"] == "manifest_differs":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
