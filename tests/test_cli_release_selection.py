import os
import platform
import subprocess
from types import SimpleNamespace

import pytest

from limulator import cli, creator
from limulator.lifecycle import Device, Paths
from limulator.profiles import get_profile

ROOT = os.path.dirname(os.path.dirname(__file__))


def test_creation_preflight_requires_release_product_instead_of_wrapper(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    paths = Paths(tmp_path / "data")
    monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
    wrapper = backend / "linux-create/vphone-cli"
    wrapper.parent.mkdir(parents=True)
    marker = tmp_path / "wrapper-ran"
    wrapper.write_text(f"#!/bin/sh\nprintf called > '{marker}'\n")
    wrapper.chmod(0o755)
    options = backend / "qemu/build/meson-info/intro-buildoptions.json"
    options.parent.mkdir(parents=True)
    options.write_text('[{"name":"pvg","value":"enabled"}]')

    profile = get_profile("iphone-duo")
    candidate = Device("release-check", paths.vms / "release-check", profile.model, profile.os_version)
    monkeypatch.setattr(creator, "_resource_guard", lambda: None)
    expected = (
        backend
        / f"linux-create/.build/out/out/Products/Release-linux-{creator.platform.machine()}/vphone-cli"
    )

    with pytest.raises(RuntimeError, match="required native tool is not built") as error:
        creator._create_locked(paths, candidate, profile, {})

    assert str(expected) in str(error.value)
    assert not marker.exists()


def _executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _doctor_paths(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    prefix = tmp_path / "tools"
    monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
    monkeypatch.setenv("LIMULATOR_PREFIX", str(prefix))
    paths = Paths(tmp_path / "data")
    wrapper = backend / "linux-create/vphone-cli"
    wrapper_marker = tmp_path / "wrapper-ran"
    _executable(
        wrapper,
        f"#!/bin/sh\nprintf stale-wrapper > '{wrapper_marker}'\nprintf 'vphone stale wrapper\\n'\n",
    )
    release = backend / f"linux-create/.build/out/out/Products/Release-linux-{platform.machine()}/vphone-cli"
    return paths, wrapper_marker, release


def _stub_doctor_host_probes(monkeypatch):
    original_run = subprocess.run

    def run(command, *args, **kwargs):
        if command == ["systemctl", "--user", "show-environment"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)


def _stub_ready_doctor_host(monkeypatch):
    original_run = subprocess.run
    original_access = os.access

    def run(command, *args, **kwargs):
        if command in (["systemctl", "--user", "show-environment"], ["vulkaninfo", "--summary"]):
            return subprocess.CompletedProcess(command, 0, "Vulkan Instance Version: 1.4\n", "")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/usr/bin/vulkaninfo" if name == "vulkaninfo" else None,
    )
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        cli.os,
        "access",
        lambda path, mode: True if os.fspath(path) == "/dev/vhost-vsock" else original_access(path, mode),
    )
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: SimpleNamespace(free=100 << 30))


def test_doctor_reports_missing_release_product_without_running_stale_wrapper(tmp_path, monkeypatch):
    paths, wrapper_marker, release = _doctor_paths(tmp_path, monkeypatch)
    _stub_doctor_host_probes(monkeypatch)

    result = cli.doctor(paths)

    assert result["tools"]["firmware_cli"] == release
    assert result["checks"]["firmware_cli"] is False
    assert result["checks"]["firmware_cli_runs"] is False
    assert not wrapper_marker.exists()


def test_doctor_runs_the_actual_release_product(tmp_path, monkeypatch):
    paths, wrapper_marker, release = _doctor_paths(tmp_path, monkeypatch)
    release_marker = tmp_path / "release-ran"
    _executable(
        release,
        "#!/bin/sh\n"
        'printf release > "$LIMULATOR_DOCTOR_RELEASE_MARKER"\n'
        "printf 'vphone release product\\n'\n",
    )
    monkeypatch.setenv("LIMULATOR_DOCTOR_RELEASE_MARKER", str(release_marker))
    _stub_doctor_host_probes(monkeypatch)

    result = cli.doctor(paths)

    assert result["tools"]["firmware_cli"] == release
    assert result["checks"]["firmware_cli"] is True
    assert result["checks"]["firmware_cli_runs"] is True
    assert release_marker.read_text() == "release"
    assert not wrapper_marker.exists()


