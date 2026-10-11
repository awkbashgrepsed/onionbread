#!/usr/bin/env python3
"""Fetch the pinned Windows VPN dependencies for Onionbread.

Run from the repository root:
    python tools/fetch_vpn_runtime.py

The tun2socks release is selected by tag and its GitHub asset digest is
verified when GitHub publishes one. Wintun is downloaded from its official
project site. Review upstream licenses and release notes before distribution.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VPN_DIR = REPO_ROOT / "runtime" / "vpn"
TUN2SOCKS_TAG = "v2.7.0"
TUN2SOCKS_API = (
    "https://api.github.com/repos/xjasonlyu/tun2socks/releases/tags/"
    + TUN2SOCKS_TAG
)
WINTUN_URL = "https://www.wintun.net/builds/wintun-0.14.1.zip"


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Onionbread-VPN-runtime-fetcher",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=128 * 1024)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    VPN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="onionbread-vpn-") as temp_name:
        temp = Path(temp_name)
        api_request = urllib.request.Request(
            TUN2SOCKS_API,
            headers={
                "User-Agent": "Onionbread-VPN-runtime-fetcher",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(api_request, timeout=30) as response:
            release = json.load(response)

        assets = release.get("assets", [])
        asset = next(
            (
                item for item in assets
                if item.get("name") == "tun2socks-windows-amd64.zip"
            ),
            None,
        )
        if asset is None:
            available = ", ".join(
                item.get("name", "?") for item in assets
                if "windows" in item.get("name", "").lower()
            )
            raise SystemExit(
                "Could not find tun2socks-windows-amd64.zip in "
                f"{TUN2SOCKS_TAG}. Windows assets found: {available or 'none'}"
            )

        tun_archive = temp / asset["name"]
        print(f"Downloading tun2socks {TUN2SOCKS_TAG}...")
        download(asset["browser_download_url"], tun_archive)
        expected = asset.get("digest") or ""
        if expected.startswith("sha256:"):
            actual = sha256(tun_archive)
            if actual.lower() != expected.split(":", 1)[1].lower():
                raise SystemExit("tun2socks SHA-256 mismatch; refusing to stage it.")
            print(f"Verified tun2socks SHA-256: {actual}")
        else:
            print("GitHub did not publish an asset digest; downloaded file was not hash-verified.")

        wintun_archive = temp / "wintun.zip"
        print("Downloading Wintun from the official project site...")
        download(WINTUN_URL, wintun_archive)

        with zipfile.ZipFile(tun_archive) as archive:
            candidates = [
                name for name in archive.namelist()
                if Path(name).name.lower() in ("tun2socks.exe", "tun2socks-windows-amd64.exe")
            ]
            if not candidates:
                raise SystemExit("The tun2socks archive did not contain a Windows executable.")
            tun_member = candidates[0]
            tun_bytes = archive.read(tun_member)

        with zipfile.ZipFile(wintun_archive) as archive:
            wintun_member = next(
                (
                    name for name in archive.namelist()
                    if name.replace("\\", "/").lower().endswith("/bin/amd64/wintun.dll")
                ),
                None,
            )
            if wintun_member is None:
                raise SystemExit("The Wintun archive did not contain bin/amd64/wintun.dll.")
            wintun_bytes = archive.read(wintun_member)

        # Only extract the two expected files; do not unpack arbitrary archive paths.
        (VPN_DIR / "tun2socks.exe").write_bytes(tun_bytes)
        (VPN_DIR / "wintun.dll").write_bytes(wintun_bytes)
        (VPN_DIR / "VPN_RUNTIME_VERSIONS.txt").write_text(
            f"tun2socks {TUN2SOCKS_TAG}\n"
            f"tun2socks source: https://github.com/xjasonlyu/tun2socks/releases/tag/{TUN2SOCKS_TAG}\n"
            f"Wintun source: {WINTUN_URL}\n",
            encoding="utf-8",
        )

    print(f"VPN runtime staged at: {VPN_DIR}")
    print("Review upstream licenses before redistributing these binaries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
