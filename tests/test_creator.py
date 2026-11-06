import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from limulator import creator
from limulator.lifecycle import Device, Paths, Registry
from limulator.profiles import FirmwareDescriptor, get_profile, list_profiles


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "data")
    backend = tmp_path / "backend"
    (backend / "setup/data").mkdir(parents=True)
    value = {
        "devices": [{"id": profile.model, "name": profile.label} for profile in list_profiles()],
        "pairings": [
            {"device": profile.model, "osVersion": profile.os_version, "ipsw": profile.main_firmware.filename}
            for profile in list_profiles()
        ],
    }
    (backend / "setup/data/compatibility.json").write_text(json.dumps(value))
    monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
    host_lock = tmp_path / "host-creation.lock"
    host_lock.touch(mode=0o644)
    monkeypatch.setattr(
        creator, "_host_creation_lock", lambda env: creator._lock_transport_file(host_lock, os.getuid())
    )
    monkeypatch.setattr(creator, "_active_vhost_cids", lambda: {})
    return paths


@pytest.mark.parametrize("name", ["../other", "/absolute", "", ".hidden"])
def test_creation_rejects_unsafe_name_before_writing(catalog, name):
    with pytest.raises(ValueError, match="device name"):
        creator.create_device(catalog, name, "iPhone19,4", "27.1", 41, 3)
    assert not catalog.data.exists()


def test_unknown_pair_and_unverified_duo_scale_are_rejected(catalog):
    with pytest.raises(ValueError, match="no profile"):
        creator.create_device(catalog, "duo", "iPhone19,4", "18.0", 41, 3)
    with pytest.raises(ValueError, match="requires scale 3"):
        creator.create_device(catalog, "duo", "iPhone19,4", "27.1", 41, 2)
    assert not catalog.data.exists()


def test_creation_lock_refuses_concurrent_restore_transport(catalog):
    catalog.data.mkdir(parents=True)
    with (catalog.data / ".creation.lock").open("a") as existing:
        creator.fcntl.flock(existing, creator.fcntl.LOCK_EX | creator.fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="shared restore transport"):
            creator.create_device(catalog, "duo", "iPhone19,4", "27.1", 41, 3)
    assert not catalog.vms.exists()


def _registered_device(paths, name, profile_id, cid):
    profile = get_profile(profile_id)
    vm = paths.vms / name
    vm.mkdir(parents=True)
    (vm / "Disk.img").write_bytes(b"existing VM data")
    (vm / "config.plist").write_bytes(plistlib.dumps({"diskImage": "Disk.img"}))
    device = Device(
        name, vm, profile.model, profile.os_version, cid, profile.width, profile.height, profile.scale
    )
    Registry(paths).register(device)
    return device


def test_missing_cid_uses_free_registered_and_pending_reservations(catalog, monkeypatch):
    _registered_device(catalog, "duo", "iphone-duo", 41)
    pending = catalog.vms / "pending-tablet"
    pending.mkdir(parents=True)
    creator._atomic_json(
        pending / ".limulator-create.json",
        {
            "specification": {
                "name": "pending-tablet",
                "model": "iPad16,1",
                "ios": "27.0.1",
                "cid": 42,
                "scale": 2,
            }
        },
    )
    seen = []

    def create(_paths, candidate, profile, _pairing):
        seen.append((candidate, profile))
        return {"state": "created"}

    monkeypatch.setattr(creator, "_create_locked", create)
    result = creator.create_device(catalog, "phone", "iPhone17,3", "27.0.1", None, None)
    assert result["cid"] == 43
    assert (seen[0][0].width, seen[0][0].height, seen[0][0].scale) == (1179, 2556, 3)
    monkeypatch.setattr(creator, "_create_locked", lambda *args: pytest.fail("collision reached restore"))
    with pytest.raises(ValueError, match="CID 41 is already registered"):
        creator.create_device(catalog, "phone", "iPhone17,3", "27.0.1", 41, None)
    with pytest.raises(ValueError, match="CID 42 is already registered or reserved"):
        creator.create_device(catalog, "phone", "iPhone17,3", "27.0.1", 42, None)


