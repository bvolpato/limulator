#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bind backend ROMs and guest resources to the locked public release archive."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

RECEIPT = ".limulator-release.json"
HOST_CREATION_LOCK = Path("/run/lock/limulator-create.lock")
_CREATE_HOST_LOCK = (
    "import os,sys\n"
    "try:\n"
    " fd=os.open(sys.argv[1],os.O_CREAT|os.O_EXCL|os.O_RDONLY|os.O_NOFOLLOW,0o644)\n"
    "except FileExistsError: pass\n"
    "else: os.close(fd)\n"
)


class ReleaseError(RuntimeError):
    pass


def lock_transport_file(path: Path, owner: int) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise RuntimeError(f"shared restore lock has unsafe ownership or permissions: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another guest creation owns the shared restore transport") from error
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _private_data_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        parent = path.parent.stat()
    except OSError as error:
        raise ReleaseError(f"Runtime directory parent is unavailable: {path.parent}") from error
    _require(stat.S_ISDIR(parent.st_mode), f"Runtime directory parent is not a directory: {path.parent}")
    _require(
        not (parent.st_mode & 0o022) or parent.st_mode & stat.S_ISVTX,
        f"Runtime directory parent permits unsafe replacement: {path.parent}",
    )
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        _require(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(),
            f"Runtime directory is not owned by this user: {path}",
        )
        entry = path.lstat()
        _require(
            (info.st_dev, info.st_ino) == (entry.st_dev, entry.st_ino),
            f"Runtime directory changed while opening: {path}",
        )
        os.fchmod(descriptor, 0o700)
        info = os.fstat(descriptor)
        entry = path.lstat()
        _require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700
            and (info.st_dev, info.st_ino) == (entry.st_dev, entry.st_ino),
            f"Runtime directory changed while securing it: {path}",
        )
    finally:
        os.close(descriptor)


def _lock_data_creation_file(path: Path, owner: int) -> int:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1,
            f"Creation lock has unsafe type or ownership: {path}",
        )
        entry = path.lstat()
        _require(
            (info.st_dev, info.st_ino) == (entry.st_dev, entry.st_ino),
            f"Creation lock changed while opening: {path}",
        )
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        entry = path.lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == owner
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600
            and (info.st_dev, info.st_ino) == (entry.st_dev, entry.st_ino),
            f"Creation lock changed while securing it: {path}",
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another guest creation owns the shared restore transport") from error
        entry = path.lstat()
        _require(
            (info.st_dev, info.st_ino) == (entry.st_dev, entry.st_ino),
            f"Creation lock changed while locking: {path}",
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def host_creation_lock(path: Path, initialize) -> int:
    try:
        return lock_transport_file(path, 0)
    except FileNotFoundError:
        initialize(["/usr/bin/python3", "-c", _CREATE_HOST_LOCK, str(path)])
        return lock_transport_file(path, 0)


def _require(condition, message):
    if not condition:
        raise ReleaseError(
            f"{message}. Resources were preserved; run limulator bootstrap release. "
            "If a complete cache is rejected, stop its users and move that SHA-256 cohort "
            "aside for inspection before bootstrapping again"
        )


def release_spec(lock: Path | None = None) -> dict:
    if lock is None:
        source = Path(__file__).resolve().parents[2] / "third_party.lock.json"
        lock = source if source.is_file() else Path(__file__).with_name("third_party.lock.json")
    value = json.loads(lock.read_text())["release"]
    _require(
        isinstance(value.get("version"), str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value["version"]),
        "Invalid release version",
    )
    _require(
        isinstance(value.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]),
        "Invalid release archive SHA-256",
    )
    _require(isinstance(value.get("url"), str) and value["url"], "Missing release URL")
    return {key: value[key] for key in ("version", "url", "sha256")}


def release_directory(data: Path, spec: dict) -> Path:
    return data / "releases" / "vphone" / spec["sha256"]


def _download_directory(data: Path, spec: dict) -> Path:
    root = release_directory(data, spec)
    ancestor = root
    while not os.path.lexists(ancestor) and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    _direct(ancestor)
    _require(ancestor.is_dir(), f"Release cache parent is not a directory: {ancestor}")
    for suffix in (".zip", ".zip.partial"):
        archive = root / f"VPhone-{spec['version']}{suffix}"
        if os.path.lexists(archive):
            _direct(archive)
            info = archive.lstat()
            _require(
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                f"Release download is not a single-link file: {archive}",
            )
    return root


def _metadata(info):
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "nlink": info.st_nlink,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _direct(path: Path):
    try:
        direct = path.is_absolute() and path.resolve(strict=True) == path
    except OSError as error:
        raise ReleaseError(
            f"Release resource is unavailable: {path}. Run limulator bootstrap release"
        ) from error
    _require(direct, f"Resource path is not direct: {path}")


