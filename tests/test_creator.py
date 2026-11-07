import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from limulator import _creation_cleanup, creator
from limulator.lifecycle import Device, Paths, Registry
from limulator.profiles import FirmwareDescriptor, get_profile, list_profiles


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    (backend / "setup/data").mkdir(parents=True)
    value = {
        "devices": [{"id": profile.model, "name": profile.label} for profile in list_profiles()],
        "pairings": [
            {
                "device": profile.model,
                "osVersion": profile.os_version,
                "ipsw": profile.main_firmware.filename,
                "build": profile.main_firmware.filename.split("_")[-2],
            }
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
    # Keep unrelated pipeline fixtures below the real Unix-socket path limit on CI too.
    with tempfile.TemporaryDirectory(prefix="lim-cr-", dir="/tmp") as temporary:
        yield Paths(creator.Path(temporary) / "data")


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


class _SettingsGuest:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def rpc(self, method, params):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.reply


def _migration_settings(*, build="24A94425", success=True):
    return {
        "values": {
            "DMLastMigrationResults": {
                "type": "json",
                "value": {"buildVersion": build, "success": success},
            }
        }
    }


def test_expected_migration_build_uses_pinned_firmware_and_rejects_catalog_conflict():
    profile = get_profile("iphone-duo")

    assert creator._expected_migration_build(profile, {"build": "24A94425"}) == "24A94425"
    assert creator._expected_migration_build(profile, {"build": "unknown"}) == "24A94425"
    with pytest.raises(RuntimeError, match="contradicts pinned profile firmware build"):
        creator._expected_migration_build(profile, {"build": "24A446"})


def test_expected_migration_build_refuses_profile_without_build_in_firmware_filename():
    profile = SimpleNamespace(id="fixture", main_firmware=SimpleNamespace(filename="unversioned.ipsw"))

    with pytest.raises(RuntimeError, match="does not identify a build version"):
        creator._expected_migration_build(profile, {"build": "unknown"})


def test_creation_rejects_failed_migration_even_when_setup_build_marker_matches(tmp_path):
    expected = {"data_migration_done": True, "pending": False}
    result = {"buildVersion": "24A94425", "success": False}
    state_path = tmp_path / ".limulator-create.json"
    state = {"stage": "verification"}

    with pytest.raises(RuntimeError, match="success=false"):
        creator._verify_guest_migration(
            _SettingsGuest(_migration_settings(success=False)),
            expected,
            state_path,
            state,
            "24A94425",
        )

    receipt = creator._read_json(state_path)
    assert receipt["migration_result"] == result
    assert receipt["setup"] == expected
    assert receipt["migration_expected_build"] == "24A94425"


def test_creation_accepts_successful_migration_for_expected_build(tmp_path):
    guest = _SettingsGuest(_migration_settings())
    result = creator._verify_guest_migration(
        guest,
        {"data_migration_done": True, "pending": False},
        tmp_path / ".limulator-create.json",
        {},
        "24A94425",
    )

    assert result == {"buildVersion": "24A94425", "success": True}
    assert guest.calls == [("settings.get", {"domain": "com.apple.migration"})]


def test_creation_rejects_migration_result_for_wrong_build(tmp_path):
    state_path = tmp_path / ".limulator-create.json"
    with pytest.raises(RuntimeError, match="expected '24A94425'"):
        creator._verify_guest_migration(
            _SettingsGuest(_migration_settings(build="24A446")),
            {"data_migration_done": True, "pending": False},
            state_path,
            {},
            "24A94425",
        )
    assert creator._read_json(state_path)["migration_result"]["buildVersion"] == "24A446"


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (None, "unsupported migration response"),
        ({"values": {}}, "missing DMLastMigrationResults"),
        ({"values": {"DMLastMigrationResults": {}}}, "unsupported DMLastMigrationResults value"),
    ],
)
def test_creation_refuses_missing_or_malformed_migration_results(tmp_path, reply, message):
    state_path = tmp_path / ".limulator-create.json"
    with pytest.raises(RuntimeError, match=message):
        creator._verify_guest_migration(
            _SettingsGuest(reply),
            {"data_migration_done": True, "pending": False},
            state_path,
            {},
            "24A94425",
        )
    receipt = creator._read_json(state_path)
    assert receipt["migration_result"] is None
    assert message in receipt["migration_result_read_error"]


def test_creation_reports_unsupported_migration_read_separately(tmp_path):
    guest = _SettingsGuest(error=creator.ProtocolError("settings.get is unsupported"))
    state_path = tmp_path / ".limulator-create.json"

    with pytest.raises(RuntimeError, match="could not be read with settings.get"):
        creator._verify_guest_migration(
            guest,
            {"data_migration_done": True, "pending": False},
            state_path,
            {},
            "24A94425",
        )

    receipt = creator._read_json(state_path)
    assert receipt["migration_result"] is None
    assert "settings.get is unsupported" in receipt["migration_result_read_error"]


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
    script.write_text("# LIMULATOR_CREATOR_USB_READY LIMULATOR_FIRST_BOOT_QMP\n")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    environment = {"LIMULATOR_FIRST_BOOT_QMP": "/tmp/previous-shared-qmp.sock"}

    def unavailable(_self):
        raise RuntimeError("pause before native launch")

    monkeypatch.setattr(creator._USBHelpers, "start", unavailable)
    host_fd = creator._host_creation_lock({})
    try:
        with pytest.raises(RuntimeError, match="pause before native launch"):
            creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, environment)
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
    qmp_path = creator.Path(receipt["first_boot_qmp"])
    assert qmp_path == creator.Path(receipt["run_dir"]) / "qmp.sock"
    assert environment["LIMULATOR_FIRST_BOOT_QMP"] == str(qmp_path)
    assert stat.S_IMODE(qmp_path.parent.stat().st_mode) == 0o700
    assert qmp_path.parent.stat().st_uid == os.getuid()
    assert disk.read_bytes() == original
    assert disk.stat().st_ino == inode
    assert Registry(catalog).list() == []


