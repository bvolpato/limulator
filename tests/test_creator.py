import fcntl
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
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from limulator import _creation_cleanup, creator
from limulator.lifecycle import Device, Paths, Registry
from limulator.profiles import FirmwareDescriptor, get_profile, list_profiles

_CREATION_INVOCATION_ID = "a" * 32


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


@pytest.mark.parametrize("success", [False, 0])
def test_creation_rejects_failed_migration_even_when_setup_build_marker_matches(tmp_path, success):
    expected = {"data_migration_done": True, "pending": False}
    result = {"buildVersion": "24A94425", "success": success}
    state_path = tmp_path / ".limulator-create.json"
    state = {"stage": "verification"}

    with pytest.raises(RuntimeError, match="success=false"):
        creator._verify_guest_migration(
            _SettingsGuest(_migration_settings(success=success)),
            expected,
            state_path,
            state,
            "24A94425",
        )

    receipt = creator._read_json(state_path)
    assert receipt["migration_result"] == result
    assert receipt["setup"] == expected
    assert receipt["migration_expected_build"] == "24A94425"


@pytest.mark.parametrize("success", [True, 1])
def test_creation_accepts_successful_migration_for_expected_build(tmp_path, success):
    guest = _SettingsGuest(_migration_settings(success=success))
    result = creator._verify_guest_migration(
        guest,
        {"data_migration_done": True, "pending": False},
        tmp_path / ".limulator-create.json",
        {},
        "24A94425",
    )

    assert result == {"buildVersion": "24A94425", "success": success}
    assert guest.calls == [("settings.get", {"domain": "com.apple.migration"})]


@pytest.mark.parametrize("success", [None, "true", "1", 1.0, 0.0, 2])
def test_creation_rejects_unrecognized_migration_success_values(success):
    with pytest.raises(RuntimeError, match="success value"):
        creator._verify_migration_result({"buildVersion": "24A94425", "success": success}, "24A94425")


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


def test_explicit_graphics_mode_is_passed_to_new_creation(catalog, monkeypatch):
    seen = []

    def create(_paths, candidate, _profile, _pairing, **options):
        seen.append((candidate, options))
        return {"state": "created"}

    monkeypatch.setattr(creator, "_create_locked", create)
    result = creator.create_device(
        catalog, "phone", "iPhone17,3", "27.0.1", 41, 3, graphics_mode="diagnostic"
    )

    assert result["state"] == "created"
    assert seen[0][1]["graphics_mode"] == "diagnostic"


def test_new_creation_defaults_to_compatible_graphics_mode(catalog, monkeypatch):
    monkeypatch.delenv("LIMULATOR_GRAPHICS_MODE", raising=False)
    seen = []

    def create(_paths, _candidate, _profile, _pairing, *, graphics_mode=creator.DEFAULT_GRAPHICS_MODE):
        seen.append(graphics_mode)
        return {"state": "created"}

    monkeypatch.setattr(creator, "_create_locked", create)
    result = creator.create_device(catalog, "phone", "iPhone17,3", "27.0.1", 41, 3)

    assert result["state"] == "created"
    assert seen == ["compatible"]


def test_create_refuses_to_change_registered_graphics_mode(catalog):
    device = _registered_device(catalog, "phone", "iphone-duo", 41)
    runtime_path = catalog.runs / device.name / "runtime.json"
    runtime_path.parent.mkdir(parents=True)
    creator._atomic_json(
        runtime_path,
        {"name": device.name, "graphics_policy": {"mode": "diagnostic"}},
    )

    with pytest.raises(RuntimeError, match="graphics mode diagnostic.*Stop it.*start it"):
        creator.create_device(catalog, "phone", "iPhone19,4", "27.1", None, None, graphics_mode="strict")


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


@pytest.mark.parametrize("fresh_disk", [False, True])
@pytest.mark.parametrize(
    ("requested_mode", "expected_mode"), [("diagnostic", "diagnostic"), (None, "strict")]
)
def test_incomplete_creation_can_select_a_new_graphics_mode(
    catalog, monkeypatch, fresh_disk, requested_mode, expected_mode
):
    monkeypatch.delenv("LIMULATOR_GRAPHICS_MODE", raising=False)
    candidate, receipt = _incomplete_vm(catalog)
    receipt["graphics_policy"] = {"mode": "strict"}
    creator._atomic_json(candidate.vm_path / ".limulator-create.json", receipt)
    seen = []

    def create(
        _paths,
        _candidate,
        _profile,
        _pairing,
        *,
        fresh_disk=False,
        graphics_mode=creator.DEFAULT_GRAPHICS_MODE,
    ):
        seen.append((fresh_disk, graphics_mode))
        return {"state": "created"}

    monkeypatch.setattr(creator, "_create_locked", create)
    options = {"fresh_disk": fresh_disk}
    if requested_mode is not None:
        options["graphics_mode"] = requested_mode
    result = creator.create_device(
        catalog, candidate.name, candidate.model, candidate.os_version, None, None, **options
    )

    assert result["state"] == "created"
    assert seen == [(fresh_disk, expected_mode)]
    assert creator._read_json(candidate.vm_path / ".limulator-create.json")["graphics_policy"]["mode"] == (
        "strict"
    )


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


@pytest.mark.parametrize("native_env", [pytest.param(False, id="direct"), pytest.param(True, id="native")])
@pytest.mark.parametrize("cache", ["default", "empty", "absolute", "relative", "home"])
def test_pipeline_uses_one_firmware_cache_for_verification_and_native_env(
    catalog, tmp_path, monkeypatch, cache, native_env
):
    monkeypatch.chdir(tmp_path)
    if cache == "absolute":
        expected = tmp_path / "private HDD" / "firmware catalog"
        configured = str(expected)
    elif cache == "relative":
        configured = "relative cache with spaces"
        expected = tmp_path / configured
    elif cache == "home":
        configured = "~/firmware catalog with spaces"
        expected = creator.Path.home() / "firmware catalog with spaces"
    else:
        configured = "" if cache == "empty" else None
        expected = creator.Path.home() / ".cache/vphone"
    expected = expected.resolve()
    monkeypatch.delenv("VSETUP_CACHE", raising=False)
    if configured is not None:
        monkeypatch.setenv("VSETUP_CACHE", configured)
    environment = (
        creator._native_env(catalog)
        if native_env
        else ({"VSETUP_CACHE": configured} if configured is not None else {})
    )
    if native_env:
        assert environment.get("VSETUP_CACHE") == str(expected)
    candidate, _ = _incomplete_vm(catalog)
    profile = get_profile("iphone-duo")
    (catalog.backend / "setup/create-vm.sh").write_text("# VPHONE_CREATOR_USB_READY VPHONE_FIRST_BOOT_QMP\n")
    verified = []

    def verify(vm, files):
        assert vm == candidate.vm_path
        verified.extend(path for path, _descriptor in files)
        return []

    def stop():
        raise RuntimeError("stop before native creation")

    def no_native_work(*_args, **_kwargs):
        pytest.fail("native creation must not run")

    monkeypatch.setattr(creator, "_verified_firmware_stage", verify)
    monkeypatch.setattr(creator, "_unit_states", stop)
    monkeypatch.setattr(creator, "_run", no_native_work)
    monkeypatch.setattr(creator._USBHelpers, "start", no_native_work)
    monkeypatch.setattr(creator._USBHelpers, "close", lambda self: None)
    monkeypatch.setattr(creator.subprocess, "Popen", no_native_work)
    with pytest.raises(RuntimeError, match="stop before native creation"):
        creator._pipeline(catalog, candidate, profile, {}, environment)

    ios, cloud = expected / profile.main_firmware.filename, expected / profile.cloud_os_firmware.filename
    assert verified == [ios, cloud]
    assert environment["VSETUP_CACHE"] == str(expected)
    assert environment["IPSW_PATH"] == environment["IPSW_URL"] == str(ios)
    assert environment["CLOUDOS_IPSW_URL"] == str(cloud)


