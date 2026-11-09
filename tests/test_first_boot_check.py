import hashlib
import os
import plistlib
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from limulator import _first_boot_check as check
from limulator import creator
from limulator.lifecycle import Paths
from limulator.profiles import get_profile


@pytest.fixture(autouse=True)
def no_native_operations(monkeypatch):
    def prohibited(*_args, **_kwargs):
        raise AssertionError("native operation prohibited in caller-check tests")

    monkeypatch.setattr(socket, "socket", prohibited)
    monkeypatch.setattr(os, "kill", prohibited)
    for name in ("_host_creation_lock", "_unit_states", "_disk_users", "_creation_scope_cgroup"):
        monkeypatch.setattr(creator, name, prohibited)
    monkeypatch.setattr(check, "_identity", prohibited)
    monkeypatch.setattr(check, "_identity_alive", prohibited)


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def commit(path, text):
    (path / "source").write_text(text)
    git(path, "add", "source")
    git(
        path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        text,
    )
    return git(path, "rev-parse", "HEAD")


def test_local_contract_exact_legacy_then_visible_hook_ancestry(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "--initial-branch=main")
    legacy = commit(tmp_path, "legacy")
    hook = commit(tmp_path, "caller contract")
    selected = [hook]
    monkeypatch.setattr(check, "_LEGACY", legacy)
    monkeypatch.setattr(check, "_CALLER_CONTRACT", hook)
    monkeypatch.setattr(check, "_locked_revision", lambda: selected[0])
    assert check.backend_contract(tmp_path)["mode"] == "caller-check-v1"
    git(tmp_path, "checkout", "-q", "--detach", legacy)
    selected[0] = legacy
    assert check.backend_contract(tmp_path)["mode"] == "legacy-strict"
    selected[0] = hook
    with pytest.raises(RuntimeError, match="does not match the lock"):
        check.backend_contract(tmp_path)
    selected[0] = legacy
    (tmp_path / "source").write_text("dirty")
    with pytest.raises(RuntimeError, match="tracked or staged"):
        check.backend_contract(tmp_path)


def test_shallow_missing_contract_refuses_without_fetching_or_moving_refs(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-q", "--initial-branch=main")
    hook = commit(remote, "caller contract")
    head = commit(remote, "later revision")
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=1", remote.as_uri(), str(clone)], check=True)
    monkeypatch.setattr(check, "_locked_revision", lambda: head)
    monkeypatch.setattr(check, "_CALLER_CONTRACT", hook)
    before = (clone / ".git/shallow").read_bytes(), git(clone, "show-ref")
    real_git, calls = check._git, []

    def read_only(path, *args):
        calls.append(args)
        assert args[0] in {"rev-parse", "diff", "merge-base"}
        return real_git(path, *args)

    monkeypatch.setattr(check, "_git", read_only)
    with pytest.raises(RuntimeError, match="local backend history"):
        check.backend_contract(clone)
    assert before == ((clone / ".git/shallow").read_bytes(), git(clone, "show-ref"))
    assert calls


def test_unowned_backend_refuses_before_git_even_when_git_might_trust_it(tmp_path, monkeypatch):
    selected_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: selected_uid + 1)
    git_probe = Mock(side_effect=AssertionError("unowned source must not reach Git"))
    monkeypatch.setattr(check, "_git", git_probe)
    with pytest.raises(RuntimeError, match="direct owned directory"):
        check.backend_contract(tmp_path)
    git_probe.assert_not_called()


def settings(build="24A94425", success=True):
    return {"values": {"DMLastMigrationResults": {"value": {"buildVersion": build, "success": success}}}}


@pytest.mark.parametrize("success", [False, 0])
def test_stale_failure_waits_but_expected_build_failure_is_terminal(success):
    setup = {"data_migration_done": True, "pending": True}
    assert check.decision(setup, settings("older", success), "24A94425") is None
    with pytest.raises(RuntimeError, match="success=false"):
        check.decision(setup, settings(success=success), "24A94425")