@pytest.mark.parametrize("component", ["comma,path", "line\nbreak", "carriage\rreturn", "x" * 100])
def test_first_boot_qmp_rejects_unsupported_socket_paths(tmp_path, component):
    with pytest.raises(ValueError, match="first-boot QMP path"):
        creator._first_boot_qmp_path(tmp_path / component)


def test_long_first_boot_socket_path_fails_before_native_work_and_records_safe_error(catalog, monkeypatch):
    paths = Paths(catalog.data / ("long-data-" * 12))
    candidate, receipt = _incomplete_vm(paths)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    original, inode = disk.read_bytes(), disk.stat().st_ino
    markers = (vm / ".create-state").read_bytes()
    receipt["first_boot_qmp"] = "/tmp/previous-run/qmp.sock"
    creator._atomic_json(vm / ".limulator-create.json", receipt)
    monkeypatch.setattr(
        creator, "_verified_firmware_stage", lambda *args: pytest.fail("firmware work must not begin")
    )
    monkeypatch.setattr(creator._USBHelpers, "start", lambda self: pytest.fail("USB setup must not begin"))
    monkeypatch.setattr(creator._USBHelpers, "close", lambda self: None)
    monkeypatch.setattr(creator.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("no native launch"))
    with pytest.raises(ValueError, match="shorten LIMULATOR_HOME"):
        creator._pipeline(paths, candidate, get_profile("iphone-duo"), {}, {})
    result = creator._read_json(vm / ".limulator-create.json")
    assert result["state"] == "error"
    assert "shorten LIMULATOR_HOME" in result["error"]
    assert "first_boot_qmp" not in result
    assert result["completed"] == creator._completed(vm)
    assert creator.Path(result["run_dir"]).parent == vm
    assert disk.read_bytes() == original
    assert disk.stat().st_ino == inode
    assert (vm / ".create-state").read_bytes() == markers


