"""Validated device registration and exclusive, persistent QEMU ownership."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from .protocols import GuestClient, ProtocolError, QMPClient

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_GIB = 1 << 30


@dataclass(frozen=True)
class Paths:
    data: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "LIMULATOR_HOME", str(Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")) / "limulator")
            )
        )
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", Path(self.data).expanduser().resolve())

    @property
    def backend(self) -> Path:
        return (
            Path(os.environ.get("LIMULATOR_BACKEND", self.data / "backends/vphone-linux"))
            .expanduser()
            .resolve()
        )

    @property
    def prefix(self) -> Path:
        return Path(os.environ.get("LIMULATOR_PREFIX", self.data / "tools")).expanduser().resolve()

    @property
    def vms(self) -> Path:
        return self.data / "vms"

    @property
    def registry(self) -> Path:
        return self.data / "devices"

    @property
    def runs(self) -> Path:
        return self.data / "runs"

    @property
    def captures(self) -> Path:
        return self.data / "captures"


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("device name must contain 1-64 ASCII letters, digits, underscores or hyphens")


@dataclass(frozen=True)
class Device:
    name: str
    vm_path: Path
    model: str
    os_version: str
    cid: int = 41
    width: int = 1398
    height: int = 2034
    scale: float = 3

    def __post_init__(self) -> None:
        _validate_name(self.name)
        object.__setattr__(self, "vm_path", Path(self.vm_path).expanduser().resolve())
        if isinstance(self.cid, bool) or not isinstance(self.cid, int) or not 3 <= self.cid <= 0xFFFFFFFE:
            raise ValueError("CID must be an integer between 3 and 4294967294")
        for key in ("width", "height"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32768:
                raise ValueError(f"{key} must be an integer between 1 and 32768")
        if (
            isinstance(self.scale, bool)
            or not isinstance(self.scale, (int, float))
            or not math.isfinite(self.scale)
            or self.scale <= 0
        ):
            raise ValueError("scale must be finite and positive")
        for key in ("model", "os_version"):
            value = getattr(self, key)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 128
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError(f"{key} must be a nonempty printable string")

    def validate_vm(self) -> None:
        if any(char in os.fspath(self.vm_path) for char in (",", "\n", "\r")):
            raise ValueError("VM path contains characters unsupported by the backend drive syntax")
        if not self.vm_path.is_dir():
            raise ValueError(f"VM directory does not exist: {self.vm_path}")
        disk = self.vm_path / "Disk.img"
        if not disk.is_file() or disk.stat().st_size == 0:
            raise ValueError(f"VM requires a nonempty regular Disk.img: {disk}")
        config = self.vm_path / "config.plist"
        try:
            if config.stat().st_size > 1 << 20:
                raise ValueError("config.plist exceeds 1 MiB")
            with config.open("rb") as stream:
                value = plistlib.load(stream)
        except (OSError, plistlib.InvalidFileException, ValueError, TypeError, ExpatError) as exc:
            raise ValueError(f"invalid VM config.plist: {exc}") from exc
        if not isinstance(value, dict) or value.get("diskImage", "Disk.img") != "Disk.img":
            raise ValueError("config.plist must describe the backend's Disk.img")
        if value.get("platformType", "vresearch101") != "vresearch101":
            raise ValueError("config.plist platformType must be vresearch101")


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError(f"runtime directory is not owned by this user: {path}")
    path.chmod(0o700)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 1 << 20:
            raise ValueError(f"JSON file exceeds 1 MiB: {path}")
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


class Registry:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def get(self, name: str) -> Device:
        _validate_name(name)
        raw = _read_json(self.paths.registry / f"{name}.json")
        if raw is None:
            raise ValueError(f"unknown device: {name}")
        try:
            device = Device(**raw)
        except TypeError as exc:
            raise ValueError(f"invalid device registration {name}: {exc}") from exc
        if device.name != name:
            raise ValueError(f"device registration name does not match its filename: {name}")
        device.validate_vm()
        return device

    def list(self) -> list[Device]:
        if not self.paths.registry.exists():
            return []
        return [self.get(path.stem) for path in sorted(self.paths.registry.glob("*.json"))]

    def register(self, device: Device) -> Device:
        device.validate_vm()
        _private_directory(self.paths.registry)
        with (self.paths.registry / ".lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            disk = (device.vm_path / "Disk.img").stat()
            for other in self.list():
                if other.name == device.name:
                    if other == device:
                        return device
                    raise ValueError(f"device name is already registered: {device.name}")
                if other.cid == device.cid:
                    raise ValueError(f"CID {device.cid} is already registered to {other.name}")
                existing = (other.vm_path / "Disk.img").stat()
                if (disk.st_dev, disk.st_ino) == (existing.st_dev, existing.st_ino):
                    raise ValueError(f"VM disk is already registered to {other.name}")
            value = asdict(device)
            value["vm_path"] = os.fspath(device.vm_path)
            _atomic_json(self.paths.registry / f"{device.name}.json", value)
        return device


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _identity(pid: int) -> dict[str, Any] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2 :].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "start_ticks": int(fields[19]), "boot_id": _boot_id()}
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
        return None


def _identity_alive(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or isinstance(value.get("pid"), bool)
        or not isinstance(value.get("pid"), int)
    ):
        return False
    return value == _identity(value["pid"])


def _disk_users(disk: Path) -> list[int]:
    target = disk.stat()
    users = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    info = fd.stat()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                    users.append(int(process.name))
                    break
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return users


def _lock_disk(disk: Path) -> int:
    fd = os.open(disk, os.O_RDONLY | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Disk.img must be a regular file")
        opened, current = os.fstat(fd), disk.stat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("Disk.img changed while acquiring ownership")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"VM disk is already owned by another runtime: {disk}") from exc
        users = _disk_users(disk)
        if users:
            raise RuntimeError(f"VM disk is already open by process IDs {users}: {disk}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _resource_guard() -> None:
    values = {
        line.split(":")[0]: int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if ":" in line and len(line.split()) >= 2
    }
    if values.get("MemAvailable", 0) < 32 * _GIB or values.get("SwapFree", 0) < 4 * _GIB:
        raise RuntimeError("guest startup requires MemAvailable >= 32 GiB and SwapFree >= 4 GiB")
    if not shutil.which("systemd-run"):
        raise RuntimeError("systemd-run is required to contain guest and GPU memory use")
    available = os.statvfs("/dev/shm")
    if available.f_bavail * available.f_frsize < 8 * _GIB:
        raise RuntimeError("guest startup requires at least 8 GiB free in /dev/shm")


class Runtime:
    def __init__(self, paths: Paths, device: Device) -> None:
        self.paths, self.device = paths, device
        self.runtime_path = paths.runs / device.name / "runtime.json"

    def _metadata(self) -> dict[str, Any]:
        value = _read_json(self.runtime_path) or {}
        if value and value.get("name") != self.device.name:
            raise RuntimeError("runtime metadata belongs to another device")
        return value

    @property
    def qmp_path(self) -> Path:
        value = self._metadata()
        if not value.get("run_dir"):
            raise RuntimeError(f"device has no runtime socket: {self.device.name}")
        directory = Path(value["run_dir"])
        if directory.parent.resolve() != self.runtime_path.parent.resolve():
            raise RuntimeError("runtime socket is outside this device's private directory")
        return directory / "qmp.sock"

    def guest(self, timeout: float = 60) -> GuestClient:
        if not _identity_alive(self._metadata().get("process")):
            raise RuntimeError("device has no live owned QEMU process")
        return GuestClient(cid=self.device.cid, timeout=timeout)

    def qmp(self) -> QMPClient:
        value = self._metadata()
        if not _identity_alive(value.get("process")):
            raise RuntimeError("device has no live owned QEMU process")
        return QMPClient(self.qmp_path)

    def status(self) -> dict[str, Any]:
        backend = self.paths.backend
        try:
            revision = subprocess.check_output(
                ["git", "-C", os.fspath(backend), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            revision = None
        ready = (backend / "run-vphone.sh").is_file() and os.access(
            backend / "qemu/build/qemu-system-aarch64", os.X_OK
        )
        result = {
            "name": self.device.name,
            "state": "stopped",
            "device": {
                key: getattr(self.device, key) for key in ("model", "os_version", "width", "height", "scale")
            },
            "backend": {"revision": revision, "ready": ready},
        }
        try:
            value = self._metadata()
            if _identity_alive(value.get("process")):
                result["state"] = "booting"
                try:
                    health = json.loads(self.guest(timeout=1).request("GET", "/v1/health"))
                    if (
                        isinstance(health, dict)
                        and health.get("status") == "ok"
                        and _identity_alive(value.get("process"))
                    ):
                        result["state"] = "running"
                except (OSError, ProtocolError, ValueError):
                    pass
            elif value.get("state") == "booting" and _identity_alive(value.get("supervisor")):
                result["state"] = "booting"
            elif value.get("state") == "error":
                result.update(state="error", error=value.get("error", "guest process exited"))
        except (OSError, ValueError, RuntimeError) as exc:
            result.update(state="error", error=str(exc))
        return result

    def start(self, window: bool = False, wait_timeout: float = 0) -> dict[str, Any]:
        if not math.isfinite(wait_timeout) or wait_timeout < 0:
            raise ValueError("wait_timeout must be finite and nonnegative")
        self.device.validate_vm()
        _private_directory(self.runtime_path.parent)
        with (self.runtime_path.parent / ".start.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            value = self._metadata()
            if _identity_alive(value.get("supervisor")) or _identity_alive(value.get("process")):
                raise RuntimeError(f"device is already started: {self.device.name}")
            if not self.status()["backend"]["ready"]:
                raise RuntimeError(f"backend is not built: {self.paths.backend}")
            _resource_guard()
            fd = _lock_disk(self.device.vm_path / "Disk.img")
            try:
                run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=self.runtime_path.parent))
                if any(char in os.fspath(run_dir) for char in (",", "\n", "\r")):
                    raise RuntimeError(
                        "LIMULATOR_HOME contains characters unsupported by the QMP socket syntax"
                    )
                if len(os.fsencode(run_dir / "qmp.sock")) > 103:
                    raise RuntimeError("runtime path is too long for a Unix socket; shorten LIMULATOR_HOME")
                command = [
                    sys.executable,
                    "-m",
                    "limulator.lifecycle",
                    "_supervise",
                    "--data",
                    os.fspath(self.paths.data),
                    "--name",
                    self.device.name,
                    "--run-dir",
                    os.fspath(run_dir),
                    "--lock-fd",
                    str(fd),
                ]
                if window:
                    command.append("--window")
                env = os.environ.copy()
                env["LIMULATOR_BACKEND"] = os.fspath(self.paths.backend)
                with (run_dir / "supervisor.log").open("ab", buffering=0) as log:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                        pass_fds=(fd,),
                        env=env,
                    )
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    value = self._metadata()
                    if value.get("run_dir") == os.fspath(run_dir):
                        break
                    if process.poll() is not None:
                        raise RuntimeError(f"runtime supervisor failed; see {run_dir / 'supervisor.log'}")
                    time.sleep(0.05)
                else:
                    raise RuntimeError(f"runtime supervisor has not acknowledged startup; see {run_dir}")
            finally:
                os.close(fd)
        deadline = time.monotonic() + wait_timeout
        while True:
            result = self.status()
            if result["state"] != "booting" or time.monotonic() >= deadline:
                return result
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def stop(self, timeout: float = 90) -> dict[str, Any]:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        value = self._metadata()
        owned = value.get("process")
        if not _identity_alive(owned):
            if _identity_alive(value.get("supervisor")):
                raise RuntimeError(
                    "guest is still starting and has no confirmed QEMU identity; retry stop shortly"
                )
            return self.status()
        deadline = time.monotonic() + timeout
        errors = []
        try:
            self.guest(timeout=max(0.1, min(5, timeout))).rpc("system.shutdown", {"force": True})
        except (OSError, ProtocolError) as exc:
            errors.append(f"guest shutdown: {exc}")
        while _identity_alive(owned) and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        forced = _identity_alive(owned)
        if forced:
            try:
                with QMPClient(self.qmp_path, timeout=5) as client:
                    if not _identity_alive(owned):
                        raise RuntimeError("owned process exited before QMP quit")
                    client.command("quit")
            except (OSError, ProtocolError, RuntimeError) as exc:
                errors.append(f"QMP quit: {exc}")
            deadline = time.monotonic() + 10
            while _identity_alive(owned) and time.monotonic() < deadline:
                time.sleep(0.1)
        if _identity_alive(owned):
            raise RuntimeError("owned QEMU did not exit after bounded shutdown: " + "; ".join(errors))
        deadline = time.monotonic() + 3
        while _identity_alive(value.get("supervisor")) and time.monotonic() < deadline:
            time.sleep(0.05)
        result = self.status()
        result["forced"] = forced
        if errors:
            result["shutdown_notes"] = errors
        return result


def _is_descendant(pid: int, parent: int) -> bool:
    for _ in range(64):
        if pid == parent:
            return True
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            pid = int(raw[raw.rfind(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        if pid <= 1:
            break
    return False


def _supervise(args: argparse.Namespace) -> int:
    paths = Paths(Path(args.data))
    device = Registry(paths).get(args.name)
    runtime = Runtime(paths, device)
    run_dir = Path(args.run_dir).resolve()
    if run_dir.parent != runtime.runtime_path.parent.resolve() or run_dir.is_symlink():
        raise RuntimeError("invalid private run directory")
    disk = (device.vm_path / "Disk.img").stat()
    locked = os.fstat(args.lock_fd)
    if (disk.st_dev, disk.st_ino) != (locked.st_dev, locked.st_ino):
        raise RuntimeError("inherited ownership lock does not match registered Disk.img")
    fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for directory in (run_dir, run_dir / "vsock", run_dir / "census"):
        _private_directory(directory)
    metadata = {
        "name": device.name,
        "run_dir": os.fspath(run_dir),
        "state": "booting",
        "supervisor": _identity(os.getpid()),
        "disk": {"dev": disk.st_dev, "ino": disk.st_ino},
    }
    _atomic_json(runtime.runtime_path, metadata)
    env = os.environ.copy()
    env["PATH"] = os.fspath(paths.prefix / "bin") + os.pathsep + env.get("PATH", os.defpath)
    libraries = [os.fspath(path) for path in (paths.prefix / "lib", paths.prefix / "lib64") if path.is_dir()]
    if libraries:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            libraries + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else [])
        )
    env.update(
        GFX="1",
        WINDOW="1" if args.window else "0",
        CID=str(device.cid),
        MEM="8G",
        VSOCK="vhost",
        VSOCK_DIR=os.fspath(run_dir / "vsock"),
        REIMS_VGPU_CENSUS_DIR=os.fspath(run_dir / "census"),
        REIMS_VGPU_WINDOW_CONTROL=os.fspath(run_dir / "window.sock"),
        QEMU=os.fspath(paths.backend / "qemu/build/qemu-system-aarch64"),
    )
    # Upstream's toolbar chooses a shared socket. The local console supplies controls.
    env["TOOLBAR"] = "0"
    command = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--expand-environment=no",
        "-p",
        "MemoryHigh=32G",
        "-p",
        "MemoryMax=40G",
        "-p",
        "MemorySwapMax=4G",
        "--",
        os.fspath(paths.backend / "run-vphone.sh"),
        os.fspath(device.vm_path),
        "-qmp",
        f"unix:{run_dir / 'qmp.sock'},server=on,wait=off",
    ]
    process = None
    try:
        with (run_dir / "qemu.log").open("ab", buffering=0) as log:
            process = subprocess.Popen(
                command,
                env=env,
                cwd=paths.backend,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                pass_fds=(args.lock_fd,),
            )
            metadata["launcher"] = _identity(process.pid)
            _atomic_json(runtime.runtime_path, metadata)
            while process.poll() is None:
                if not _identity_alive(metadata.get("process")):
                    for pid in _disk_users(device.vm_path / "Disk.img"):
                        if _is_descendant(pid, os.getpid()):
                            try:
                                executable = Path(f"/proc/{pid}/exe").resolve().name
                            except OSError:
                                continue
                            if executable.startswith("qemu-system-"):
                                metadata["process"] = _identity(pid)
                                _atomic_json(runtime.runtime_path, metadata)
                                break
                time.sleep(0.2)
            metadata["exit_code"] = process.returncode
            metadata["state"] = "stopped" if process.returncode == 0 else "error"
            if process.returncode:
                metadata["error"] = (
                    f"QEMU launcher exited with status {process.returncode}; see {run_dir / 'qemu.log'}"
                )
    except Exception as exc:
        metadata.update(state="error", error=str(exc))
    finally:
        # A metadata or probing failure must never relinquish a live guest's lock.
        if process is not None and process.poll() is None:
            process.wait()
        _atomic_json(runtime.runtime_path, metadata)
        os.close(args.lock_fd)
    return 0 if metadata["state"] == "stopped" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["_supervise"])
    parser.add_argument("--data", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--window", action="store_true")
    return _supervise(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