@pytest.mark.parametrize("success", [True, 1])
def test_strict_success_allows_pending_setup_assistant(success):
    result = check.decision(
        {"data_migration_done": True, "pending": True}, settings(success=success), "24A94425"
    )
    assert result is not None
    assert type(result["success"]) is type(success)


@pytest.mark.parametrize("success", ["true", "1", 1.0, 0.0, 2, None])
def test_unsupported_success_does_not_permit_setup(success):
    assert check.decision({"data_migration_done": True}, settings(success=success), "24A94425") is None
    assert check.decision({"data_migration_done": 1}, settings(), "24A94425") is None
    assert check.decision({"data_migration_done": True}, {"values": {}}, "24A94425") is None


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    vm = tmp_path / 'VM with "quotes" $literal; path'
    vm.mkdir(mode=0o700)
    (vm / "Disk.img").write_bytes(b"synthetic disk identity")
    run = vm / "create-fixture"
    run.mkdir(mode=0o700)
    profile = get_profile("iphone-duo")
    specification = {"name": "duo", "model": profile.model, "ios": profile.os_version, "cid": 41, "scale": 3}
    identities = {
        "owner": {"pid": 10001, "start_ticks": 11, "boot_id": "fixture-boot"},
        "launcher": {"pid": 10002, "start_ticks": 12, "boot_id": "fixture-boot"},
        "qemu": {"pid": 10003, "start_ticks": 13, "boot_id": "fixture-boot"},
    }
    unit = "limulator-create-1000-01234567.scope"
    contract = {"mode": "caller-check-v1", "revision": "a" * 40}
    clock = [100.0]
    monkeypatch.setattr(check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(check.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    reference = check.prepare(
        run,
        vm,
        specification,
        profile.id,
        "24A94425",
        identities["owner"],
        unit,
        tmp_path / "qemu-system-aarch64",
        contract,
        150,
        {},
    )
    config_path = Path(reference["configuration"])
    config, digest = check._private_json(config_path)
    config["wait_timeout"] = 15
    check._atomic_json(config_path, config)
    config, digest = check._private_json(config_path)
    reference["configuration_sha256"] = digest
    state = {
        "state": "creating",
        "process": identities["owner"],
        "run_dir": str(run),
        "scope": unit,
        "specification": specification,
        "profile_id": profile.id,
        "migration_expected_build": "24A94425",
        "launcher": identities["launcher"],
        "scope_invocation_id": "b" * 32,
        "first_boot_check": reference,
    }
    check._atomic_json(Path(config["state_path"]), state)
    (vm / ".qemu.pid").write_text("10003\n")
    (vm / "cid").write_text("41\n")
    tree = vm / "iPhone_Fixture_Restore"
    tree.mkdir()
    manifest = tree / "iPhone-BuildManifest.plist"
    manifest.write_bytes(
        plistlib.dumps(
            {
                "SupportedProductTypes": [profile.model],
                "ProductVersion": profile.os_version,
                "ProductBuildVersion": "24A94425",
            }
        )
    )
    arguments = [b"qemu-system-aarch64", b"vhost-vsock-pci,guest-cid=41"]
    monkeypatch.setattr(check, "_identity_alive", lambda value: value in identities.values())
    monkeypatch.setattr(
        check,
        "_identity",
        lambda pid: next((value for value in identities.values() if value["pid"] == pid), None),
    )
    monkeypatch.setattr(check, "_arguments", lambda _pid: arguments)
    monkeypatch.setattr(check, "_executable", lambda _pid: Path(config["qemu"]))
    monkeypatch.setattr(creator, "_disk_users", lambda _disk: [identities["qemu"]["pid"]])
    monkeypatch.setattr(
        creator,
        "_creation_scope_cgroup",
        lambda _pid: f"0::{creator._creation_scope_group(os.getuid(), unit)}",
    )
    return SimpleNamespace(
        vm=vm,
        run=run,
        config=config,
        path=config_path,
        digest=digest,
        reference=reference,
        state=state,
        manifest=manifest,
        clock=clock,
        identities=identities,
        arguments=arguments,
        contract=contract,
    )


def guest_factory(replies, *, after_settings=None):
    calls = []

    class Guest:
        def __init__(self, cid, deadline):
            assert cid == 41 and deadline > 100

        def rpc(self, method, params):
            calls.append((method, params))
            if method == "setup.status":
                return {"data_migration_done": True, "pending": True}
            result = replies.pop(0) if replies else {"values": {}}
            if after_settings:
                after_settings()
            return result

    return Guest, calls


def test_actual_callback_waits_for_missing_and_stale_results_without_parent_writes(invocation):
    value = invocation
    original = Path(value.config["state_path"]).read_bytes()
    factory, calls = guest_factory([{"values": {}}, settings("older", False), settings(success=1)])
    check.run_check(value.config, value.path, value.digest, guest_factory=factory)
    assert len(calls) == 6
    assert value.clock[0] == 110
    assert Path(value.config["state_path"]).read_bytes() == original
    updated = check.verify_proof(
        value.reference,
        value.vm,
        value.config["specification"],
        "24A94425",
        "iphone-duo",
        contract=value.contract,
    )
    assert updated["proof_sha256"]
    proof, _ = check._private_json(Path(updated["proof"]))
    assert proof["state"] == "passed" and proof["setup"]["pending"] is True
    assert "last_error" not in proof


def test_callback_cancellation_after_rpc_cannot_publish_passed_proof(invocation):
    value = invocation

    def cancel():
        check._atomic_json(Path(value.config["state_path"]), value.state | {"state": "error"})

    factory, _ = guest_factory([settings()], after_settings=cancel)
    with pytest.raises(RuntimeError, match="cancelled"):
        check.run_check(value.config, value.path, value.digest, guest_factory=factory)
    proof, _ = check._private_json(value.run / check._PROOF)
    assert proof["state"] == "failed"


def test_callback_missing_results_stop_at_own_absolute_deadline(invocation):
    factory, _ = guest_factory([])
    with pytest.raises(RuntimeError, match="absolute deadline"):
        check.run_check(invocation.config, invocation.path, invocation.digest, guest_factory=factory)
    assert invocation.clock[0] == 115


@pytest.mark.parametrize("changed", ["generation", "cid", "scope", "parent", "manifest"])
def test_real_ownership_guard_refuses_changed_binding(invocation, monkeypatch, changed):
    value = invocation
    manifest, identity = check.prepared_manifest(value.config)
    guard = check.Ownership(value.config, value.path, value.digest, manifest, identity, 150)
    guard.check()
    if changed == "generation":
        value.identities["qemu"] = value.identities["qemu"] | {"start_ticks": 14}
    elif changed == "cid":
        value.arguments[-1] = b"vhost-vsock-pci,guest-cid=42"
    elif changed == "scope":
        monkeypatch.setattr(creator, "_creation_scope_cgroup", lambda _pid: "0::/other")
    elif changed == "parent":
        check._atomic_json(Path(value.config["state_path"]), value.state | {"state": "error"})
    else:
        value.manifest.write_bytes(b"changed manifest")
    with pytest.raises(RuntimeError):
        guard.check()


@pytest.mark.parametrize(
    "key,value",
    [("SupportedProductTypes", ["other"]), ("ProductVersion", "26.0"), ("ProductBuildVersion", "older")],
)
def test_original_prepared_manifest_must_match_pinned_profile(invocation, key, value):
    manifest = plistlib.loads(invocation.manifest.read_bytes())
    manifest[key] = value
    invocation.manifest.write_bytes(plistlib.dumps(manifest))
    with pytest.raises(RuntimeError, match="contradicts"):
        check.prepared_manifest(invocation.config)


def test_historical_proof_reuse_preserves_strict_observations_without_live_health(invocation, monkeypatch):
    value = invocation
    factory, _ = guest_factory([settings()])
    check.run_check(value.config, value.path, value.digest, guest_factory=factory)
    reference = check.verify_proof(
        value.reference,
        value.vm,
        value.config["specification"],
        "24A94425",
        "iphone-duo",
        contract=value.contract,
    )
    monkeypatch.setattr(
        check, "_identity_alive", Mock(side_effect=AssertionError("history is not current health"))
    )
    value.clock[0] = 100000
    result = check.verify_proof(
        reference,
        value.vm,
        value.config["specification"],
        "24A94425",
        "iphone-duo",
        contract=value.contract,
        historical=True,
    )
    assert result["historical"] is True


@pytest.mark.parametrize("changed", ["disk", "profile", "build", "source", "false", "missing-digest"])
def test_historical_proof_refuses_wrong_target_or_unverified_results(invocation, changed):
    value = invocation
    factory, _ = guest_factory([settings()])
    check.run_check(value.config, value.path, value.digest, guest_factory=factory)
    reference = check.verify_proof(
        value.reference,
        value.vm,
        value.config["specification"],
        "24A94425",
        "iphone-duo",
        contract=value.contract,
    )
    profile, build = "iphone-duo", "24A94425"
    if changed == "disk":
        replacement = value.vm / "replacement"
        replacement.write_bytes(b"another synthetic disk")
        replacement.replace(value.vm / "Disk.img")
    elif changed == "profile":
        profile = "other-profile"
    elif changed == "build":
        build = "older"
    elif changed == "source":
        value.contract["revision"] = "b" * 40
    elif changed == "false":
        proof, _ = check._private_json(Path(reference["proof"]))
        proof["migration_result"]["success"] = False
        check._atomic_json(Path(reference["proof"]), proof)
        reference["proof_sha256"] = hashlib.sha256(Path(reference["proof"]).read_bytes()).hexdigest()
    else:
        reference.pop("proof_sha256")
    with pytest.raises(RuntimeError):
        check.verify_proof(
            reference,
            value.vm,
            value.config["specification"],
            build,
            profile,
            contract=value.contract,
            historical=True,
        )


def test_launcher_imports_from_unrelated_cwd_and_rejects_wrong_cid_before_runtime(invocation, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    result = subprocess.run(
        [invocation.reference["launcher"], str(invocation.vm), "42"],
        cwd=other,
        env=os.environ | {"PYTHONPATH": "/ignored-by-isolated-python"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "VM/CID arguments do not match" in result.stderr
    assert not (invocation.run / check._PROOF).exists()


@pytest.mark.parametrize("delay", [None, "headers", "connect"])
def test_rpc_transport_preserves_absolute_deadline_and_refuses_late_dispatch(monkeypatch, delay):
    clock, seen = [100.0], []
    monkeypatch.setattr(check.time, "monotonic", lambda: clock[0])
    guest = check._DeadlineGuest(41, 105)
    sock = SimpleNamespace(close=lambda: seen.append("close"))

    def headers(*_args):
        if delay == "headers":
            clock[0] = 106
        return b"headers"

    def connect(deadline):
        seen.append(("connect", deadline))
        if delay == "connect":
            clock[0] = 106
        return sock, object()

    def send(_sock, data, deadline, _operation):
        seen.append(("send", data, deadline))

    monkeypatch.setattr(guest, "_request_headers", headers)
    monkeypatch.setattr(guest, "_connect", connect)
    monkeypatch.setattr(check, "_sendall", send)
    monkeypatch.setattr(
        guest, "_read_response", lambda _method, _reader, deadline: seen.append(("read", deadline)) or b"{}"
    )
    if delay:
        with pytest.raises(check.ProtocolError, match="absolute deadline"):
            guest.request("POST", "/rpc", b"{}")
        assert seen == ([] if delay == "headers" else [("connect", 105), "close"])
    else:
        assert guest.request("POST", "/rpc", b"{}") == b"{}"
        assert seen == [
            ("connect", 105),
            ("send", b"headers", 105),
            ("send", b"{}", 105),
            ("read", 105),
            "close",
        ]


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMULATOR_BACKEND", str(tmp_path / "backend"))
    paths = Paths(tmp_path / "data")
    vm = paths.vms / "duo"
    vm.mkdir(parents=True)
    (vm / "Disk.img").write_bytes(b"synthetic preserved disk")
    (vm / ".create-state").write_text("bundle\n")
    profile = get_profile("iphone-duo")
    candidate = SimpleNamespace(
        name="duo",
        vm_path=vm,
        model=profile.model,
        os_version=profile.os_version,
        cid=41,
        scale=3,
        validate_vm=lambda: None,
    )
    specification = creator._creation_specification(candidate)
    creator._atomic_json(vm / ".limulator-create.json", {"specification": specification, "state": "error"})
    script = paths.backend / "setup/create-vm.sh"
    script.parent.mkdir(parents=True)
    script.write_text("# fixture for VPHONE_CREATOR_USB_READY and VPHONE_FIRST_BOOT_QMP\n")
    calls = []
    monkeypatch.setattr(
        creator, "_identity", lambda _pid: {"pid": 10001, "start_ticks": 11, "boot_id": "fixture"}
    )
    monkeypatch.setattr(creator, "_first_boot_qmp_path", lambda run: run / "qmp.sock")
    monkeypatch.setattr(creator, "_firmware_cache", lambda _env: tmp_path / "firmware")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_args: calls.append("firmware") or {})
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_lock_disk", lambda _path: None)
    monkeypatch.setattr(creator, "_bind_vm_ecid", lambda *_args: None)
    monkeypatch.setattr(creator, "_resume_completed_boot", lambda *_args: None)
    monkeypatch.setattr(creator, "_apply_profile_configuration", lambda *_args: None)
    monkeypatch.setattr(creator, "_archive_creation_logs", lambda *_args: None)

    class BoundaryReached(RuntimeError):
        pass

    class Helpers:
        def __init__(self, _paths, _run, env, _unit):
            self.env = env

        def start(self):
            calls.append(dict(self.env))
            raise BoundaryReached("mocked before USB daemon or backend start")

        def close(self):
            pass

    monkeypatch.setattr(creator, "_USBHelpers", Helpers)
    return SimpleNamespace(
        paths=paths, vm=vm, candidate=candidate, profile=profile, calls=calls, boundary=BoundaryReached
    )


@pytest.mark.parametrize("mode", ["legacy-strict", "caller-check-v1"])
def test_actual_pipeline_installs_callback_only_for_verified_modern_backend(pipeline, monkeypatch, mode):
    value = pipeline
    monkeypatch.setattr(check, "backend_contract", lambda _backend: {"mode": mode, "revision": "a" * 40})
    with pytest.raises(value.boundary):
        creator._pipeline(value.paths, value.candidate, value.profile, {}, {"QEMU": "/fixture/qemu"})
    env = value.calls[-1]
    assert value.calls[0] == "firmware"
    if mode == "legacy-strict":
        assert "VPHONE_FIRST_BOOT_CHECK" not in env
    else:
        launcher = Path(env["VPHONE_FIRST_BOOT_CHECK"])
        assert os.access(launcher, os.X_OK)
        state = creator._read_json(value.vm / ".limulator-create.json")
        config, digest = check._private_json(Path(state["first_boot_check"]["configuration"]))
        assert config["vm"] == str(value.vm) and config["specification"]["cid"] == 41
        assert digest == state["first_boot_check"]["configuration_sha256"]


@pytest.mark.parametrize("failure", ["source-contract", "missing-historical-proof"])
def test_pipeline_refuses_unsupported_source_or_unproved_resume_before_firmware(
    pipeline, monkeypatch, failure
):
    value = pipeline
    if failure == "source-contract":
        monkeypatch.setattr(check, "backend_contract", Mock(side_effect=RuntimeError("unsupported source")))
    else:
        monkeypatch.setattr(
            check, "backend_contract", lambda _backend: {"mode": "caller-check-v1", "revision": "a" * 40}
        )
        (value.vm / ".create-state").write_text("bundle\nbooted\n")
    before = (value.vm / "Disk.img").read_bytes()
    with pytest.raises(RuntimeError, match="unsupported source|missing caller migration proof"):
        creator._pipeline(value.paths, value.candidate, value.profile, {}, {"QEMU": "/fixture/qemu"})
    assert value.calls == []
    assert (value.vm / "Disk.img").read_bytes() == before