@pytest.fixture
def patched_first_boot(tmp_path):
    source = Paths().backend / "setup/create-vm.sh"
    if not source.is_file():
        pytest.skip("the external vphone-linux setup source is required")
    target = tmp_path / "setup/create-vm.sh"
    target.parent.mkdir()
    target.write_bytes(source.read_bytes())
    patch = creator.Path(__file__).resolve().parents[1] / "patches/0012-first-boot-qmp.patch"
    check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=tmp_path, capture_output=True)
    if check.returncode == 0:
        subprocess.run(["git", "apply", str(patch)], cwd=tmp_path, check=True, capture_output=True)
    else:
        subprocess.run(
            ["git", "apply", "--reverse", "--check", str(patch)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
    result = re.search(r"^boot_vm\(\) \{.*?^\}", target.read_text(), re.MULTILINE | re.DOTALL)
    assert result is not None
    return result.group()


@pytest.mark.parametrize("qmp,already_running", [(False, False), (True, False), (True, True)])
def test_first_boot_qmp_is_optional_and_never_restarts_an_existing_guest(
    tmp_path, patched_first_boot, qmp, already_running
):
    vm = tmp_path / "VM with spaces"
    vm.mkdir()
    arguments = tmp_path / "recorded-arguments"
    arguments.write_bytes(b"not launched")
    launcher = tmp_path / "fake run-vphone.sh"
    launcher.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$ARG_LOG"\n')
    launcher.chmod(0o700)
    socket = tmp_path / "private run $(literal); path" / "qmp.sock"
    env = os.environ | {
        "VM": str(vm),
        "QEMU_RUN": str(launcher),
        "QEMU_PIDFILE": str(vm / ".qemu.pid"),
        "ARG_LOG": str(arguments),
        "CID": "41",
        "GFX": "1",
        "WINDOW": "0",
    }
    env.pop("LIMULATOR_FIRST_BOOT_QMP", None)
    if qmp:
        env["LIMULATOR_FIRST_BOOT_QMP"] = str(socket)
    script = (
        "set -Eeuo pipefail\n"
        "log() { :; }\n"
        "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        "vphoned_up() { return 0; }\n"
        f"running_vm() {{ QEMU_PID=987654; return {0 if already_running else 1}; }}\n"
        + patched_first_boot
        + "\nboot_vm\n"
        + ("" if already_running else 'wait "$QEMU_PID"\n')
    )
    subprocess.run(["bash"], input=script, env=env, text=True, check=True, capture_output=True, timeout=5)
    if already_running:
        assert arguments.read_bytes() == b"not launched"
    else:
        actual = arguments.read_bytes().split(b"\0")[:-1]
        expected = [str(vm)]
        if qmp:
            expected += ["-qmp", f"unix:{socket},server=on,wait=off"]
        assert actual == [os.fsencode(value) for value in expected]


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
    script.write_text("# LIMULATOR_CREATOR_USB_READY LIMULATOR_FIRST_BOOT_QMP\n")
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


@pytest.mark.parametrize("scope_owned", [False, True])
@pytest.mark.parametrize("failure", ["scope", "private USB", "both"])
def test_failed_creation_cleanup_blocks_recovery_even_when_disk_has_no_users(
    catalog, monkeypatch, scope_owned, failure
):
    candidate, _ = _incomplete_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    original = disk.read_bytes()
    script = catalog.backend / "setup/create-vm.sh"
    script.write_text("# LIMULATOR_CREATOR_USB_READY LIMULATOR_FIRST_BOOT_QMP\n")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_sudo_shim", lambda *_: None)
    monkeypatch.setattr(creator, "_disk_users", lambda *_: [])
    monkeypatch.setattr(creator, "_in_scope", lambda *_: scope_owned)
    monkeypatch.setattr(creator, "_run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    closed = []
    real_close = creator._USBHelpers.close
    private_identity = {"pid": 4322, "start_ticks": 1234, "boot_id": "owned-boot"}

    def wait(*, timeout):
        raise subprocess.TimeoutExpired("private usbmuxd", timeout)

    def start_helpers(self):
        if failure == "private USB":
            self.daemon_identity = private_identity
            self.daemon = SimpleNamespace(wait=wait)

    monkeypatch.setattr(creator._USBHelpers, "start", start_helpers)
    if failure == "private USB":
        monkeypatch.setattr(creator, "_identity_alive", lambda value: value == private_identity)
        monkeypatch.setattr(creator, "_signal_owned", lambda *_: True)
        monkeypatch.setattr(creator, "_vhci_ports", lambda: {})

    def close_helpers(_self):
        closed.append(True)
        if failure == "private USB":
            return real_close(_self)
        if failure == "both":
            raise RuntimeError("private USB helper remains active")

    monkeypatch.setattr(creator._USBHelpers, "close", close_helpers)
    monkeypatch.setattr(
        creator._USBHelpers, "poll", lambda self: (_ for _ in ()).throw(RuntimeError("creation interrupted"))
    )
    monkeypatch.setattr(
        creator.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=os.getpid(), poll=lambda: None),
    )

    def incomplete(*_args, **_kwargs):
        if failure in ("scope", "both"):
            raise RuntimeError("creation cleanup incomplete: PID 4321 /usr/bin/sshfs root@vphone:/mnt1")

    monkeypatch.setattr(creator, "_cleanup_creation_scope", incomplete)
    message = "usbmuxd survived TERM/KILL" if failure == "private USB" else "cleanup incomplete.*sshfs"
    with pytest.raises(RuntimeError, match=message):
        creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, {}, fresh_disk=True)

    receipt = creator._read_json(vm / ".limulator-create.json")
    record = receipt["fresh_disks"][-1]
    assert receipt["state"] == "error"
    if failure in ("scope", "both"):
        assert "PID 4321 /usr/bin/sshfs" in receipt["cleanup_error"]
    if failure == "private USB":
        assert "usbmuxd survived TERM/KILL" in receipt["cleanup_error"]
        assert "4322" in receipt["cleanup_error"]
    if failure == "both":
        assert "private USB helper remains active" in receipt["cleanup_error"]
    assert record["state"] == ("restore-started" if scope_owned else "prepared")
    assert disk.read_bytes() == bytes(len(original))
    assert creator.Path(record["disk"]["path"]).read_bytes() == original
    assert closed == [True]
    released = creator._lock_disk(disk)
    os.close(released)


@pytest.mark.parametrize("failure", ["scope", "private USB"])
def test_verified_guest_is_not_registered_until_all_creation_cleanup_succeeds(catalog, monkeypatch, failure):
    candidate, _ = _unregistered_booted_vm(catalog)
    vm = candidate.vm_path
    (catalog.backend / "setup/create-vm.sh").write_text(
        "# LIMULATOR_CREATOR_USB_READY LIMULATOR_FIRST_BOOT_QMP\n"
    )
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_sudo_shim", lambda *_: None)
    monkeypatch.setattr(creator, "_resume_completed_boot", lambda *_: None)
    monkeypatch.setattr(creator._USBHelpers, "start", lambda self: None)
    monkeypatch.setattr(
        creator.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=os.getpid(), poll=lambda: 0, returncode=0),
    )
    monkeypatch.setattr(
        creator, "GuestClient", lambda **kwargs: SimpleNamespace(rpc=lambda *_: {"locked": False})
    )
    monkeypatch.setattr(creator, "_verify_guest_migration", lambda *_: _migration_settings())
    monkeypatch.setattr(creator, "_verified_guest_info", lambda *_: {"ios_version": candidate.os_version})
    monkeypatch.setattr(creator, "_stop_creation_guest", lambda *_: None)
    monkeypatch.setattr(creator, "Runtime", lambda *_: pytest.fail("runtime must not start before cleanup"))
    closed = []

    def cleanup_scope(*_args, **_kwargs):
        if failure == "scope":
            raise RuntimeError("owned scope cleanup failed")

    def close_helpers(_self):
        closed.append(True)
        if failure == "private USB":
            raise RuntimeError("private USB helper remains active")

    monkeypatch.setattr(creator, "_cleanup_creation_scope", cleanup_scope)
    monkeypatch.setattr(creator._USBHelpers, "close", close_helpers)
    message = "owned scope cleanup failed" if failure == "scope" else "private USB helper remains active"
    with pytest.raises(RuntimeError, match=message):
        creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, {})

    assert Registry(catalog).list() == []
    receipt = creator._read_json(vm / ".limulator-create.json")
    assert receipt["state"] == "error"
    assert message in receipt["cleanup_error"]
    assert closed == [True]