def test_missing_cid_and_scale_reuse_legacy_duo_resume_specification(catalog, monkeypatch):
    vm = catalog.vms / "duo"
    vm.mkdir(parents=True)
    old = {
        "specification": {"name": "duo", "model": "iPhone19,4", "ios": "27.1", "cid": 48, "scale": 3},
        "completed": ["bundle", "firmware", "gpu-driver", "prepare", "patch"],
    }
    creator._atomic_json(vm / ".limulator-create.json", old)
    seen = []
    monkeypatch.setattr(
        creator,
        "_create_locked",
        lambda _paths, candidate, *_: seen.append(candidate) or {"state": "created"},
    )
    assert creator.create_device(catalog, "duo", "iPhone19,4", "27.1", None, None)["cid"] == 48
    assert seen[0].scale == 3
    assert creator._read_json(vm / ".limulator-create.json") == old
    with pytest.raises(ValueError, match="CID differs"):
        creator.create_device(catalog, "duo", "iPhone19,4", "27.1", 49, None)


def test_active_external_vhost_cid_is_avoided_before_creation(catalog, monkeypatch):
    monkeypatch.setattr(creator, "_active_vhost_cids", lambda: {41: 12345})
    monkeypatch.setattr(creator, "_create_locked", lambda *args: {"state": "created"})
    assert creator.create_device(catalog, "duo", "iPhone19,4", "27.1", None, None)["cid"] == 42
    with pytest.raises(ValueError, match="live QEMU process 12345"):
        creator.create_device(catalog, "duo", "iPhone19,4", "27.1", 41, None)