@pytest.mark.parametrize(
    ("native_trace", "legacy_trace", "trace_enabled"),
    [(None, None, False), (None, "1", True), ("0", "1", False), ("1", "0", True), ("0", None, False)],
)
def test_verification_resume_preserves_state_and_only_reselects_first_boot(
    catalog, monkeypatch, native_trace, legacy_trace, trace_enabled
):
    candidate, _ = _unregistered_booted_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    original, inode = disk.read_bytes(), disk.stat().st_ino
    markers = (vm / ".create-state").read_bytes()
    script = catalog.backend / "setup/create-vm.sh"
    script.write_text("# VPHONE_CREATOR_USB_READY VPHONE_FIRST_BOOT_QMP\n")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    inherited_trace = catalog.data / "inherited-generic-trace.jsonl"
    inherited_legacy_trace = catalog.data / "inherited-legacy-trace.jsonl"
    environment = {
        "LIMULATOR_FIRST_BOOT_QMP": "/tmp/previous-shared-qmp.sock",
        "VPHONE_FIRST_BOOT_QMP": "/tmp/inherited-generic-qmp.sock",
        "VPHONE_USB_TRACE_FILE": str(inherited_trace),
        "LIMULATOR_USB_TRACE_FILE": str(inherited_legacy_trace),
    }
    if native_trace is not None:
        environment["VPHONE_USB_TRACE"] = native_trace
    if legacy_trace is not None:
        environment["LIMULATOR_USB_TRACE"] = legacy_trace

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
    assert "LIMULATOR_FIRST_BOOT_QMP" not in environment
    assert environment["VPHONE_FIRST_BOOT_QMP"] == str(qmp_path)
    trace_path = qmp_path.parent / "usb-transport-trace.jsonl"
    assert "LIMULATOR_USB_TRACE" not in environment
    assert environment["VPHONE_USB_TRACE"] == ("1" if trace_enabled else "0")
    assert "LIMULATOR_USB_TRACE_FILE" not in environment
    if trace_enabled:
        assert environment["VPHONE_USB_TRACE_FILE"] == str(trace_path)
        assert stat.S_IMODE(trace_path.stat().st_mode) == 0o600
    else:
        assert "VPHONE_USB_TRACE_FILE" not in environment
        assert not trace_path.exists()
    assert not inherited_trace.exists()
    assert not inherited_legacy_trace.exists()
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
def patched_first_boot():
    backend = os.environ.get("LIMULATOR_BACKEND")
    source = (
        creator.Path(backend) / "setup/create-vm.sh"
        if backend
        else creator.Path(__file__).parent / "fixtures/first-boot-qmp.sh"
    )
    result = re.search(r"^boot_vm\(\) \{.*?^\}", source.read_text(), re.MULTILINE | re.DOTALL)
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
    env.pop("VPHONE_FIRST_BOOT_QMP", None)
    if qmp:
        env["VPHONE_FIRST_BOOT_QMP"] = str(socket)
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
    script.write_text("# VPHONE_CREATOR_USB_READY VPHONE_FIRST_BOOT_QMP\n")
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
    script.write_text("# VPHONE_CREATOR_USB_READY VPHONE_FIRST_BOOT_QMP\n")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_sudo_shim", lambda *_: None)
    monkeypatch.setattr(creator, "_disk_users", lambda *_: [])
    monkeypatch.setattr(
        creator,
        "_capture_creation_scope_invocation",
        lambda *_: _CREATION_INVOCATION_ID if scope_owned else None,
    )
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


def test_creation_scope_invocation_capture_requires_exact_active_membership(monkeypatch):
    unit = "limulator-create-1000-aabbccdd.scope"
    group = creator._creation_scope_group(os.getuid(), unit)
    expected_cgroup = f"0::{group}"
    membership = expected_cgroup
    monkeypatch.setattr(creator, "_creation_scope_cgroup", lambda _pid: membership)
    invocation_id = _CREATION_INVOCATION_ID
    result = SimpleNamespace(
        stdout=f"LoadState=loaded\nActiveState=active\nInvocationID={invocation_id}\nControlGroup={group}\n"
    )
    calls = []

    def show(command, **kwargs):
        calls.append((command, kwargs))
        return result

    monkeypatch.setattr(creator, "_run", show)
    captured = creator._capture_creation_scope_invocation(
        4321, unit, {"DBUS_SESSION_BUS_ADDRESS": "user-bus"}
    )
    assert captured == invocation_id
    assert calls == [
        (
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,InvocationID,ControlGroup",
            ],
            {"env": {"DBUS_SESSION_BUS_ADDRESS": "user-bus"}, "timeout": 5},
        )
    ]
    membership = "0::/user.slice/other.scope"
    assert creator._capture_creation_scope_invocation(4321, unit, {}) is None
    assert len(calls) == 1


def test_creation_scope_invocation_capture_refuses_unexpected_control_group(monkeypatch):
    unit = "limulator-create-1000-aabbccdd.scope"
    group = creator._creation_scope_group(os.getuid(), unit)
    expected_cgroup = f"0::{group}"
    monkeypatch.setattr(creator, "_creation_scope_cgroup", lambda _pid: expected_cgroup)
    monkeypatch.setattr(
        creator,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=active\n"
                f"InvocationID={_CREATION_INVOCATION_ID}\n"
                "ControlGroup=/user.slice/user-1000.slice/other.scope\n"
            )
        ),
    )
    with pytest.raises(RuntimeError, match="identity could not be established"):
        creator._capture_creation_scope_invocation(4321, unit, {})


