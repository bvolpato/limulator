"""Bounded cleanup for one creation scope, executed with the existing sudo authority."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import json
import os
import pwd
import re
import select
import signal
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path


def mounts() -> list[dict]:
    result = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        separator = fields.index("-")

        def unescape(value):
            return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

        result.append(
            {
                "id": fields[0],
                "target": unescape(fields[4]),
                "type": fields[separator + 1],
                "source": unescape(fields[separator + 2]),
                "device": fields[2],
                "options": fields[5].split(","),
                "record": line,
            }
        )
    return result


def _identity(pid: int) -> dict | None:
    process = Path(f"/proc/{pid}")
    try:
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "start_ticks": int(fields[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "uid": process.stat().st_uid,
            "exe": os.readlink(process / "exe"),
            "argv": os.fsdecode((process / "cmdline").read_bytes()).split("\0")[:-1],
            "cgroup": (process / "cgroup").read_text(),
            "mount_namespace": os.readlink(process / "ns/mnt"),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def _scope_processes(group: str) -> list[dict]:
    result = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            groups = (process / "cgroup").read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            continue
        unit = group.rsplit("/", 1)[-1]
        if any(unit in line.split("/") for line in groups):
            if groups != [f"0::{group}"]:
                raise RuntimeError(f"creation scope has an unexpected cgroup: PID {process.name}; {groups}")
            identity = _identity(int(process.name))
            if identity is not None:
                result.append(identity)
    return result


def _pidfd(pid: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes = (ctypes.c_int, ctypes.c_uint)
    libc.pidfd_open.restype = ctypes.c_int
    fd = libc.pidfd_open(pid, 0)
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return fd


def _signal(fd: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_send_signal.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    libc.pidfd_send_signal.restype = ctypes.c_int
    if libc.pidfd_send_signal(fd, value, None, 0) < 0:
        code = ctypes.get_errno()
        if code != errno.ESRCH:
            raise OSError(code, os.strerror(code))


def _cfw_target(target: str) -> bool:
    return re.fullmatch(r"/var/tmp/vphone-cfw\.[A-Za-z0-9]{8}/(?:system|xart|preboot)", target) is not None


def _cryptex_target(target: str) -> bool:
    return re.fullmatch(r"/var/tmp/vphone-cfw\.[A-Za-z0-9]{8}/mnt-(?:os|app)", target) is not None


def _loop_status(fd: int) -> dict | None:
    data = bytearray(232)  # Linux loop_info64, including the trailing lo_init fields.
    try:
        fcntl.ioctl(fd, 0x4C05, data)  # LOOP_GET_STATUS64
    except OSError as error:
        if error.errno == errno.ENXIO:
            return None
        raise
    device, inode, _, offset, limit, number, encryption, key_size, flags = struct.unpack_from(
        "=QQQQQIIII", data
    )
    # loop_info64 uses huge_encode_dev/new_encode_dev, not the kernel's
    # internal 12:20 dev_t layout. Require compatibility with stat.st_dev.
    major = (device & 0xFFF00) >> 8
    minor = (device & 0xFF) | ((device >> 12) & 0xFFF00)
    if device >> 32 or os.makedev(major, minor) != device:
        raise RuntimeError(f"unsupported loop backing-device encoding: {device}")
    return {
        "backing": (device, inode),
        "number": number,
        "offset": offset,
        "limit": limit,
        "encryption": encryption,
        "key_size": key_size,
        "flags": flags,
        "filename": os.fsdecode(bytes(data[56:120]).split(b"\0", 1)[0]),
        "record": bytes(data),
    }


def _open_loop(path: str, *, exclusive: bool = False) -> int:
    return os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | (os.O_EXCL if exclusive else 0))


def _loop_states() -> list[dict]:
    result = []
    for device in Path("/sys/class/block").glob("loop[0-9]*"):
        if not re.fullmatch(r"loop[0-9]+", device.name):
            continue
        path = f"/dev/{device.name}"
        try:
            fd = _open_loop(path)
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(fd)
            status = _loop_status(fd)
            if not stat.S_ISBLK(info.st_mode) or os.major(info.st_rdev) != 7:
                raise RuntimeError(f"unexpected loop device identity: {path}")
            if status is not None:
                result.append(status | {"path": path, "node": (info.st_dev, info.st_ino, info.st_rdev)})
        finally:
            os.close(fd)
    return result


def _require_no_unproven_cfw_loops(proven: tuple[dict, ...] = ()) -> None:
    for loop in _loop_states():
        if (
            re.fullmatch(
                r"/var/tmp/vphone-cfw\.[A-Za-z0-9]{8}/(?:SystemOS(?:-source)?|AppOS)\.dmg",
                loop["filename"],
            )
            and loop not in proven
        ):
            # A name can require refusal, but never establishes authority to detach.
            raise RuntimeError(f"creation cleanup has an unproven CFW loop attachment: {loop['path']}")


def _file_identity(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _verify_cryptex_file(row: dict) -> None:
    directory = row["directory"]
    current = directory.lstat()
    info = os.fstat(row["directory_fd"])
    if (
        (current.st_dev, current.st_ino) != row["directory_identity"]
        or (info.st_dev, info.st_ino) != row["directory_identity"]
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RuntimeError(f"creation Cryptex directory identity changed: {directory}")
    info = os.fstat(row["file_fd"])
    current = os.stat(row["file"], dir_fd=row["directory_fd"], follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or _file_identity(info) != row["file_identity"]
        or _file_identity(current) != row["file_identity"]
    ):
        raise RuntimeError(f"creation Cryptex backing file identity changed: {directory / row['file']}")


def _cryptex_attachments(owned_mounts: list, before: list[dict], baseline_records: set[str], resources):
    images = []
    directories = {Path(mount["target"]).parent: (fd, identity) for mount, _, fd, identity in owned_mounts}
    for directory, (directory_fd, directory_identity) in directories.items():
        for name, target in (
            ("SystemOS.dmg", "mnt-os"),
            ("SystemOS-source.dmg", "mnt-os"),
            ("AppOS.dmg", "mnt-app"),
        ):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            resources.callback(os.close, fd)
            row = {
                "directory": directory,
                "directory_fd": directory_fd,
                "directory_identity": directory_identity,
                "file": name,
                "file_fd": fd,
                "file_identity": _file_identity(os.fstat(fd)),
                "target": str(directory / target),
            }
            _verify_cryptex_file(row)
            images.append(row)
    if not images:
        return images, []
    result = []
    for loop in _loop_states():
        matching = [row for row in images if row["file_identity"][:2] == loop["backing"]]
        if not matching:
            continue
        if (
            len(matching) != 1
            or loop["flags"] != 1  # LO_FLAGS_READ_ONLY, with no other attachment modes.
            or loop["offset"] != 0
            or loop["limit"] != 0
            or loop["encryption"] != 0
            or loop["key_size"] != 0
            or loop["path"] != f"/dev/loop{loop['number']}"
        ):
            raise RuntimeError(f"creation Cryptex loop ownership could not be proven: {loop}")
        row = matching[0] | {"loop": loop}
        device = f"{os.major(loop['node'][2])}:{os.minor(loop['node'][2])}"
        matches = [mount for mount in before if mount["target"] == row["target"]]
        if matches and (
            len(matches) != 1
            or matches[0]["record"] in baseline_records
            or matches[0]["type"] != "apfs"
            or matches[0]["source"] != loop["path"]
            or matches[0].get("device") != device
            or "ro" not in matches[0].get("options", [])
        ):
            raise RuntimeError(f"creation Cryptex mount ownership could not be proven: {matches}")
        row["mount"] = matches[0] if matches else None
        if any(
            (mount["source"] == loop["path"] or mount.get("device") == device) and mount != row["mount"]
            for mount in before
        ):
            raise RuntimeError(f"creation Cryptex loop has a foreign mount: {loop}")
        result.append(row)
    return images, result


def _require_no_cryptex_loops(images: list[dict]) -> None:
    for image in images:
        _verify_cryptex_file(image)
    if any(loop["backing"] == image["file_identity"][:2] for loop in _loop_states() for image in images):
        raise RuntimeError("creation cleanup has residual Cryptex loop attachments")


def _require_unused_loop(loop: dict) -> None:
    device = f"{os.major(loop['node'][2])}:{os.minor(loop['node'][2])}"
    if any(mount["source"] == loop["path"] or mount.get("device") == device for mount in mounts()):
        raise RuntimeError(f"creation Cryptex loop remains mounted: {loop['path']}")
    sysfs = Path(f"/sys/dev/block/{device}")
    if any(re.fullmatch(rf"loop{loop['number']}p[0-9]+", child.name) for child in sysfs.iterdir()):
        raise RuntimeError(f"creation Cryptex loop has partitions: {loop['path']}")
    if list((sysfs / "holders").iterdir()):
        raise RuntimeError(f"creation Cryptex loop has device holders: {loop['path']}")
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            for descriptor in (process / "fd").iterdir():
                try:
                    info = descriptor.stat()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if stat.S_ISBLK(info.st_mode) and info.st_rdev == loop["node"][2]:
                    raise RuntimeError(f"creation Cryptex loop has an open user: PID {process.name}")
        except (FileNotFoundError, ProcessLookupError):
            continue


def _verify_loop(fd: int, loop: dict) -> dict | None:
    info = os.fstat(fd)
    status = _loop_status(fd)
    if (
        not stat.S_ISBLK(info.st_mode)
        or (info.st_dev, info.st_ino, info.st_rdev) != loop["node"]
        or (status is not None and status["record"] != loop["record"])
    ):
        raise RuntimeError(f"creation Cryptex loop identity changed: {loop['path']}")
    return status


def _cleanup_cryptex(attachments: list[dict], require_invocation) -> None:
    for row in reversed(attachments):
        _verify_cryptex_file(row)
        mount = row["mount"]
        current = [item for item in mounts() if item["target"] == row["target"]]
        fd = _open_loop(row["loop"]["path"])
        try:
            if _verify_loop(fd, row["loop"]) is None and current:
                raise RuntimeError(f"creation Cryptex loop disappeared while mounted: {row['loop']['path']}")
        finally:
            os.close(fd)
        if current:
            if current != [mount] or any(item["target"].startswith(row["target"] + "/") for item in mounts()):
                raise RuntimeError(f"creation Cryptex mount identity changed before unmount: {current}")
            require_invocation()
            result = subprocess.run(
                ["/usr/bin/umount", "--", row["target"]], capture_output=True, text=True, timeout=15
            )
            if result.returncode:
                raise RuntimeError(
                    f"owned creation Cryptex mount did not detach: {mount}; {result.stderr.strip()}"
                )
        fd = _open_loop(row["loop"]["path"], exclusive=True)
        try:
            status = _verify_loop(fd, row["loop"])
            if status is None:
                continue
            _require_unused_loop(row["loop"])
            _verify_cryptex_file(row)
            require_invocation()
            if _loop_status(fd) != status:
                raise RuntimeError(
                    f"creation Cryptex loop identity changed before detach: {row['loop']['path']}"
                )
            fcntl.ioctl(fd, 0x4C01)  # LOOP_CLR_FD on the verified descriptor, never a re-resolved path.
        finally:
            os.close(fd)
        if any(current["path"] == row["loop"]["path"] for current in _loop_states()):
            raise RuntimeError(f"creation Cryptex loop remains attached: {row['loop']['path']}")


def _cfw_install_client(identity: dict) -> bool:
    argv = identity["argv"]
    return (
        identity["uid"] == 0
        and Path(identity["exe"]).name == "vphone-cli"
        and len(argv) >= 3
        and Path(argv[0]).name == "vphone-cli"
        and argv[1:3] == ["cfw", "install"]
    )


def _unit_properties(unit: str, uid: int) -> dict[str, str]:
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError as error:
        raise RuntimeError(f"creation scope user does not exist: {uid}") from error
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            f"--machine={username}@.host",
            "show",
            unit,
            "--property=LoadState,ActiveState,InvocationID,ControlGroup",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode:
        raise RuntimeError(f"cannot inspect creation scope for UID {uid}: {result.stderr.strip()}")
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not {"LoadState", "ActiveState", "InvocationID", "ControlGroup"} <= properties.keys():
        raise RuntimeError(f"creation scope status is incomplete for UID {uid}: {properties}")
    return properties


def _inactive(properties: dict[str, str]) -> bool:
    if properties["LoadState"] not in ("loaded", "not-found"):
        raise RuntimeError(f"creation scope has an unexpected systemd load state: {properties}")
    if properties["ActiveState"] in ("inactive", "failed"):
        return True
    if properties["ActiveState"] != "active":
        raise RuntimeError(f"creation scope is transitioning: {properties}")
    return False


def _require_active_invocation(
    properties: dict[str, str], group: str, expected_invocation_id: str | None, scope_owned: bool
) -> None:
    if _inactive(properties):
        raise RuntimeError(f"creation scope became inactive before cleanup: {properties}")
    invocation_id = properties["InvocationID"].lower()
    if (
        scope_owned is not True
        or expected_invocation_id is None
        or invocation_id != expected_invocation_id
        or properties["ControlGroup"] != group
    ):
        raise RuntimeError(
            f"creation scope identity changed; refusing cleanup: expected InvocationID="
            f"{expected_invocation_id}, got {properties}"
        )


def _inactive_scope_is_empty(
    unit: str,
    uid: int,
    group: str,
    expected_invocation_id: str | None,
    scope_owned: bool,
    baseline_records: set[str],
    candidate_targets: set[str],
) -> bool:
    properties = _unit_properties(unit, uid)
    if not _inactive(properties):
        _require_active_invocation(properties, group, expected_invocation_id, scope_owned)
        return False
    identities = _scope_processes(group)
    before = mounts()
    properties = _unit_properties(unit, uid)
    if not _inactive(properties):
        _require_active_invocation(properties, group, expected_invocation_id, scope_owned)
        return False
    remaining_mounts = [
        mount
        for mount in before
        if mount["record"] not in baseline_records
        and (
            _cfw_target(mount["target"])
            or _cryptex_target(mount["target"])
            or mount["target"] in candidate_targets
        )
    ]
    if identities or remaining_mounts:
        raise RuntimeError(
            f"inactive creation scope has residual resources: processes={identities}; mounts={remaining_mounts}"
        )
    _require_no_unproven_cfw_loops()
    return True


def _wait_for_identity_exit(identity: dict, fd: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        current = _identity(identity["pid"])
        if current is None:
            return True
        if current != identity:
            raise RuntimeError(f"creation process identity changed while waiting for exit: {identity}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        select.select([fd], [], [], min(0.1, remaining))


def _stop_cfw_install_client(
    identity: dict,
    fd: int,
    unit: str,
    uid: int,
    group: str,
    expected_invocation_id: str | None,
    scope_owned: bool,
) -> None:
    current = _identity(identity["pid"])
    if current is None:
        return
    if current != identity:
        raise RuntimeError(f"creation CFW client identity changed before TERM: {identity}")
    _require_active_invocation(_unit_properties(unit, uid), group, expected_invocation_id, scope_owned)
    _signal(fd, signal.SIGTERM)
    if _wait_for_identity_exit(identity, fd, 5):
        return
    current = _identity(identity["pid"])
    if current is None:
        return
    if current != identity:
        raise RuntimeError(f"creation CFW client identity changed before KILL: {identity}")
    _require_active_invocation(_unit_properties(unit, uid), group, expected_invocation_id, scope_owned)
    _signal(fd, signal.SIGKILL)
    if not _wait_for_identity_exit(identity, fd, 5):
        raise RuntimeError(f"creation CFW client survived TERM/KILL: {identity}")


def cleanup(
    unit: str,
    uid: int,
    boot_id: str,
    baseline: list[dict],
    *,
    scope_owned: bool = False,
    expected_invocation_id: str | None = None,
) -> None:
    if (
        type(uid) is not int
        or uid < 0
        or not re.fullmatch(rf"limulator-create-{uid}-[0-9a-f]{{8}}\.scope", unit)
    ):
        raise RuntimeError("refusing an invalid creation scope")
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != boot_id:
        raise RuntimeError("creation kernel boot identity changed")
    group = f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}"
    if expected_invocation_id is not None and not re.fullmatch(r"[0-9a-f]{32}", expected_invocation_id):
        raise RuntimeError("refusing an invalid creation scope InvocationID")
    baseline_records = {mount["record"] for mount in baseline}
    if _inactive_scope_is_empty(
        unit, uid, group, expected_invocation_id, scope_owned, baseline_records, set()
    ):
        return
    identities = _scope_processes(group)
    candidate_targets = {target for identity in identities for target in identity["argv"]}
    if _inactive_scope_is_empty(
        unit, uid, group, expected_invocation_id, scope_owned, baseline_records, candidate_targets
    ):
        return
    before = mounts()
    candidates = [
        mount
        for mount in before
        if mount["record"] not in baseline_records
        and (
            _cfw_target(mount["target"])
            or _cryptex_target(mount["target"])
            or any(mount["target"] in identity["argv"] for identity in identities)
        )
    ]
    candidate_targets = {mount["target"] for mount in candidates}
    if _inactive_scope_is_empty(
        unit,
        uid,
        group,
        expected_invocation_id,
        scope_owned,
        baseline_records,
        candidate_targets,
    ):
        return
    owned_mounts = []
    with contextlib.ExitStack() as resources:
        pinned = []
        for identity in identities:
            if identity["boot_id"] != boot_id or identity["cgroup"].splitlines() != [f"0::{group}"]:
                raise RuntimeError(f"creation process is outside the owned scope: {identity}")
            if identity["mount_namespace"] != os.readlink("/proc/self/ns/mnt"):
                raise RuntimeError(f"creation process has an unexpected mount namespace: {identity}")
            try:
                fd = _pidfd(identity["pid"])
            except ProcessLookupError:
                continue
            resources.callback(os.close, fd)
            current = _identity(identity["pid"])
            if current is None:
                continue
            if current != identity:
                raise RuntimeError(f"creation process identity changed: {identity}")
            pinned.append((identity, fd))
            if identity["exe"] != "/usr/bin/sshfs":
                continue
            argv = identity["argv"]
            if identity["uid"] != 0 or len(argv) < 4 or argv[1] != "-f" or not _cfw_target(argv[3]):
                raise RuntimeError(f"unrecognized creation SSHFS identity: {identity}")
            target, source = argv[3], argv[2]
            if not re.fullmatch(r"root@vphone:/mnt[0-9]+", source):
                raise RuntimeError(f"unrecognized creation SSHFS source: {identity}")
            matches = [m for m in before if m["target"] == target]
            if not matches:
                continue
            directory = Path(target).parent
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            resources.callback(os.close, directory_fd)
            info = os.fstat(directory_fd)
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError(f"creation SSHFS directory is not root-owned and private: {directory}")
            if (
                len(matches) != 1
                or matches[0] not in candidates
                or matches[0]["type"] != "fuse.sshfs"
                or matches[0]["source"] != source
            ):
                raise RuntimeError(f"creation SSHFS mount identity differs: {identity}; mounts={matches}")
            owned_mounts.append((matches[0], identity, directory_fd, (info.st_dev, info.st_ino)))
        images, cryptex = _cryptex_attachments(owned_mounts, before, baseline_records, resources)
        proven_mounts = [row[0] for row in owned_mounts] + [row["mount"] for row in cryptex]
        unknown = [m for m in candidates if m not in proven_mounts]
        if unknown:
            raise RuntimeError(f"creation mount ownership could not be proven: {unknown}")
        _require_no_unproven_cfw_loops(tuple(row["loop"] for row in cryptex))
        cfw_clients = [(identity, fd) for identity, fd in pinned if _cfw_install_client(identity)]
        if len(cfw_clients) > 1:
            raise RuntimeError(f"multiple creation CFW install clients are active: {cfw_clients}")
        if cfw_clients:
            client, fd = cfw_clients[0]
            _stop_cfw_install_client(client, fd, unit, uid, group, expected_invocation_id, scope_owned)
        _require_active_invocation(_unit_properties(unit, uid), group, expected_invocation_id, scope_owned)
        late = [
            identity
            for identity in _scope_processes(group)
            if identity not in identities or Path(identity["exe"]).name in ("losetup", "mount")
        ]
        if late:
            raise RuntimeError(f"creation resource producers remain active or changed: {late}")
        for row in cryptex:
            _verify_cryptex_file(row)
        current_mounts = mounts()
        current_images, current_cryptex = _cryptex_attachments(
            owned_mounts, current_mounts, baseline_records, resources
        )
        images.extend(current_images)
        for row in current_cryptex:
            if not any(
                row["loop"] == old["loop"]
                and row["mount"] == old["mount"]
                and row["file_identity"] == old["file_identity"]
                for old in cryptex
            ):
                raise RuntimeError(f"creation Cryptex attachment changed after installer exit: {row['loop']}")
        if any(
            mount["record"] not in baseline_records
            and (_cfw_target(mount["target"]) or _cryptex_target(mount["target"]))
            and mount not in proven_mounts
            for mount in current_mounts
        ):
            raise RuntimeError("creation mounts changed after installer exit")
        _cleanup_cryptex(
            cryptex,
            lambda: _require_active_invocation(
                _unit_properties(unit, uid), group, expected_invocation_id, scope_owned
            ),
        )
        remaining_pinned = [(identity, fd) for identity, fd in pinned if not _cfw_install_client(identity)]
        for mount, identity, directory_fd, directory_identity in owned_mounts:
            current = Path(mount["target"]).parent.lstat()
            if (current.st_dev, current.st_ino) != directory_identity or _identity(
                identity["pid"]
            ) != identity:
                raise RuntimeError(
                    f"creation SSHFS identity changed before unmount: {identity}; mount={mount}"
                )
            _require_active_invocation(
                _unit_properties(unit, uid), group, expected_invocation_id, scope_owned
            )
            if mount not in mounts():
                raise RuntimeError(f"creation SSHFS mount disappeared before unmount: {mount}")
            _require_active_invocation(
                _unit_properties(unit, uid), group, expected_invocation_id, scope_owned
            )
            result = subprocess.run(
                ["/usr/bin/fusermount3", "-u", mount["target"]],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode:
                raise RuntimeError(f"owned creation mount did not detach: {mount}; {result.stderr.strip()}")
        for value, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            _require_active_invocation(
                _unit_properties(unit, uid), group, expected_invocation_id, scope_owned
            )
            for identity, fd in remaining_pinned:
                current = _identity(identity["pid"])
                if current is None:
                    continue
                if current != identity:
                    raise RuntimeError(
                        f"creation process identity changed before signal: {identity}; current={current}"
                    )
                _signal(fd, value)
            deadline = time.monotonic() + wait
            while True:
                if _inactive_scope_is_empty(
                    unit,
                    uid,
                    group,
                    expected_invocation_id,
                    scope_owned,
                    baseline_records,
                    candidate_targets,
                ):
                    if images:
                        _require_no_cryptex_loops(images)
                    _require_no_unproven_cfw_loops()
                    return
                survivors = _scope_processes(group)
                remaining_mounts = [
                    m
                    for m in mounts()
                    if m["record"] not in baseline_records
                    and (
                        _cfw_target(m["target"])
                        or _cryptex_target(m["target"])
                        or any(m["target"] == old["target"] for old in candidates)
                    )
                ]
                if not survivors and not remaining_mounts:
                    if images:
                        _require_no_cryptex_loops(images)
                    _require_no_unproven_cfw_loops()
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                pending = [fd for identity, fd in remaining_pinned if _identity(identity["pid"]) is not None]
                if pending:
                    select.select(pending, [], [], min(0.1, remaining))
                else:
                    time.sleep(min(0.1, remaining))
        raise RuntimeError(f"creation cleanup incomplete: processes={survivors}; mounts={remaining_mounts}")


if __name__ == "__main__":
    try:
        cleanup(**json.load(sys.stdin))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
