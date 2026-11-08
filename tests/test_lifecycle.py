import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace

import pytest

from limulator import lifecycle
from limulator.lifecycle import Device, Paths, Registry, Runtime


@pytest.fixture
def device(tmp_path):
    vm = tmp_path / "vm"
    vm.mkdir()
    (vm / "Disk.img").write_bytes(b"real-test-disk")
    (vm / "config.plist").write_bytes(
        plistlib.dumps({"platformType": "vresearch101", "diskImage": "Disk.img"})
    )
    return Device("phone", vm, "iPhone Duo", "26.1")


def test_paths_use_home_and_backend_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMULATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIMULATOR_BACKEND", str(tmp_path / "backend"))
    monkeypatch.delenv("LIMULATOR_PREFIX", raising=False)
    paths = Paths()
    assert paths.data == tmp_path / "home"
    assert paths.backend == tmp_path / "backend"
    assert paths.prefix == paths.data / "tools"


def test_graphics_modes_override_inherited_strict_and_census_flags(tmp_path):
    inherited = {
        "REIMS_VGPU_STRICT": "0",
        "REIMS_VGPU_CENSUS_DIR": "/inherited/census",
        "REIMS_VGPU_EXACT": "batch_order",
        "REIMS_VGPU_DIRTY_PAGE_TRACKING": "0",
    }

    strict = dict(inherited)
    lifecycle.apply_graphics_environment(strict, "strict", tmp_path / "strict-census")
    assert strict["REIMS_VGPU_STRICT"] == "1"
    assert strict["REIMS_VGPU_CENSUS_DIR"] == ""
    assert strict["REIMS_VGPU_EXACT"] == "batch_order"
    assert strict["REIMS_VGPU_DIRTY_PAGE_TRACKING"] == "0"

    compatible = dict(inherited)
    lifecycle.apply_graphics_environment(compatible, "compatible", tmp_path / "compatible-census")
    assert compatible["REIMS_VGPU_STRICT"] == "0"
    assert compatible["REIMS_VGPU_CENSUS_DIR"] == ""
    assert compatible["REIMS_VGPU_EXACT"] == "batch_order"
    assert compatible["REIMS_VGPU_DIRTY_PAGE_TRACKING"] == "0"

    diagnostic = dict(inherited)
    census = tmp_path / "diagnostic-census"
    lifecycle.apply_graphics_environment(diagnostic, "diagnostic", census)
    assert diagnostic["REIMS_VGPU_STRICT"] == "0"
    assert diagnostic["REIMS_VGPU_CENSUS_DIR"] == str(census)
    assert diagnostic["REIMS_VGPU_EXACT"] == "batch_order"
    assert diagnostic["REIMS_VGPU_DIRTY_PAGE_TRACKING"] == "0"


def test_graphics_mode_environment_selects_a_new_launch_mode(monkeypatch):
    monkeypatch.delenv("LIMULATOR_GRAPHICS_MODE", raising=False)
    assert lifecycle.select_graphics_mode(None) == ("compatible", False)

    monkeypatch.setenv("LIMULATOR_GRAPHICS_MODE", "compatible")
    assert lifecycle.select_graphics_mode(None) == ("compatible", True)

    monkeypatch.setenv("LIMULATOR_GRAPHICS_MODE", "diagnostic")
    assert lifecycle.select_graphics_mode(None) == ("diagnostic", True)

    monkeypatch.setenv("LIMULATOR_GRAPHICS_MODE", "metal")
    with pytest.raises(ValueError, match="graphics mode must be compatible, strict, or diagnostic"):
        lifecycle.select_graphics_mode(None)


def test_start_reports_mode_mismatch_for_legacy_active_session(device, tmp_path, monkeypatch):
    runtime = Runtime(Paths(tmp_path / "data"), device)
    runtime.runtime_path.parent.mkdir(parents=True)
    lifecycle._atomic_json(
        runtime.runtime_path,
        {
            "name": device.name,
            "process": {"pid": 123},
            "supervisor": {"pid": 123},
        },
    )
    monkeypatch.setattr(lifecycle, "_identity_alive", lambda identity: identity is not None)
    monkeypatch.setattr(runtime, "status", lambda: pytest.fail("an explicit mismatch must fail"))

    with pytest.raises(RuntimeError, match="graphics mode unknown.*Stop it.*start it"):
        runtime.start(graphics_mode="strict")


def test_status_marks_unrecorded_graphics_mode_unknown(device, tmp_path):
    runtime = Runtime(Paths(tmp_path / "data"), device)
    runtime.runtime_path.parent.mkdir(parents=True)
    process = lifecycle._identity(os.getpid())
    stale = {**process, "start_ticks": process["start_ticks"] + 1}
    lifecycle._atomic_json(
        runtime.runtime_path,
        {"name": device.name, "process": stale},
    )

    assert runtime.status()["graphics_policy"] == {
        "mode": "unknown",
        "reims_vgpu_exact": {"known": False},
    }