@pytest.mark.parametrize("failure", ["scope", "private USB", "device info", None])
def test_verified_guest_is_not_registered_until_all_creation_cleanup_succeeds(catalog, monkeypatch, failure):
    candidate, _ = _unregistered_booted_vm(catalog)
    vm = candidate.vm_path
    (catalog.backend / "setup/create-vm.sh").write_text("# VPHONE_CREATOR_USB_READY VPHONE_FIRST_BOOT_QMP\n")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_sudo_shim", lambda *_: None)
    monkeypatch.setattr(creator, "_resume_completed_boot", lambda *_: None)
    monkeypatch.setattr(creator._USBHelpers, "start", lambda self: None)

    launch_environments = []

    def launch(*_args, **kwargs):
        launch_environments.append(kwargs["env"])
        return SimpleNamespace(pid=os.getpid(), poll=lambda: 0, returncode=0)

    monkeypatch.setattr(
        creator.subprocess,
        "Popen",
        launch,
    )
    monkeypatch.setenv("REIMS_VGPU_STRICT", "0")
    monkeypatch.setenv("REIMS_VGPU_CENSUS_DIR", "/inherited/census")
    monkeypatch.setenv("REIMS_VGPU_EXACT", "batch_order")
    monkeypatch.setenv("LIMULATOR_GRAPHICS_MODE", "diagnostic")
    guest_clients = []

    class Guest:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.calls = []
            guest_clients.append(self)

        def rpc(self, method, params):
            self.calls.append((method, params))
            return {"locked": False}

    monkeypatch.setattr(creator, "GuestClient", Guest)
    monkeypatch.setattr(creator, "_verify_guest_migration", lambda *_: _migration_settings())
    monkeypatch.setattr(creator, "_verified_guest_info", lambda *_: {"ios_version": candidate.os_version})
    if failure == "device info":
        def failed_info(*_args):
            raise creator.ProtocolError("timed out while reading HTTP response headers")

        monkeypatch.setattr(creator, "_verified_guest_info", failed_info)
    monkeypatch.setattr(creator, "_stop_creation_guest", lambda *_: None)
    runtime_guest_calls = []
    runtime_start_modes = []

    class Runtime:
        def __init__(self, *_):
            pass

        def start(self, *, wait_timeout, graphics_mode):
            assert wait_timeout == 600
            runtime_start_modes.append(graphics_mode)
            assert graphics_mode == "strict"
            return {"state": "running"}

        def guest(self, *, timeout):
            return SimpleNamespace(
                rpc=lambda method, params: (
                    runtime_guest_calls.append((timeout, method, params)) or {"locked": False}
                )
            )

        def qmp(self):
            return nullcontext(
                SimpleNamespace(screenshot=lambda path: creator.Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"))
            )

        def status(self):
            return {"state": "running"}

    if failure is None:
        monkeypatch.setattr(creator, "Runtime", Runtime)
    else:
        monkeypatch.setattr(
            creator, "Runtime", lambda *_: pytest.fail("runtime must not start before cleanup")
        )
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
    if failure is None:
        result = creator._pipeline(
            catalog,
            candidate,
            get_profile("iphone-duo"),
            {},
            {
                "REIMS_VGPU_STRICT": "0",
                "REIMS_VGPU_CENSUS_DIR": "/inherited/census",
                "REIMS_VGPU_EXACT": "batch_order",
            },
            graphics_mode="strict",
        )
        assert result["state"] == "running"
        assert runtime_start_modes == ["strict"]
        assert launch_environments[0]["REIMS_VGPU_STRICT"] == "1"
        assert launch_environments[0]["REIMS_VGPU_CENSUS_DIR"] == ""
        assert launch_environments[0]["REIMS_VGPU_EXACT"] == "batch_order"
        assert Registry(catalog).list() == [candidate]
        receipt = creator._read_json(vm / ".limulator-create.json")
        assert receipt["state"] == "ready"
        assert receipt["graphics_policy"] == {
            "mode": "strict",
            "reims_vgpu_exact": {"known": True, "set": True, "value": "batch_order"},
        }
        assert [client.options.get("timeout") for client in guest_clients] == [30, 120]
        assert guest_clients[0].calls == [("setup.status", {})]
        assert guest_clients[1].calls == [("screen.unlock", {"timeout": 60})]
        assert runtime_guest_calls == [
            (120, "screen.unlock", {"timeout": 60}),
            (30, "input.button", {"name": "home"}),
        ]
        return

    message = {
        "scope": "owned scope cleanup failed",
        "private USB": "private USB helper remains active",
        "device info": "timed out while reading HTTP response headers",
    }[failure]
    with pytest.raises(RuntimeError, match=message):
        creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, {})

    assert Registry(catalog).list() == []
    receipt = creator._read_json(vm / ".limulator-create.json")
    assert receipt["state"] == "error"
    if failure == "device info":
        assert receipt["stage"] == "verification-device-info"
        assert receipt["unlock"] == {"locked": False}
        assert receipt["migration_result"] == _migration_settings()
        assert message in receipt["error"]
        assert "cleanup_error" not in receipt
    else:
        assert message in receipt["cleanup_error"]
    assert closed == [True]


@pytest.mark.parametrize("survivor", ["process", "mount", "none"])
def test_launch_refusal_does_not_clean_an_unproven_scope(catalog, monkeypatch, survivor):
    candidate, _ = _incomplete_vm(catalog)
    vm, disk = candidate.vm_path, candidate.vm_path / "Disk.img"
    original = disk.read_bytes()
    (catalog.backend / "setup/create-vm.sh").write_text("# VPHONE_CREATOR_USB_READY VPHONE_FIRST_BOOT_QMP\n")
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_sudo_shim", lambda *_: None)
    monkeypatch.setattr(creator._USBHelpers, "start", lambda self: None)
    monkeypatch.setattr(creator._USBHelpers, "close", lambda self: None)
    monkeypatch.setattr(_creation_cleanup, "_loop_states", lambda: [], raising=False)
    monkeypatch.setattr(
        creator.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=os.getpid(), poll=lambda: 1, returncode=1),
    )
    monkeypatch.setattr(
        creator,
        "_capture_creation_scope_invocation",
        lambda *_: pytest.fail("the refused launcher has exited"),
    )
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
    inactive = survivor == "none"

    def unit_properties(unit, uid):
        return {
            "LoadState": "not-found" if inactive else "loaded",
            "ActiveState": "inactive" if inactive else "active",
            "InvocationID": "0" * 32 if inactive else "b" * 32,
            "ControlGroup": ""
            if inactive
            else f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}",
        }

    monkeypatch.setattr(_creation_cleanup, "_unit_properties", unit_properties)
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
        "upstream creation exited with status 1" if survivor == "none" else "creation scope identity changed"
    )
    with pytest.raises(RuntimeError, match=message):
        creator._pipeline(catalog, candidate, get_profile("iphone-duo"), {}, {})

    assert requests[0]["scope_owned"] is False
    receipt = creator._read_json(vm / ".limulator-create.json")
    assert "upstream creation exited with status 1" in receipt["error"]
    if survivor == "none":
        assert "cleanup_error" not in receipt
    else:
        assert "creation scope identity changed" in receipt["cleanup_error"]
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
    properties = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "InvocationID": _CREATION_INVOCATION_ID,
        "ControlGroup": group,
    }
    monkeypatch.setattr(_creation_cleanup, "_unit_properties", lambda *_: properties)
    monkeypatch.setattr(_creation_cleanup, "_scope_processes", lambda *_: list(alive.values()))
    monkeypatch.setattr(_creation_cleanup, "_identity", lambda pid: alive.get(pid))
    monkeypatch.setattr(_creation_cleanup, "_pidfd", lambda pid: os.open("/dev/null", os.O_RDONLY))
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: [])
    monkeypatch.setattr(_creation_cleanup, "_loop_states", lambda: [], raising=False)
    return unit, boot, identity, alive, _CREATION_INVOCATION_ID


@pytest.mark.parametrize("changed", ["start_ticks", "cgroup"])
def test_scope_cleanup_refuses_reused_pid_or_foreign_scope(cleanup_scope, monkeypatch, changed):
    unit, boot, identity, _, invocation_id = cleanup_scope
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
        _creation_cleanup.cleanup(
            unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id
        )


