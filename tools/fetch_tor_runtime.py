#!/usr/bin/env python3
"""Fetch and stage the official Tor Expert Bundle for Onionbread.

Windows x86_64 packaging helper. Run from the repository root:
    python tools/fetch_tor_runtime.py
Optional:
    python tools/fetch_tor_runtime.py --version 15.0.24
"""
from __future__ import annotations

import argparse
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "runtime" / "tor"
DEFAULT_VERSION = "15.0.24"
BASE_URL = "https://dist.torproject.org/torbrowser"


def download_with_progress(url: str, destination: Path) -> None:
    """Download a file while displaying a terminal progress bar."""
    request = urllib.request.Request(url, headers={"User-Agent": "Onionbread-runtime-fetcher"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length", "0") or 0)
        downloaded = 0
        bar_width = 32
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 128)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    fraction = min(downloaded / total, 1.0)
                    filled = int(bar_width * fraction)
                    bar = "#" * filled + "-" * (bar_width - filled)
                    percent = int(fraction * 100)
                    print(
                        f"\\rDownloading [{bar}] {percent:3d}% "
                        f"({downloaded / (1024 * 1024):.1f}/"
                        f"{total / (1024 * 1024):.1f} MiB)",
                        end="",
                        flush=True,
                    )
                else:
                    print(
                        f"\\rDownloading... {downloaded / (1024 * 1024):.1f} MiB",
                        end="",
                        flush=True,
                    )
    print("\\nDownload complete.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION, help="Tor Browser bundle version (default: %(default)s)")
    args = parser.parse_args()

    version = args.version.strip()
    if not version or any(ch not in "0123456789.aA-" for ch in version):
        parser.error("version may contain only numbers, dots, letters, and hyphens")

    filename = f"tor-expert-bundle-windows-x86_64-{version}.tar.gz"
    url = f"{BASE_URL}/{version}/{filename}"
    print(f"Downloading official Tor Expert Bundle: {url}")

    with tempfile.TemporaryDirectory(prefix="onionbread-runtime-") as temp_name:
        temp = Path(temp_name)
        archive = temp / filename
        download_with_progress(url, archive)

        print("Extracting Tor Expert Bundle...")
        unpacked = temp / "unpacked"
        unpacked.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            # Python's data filter blocks path traversal and unsafe links.
            bundle.extractall(unpacked, filter="data")

        matches = [
            p for p in unpacked.rglob("tor.exe")
            if (p.parent / "pluggable_transports" / "lyrebird.exe").is_file()
        ]
        if not matches:
            raise SystemExit(
                "The downloaded archive did not contain tor.exe and "
                "pluggable_transports/lyrebird.exe in the expected layout."
            )

        source_root = matches[0].parent
        if RUNTIME_DIR.exists():
            shutil.rmtree(RUNTIME_DIR)
        RUNTIME_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, RUNTIME_DIR)
        (RUNTIME_DIR / "TOR_EXPERT_BUNDLE_VERSION.txt").write_text(
            f"Tor Expert Bundle {version}\nSource: {url}\n",
            encoding="utf-8",
        )

    print(f"Runtime staged at: {RUNTIME_DIR}")
    print("Check the Tor Project signature before distributing a release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
