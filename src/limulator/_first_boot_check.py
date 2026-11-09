"""Caller-owned migration acceptance before the backend changes guest setup."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

from .lifecycle import _atomic_json, _identity, _identity_alive, _read_vm_identity
from .profiles import get_profile
from .protocols import GuestClient, ProtocolError, _sendall

_LEGACY = "909f3a61cf02d5b40b265a5e4e534730bc39ab25"
_CALLER_CONTRACT = "adf9257fed905a1782d10c7ad1a43423da3022e3"
_POLICY = "strict-expected-build-migration-v1"
_CONFIG = "first-boot-migration-config.json"
_PROOF = "first-boot-migration-proof.json"


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _git(backend, *arguments):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["git", "--no-optional-locks", "--no-replace-objects", "-C", str(backend), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _locked_revision():
    installed = Path(__file__).with_name("third_party.lock.json")
    lock = installed if installed.is_file() else Path(__file__).resolve().parents[2] / "third_party.lock.json"
    value = json.loads(_read_vm_identity(lock, 1 << 20))["backend"]["revision"]
    _require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "backend lock must pin a full commit"
    )
    return value


def backend_contract(backend):
    """Establish support using local immutable Git history, without fetching."""
    backend = Path(backend).absolute()
    guidance = "Preserve this checkout and run `limulator bootstrap sources` with a complete, clean backend."
    info = backend.lstat()
    _require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and not any(part.is_symlink() for part in backend.parents),
        f"backend source path is not a direct owned directory. {guidance}",
    )
    top = _git(backend, "rev-parse", "--show-toplevel")
    _require(
        top.returncode == 0 and Path(top.stdout.strip()).resolve() == backend.resolve(),
        f"backend is not its own source checkout. {guidance}",
    )
    for arguments in (
        ("diff", "--no-ext-diff", "--quiet", "--ignore-submodules=all"),
        ("diff", "--no-ext-diff", "--cached", "--quiet", "--ignore-submodules=none"),
    ):
        _require(
            _git(backend, *arguments).returncode == 0,
            f"backend sources have tracked or staged changes. {guidance}",
        )
    expected = _locked_revision()
    head = _git(backend, "rev-parse", "HEAD")
    _require(
        head.returncode == 0 and head.stdout.strip() == expected,
        f"backend source revision does not match the lock. {guidance}",
    )
    if expected == _LEGACY:
        return {"mode": "legacy-strict", "revision": expected}
    supported = _git(backend, "merge-base", "--is-ancestor", _CALLER_CONTRACT, expected)
    _require(
        supported.returncode == 0,
        f"local backend history does not establish the required first-boot caller contract. {guidance}",
    )
    return {"mode": "caller-check-v1", "revision": expected}


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _private_json(path):
    before = path.lstat()
    _require(
        path.resolve(strict=True) == path
        and stat.S_ISREG(before.st_mode)
        and before.st_uid == os.getuid()
        and before.st_nlink == 1
        and stat.S_IMODE(before.st_mode) == 0o600,
        f"callback file is not private: {path}",
    )
    raw = _read_vm_identity(path, 1 << 20)
    after = path.lstat()
    _require(
        _file_identity(before) == _file_identity(after)
        and (after.st_uid, after.st_nlink, stat.S_IMODE(after.st_mode)) == (os.getuid(), 1, 0o600),
        f"callback file changed while reading: {path}",
    )
    value = json.loads(raw)
    _require(isinstance(value, dict), f"callback file is not an object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _write_private(path, raw, mode):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _target(vm, specification, expected_build, profile_id):
    _require(vm.resolve(strict=True) == vm, "selected migration VM path is not canonical")
    info = (vm / "Disk.img").lstat()
    _require(stat.S_ISREG(info.st_mode), "selected migration disk is not a regular file")
    return {
        "vm": str(vm),
        "disk": {"device": info.st_dev, "inode": info.st_ino},
        "specification": specification,
        "expected_build": expected_build,
        "profile_id": profile_id,
    }


def prepare(run, vm, specification, profile_id, expected_build, owner, unit, qemu, contract, deadline, env):
    _require(
        not env.get("VPHONE_FIRST_BOOT_CHECK"),
        "VPHONE_FIRST_BOOT_CHECK conflicts with Limulator's migration policy",
    )
    timeout = int(env.get("VPHONE_MIGRATION_TIMEOUT") or 3600)
    _require(timeout >= 0, "migration timeout cannot be negative")
    timeout = timeout or 3600
    configuration = {
        "schema_version": 1,
        "policy": _POLICY,
        **_target(vm, specification, expected_build, profile_id),
        "owner": owner,
        "unit": unit,
        "qemu": str(Path(qemu).resolve()),
        "parent_deadline": deadline,
        "wait_timeout": timeout,
        "backend_revision": contract["revision"],
        "run": str(run),
        "state_path": str(vm / ".limulator-create.json"),
    }
    raw = (json.dumps(configuration, allow_nan=False, indent=2) + "\n").encode()
    config, proof, launcher = run / _CONFIG, run / _PROOF, run / "first-boot-migration-check"
    _write_private(config, raw, 0o600)
    command = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -m limulator._first_boot_check {shlex.quote(str(config))} "$@"\n'
    _write_private(launcher, command.encode(), 0o700)
    return {
        "contract": contract["mode"],
        "backend_revision": contract["revision"],
        "configuration": str(config),
        "configuration_sha256": hashlib.sha256(raw).hexdigest(),
        "proof": str(proof),
        "launcher": str(launcher),
        "historical": False,
    }


def _proof_context(reference, vm, specification, expected_build, profile_id):
    config_path, proof_path = Path(reference["configuration"]), Path(reference["proof"])
    _require(
        config_path.name == _CONFIG
        and proof_path.name == _PROOF
        and config_path.parent == proof_path.parent
        and config_path.parent.parent == vm,
        "migration proof belongs to another VM run",
    )
    run = config_path.parent.lstat()
    _require(
        stat.S_ISDIR(run.st_mode) and run.st_uid == os.getuid() and stat.S_IMODE(run.st_mode) == 0o700,
        "migration proof run is not private",
    )
    config, digest = _private_json(config_path)
    _require(
        digest == reference["configuration_sha256"]
        and type(config.get("schema_version")) is int
        and config["schema_version"] == 1
        and config.get("policy") == _POLICY
        and config.get("backend_revision") == reference.get("backend_revision")
        and all(
            config.get(key) == value
            for key, value in _target(vm, specification, expected_build, profile_id).items()
        ),
        "migration configuration does not match this VM, disk, profile, CID or build",
    )
    return config, digest, proof_path


def verify_proof(reference, vm, specification, expected_build, profile_id, *, contract, historical=False):
    from . import creator

    _require(
        isinstance(reference, dict)
        and reference.get("contract") == "caller-check-v1"
        and contract.get("mode") == "caller-check-v1"
        and reference.get("backend_revision") == contract.get("revision")
        and all(
            isinstance(reference.get(key), str) for key in ("configuration", "configuration_sha256", "proof")
        ),
        "missing caller migration proof contract",
    )
    config, digest, path = _proof_context(reference, vm, specification, expected_build, profile_id)
    proof, proof_digest = _private_json(path)
    if historical:
        _require(proof_digest == reference.get("proof_sha256"), "historical migration proof digest differs")
    _require(
        type(proof.get("schema_version")) is int
        and proof["schema_version"] == 1
        and proof.get("policy") == _POLICY
        and proof.get("state") == "passed"
        and proof.get("configuration_sha256") == digest
        and proof.get("context")
        == {key: config[key] for key in ("vm", "disk", "specification", "expected_build", "profile_id")}
        and isinstance(proof.get("setup"), dict)
        and proof["setup"].get("data_migration_done") is True
        and isinstance(proof.get("binding"), dict)
        and proof["binding"].get("owner") == config["owner"]
        and proof["binding"].get("unit") == config["unit"]
        and isinstance(proof["binding"].get("scope_invocation_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", proof["binding"]["scope_invocation_id"]),
        "migration callback has no matching passed pre-setup proof",
    )
    for key in ("owner", "launcher", "qemu"):
        value = proof["binding"].get(key)
        _require(
            isinstance(value, dict)
            and type(value.get("pid")) is int
            and value["pid"] > 0
            and type(value.get("start_ticks")) is int
            and value["start_ticks"] > 0
            and value.get("boot_id") == config["owner"]["boot_id"],
            "migration proof process generation is malformed",
        )
    result = proof.get("migration_result")
    _require(isinstance(result, dict), "migration proof result is malformed")
    creator._verify_migration_result(result, expected_build)
    return reference | {"proof_sha256": proof_digest, "historical": historical}


def decision(setup, settings, expected_build):
    """Return an accepted result, or wait. Only a current explicit failure is fatal."""
    from . import creator

    try:
        result = creator._migration_result_from_settings(settings)
    except RuntimeError:
        return None
    if result.get("buildVersion") != expected_build:
        return None
    success = result.get("success")
    if success is False or (type(success) is int and success == 0):
        creator._verify_migration_result(result, expected_build)
    try:
        creator._verify_migration_result(result, expected_build)
    except RuntimeError:
        return None
    return result if isinstance(setup, dict) and setup.get("data_migration_done") is True else None


def prepared_manifest(config):
    vm, profile = Path(config["vm"]), get_profile(config["profile_id"])
    from . import creator

    _require(
        profile.model == config["specification"]["model"]
        and profile.os_version == config["specification"]["ios"]
        and creator._expected_migration_build(profile, {"build": config["expected_build"]})
        == config["expected_build"],
        "callback profile or expected firmware build differs",
    )
    trees = list(vm.glob("iPhone*_Restore"))
    _require(
        len(trees) == 1 and stat.S_ISDIR(trees[0].lstat().st_mode),
        "expected one original prepared iPhone restore tree",
    )
    path = trees[0] / "iPhone-BuildManifest.plist"
    before = path.lstat()
    manifest = plistlib.loads(_read_vm_identity(path, 64 << 20))
    _require(
        isinstance(manifest, dict)
        and isinstance(manifest.get("SupportedProductTypes"), list)
        and profile.model in manifest["SupportedProductTypes"]
        and manifest.get("ProductVersion") == profile.os_version
        and manifest.get("ProductBuildVersion") == config["expected_build"],
        "original prepared BuildManifest contradicts the selected product, OS or build",
    )
    return path, _file_identity(before)


class _AwaitOwnership(RuntimeError):
    pass


def _arguments(pid):
    return _read_vm_identity(Path(f"/proc/{pid}/cmdline"), 1 << 20).split(b"\0")


def _executable(pid):
    return Path(f"/proc/{pid}/exe").resolve(strict=True)


class Ownership:
    def __init__(self, config, config_path, digest, manifest, manifest_identity, deadline):
        self.config, self.config_path = config, config_path
        self.config_identity = _file_identity(config_path.lstat())
        _require(
            hashlib.sha256(_read_vm_identity(config_path, 1 << 20)).hexdigest() == digest,
            "callback configuration changed before ownership validation",
        )
        self.digest = digest
        self.manifest, self.manifest_identity, self.deadline = manifest, manifest_identity, deadline
        self.bound = None

    def remaining(self):
        value = self.deadline - time.monotonic()
        _require(value > 0, "pre-setup migration check exceeded its absolute deadline")
        return value

    def check(self):
        from . import creator

        self.remaining()
        config, vm = self.config, Path(self.config["vm"])
        _require(_identity_alive(config["owner"]), "creation owner generation exited")
        _require(
            _file_identity(self.config_path.lstat()) == self.config_identity
            and _file_identity(self.manifest.lstat()) == self.manifest_identity,
            "callback configuration or original manifest changed",
        )
        state_path = Path(config["state_path"])
        before = _file_identity(state_path.lstat())
        try:
            state, _ = _private_json(state_path)
        except (ValueError, RuntimeError):
            if before != _file_identity(state_path.lstat()):
                raise _AwaitOwnership("creation receipt changed during its atomic publication") from None
            raise
        _require(
            state.get("state") == "creating"
            and state.get("process") == config["owner"]
            and state.get("run_dir") == config["run"]
            and state.get("scope") == config["unit"]
            and state.get("specification") == config["specification"]
            and state.get("profile_id") == config["profile_id"]
            and state.get("migration_expected_build") == config["expected_build"]
            and isinstance(state.get("first_boot_check"), dict)
            and state["first_boot_check"].get("configuration_sha256") == self.digest
            and state["first_boot_check"].get("configuration") == str(self.config_path),
            "creation was cancelled or its binding changed",
        )
        launcher, invocation = state.get("launcher"), state.get("scope_invocation_id")
        if launcher is None or invocation is None:
            raise _AwaitOwnership("waiting for the creator's scope generation")
        _require(
            _identity_alive(launcher)
            and isinstance(invocation, str)
            and re.fullmatch(r"[0-9a-f]{32}", invocation),
            "creation launcher generation changed",
        )
        pid = int(_read_vm_identity(vm / ".qemu.pid", 32).strip())
        qemu = _identity(pid)
        _require(qemu is not None and pid > 0, "selected QEMU generation is unavailable")
        group = f"0::{creator._creation_scope_group(os.getuid(), config['unit'])}"
        _require(
            all(
                creator._creation_scope_cgroup(member) == group
                for member in (os.getpid(), pid, launcher["pid"])
            ),
            "callback, launcher and QEMU do not share the owned creation scope",
        )
        target = _target(vm, config["specification"], config["expected_build"], config["profile_id"])
        _require(
            target["disk"] == config["disk"]
            and pid in creator._disk_users(vm / "Disk.img")
            and _executable(pid) == Path(config["qemu"]),
            "QEMU does not own the selected disk or executable",
        )
        cids = [
            re.search(rb"(?:^|,)guest-cid=([0-9]+)(?:,|$)", argument)
            for argument in _arguments(pid)
            if argument.startswith((b"vhost-vsock-pci,", b"vhost-vsock-device,"))
        ]
        _require(
            len(cids) == 1
            and cids[0] is not None
            and int(cids[0].group(1)) == config["specification"]["cid"]
            and int(_read_vm_identity(vm / "cid", 32).strip()) == config["specification"]["cid"],
            "QEMU or VM marker has another VSOCK CID",
        )
        binding = {
            "owner": config["owner"],
            "launcher": launcher,
            "qemu": qemu,
            "unit": config["unit"],
            "scope_invocation_id": invocation,
            "disk": target["disk"],
        }
        _require(self.bound is None or self.bound == binding, "first-boot process ownership changed")
        self.remaining()
        self.bound = binding
        return binding


class _DeadlineGuest(GuestClient):
    def __init__(self, cid, deadline):
        super().__init__(cid=cid, timeout=30)
        self.deadline = deadline

    def request(self, method, path, body=b"", content_type="application/json"):
        deadline = min(self.deadline, time.monotonic() + 30)
        start = self._validate_request(method, path, content_type)
        headers = self._request_headers(start, len(body), content_type)
        if time.monotonic() >= deadline:
            raise ProtocolError("migration request exceeded its absolute deadline before connecting")
        sock, reader = self._connect(deadline)
        try:
            if time.monotonic() >= deadline:
                raise ProtocolError("migration request exceeded its absolute deadline before sending")
            _sendall(sock, headers, deadline, "writing migration request headers")
            if body:
                _sendall(sock, body, deadline, "writing migration request body")
            return self._read_response(method, reader, deadline)
        finally:
            sock.close()


def run_check(config, config_path, digest, *, guest_factory=_DeadlineGuest):
    deadline = min(config["parent_deadline"], time.monotonic() + config["wait_timeout"])
    proof_path = config_path.with_name(_PROOF)
    row = {
        "schema_version": 1,
        "policy": _POLICY,
        "state": "waiting",
        "configuration_sha256": digest,
        "context": {
            key: config[key] for key in ("vm", "disk", "specification", "expected_build", "profile_id")
        },
    }
    _write_private(proof_path, (json.dumps(row, allow_nan=False) + "\n").encode(), 0o600)
    try:
        manifest, identity = prepared_manifest(config)
        guard = Ownership(config, config_path, digest, manifest, identity, deadline)
        guest = guest_factory(config["specification"]["cid"], deadline)
        while True:
            try:
                binding = guard.check()
                replies = []
                for method, params in (
                    ("setup.status", {}),
                    ("settings.get", {"domain": "com.apple.migration"}),
                ):
                    guard.check()
                    try:
                        replies.append(guest.rpc(method, params))
                    finally:
                        guard.check()
                result = decision(*replies, config["expected_build"])
                row.update(
                    binding=binding,
                    setup={
                        key: replies[0].get(key) if type(replies[0].get(key)) is bool else None
                        for key in ("data_migration_done", "pending")
                    }
                    if isinstance(replies[0], dict)
                    else {},
                )
                if result is not None:
                    guard.check()
                    row.pop("last_error", None)
                    row.update(
                        state="passed",
                        migration_result={key: result[key] for key in ("buildVersion", "success")},
                    )
                    _atomic_json(proof_path, row)
                    return
                row["last_error"] = "waiting for strict successful migration results for the expected build"
            except (_AwaitOwnership, ProtocolError) as error:
                row["last_error"] = str(error)[:1000]
            _atomic_json(proof_path, row)
            time.sleep(min(5, guard.remaining()))
    except BaseException as error:
        row.update(state="failed", error=str(error)[:1000])
        _atomic_json(proof_path, row)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", type=Path)
    parser.add_argument("vm", type=Path)
    parser.add_argument("cid", type=int)
    args = parser.parse_args(argv)
    try:
        config, digest = _private_json(args.configuration)
        _require(
            str(args.vm) == config.get("vm") and args.cid == config.get("specification", {}).get("cid"),
            "first-boot callback VM/CID arguments do not match the pinned invocation",
        )
        _require(
            type(config.get("schema_version")) is int
            and config["schema_version"] == 1
            and config.get("policy") == _POLICY
            and type(config.get("parent_deadline")) in (int, float)
            and math.isfinite(config["parent_deadline"])
            and type(config.get("wait_timeout")) is int
            and config["wait_timeout"] > 0,
            "migration callback configuration is malformed",
        )
        run_check(config, args.configuration, digest)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"pre-setup migration check refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