def test_scope_cleanup_refuses_replacement_before_process_enumeration(cleanup_scope, monkeypatch):
    unit, boot, _, _, invocation_id = cleanup_scope
    group = f"/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}"
    monkeypatch.setattr(
        _creation_cleanup,
        "_unit_properties",
        lambda *_: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "b" * 32,
            "ControlGroup": group,
        },
    )
    monkeypatch.setattr(
        _creation_cleanup, "_scope_processes", lambda *_: pytest.fail("replacement scope was enumerated")
    )
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: pytest.fail("replacement mounts were read"))
    with pytest.raises(RuntimeError, match="InvocationID"):
        _creation_cleanup.cleanup(
            unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id
        )


@pytest.mark.parametrize(
    "replacement_phase,expected_signals",
    [("before_term", []), ("before_kill", [creator.signal.SIGTERM])],
)
def test_scope_cleanup_refuses_replacement_before_signal_pass(
    cleanup_scope, monkeypatch, replacement_phase, expected_signals
):
    unit, boot, identity, _, invocation_id = cleanup_scope
    identity["exe"] = "/usr/bin/qemu-system-aarch64"
    replaced = False
    group = f"/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}"

    def unit_properties(*_):
        return {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "b" * 32 if replaced else invocation_id,
            "ControlGroup": group,
        }

    monkeypatch.setattr(_creation_cleanup, "_unit_properties", unit_properties)
    signals = []
    if replacement_phase == "before_term":

        def pin(pid):
            nonlocal replaced
            replaced = True
            return os.open("/dev/null", os.O_RDONLY)

        monkeypatch.setattr(_creation_cleanup, "_pidfd", pin)
    else:

        def terminate(fd, value):
            nonlocal replaced
            signals.append(value)

        monkeypatch.setattr(_creation_cleanup, "_signal", terminate)
        ticks = iter([0, 6])

        def monotonic():
            nonlocal replaced
            value = next(ticks)
            if value == 6:
                replaced = True
            return value

        monkeypatch.setattr(_creation_cleanup.time, "monotonic", monotonic)
    with pytest.raises(RuntimeError, match="InvocationID"):
        _creation_cleanup.cleanup(
            unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id
        )
    assert signals == expected_signals


@pytest.mark.parametrize("source", ["root@vphone:/mnt1", "root@foreign:/mnt1"])
@pytest.mark.parametrize("replacement_at_unmount", [False, True])
def test_scope_cleanup_requires_exact_private_sshfs_before_unmount_and_pidfd_signal(
    cleanup_scope, tmp_path, monkeypatch, source, replacement_at_unmount
):
    unit, boot, identity, alive, invocation_id = cleanup_scope
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
    replaced = False
    mount_reads = 0

    def read_mounts():
        nonlocal replaced, mount_reads
        mount_reads += 1
        current = list(attached)
        if replacement_at_unmount and mount_reads == 2:
            replaced = True
        return current

    monkeypatch.setattr(_creation_cleanup, "mounts", read_mounts)

    def unit_properties(_unit, uid):
        group = f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}"
        return {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "b" * 32 if replaced else invocation_id,
            "ControlGroup": group,
        }

    monkeypatch.setattr(_creation_cleanup, "_unit_properties", unit_properties)
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
        if replacement_at_unmount:
            with pytest.raises(RuntimeError, match="InvocationID"):
                _creation_cleanup.cleanup(
                    unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id
                )
            assert actions == []
        else:
            _creation_cleanup.cleanup(
                unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id
            )
            assert actions == ["unmount", "pidfd TERM"]
    else:
        with pytest.raises(RuntimeError, match="SSHFS mount identity differs"):
            _creation_cleanup.cleanup(
                unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id
            )
        assert actions == []


def test_scope_cleanup_stops_cfw_client_before_unmount_and_other_processes(
    cleanup_scope, tmp_path, monkeypatch
):
    unit, boot, first_sshfs, alive, invocation_id = cleanup_scope
    directory = tmp_path / "vphone-cfw.aabbccdd"
    directory.mkdir(mode=0o700)
    sources = ("/mnt1", "/mnt6", "/mnt2")
    volumes = ("system", "xart", "preboot")
    mounts = []
    sshfs_processes = []
    for index, (source, volume) in enumerate(zip(sources, volumes)):
        target = str(directory / volume)
        identity = first_sshfs if index == 0 else dict(first_sshfs, pid=4321 + index)
        identity["exe"] = "/usr/bin/sshfs"
        identity["argv"] = ["/usr/bin/sshfs", "-f", f"root@vphone:{source}", target]
        alive[identity["pid"]] = identity
        sshfs_processes.append(identity)
        mounts.append(
            {
                "id": str(index + 1),
                "target": target,
                "source": f"root@vphone:{source}",
                "type": "fuse.sshfs",
                "record": f"mount {volume}",
            }
        )
    client = dict(
        first_sshfs,
        pid=4444,
        exe="/opt/vphone-cli",
        argv=[
            "/opt/vphone-cli",
            "cfw",
            "install",
            "-l",
            "/vms",
            "Duo",
            "--work-parent",
            "/var/tmp",
            "--keep-artifacts",
        ],
    )
    alive[client["pid"]] = client
    other = dict(
        first_sshfs,
        pid=5555,
        uid=1000,
        exe="/usr/bin/qemu-system-aarch64",
        argv=["qemu-system-aarch64", "-name", "Duo"],
    )
    alive[other["pid"]] = other
    attached = list(mounts)
    monkeypatch.setattr(
        _creation_cleanup, "_cfw_target", lambda value: value in {m["target"] for m in mounts}
    )
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: list(attached))
    fstat = os.fstat

    def root_directory_metadata(fd):
        info = fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            return info
        metadata = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        return SimpleNamespace(**(metadata | {"st_uid": 0}))

    monkeypatch.setattr(_creation_cleanup.os, "fstat", root_directory_metadata)
    pinned = {}
    actions = []

    def pin(pid):
        fd = os.open("/dev/null", os.O_RDONLY)
        pinned[fd] = pid
        return fd

    def signal_process(fd, value):
        pid = pinned[fd]
        if pid == client["pid"]:
            actions.append(("client", value))
        else:
            actions.append(("scope", pid, value))
        alive.pop(pid, None)

    def unmount(command, **_kwargs):
        target = command[-1]
        if client["pid"] in alive:
            actions.append(("busy", target))
            return SimpleNamespace(returncode=1, stderr="Device or resource busy")
        actions.append(("unmount", target))
        attached[:] = [mount for mount in attached if mount["target"] != target]
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(_creation_cleanup, "_pidfd", pin)
    monkeypatch.setattr(_creation_cleanup, "_signal", signal_process)
    monkeypatch.setattr(_creation_cleanup.subprocess, "run", unmount)
    _creation_cleanup.cleanup(unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id)

    client_term = actions.index(("client", creator.signal.SIGTERM))
    unmounts = [index for index, action in enumerate(actions) if action[0] == "unmount"]
    remaining_terms = [
        index
        for index, action in enumerate(actions)
        if action[0] == "scope" and action[2] == creator.signal.SIGTERM
    ]
    assert len(unmounts) == 3
    assert len(remaining_terms) == len(sshfs_processes) + 1
    assert client_term < min(unmounts) <= max(unmounts) < min(remaining_terms)
    assert not any(action[0] == "busy" for action in actions)
    assert attached == []


