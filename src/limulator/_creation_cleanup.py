"""Bounded cleanup for one creation scope, executed with the existing sudo authority."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import re
import select
import signal
import stat
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


def cleanup(unit: str, uid: int, boot_id: str, baseline: list[dict], *, scope_owned: bool = False) -> None:
    if (
        type(uid) is not int
        or uid < 0
        or not re.fullmatch(rf"limulator-create-{uid}-[0-9a-f]{{8}}\.scope", unit)
    ):
        raise RuntimeError("refusing an invalid creation scope")
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != boot_id:
        raise RuntimeError("creation kernel boot identity changed")
    group = f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}"
    identities = _scope_processes(group)
    before = mounts()
    baseline_records = {mount["record"] for mount in baseline}
    candidates = [
        mount
        for mount in before
        if mount["record"] not in baseline_records
        and (
            _cfw_target(mount["target"])
            or any(mount["target"] in identity["argv"] for identity in identities)
        )
    ]
    if scope_owned is not True:
        if identities or candidates:
            raise RuntimeError(
                f"creation scope ownership was not established; refusing cleanup: "
                f"processes={identities}; mounts={candidates}"
            )
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
        unknown = [m for m in candidates if m not in [row[0] for row in owned_mounts]]
        if unknown:
            raise RuntimeError(f"creation mount ownership could not be proven: {unknown}")
        for mount, identity, directory_fd, directory_identity in owned_mounts:
            current = Path(mount["target"]).parent.lstat()
            if (
                (current.st_dev, current.st_ino) != directory_identity
                or mount not in mounts()
                or _identity(identity["pid"]) != identity
            ):
                raise RuntimeError(
                    f"creation SSHFS identity changed before unmount: {identity}; mount={mount}"
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
            for identity, fd in pinned:
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
                survivors = _scope_processes(group)
                remaining_mounts = [
                    m
                    for m in mounts()
                    if m["record"] not in baseline_records
                    and (_cfw_target(m["target"]) or any(m["target"] == old["target"] for old in candidates))
                ]
                if not survivors and not remaining_mounts:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                pending = [fd for identity, fd in pinned if _identity(identity["pid"]) is not None]
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