def test_doctor_requires_a_nonempty_qemu_rom_in_its_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.platform, "machine", lambda: "x86_64")
    paths, _wrapper_marker, release = _doctor_paths(tmp_path, monkeypatch)
    qemu = paths.backend / "qemu/build/qemu-system-aarch64"
    rom_dir = paths.backend / "qemu/build/qemu-bundle/usr/local/share/qemu"
    _executable(
        qemu,
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "-L help") printf "%s\\n" "$LIMULATOR_DOCTOR_QEMU_DATA_DIR" ;;\n'
        '  "-machine help") printf "vresearch101\\n" ;;\n'
        '  "-device apple-gfx-mmio,help") printf "apple-gfx-mmio\\n" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n",
    )
    _executable(release, "#!/bin/sh\nprintf 'vphone release product\\n'\n")
    usbmuxd = paths.backend / ".build/usbmuxd-32k/usbmuxd"
    apfs = paths.backend / ".build/linux-apfs-rw/apfs.ko"
    usbmuxd.parent.mkdir(parents=True)
    apfs.parent.mkdir(parents=True)
    usbmuxd.touch()
    apfs.touch()
    monkeypatch.setenv("LIMULATOR_DOCTOR_QEMU_DATA_DIR", str(rom_dir))
    _stub_ready_doctor_host(monkeypatch)

    missing = cli.doctor(paths)
    assert missing["ready"] is False
    assert missing["checks"]["qemu_romfile"] is False
    assert missing["tools"]["qemu_romfile"] is None

    rom_dir.mkdir(parents=True)
    rom = rom_dir / "efi-virtio.rom"
    rom.touch()
    empty = cli.doctor(paths)
    assert empty["ready"] is False
    assert empty["checks"]["qemu_romfile"] is False

    rom.write_bytes(b"firmware rom")
    complete = cli.doctor(paths)
    assert complete["ready"] is True
    assert complete["checks"]["qemu_romfile"] is True
    assert complete["tools"]["qemu_romfile"] == rom


def test_backend_cli_bootstrap_requests_release_configuration(tmp_path):
    home = tmp_path / "home"
    backend = tmp_path / "backend"
    prefix = tmp_path / "tools"
    source = backend / "cpu-tree/vphone-cli-2.14.2"
    source.mkdir(parents=True)
    (source / ".git").mkdir()
    build = backend / "linux-create/build.sh"
    args_file = tmp_path / "build-arguments"
    source_file = tmp_path / "build-source"
    _executable(
        build,
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$LIMULATOR_TEST_BUILD_ARGS"\n'
        'printf "%s\\n" "$VPHONE_CLI" > "$LIMULATOR_TEST_BUILD_SOURCE"\n',
    )
    bin_dir = prefix / "bin"
    _executable(
        bin_dir / "jq",
        "#!/usr/bin/env bash\n"
        'case "${2:-}" in\n'
        '  .vphone_cli.version) printf "2.14.2\\n" ;;\n'
        '  .vphone_cli.repository) printf "https://example.invalid/vphone-cli.git\\n" ;;\n'
        '  .vphone_cli.revision) printf "83197152137ddd2aad70eb121d6b88a9afbee36e\\n" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n",
    )
    _executable(
        bin_dir / "git",
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"rev-parse HEAD"* ]]; then\n'
        '  printf "83197152137ddd2aad70eb121d6b88a9afbee36e\\n"\n'
        "else\n"
        "  exit 3\n"
        "fi\n",
    )
    _executable(bin_dir / "uv", "#!/usr/bin/env bash\nexit 0\n")
    _executable(
        bin_dir / "awk",
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *MemAvailable*) printf "64000000\\n" ;;\n'
        '  *SwapFree*) printf "8000000\\n" ;;\n'
        '  *) exec /usr/bin/awk "$@" ;;\n'
        "esac\n",
    )
    _executable(
        bin_dir / "systemd-run",
        "#!/usr/bin/env bash\n"
        'while [[ $# -gt 0 && "$1" != -- ]]; do shift; done\n'
        "[[ $# -gt 0 ]] || exit 4\n"
        "shift\n"
        'exec "$@"\n',
    )
    env = os.environ | {
        "HOME": str(home),
        "LIMULATOR_HOME": str(home / ".local/share/limulator"),
        "LIMULATOR_CACHE": str(home / ".cache/limulator"),
        "LIMULATOR_BACKEND": str(backend),
        "LIMULATOR_PREFIX": str(prefix),
        "LIMULATOR_JOBS": "2",
        "LIMULATOR_TEST_BUILD_ARGS": str(args_file),
        "LIMULATOR_TEST_BUILD_SOURCE": str(source_file),
    }

    result = subprocess.run(
        ["bash", os.path.join(ROOT, "tools/bootstrap_backend.sh"), "cli"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    arguments = args_file.read_text().splitlines()
    assert arguments[:2] == ["--configuration", "release"]
    assert arguments[arguments.index("--jobs") + 1] == "2"
    assert source_file.read_text().strip() == str(source)