@pytest.mark.parametrize("survivor", ["process", "mount", "none"])
def test_launch_refusal_does_not_clean_an_unproven_scope(catalog, monkeypatch, survivor):
    candidate, _ = _incomplete_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    original = disk.read_bytes()
    (catalog.backend / "setup/create-vm.sh").write_text(
        "# LIMULATOR_CREATOR_USB_READY LIMULATOR_FIRST_BOOT_QMP\n"
    )
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_sudo_shim", lambda *_: None)
    monkeypatch.setattr(creator._USBHelpers, "start", lambda self: None)
    monkeypatch.setattr(creator._USBHelpers, "close", lambda self: None)
    monkeypatch.setattr(
        creator.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=os.getpid(), poll=lambda: 1, returncode=1),
    )
    monkeypatch.setattr(creator, "_in_scope", lambda *_: pytest.fail("the refused launcher has exited"))
    monkeypatch.setattr(
        _creation_cleanup,
        "_scope_processes",
        lambda group: (
            [
                {
                    "pid": 4321,
                    "start_ticks": 1234,
                    "boot_id": creator._identity(os.getpid())["boot_id"],
                    "uid": 0,
                    "exe": "/usr/bin/sshfs",
                    "argv": [],
                    "cgroup": f"0::{group}",
                    "mount_namespace": os.readlink("/proc/self/ns/mnt"),
                }
            ]
            if survivor == "process"
            else []
        ),
    )
    mount = {"target": "/var/tmp/vphone-cfw.aabbccdd/system", "record": "surviving mount"}
    snapshots = iter([[], [mount] if survivor == "mount" else []])
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: next(snapshots))
    monkeypatch.setattr(
        _creation_cleanup, "_pidfd", lambda *_: pytest.fail("an unproven scope must not pin processes")
    )
    monkeypatch.setattr(
        _creation_cleanup, "_signal", lambda *_: pytest.fail("an unproven scope must not signal processes")
    )
    monkeypatch.setattr(
        _creation_cleanup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not unmount")
    )
    requests = []

    def inspect_only(_command, _env, *, input, timeout):
        request = json.loads(input)
        requests.append(request)
        _creation_cleanup.cleanup(**request)

    monkeypatch.setattr(creator, "_sudo", inspect_only)
    message = (
        "upstream creation exited with status 1"
        if survivor == "none"
        else "scope ownership was not established"
    )
    with pytest.raises(RuntimeError, match=message):
        creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, {})

    assert requests[0]["scope_owned"] is False
    receipt = creator._read_json(vm / ".limulator-create.json")
    assert "upstream creation exited with status 1" in receipt["error"]
    if survivor == "none":
        assert "cleanup_error" not in receipt
    else:
        assert "scope ownership was not established" in receipt["cleanup_error"]
    assert disk.read_bytes() == original


@pytest.mark.parametrize("fresh_disk", [False, True])
def test_retry_preserves_unresolved_cleanup_without_accepting_a_new_mount_baseline(
    catalog, monkeypatch, fresh_disk
):
    candidate, receipt = _incomplete_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    receipt.update(
        scope="limulator-create-1000-aabbccdd.scope", cleanup_error="surviving root SSHFS PID 4321"
    )
    state_path = vm / ".limulator-create.json"
    creator._atomic_json(state_path, receipt)
    before = state_path.read_bytes(), disk.read_bytes(), sorted(vm.iterdir())
    monkeypatch.setattr(
        _creation_cleanup, "mounts", lambda: pytest.fail("unresolved mounts must not become a new baseline")
    )
    monkeypatch.setattr(creator._USBHelpers, "start", lambda self: pytest.fail("must not start helpers"))
    with pytest.raises(RuntimeError, match="previous creation cleanup is unresolved.*surviving root SSHFS"):
        creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, {}, fresh_disk=fresh_disk)
    assert (state_path.read_bytes(), disk.read_bytes(), sorted(vm.iterdir())) == before


@pytest.fixture
def cleanup_scope(monkeypatch):
    unit = "limulator-create-1000-aabbccdd.scope"
    group = f"/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}"
    boot = creator.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    identity = {
        "pid": 4321,
        "start_ticks": 1234,
        "boot_id": boot,
        "uid": 0,
        "exe": "/usr/bin/sshfs",
        "argv": [],
        "cgroup": f"0::{group}\n",
        "mount_namespace": os.readlink("/proc/self/ns/mnt"),
    }
    alive = {identity["pid"]: identity}
    monkeypatch.setattr(_creation_cleanup, "_scope_processes", lambda *_: list(alive.values()))
    monkeypatch.setattr(_creation_cleanup, "_identity", lambda pid: alive.get(pid))
    monkeypatch.setattr(_creation_cleanup, "_pidfd", lambda pid: os.open("/dev/null", os.O_RDONLY))
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: [])
    return unit, boot, identity, alive


