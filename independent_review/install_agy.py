"""Install a reviewed official agy binary without shell setup or auto-updating."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
import urllib.request


def install(destination):
    release = json.loads(Path(__file__).with_name("agy-release.json").read_text())
    machine = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    target = platform.system().lower() + "_" + machine
    if target not in release["platforms"]:
        raise ValueError("Unsupported agy platform")
    asset = release["platforms"][target]
    with urllib.request.urlopen(asset["url"], timeout=90) as response:
        data = response.read(200_000_001)
    if len(data) > 200_000_000 or hashlib.sha512(data).hexdigest() != asset["sha512"]:
        raise ValueError("Official agy release checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("antigravity")
        if not member.isfile() or member.size > 400_000_000:
            raise ValueError("Invalid agy archive")
        binary = archive.extractfile(member).read()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(binary)
    destination.chmod(0o700)
    print("Verified agy", release["version"], target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    install(parser.parse_args().out)
