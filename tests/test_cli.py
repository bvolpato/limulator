import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call

import pytest

from limulator import cli
from limulator.lifecycle import Paths


def test_module_entrypoint_propagates_an_unknown_device_failure(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "limulator", "--data", str(tmp_path), "status"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "unknown device" in result.stderr


def test_create_profile_resolves_model_os_and_scale(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("limulator.creator.create_device", lambda *args, **kwargs: calls.append(args) or {})
    assert (
        cli.main(
            [
                "--data",
                str(tmp_path),
                "--device",
                "mini",
                "create",
                "--profile",
                "ipad-mini-a17-pro",
                "--cid",
                "43",
                "--scale",
                "2",
            ]
        )
        == 0
    )
    assert calls[0][1:] == ("mini", "iPad16,1", "27.0.1", 43, 2)


@pytest.mark.parametrize("mode", ["compatible", "strict", "diagnostic"])
def test_create_accepts_explicit_graphics_mode(monkeypatch, tmp_path, mode):
    calls = []
    monkeypatch.setattr("limulator.creator.create_device", lambda *args, **kwargs: calls.append(kwargs) or {})

    assert cli.main(["--data", str(tmp_path), "create", "--graphics-mode", mode]) == 0
    assert calls[0]["graphics_mode"] == mode


def test_start_passes_explicit_graphics_mode(monkeypatch, tmp_path):
    runtime = Mock()
    runtime.start.return_value = {"state": "booting"}
    monkeypatch.setattr(cli.Registry, "get", lambda self, name: object())
    monkeypatch.setattr(cli, "Runtime", lambda paths, device: runtime)
    monkeypatch.setattr(cli, "DeviceService", lambda selected_runtime: object())

    assert cli.main(["--data", str(tmp_path), "start", "--graphics-mode", "diagnostic"]) == 0
    runtime.start.assert_called_once_with(window=False, wait_timeout=300, graphics_mode="diagnostic")


@pytest.mark.parametrize("override", [["--model", "iPhone19,4"], ["--ios", "27.1"], ["--scale", "3"]])
def test_create_rejects_contradictory_profile_before_creation(monkeypatch, tmp_path, override):
    monkeypatch.setattr(
        "limulator.creator.create_device", lambda *args, **kwargs: pytest.fail("creation must not run")
    )
    assert cli.main(["--data", str(tmp_path), "create", "--profile", "ipad-mini-a17-pro", *override]) == 1


def test_create_without_options_preserves_duo_default(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("limulator.creator.create_device", lambda *args, **kwargs: calls.append(args) or {})
    assert cli.main(["--data", str(tmp_path), "create"]) == 0
    assert calls[0][1:] == ("duo", "iPhone19,4", "27.1", None, None)


@pytest.mark.parametrize("options,expected", [([], False), (["--fresh-disk"], True)])
def test_fresh_disk_recovery_requires_an_explicit_cli_flag(monkeypatch, tmp_path, options, expected):
    calls = []

    def create(*args, fresh_disk=False):
        calls.append(fresh_disk)
        return {}

    monkeypatch.setattr("limulator.creator.create_device", create)
    assert cli.main(["--data", str(tmp_path), "create", *options]) == 0
    assert calls == [expected]


def test_fresh_disk_creator_refusal_is_a_cli_failure(monkeypatch, tmp_path, capsys):
    def create(*args, fresh_disk=False):
        assert fresh_disk is True
        raise ValueError("fresh disk refused: device is already registered")

    monkeypatch.setattr("limulator.creator.create_device", create)
    assert cli.main(["--data", str(tmp_path), "create", "--fresh-disk"]) == 1
    assert "fresh disk refused: device is already registered" in capsys.readouterr().err


@pytest.mark.parametrize("state", ["running", "stopped"])
@pytest.mark.parametrize("unlock_error", [None, "the device has a passcode; pass it as passcode"])
def test_run_unlocks_after_install_and_stops_before_launch_if_unlock_fails(
    monkeypatch, tmp_path, capsys, state, unlock_error
):
    workflow = Mock()
    ipa = tmp_path / "Example.ipa"
    workflow.build.return_value = ipa
    workflow.status.return_value = {"state": state}
    workflow.install.return_value = {"bundle_id": "com.example.Example"}
    workflow.launch.return_value = {"launched": True}
    if unlock_error:
        workflow.unlock.side_effect = cli.DeviceError(unlock_error)
    runtime = SimpleNamespace(status=workflow.status, start=workflow.start)
    service = SimpleNamespace(install=workflow.install, unlock=workflow.unlock, launch=workflow.launch)
    monkeypatch.setattr(cli.Registry, "get", lambda self, name: object())
    monkeypatch.setattr(cli, "Runtime", lambda paths, device: runtime)
    monkeypatch.setattr(cli, "DeviceService", lambda selected_runtime: service)
    monkeypatch.setattr(cli, "_build_project", workflow.build)

    result = cli.main(["--data", str(tmp_path), "run", str(tmp_path / "Example")])

    expected = [call.build(ANY, ANY), call.status()]
    if state != "running":
        expected.append(call.start(wait_timeout=300))
    expected.extend([call.install(ipa), call.unlock()])
    if unlock_error:
        assert result == 1
        assert unlock_error in capsys.readouterr().err
    else:
        expected.append(call.launch("com.example.Example"))
        assert result == 0
    assert workflow.mock_calls == expected


def test_run_rejects_explicit_graphics_mode_mismatch_when_reusing_running_guest(
    monkeypatch, tmp_path, capsys
):
    workflow = Mock()
    workflow.build.return_value = tmp_path / "Example.ipa"
    workflow.status.return_value = {"state": "running"}
    workflow.require_graphics_mode.side_effect = RuntimeError(
        "device 'duo' has graphics mode unknown; stop it and start it with strict"
    )
    runtime = SimpleNamespace(
        status=workflow.status,
        start=workflow.start,
        require_graphics_mode=workflow.require_graphics_mode,
    )
    monkeypatch.setattr(cli.Registry, "get", lambda self, name: object())
    monkeypatch.setattr(cli, "Runtime", lambda paths, device: runtime)
    monkeypatch.setattr(cli, "DeviceService", lambda selected_runtime: SimpleNamespace())
    monkeypatch.setattr(cli, "_build_project", workflow.build)

    result = cli.main(
        ["--data", str(tmp_path), "run", str(tmp_path / "Example"), "--graphics-mode", "strict"]
    )

    assert result == 1
    assert "graphics mode unknown" in capsys.readouterr().err
    workflow.build.assert_not_called()
    workflow.status.assert_not_called()
    workflow.start.assert_not_called()


def test_console_caches_each_named_devices_frame_without_polling_catalog(monkeypatch, tmp_path):
    devices = {
        name: SimpleNamespace(
            name=name, model=model, os_version=os_version, width=100, height=200, scale=3, cid=cid
        )
        for name, model, os_version, cid in [
            ("duo", "iPhone19,4", "27.1", 41),
            ("phone", "iPhone17,3", "27.0.1", 42),
        ]
    }

    class Registry:
        def __init__(self, paths):
            pass

        def get(self, name):
            if name not in devices:
                raise ValueError(f"unknown device: {name}")
            return devices[name]

        def list(self):
            return list(devices.values())

    monkeypatch.setattr(cli, "Registry", Registry)
    monkeypatch.setattr(cli, "Runtime", lambda paths, device: SimpleNamespace(device=device))
    console = cli._ConsoleService("duo", Paths(tmp_path))
    duo = console.for_device("duo")
    phone = console.for_device("phone")
    duo._frame = "duo-frame"
    phone._frame = "phone-frame"
    assert console.for_device("duo") is duo
    assert console.for_device("phone")._frame == "phone-frame"
    catalog = console.devices()
    assert [device["name"] for device in catalog["devices"]] == ["duo", "phone"]
    assert catalog["devices"][0]["supports_fold"] is True
    assert catalog["devices"][1]["supports_fold"] is False
    assert "main_firmware" not in catalog["profiles"][0]
