"""Create a guest using public firmware and task-owned restore helpers."""

from __future__ import annotations

import contextlib
import ctypes
import datetime
import errno
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import _creation_cleanup, _first_boot_check
from .graphics import (
    DEFAULT_GRAPHICS_MODE,
    apply_graphics_environment,
    graphics_policy_metadata,
    recorded_graphics_mode,
    require_graphics_mode,
    select_graphics_mode,
)
from .lifecycle import (
    Device,
    Paths,
    Registry,
    Runtime,
    _atomic_json,
    _disk_users,
    _identity,
    _identity_alive,
    _lock_disk,
    _private_directory,
    _read_json,
    _read_vm_identity,
    _require_cache_publication_complete,
    _resource_guard,
    _validate_name,
)
from .profiles import DeviceProfile, FirmwareDescriptor, profile_for
from .protocols import GuestClient, ProtocolError

_STAGES = ("sanity", "bundle", "firmware", "gpu-driver", "prepare", "patch", "restore", "cfw", "booted")
_HOST_CREATION_LOCK = Path("/run/lock/limulator-create.lock")
_BACKEND_CONTROL_ALIASES = (
    ("LIMULATOR_FIRST_BOOT_QMP", "VPHONE_FIRST_BOOT_QMP"),
    ("LIMULATOR_USB_TRACE", "VPHONE_USB_TRACE"),
    ("LIMULATOR_USB_TRACE_FILE", "VPHONE_USB_TRACE_FILE"),
    ("LIMULATOR_FIRST_BOOT_TIMEOUT", "VPHONE_FIRST_BOOT_TIMEOUT"),
    ("LIMULATOR_SHUTDOWN_TIMEOUT", "VPHONE_SHUTDOWN_TIMEOUT"),
    ("LIMULATOR_MIGRATION_TIMEOUT", "VPHONE_MIGRATION_TIMEOUT"),
)


def _run(command, *, env=None, timeout=30, check=True, input=None):
    result = subprocess.run(command, env=env, input=input, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{command[0]} exited with status {result.returncode}: {result.stderr.strip()}")
    return result


def _normalize_backend_controls(env: dict[str, str]) -> None:
    for legacy, native in _BACKEND_CONTROL_ALIASES:
        if native not in env and legacy in env:
            env[native] = env[legacy]
        env.pop(legacy, None)
    env.pop("LIMULATOR_CREATOR_USB_READY", None)
    env.pop("VPHONE_CREATOR_USB_READY", None)


def _firmware_cache(env: dict[str, str]) -> Path:
    return Path(env.get("VSETUP_CACHE") or Path.home() / ".cache/vphone").expanduser().resolve()


def _sshrd_bootstrap_environment(env: dict[str, str]) -> None:
    prebuilt = env.get("VPHONE_SSHRD_BOOTSTRAP", "")
    build = env.get("VPHONE_SSHRD_BOOTSTRAP_BUILD", "0" if prebuilt else "1")
    if build not in ("0", "1"):
        raise ValueError("VPHONE_SSHRD_BOOTSTRAP_BUILD must be 0 or 1")
    if prebuilt and build == "1":
        raise ValueError("choose VPHONE_SSHRD_BOOTSTRAP or VPHONE_SSHRD_BOOTSTRAP_BUILD=1, not both")
    if not prebuilt and build == "0":
        raise ValueError("creation requires an ECID-aware SSHRD bootstrap: select source build or a prebuilt")
    env["VPHONE_SSHRD_BOOTSTRAP_BUILD"] = build
    if prebuilt:
        env["VPHONE_SSHRD_BOOTSTRAP"] = str(Path(prebuilt).expanduser().resolve())


def _native_env(paths: Paths) -> dict[str, str]:
    env = os.environ.copy()
    xtool_config = Path(env.get("LIMULATOR_XTOOL_CONFIG") or paths.data / "xtool/config").expanduser()
    env.setdefault("DARWIN_SDK_BUNDLE", str(xtool_config / "swiftpm/swift-sdks/darwin.artifactbundle"))
    env["PATH"] = os.pathsep.join(
        (str(paths.prefix / "bin"), str(Path.home() / ".cargo/bin"), env.get("PATH", os.defpath))
    )
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        [str(paths.prefix / "lib"), str(paths.prefix / "lib64")]
        + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else [])
    )
    env.update(
        LIMULATOR_HOME=str(paths.data),
        VSETUP_SRC=str(paths.backend),
        VSETUP_DEVICES=str(paths.vms),
        VSETUP_RELEASE=str(paths.data / "releases/vphone"),
        VSETUP_CACHE=str(_firmware_cache(env)),
        VPHONE_RCODESIGN=str(paths.prefix / "bin/rcodesign"),
        VPHONE_APFS_KO=str(paths.backend / ".build/linux-apfs-rw/apfs.ko"),
        VC=str(
            paths.backend
            / f"linux-create/.build/out/out/Products/Release-linux-{platform.machine()}/vphone-cli"
        ),
        QEMU=str(paths.backend / "qemu/build/qemu-system-aarch64"),
        GFX="1",
        WINDOW="0",
        MEM="8G",
        KEEP="1",
        VSETUP_ASSUME_YES="1",
    )
    _sshrd_bootstrap_environment(env)
    _normalize_backend_controls(env)
    return env


def _hex_ecid(value: str, label: str) -> int:
    if not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{1,16}", value) or int(value, 16) == 0:
        raise ValueError(f"{label} must be nonzero with 1-16 hexadecimal digits, optionally prefixed with 0x")
    return int(value, 16)


def _bind_vm_ecid(vm: Path, env: dict[str, str]) -> None:
    try:
        selected = _hex_ecid(_read_vm_identity(vm / "ecid", 64).decode("ascii").rstrip("\n"), "VM ECID")
        config = plistlib.loads(_read_vm_identity(vm / "config.plist", 1 << 20))
        identifier = config.get("machineIdentifier") if isinstance(config, dict) else None
        machine = plistlib.loads(identifier) if isinstance(identifier, bytes) else None
        configured = machine.get("ECID") if isinstance(machine, dict) else None
        if type(configured) is not int or not 0 < configured < 1 << 64:
            raise ValueError("config.plist has no valid nonzero UInt64 machine ECID")
        if configured != selected:
            raise ValueError("VM ecid does not match config.plist machine ECID")
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException, ExpatError) as error:
        raise ValueError(f"invalid selected VM ECID identity: {error}") from error
    if "VPHONE_ECID" in env and _hex_ecid(env["VPHONE_ECID"], "VPHONE_ECID") != selected:
        raise ValueError("VPHONE_ECID does not match the selected VM bundle")
    env["VPHONE_ECID"] = f"0x{selected:x}"


def _write_usb_trace(path: Path, source: str, event: str, **fields) -> bool:
    """Append one bounded, payload-free restore transport event."""
    try:
        record = {
            "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "monotonic_ns": time.monotonic_ns(),
            "source": source,
            "event": event,
            **fields,
        }
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            return os.write(descriptor, data) == len(data)
        finally:
            os.close(descriptor)
    except Exception:
        return False


def _sudo(command, env, **kwargs):
    keys = (
        "PATH",
        "LD_LIBRARY_PATH",
        "USBMUXD_SOCKET",
        "USBMUXD_SOCKET_ADDRESS",
        "VPHONE_RCODESIGN",
        "VPHONE_APFS_KO",
    )
    assignments = [f"{key}={env[key]}" for key in keys if key in env]
    return _run(["/usr/bin/sudo", "-n", "/usr/bin/env", *assignments, *map(str, command)], env=env, **kwargs)


def _pairing(paths: Paths, model: str, ios: str) -> tuple[dict, dict]:
    value = _read_json(paths.backend / "setup/data/compatibility.json")
    if value is None:
        raise RuntimeError("backend compatibility catalog is missing; bootstrap the backend first")
    device = next((item for item in value.get("devices", []) if item.get("id") == model), None)
    pair = next(
        (
            item
            for item in value.get("pairings", [])
            if item.get("device") == model and item.get("osVersion") == ios and not item.get("unsupported")
        ),
        None,
    )
    if not device or not pair:
        raise ValueError(f"no supported firmware pairing for {model} and iOS {ios}")
    if pair.get("requiresMacBuilder"):
        raise ValueError(
            "this firmware pairing requires a macOS builder and cannot be created locally on Linux"
        )
    return device, pair


