"""Create a guest using public firmware and task-owned restore helpers."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
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
    _resource_guard,
    _validate_name,
)
from .profiles import DeviceProfile, FirmwareDescriptor, profile_for
from .protocols import GuestClient

_STAGES = ("sanity", "bundle", "firmware", "gpu-driver", "prepare", "patch", "restore", "cfw", "booted")
_HOST_CREATION_LOCK = Path("/run/lock/limulator-create.lock")


def _run(command, *, env=None, timeout=30, check=True, input=None):
    result = subprocess.run(command, env=env, input=input, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{command[0]} exited with status {result.returncode}: {result.stderr.strip()}")
    return result


def _native_env(paths: Paths) -> dict[str, str]:
    env = os.environ.copy()
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
        VPHONE_RCODESIGN=str(paths.prefix / "bin/rcodesign"),
        VPHONE_APFS_KO=str(paths.backend / ".build/linux-apfs-rw/apfs.ko"),
        VC=str(paths.backend / "linux-create/vphone-cli"),
        QEMU=str(paths.backend / "qemu/build/qemu-system-aarch64"),
        GFX="1",
        WINDOW="0",
        MEM="8G",
        KEEP="1",
        VSETUP_ASSUME_YES="1",
    )
    return env


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
    original = path.read_bytes()
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


class _USBHelpers:
    def __init__(self, paths: Paths, directory: Path, env: dict[str, str], unit: str):
        self.paths, self.directory, self.env, self.unit = paths, directory, env, unit
        self.rule = Path(f"/run/udev/rules.d/99-limulator-{os.getuid()}-{uuid.uuid4().hex[:8]}.rules")
        self.daemon = None
        self.owned_ports: set[int] = set()
        self.seen_devices: set[tuple[str, int, int]] = set()
        self.rule_created = False
        self.daemon_identity = None
        self.attach_failures = 0

    def start(self):
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
        socket_path = self.directory / "usbmuxd.sock"
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
        self.env["LIMULATOR_CREATOR_USB_READY"] = "1"

    def poll(self):
        if not _identity_alive(self.daemon_identity):
            raise RuntimeError("task-owned usbmuxd exited during creation")
        physical = [path for path in _apple_devices() if "/vhci_hcd." not in path]
        if physical:
            raise RuntimeError(
                "a physical Apple device appeared during restore; preserving it and stopping this creation"
            )
        owners = _listener_pids()
        if not owners:
            return
        if any(not _in_scope(pid, self.unit) for pid in owners):
            raise RuntimeError("USB/IP listener does not belong to this creation's exact systemd scope")
        ports = _vhci_ports()
        foreign = set(ports) - self.owned_ports
        if foreign:
            raise RuntimeError(f"unowned USB/IP imports appeared during creation: {sorted(foreign)}")
        if not ports:
            remote = _run(["usbip", "list", "-r", "127.0.0.1"], env=self.env, check=False, timeout=5)
            if not re.search(r"^\s*1-1:.*\(05ac:[0-9a-f]{4}\)", remote.stdout, re.MULTILINE | re.IGNORECASE):
                return
            attached = _sudo(
                ["usbip", "attach", "-r", "127.0.0.1", "-b", "1-1"], self.env, check=False, timeout=10
            )
            if attached.returncode:
                self.attach_failures += 1
                if self.attach_failures >= 20:
                    raise RuntimeError(
                        f"task-owned USB/IP import failed after bounded retries: {attached.stderr.strip()}"
                    )
                return
            self.attach_failures = 0
            current = _vhci_ports()
            deadline = time.monotonic() + 5
            while not current and time.monotonic() < deadline:
                time.sleep(0.05)
                current = _vhci_ports()
            if len(current) != 1:
                raise RuntimeError("could not identify this task's newly imported virtual USB port")
            self.owned_ports.update(current)
            ports = current
        for local in ports.values():
            if local == "0-0":
                continue
            node = Path("/sys/bus/usb/devices") / local
            with contextlib.suppress(FileNotFoundError):
                if (node / "idVendor").read_text().strip() != "05ac":
                    raise RuntimeError("owned USB/IP port contains an unexpected vendor")
                bus, device = int((node / "busnum").read_text()), int((node / "devnum").read_text())
                key = (local, bus, device)
                if key in self.seen_devices:
                    continue
                usb = Path(f"/dev/bus/usb/{bus:03d}/{device:03d}")
                info = usb.stat()
                if info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o600:
                    _signal_owned(self.daemon_identity, signal.SIGUSR2)
                    self.seen_devices.add(key)

    def close(self):
        if self.daemon is not None and _identity_alive(self.daemon_identity):
            _signal_owned(self.daemon_identity, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.daemon.wait(timeout=10)
        for port in self.owned_ports & _vhci_ports().keys():
            _sudo(["usbip", "detach", "-p", str(port)], self.env, check=False)
        if self.rule_created:
            _sudo(["rm", "--", self.rule], self.env)
            _sudo(["udevadm", "control", "--reload"], self.env)


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


def create_device(
    paths: Paths,
    name: str,
    model: str,
    ios: str,
    cid: int | None,
    scale: int | None,
    *,
    fresh_disk: bool = False,
) -> dict:
    _validate_name(name)
    profile = profile_for(model, ios)
    candidate, registered = _resolve_candidate(paths, name, profile, cid, scale)
    if registered:
        if fresh_disk:
            raise ValueError("fresh disk recovery is unavailable for a registered device")
        return Runtime(paths, candidate).status() | {"cid": candidate.cid}
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
                return Runtime(paths, candidate).status() | {"cid": candidate.cid}
            if fresh_disk:
                _fresh_disk_allowed(
                    paths, candidate, _read_json(candidate.vm_path / ".limulator-create.json")
                )
            options = {"fresh_disk": True} if fresh_disk else {}
            return _create_locked(paths, candidate, profile, pairing, **options) | {"cid": candidate.cid}
        finally:
            os.close(host_guard)


def _create_locked(
    paths: Paths, candidate: Device, profile: DeviceProfile, pairing: dict, *, fresh_disk: bool = False
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
    return _pipeline(paths, candidate, profile, pairing, env, fresh_disk=fresh_disk)


def _pipeline(
    paths: Paths,
    candidate: Device,
    profile: DeviceProfile,
    pairing: dict,
    env: dict[str, str],
    *,
    fresh_disk: bool = False,
) -> dict:
    vm = candidate.vm_path
    specification = _creation_specification(candidate)
    state_path = vm / ".limulator-create.json"
    old = _read_json(state_path)
    previous_receipt = state_path.read_bytes() if fresh_disk and old else None
    if fresh_disk:
        _fresh_disk_allowed(paths, candidate, old)
    if vm.exists() and any(vm.iterdir()) and (not old or old.get("specification") != specification):
        raise RuntimeError(f"existing VM data has no matching creation receipt; preserving {vm}")
    _private_directory(vm)
    state = dict(old) if old else {"specification": specification, "completed": []}
    state.pop("error", None)
    state.update(
        state="creating",
        process=_identity(os.getpid()),
        profile_id=profile.id,
        upstream_linux_tested=profile.upstream_linux_tested,
    )
    _atomic_json(state_path, state)
    unit = f"limulator-create-{os.getuid()}-{uuid.uuid4().hex[:8]}.scope"
    run_dir = Path(tempfile.mkdtemp(prefix="create-", dir=vm))
    env.update(VSOCK="vhost", VSOCK_DIR=str(run_dir / "vsock"), REIMS_VGPU_CENSUS_DIR=str(run_dir / "census"))
    for directory in (run_dir / "vsock", run_dir / "census"):
        _private_directory(directory)
    helpers = _USBHelpers(paths, run_dir, env, unit)
    disk_fd = None
    previous_disk_fd = None
    fresh_record = None
    process = None
    scope_owned = False
    try:
        catalog = Path.home() / ".cache/vphone"
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
        if "LIMULATOR_CREATOR_USB_READY" not in script.read_text():
            raise RuntimeError(
                "backend lacks the scoped USB creation patch; bootstrap it before creating a guest"
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
        deadline = time.monotonic() + 3 * 3600
        previous = None
        while process.poll() is None:
            scope_owned = scope_owned or _in_scope(process.pid, unit)
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
        guest = GuestClient(cid=candidate.cid, timeout=30)
        setup = guest.rpc("setup.status", {})
        unlock = guest.rpc("screen.unlock", {})
        if not isinstance(setup, dict) or setup.get("data_migration_done") is not True:
            raise RuntimeError("guest first-boot data migration is not complete")
        if setup.get("pending") is not False:
            raise RuntimeError("guest setup assistant is still pending")
        info = _verified_guest_info(guest, candidate.os_version)
        if not isinstance(unlock, dict) or unlock.get("locked") is not False:
            raise RuntimeError("guest did not confirm an unlocked screen")
        state.update(completed=_completed(vm), setup=setup, guest_info=info, unlock=unlock, stage="shutdown")
        _atomic_json(state_path, state)
        _stop_creation_guest(vm, candidate.cid, unit)
        _apply_profile_configuration(vm, profile, int(candidate.scale))
        os.close(disk_fd)
        disk_fd = None
        with (vm / "config.plist").open("rb") as stream:
            screen = plistlib.load(stream)["screenConfig"]
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
        Registry(paths).register(device)
        state.update(state="created", stage="owned-runtime-start")
        _atomic_json(state_path, state)
    except BaseException as exc:
        state.update(state="error", error=str(exc), completed=_completed(vm))
        _atomic_json(state_path, state)
        raise
    finally:
        if scope_owned:
            _run(["systemctl", "--user", "kill", "--signal=SIGTERM", unit], check=False)
            until = time.monotonic() + 15
            while _disk_users(vm / "Disk.img") and time.monotonic() < until:
                time.sleep(0.25)
            if _disk_users(vm / "Disk.img"):
                _run(["systemctl", "--user", "kill", "--signal=SIGKILL", unit], check=False)
                until = time.monotonic() + 5
                while _disk_users(vm / "Disk.img") and time.monotonic() < until:
                    time.sleep(0.1)
        try:
            helpers.close()
        finally:
            try:
                if fresh_record is not None and previous_disk_fd is not None:
                    _rollback_fresh_disk(vm, fresh_record, previous_disk_fd, disk_fd)
                    _atomic_json(state_path, state)
            finally:
                if previous_disk_fd is not None and previous_disk_fd != disk_fd:
                    os.close(previous_disk_fd)
                if disk_fd is not None:
                    os.close(disk_fd)
    runtime = Runtime(paths, device)
    try:
        result = runtime.start(wait_timeout=600)
        if result["state"] != "running":
            raise RuntimeError(f"created guest did not reach its owned runtime API: {result}")
        final_unlock = runtime.guest(timeout=30).rpc("screen.unlock", {})
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