def test_scope_cleanup_refuses_mount_without_an_owned_process(cleanup_scope, monkeypatch):
    unit, boot, _, alive, invocation_id = cleanup_scope
    alive.clear()
    mount = {"target": "/var/tmp/vphone-cfw.aabbccdd/system", "record": "unattributed mount"}
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: [mount])
    monkeypatch.setattr(
        _creation_cleanup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not unmount")
    )
    with pytest.raises(RuntimeError, match="mount ownership could not be proven"):
        _creation_cleanup.cleanup(
            unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id
        )


def test_scope_cleanup_accepts_empty_scope_that_exits_during_inspection(cleanup_scope, monkeypatch):
    unit, boot, _, alive, invocation_id = cleanup_scope
    alive.clear()
    properties = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "InvocationID": invocation_id,
        "ControlGroup": f"/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}",
    }
    monkeypatch.setattr(_creation_cleanup, "_unit_properties", lambda *_: properties)

    def scope_processes(*_):
        properties.update(
            LoadState="not-found", ActiveState="inactive", InvocationID="0" * 32, ControlGroup=""
        )
        return []

    monkeypatch.setattr(_creation_cleanup, "_scope_processes", scope_processes)
    monkeypatch.setattr(_creation_cleanup, "_signal", lambda *_: pytest.fail("empty scope must not signal"))
    monkeypatch.setattr(
        _creation_cleanup.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("empty scope must not unmount"),
    )
    _creation_cleanup.cleanup(unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id)


def test_scope_cleanup_rejects_mount_left_by_inactive_unowned_scope(cleanup_scope, monkeypatch):
    unit, boot, _, alive, _ = cleanup_scope
    alive.clear()
    properties = {
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "InvocationID": "0" * 32,
        "ControlGroup": "",
    }
    mount = {"target": "/var/tmp/vphone-cfw.aabbccdd/system", "record": "leftover mount"}
    monkeypatch.setattr(_creation_cleanup, "_unit_properties", lambda *_: properties)
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: [mount])
    monkeypatch.setattr(
        _creation_cleanup.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("inactive unowned mount must not be detached"),
    )
    with pytest.raises(RuntimeError, match="inactive creation scope has residual resources"):
        _creation_cleanup.cleanup(unit, 1000, boot, [])


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


@pytest.mark.parametrize(
    "path_kind, message", [("ascii", "107 bytes"), ("utf8", "107 bytes"), ("nul", "NUL")]
)
def test_private_helpers_reject_unusable_socket_path_before_host_setup(
    catalog, monkeypatch, path_kind, message
):
    if path_kind == "nul":
        directory = catalog.data / "bad\0name"
    else:
        padding = 108 - len(os.fsencode(catalog.data / "usbmuxd.sock")) - 1
        component = "é" + "a" * (padding - 2) if path_kind == "utf8" else "a" * padding
        directory = catalog.data / component
        assert len(os.fsencode(directory / "usbmuxd.sock")) == 108
        if path_kind == "utf8":
            assert len(str(directory / "usbmuxd.sock")) == 107
    env = {"LIMULATOR_CREATOR_USB_READY": "1", "VPHONE_CREATOR_USB_READY": "0"}
    helper = creator._USBHelpers(catalog, directory, env, "owned.scope")

    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid socket path must be rejected before host setup")

    monkeypatch.setattr(creator, "_apple_devices", unexpected)
    monkeypatch.setattr(creator, "_sudo", unexpected)
    monkeypatch.setattr(creator.subprocess, "Popen", unexpected)
    with pytest.raises(ValueError, match=message):
        helper.start()
    assert env == {"LIMULATOR_CREATOR_USB_READY": "1", "VPHONE_CREATOR_USB_READY": "0"}
    assert helper.daemon is None
    assert helper.rule_created is False