def test_host_lock_serializes_different_data_directories(catalog, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def create(_paths, candidate, *_args):
        if candidate.name == "first":
            entered.set()
            assert release.wait(timeout=5)
        return {"state": "created"}

    monkeypatch.setattr(creator, "_create_locked", create)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(creator.create_device, catalog, "first", "iPhone19,4", "27.1", None, None)
        try:
            assert entered.wait(timeout=3)
            with pytest.raises(RuntimeError, match="shared restore transport"):
                creator.create_device(
                    Paths(tmp_path / "other-data"), "second", "iPhone19,4", "27.1", None, None
                )
        finally:
            release.set()
        assert first.result(timeout=3)["cid"] == 41
    assert (
        creator.create_device(Paths(tmp_path / "other-data"), "second", "iPhone19,4", "27.1", None, None)[
            "cid"
        ]
        == 41
    )


@pytest.mark.parametrize("profile", list_profiles(), ids=lambda profile: profile.id)
def test_profile_configuration_sets_native_product_and_geometry(tmp_path, profile):
    vm = tmp_path / profile.id
    vm.mkdir()
    original = {
        "machineIdentifier": b"preserved identity",
        "memorySize": 8 << 30,
        "screenConfig": {"width": 1290, "height": 2796},
    }
    (vm / "config.plist").write_bytes(plistlib.dumps(original))
    (vm / "Disk.img").write_bytes(b"preserved disk")
    creator._apply_profile_configuration(vm, profile, profile.scale)
    actual = plistlib.loads((vm / "config.plist").read_bytes())
    assert actual["machineIdentifier"] == original["machineIdentifier"]
    assert actual["memorySize"] == original["memorySize"]
    assert actual["guestProductType"] == profile.model
    assert actual["screenConfig"] == {
        "width": profile.width,
        "height": profile.height,
        "pixelsPerInch": profile.pixels_per_inch,
        "scale": profile.scale,
    }
    assert (vm / "gfx-res").read_text().strip() == f"{profile.width}x{profile.height}"
    assert (vm / "Disk.img").read_bytes() == b"preserved disk"


def test_existing_unrecognized_vm_is_never_recreated(catalog):
    vm = catalog.vms / "duo"
    vm.mkdir(parents=True)
    disk = vm / "Disk.img"
    disk.write_bytes(b"interrupted-user-disk")
    candidate = Device("duo", vm, "iPhone19,4", "27.1")
    with pytest.raises(RuntimeError, match="preserving"):
        creator._pipeline(catalog, candidate, {}, {}, {})
    assert disk.read_bytes() == b"interrupted-user-disk"
    assert not (vm / ".limulator-create.json").exists()


def test_resumed_creation_preserves_previous_native_log_bytes(tmp_path):
    vm, run = tmp_path / "vm", tmp_path / "new-run"
    previous = vm / ".runs-dfu"
    previous.mkdir(parents=True)
    run.mkdir()
    (previous / "serial.log").write_bytes(b"previous boot\r\n\xff")
    (previous / "restore.log").write_bytes(b"previous restore failure\n")
    creator._archive_creation_logs(vm, run)
    (previous / "serial.log").write_bytes(b"new boot")
    assert (run / "previous-runs-dfu/serial.log").read_bytes() == b"previous boot\r\n\xff"
    assert (run / "previous-runs-dfu/restore.log").read_bytes() == b"previous restore failure\n"


def _incomplete_vm(paths):
    vm = paths.vms / "duo"
    vm.mkdir(parents=True)
    with (vm / "Disk.img").open("wb") as stream:
        stream.write(b"interrupted APFS media")
        stream.truncate(1 << 20)
    (vm / "config.plist").write_bytes(
        plistlib.dumps({"diskImage": "Disk.img", "screenConfig": {"width": 12, "height": 24}})
    )
    for name in ("ecid", "nvram.bin", "SEPStorage", "gfx-res", "gfx-res.3x"):
        (vm / name).write_bytes(f"previous {name}".encode())
    (vm / ".create-state").write_text("sanity\nbundle\nfirmware\ngpu-driver\nprepare\npatch\n")
    receipt = {
        "specification": {"name": "duo", "model": "iPhone19,4", "ios": "27.1", "cid": 41, "scale": 3},
        "state": "error",
        "stage": "restore",
    }
    creator._atomic_json(vm / ".limulator-create.json", receipt)
    return Device("duo", vm, "iPhone19,4", "27.1"), receipt


def _unregistered_booted_vm(paths):
    candidate, receipt = _incomplete_vm(paths)
    (candidate.vm_path / ".create-state").write_bytes(
        b"sanity\r\nbundle\r\nfirmware\r\ngpu-driver\r\nprepare\r\npatch\r\nrestore\r\ncfw\r\nbooted\r\n"
    )
    receipt.update(completed=list(creator._STAGES), stage="verification")
    creator._atomic_json(candidate.vm_path / ".limulator-create.json", receipt)
    return candidate, receipt


def test_verification_resume_preserves_state_and_only_reselects_first_boot(catalog, monkeypatch):
    candidate, _ = _unregistered_booted_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    original, inode = disk.read_bytes(), disk.stat().st_ino
    markers = (vm / ".create-state").read_bytes()
    script = catalog.backend / "setup/create-vm.sh"
    script.write_text("# LIMULATOR_CREATOR_USB_READY\n")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})

    def unavailable(_self):
        raise RuntimeError("pause before native launch")

    monkeypatch.setattr(creator._USBHelpers, "start", unavailable)
    host_fd = creator._host_creation_lock({})
    try:
        with pytest.raises(RuntimeError, match="pause before native launch"):
            creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, {})
    finally:
        os.close(host_fd)
    receipt = creator._read_json(vm / ".limulator-create.json")
    record = receipt["verification_resume"]
    assert record["selected_stages"] == ["booted"]
    assert creator.Path(record["preserved_state"]).read_bytes() == markers
    assert record["previous_state_sha256"] == hashlib.sha256(markers).hexdigest()
    assert (vm / ".create-state").read_bytes() == markers.replace(b"booted\r\n", b"")
    assert creator._completed(vm) == list(creator._STAGES[:-1])
    assert receipt["stage"] == "booted"
    assert receipt["state"] == "error"
    assert disk.read_bytes() == original
    assert disk.stat().st_ino == inode
    assert Registry(catalog).list() == []


@pytest.mark.parametrize("refusal", ["registered", "mismatched", "ready", "missing-cfw"])
def test_verification_resume_refuses_nonmatching_or_completed_device(catalog, refusal):
    candidate, receipt = _unregistered_booted_vm(catalog)
    if refusal == "registered":
        Registry(catalog).register(candidate)
    elif refusal == "mismatched":
        receipt["specification"]["cid"] = 42
    elif refusal == "ready":
        receipt["state"] = "ready"
    else:
        state_path = candidate.vm_path / ".create-state"
        state_path.write_bytes(state_path.read_bytes().replace(b"cfw\r\n", b""))
    run = candidate.vm_path / "new-run"
    run.mkdir()
    original = (candidate.vm_path / ".create-state").read_bytes()
    disk_fd = creator._lock_disk(candidate.vm_path / "Disk.img")
    try:
        with pytest.raises(RuntimeError, match="registered device|matching incomplete"):
            creator._resume_completed_boot(catalog, candidate, receipt, run, disk_fd)
    finally:
        os.close(disk_fd)
    assert (candidate.vm_path / ".create-state").read_bytes() == original
    assert list(run.iterdir()) == []