def _verify_firmware(path: Path, expected: str, expected_size: int | None = None) -> dict:
    if not path.is_file():
        raise RuntimeError(f"verified Apple firmware is required before restore: {path}")
    if expected_size is not None and path.stat().st_size != expected_size:
        raise RuntimeError(f"firmware size mismatch for {path.name}: expected {expected_size} bytes")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise RuntimeError(f"firmware SHA-256 mismatch for {path.name}: expected {expected}, got {actual}")
    info = path.stat()
    return {"path": str(path), "sha256": actual, "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _verified_firmware_stage(vm: Path, files: tuple[tuple[Path, FirmwareDescriptor], ...]) -> list[dict]:
    receipts = []
    for path, descriptor in files:
        if not descriptor.metadata_ready or descriptor.sha256 is None:
            raise RuntimeError(f"firmware metadata is not verified for {path.name}")
        receipts.append(_verify_firmware(path, descriptor.sha256, descriptor.size))
    if "firmware" not in _completed(vm):
        with (vm / ".create-state").open("a") as stream:
            stream.write("firmware\n")
    return receipts


def _lock_transport_file(path: Path, owner: int) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise RuntimeError(f"shared restore lock has unsafe ownership or permissions: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another guest creation owns the shared restore transport") from exc
        return fd
    except BaseException:
        os.close(fd)
        raise


def _host_creation_lock(env: dict[str, str]) -> int:
    try:
        return _lock_transport_file(_HOST_CREATION_LOCK, 0)
    except FileNotFoundError:
        # Root ownership keeps another user from replacing the shared inode.
        program = (
            "import os,sys\n"
            "try:\n"
            " fd=os.open(sys.argv[1],os.O_CREAT|os.O_EXCL|os.O_RDONLY|os.O_NOFOLLOW,0o644)\n"
            "except FileExistsError: pass\n"
            "else: os.close(fd)\n"
        )
        _sudo(["/usr/bin/python3", "-c", program, str(_HOST_CREATION_LOCK)], env)
        return _lock_transport_file(_HOST_CREATION_LOCK, 0)


def _active_vhost_cids() -> dict[int, int]:
    active = {}
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if not (process / "exe").resolve().name.startswith("qemu-system-"):
                continue
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            for argument in arguments:
                if not argument.startswith((b"vhost-vsock-pci,", b"vhost-vsock-device,")):
                    continue
                match = re.search(rb"(?:^|,)guest-cid=([0-9]+)(?:,|$)", argument)
                if match and _identity(int(process.name)):
                    active[int(match[1])] = int(process.name)
        except OSError:
            continue
    return active


def _resolve_candidate(
    paths: Paths, name: str, profile: DeviceProfile, cid: int | None, scale: int | None
) -> tuple[Device, bool]:
    _validate_name(name)
    registered = Registry(paths).list()
    current = next((device for device in registered if device.name == name), None)
    receipt = _read_json(paths.vms / name / ".limulator-create.json") or {}
    previous = receipt.get("specification", {})
    if not isinstance(previous, dict):
        raise ValueError("invalid creation receipt specification; its data is preserved")
    if current is not None:
        previous = {
            "model": current.model,
            "ios": current.os_version,
            "cid": current.cid,
            "scale": current.scale,
        }
    if previous:
        if type(previous.get("cid")) is not int:
            raise ValueError("invalid CID in creation receipt; its data is preserved")
        if previous.get("model") != profile.model or previous.get("ios") != profile.os_version:
            raise ValueError("device name has different creation settings; its existing data is preserved")
        if cid is not None and cid != previous.get("cid"):
            raise ValueError("CID differs from the same-name creation receipt")
        if scale is not None and scale != previous.get("scale"):
            raise ValueError("scale differs from the same-name creation receipt")
        cid, scale = previous.get("cid"), previous.get("scale")
    scale = profile.scale if scale is None else scale
    if isinstance(scale, bool) or scale not in (1, 2, 3):
        raise ValueError("scale must be 1, 2, or 3")
    scale = int(scale)
    if profile.os_version.startswith("27.") and scale < 2:
        raise ValueError("iOS 27 requires a display scale of at least 2")
    if profile.category == "foldable" and scale != profile.scale:
        raise ValueError(f"{profile.label} creation currently requires scale {profile.scale}")
    reserved = {device.cid: device.name for device in registered if device.name != name}
    for path in paths.vms.glob("*/.limulator-create.json"):
        if path.parent.name == name:
            continue
        pending = (_read_json(path) or {}).get("specification", {})
        value = pending.get("cid")
        if type(value) is int and 3 <= value <= 0xFFFFFFFE:
            reserved.setdefault(value, path.parent.name)
    active = {} if current is not None else _active_vhost_cids()
    if cid is None:
        cid = 41
        while cid in reserved or cid in active:
            cid += 1
    if cid in reserved:
        raise ValueError(f"CID {cid} is already registered or reserved to {reserved[cid]}")
    if cid in active:
        raise ValueError(f"CID {cid} is already used by live QEMU process {active[cid]}")
    width, height = profile.points
    candidate = Device(
        name, paths.vms / name, profile.model, profile.os_version, cid, width * scale, height * scale, scale
    )
    return current or candidate, current is not None


def _apply_profile_configuration(vm: Path, profile: DeviceProfile, scale: int) -> None:
    path = vm / "config.plist"
    original = _read_vm_identity(path, 1 << 20)
    config = plistlib.loads(original)
    updated = dict(config)
    updated.update(
        guestProductType=profile.model,
        screenConfig={
            "width": profile.width,
            "height": profile.height,
            "pixelsPerInch": profile.pixels_per_inch,
            "scale": float(profile.scale),
        },
    )
    if updated != config:
        fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=vm)
        try:
            with os.fdopen(fd, "wb") as stream:
                plistlib.dump(
                    updated,
                    stream,
                    fmt=plistlib.FMT_BINARY if original.startswith(b"bplist00") else plistlib.FMT_XML,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
    width, height = profile.points
    for name, value in (
        ("gfx-res", f"{width * scale}x{height * scale}\n"),
        ("gfx-res.3x", f"{profile.width}x{profile.height}\n"),
    ):
        target = vm / name
        if not target.exists() or target.read_text() != value:
            target.write_text(value)


def _creation_specification(candidate: Device) -> dict:
    return {
        "name": candidate.name,
        "model": candidate.model,
        "ios": candidate.os_version,
        "cid": candidate.cid,
        "scale": candidate.scale,
    }


def _fresh_disk_allowed(paths: Paths, candidate: Device, receipt: dict | None) -> None:
    for device in Registry(paths).list():
        disk = candidate.vm_path / "Disk.img"
        if device.name == candidate.name or (disk.exists() and disk.samefile(device.vm_path / "Disk.img")):
            raise ValueError("fresh disk recovery is unavailable for a registered device")
    if not receipt or receipt.get("specification") != _creation_specification(candidate):
        raise ValueError("fresh disk recovery requires a matching incomplete creation receipt")
    pending = candidate.vm_path / ".vphone-dsc-pending.json"
    if pending.exists() or pending.is_symlink():
        raise ValueError(
            "fresh disk recovery refuses an unresolved cache publication; preserve and inspect "
            f"the disk and recovery stage recorded by {pending}"
        )
    completed = _completed(candidate.vm_path)
    if (
        receipt.get("state") in ("created", "ready")
        or "bundle" not in completed
        or any(stage in completed for stage in ("restore", "cfw", "booted"))
    ):
        raise ValueError("fresh disk recovery requires an incomplete restore")
    owner = receipt.get("process")
    if _identity_alive(owner) and owner["pid"] != os.getpid():
        raise RuntimeError("the incomplete creation still has a live owner")


def _rename_preserved(source: Path, destination: Path) -> None:
    # Linux RENAME_NOREPLACE makes preservation independent of a prior existence check.
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def _disk_receipt(path: Path, fd: int) -> dict:
    before = os.fstat(fd)
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, 8 << 20, offset):
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RuntimeError("incomplete disk changed while its recovery backup was verified")
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size": before.st_size,
        "device": before.st_dev,
        "inode": before.st_ino,
    }


