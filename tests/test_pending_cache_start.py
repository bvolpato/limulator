import os
import plistlib
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from limulator import creator, lifecycle
from limulator.lifecycle import Device, Paths, Registry, Runtime
from limulator.profiles import get_profile


@pytest.fixture
def pending_runtime(tmp_path, monkeypatch):
    profile = get_profile("iphone-duo")
    vm = tmp_path / "vm"
    vm.mkdir()
    disk = vm / "Disk.img"
    disk.write_bytes(b"preserved guest disk")
    (vm / "config.plist").write_bytes(plistlib.dumps({"diskImage": "Disk.img"}))
    device = Device("phone", vm, profile.model, profile.os_version)
    retained = tmp_path / "retained-cache"
    retained.mkdir()
    (retained / "before").write_bytes(b"original cache bytes")
    (retained / "after").write_bytes(b"partially published replacement")
    launches = []

    def launcher(*args, **kwargs):
        launches.append(args[0])
        raise AssertionError("mock launcher boundary reached")

    monkeypatch.setattr(lifecycle.subprocess, "Popen", launcher)
    monkeypatch.setattr(lifecycle.subprocess, "check_output", lambda *args, **kwargs: "fixture-revision\n")
    monkeypatch.setattr(lifecycle, "_resource_guard", lambda: None)
    with tempfile.TemporaryDirectory(prefix="lim-pending-", dir="/tmp") as temporary:
        paths = Paths(Path(temporary) / "data")
        backend = paths.data / "backend"
        binary = backend / "qemu/build/qemu-system-aarch64"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"never executed")
        binary.chmod(0o700)
        (backend / "run-vphone.sh").write_bytes(b"never executed")
        monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
        Registry(paths).register(device)
        yield SimpleNamespace(
            runtime=Runtime(paths, device),
            disk=disk,
            retained=retained,
            journal=vm / ".vphone-dsc-pending.json",
            launches=launches,
        )


def _preserved_bytes(fixture):
    return (
        fixture.disk.read_bytes(),
        fixture.disk.stat().st_ino,
        (fixture.disk.parent / "config.plist").read_bytes(),
        (fixture.retained / "before").read_bytes(),
        (fixture.retained / "after").read_bytes(),
    )


@pytest.mark.parametrize("entry", ["regular", "empty", "directory", "dangling-symlink", "symlink", "fifo"])
def test_start_refuses_any_pending_journal_entry_without_changing_recovery(pending_runtime, entry):
    fixture = pending_runtime
    journal = fixture.journal
    if entry == "directory":
        journal.mkdir()
        (journal / "retained").write_bytes(b"journal directory contents")
    elif entry == "dangling-symlink":
        journal.symlink_to(journal.parent / "missing-journal-target")
    elif entry == "symlink":
        journal.symlink_to(fixture.retained / "before")
    elif entry == "fifo":
        os.mkfifo(journal)
    else:
        journal.write_bytes(b'{"state":"publishing"' if entry == "regular" else b"")
    before = _preserved_bytes(fixture)
    identity = journal.lstat()
    with pytest.raises(RuntimeError, match="refusing boot.*unresolved dyld cache publication journal"):
        fixture.runtime.start()
    after = journal.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns) == (
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
        identity.st_size,
        identity.st_mtime_ns,
    )
    if entry in ("regular", "empty"):
        assert journal.read_bytes() == (b'{"state":"publishing"' if entry == "regular" else b"")
    if entry == "directory":
        assert (journal / "retained").read_bytes() == b"journal directory contents"
    if entry.endswith("symlink"):
        assert journal.is_symlink()
    assert _preserved_bytes(fixture) == before
    assert fixture.launches == []
    assert not fixture.runtime.runtime_path.parent.exists()


def test_start_rechecks_pending_journal_under_disk_ownership(pending_runtime, monkeypatch):
    fixture = pending_runtime
    before = _preserved_bytes(fixture)
    lock_disk = lifecycle._lock_disk
    locks = []

    def journal_appears(disk):
        fd = lock_disk(disk)
        locks.append(fd)
        fixture.journal.write_bytes(b"journal appeared at disk-lock boundary")
        with pytest.raises(RuntimeError, match="already owned"):
            lock_disk(disk)
        return fd

    monkeypatch.setattr(lifecycle, "_lock_disk", journal_appears)
    with pytest.raises(RuntimeError, match="unresolved dyld cache publication journal"):
        fixture.runtime.start()
    assert len(locks) == 1
    assert fixture.launches == []
    assert fixture.journal.read_bytes() == b"journal appeared at disk-lock boundary"
    assert _preserved_bytes(fixture) == before
    assert list(fixture.runtime.runtime_path.parent.glob("run-*")) == []
    released = lock_disk(fixture.disk)
    os.close(released)


def test_supervisor_rechecks_pending_journal_before_guest_launch(pending_runtime):
    fixture = pending_runtime
    runtime = fixture.runtime
    run_dir = runtime.runtime_path.parent / "run-fixture"
    run_dir.mkdir(parents=True)
    before = _preserved_bytes(fixture)
    fd = lifecycle._lock_disk(fixture.disk)
    fixture.journal.write_bytes(b"retained pending publication")
    args = lifecycle.argparse.Namespace(
        data=str(runtime.paths.data), name=runtime.device.name, run_dir=str(run_dir), lock_fd=fd, window=False
    )
    assert lifecycle._supervise(args) == 1
    assert "unresolved dyld cache publication journal" in runtime._metadata()["error"]
    assert fixture.launches == []
    assert fixture.journal.read_bytes() == b"retained pending publication"
    assert _preserved_bytes(fixture) == before
    released = lifecycle._lock_disk(fixture.disk)
    os.close(released)