@pytest.mark.parametrize("changed", ["start_ticks", "cgroup"])
def test_scope_cleanup_refuses_reused_pid_or_foreign_scope(cleanup_scope, monkeypatch, changed):
    unit, boot, identity, _ = cleanup_scope
    if changed == "start_ticks":
        monkeypatch.setattr(_creation_cleanup, "_identity", lambda pid: identity | {"start_ticks": 9999})
    else:
        identity["cgroup"] = "0::/other.scope\n"
    monkeypatch.setattr(
        _creation_cleanup, "_signal", lambda *_: pytest.fail("identity mismatch must prevent signaling")
    )
    monkeypatch.setattr(
        _creation_cleanup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not unmount")
    )
    with pytest.raises(RuntimeError, match="identity changed|outside the owned scope"):
        _creation_cleanup.cleanup(unit, 1000, boot, [], scope_owned=True)


@pytest.mark.parametrize("source", ["root@vphone:/mnt1", "root@foreign:/mnt1"])
def test_scope_cleanup_requires_exact_private_sshfs_before_unmount_and_pidfd_signal(
    cleanup_scope, tmp_path, monkeypatch, source
):
    unit, boot, identity, alive = cleanup_scope
    directory = tmp_path / "vphone-cfw.aabbccdd"
    directory.mkdir(mode=0o700)
    target = str(directory / "system")
    identity["argv"] = ["/usr/bin/sshfs", "-f", "root@vphone:/mnt1", target]
    mount = {
        "id": "123",
        "target": target,
        "source": source,
        "type": "fuse.sshfs",
        "record": "exact mount",
    }
    attached = [mount]
    monkeypatch.setattr(_creation_cleanup, "_cfw_target", lambda value: value == target)
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: list(attached))
    fstat = os.fstat

    def root_directory_metadata(fd):
        info = fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            return info
        metadata = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        return SimpleNamespace(**(metadata | {"st_uid": 0}))

    monkeypatch.setattr(
        _creation_cleanup.os,
        "fstat",
        root_directory_metadata,
    )
    actions = []

    def unmount(command, **kwargs):
        assert command == ["/usr/bin/fusermount3", "-u", target]
        actions.append("unmount")
        attached.clear()
        return SimpleNamespace(returncode=0)

    def terminate(fd, value):
        assert value == creator.signal.SIGTERM
        actions.append("pidfd TERM")
        alive.clear()

    monkeypatch.setattr(_creation_cleanup.subprocess, "run", unmount)
    monkeypatch.setattr(_creation_cleanup, "_signal", terminate)
    if source == "root@vphone:/mnt1":
        _creation_cleanup.cleanup(unit, 1000, boot, [], scope_owned=True)
        assert actions == ["unmount", "pidfd TERM"]
    else:
        with pytest.raises(RuntimeError, match="SSHFS mount identity differs"):
            _creation_cleanup.cleanup(unit, 1000, boot, [], scope_owned=True)
        assert actions == []


def test_scope_cleanup_refuses_mount_without_an_owned_process(cleanup_scope, monkeypatch):
    unit, boot, _, alive = cleanup_scope
    alive.clear()
    mount = {"target": "/var/tmp/vphone-cfw.aabbccdd/system", "record": "unattributed mount"}
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: [mount])
    monkeypatch.setattr(
        _creation_cleanup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not unmount")
    )
    with pytest.raises(RuntimeError, match="mount ownership could not be proven"):
        _creation_cleanup.cleanup(unit, 1000, boot, [], scope_owned=True)


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


@pytest.mark.parametrize("journal_kind", ["file", "dangling-symlink"])
def test_fresh_disk_preserves_pending_cache_publication(catalog, journal_kind):
    candidate, receipt = _incomplete_vm(catalog)
    pending = candidate.vm_path / ".vphone-dsc-pending.json"
    if journal_kind == "file":
        pending.write_text('{"stage":"retained"}')
    else:
        pending.symlink_to("missing-recovery-stage")
    disk = candidate.vm_path / "Disk.img"
    original, inode = disk.read_bytes(), disk.stat().st_ino
    before = pending.lstat()
    with pytest.raises(ValueError, match="unresolved cache publication"):
        creator._fresh_disk_allowed(catalog, candidate, receipt)
    assert disk.read_bytes() == original
    assert disk.stat().st_ino == inode
    after = pending.lstat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
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
    monkeypatch.setattr(creator, "_vhci_ports", lambda: {})
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
    ports = {}
    connection = ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678)
    monkeypatch.setattr(creator, "_vhci_ports", lambda: dict(ports))
    monkeypatch.setattr(creator, "_usbip_connections", lambda: {connection} if ports else set())
    monkeypatch.setattr(
        creator, "_run", lambda *args, **kwargs: SimpleNamespace(stdout="  1-1: Apple DFU (05ac:1227)\n")
    )
    calls = []

    def attach(command, *_args, **_kwargs):
        calls.append(command)
        ports[0] = "0-0"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(creator, "_sudo", attach)
    helper.poll()
    assert calls == [["usbip", "attach", "-r", "127.0.0.1", "-b", "1-1"]]
    assert helper.owned_ports == {0}
    assert helper.import_connections == {0: connection}


