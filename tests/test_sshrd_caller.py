import os
import plistlib
import tempfile
from pathlib import Path

import pytest

from limulator import creator
from limulator.lifecycle import Device, Paths
from limulator.profiles import get_profile


@pytest.fixture
def caller_environment(monkeypatch):
    for name in ("VPHONE_SSHRD_BOOTSTRAP", "VPHONE_SSHRD_BOOTSTRAP_BUILD", "VPHONE_ECID"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("caller_environment")
def test_default_caller_explicitly_selects_ecid_aware_source(tmp_path):
    environment = creator._native_env(Paths(tmp_path))
    assert environment["VPHONE_SSHRD_BOOTSTRAP_BUILD"] == "1"


@pytest.mark.usefixtures("caller_environment")
@pytest.mark.parametrize("build", [None, "0"])
def test_prebuilt_selection_resolves_before_backend_changes_directory(tmp_path, monkeypatch, build):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VPHONE_SSHRD_BOOTSTRAP", "bootstrap with spaces")
    if build is not None:
        monkeypatch.setenv("VPHONE_SSHRD_BOOTSTRAP_BUILD", build)
    environment = creator._native_env(Paths(tmp_path / "data"))
    assert environment["VPHONE_SSHRD_BOOTSTRAP_BUILD"] == "0"
    assert environment["VPHONE_SSHRD_BOOTSTRAP"] == str(tmp_path / "bootstrap with spaces")


@pytest.mark.usefixtures("caller_environment")
@pytest.mark.parametrize(
    ("build", "prebuilt", "message"),
    [
        ("", "", "must be 0 or 1"),
        ("true", "", "must be 0 or 1"),
        ("2", "", "must be 0 or 1"),
        ("1", "bootstrap", "choose"),
        ("0", "", "ECID-aware"),
    ],
)
def test_invalid_or_stock_choices_fail_before_the_creation_pipeline(
    tmp_path, monkeypatch, build, prebuilt, message
):
    monkeypatch.setenv("VPHONE_SSHRD_BOOTSTRAP_BUILD", build)
    monkeypatch.setenv("VPHONE_SSHRD_BOOTSTRAP", prebuilt)
    with pytest.raises(ValueError, match=message):
        creator._native_env(Paths(tmp_path))


def write_identity(vm, *, text="0x1234\n", configured=0x1234):
    (vm / "ecid").write_text(text)
    (vm / "config.plist").write_bytes(
        plistlib.dumps({"diskImage": "Disk.img", "machineIdentifier": plistlib.dumps({"ECID": configured})})
    )


@pytest.mark.parametrize(
    ("text", "configured"),
    [
        ("0\n", 0),
        ("0x\n", 0x1234),
        (" 1234\n", 0x1234),
        ("-1234\n", 0x1234),
        ("10000000000000000\n", 0x1234),
        ("1234\n", 0x4321),
        ("1\n", True),
        ("1\n", 1.0),
        ("1\n", "1"),
        ("1\n", -1),
    ],
)
def test_binding_refuses_invalid_or_disagreeing_bundle_identities(tmp_path, text, configured):
    write_identity(tmp_path, text=text, configured=configured)
    environment = {}
    with pytest.raises(ValueError, match="ECID"):
        creator._bind_vm_ecid(tmp_path, environment)
    assert "VPHONE_ECID" not in environment


@pytest.mark.parametrize("override", ["", "0", "1.0", " 1234", "0x10000000000000000"])
def test_binding_refuses_malformed_override(tmp_path, override):
    write_identity(tmp_path)
    environment = {"VPHONE_ECID": override}
    with pytest.raises(ValueError, match="VPHONE_ECID"):
        creator._bind_vm_ecid(tmp_path, environment)
    assert environment["VPHONE_ECID"] == override


@pytest.mark.parametrize("selected", [1, (1 << 64) - 1])
def test_binding_accepts_nonzero_uint64_boundaries(tmp_path, selected):
    write_identity(tmp_path, text=f"{selected:016X}\n", configured=selected)
    environment = {"VPHONE_ECID": f"0X{selected:016X}"}
    creator._bind_vm_ecid(tmp_path, environment)
    assert environment["VPHONE_ECID"] == f"0x{selected:x}"


@pytest.mark.parametrize("node", ["fifo", "symlink", "oversized"])
def test_binding_refuses_nonregular_or_oversized_identity_before_open(tmp_path, monkeypatch, node):
    write_identity(tmp_path)
    target = tmp_path / "ecid"
    target.unlink()
    if node == "fifo":
        os.mkfifo(target)
    elif node == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("0x1234\n")
        target.symlink_to(outside)
    else:
        target.write_bytes(b"1" * 65)
    monkeypatch.setattr(creator.os, "open", lambda *_args: pytest.fail("invalid identity reached open"))
    with pytest.raises(ValueError, match="bounded regular file"):
        creator._bind_vm_ecid(tmp_path, {})


@pytest.fixture
def short_paths():
    with tempfile.TemporaryDirectory(prefix="lim-sshrd-", dir="/tmp") as directory:
        yield Paths(Path(directory) / "data")


@pytest.mark.parametrize(
    ("override", "config_node"),
    [("0x4321", None), ("0X00001234", None), (None, None), (None, "fifo"), (None, "symlink")],
)
def test_pipeline_validates_and_binds_the_selected_bundle_before_native_helpers(
    tmp_path, short_paths, monkeypatch, override, config_node
):
    paths = short_paths
    backend = tmp_path / "backend"
    script = backend / "setup/create-vm.sh"
    script.parent.mkdir(parents=True)
    script.write_text("# VPHONE_CREATOR_USB_READY VPHONE_FIRST_BOOT_QMP\n")
    monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
    profile = get_profile("iphone-duo")
    candidate = Device("fixture", paths.vms / "fixture", profile.model, profile.os_version)
    vm = candidate.vm_path
    vm.mkdir(parents=True)
    (vm / "Disk.img").write_bytes(b"fixture disk")
    (vm / ".create-state").write_text("bundle\n")
    write_identity(vm)
    if config_node is not None:
        config = vm / "config.plist"
        contents = config.read_bytes()
        config.unlink()
        if config_node == "fifo":
            os.mkfifo(config)
        else:
            outside = tmp_path / "outside.plist"
            outside.write_bytes(contents)
            config.symlink_to(outside)
        original_open = Path.open

        def refuse_blocking_config(path, *args, **kwargs):
            if path == config:
                pytest.fail("invalid config reached a blocking read")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse_blocking_config)
        monkeypatch.setattr(
            creator,
            "_apply_profile_configuration",
            lambda *_args: pytest.fail("invalid config reached a profile write"),
        )
    creator._atomic_json(
        vm / ".limulator-create.json", {"specification": creator._creation_specification(candidate)}
    )
    environment = {"VPHONE_SSHRD_BOOTSTRAP_BUILD": "1"}
    if override is not None:
        environment["VPHONE_ECID"] = override
    monkeypatch.setattr(creator, "_verified_firmware_stage", lambda *_args: [])
    monkeypatch.setattr(creator, "_unit_states", lambda: {})
    monkeypatch.setattr(creator, "_lock_disk", lambda filename: os.open(filename, os.O_RDONLY))
    seen = []

    class Helpers:
        def __init__(self, *_args):
            pass

        def start(self):
            if override == "0x4321" or config_node is not None:
                pytest.fail("invalid bundle reached native helpers")
            seen.append(environment["VPHONE_ECID"])
            raise RuntimeError("fixture stops before native helpers")

        def close(self):
            pass

    monkeypatch.setattr(creator, "_USBHelpers", Helpers)
    monkeypatch.setattr(
        creator.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("native process started")
    )
    expected = (
        "invalid VM config.plist"
        if config_node is not None
        else "does not match"
        if override == "0x4321"
        else "fixture stops"
    )
    with pytest.raises((ValueError, RuntimeError), match=expected):
        creator._pipeline(paths, candidate, profile, {}, environment)
    assert seen == ([] if override == "0x4321" or config_node is not None else ["0x1234"])