@pytest.mark.parametrize("socket_path_bytes", [None, 107])
def test_private_helpers_publish_native_ready_only_after_private_socket(
    catalog, tmp_path, monkeypatch, socket_path_bytes
):
    if socket_path_bytes is not None:
        padding = socket_path_bytes - len(os.fsencode(catalog.data / "usbmuxd.sock")) - 1
        tmp_path = catalog.data / ("a" * padding)
        tmp_path.mkdir(parents=True)
        assert len(os.fsencode(tmp_path / "usbmuxd.sock")) == socket_path_bytes
    env = {"LIMULATOR_CREATOR_USB_READY": "1", "VPHONE_CREATOR_USB_READY": "0"}
    helper = creator._USBHelpers(catalog, tmp_path, env, "owned.scope")
    assert env == {"LIMULATOR_CREATOR_USB_READY": "1", "VPHONE_CREATOR_USB_READY": "0"}
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: [])
    monkeypatch.setattr(creator, "_vhci_ports", lambda: {})
    monkeypatch.setattr(creator, "_sudo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(creator, "_identity", lambda _pid: {"pid": 1234})
    monkeypatch.setattr(
        creator.socket,
        "socket",
        lambda *_args: nullcontext(SimpleNamespace(setsockopt=lambda *_: None, bind=lambda *_: None)),
    )

    def start_daemon(_command, **_kwargs):
        assert "LIMULATOR_CREATOR_USB_READY" not in env
        assert "VPHONE_CREATOR_USB_READY" not in env
        return SimpleNamespace(pid=1234, poll=lambda: None)

    ready = iter((False, True))

    def socket_ready(path):
        assert path == tmp_path / "usbmuxd.sock"
        assert "LIMULATOR_CREATOR_USB_READY" not in env
        assert "VPHONE_CREATOR_USB_READY" not in env
        return next(ready)

    monkeypatch.setattr(creator.subprocess, "Popen", start_daemon)
    monkeypatch.setattr(creator.Path, "is_socket", socket_ready)
    monkeypatch.setattr(creator.time, "sleep", lambda _seconds: None)
    helper.start()
    assert "LIMULATOR_CREATOR_USB_READY" not in env
    assert env["VPHONE_CREATOR_USB_READY"] == "1"
    assert env["USBMUXD_SOCKET_ADDRESS"] == f"UNIX:{tmp_path / 'usbmuxd.sock'}"


@pytest.mark.parametrize("suffix", ["FIRST_BOOT_TIMEOUT", "SHUTDOWN_TIMEOUT", "MIGRATION_TIMEOUT"])
@pytest.mark.parametrize(
    "legacy,generic,expected",
    [(None, None, None), ("17", None, "17"), (None, "23", "23"), ("17", "99", "99"), ("", "99", "99")],
)
def test_native_environment_removes_timeout_aliases_and_prefers_vphone(
    catalog, monkeypatch, suffix, legacy, generic, expected
):
    legacy_key, generic_key = f"LIMULATOR_{suffix}", f"VPHONE_{suffix}"
    for key, value in ((legacy_key, legacy), (generic_key, generic)):
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    env = creator._native_env(catalog)
    if expected is None:
        assert legacy_key not in env and generic_key not in env
    else:
        assert legacy_key not in env
        assert env[generic_key] == expected


def test_backend_control_aliases_are_removed_and_keep_native_precedence():
    env = {
        "LIMULATOR_HOME": "/tmp/limulator-data",
        "LIMULATOR_GRAPHICS_MODE": "diagnostic",
        "LIMULATOR_FIRST_BOOT_QMP": "/tmp/legacy-qmp.sock",
        "VPHONE_FIRST_BOOT_QMP": "/tmp/native-qmp.sock",
        "LIMULATOR_USB_TRACE": "1",
        "VPHONE_USB_TRACE": "0",
        "LIMULATOR_USB_TRACE_FILE": "/tmp/legacy-trace.jsonl",
        "VPHONE_USB_TRACE_FILE": "/tmp/native-trace.jsonl",
        "LIMULATOR_CREATOR_USB_READY": "1",
        "VPHONE_CREATOR_USB_READY": "1",
    }
    creator._normalize_backend_controls(env)

    assert env == {
        "LIMULATOR_HOME": "/tmp/limulator-data",
        "LIMULATOR_GRAPHICS_MODE": "diagnostic",
        "VPHONE_FIRST_BOOT_QMP": "/tmp/native-qmp.sock",
        "VPHONE_USB_TRACE": "0",
        "VPHONE_USB_TRACE_FILE": "/tmp/native-trace.jsonl",
    }

    legacy = {
        "LIMULATOR_FIRST_BOOT_QMP": "/tmp/legacy-qmp.sock",
        "LIMULATOR_USB_TRACE": "1",
        "LIMULATOR_USB_TRACE_FILE": "/tmp/legacy-trace.jsonl",
    }
    creator._normalize_backend_controls(legacy)
    assert legacy == {
        "VPHONE_FIRST_BOOT_QMP": "/tmp/legacy-qmp.sock",
        "VPHONE_USB_TRACE": "1",
        "VPHONE_USB_TRACE_FILE": "/tmp/legacy-trace.jsonl",
    }


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


def test_usb_transport_trace_is_opt_in_and_deduplicates_state(catalog, tmp_path, monkeypatch):
    trace_path = tmp_path / "usb-transport-trace.jsonl"
    disabled = creator._USBHelpers(catalog, tmp_path, {}, "owned.scope")
    disabled._trace("host.listener_state", listener_count=0)
    assert not trace_path.exists()

    monkeypatch.setenv("LIMULATOR_USB_TRACE", "1")
    monkeypatch.setenv("LIMULATOR_USB_TRACE_FILE", str(trace_path))
    env = creator._native_env(catalog)
    assert env["VPHONE_USB_TRACE"] == "1"
    assert "LIMULATOR_USB_TRACE" not in env
    assert "LIMULATOR_USB_TRACE_FILE" not in env
    helper = creator._USBHelpers(catalog, tmp_path, env, "owned.scope")
    for _ in range(2):
        helper._trace_change(
            "listener",
            (0, False),
            "host.listener_state",
            listener_count=0,
            scope_verified=False,
        )

    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["source"] == "host"
    assert records[0]["event"] == "host.listener_state"
    assert records[0]["listener_count"] == 0
    assert records[0]["scope_verified"] is False
    assert records[0]["monotonic_ns"] > 0
    assert records[0]["utc"].endswith("Z")
    assert stat.S_IMODE(trace_path.stat().st_mode) == 0o600


def test_optional_product_id_read_does_not_skip_owned_rescan(catalog, tmp_path, monkeypatch):
    trace_path = tmp_path / "usb-transport-trace.jsonl"
    helper = creator._USBHelpers(
        catalog,
        tmp_path,
        {"VPHONE_USB_TRACE": "1", "VPHONE_USB_TRACE_FILE": str(trace_path)},
        "owned.scope",
    )
    helper.daemon_identity = creator._identity(os.getpid())
    helper.owned_ports = {0}
    connection = ("net:[123]", "0100007F:1234", "0100007F:0CA8", 5678)
    helper.import_connections = {0: connection}
    helper._owned_import = lambda _port: True
    monkeypatch.setattr(creator, "_apple_devices", lambda: [])
    monkeypatch.setattr(creator, "_listener_pids", lambda: [1234])
    monkeypatch.setattr(creator, "_in_scope", lambda *_: True)
    monkeypatch.setattr(creator, "_vhci_ports", lambda: {0: "11-1"})
    monkeypatch.setattr(creator, "_usbip_connections", lambda: {connection})
    signals = []
    monkeypatch.setattr(
        creator, "_signal_owned", lambda identity, value: signals.append(value) or True
    )

    class SysfsPath:
        def __init__(self, value):
            self.value = str(value)

        def __truediv__(self, value):
            return SysfsPath(f"{self.value}/{value}")

        def read_text(self):
            name = self.value.rsplit("/", 1)[-1]
            values = {"idVendor": "05ac", "busnum": "11", "devnum": "2"}
            if name == "idProduct":
                raise FileNotFoundError(self.value)
            return values[name]

        def stat(self):
            return SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFCHR | 0o600)

    monkeypatch.setattr(creator, "Path", SysfsPath)
    helper.poll()

    assert signals == [creator.signal.SIGUSR2]
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    node_event = next(event for event in events if event["event"] == "host.vhci_device_node")
    assert node_event["product_id"] is None
    assert node_event["permitted"] is True
    assert any(event["event"] == "host.usbmux_rescan" and event["sent"] for event in events)


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