def test_verification_resume_atomic_write_failure_preserves_all_stages(catalog, monkeypatch):
    candidate, receipt = _unregistered_booted_vm(catalog)
    vm, run = candidate.vm_path, candidate.vm_path / "new-run"
    run.mkdir()
    markers = (vm / ".create-state").read_bytes()
    disk_fd = creator._lock_disk(vm / "Disk.img")

    def unavailable(*_args):
        raise OSError("state replacement unavailable")

    monkeypatch.setattr(creator.os, "replace", unavailable)
    try:
        with pytest.raises(OSError, match="state replacement unavailable"):
            creator._resume_completed_boot(catalog, candidate, receipt, run, disk_fd)
    finally:
        os.close(disk_fd)
    assert (vm / ".create-state").read_bytes() == markers
    assert (run / ".create-state.before-verification-resume").read_bytes() == markers


def test_verification_resume_refuses_an_external_open_disk(catalog):
    candidate, receipt = _unregistered_booted_vm(catalog)
    vm, run = candidate.vm_path, candidate.vm_path / "new-run"
    run.mkdir()
    markers = (vm / ".create-state").read_bytes()
    disk_fd = creator._lock_disk(vm / "Disk.img")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; f=open(sys.argv[1],'rb'); print('ready',flush=True); sys.stdin.read()",
            str(vm / "Disk.img"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="active owner"):
            creator._resume_completed_boot(catalog, candidate, receipt, run, disk_fd)
    finally:
        process.communicate("", timeout=5)
        os.close(disk_fd)
    assert (vm / ".create-state").read_bytes() == markers
    assert list(run.iterdir()) == []


def test_fresh_disk_preserves_original_bytes_identity_companions_and_both_locks(catalog):
    candidate, state = _incomplete_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    old_bytes, old_inode = disk.read_bytes(), disk.stat().st_ino
    previous_receipt = (vm / ".limulator-create.json").read_bytes()
    old_fd = creator._lock_disk(disk)
    new_fd = None
    try:
        new_fd, record = creator._prepare_fresh_disk(vm, state, vm / ".limulator-create.json", old_fd)
        backup = creator.Path(record["disk"]["path"])
        assert backup.read_bytes() == old_bytes
        assert backup.stat().st_ino == old_inode == record["disk"]["inode"]
        assert record["disk"]["sha256"] == hashlib.sha256(old_bytes).hexdigest()
        assert record["disk"]["size"] == disk.stat().st_size == len(old_bytes)
        assert disk.read_bytes() == bytes(len(old_bytes))
        assert disk.stat().st_blocks == 0
        assert stat.S_IMODE(disk.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
        assert (backup.parent / ".limulator-create.json").read_bytes() == previous_receipt
        for name, digest in record["companions"].items():
            assert (backup.parent / name).read_bytes() == (vm / name).read_bytes()
            assert hashlib.sha256((vm / name).read_bytes()).hexdigest() == digest
        for owned in (disk, backup):
            with pytest.raises(RuntimeError, match="already owned"):
                creator._lock_disk(owned)
        assert creator._read_json(vm / ".limulator-create.json")["fresh_disks"][-1] == record
    finally:
        if new_fd is not None:
            os.close(new_fd)
        os.close(old_fd)


def test_preservation_rename_never_overwrites_an_existing_backup(tmp_path):
    source, backup = tmp_path / "Disk.img", tmp_path / "preserved-Disk.img"
    source.write_bytes(b"current disk")
    backup.write_bytes(b"existing backup")
    with pytest.raises(FileExistsError):
        creator._rename_preserved(source, backup)
    assert source.read_bytes() == b"current disk"
    assert backup.read_bytes() == b"existing backup"


def test_fresh_disk_rollback_refuses_to_replace_an_unexpected_inode(catalog):
    candidate, state = _incomplete_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    original = disk.read_bytes()
    old_fd = creator._lock_disk(disk)
    new_fd = None
    try:
        new_fd, record = creator._prepare_fresh_disk(vm, state, vm / ".limulator-create.json", old_fd)
        disk.rename(vm / "other-replacement.img")
        disk.write_bytes(b"unrelated replacement")
        with pytest.raises(RuntimeError, match="replacement disk has another owner"):
            creator._rollback_fresh_disk(vm, record, old_fd, new_fd)
        assert disk.read_bytes() == b"unrelated replacement"
        assert creator.Path(record["disk"]["path"]).read_bytes() == original
    finally:
        if new_fd is not None:
            os.close(new_fd)
        os.close(old_fd)


def test_fresh_disk_allocation_failure_restores_exact_original_path(catalog, monkeypatch):
    candidate, state = _incomplete_vm(catalog)
    disk = candidate.vm_path / "Disk.img"
    original, inode = disk.read_bytes(), disk.stat().st_ino
    old_fd = creator._lock_disk(disk)
    monkeypatch.setattr(creator.os, "ftruncate", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    try:
        with pytest.raises(OSError, match="disk full"):
            creator._prepare_fresh_disk(
                candidate.vm_path, state, candidate.vm_path / ".limulator-create.json", old_fd
            )
        assert disk.stat().st_ino == inode
        assert disk.read_bytes() == original
        record = creator._read_json(candidate.vm_path / ".limulator-create.json")["fresh_disks"][-1]
        assert record["state"] == "rolled-back"
        assert (creator.Path(record["disk"]["path"]).parent / "unused-Disk.img").exists()
    finally:
        os.close(old_fd)


@pytest.mark.parametrize("fresh_disk", [False, True])
def test_prelaunch_failure_preserves_disk_and_rolls_back_explicit_recovery(catalog, monkeypatch, fresh_disk):
    candidate, _ = _incomplete_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    original, inode = disk.read_bytes(), disk.stat().st_ino
    config = (vm / "config.plist").read_bytes()
    previous_receipt = (vm / ".limulator-create.json").read_bytes()
    script = catalog.backend / "setup/create-vm.sh"
    script.write_text("# LIMULATOR_CREATOR_USB_READY\n")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})

    def unavailable(_self):
        raise RuntimeError("private USB setup unavailable")

    monkeypatch.setattr(creator._USBHelpers, "start", unavailable)
    with pytest.raises(RuntimeError, match="private USB setup unavailable"):
        creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, {}, fresh_disk=fresh_disk)
    assert disk.read_bytes() == original
    assert disk.stat().st_ino == inode
    receipt = creator._read_json(vm / ".limulator-create.json")
    if fresh_disk:
        record = receipt["fresh_disks"][-1]
        assert record["state"] == "rolled-back"
        assert (vm / "config.plist").read_bytes() == config
        assert (
            creator.Path(record["disk"]["path"]).parent / ".limulator-create.json"
        ).read_bytes() == previous_receipt
    else:
        assert "fresh_disks" not in receipt
    released = creator._lock_disk(disk)
    os.close(released)