@pytest.mark.parametrize("when", ["before", "during"])
def test_attacher_refuses_listener_replacement_before_recording_import_identity(
    catalog, tmp_path, monkeypatch, when
):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.daemon_identity = creator._identity(os.getpid())
    ports = {}
    connection = ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678)
    listeners = iter([[1234], [9999]] if when == "before" else [[1234], [1234], [9999]])
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: next(listeners))
    monkeypatch.setattr(creator, "_in_scope", lambda pid, unit: pid == 1234)
    monkeypatch.setattr(creator, "_vhci_ports", lambda: dict(ports))
    monkeypatch.setattr(creator, "_usbip_connections", lambda: {connection} if ports else set())
    monkeypatch.setattr(
        creator, "_run", lambda *args, **kwargs: SimpleNamespace(stdout="  1-1: Apple DFU (05ac:1227)\n")
    )
    commands = []

    def attach(command, *_args, **_kwargs):
        commands.append(command)
        ports[0] = "0-0"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(creator, "_sudo", attach)
    with pytest.raises(RuntimeError, match=f"listener ownership changed {when} owned attach"):
        helper.poll()
    assert helper.import_connections == {}
    assert len(commands) == (0 if when == "before" else 1)
    if when == "during":
        ticks = iter(range(20))
        monkeypatch.setattr(creator.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(creator.time, "sleep", lambda *_: None)
        monkeypatch.setattr(
            creator, "_sudo", lambda *_args, **_kwargs: pytest.fail("unproven import must not be detached")
        )
        with pytest.raises(RuntimeError, match="no recorded import connection identity"):
            helper.close()
        assert ports == {0: "0-0"}


def test_ambiguous_attach_cannot_report_cleanup_complete_or_detach_unproven_imports(
    catalog, tmp_path, monkeypatch
):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.daemon_identity = creator._identity(os.getpid())
    ports = {}
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: [1234])
    monkeypatch.setattr(creator, "_in_scope", lambda *_: True)
    monkeypatch.setattr(creator, "_vhci_ports", lambda: dict(ports))
    monkeypatch.setattr(creator, "_usbip_connections", lambda: set())
    monkeypatch.setattr(
        creator, "_run", lambda *args, **kwargs: SimpleNamespace(stdout="  1-1: Apple DFU (05ac:1227)\n")
    )

    def attach(*_args, **_kwargs):
        ports.update({0: "0-0", 7: "foreign"})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(creator, "_sudo", attach)
    with pytest.raises(RuntimeError, match="could not identify.*newly imported virtual USB port"):
        helper.poll()
    monkeypatch.setattr(
        creator, "_sudo", lambda *_args, **_kwargs: pytest.fail("unproven imports must not be detached")
    )
    with pytest.raises(RuntimeError, match="unresolved import identity: active ports"):
        helper.close()
    assert ports == {0: "0-0", 7: "foreign"}


@pytest.mark.parametrize("phase", ["poll", "close"])
@pytest.mark.parametrize("change", ["socket", "endpoint", "missing-receipt", "another-port"])
def test_same_index_import_replacement_is_never_detached(catalog, tmp_path, monkeypatch, phase, change):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.daemon_identity = creator._identity(os.getpid())
    helper.owned_ports = {0}
    connection = ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678)
    if change != "missing-receipt":
        helper.import_connections = {0: connection}
    current = connection
    if change == "socket":
        current = (*connection[:3], 9999)
    if change == "endpoint":
        current = (connection[0], "0100007F:4321", *connection[2:])
    ports = {0: "0-0"}
    if change == "another-port":
        ports[7] = "foreign"
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: [])
    monkeypatch.setattr(creator, "_vhci_ports", lambda: dict(ports))
    monkeypatch.setattr(creator, "_usbip_connections", lambda: {current})
    monkeypatch.setattr(
        creator, "_sudo", lambda *_args, **_kwargs: pytest.fail("unproven import must not be detached")
    )
    ticks = iter(range(30))
    monkeypatch.setattr(creator.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(creator.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="identity|ambiguous|unowned USB/IP imports"):
        getattr(helper, phase)()
    assert ports[0] == "0-0"


def test_owned_import_identity_survives_usb_address_changes_and_allows_own_reattach(
    catalog, tmp_path, monkeypatch
):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.daemon_identity = creator._identity(os.getpid())
    ports, connections = {}, set()
    identities = iter(
        [
            ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678),
            ("net:[123]", "0100007F:4321", "0100007F:0CA8", 9999),
        ]
    )
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: [1234])
    monkeypatch.setattr(creator, "_in_scope", lambda *_: True)
    monkeypatch.setattr(creator, "_vhci_ports", lambda: dict(ports))
    monkeypatch.setattr(creator, "_usbip_connections", lambda: set(connections))
    monkeypatch.setattr(
        creator, "_run", lambda *args, **kwargs: SimpleNamespace(stdout="  1-1: Apple DFU (05ac:1227)\n")
    )

    def attach(*_args, **_kwargs):
        ports[0] = "0-0"
        connections.add(next(identities))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(creator, "_sudo", attach)
    helper.poll()
    first = helper.import_connections[0]
    ports[0] = "nonexistent-test-usb-address"
    helper.poll()
    assert helper.import_connections[0] == first
    ports.clear()
    connections.clear()
    helper.poll()
    assert helper.owned_ports == {0}
    assert helper.import_connections[0] != first
    assert set(helper.import_connections.values()) == connections