@pytest.fixture
def cryptex_cleanup_scope(cleanup_scope, tmp_path, monkeypatch):
    unit, boot, sshfs, alive, invocation_id = cleanup_scope
    directory = tmp_path / "vphone-cfw.aabbccdd"
    directory.mkdir(mode=0o700)
    ssh_target = str(directory / "system")
    sshfs["argv"] = ["/usr/bin/sshfs", "-f", "root@vphone:/mnt1", ssh_target]
    client = dict(sshfs, pid=4444, exe="/opt/vphone-cli", argv=["/opt/vphone-cli", "cfw", "install"])
    alive[client["pid"]] = client
    attached = [
        {
            "id": "1",
            "target": ssh_target,
            "source": "root@vphone:/mnt1",
            "type": "fuse.sshfs",
            "record": "sshfs",
        }
    ]
    loops = {}
    for index, (image, target) in enumerate((("SystemOS.dmg", "mnt-os"), ("AppOS.dmg", "mnt-app")), 40):
        path = directory / image
        path.write_bytes(b"owned image")
        info = path.stat()
        device = f"/dev/loop{index}"
        loops[device] = {
            "path": device,
            "node": (1, 9000 + index, os.makedev(7, index)),
            "backing": (info.st_dev, info.st_ino),
            "number": index,
            "offset": 0,
            "limit": 0,
            "encryption": 0,
            "key_size": 0,
            "flags": 1,
            "filename": f"/var/tmp/vphone-cfw.aabbccdd/{image}",
            "record": image.encode(),
        }
        attached.append(
            {
                "id": str(index),
                "target": str(directory / target),
                "source": device,
                "type": "apfs",
                "device": f"7:{index}",
                "options": ["ro"],
                "record": target,
            }
        )
        (tmp_path / "sys/dev/block" / f"7:{index}" / "holders").mkdir(parents=True)
    (tmp_path / "proc").mkdir()
    real_path = _creation_cleanup.Path

    def host_path(value):
        value = str(value)
        if value == "/proc" or value.startswith("/sys/dev/block/"):
            return tmp_path / value.lstrip("/")
        return real_path(value)

    monkeypatch.setattr(_creation_cleanup, "Path", host_path)
    monkeypatch.setattr(_creation_cleanup, "_cfw_target", lambda value: value == ssh_target)
    monkeypatch.setattr(
        _creation_cleanup,
        "_cryptex_target",
        lambda value: value in {str(directory / "mnt-os"), str(directory / "mnt-app")},
        raising=False,
    )
    monkeypatch.setattr(_creation_cleanup, "mounts", lambda: list(attached))
    monkeypatch.setattr(
        _creation_cleanup, "_loop_states", lambda: [dict(loop) for loop in loops.values()], raising=False
    )
    process_fds, loop_fds = {}, {}
    actions = []
    state = SimpleNamespace(after_client_stop=None, after_unmount=None, alive=alive, identity=sshfs)
    properties = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "InvocationID": invocation_id,
        "ControlGroup": sshfs["cgroup"].strip()[3:],
    }
    monkeypatch.setattr(_creation_cleanup, "_unit_properties", lambda *_: dict(properties))

    def pin(pid):
        fd = os.open("/dev/null", os.O_RDONLY)
        process_fds[fd] = pid
        return fd

    def open_loop(path, *, exclusive=False):
        fd = os.open("/dev/null", os.O_RDONLY)
        loop_fds[fd] = path
        return fd

    real_fstat = os.fstat
    real_close = os.close

    def close(fd):
        loop_fds.pop(fd, None)
        real_close(fd)

    def metadata(fd):
        if fd in loop_fds:
            dev, ino, rdev = loops[loop_fds[fd]]["node"]
            return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_dev=dev, st_ino=ino, st_rdev=rdev)
        info = real_fstat(fd)
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        return SimpleNamespace(**(values | {"st_uid": 0}))

    def loop_status(fd):
        loop = loops.get(loop_fds[fd])
        return {key: value for key, value in loop.items() if key not in ("path", "node")} if loop else None

    def ioctl(fd, request):
        assert request == 0x4C01
        path = loop_fds[fd]
        actions.append(("detach", path))
        loops.pop(path)

    def signal_process(fd, value):
        pid = process_fds[fd]
        actions.append(("signal", pid, value))
        alive.pop(pid)
        if pid == client["pid"] and state.after_client_stop:
            state.after_client_stop()

    def unmount(command, **_kwargs):
        target = command[-1]
        assert command == (
            ["/usr/bin/umount", "--", target]
            if target != ssh_target
            else ["/usr/bin/fusermount3", "-u", target]
        )
        actions.append(("unmount", target))
        attached[:] = [mount for mount in attached if mount["target"] != target]
        if state.after_unmount:
            state.after_unmount(target)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(_creation_cleanup, "_pidfd", pin)
    monkeypatch.setattr(_creation_cleanup, "_signal", signal_process)
    monkeypatch.setattr(_creation_cleanup, "_open_loop", open_loop, raising=False)
    monkeypatch.setattr(_creation_cleanup, "_loop_status", loop_status, raising=False)
    monkeypatch.setattr(_creation_cleanup.os, "fstat", metadata)
    monkeypatch.setattr(_creation_cleanup.os, "close", close)
    monkeypatch.setattr(fcntl, "ioctl", ioctl)
    monkeypatch.setattr(_creation_cleanup.subprocess, "run", unmount)
    state.run = lambda baseline=(): _creation_cleanup.cleanup(
        unit, 1000, boot, list(baseline), scope_owned=True, expected_invocation_id=invocation_id
    )
    return state, directory, attached, loops, actions, properties, tmp_path


@pytest.mark.parametrize("mounted", [False, True])
def test_scope_cleanup_detaches_owned_cryptex_before_sshfs(cryptex_cleanup_scope, mounted):
    state, directory, attached, loops, actions, _, _ = cryptex_cleanup_scope
    if not mounted:
        attached[:] = [mount for mount in attached if mount["type"] == "fuse.sshfs"]
    state.run()
    assert loops == {}
    assert attached == []
    assert actions[0] == ("signal", 4444, creator.signal.SIGTERM)
    ssh_unmount = actions.index(("unmount", str(directory / "system")))
    assert all(index < ssh_unmount for index, action in enumerate(actions) if action[0] == "detach")
    if mounted:
        for number, target in ((40, "mnt-os"), (41, "mnt-app")):
            assert actions.index(("unmount", str(directory / target))) < actions.index(
                ("detach", f"/dev/loop{number}")
            )
    assert actions[-1] == ("signal", 4321, creator.signal.SIGTERM)


@pytest.mark.parametrize("foreign", ["writable", "backing", "mount", "baseline", "hardlink", "symlink"])
def test_scope_cleanup_refuses_unproven_cryptex_before_mutation(cryptex_cleanup_scope, foreign):
    state, directory, attached, loops, actions, _, _ = cryptex_cleanup_scope
    baseline = []
    if foreign == "writable":
        loops["/dev/loop41"]["flags"] = 0
    elif foreign == "backing":
        loops["/dev/loop41"]["backing"] = (1, 2)
    elif foreign == "mount":
        attached.append(dict(attached[-1], target="/foreign/mount", record="foreign mount"))
    elif foreign == "baseline":
        baseline = [dict(attached[-1])]
    elif foreign == "hardlink":
        os.link(directory / "AppOS.dmg", directory / "foreign-image")
    else:
        (directory / "AppOS.dmg").rename(directory / "actual-image")
        (directory / "AppOS.dmg").symlink_to("actual-image")
    with pytest.raises((RuntimeError, OSError)):
        state.run(baseline)
    assert actions == []
    assert loops


@pytest.mark.parametrize("replacement", ["directory", "file", "mount", "invocation"])
def test_scope_cleanup_preserves_replacements_before_cryptex_unmount(cryptex_cleanup_scope, replacement):
    state, directory, attached, loops, actions, properties, _ = cryptex_cleanup_scope

    def replace():
        if replacement == "directory":
            directory.rename(directory.with_name("old-directory"))
            directory.mkdir(mode=0o700)
        elif replacement == "file":
            (directory / "AppOS.dmg").rename(directory / "old-image")
            (directory / "AppOS.dmg").write_bytes(b"replacement")
        elif replacement == "mount":
            attached[-1] = dict(attached[-1], source="/dev/foreign", record="replacement mount")
        else:
            properties["InvocationID"] = "b" * 32

    state.after_client_stop = replace
    with pytest.raises(RuntimeError, match="identity changed|InvocationID|ownership could not be proven"):
        state.run()
    assert actions == [("signal", 4444, creator.signal.SIGTERM)]
    assert loops