@pytest.mark.parametrize("mismatch", ["missing", "specification", "restore", "ready"])
def test_fresh_disk_requires_matching_incomplete_receipt(catalog, mismatch):
    candidate, receipt = _incomplete_vm(catalog)
    if mismatch == "missing":
        receipt = None
    elif mismatch == "specification":
        receipt["specification"]["cid"] = 42
    elif mismatch == "restore":
        with (candidate.vm_path / ".create-state").open("a") as stream:
            stream.write("restore\n")
    else:
        receipt["state"] = "ready"
    disk = candidate.vm_path / "Disk.img"
    original = disk.read_bytes()
    with pytest.raises(ValueError, match="matching incomplete|incomplete restore"):
        creator._fresh_disk_allowed(catalog, candidate, receipt)
    assert disk.read_bytes() == original
    assert not list(candidate.vm_path.glob("preserved-disk-*"))


def test_fresh_disk_refuses_registered_device_before_restore(catalog):
    device = _registered_device(catalog, "duo", "iphone-duo", 41)
    original = (device.vm_path / "Disk.img").read_bytes()
    with pytest.raises(ValueError, match="registered device"):
        creator.create_device(catalog, "duo", "iPhone19,4", "27.1", None, None, fresh_disk=True)
    assert (device.vm_path / "Disk.img").read_bytes() == original


def test_fresh_disk_refuses_an_external_open_disk(catalog):
    candidate, state = _incomplete_vm(catalog)
    disk = candidate.vm_path / "Disk.img"
    old_fd = creator._lock_disk(disk)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; f=open(sys.argv[1],'rb'); print('ready',flush=True); sys.stdin.read()",
            str(disk),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="active owner"):
            creator._prepare_fresh_disk(
                candidate.vm_path, state, candidate.vm_path / ".limulator-create.json", old_fd
            )
        assert not list(candidate.vm_path.glob("preserved-disk-*"))
    finally:
        process.communicate("", timeout=5)
        os.close(old_fd)