@pytest.mark.parametrize("name", ["", "../escape", "/absolute", ".", "a.b", "phone/other", "a\n", "é"])
def test_names_cannot_escape_registry(device, name):
    with pytest.raises(ValueError, match="device name"):
        replace(device, name=name)


@pytest.mark.parametrize("cid", [0, 1, 2, -1, True, 3.5, 0xFFFFFFFF])
def test_reserved_or_invalid_cids_are_rejected(device, cid):
    with pytest.raises(ValueError, match="CID"):
        replace(device, cid=cid)


def test_registry_roundtrip_and_duplicate_cid(device, tmp_path):
    registry = Registry(Paths(tmp_path / "data"))
    assert registry.register(device) == device
    assert registry.get("phone") == device
    assert registry.list() == [device]
    other_vm = tmp_path / "other"
    other_vm.mkdir()
    (other_vm / "Disk.img").write_bytes(b"other-test-disk")
    (other_vm / "config.plist").write_bytes(plistlib.dumps({}))
    with pytest.raises(ValueError, match="CID 41 is already registered"):
        registry.register(replace(device, name="other", vm_path=other_vm))


def test_registry_rejects_disk_alias(device, tmp_path):
    registry = Registry(Paths(tmp_path / "data"))
    registry.register(device)
    alias = tmp_path / "alias"
    alias.mkdir()
    os.link(device.vm_path / "Disk.img", alias / "Disk.img")
    (alias / "config.plist").write_bytes(plistlib.dumps({}))
    with pytest.raises(ValueError, match="disk is already registered"):
        registry.register(replace(device, name="alias", cid=42, vm_path=alias))


def test_registry_rejects_missing_disk_and_invalid_plist(device, tmp_path):
    registry = Registry(Paths(tmp_path / "data"))
    disk = device.vm_path / "Disk.img"
    disk.unlink()
    with pytest.raises(ValueError, match="nonempty regular Disk.img"):
        registry.register(device)
    disk.write_bytes(b"disk")
    (device.vm_path / "config.plist").write_bytes(b"not a plist")
    with pytest.raises(ValueError, match="invalid VM config.plist"):
        registry.register(device)
    (device.vm_path / "config.plist").write_bytes(b'<?xml version="1.0"?><plist><dict><bad>')
    with pytest.raises(ValueError, match="invalid VM config.plist"):
        registry.register(device)


def test_registry_rejects_tampered_filename_and_shape(device, tmp_path):
    registry = Registry(Paths(tmp_path / "data"))
    registry.register(device)
    path = registry.paths.registry / "phone.json"
    value = json.loads(path.read_text())
    value["name"] = "other"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="name does not match"):
        registry.get("phone")
    path.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        registry.get("phone")


def test_lock_protects_same_inode_through_hardlink(device, tmp_path):
    disk = device.vm_path / "Disk.img"
    alias = tmp_path / "disk-alias"
    os.link(disk, alias)
    owned = lifecycle._lock_disk(disk)
    try:
        with pytest.raises(RuntimeError, match="already owned"):
            lifecycle._lock_disk(alias)
    finally:
        os.close(owned)
    released = lifecycle._lock_disk(alias)
    os.close(released)


def test_uncooperative_external_disk_opener_is_detected(device):
    disk = device.vm_path / "Disk.img"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; f=open(sys.argv[1],'rb'); print('ready',flush=True); time.sleep(20)",
            str(disk),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="already open by process IDs"):
            lifecycle._lock_disk(disk)
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_process_identity_rejects_reused_pid():
    actual = lifecycle._identity(os.getpid())
    assert lifecycle._identity_alive(actual)
    assert not lifecycle._identity_alive({**actual, "start_ticks": actual["start_ticks"] + 1})
    assert not lifecycle._identity_alive({**actual, "boot_id": "previous-host-boot"})


def test_stale_pid_does_not_probe_or_stop_an_unrelated_process(device, tmp_path, monkeypatch):
    runtime = Runtime(Paths(tmp_path / "data"), device)
    runtime.runtime_path.parent.mkdir(parents=True)
    actual = lifecycle._identity(os.getpid())
    stale = {**actual, "start_ticks": actual["start_ticks"] + 1}
    lifecycle._atomic_json(runtime.runtime_path, {"name": device.name, "process": stale, "supervisor": stale})
    monkeypatch.setattr(
        runtime, "guest", lambda **kwargs: pytest.fail("stale identity must not access guest")
    )
    assert runtime.status()["state"] == "stopped"
    assert runtime.stop(timeout=0)["state"] == "stopped"
    assert lifecycle._identity_alive(actual)