def test_private_helper_close_waits_for_normal_disconnection_without_detaching_a_replacement(
    catalog, tmp_path, monkeypatch
):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.owned_ports = {0}
    helper.import_connections = {0: ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678)}
    snapshots = iter([{0: "0-0"}, {0: "0-0"}, {}])
    monkeypatch.setattr(creator, "_vhci_ports", lambda: next(snapshots, {}))
    monkeypatch.setattr(creator, "_usbip_connections", lambda: set())
    waits = []
    monkeypatch.setattr(creator.time, "sleep", lambda duration: waits.append(duration))
    monkeypatch.setattr(
        creator, "_sudo", lambda *_args, **_kwargs: pytest.fail("disconnected imports must not be detached")
    )
    helper.close()
    assert waits == [0.05]
    assert helper.owned_ports == set()
    assert helper.import_connections == {}


@pytest.mark.parametrize("inode", [5678, 0])
def test_usbip_connection_identity_uses_established_socket_inode_and_endpoints(tmp_path, monkeypatch, inode):
    tcp = tmp_path / "tcp"
    tcp.write_text(
        "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
        f"0: 0100007F:1234 0100007F:0CA8 01 0:0 0:0 0 0 0 {inode}\n"
        "1: 0100007F:0CA8 0100007F:1234 01 0:0 0:0 0 0 0 9999\n"
        "2: 0100007F:4321 0100007F:0CA8 06 0:0 0:0 0 0 0 0\n"
    )
    read_text = creator.Path.read_text
    monkeypatch.setattr(
        creator.Path,
        "read_text",
        lambda path, *args, **kwargs: read_text(
            tcp if str(path) == "/proc/net/tcp" else path, *args, **kwargs
        ),
    )
    if inode:
        assert creator._usbip_connections() == {
            (os.readlink("/proc/self/ns/net"), "0100007F:1234", "0100007F:0CA8", inode)
        }
    else:
        with pytest.raises(RuntimeError, match="no kernel socket inode"):
            creator._usbip_connections()


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


def test_private_helper_close_escalates_owned_daemon_and_preserves_foreign_imports(
    catalog, tmp_path, monkeypatch
):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    identity = {"pid": 4321, "start_ticks": 1234, "boot_id": "owned-boot"}
    helper.daemon_identity = identity
    helper.owned_ports = {2}
    connection = ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678)
    helper.import_connections = {2: connection}
    monkeypatch.setattr(creator, "_usbip_connections", lambda: {connection})
    helper.rule_created = True
    alive = True
    signals, waits, commands = [], [], []
    ports = {2: "owned"}
    monkeypatch.setattr(creator, "_identity_alive", lambda value: value == identity and alive)
    monkeypatch.setattr(creator, "_vhci_ports", lambda: dict(ports))

    def signal_owned(value, signal):
        nonlocal alive
        assert value == identity
        signals.append(signal)
        if signal == creator.signal.SIGKILL:
            alive = False

    def wait(*, timeout):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("private usbmuxd", timeout)

    def privileged(command, *_args, **_kwargs):
        commands.append(command)
        if command[:2] == ["usbip", "detach"]:
            assert command == ["usbip", "detach", "-p", "2"]
            ports.pop(2)
            ports[7] = "foreign"
        return SimpleNamespace(returncode=0)

    helper.daemon = SimpleNamespace(wait=wait)
    monkeypatch.setattr(creator, "_signal_owned", signal_owned)
    monkeypatch.setattr(creator, "_sudo", privileged)
    helper.close()
    assert signals == [creator.signal.SIGTERM, creator.signal.SIGKILL]
    assert waits == [10, 5]
    assert ports == {7: "foreign"}
    assert helper.owned_ports == set()
    assert helper.rule_created is False
    assert commands == [
        ["usbip", "detach", "-p", "2"],
        ["rm", "--", helper.rule],
        ["udevadm", "control", "--reload"],
    ]