def test_firmware_digest_failure_does_not_modify_download(tmp_path):
    firmware = tmp_path / "firmware.ipsw"
    firmware.write_bytes(b"public-firmware-test-data")
    digest = hashlib.sha256(firmware.read_bytes()).hexdigest()
    verified = creator._verify_firmware(firmware, digest)
    assert verified["sha256"] == digest
    with pytest.raises(RuntimeError, match="firmware SHA-256 mismatch"):
        creator._verify_firmware(firmware, "0" * 64)
    assert firmware.read_bytes() == b"public-firmware-test-data"


def test_firmware_stage_is_recorded_only_after_both_downloads_are_verified(tmp_path):
    vm = tmp_path / "vm"
    vm.mkdir()
    ios, cloud = tmp_path / "ios.ipsw", tmp_path / "cloud.ipsw"
    ios.write_bytes(b"ios")
    cloud.write_bytes(b"wrong")
    files = tuple(
        (
            path,
            FirmwareDescriptor(
                path.name,
                "https://updates.cdn-apple.com/test",
                hashlib.sha256(data).hexdigest(),
                len(data),
                "verified",
            ),
        )
        for path, data in ((ios, b"ios"), (cloud, b"cloud"))
    )
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        creator._verified_firmware_stage(vm, files)
    assert creator._completed(vm) == []
    cloud.write_bytes(b"cloud")
    receipts = creator._verified_firmware_stage(vm, files)
    assert [receipt["size"] for receipt in receipts] == [3, 5]
    assert creator._completed(vm) == ["firmware"]
    creator._verified_firmware_stage(vm, files)
    assert (vm / ".create-state").read_text().splitlines() == ["firmware"]


def test_creator_refuses_incomplete_non_gpu_backend(catalog, monkeypatch):
    options = catalog.backend / "qemu/build/meson-info/intro-buildoptions.json"
    options.parent.mkdir(parents=True)
    options.write_text(json.dumps([{"name": "pvg", "value": "disabled"}]))
    monkeypatch.setattr(creator, "_resource_guard", lambda: None)
    with pytest.raises(RuntimeError, match="required paravirtual GPU"):
        creator.create_device(catalog, "duo", "iPhone19,4", "27.1", 41, 3)
    assert not catalog.vms.exists()


def test_private_helpers_refuse_existing_physical_apple_device(catalog, tmp_path, monkeypatch):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    monkeypatch.setattr(creator, "_apple_devices", lambda: ["/sys/devices/pci/usb/phone"])
    monkeypatch.setattr(creator, "_sudo", lambda *args, **kwargs: pytest.fail("must not mutate host"))
    with pytest.raises(RuntimeError, match="will not take over live devices"):
        helper.start()


def test_attacher_refuses_unowned_local_listener(catalog, tmp_path, monkeypatch):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.daemon_identity = creator._identity(os.getpid())
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: [1234])
    monkeypatch.setattr(creator, "_in_scope", lambda *_: False)
    monkeypatch.setattr(
        creator, "_sudo", lambda *args, **kwargs: pytest.fail("unowned listener must not be attached")
    )
    with pytest.raises(RuntimeError, match="does not belong"):
        helper.poll()