def _open_regular(path: Path):
    _direct(path)
    before = path.lstat()
    _require(
        stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
        f"Resource is not a single-link file: {path}",
    )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        _require(
            stat.S_ISREG(os.fstat(descriptor).st_mode)
            and _metadata(before) == _metadata(os.fstat(descriptor)) == _metadata(path.lstat()),
            f"Resource changed before verification: {path}",
        )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _fingerprint(path: Path) -> dict:
    with _open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        _require(
            _metadata(before) == _metadata(os.fstat(stream.fileno())) == _metadata(path.lstat()),
            f"Resource changed during verification: {path}",
        )
    return _metadata(before) | {"sha256": digest}


def _archive(root: Path, spec: dict) -> tuple[dict, dict]:
    archive = root / f"VPhone-{spec['version']}.zip"
    fingerprint = _fingerprint(archive)
    _require(fingerprint["sha256"] == spec["sha256"], "Release archive does not match its lock")
    identity = {key: value for key, value in fingerprint.items() if key != "sha256"}
    with _open_regular(archive) as stream:
        _require(
            _metadata(os.fstat(stream.fileno())) == identity,
            "Release archive changed before inspection",
        )
        with zipfile.ZipFile(stream) as bundle:
            expected = _archive_payloads(bundle)
        _require(
            _metadata(os.fstat(stream.fileno())) == _metadata(archive.lstat()) == identity,
            "Release archive changed during inspection",
        )
    return fingerprint, expected


def _archive_payloads(bundle: zipfile.ZipFile) -> dict:
    entries = bundle.infolist()
    _require(len(entries) <= 20000, "Release archive has too many entries")
    names = {}
    for entry in entries:
        name = entry.filename.rstrip("/")
        _require(
            name
            and not name.startswith("/")
            and "\\" not in name
            and str(PurePosixPath(name)) == name
            and ".." not in PurePosixPath(name).parts,
            "Unsafe release archive path",
        )
        _require(name not in names, "Duplicate release archive path")
        names[name] = entry
    marker = "VPhone.bundle/Contents/Resources/"
    prefixes = {name.split(marker)[0] + marker for name in names if marker in name}
    _require(len(prefixes) == 1, "Release archive has no unique VPhone resources directory")
    prefix = prefixes.pop()
    selected = {
        name[len(prefix) :]: entry
        for name, entry in names.items()
        if name.startswith(prefix + "guest-resources/") and not entry.is_dir()
    }
    for rom in ("OpenAVB", "OpenSEP"):
        relative = next((name for name in (f"{rom}/{rom}.bin", f"{rom}.bin") if prefix + name in names), None)
        _require(relative is not None, f"Release archive lacks {rom}")
        selected[relative] = names[prefix + relative]
    _require(
        {"guest-resources/vphoned", "guest-resources/vphoned.plist"} <= selected.keys(),
        "Release archive lacks guest worker resources",
    )
    expected = {}
    for relative, entry in selected.items():
        mode = entry.external_attr >> 16
        _require(
            stat.S_IFMT(mode) in (0, stat.S_IFREG) and 0 < entry.file_size <= 256 << 20,
            f"Release payload is not a bounded regular file: {relative}",
        )
        with bundle.open(entry) as stream:
            expected[relative] = {
                "size": entry.file_size,
                "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
            }
    return expected


def _bundle(root: Path, expected: dict) -> dict:
    resources = root / "app/VPhone.bundle/Contents/Resources"
    _direct(resources)
    guest = resources / "guest-resources"
    _direct(guest)
    actual_names = set()

    def unreadable(error):
        raise error

    for parent, directories, files in os.walk(guest, followlinks=False, onerror=unreadable):
        for name in directories + files:
            path = Path(parent) / name
            _require(not path.is_symlink(), f"Symlink in guest resources: {path}")
        actual_names.update(str((Path(parent) / name).relative_to(resources)) for name in files)
    _require(
        actual_names == {name for name in expected if name.startswith("guest-resources/")},
        "Cached guest resource inventory differs from the locked archive",
    )
    actual = {}
    for name, entry in expected.items():
        actual[name] = _fingerprint(resources / name)
        _require(
            all(actual[name][key] == entry[key] for key in ("size", "sha256")),
            f"Cached release payload differs from the locked archive: {name}",
        )
    return actual


