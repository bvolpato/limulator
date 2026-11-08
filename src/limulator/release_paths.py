"""Select the pinned public VPhone release directory."""

from __future__ import annotations

import json
import re
from pathlib import Path

_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[a-fA-F0-9]{64}")


def release_pin(lock_path: Path | None = None) -> tuple[str, str]:
    """Return the public release version and ZIP content hash from the backend lock."""
    if lock_path is None:
        packaged_lock = Path(__file__).with_name("third_party.lock.json")
        lock_path = (
            packaged_lock
            if packaged_lock.is_file()
            else Path(__file__).resolve().parents[2] / "third_party.lock.json"
        )
    lock = json.loads(lock_path.read_text())
    release = lock.get("release")
    version = release.get("version") if isinstance(release, dict) else None
    sha256 = release.get("sha256") if isinstance(release, dict) else None
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError(f"Invalid release version in {lock_path}")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise ValueError(f"Invalid release SHA-256 in {lock_path}")
    return version, sha256.lower()


def release_root(data_home: Path, release_base: Path | str | None = None) -> Path:
    """Return a version and content-addressed directory under the configured release base."""
    version, sha256 = release_pin()
    base = Path(release_base).expanduser() if release_base is not None else data_home / "releases/vphone"
    return base / f"{version}-{sha256}"