def _rollback_fresh_disk(vm: Path, record: dict, old_fd: int, new_fd: int | None) -> None:
    disk, backup = vm / "Disk.img", Path(record["disk"]["path"])
    old = os.fstat(old_fd)
    if (backup.stat().st_dev, backup.stat().st_ino) != (old.st_dev, old.st_ino):
        raise RuntimeError("recovery backup identity changed; refusing to replace the current disk")
    if new_fd is not None:
        new = os.fstat(new_fd)
        current = disk.lstat()
        if (current.st_dev, current.st_ino) != (new.st_dev, new.st_ino) or _disk_users(disk):
            raise RuntimeError("replacement disk has another owner; recovery backup remains preserved")
        _rename_preserved(disk, backup.parent / "unused-Disk.img")
    elif disk.exists() or disk.is_symlink():
        raise RuntimeError("unexpected disk appeared during recovery; backup remains preserved")
    _rename_preserved(backup, disk)
    for name in record["companions"]:
        original = backup.parent / name
        target = vm / name
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"mutable companion changed identity during recovery: {target}")
        if original.read_bytes() != target.read_bytes():
            fd, temporary = tempfile.mkstemp(prefix=".recovery-", dir=vm)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(original.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                shutil.copymode(original, temporary)
                os.replace(temporary, target)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
    record.update(state="rolled-back", restored_path=str(disk))


def _prepare_fresh_disk(
    vm: Path, state: dict, state_path: Path, old_fd: int, previous_receipt: bytes | None = None
) -> tuple[int, dict]:
    disk = vm / "Disk.img"
    opened, current = os.fstat(old_fd), disk.lstat()
    if not stat.S_ISREG(current.st_mode) or (opened.st_dev, opened.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        raise RuntimeError("fresh disk recovery requires the locked regular Disk.img")
    if _disk_users(disk):
        raise RuntimeError("fresh disk recovery refuses a disk with an active owner")
    directory = Path(tempfile.mkdtemp(prefix="preserved-disk-", dir=vm))
    record = {"state": "preparing", "disk": _disk_receipt(directory / "Disk.img", old_fd), "companions": {}}
    for name in ("config.plist", "ecid", "nvram.bin", "SEPStorage", "gfx-res", "gfx-res.3x", ".create-state"):
        source = vm / name
        if not stat.S_ISREG(source.lstat().st_mode):
            raise RuntimeError(f"fresh disk recovery requires a regular companion: {source}")
        target = directory / name
        shutil.copy2(source, target)
        record["companions"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
    receipt_backup = directory / state_path.name
    receipt_backup.write_bytes(previous_receipt if previous_receipt is not None else state_path.read_bytes())
    receipt_backup.chmod(0o600)
    record["receipt_sha256"] = hashlib.sha256(receipt_backup.read_bytes()).hexdigest()
    state.setdefault("fresh_disks", []).append(record)
    _atomic_json(state_path, state)
    new_fd = None
    moved = False
    try:
        _rename_preserved(disk, directory / "Disk.img")
        moved = True
        new_fd = os.open(disk, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        fcntl.flock(new_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(new_fd, opened.st_size)
        os.fsync(new_fd)
        info = os.fstat(new_fd)
        record.update(state="prepared", replacement={"device": info.st_dev, "inode": info.st_ino})
        _atomic_json(state_path, state)
        return new_fd, record
    except BaseException:
        try:
            if moved:
                _rollback_fresh_disk(vm, record, old_fd, new_fd)
            with contextlib.suppress(OSError):
                _atomic_json(state_path, state)
        finally:
            if new_fd is not None:
                os.close(new_fd)
        raise


def _resume_completed_boot(
    paths: Paths, candidate: Device, receipt: dict | None, run_dir: Path, disk_fd: int
) -> dict | None:
    vm = candidate.vm_path
    completed = _completed(vm)
    if "booted" not in completed:
        return None
    if (
        not receipt
        or receipt.get("specification") != _creation_specification(candidate)
        or receipt.get("state") not in ("creating", "error")
        or completed != list(_STAGES)
    ):
        raise RuntimeError("verification resume requires a matching incomplete creation receipt")
    disk, opened = vm / "Disk.img", os.fstat(disk_fd)
    current = disk.lstat()
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise RuntimeError("verification resume requires ownership of the original regular disk")
    if _disk_users(disk):
        raise RuntimeError("verification resume refuses a disk with an active owner")
    if any(
        device.name == candidate.name or disk.samefile(device.vm_path / "Disk.img")
        for device in Registry(paths).list()
    ):
        raise RuntimeError("verification resume is unavailable for a registered device")
    state_path = vm / ".create-state"
    if not stat.S_ISREG(state_path.lstat().st_mode):
        raise RuntimeError("verification resume requires a regular creation state file")
    original = state_path.read_bytes()
    updated = b"".join(
        line for line in original.splitlines(keepends=True) if line.rstrip(b"\r\n") != b"booted"
    )
    backup = run_dir / ".create-state.before-verification-resume"
    with backup.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    fd, temporary = tempfile.mkstemp(prefix=".create-state-", dir=vm)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return {
        "preserved_state": str(backup),
        "previous_state_sha256": hashlib.sha256(original).hexdigest(),
        "resumed_state_sha256": hashlib.sha256(updated).hexdigest(),
        "selected_stages": ["booted"],
    }


def _unit_states() -> dict[str, str]:
    return {
        unit: _run(["systemctl", "is-active", unit], check=False).stdout.strip()
        for unit in ("usbmuxd.service", "usbmuxd.socket")
    }


def _apple_devices() -> list[str]:
    devices = []
    for vendor in Path("/sys/bus/usb/devices").glob("*/idVendor"):
        with contextlib.suppress(OSError):
            if vendor.read_text().strip().lower() == "05ac":
                devices.append(str(vendor.parent.resolve()))
    return sorted(devices)


def _vhci_ports() -> dict[int, str]:
    ports = {}
    for path in Path("/sys/devices/platform").glob("vhci_hcd.*/status*"):
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 7 and fields[2] in ("005", "006"):
                ports[int(fields[1])] = fields[6]
    return ports


def _usbip_connections() -> set[tuple[str, str, str, int]]:
    namespace = os.readlink("/proc/self/ns/net")
    connections = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[2] != "0100007F:0CA8" or fields[3] != "01" or not fields[1].startswith("0100007F:"):
            continue
        inode = int(fields[9])
        if inode == 0:
            raise RuntimeError("USB/IP restore connection has no kernel socket inode")
        connections.add((namespace, fields[1], fields[2], inode))
    return connections


def _listener_pids(port=3240) -> list[int]:
    inodes = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == f"0100007F:{port:04X}" and fields[3] == "0A":
            inodes.add(fields[9])
    if not inodes:
        return []
    owners = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            for fd in (process / "fd").iterdir():
                with contextlib.suppress(OSError):
                    if fd.readlink().name in {f"socket:[{inode}]" for inode in inodes}:
                        owners.append(int(process.name))
                        break
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return owners


def _in_scope(pid: int, unit: str) -> bool:
    try:
        return any(unit in line.split("/") for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines())
    except OSError:
        return False


def _creation_scope_group(uid: int, unit: str) -> str:
    return f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}"


def _creation_scope_cgroup(pid: int) -> str | None:
    try:
        groups = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except OSError:
        return None
    return groups[0] if len(groups) == 1 else None


def _capture_creation_scope_invocation(pid: int, unit: str, env: dict[str, str]) -> str | None:
    uid = os.getuid()
    group = _creation_scope_group(uid, unit)
    expected_cgroup = f"0::{group}"
    if _creation_scope_cgroup(pid) != expected_cgroup:
        return None
    result = _run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=LoadState,ActiveState,InvocationID,ControlGroup",
        ],
        env=env,
        timeout=5,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    invocation_id = properties.get("InvocationID", "").lower()
    if (
        properties.get("LoadState") != "loaded"
        or properties.get("ActiveState") != "active"
        or properties.get("ControlGroup") != group
        or not re.fullmatch(r"[0-9a-f]{32}", invocation_id)
        or invocation_id == "0" * 32
    ):
        raise RuntimeError(f"creation scope identity could not be established: {unit}; {properties}")
    if _creation_scope_cgroup(pid) != expected_cgroup:
        raise RuntimeError(f"creation launcher left its scope while capturing identity: {unit}")
    return invocation_id


def _signal_owned(identity: dict, value: int) -> bool:
    if not _identity_alive(identity):
        return False
    # Standalone Python builds can omit these APIs even on a supporting kernel.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes = (ctypes.c_int, ctypes.c_uint)
    libc.pidfd_open.restype = ctypes.c_int
    libc.pidfd_send_signal.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    libc.pidfd_send_signal.restype = ctypes.c_int
    fd = libc.pidfd_open(identity["pid"], 0)
    if fd < 0:
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            return False
        raise OSError(error, os.strerror(error))
    try:
        if not _identity_alive(identity):
            return False
        if libc.pidfd_send_signal(fd, value, None, 0) < 0:
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return False
            raise OSError(error, os.strerror(error))
        return True
    finally:
        os.close(fd)


def _cleanup_creation_scope(
    unit: str,
    env: dict[str, str],
    owner: dict,
    baseline: list[dict],
    *,
    scope_owned: bool,
    expected_invocation_id: str | None,
) -> None:
    _sudo(
        ["/usr/bin/python3", Path(_creation_cleanup.__file__).resolve()],
        env,
        input=json.dumps(
            {
                "unit": unit,
                "uid": os.getuid(),
                "boot_id": owner["boot_id"],
                "baseline": baseline,
                "scope_owned": scope_owned,
                "expected_invocation_id": expected_invocation_id,
            }
        ),
        timeout=60,
    )


class _USBHelpers:
    def __init__(self, paths: Paths, directory: Path, env: dict[str, str], unit: str):
        self.paths, self.directory, self.env, self.unit = paths, directory, env, unit
        self.rule = Path(f"/run/udev/rules.d/99-limulator-{os.getuid()}-{uuid.uuid4().hex[:8]}.rules")
        self.daemon = None
        self.owned_ports: set[int] = set()
        self.import_connections: dict[int, tuple[str, str, str, int]] = {}
        self.import_unproven = False
        self.seen_devices: set[tuple[str, int, int]] = set()
        self.rule_created = False
        self.daemon_identity = None
        self.attach_failures = 0
        self.trace_enabled = env.get("VPHONE_USB_TRACE") == "1"
        self.trace_path = Path(env.get("VPHONE_USB_TRACE_FILE", str(directory / "usb-transport-trace.jsonl")))
        self.trace_states = {}
        self.trace_warning_reported = False

    def _trace(self, event: str, **fields) -> None:
        if not self.trace_enabled:
            return
        if not _write_usb_trace(self.trace_path, "host", event, **fields) and not self.trace_warning_reported:
            print("USB transport trace could not be written", file=sys.stderr, flush=True)
            self.trace_warning_reported = True

    def _trace_change(self, key: str, value, event: str, **fields) -> None:
        if not self.trace_enabled or self.trace_states.get(key, object()) == value:
            return
        self.trace_states[key] = value
        self._trace(event, **fields)

    def start(self):
        socket_path = self.directory / "usbmuxd.sock"
        encoded_path = os.fsencode(socket_path)
        if b"\0" in encoded_path:
            raise ValueError("private usbmuxd socket path contains a NUL byte")
        if len(encoded_path) > 107:
            raise ValueError("private usbmuxd socket path exceeds 107 bytes; shorten LIMULATOR_HOME")
        self.env.pop("LIMULATOR_CREATOR_USB_READY", None)
        self.env.pop("VPHONE_CREATOR_USB_READY", None)
        if _apple_devices():
            raise RuntimeError(
                "Apple USB devices are already attached; creation will not take over live devices"
            )
        if _listener_pids():
            raise RuntimeError("localhost USB/IP port 3240 is already owned by another process")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", 3240))
            except OSError as exc:
                raise RuntimeError("localhost USB/IP port 3240 is already in use") from exc
        _sudo(["modprobe", "vhci-hcd"], self.env)
        if _vhci_ports():
            raise RuntimeError(
                "USB/IP devices are already imported; creation requires an unused virtual USB controller"
            )
        # This rule only applies to virtual Apple devices, never physical USB ports.
        rule = (
            f'SUBSYSTEM=="usb", KERNELS=="vhci_hcd.*", ATTR{{idVendor}}=="05ac", '
            f'OWNER:="{os.getuid()}", MODE:="0600", TAG-="systemd", '
            'ENV{SYSTEMD_WANTS}="", RUN:=""\n'
        )
        _sudo(["install", "-m", "0644", "/dev/stdin", self.rule], self.env, input=rule)
        self.rule_created = True
        _sudo(["udevadm", "control", "--reload"], self.env)
        self.env.update(USBMUXD_SOCKET=str(socket_path), USBMUXD_SOCKET_ADDRESS=f"UNIX:{socket_path}")
        private_config = self.directory / "pairing"
        _private_directory(private_config)
        log = (self.directory / "usbmuxd.log").open("ab", buffering=0)
        try:
            self.daemon = subprocess.Popen(
                [
                    str(self.paths.backend / ".build/usbmuxd-32k/usbmuxd"),
                    "-f",
                    "-n",
                    "-z",
                    "-p",
                    "-S",
                    str(socket_path),
                    "-P",
                    str(self.directory / "usbmuxd.pid"),
                    "-C",
                    str(private_config),
                ],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
        finally:
            log.close()
        self.daemon_identity = _identity(self.daemon.pid)
        deadline = time.monotonic() + 10
        while not socket_path.is_socket():
            if self.daemon.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"private usbmuxd did not start; see {self.directory / 'usbmuxd.log'}")
            time.sleep(0.1)
        self._trace("host.private_usbmuxd_ready", socket_ready=True)
        self.env["VPHONE_CREATOR_USB_READY"] = "1"

    def _owned_import(self, port: int) -> bool:
        expected = self.import_connections.get(port)
        if expected is None:
            raise RuntimeError(f"USB/IP port {port} has no recorded import connection identity")
        deadline = time.monotonic() + 5
        while True:
            ports = _vhci_ports()
            if port not in ports:
                return False
            if set(ports) != {port}:
                raise RuntimeError(
                    f"USB/IP port {port} connection attribution is ambiguous: active ports {sorted(ports)}"
                )
            connections = _usbip_connections()
            if connections == {expected}:
                current = _vhci_ports()
                if port not in current:
                    return False
                if set(current) == {port} and _usbip_connections() == connections:
                    return True
                raise RuntimeError(f"USB/IP port {port} changed during import connection verification")
            if connections or time.monotonic() >= deadline:
                raise RuntimeError(
                    f"USB/IP port {port} import connection identity changed or is unproven: "
                    f"expected={expected}; current={sorted(connections)}"
                )
            # Kernel shutdown releases the TCP socket before it clears the VHCI port.
            time.sleep(0.05)

    def poll(self):
        if not _identity_alive(self.daemon_identity):
            raise RuntimeError("task-owned usbmuxd exited during creation")
        physical = [path for path in _apple_devices() if "/vhci_hcd." not in path]
        if physical:
            raise RuntimeError(
                "a physical Apple device appeared during restore; preserving it and stopping this creation"
            )
        ports = _vhci_ports()
        foreign = set(ports) - self.owned_ports
        if foreign:
            raise RuntimeError(f"unowned USB/IP imports appeared during creation: {sorted(foreign)}")
        for port in ports:
            self._owned_import(port)
        ports = _vhci_ports()
        self.owned_ports.intersection_update(ports)
        self.import_connections = {
            port: value for port, value in self.import_connections.items() if port in ports
        }
        owners = _listener_pids()
        listener_scoped = bool(owners) and all(_in_scope(pid, self.unit) for pid in owners)
        self._trace_change(
            "listener",
            (len(owners), listener_scoped),
            "host.listener_state",
            listener_count=len(owners),
            scope_verified=listener_scoped,
        )
        if not owners:
            return
        if not listener_scoped:
            raise RuntimeError("USB/IP listener does not belong to this creation's exact systemd scope")
        if not ports:
            remote = _run(["usbip", "list", "-r", "127.0.0.1"], env=self.env, check=False, timeout=5)
            products = tuple(
                int(product, 16)
                for product in re.findall(
                    r"^\s*1-1:.*\(05ac:([0-9a-f]{4})\)", remote.stdout, re.MULTILINE | re.IGNORECASE
                )
            )
            self._trace_change(
                "devlist",
                products,
                "host.devlist_state",
                device_count=len(products),
                product_ids=[f"{product:04x}" for product in products],
            )
            if not products:
                return
            if _vhci_ports() or _usbip_connections():
                raise RuntimeError("USB/IP controller or restore connection changed before owned attach")
            owners = _listener_pids()
            if not owners or any(not _in_scope(pid, self.unit) for pid in owners):
                raise RuntimeError("USB/IP listener ownership changed before owned attach")
            self.import_unproven = True
            attach_started = time.monotonic_ns()
            attached = _sudo(
                ["usbip", "attach", "-r", "127.0.0.1", "-b", "1-1"], self.env, check=False, timeout=10
            )
            if attached.returncode:
                self.attach_failures += 1
                self._trace(
                    "host.attach_result",
                    result="failed",
                    returncode=attached.returncode,
                    consecutive_failures=self.attach_failures,
                    elapsed_ns=time.monotonic_ns() - attach_started,
                    product_ids=[f"{product:04x}" for product in products],
                )
                if self.attach_failures >= 20:
                    raise RuntimeError(
                        f"task-owned USB/IP import failed after bounded retries: {attached.stderr.strip()}"
                    )
                return
            self.attach_failures = 0
            self._trace(
                "host.attach_result",
                result="accepted",
                returncode=0,
                consecutive_failures=0,
                elapsed_ns=time.monotonic_ns() - attach_started,
                product_ids=[f"{product:04x}" for product in products],
            )
            current = _vhci_ports()
            deadline = time.monotonic() + 5
            while not current and time.monotonic() < deadline:
                time.sleep(0.05)
                current = _vhci_ports()
            if len(current) != 1:
                raise RuntimeError("could not identify this task's newly imported virtual USB port")
            self.owned_ports.update(current)
            owners = _listener_pids()
            if not owners or any(not _in_scope(pid, self.unit) for pid in owners):
                raise RuntimeError("USB/IP listener ownership changed during owned attach")
            connections = _usbip_connections()
            if len(connections) != 1:
                raise RuntimeError(
                    f"could not identify the owned import's unique TCP connection: {sorted(connections)}"
                )
            port = next(iter(current))
            self.import_connections[port] = next(iter(connections))
            self._owned_import(port)
            self._trace(
                "host.vhci_import_verified",
                port=port,
                product_ids=[f"{product:04x}" for product in products],
            )
            self.import_unproven = False
            ports = current
        for port, local in ports.items():
            if not self._owned_import(port):
                continue
            if local == "0-0":
                continue
            node = Path("/sys/bus/usb/devices") / local
            with contextlib.suppress(FileNotFoundError):
                vendor_id = (node / "idVendor").read_text().strip().lower()
                product_id = None
                if self.trace_enabled:
                    with contextlib.suppress(OSError):
                        product_id = (node / "idProduct").read_text().strip().lower()
                if vendor_id != "05ac":
                    raise RuntimeError("owned USB/IP port contains an unexpected vendor")
                bus, device = int((node / "busnum").read_text()), int((node / "devnum").read_text())
                key = (local, bus, device)
                if key in self.seen_devices:
                    continue
                usb = Path(f"/dev/bus/usb/{bus:03d}/{device:03d}")
                info = usb.stat()
                permitted = info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o600
                self._trace_change(
                    f"devnode-{key}",
                    (info.st_uid, info.st_mode & 0o777),
                    "host.vhci_device_node",
                    port=port,
                    product_id=product_id,
                    owner_uid=info.st_uid,
                    mode=f"{info.st_mode & 0o777:04o}",
                    permitted=permitted,
                )
                if permitted:
                    try:
                        sent = _signal_owned(self.daemon_identity, signal.SIGUSR2)
                    except OSError as error:
                        self._trace(
                            "host.usbmux_rescan",
                            sent=False,
                            error_errno=error.errno,
                            port=port,
                        )
                        raise
                    self._trace("host.usbmux_rescan", sent=sent, port=port)
                    self.seen_devices.add(key)

    def close(self):
        errors = []
        if self.daemon is not None:
            if self.daemon_identity is None and self.daemon.poll() is None:
                errors.append(f"private usbmuxd has no validated identity: PID {self.daemon.pid}")
            for value, timeout in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                if not _identity_alive(self.daemon_identity):
                    break
                try:
                    _signal_owned(self.daemon_identity, value)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self.daemon.wait(timeout=timeout)
                except Exception as exc:
                    errors.append(f"private usbmuxd {signal.Signals(value).name} failed: {exc}")
            if _identity_alive(self.daemon_identity):
                errors.append(f"private usbmuxd survived TERM/KILL: {self.daemon_identity}")
        try:
            current = _vhci_ports()
            if self.import_unproven and current:
                errors.append(f"USB/IP attach has unresolved import identity: active ports {sorted(current)}")
            ports = self.owned_ports & current.keys()
        except Exception as exc:
            errors.append(f"owned USB/IP inventory failed: {exc}")
            ports = set()
        for port in sorted(ports):
            try:
                if not self._owned_import(port):
                    continue
                result = _sudo(["usbip", "detach", "-p", str(port)], self.env, check=False, timeout=10)
                if result.returncode:
                    errors.append(
                        f"owned USB/IP port {port} detach exited {result.returncode}: {result.stderr.strip()}"
                    )
            except Exception as exc:
                errors.append(f"owned USB/IP port {port} detach failed: {exc}")
        try:
            deadline = time.monotonic() + 5
            remaining = self.owned_ports & _vhci_ports().keys()
            while remaining and time.monotonic() < deadline:
                time.sleep(0.1)
                remaining = self.owned_ports & _vhci_ports().keys()
            self.owned_ports.intersection_update(remaining)
            self.import_connections = {
                port: value for port, value in self.import_connections.items() if port in remaining
            }
            if remaining:
                errors.append(f"owned USB/IP imports remain attached: {sorted(remaining)}")
        except Exception as exc:
            errors.append(f"owned USB/IP detach verification failed: {exc}")
        if self.rule_created:
            try:
                _sudo(["rm", "--", self.rule], self.env)
                self.rule_created = False
            except Exception as exc:
                errors.append(f"task udev rule removal failed for {self.rule}: {exc}")
            try:
                _sudo(["udevadm", "control", "--reload"], self.env)
            except Exception as exc:
                errors.append(f"task udev rule reload failed: {exc}")
        if errors:
            raise RuntimeError("private USB cleanup incomplete: " + "; ".join(errors))


def _native_python_shim(bindir: Path) -> None:
    result = _run(
        [
            "/usr/bin/python3",
            "-c",
            "import socket,sys; sys.exit(0 if getattr(socket,'AF_VSOCK',None)==40 else 1)",
        ],
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("native creation helpers require /usr/bin/python3 with AF_VSOCK support")
    fd = os.open(bindir / "python3", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o700)
        stream.write('#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
        stream.flush()
        os.fsync(stream.fileno())


def _sudo_shim(directory: Path, env: dict[str, str]):
    bindir = directory / "bin"
    _private_directory(bindir)
    _native_python_shim(bindir)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    keys = (
        "PATH",
        "LD_LIBRARY_PATH",
        "USBMUXD_SOCKET",
        "USBMUXD_SOCKET_ADDRESS",
        "VPHONE_RCODESIGN",
        "VPHONE_APFS_KO",
    )
    arguments = ["/usr/bin/sudo", "-n", "/usr/bin/env", *[f"{key}={env[key]}" for key in keys if key in env]]
    shim = bindir / "sudo"
    shim.write_text(
        "#!/usr/bin/python3\nimport os,sys\nos.execv('/usr/bin/sudo', "
        + repr(arguments)
        + " + sys.argv[1:])\n"
    )
    shim.chmod(0o700)


def _completed(vm: Path) -> list[str]:
    try:
        markers = (vm / ".create-state").read_text().splitlines()
    except FileNotFoundError:
        return []
    if any(marker not in _STAGES for marker in markers):
        raise RuntimeError("upstream creation state contains an unknown stage")
    return [stage for stage in _STAGES if stage in markers]


def _archive_creation_logs(vm: Path, run_dir: Path) -> None:
    for name in (".runs-dfu", ".runs-sshrd"):
        source = vm / name
        if not source.is_dir() or source.is_symlink():
            continue
        destination = run_dir / f"previous-{name.lstrip('.')}"
        _private_directory(destination)
        for artifact in source.iterdir():
            if artifact.is_file() and not artifact.is_symlink():
                shutil.copy2(artifact, destination / artifact.name)


def _stop_creation_guest(vm: Path, cid: int, unit: str):
    try:
        pid = int((vm / ".qemu.pid").read_text().strip())
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError("creation did not record its final QEMU PID") from exc
    identity = _identity(pid)
    if not identity or not _in_scope(pid, unit) or pid not in _disk_users(vm / "Disk.img"):
        raise RuntimeError("final QEMU identity is not an owned process with this VM disk open")
    GuestClient(cid=cid, timeout=10).rpc("system.shutdown", {"force": True})
    deadline = time.monotonic() + 120
    while _identity_alive(identity) and time.monotonic() < deadline:
        time.sleep(0.25)
    if _identity_alive(identity):
        raise RuntimeError(
            "task-created guest did not shut down in 120 seconds; ownership remains with the creation scope"
        )


def _verified_guest_info(guest: GuestClient, expected_ios: str) -> dict:
    def canonical(version):
        if not isinstance(version, str) or not re.fullmatch(
            r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*", version
        ):
            return None
        parts = version.split(".")
        while len(parts) > 1 and parts[-1] == "0":
            parts.pop()
        return tuple(parts)

    info = guest.rpc("device.info", {})
    actual = info.get("ios_version") if isinstance(info, dict) else None
    expected = canonical(expected_ios)
    if expected is None or canonical(actual) != expected:
        raise RuntimeError(f"guest reports iOS {actual!r}; requested iOS {expected_ios}")
    return info


def _expected_migration_build(profile: DeviceProfile, pairing: dict) -> str:
    pairing_build = pairing.get("build")
    if not isinstance(pairing_build, str) or not pairing_build or pairing_build == "unknown":
        pairing_build = None

    filename = profile.main_firmware.filename
    match = re.search(r"_([^_]+)_Restore\.ipsw$", filename) if isinstance(filename, str) else None
    firmware_build = match.group(1) if match and match.group(1) != "unknown" else None
    if firmware_build is None:
        raise RuntimeError(f"profile {profile.id} firmware filename does not identify a build version")

    if pairing_build is not None and pairing_build != firmware_build:
        raise RuntimeError(
            f"compatibility catalog build {pairing_build!r} contradicts pinned profile firmware build "
            f"{firmware_build!r} for {profile.id}"
        )
    return firmware_build


def _migration_result_from_settings(reply: object) -> dict:
    if not isinstance(reply, dict) or not isinstance(reply.get("values"), dict):
        raise RuntimeError("guest settings.get returned an unsupported migration response")
    entry = reply["values"].get("DMLastMigrationResults")
    if entry is None:
        raise RuntimeError("guest settings.get is missing DMLastMigrationResults")
    if not isinstance(entry, dict) or not isinstance(entry.get("value"), dict):
        raise RuntimeError("guest settings.get returned an unsupported DMLastMigrationResults value")
    return entry["value"]


def _verify_migration_result(result: dict, expected_build: str) -> None:
    success = result.get("success")
    if success is False or (type(success) is int and success == 0):
        raise RuntimeError("guest DataMigrator reported migration success=false")
    if success is not True and not (type(success) is int and success == 1):
        raise RuntimeError("guest DMLastMigrationResults has an unsupported success value")

    actual_build = result.get("buildVersion")
    if not isinstance(actual_build, str) or not actual_build:
        raise RuntimeError("guest DMLastMigrationResults is missing a valid buildVersion")
    if actual_build != expected_build:
        raise RuntimeError(f"guest DataMigrator recorded build {actual_build!r}; expected {expected_build!r}")


def _verify_guest_migration(
    guest: GuestClient,
    setup: object,
    state_path: Path,
    state: dict,
    expected_build: str,
) -> dict:
    state.update(setup=setup, migration_expected_build=expected_build)
    state.pop("migration_result_read_error", None)
    state["migration_result"] = None
    _atomic_json(state_path, state)

    if not isinstance(setup, dict) or setup.get("data_migration_done") is not True:
        raise RuntimeError("guest first-boot data migration is not complete")
    if setup.get("pending") is not False:
        raise RuntimeError("guest setup assistant is still pending")

    try:
        reply = guest.rpc("settings.get", {"domain": "com.apple.migration"})
    except ProtocolError as exc:
        message = f"guest migration results could not be read with settings.get: {exc}"
        state["migration_result_read_error"] = message
        _atomic_json(state_path, state)
        raise RuntimeError(message) from exc

    try:
        result = _migration_result_from_settings(reply)
    except RuntimeError as exc:
        state["migration_result_read_error"] = str(exc)
        _atomic_json(state_path, state)
        raise

    state["migration_result"] = result
    _atomic_json(state_path, state)
    _verify_migration_result(result, expected_build)
    return result


def create_device(
    paths: Paths,
    name: str,
    model: str,
    ios: str,
    cid: int | None,
    scale: int | None,
    *,
    fresh_disk: bool = False,
    graphics_mode: str | None = None,
) -> dict:
    selected_mode, explicit_mode = select_graphics_mode(graphics_mode)
    _validate_name(name)
    profile = profile_for(model, ios)
    candidate, registered = _resolve_candidate(paths, name, profile, cid, scale)
    if registered:
        if fresh_disk:
            raise ValueError("fresh disk recovery is unavailable for a registered device")
        status = Runtime(paths, candidate).status()
        if explicit_mode:
            require_graphics_mode(name, recorded_graphics_mode(status), selected_mode)
        return status | {"cid": candidate.cid}
    _, pairing = _pairing(paths, model, ios)
    if not profile.firmware_metadata_ready:
        raise RuntimeError(f"profile {profile.id} does not have verified firmware metadata")
    _private_directory(paths.data)
    with (paths.data / ".creation.lock").open("a") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another guest creation owns the shared restore transport") from exc
        host_guard = _host_creation_lock(_native_env(paths))
        try:
            # Repeat under the host lock because another creation may have just registered.
            candidate, registered = _resolve_candidate(paths, name, profile, cid, scale)
            if registered:
                if fresh_disk:
                    raise ValueError("fresh disk recovery is unavailable for a registered device")
                status = Runtime(paths, candidate).status()
                if explicit_mode:
                    require_graphics_mode(name, recorded_graphics_mode(status), selected_mode)
                return status | {"cid": candidate.cid}
            old_receipt = _read_json(candidate.vm_path / ".limulator-create.json")
            if not explicit_mode and old_receipt is not None:
                recorded_mode = recorded_graphics_mode(old_receipt)
                if recorded_mode != "unknown":
                    selected_mode = recorded_mode
            if fresh_disk:
                _fresh_disk_allowed(paths, candidate, old_receipt)
            options = {"fresh_disk": True} if fresh_disk else {}
            if selected_mode != DEFAULT_GRAPHICS_MODE:
                options["graphics_mode"] = selected_mode
            return _create_locked(paths, candidate, profile, pairing, **options) | {"cid": candidate.cid}
        finally:
            os.close(host_guard)


def _create_locked(
    paths: Paths,
    candidate: Device,
    profile: DeviceProfile,
    pairing: dict,
    *,
    fresh_disk: bool = False,
    graphics_mode: str = DEFAULT_GRAPHICS_MODE,
) -> dict:
    _resource_guard()
    env = _native_env(paths)
    qemu_options = json.loads((paths.backend / "qemu/build/meson-info/intro-buildoptions.json").read_text())
    if not any(option.get("name") == "pvg" and option.get("value") == "enabled" for option in qemu_options):
        raise RuntimeError("backend QEMU is not yet configured with the required paravirtual GPU")
    for executable in (env["VC"], env["QEMU"], env["VPHONE_RCODESIGN"]):
        if not os.access(executable, os.X_OK):
            raise RuntimeError(f"required native tool is not built: {executable}")
    if shutil.disk_usage(paths.data).free < 80 * (1 << 30):
        raise RuntimeError(
            "guest creation requires at least 80 GiB free for firmware, restore trees, and preserved images"
        )
    return _pipeline(
        paths, candidate, profile, pairing, env, fresh_disk=fresh_disk, graphics_mode=graphics_mode
    )


def _first_boot_qmp_path(run_dir: Path) -> Path:
    _private_directory(run_dir)
    path = run_dir / "qmp.sock"
    if any(char in str(path) for char in (",", "\n", "\r")):
        raise ValueError("first-boot QMP path contains characters unsupported by QEMU's socket syntax")
    if len(os.fsencode(path)) > 103:
        raise ValueError("first-boot QMP path is too long for a Unix socket; shorten LIMULATOR_HOME")
    return path


def _pipeline(
    paths: Paths,
    candidate: Device,
    profile: DeviceProfile,
    pairing: dict,
    env: dict[str, str],
    *,
    fresh_disk: bool = False,
    graphics_mode: str = DEFAULT_GRAPHICS_MODE,
) -> dict:
    vm = candidate.vm_path
    _normalize_backend_controls(env)
    _require_cache_publication_complete(vm)
    specification = _creation_specification(candidate)
    state_path = vm / ".limulator-create.json"
    old = _read_json(state_path)
    if old and "cleanup_error" in old:
        raise RuntimeError(
            f"previous creation cleanup is unresolved for scope {old.get('scope', 'unknown')}; "
            f"inspect {state_path}: {old['cleanup_error']}"
        )
    previous_receipt = state_path.read_bytes() if fresh_disk and old else None
    if fresh_disk:
        _fresh_disk_allowed(paths, candidate, old)
    if vm.exists() and any(vm.iterdir()) and (not old or old.get("specification") != specification):
        raise RuntimeError(f"existing VM data has no matching creation receipt; preserving {vm}")
    expected_migration_build = _expected_migration_build(profile, pairing)
    if env.get("VPHONE_FIRST_BOOT_CHECK"):
        raise RuntimeError("VPHONE_FIRST_BOOT_CHECK is managed by Limulator during guest creation")
    first_boot_contract = _first_boot_check.backend_contract(paths.backend)
    historical_check = None
    if first_boot_contract["mode"] == "caller-check-v1" and "booted" in _completed(vm):
        historical_check = _first_boot_check.verify_proof(
            old.get("first_boot_check") if old else None,
            vm,
            specification,
            expected_migration_build,
            profile.id,
            contract=first_boot_contract,
            historical=True,
        )
    _private_directory(vm)
    state = dict(old) if old else {"specification": specification, "completed": []}
    state.pop("error", None)
    state.pop("first_boot_qmp", None)
    if first_boot_contract["mode"] == "caller-check-v1":
        state.pop("scope_invocation_id", None)
        state.pop("launcher", None)
    state["backend_first_boot_contract"] = first_boot_contract
    if historical_check is not None:
        state["first_boot_check_historical"] = historical_check
    state.update(
        state="creating",
        process=_identity(os.getpid()),
        graphics_policy=graphics_policy_metadata(graphics_mode, env),
        profile_id=profile.id,
        migration_expected_build=expected_migration_build,
        upstream_linux_tested=profile.upstream_linux_tested,
    )
    _atomic_json(state_path, state)
    unit = f"limulator-create-{os.getuid()}-{uuid.uuid4().hex[:8]}.scope"
    run_dir = Path(tempfile.mkdtemp(prefix="create-", dir=vm))
    state["run_dir"] = str(run_dir)
    env.update(VSOCK="vhost", VSOCK_DIR=str(run_dir / "vsock"))
    apply_graphics_environment(env, graphics_mode, run_dir / "census")
    state["graphics_policy"] = graphics_policy_metadata(graphics_mode, env)
    _atomic_json(state_path, state)
    trace_enabled = env.get("VPHONE_USB_TRACE") == "1"
    env["VPHONE_USB_TRACE"] = "1" if trace_enabled else "0"
    if trace_enabled:
        trace_path = run_dir / "usb-transport-trace.jsonl"
        env["VPHONE_USB_TRACE_FILE"] = str(trace_path)
        trace_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        with contextlib.suppress(OSError):
            trace_fd = os.open(trace_path, trace_flags, 0o600)
            os.close(trace_fd)
        print(f"USB transport trace: {trace_path}", file=sys.stderr, flush=True)
    else:
        env.pop("VPHONE_USB_TRACE_FILE", None)
    for directory in (run_dir / "vsock", run_dir / "census"):
        _private_directory(directory)
    helpers = _USBHelpers(paths, run_dir, env, unit)
    disk_fd = None
    previous_disk_fd = None
    fresh_record = None
    process = None
    scope_owned = False
    scope_invocation_id = None
    try:
        qmp_path = _first_boot_qmp_path(run_dir)
        env["VPHONE_FIRST_BOOT_QMP"] = str(qmp_path)
        state.update(run_dir=str(run_dir), first_boot_qmp=str(qmp_path))
        _atomic_json(state_path, state)
        catalog = _firmware_cache(env)
        env["VSETUP_CACHE"] = str(catalog)
        ios_file = catalog / profile.main_firmware.filename
        cloud_file = catalog / profile.cloud_os_firmware.filename
        state["firmware"] = _verified_firmware_stage(
            vm, ((ios_file, profile.main_firmware), (cloud_file, profile.cloud_os_firmware))
        )
        env.update(
            IPSW_PATH=str(ios_file),
            IPSW_URL=str(ios_file),
            CLOUDOS_IPSW_URL=str(cloud_file),
            CID=str(candidate.cid),
        )
        script = paths.backend / "setup/create-vm.sh"
        script_source = script.read_text()
        if "VPHONE_CREATOR_USB_READY" not in script_source:
            raise RuntimeError(
                "backend sources lack scoped USB creation support; bootstrap them before creating a guest"
            )
        if "VPHONE_FIRST_BOOT_QMP" not in script_source:
            raise RuntimeError(
                "backend sources lack the private first-boot QMP hook; bootstrap them before creating a guest"
            )
        baseline = _unit_states()
        state.update(usb_baseline=baseline, scope=unit, run_dir=str(run_dir))
        _atomic_json(state_path, state)
        if (vm / "Disk.img").exists() and "bundle" not in _completed(vm):
            raise RuntimeError(
                "an interrupted bundle has no completion marker; preserving its disk for inspection"
            )
        if "bundle" not in _completed(vm):
            _run(
                [
                    sys.executable,
                    str(paths.backend / "setup/lib/make-bundle.py"),
                    str(vm),
                    candidate.model,
                    str(candidate.scale),
                ],
                env=env,
                timeout=60,
            )
            for rom in ("OpenAVB", "OpenSEP"):
                shutil.copy2(paths.backend / rom / f"{rom}.bin", vm / f"{rom}.bin")
            candidate.validate_vm()
            _apply_profile_configuration(vm, profile, int(candidate.scale))
            with (vm / ".create-state").open("a") as stream:
                stream.write("bundle\n")
        candidate.validate_vm()
        disk_fd = _lock_disk(vm / "Disk.img")
        _require_cache_publication_complete(vm)
        _bind_vm_ecid(vm, env)
        resumed = _resume_completed_boot(paths, candidate, old, run_dir, disk_fd)
        if resumed is not None:
            state.update(verification_resume=resumed, completed=_completed(vm), stage="booted")
            _atomic_json(state_path, state)
        if fresh_disk:
            previous_disk_fd = disk_fd
            disk_fd, fresh_record = _prepare_fresh_disk(
                vm, state, state_path, previous_disk_fd, previous_receipt
            )
        _apply_profile_configuration(vm, profile, int(candidate.scale))
        _archive_creation_logs(vm, run_dir)
        deadline = time.monotonic() + 3 * 3600
        if first_boot_contract["mode"] == "caller-check-v1":
            check = _first_boot_check.prepare(
                run_dir,
                vm,
                specification,
                profile.id,
                expected_migration_build,
                state["process"],
                unit,
                env["QEMU"],
                first_boot_contract,
                deadline,
                env,
            )
            env["VPHONE_FIRST_BOOT_CHECK"] = check["launcher"]
            state["first_boot_check"] = check
            _atomic_json(state_path, state)
        helpers.start()
        _sudo_shim(run_dir, env)
        command = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--expand-environment=no",
            "--unit",
            unit,
            "-p",
            "MemoryHigh=32G",
            "-p",
            "MemoryMax=40G",
            "-p",
            "MemorySwapMax=4G",
            "--",
            "bash",
            str(script),
            "--device",
            candidate.model,
            "--ios",
            candidate.os_version,
            "--name",
            candidate.name,
            "--cid",
            str(candidate.cid),
            "--scale",
            str(candidate.scale),
            "-y",
        ]
        log_path = run_dir / "creation.log"
        state["log"] = str(log_path)
        mount_baseline = _creation_cleanup.mounts()
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                command,
                env=env,
                cwd=paths.backend,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                pass_fds=(disk_fd,),
            )
        state["launcher"] = _identity(process.pid)
        _atomic_json(state_path, state)
        previous = None
        while process.poll() is None:
            if not scope_owned:
                scope_invocation_id = _capture_creation_scope_invocation(process.pid, unit, env)
                scope_owned = scope_invocation_id is not None
                if scope_owned:
                    state["scope_invocation_id"] = scope_invocation_id
                    _atomic_json(state_path, state)
            if scope_owned and fresh_record is not None and previous_disk_fd is not None:
                fresh_record["state"] = "restore-started"
                _atomic_json(state_path, state)
                os.close(previous_disk_fd)
                previous_disk_fd = None
            helpers.poll()
            completed = _completed(vm)
            if completed != previous:
                state.update(
                    completed=completed,
                    stage=next((item for item in _STAGES if item not in completed), "verification"),
                )
                _atomic_json(state_path, state)
                print(
                    f"Creating {candidate.name}: {state['stage']}; log: {log_path}",
                    file=sys.stderr,
                    flush=True,
                )
                previous = completed
            if time.monotonic() >= deadline:
                raise RuntimeError(f"guest creation exceeded its three-hour bound; inspect {log_path}")
            time.sleep(0.5)
        if process.returncode:
            raise RuntimeError(f"upstream creation exited with status {process.returncode}; see {log_path}")
        if "booted" not in _completed(vm):
            raise RuntimeError("upstream returned without a completed first boot")
        if first_boot_contract["mode"] == "caller-check-v1":
            state["first_boot_check"] = _first_boot_check.verify_proof(
                state["first_boot_check"], vm, specification, expected_migration_build, profile.id,
                contract=first_boot_contract,
            )
            _atomic_json(state_path, state)
        guest = GuestClient(cid=candidate.cid, timeout=30)
        state.update(stage="verification-setup")
        _atomic_json(state_path, state)
        setup = guest.rpc("setup.status", {})
        state.update(stage="verification-migration", setup=setup)
        _atomic_json(state_path, state)
        migration_result = _verify_guest_migration(
            guest,
            setup,
            state_path,
            state,
            expected_migration_build,
        )
        state.update(stage="verification-unlock", migration_result=migration_result)
        _atomic_json(state_path, state)
        unlock = GuestClient(cid=candidate.cid, timeout=120).rpc("screen.unlock", {"timeout": 60})
        if not isinstance(unlock, dict) or unlock.get("locked") is not False:
            raise RuntimeError("guest did not confirm an unlocked screen")
        state.update(stage="verification-device-info", unlock=unlock)
        _atomic_json(state_path, state)
        info = _verified_guest_info(guest, candidate.os_version)
        state.update(
            completed=_completed(vm),
            setup=setup,
            migration_result=migration_result,
            guest_info=info,
            unlock=unlock,
            stage="shutdown",
        )
        _atomic_json(state_path, state)
        _stop_creation_guest(vm, candidate.cid, unit)
        _apply_profile_configuration(vm, profile, int(candidate.scale))
        screen = plistlib.loads(_read_vm_identity(vm / "config.plist", 1 << 20))["screenConfig"]
        width, height = map(int, (vm / "gfx-res").read_text().strip().split("x"))
        device = Device(
            candidate.name,
            vm,
            candidate.model,
            candidate.os_version,
            candidate.cid,
            width,
            height,
            candidate.scale or screen["scale"],
        )
    except BaseException as exc:
        state.update(state="error", error=str(exc), completed=_completed(vm))
        _atomic_json(state_path, state)
        raise
    finally:
        cleanup_complete = False
        cleanup_errors = []
        try:
            try:
                if process is not None:
                    _cleanup_creation_scope(
                        unit,
                        env,
                        state["process"],
                        mount_baseline,
                        scope_owned=scope_owned,
                        expected_invocation_id=scope_invocation_id,
                    )
            except BaseException as exc:
                cleanup_errors.append(("scope", exc))
            try:
                helpers.close()
            except BaseException as exc:
                cleanup_errors.append(("private USB", exc))
            if cleanup_errors:
                state.update(
                    state="error",
                    cleanup_error="; ".join(
                        f"{label} cleanup failed: {exc}" for label, exc in cleanup_errors
                    ),
                )
                _atomic_json(state_path, state)
                raise cleanup_errors[0][1]
            cleanup_complete = True
        finally:
            try:
                if cleanup_complete and fresh_record is not None and previous_disk_fd is not None:
                    _rollback_fresh_disk(vm, fresh_record, previous_disk_fd, disk_fd)
                    _atomic_json(state_path, state)
            finally:
                if previous_disk_fd is not None and previous_disk_fd != disk_fd:
                    os.close(previous_disk_fd)
                if disk_fd is not None:
                    os.close(disk_fd)
    runtime = Runtime(paths, device)
    try:
        Registry(paths).register(device)
        state.update(state="created", stage="owned-runtime-start")
        _atomic_json(state_path, state)
        result = runtime.start(wait_timeout=600, graphics_mode=graphics_mode)
        if result["state"] != "running":
            raise RuntimeError(f"created guest did not reach its owned runtime API: {result}")
        final_unlock = runtime.guest(timeout=120).rpc("screen.unlock", {"timeout": 60})
        if not isinstance(final_unlock, dict) or final_unlock.get("locked") is not False:
            raise RuntimeError("owned runtime did not confirm an unlocked screen")
        runtime.guest(timeout=30).rpc("input.button", {"name": "home"})
        _private_directory(paths.captures)
        capture = paths.captures / f"{candidate.name}-created-home-{time.time_ns()}.png"
        with runtime.qmp() as qmp:
            qmp.screenshot(capture)
        if not capture.is_file() or capture.stat().st_size < 8:
            raise RuntimeError("owned runtime did not write its real home-screen framebuffer")
        state.update(
            state="ready",
            stage="ready",
            runtime=result,
            unlock=final_unlock,
            home_capture=str(capture),
            usb_after=_unit_states(),
        )
        _atomic_json(state_path, state)
        return result | {"home_capture": str(capture)}
    except BaseException as exc:
        state.update(state="error", stage="owned-runtime-start", error=str(exc), runtime=runtime.status())
        _atomic_json(state_path, state)
        raise