def test_attacher_only_requests_the_owned_exported_apple_bus(catalog, tmp_path, monkeypatch):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.daemon_identity = creator._identity(os.getpid())
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: [1234])
    monkeypatch.setattr(creator, "_in_scope", lambda *_: True)
    states = iter([{}, {}, {0: "0-0"}])
    monkeypatch.setattr(creator, "_vhci_ports", lambda: next(states))
    monkeypatch.setattr(
        creator, "_run", lambda *args, **kwargs: SimpleNamespace(stdout="  1-1: Apple DFU (05ac:1227)\n")
    )
    calls = []

    def attach(command, *_args, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(creator, "_sudo", attach)
    helper.poll()
    assert calls == [["usbip", "attach", "-r", "127.0.0.1", "-b", "1-1"]]
    assert helper.owned_ports == {0}


def test_vhci_parser_counts_import_before_device_gets_its_usb_address(tmp_path, monkeypatch):
    status = tmp_path / "status"
    status.write_text(
        "hub port sta spd dev sockfd local_busid\n"
        "hs 0000 005 003 00010001 000003 0-0\n"
        "hs 0001 006 003 00010001 000005 11-2\n"
        "hs 0002 004 000 00000000 000000 0-0\n"
    )
    glob = creator.Path.glob

    def inventory(path, pattern):
        return [status] if str(path) == "/sys/devices/platform" else glob(path, pattern)

    monkeypatch.setattr(creator.Path, "glob", inventory)
    assert creator._vhci_ports() == {0: "0-0", 1: "11-2"}


def test_private_daemon_and_rule_are_cleaned_without_global_service_operations(
    catalog, tmp_path, monkeypatch
):
    native = catalog.backend / ".build/usbmuxd-32k/usbmuxd"
    native.parent.mkdir(parents=True)
    native.write_text(
        f"#!{sys.executable}\nimport socket,sys,time\n"
        "s=socket.socket(socket.AF_UNIX)\ns.bind(sys.argv[sys.argv.index('-S')+1])\ns.listen()\ntime.sleep(60)\n"
    )
    native.chmod(0o700)
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: [])
    monkeypatch.setattr(creator, "_vhci_ports", lambda: {})

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def setsockopt(self, *args):
            return None

        def bind(self, address):
            assert address == ("127.0.0.1", 3240)

    monkeypatch.setattr(creator.socket, "socket", lambda *args, **kwargs: Probe())
    commands = []

    def privileged(command, *_args, **kwargs):
        commands.append((list(map(str, command)), kwargs.get("input")))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(creator, "_sudo", privileged)
    try:
        helper.start()
        assert (tmp_path / "usbmuxd.sock").is_socket()
        assert helper.env["USBMUXD_SOCKET_ADDRESS"] == f"UNIX:{tmp_path / 'usbmuxd.sock'}"
        rule = next(body for command, body in commands if command[0] == "install")
        assert 'KERNELS=="vhci_hcd.*"' in rule
        assert f'OWNER:="{os.getuid()}"' in rule
        assert 'MODE:="0600"' in rule
        assert not any(command[0] in {"systemctl", "pkill"} for command, _ in commands)
    finally:
        helper.close()
    assert helper.daemon.poll() is not None
    assert any(command == ["rm", "--", str(helper.rule)] for command, _ in commands)


def test_final_guest_shutdown_refuses_unowned_process(catalog, tmp_path, monkeypatch):
    (tmp_path / ".qemu.pid").write_text(str(os.getpid()))
    monkeypatch.setattr(creator, "_in_scope", lambda *_: False)
    monkeypatch.setattr(
        creator, "GuestClient", lambda **kwargs: pytest.fail("must not contact unowned guest")
    )
    with pytest.raises(RuntimeError, match="not an owned process"):
        creator._stop_creation_guest(tmp_path, 41, "foreign.scope")


def test_signal_refuses_reused_process_identity():
    identity = creator._identity(os.getpid())
    stale = {**identity, "start_ticks": identity["start_ticks"] + 1}
    assert creator._signal_owned(stale, creator.signal.SIGTERM) is False
    assert creator._identity_alive(identity)


def test_ready_guest_must_report_the_requested_ios_version():
    class Guest:
        version = "26.4"

        def rpc(self, method, params):
            assert (method, params) == ("device.info", {})
            return {"ios_version": self.version, "model": "iPhone Duo"}

    guest = Guest()
    with pytest.raises(RuntimeError, match="reports iOS '26.4'; requested iOS 27.1"):
        creator._verified_guest_info(guest, "27.1")
    guest.version = "27.1"
    assert creator._verified_guest_info(guest, "27.1")["ios_version"] == "27.1"


def test_sudo_shim_preserves_known_native_transport_environment(catalog, tmp_path):
    env = creator._native_env(catalog)
    env.update(USBMUXD_SOCKET="/tmp/private.sock", USBMUXD_SOCKET_ADDRESS="UNIX:/tmp/private.sock")
    creator._sudo_shim(tmp_path, env)
    # The real sudo only executes env for this smoke check; no privileged writes occur.
    result = subprocess.run(
        [str(tmp_path / "bin/sudo"), "env"], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert values["USBMUXD_SOCKET_ADDRESS"] == "UNIX:/tmp/private.sock"
    assert values["VPHONE_RCODESIGN"] == str(catalog.prefix / "bin/rcodesign")