def _backend(root: Path, backend: Path, payloads: dict) -> dict:
    guest = root / "app/VPhone.bundle/Contents/Resources/guest-resources"
    link = backend / "guest-resources"
    _direct(backend)
    _require(
        link.is_symlink() and link.resolve(strict=True) == guest,
        "Backend guest-resources does not target the locked release cohort",
    )
    link_info = link.lstat()
    result = {"guest_link": _metadata(link_info) | {"target": os.readlink(link)}, "roms": {}}
    for rom in ("OpenAVB", "OpenSEP"):
        name = next(name for name in payloads if name in (f"{rom}/{rom}.bin", f"{rom}.bin"))
        value = _fingerprint(backend / rom / f"{rom}.bin")
        _require(
            all(value[key] == payloads[name][key] for key in ("size", "sha256")),
            f"Backend {rom} does not match the locked archive",
        )
        result["roms"][rom] = value
    _require(
        _metadata(link.lstat()) == _metadata(link_info), "Backend resource link changed during verification"
    )
    return result


def prepare_release(data: Path, backend: Path, spec: dict) -> dict:
    root = release_directory(data, spec)
    archive, expected = _archive(root, spec)
    _direct(backend)
    target = root / "app/VPhone.bundle"
    if os.path.lexists(target):
        _bundle(root, expected)
    elif os.path.lexists(root / "app"):
        _direct(root / "app")
        _require(not any((root / "app").iterdir()), "Unverified partial release cache requires inspection")
    for rom in ("OpenAVB", "OpenSEP"):
        directory = backend / rom
        _require(not directory.is_symlink(), f"Refusing a linked backend ROM directory: {directory}")
        destination = directory / f"{rom}.bin"
        if os.path.lexists(destination):
            _require(
                not destination.is_symlink() and destination.is_file() and destination.stat().st_nlink == 1,
                f"Refusing a linked backend ROM: {destination}",
            )
    link = backend / "guest-resources"
    _require(
        not os.path.lexists(link) or link.is_symlink(), "Refusing to replace a backend resource directory"
    )
    return archive


def _proof(data: Path, backend: Path, spec: dict) -> dict:
    root = release_directory(data, spec)
    archive, expected = _archive(root, spec)
    payloads = _bundle(root, expected)
    return {
        "schema_version": 1,
        "release": spec,
        "archive": archive,
        "payloads": payloads,
        "backend": _backend(root, backend, payloads),
    }


def verify_release(data: Path, backend: Path, spec: dict | None = None) -> dict:
    spec = release_spec() if spec is None else spec
    receipt = release_directory(data, spec) / RECEIPT
    _require(receipt.is_file(), "Locked release receipt is missing")
    with _open_regular(receipt) as stream:
        before = os.fstat(stream.fileno())
        _require(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= 1 << 20,
            "Locked release receipt is not a bounded single-link file",
        )
        try:
            value = json.load(stream)
        except (ValueError, UnicodeError) as error:
            raise ReleaseError("Invalid release receipt; run limulator bootstrap release") from error
        _require(
            _metadata(before) == _metadata(os.fstat(stream.fileno())) == _metadata(receipt.lstat()),
            "Locked release receipt changed during verification",
        )
    _require(
        value == _proof(data, backend, spec), "Locked release receipt or resource identities no longer match"
    )
    return value


def record_release(data: Path, backend: Path, spec: dict) -> None:
    value = _proof(data, backend, spec)
    root = release_directory(data, spec)
    descriptor, temporary = tempfile.mkstemp(prefix=".release-receipt-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / RECEIPT)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_release(data: Path, backend: Path, spec: dict) -> None:
    _private_data_directory(data)
    lock = data / ".creation.lock"
    data_guard = _lock_data_creation_file(lock, os.getuid())
    try:
        host_guard = host_creation_lock(
            HOST_CREATION_LOCK,
            lambda command: subprocess.run(["sudo", "-n", *command], check=True),
        )
        try:
            prepare_release(data, backend, spec)
            environment = os.environ | {
                "VSETUP_SRC": str(backend),
                "VSETUP_RELEASE": str(release_directory(data, spec)),
                "VPHONE_CLI_PUBLIC": spec["version"],
            }
            subprocess.run(
                [str(backend / "setup/setup.sh"), "release"],
                env=environment,
                check=True,
                pass_fds=(data_guard, host_guard),
            )
            record_release(data, backend, spec)
        finally:
            os.close(host_guard)
    finally:
        os.close(data_guard)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("path", "install"))
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--backend", type=Path)
    args = parser.parse_args()
    spec = release_spec(args.lock)
    if args.operation == "path":
        print(_download_directory(args.data, spec))
    else:
        if args.backend is None:
            parser.error("--backend is required for install")
        install_release(args.data, args.backend, spec)


if __name__ == "__main__":
    main()