@pytest.mark.parametrize("replacement", ["loop", "invocation", "holder", "partition", "open_user"])
def test_scope_cleanup_preserves_busy_or_replaced_loop_before_detach(
    cryptex_cleanup_scope, monkeypatch, replacement
):
    state, directory, _, loops, actions, properties, temporary = cryptex_cleanup_scope

    def replace(target):
        if target != str(directory / "mnt-app"):
            return
        if replacement == "loop":
            loops["/dev/loop41"] = dict(loops["/dev/loop41"], backing=(2, 3), record=b"foreign attachment")
        elif replacement == "invocation":
            properties["InvocationID"] = "b" * 32
        elif replacement == "holder":
            (temporary / "sys/dev/block/7:41/holders/foreign").touch()
        elif replacement == "partition":
            (temporary / "sys/dev/block/7:41/loop41p1").mkdir()
        else:
            descriptor = temporary / "proc/999/fd/3"
            descriptor.parent.mkdir(parents=True)
            descriptor.touch()
            real_stat = os.stat

            def metadata(path, *args, **kwargs):
                if str(path) == str(descriptor):
                    return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 41))
                return real_stat(path, *args, **kwargs)

            monkeypatch.setattr(_creation_cleanup.os, "stat", metadata)

    state.after_unmount = replace
    with pytest.raises(RuntimeError, match="identity changed|InvocationID|holders|partitions|open user"):
        state.run()
    assert actions == [("signal", 4444, creator.signal.SIGTERM), ("unmount", str(directory / "mnt-app"))]
    assert loops


@pytest.mark.parametrize("late", ["existing_producer", "new_process", "new_loop", "new_mount"])
def test_scope_cleanup_refuses_late_creation_resources(cryptex_cleanup_scope, late):
    state, directory, attached, loops, actions, _, _ = cryptex_cleanup_scope
    if late == "existing_producer":
        state.alive[5555] = dict(state.identity, pid=5555, exe="/usr/sbin/losetup", argv=["losetup"])
    if late == "new_loop":
        attached[:] = [mount for mount in attached if mount["type"] == "fuse.sshfs"]
        new_loop = loops["/dev/loop41"]
        loops.clear()
    if late == "new_mount":
        new_mount = attached.pop()

    def add_resource():
        if late == "new_process":
            state.alive[5555] = dict(state.identity, pid=5555, exe="/usr/sbin/losetup", argv=["losetup"])
        elif late == "new_loop":
            loops["/dev/loop41"] = new_loop
        elif late == "new_mount":
            attached.append(new_mount)

    state.after_client_stop = add_resource
    with pytest.raises(RuntimeError, match="resource producers|attachment changed"):
        state.run()
    assert actions == [("signal", 4444, creator.signal.SIGTERM)]
    assert attached
    assert loops


def test_scope_cleanup_rechecks_loops_before_reporting_success(cryptex_cleanup_scope, monkeypatch):
    state, _, _, loops, actions, _, _ = cryptex_cleanup_scope
    late_loop = dict(loops["/dev/loop41"])
    signal_process = _creation_cleanup._signal

    def signal_and_attach(fd, value):
        signal_process(fd, value)
        if not state.alive:
            loops[late_loop["path"]] = late_loop

    monkeypatch.setattr(_creation_cleanup, "_signal", signal_and_attach)
    with pytest.raises(RuntimeError, match="residual Cryptex loop attachments"):
        state.run()
    assert actions[-1] == ("signal", 4321, creator.signal.SIGTERM)
    assert loops == {late_loop["path"]: late_loop}


def test_loop_status_reads_kernel_backing_identity_and_accepts_unbound_device(monkeypatch):
    import struct

    data = struct.pack("=QQQQQIIII64s64s32sQQ", 123, 456, 0, 0, 0, 7, 0, 0, 1, b"image", b"", b"", 0, 0)

    def status(fd, request, output):
        assert fd == 42 and request == 0x4C05
        output[:] = data

    monkeypatch.setattr(fcntl, "ioctl", status)
    result = _creation_cleanup._loop_status(42)
    assert result["backing"] == (123, 456)
    assert result["number"] == 7
    assert result["flags"] == 1
    assert result["record"] == data

    def unbound(*_):
        import errno

        raise OSError(errno.ENXIO, "No such device or address")

    monkeypatch.setattr(fcntl, "ioctl", unbound)
    assert _creation_cleanup._loop_status(42) is None


def test_scope_cleanup_uses_kernel_device_number_when_loop_partitions_are_configured(cryptex_cleanup_scope):
    state, _, attached, loops, _, _, temporary = cryptex_cleanup_scope
    device, inode, _ = loops["/dev/loop41"]["node"]
    loops["/dev/loop41"]["node"] = (device, inode, os.makedev(7, 410))
    attached[-1]["device"] = "7:410"
    (temporary / "sys/dev/block/7:410/holders").mkdir(parents=True)
    state.run()
    assert loops == {}


@pytest.mark.parametrize("inactive,mounted", [(False, False), (False, True), (True, False)])
def test_scope_cleanup_refuses_cfw_loops_without_live_sshfs_provenance(
    cryptex_cleanup_scope, inactive, mounted
):
    state, _, attached, loops, actions, properties, _ = cryptex_cleanup_scope
    state.alive.pop(4321)
    attached[:] = [mount for mount in attached if mounted and mount["type"] == "apfs"]
    if inactive:
        state.alive.clear()
        properties.update(ActiveState="inactive", InvocationID="0" * 32, ControlGroup="")
    with pytest.raises(RuntimeError, match="unproven CFW loop|ownership could not be proven"):
        state.run()
    assert actions == []
    assert loops


def test_scope_cleanup_refuses_late_unproven_cfw_loop_when_no_images_were_anchored(
    cleanup_scope, monkeypatch
):
    unit, boot, identity, alive, invocation_id = cleanup_scope
    identity.update(exe="/usr/bin/qemu-system-aarch64", argv=["qemu-system-aarch64"])
    loops = []
    monkeypatch.setattr(_creation_cleanup, "_loop_states", lambda: list(loops))

    def terminate(*_):
        alive.clear()
        loops.append({"path": "/dev/loop42", "filename": "/var/tmp/vphone-cfw.aabbccdd/AppOS.dmg"})

    monkeypatch.setattr(_creation_cleanup, "_signal", terminate)
    monkeypatch.setattr(
        _creation_cleanup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not unmount")
    )
    with pytest.raises(RuntimeError, match="unproven CFW loop"):
        _creation_cleanup.cleanup(
            unit, 1000, boot, [], scope_owned=True, expected_invocation_id=invocation_id
        )
    assert loops


@pytest.mark.parametrize("major,minor,encoded", [(8, 0x12345, 0x12300845), (0xFFF, 0xFFFFF, 0xFFFFFFFF)])
def test_loop_status_matches_stat_device_encoding_for_high_minors(monkeypatch, major, minor, encoded):
    import struct

    data = struct.pack("=QQQQQIIII64s64s32sQQ", encoded, 456, 0, 0, 0, 7, 0, 0, 1, b"image", b"", b"", 0, 0)

    def status(_fd, _request, output):
        output[:] = data

    monkeypatch.setattr(fcntl, "ioctl", status)
    assert _creation_cleanup._loop_status(42)["backing"] == (os.makedev(major, minor), 456)


def test_loop_status_rejects_unsupported_device_encoding(monkeypatch):
    import struct

    data = struct.pack("=QQQQQIIII64s64s32sQQ", 1 << 32, 456, 0, 0, 0, 7, 0, 0, 1, b"image", b"", b"", 0, 0)

    def status(_fd, _request, output):
        output[:] = data

    monkeypatch.setattr(fcntl, "ioctl", status)
    with pytest.raises(RuntimeError, match="unsupported loop backing-device encoding"):
        _creation_cleanup._loop_status(42)