def test_private_helper_close_reports_survivors_and_attempts_remaining_owned_cleanup(
    catalog, tmp_path, monkeypatch
):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.daemon_identity = {"pid": 4321, "start_ticks": 1234, "boot_id": "owned-boot"}
    helper.owned_ports = {2}
    connection = ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678)
    helper.import_connections = {2: connection}
    monkeypatch.setattr(creator, "_usbip_connections", lambda: {connection})
    helper.rule_created = True
    ports = {2: "owned"}
    signals, commands = [], []
    monkeypatch.setattr(creator, "_identity_alive", lambda *_: True)
    monkeypatch.setattr(creator, "_signal_owned", lambda identity, value: signals.append(value))
    monkeypatch.setattr(creator, "_vhci_ports", lambda: dict(ports))
    ticks = iter(range(20))
    monkeypatch.setattr(creator.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(creator.time, "sleep", lambda *_: None)

    def wait(*, timeout):
        raise subprocess.TimeoutExpired("private usbmuxd", timeout)

    def privileged(command, *_args, **_kwargs):
        commands.append(command)
        if command == ["usbip", "detach", "-p", "2"]:
            return SimpleNamespace(returncode=1, stderr="detach denied")
        if command[0] == "rm":
            raise RuntimeError("rule removal denied")
        return SimpleNamespace(returncode=0)

    helper.daemon = SimpleNamespace(wait=wait)
    monkeypatch.setattr(creator, "_sudo", privileged)
    with pytest.raises(RuntimeError, match="private USB cleanup incomplete") as failure:
        helper.close()
    assert "usbmuxd survived TERM/KILL" in str(failure.value)
    assert "4321" in str(failure.value)
    assert "port 2 detach exited 1: detach denied" in str(failure.value)
    assert "imports remain attached: [2]" in str(failure.value)
    assert "rule removal denied" in str(failure.value)
    assert signals == [creator.signal.SIGTERM, creator.signal.SIGKILL]
    assert helper.owned_ports == {2}
    assert ports == {2: "owned"}
    assert commands == [
        ["usbip", "detach", "-p", "2"],
        ["rm", "--", helper.rule],
        ["udevadm", "control", "--reload"],
    ]


@pytest.mark.parametrize(
    "status,remains,message", [(0, True, "remain attached"), (1, False, "detach exited 1")]
)
def test_private_helper_close_requires_detach_success_and_verified_port_absence(
    catalog, tmp_path, monkeypatch, status, remains, message
):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.owned_ports = {2}
    connection = ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678)
    helper.import_connections = {2: connection}
    monkeypatch.setattr(creator, "_usbip_connections", lambda: {connection})
    ports = {2: "owned"}
    monkeypatch.setattr(creator, "_vhci_ports", lambda: dict(ports))
    ticks = iter(range(20))
    monkeypatch.setattr(creator.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(creator.time, "sleep", lambda *_: None)
    commands = []

    def detach(command, *_args, **_kwargs):
        commands.append(command)
        if not remains:
            ports.pop(2)
        ports[7] = "foreign"
        return SimpleNamespace(returncode=status, stderr="detach denied")

    monkeypatch.setattr(creator, "_sudo", detach)
    with pytest.raises(RuntimeError, match=message):
        helper.close()
    assert commands == [["usbip", "detach", "-p", "2"]]
    assert 7 in ports
    assert helper.owned_ports == ({2} if remains else set())


def test_private_helper_close_preserves_a_daemon_without_validated_identity(catalog, tmp_path, monkeypatch):
    helper = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    helper.daemon = SimpleNamespace(pid=4321, poll=lambda: None)
    monkeypatch.setattr(creator, "_vhci_ports", lambda: {})
    monkeypatch.setattr(
        creator, "_signal_owned", lambda *_: pytest.fail("unproven daemon must not be signaled")
    )
    with pytest.raises(RuntimeError, match="no validated identity: PID 4321"):
        helper.close()


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


@pytest.mark.parametrize(
    "reported,accepted",
    [
        ("27.1", True),
        ("27.1.0", True),
        ("27.1.0.0", True),
        ("27.1.1", False),
        ("27.2", False),
        ("26.4", False),
        ("27.01", False),
        ("27..1", False),
        ("27.1beta", False),
        ("27.1 ", False),
        ("", False),
        (None, False),
        (27.1, False),
        (True, False),
    ],
)
def test_ready_guest_must_report_the_requested_ios_version(reported, accepted):
    class Guest:
        def rpc(self, method, params):
            assert (method, params) == ("device.info", {})
            return {"ios_version": reported, "model": "iPhone Duo"}

    guest = Guest()
    if accepted:
        assert creator._verified_guest_info(guest, "27.1")["ios_version"] == reported
    else:
        with pytest.raises(RuntimeError, match="requested iOS 27.1"):
            creator._verified_guest_info(guest, "27.1")


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
    result = subprocess.run(
        [
            str(tmp_path / "bin/sudo"),
            "python3",
            "-c",
            "import sys,socket; print(sys.executable,socket.AF_VSOCK)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/usr/bin/python3 40"


def test_native_helpers_resolve_distro_python_and_preserve_arguments(catalog, tmp_path, monkeypatch):
    incompatible = tmp_path / "managed"
    incompatible.mkdir()
    (incompatible / "python3").write_text("#!/bin/sh\nexit 71\n")
    (incompatible / "python3").chmod(0o700)
    monkeypatch.setenv("PATH", str(incompatible) + os.pathsep + os.environ["PATH"])
    env = creator._native_env(catalog)
    original_interpreter = sys.executable
    creator._sudo_shim(tmp_path / "native-run", env)
    result = subprocess.run(
        [
            "/usr/bin/env",
            "python3",
            "-c",
            "import sys,socket,json; print(json.dumps([sys.executable,socket.AF_VSOCK,sys.argv[1:]]))",
            "literal with spaces",
            "$unchanged",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["/usr/bin/python3", 40, ["literal with spaces", "$unchanged"]]
    assert sys.executable == original_interpreter
    assert stat.S_IMODE((tmp_path / "native-run/bin/python3").stat().st_mode) == 0o700


def test_native_python_selection_refuses_existing_file_or_missing_vsock(catalog, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    python = bindir / "python3"
    python.write_bytes(b"existing interpreter wrapper")
    with pytest.raises(FileExistsError):
        creator._native_python_shim(bindir)
    assert python.read_bytes() == b"existing interpreter wrapper"
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(creator, "_run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="AF_VSOCK support"):
        creator._native_python_shim(empty)
    assert list(empty.iterdir()) == []