def test_running_requires_successful_live_guest_health(device, tmp_path, monkeypatch):
    runtime = Runtime(Paths(tmp_path / "data"), device)
    runtime.runtime_path.parent.mkdir(parents=True)
    lifecycle._atomic_json(
        runtime.runtime_path, {"name": device.name, "process": lifecycle._identity(os.getpid())}
    )

    class FakeGuest:
        body = b'{"status":"ok"}'

        def request(self, method, path):
            assert (method, path) == ("GET", "/v1/health")
            return self.body

    guest = FakeGuest()
    monkeypatch.setattr(runtime, "guest", lambda **kwargs: guest)
    assert runtime.status()["state"] == "running"
    assert runtime.status()["graphics_policy"]["mode"] == "unknown"
    guest.body = b'{"status":"starting"}'
    assert runtime.status()["state"] == "booting"
    guest.body = b"invalid json"
    assert runtime.status()["state"] == "booting"


def test_offline_guest_and_qmp_refuse_connection(device, tmp_path):
    runtime = Runtime(Paths(tmp_path / "data"), device)
    with pytest.raises(RuntimeError, match="no live owned QEMU"):
        runtime.guest()
    with pytest.raises(RuntimeError, match="no live owned QEMU"):
        runtime.qmp()


def test_failed_supervisor_persists_error_and_releases_ownership(device, tmp_path, monkeypatch):
    paths = Paths(tmp_path / "data")
    Registry(paths).register(device)
    runtime = Runtime(paths, device)
    run_dir = runtime.runtime_path.parent / "run-test"
    run_dir.mkdir(parents=True)
    backend = tmp_path / "backend"
    backend.mkdir()
    monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake = fakebin / "systemd-run"
    fake.write_text("#!/bin/sh\nexit 27\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ["PATH"])
    fd = lifecycle._lock_disk(device.vm_path / "Disk.img")
    args = lifecycle.argparse.Namespace(
        data=str(paths.data), name=device.name, run_dir=str(run_dir), lock_fd=fd, window=False
    )
    assert lifecycle._supervise(args) == 1
    assert runtime.status()["state"] == "error"
    assert "status 27" in runtime.status()["error"]
    unlocked = lifecycle._lock_disk(device.vm_path / "Disk.img")
    os.close(unlocked)


def test_resource_guard_rejects_low_memory_without_starting_workload(monkeypatch):
    read_text = lifecycle.Path.read_text

    def memory(path, *args, **kwargs):
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 1024 kB\nSwapFree: 999999999 kB\n"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle.Path, "read_text", memory)
    with pytest.raises(RuntimeError, match="MemAvailable >= 32 GiB"):
        lifecycle._resource_guard()


@pytest.mark.parametrize("window", [False, True])
@pytest.mark.parametrize("graphics_mode", ["compatible", "strict", "diagnostic"])
def test_detached_supervisor_retains_disk_lock_and_stops_exact_qmp_peer(
    device, monkeypatch, window, graphics_mode
):
    monkeypatch.delenv("LIMULATOR_PREFIX", raising=False)
    # Run the real supervisor against a tiny QMP peer with a QEMU-named executable.
    # The fake scope launcher consumes no guest RAM and does not contact systemd.
    with tempfile.TemporaryDirectory(prefix="lim-life-") as temporary:
        paths = Paths(lifecycle.Path(temporary))
        backend = paths.data / "backend"
        binary = backend / "qemu/build/qemu-system-aarch64"
        binary.parent.mkdir(parents=True)
        shutil.copy2(lifecycle.Path(sys.executable).resolve(), binary)
        script = backend / "run-vphone.sh"
        # Keep the backend's unset-versus-empty census default in this fake launcher.
        script.write_text(
            "#!/bin/sh\n"
            "export REIMS_VGPU_CENSUS_DIR=${REIMS_VGPU_CENSUS_DIR-$PWD/runs/census}\n"
            'if [ -n "$REIMS_VGPU_CENSUS_DIR" ]; then mkdir -p "$REIMS_VGPU_CENSUS_DIR"; '
            "else unset REIMS_VGPU_CENSUS_DIR; fi\n"
            'export PYTHONHOME="$FAKE_PYTHON_HOME"\n'
            'exec "$QEMU" -c "$FAKE_QEMU_PROGRAM" "$@"\n'
        )
        script.chmod(0o700)
        fakebin = paths.prefix / "bin"
        fakebin.mkdir(parents=True)
        scope = fakebin / "systemd-run"
        scope.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_SCOPE_CAPTURE"\n'
            'while [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n'
        )
        scope.chmod(0o700)
        program = """
import json,os,socket,sys,time
qmp = sys.argv[sys.argv.index('-qmp') + 1].split(',')[0].removeprefix('unix:')
environment = {key: os.environ.get(key) for key in (
    'WINDOW','TOOLBAR','REIMS_VGPU_WINDOW_CONTROL','REIMS_VGPU_STRICT',
    'REIMS_VGPU_CENSUS_DIR','REIMS_VGPU_EXACT')}
with open(os.environ['FAKE_ENV_CAPTURE'], 'w') as f:
    json.dump(environment, f)
time.sleep(0.5)
server = socket.socket(socket.AF_UNIX)
server.bind(qmp)
server.listen()
connection,_ = server.accept()
stream = connection.makefile('rwb',buffering=0)
stream.write(b'{"QMP":{"version":{},"capabilities":[]}}\\n')
for line in stream:
    request=json.loads(line)
    stream.write((json.dumps({'return':{},'id':request['id']})+'\\n').encode())
    if request['execute']=='quit': break
connection.close()
server.close()
        """
        monkeypatch.setenv("FAKE_QEMU_PROGRAM", program)
        env_capture = paths.data / "qemu-env.json"
        monkeypatch.setenv("FAKE_ENV_CAPTURE", str(env_capture))
        scope_capture = paths.data / "scope-args.txt"
        monkeypatch.setenv("FAKE_SCOPE_CAPTURE", str(scope_capture))
        monkeypatch.setenv("FAKE_PYTHON_HOME", sys.base_prefix)
        monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
        monkeypatch.setenv("REIMS_VGPU_STRICT", "0" if graphics_mode == "strict" else "1")
        monkeypatch.setenv("REIMS_VGPU_CENSUS_DIR", "/inherited/census")
        monkeypatch.setenv("REIMS_VGPU_EXACT", "pipeline_waits")
        isolated = replace(device, cid=500001)
        Registry(paths).register(isolated)
        runtime = Runtime(paths, isolated)
        command = (
            "from pathlib import Path; from limulator.lifecycle import Paths,Registry,Runtime; "
            "import limulator.lifecycle as life; life._resource_guard=lambda:None; "
            f"p=Paths(Path({str(paths.data)!r})); Runtime(p,Registry(p).get('phone')).start("
            f"window={window!r},graphics_mode={graphics_mode!r})"
        )
        subprocess.run([sys.executable, "-c", command], check=True, timeout=15)
        # The starting CLI has exited. Its independent worker must retain ownership.
        try:
            deadline = time.monotonic() + 5
            while not (
                lifecycle._identity_alive(runtime._metadata().get("process")) and runtime.qmp_path.is_socket()
            ):
                assert runtime._metadata().get("state") != "error", (
                    lifecycle.Path(runtime._metadata()["run_dir"]) / "qemu.log"
                ).read_text()
                assert time.monotonic() < deadline, runtime._metadata()
                time.sleep(0.05)
            actual_env = json.loads(env_capture.read_text())
            assert actual_env["WINDOW"] == ("1" if window else "0")
            assert actual_env["TOOLBAR"] == ("1" if window else "0")
            assert actual_env["REIMS_VGPU_WINDOW_CONTROL"] == (
                f"{runtime._metadata()['run_dir']}/window.sock"
            )
            assert actual_env["REIMS_VGPU_STRICT"] == ("1" if graphics_mode == "strict" else "0")
            if graphics_mode in {"compatible", "strict"}:
                assert actual_env["REIMS_VGPU_CENSUS_DIR"] is None
            else:
                census_dir = f"{runtime._metadata()['run_dir']}/census"
                assert actual_env["REIMS_VGPU_CENSUS_DIR"] == census_dir
            assert actual_env["REIMS_VGPU_EXACT"] == "pipeline_waits"
            assert runtime.status()["graphics_policy"] == {
                "mode": graphics_mode,
                "reims_vgpu_exact": {"known": True, "set": True, "value": "pipeline_waits"},
            }
            scope_args = scope_capture.read_text().splitlines()
            assert "MemoryHigh=32G" in scope_args
            assert "MemoryMax=40G" in scope_args
            assert "MemorySwapMax=0" in scope_args
            assert lifecycle._identity_alive(runtime._metadata()["supervisor"])
            with pytest.raises(RuntimeError, match="already owned"):
                lifecycle._lock_disk(isolated.vm_path / "Disk.img")
            assert runtime.qmp_path.is_socket()
            result = runtime.stop(timeout=0)
            assert result["state"] == "stopped"
            assert result["forced"] is True
            assert not lifecycle._identity_alive(runtime._metadata()["process"])
            released = lifecycle._lock_disk(isolated.vm_path / "Disk.img")
            os.close(released)
        finally:
            owned = runtime._metadata().get("process")
            if lifecycle._identity_alive(owned):
                os.kill(owned["pid"], 15)