def test_pending_journal_preserves_status_stop_and_registry_inspection(pending_runtime):
    fixture = pending_runtime
    fixture.journal.write_bytes(b"retained pending publication")
    before = _preserved_bytes(fixture)
    runtime = fixture.runtime
    assert runtime.status()["state"] == "stopped"
    assert runtime.stop(timeout=0)["state"] == "stopped"
    assert Registry(runtime.paths).get(runtime.device.name) == runtime.device
    runtime.device.validate_vm()
    assert fixture.journal.read_bytes() == b"retained pending publication"
    assert _preserved_bytes(fixture) == before
    assert fixture.launches == []


def test_start_without_pending_journal_reaches_only_mocked_launcher(pending_runtime):
    fixture = pending_runtime
    with pytest.raises(AssertionError, match="mock launcher boundary reached"):
        fixture.runtime.start()
    assert len(fixture.launches) == 1
    assert not fixture.journal.exists()
    released = lifecycle._lock_disk(fixture.disk)
    os.close(released)


@pytest.mark.parametrize("entry", ["regular", "dangling-symlink"])
def test_creation_resume_refuses_pending_cache_before_rewriting_receipt_or_starting_helpers(
    pending_runtime, monkeypatch, entry
):
    fixture = pending_runtime
    vm = fixture.disk.parent
    profile = get_profile("iphone-duo")
    markers = "\n".join(creator._STAGES) + "\n"
    (vm / ".create-state").write_text(markers)
    state_path = vm / ".limulator-create.json"
    creator._atomic_json(
        state_path,
        {
            "specification": creator._creation_specification(fixture.runtime.device),
            "state": "error",
            "stage": "verification",
            "completed": list(creator._STAGES),
        },
    )
    receipt = state_path.read_bytes()
    if entry == "regular":
        fixture.journal.write_bytes(b"retained pending publication")
    else:
        fixture.journal.symlink_to(vm / "missing-journal-target")
    before = _preserved_bytes(fixture), sorted(vm.iterdir()), fixture.journal.lstat().st_ino
    monkeypatch.setattr(
        creator,
        "_verified_firmware_stage",
        lambda *_: pytest.fail("pending cache must refuse before firmware"),
    )
    monkeypatch.setattr(creator._USBHelpers, "start", lambda *_: pytest.fail("must not start helpers"))
    with pytest.raises(RuntimeError, match="unresolved dyld cache publication journal"):
        creator._pipeline(fixture.runtime.paths, fixture.runtime.device, profile, {}, {})
    assert state_path.read_bytes() == receipt
    assert (vm / ".create-state").read_text() == markers
    assert (_preserved_bytes(fixture), sorted(vm.iterdir()), fixture.journal.lstat().st_ino) == before
    assert fixture.launches == []


@pytest.mark.parametrize("entry", ["regular", "dangling-symlink"])
def test_creation_rechecks_pending_cache_under_disk_ownership_before_resume_or_helpers(
    pending_runtime, monkeypatch, entry
):
    fixture = pending_runtime
    vm = fixture.disk.parent
    profile = get_profile("iphone-duo")
    markers = "\n".join(creator._STAGES) + "\n"
    (vm / ".create-state").write_text(markers)
    state_path = vm / ".limulator-create.json"
    creator._atomic_json(
        state_path,
        {
            "specification": creator._creation_specification(fixture.runtime.device),
            "state": "error",
            "stage": "verification",
            "completed": list(creator._STAGES),
        },
    )
    script = fixture.runtime.paths.backend / "setup/create-vm.sh"
    script.parent.mkdir()
    script.write_text("# VPHONE_CREATOR_USB_READY VPHONE_FIRST_BOOT_QMP\n")
    before = _preserved_bytes(fixture)
    lock_disk = creator._lock_disk
    locks = []

    def journal_appears(disk):
        fd = lock_disk(disk)
        locks.append(fd)
        if entry == "regular":
            fixture.journal.write_bytes(b"publication became pending at creation disk lock")
        else:
            fixture.journal.symlink_to(vm / "missing-journal-target")
        with pytest.raises(RuntimeError, match="already owned"):
            lock_disk(disk)
        return fd

    monkeypatch.setattr(creator, "_lock_disk", journal_appears)
    monkeypatch.setattr(
        creator._first_boot_check,
        "backend_contract",
        lambda _backend: {"mode": "legacy-strict", "revision": creator._first_boot_check._LEGACY},
    )
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_first_boot_qmp_path", lambda *_: fixture.runtime.paths.data / "qmp.sock")
    monkeypatch.setattr(
        creator, "_resume_completed_boot", lambda *_: pytest.fail("pending cache must refuse before resume")
    )
    monkeypatch.setattr(creator._USBHelpers, "start", lambda *_: pytest.fail("must not start helpers"))
    monkeypatch.setattr(creator._USBHelpers, "close", lambda *_: None)
    with pytest.raises(RuntimeError, match="unresolved dyld cache publication journal"):
        creator._pipeline(fixture.runtime.paths, fixture.runtime.device, profile, {}, {})
    assert len(locks) == 1
    assert fixture.launches == []
    assert (vm / ".create-state").read_text() == markers
    assert _preserved_bytes(fixture) == before
    if entry == "regular":
        assert fixture.journal.read_bytes() == b"publication became pending at creation disk lock"
    else:
        assert fixture.journal.is_symlink()
        assert os.readlink(fixture.journal) == str(vm / "missing-journal-target")
    receipt = creator._read_json(state_path)
    assert receipt["state"] == "error"
    assert "unresolved dyld cache publication journal" in receipt["error"]
    released = lock_disk(fixture.disk)
    os.close(released)
