import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from limulator import creator
from limulator import release_resources as resources
from limulator.lifecycle import Device, Paths
from limulator.profiles import get_profile

ROOT = Path(__file__).resolve().parents[1]


def archive_bytes(payload=b"worker"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, contents in {
            "OpenAVB/OpenAVB.bin": b"avb",
            "OpenSEP/OpenSEP.bin": b"sep",
            "guest-resources/vphoned": payload,
            "guest-resources/vphoned.plist": b"plist",
        }.items():
            archive.writestr("VPhone.bundle/Contents/Resources/" + name, contents)
    return output.getvalue()


def cohort(tmp_path, payload=b"worker"):
    data, backend = tmp_path / "data", tmp_path / "backend"
    archive = archive_bytes(payload)
    spec = {
        "version": "9.9.0",
        "url": "https://example.invalid/VPhone-9.9.0.zip",
        "sha256": hashlib.sha256(archive).hexdigest(),
    }
    folder = resources.release_directory(data, spec)
    folder.mkdir(parents=True)
    (folder / "VPhone-9.9.0.zip").write_bytes(archive)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        source.extractall(folder / "app")
    guest = folder / "app/VPhone.bundle/Contents/Resources/guest-resources"
    backend.mkdir(exist_ok=True)
    (backend / "guest-resources").symlink_to(guest, target_is_directory=True)
    for name in ("OpenAVB", "OpenSEP"):
        (backend / name).mkdir()
        shutil.copyfile(guest.parent / name / f"{name}.bin", backend / name / f"{name}.bin")
    resources.record_release(data, backend, spec)
    return data, backend, spec, folder


def test_release_receipt_binds_the_archive_payloads_and_link(tmp_path):
    data, backend, spec, folder = cohort(tmp_path)
    proof = resources.verify_release(data, backend, spec)
    assert proof["release"] == spec
    assert proof["archive"]["sha256"] == spec["sha256"]
    assert set(proof["payloads"]) == {
        "OpenAVB/OpenAVB.bin",
        "OpenSEP/OpenSEP.bin",
        "guest-resources/vphoned",
        "guest-resources/vphoned.plist",
    }
    assert proof["backend"]["guest_link"]["target"].startswith(str(folder))


def test_fingerprint_refuses_a_fifo_before_opening_it(tmp_path, monkeypatch):
    fifo = tmp_path / "payload"
    os.mkfifo(fifo)

    def refuse_open(*_args, **_kwargs):
        pytest.fail("nonregular resource reached open")

    monkeypatch.setattr(resources.os, "open", refuse_open)
    with pytest.raises(resources.ReleaseError, match="single-link file"):
        resources._fingerprint(fifo)


def test_fingerprint_refuses_a_regular_file_swapped_to_fifo_at_open(tmp_path, monkeypatch):
    target = tmp_path / "payload"
    target.write_bytes(b"original")
    original_open = os.open

    def swapped_open(filename, flags, *args, **kwargs):
        assert Path(filename) == target
        target.unlink()
        os.mkfifo(target)
        assert flags & os.O_NONBLOCK, "open-time type replacement could block"
        return original_open(filename, flags, *args, **kwargs)

    monkeypatch.setattr(resources.os, "open", swapped_open)
    with pytest.raises(resources.ReleaseError, match="changed|single-link file"):
        resources._fingerprint(target)


@pytest.mark.parametrize("initialize", [False, True])
def test_creator_host_lock_keeps_its_local_lock_and_sudo_hooks(tmp_path, monkeypatch, initialize):
    lock = tmp_path / "host.lock"
    lock.touch(mode=0o644)
    original_lock = resources.lock_transport_file
    calls = []
    environment = {"fixture": "environment"}

    def local_lock(filename, owner):
        assert filename == creator._HOST_CREATION_LOCK and owner == 0
        calls.append("lock")
        if initialize and calls == ["lock"]:
            raise FileNotFoundError
        return original_lock(lock, os.getuid())

    def initialize_lock(command, env):
        assert command[:2] == ["/usr/bin/python3", "-c"]
        assert command[-1] == str(creator._HOST_CREATION_LOCK)
        assert env is environment
        calls.append("initialize")

    def forbidden_lock(*_args):
        pytest.fail("creator bypassed its disposable local lock hook")

    monkeypatch.setattr(resources, "lock_transport_file", forbidden_lock)
    monkeypatch.setattr(creator, "_lock_transport_file", local_lock)
    monkeypatch.setattr(creator, "_sudo", initialize_lock)
    descriptor = creator._host_creation_lock(environment)
    os.close(descriptor)
    assert calls == (["lock", "initialize", "lock"] if initialize else ["lock"])


@pytest.mark.parametrize(
    "change",
    ["archive", "cache", "rom", "link", "same-bytes-new-inode", "receipt", "invalid-receipt"],
)
def test_release_verification_refuses_stale_or_replaced_resources(tmp_path, change):
    data, backend, spec, folder = cohort(tmp_path)
    worker = folder / "app/VPhone.bundle/Contents/Resources/guest-resources/vphoned"
    if change == "archive":
        (folder / "VPhone-9.9.0.zip").write_bytes(b"another archive")
    elif change == "cache":
        worker.write_bytes(b"stale complete cached bundle")
    elif change == "rom":
        (backend / "OpenAVB/OpenAVB.bin").write_bytes(b"another ROM")
    elif change == "link":
        (backend / "guest-resources").unlink()
        outside = tmp_path / "outside-resources"
        outside.mkdir()
        (backend / "guest-resources").symlink_to(outside, target_is_directory=True)
    elif change == "same-bytes-new-inode":
        contents = worker.read_bytes()
        replacement = worker.with_suffix(".replacement")
        replacement.write_bytes(contents)
        replacement.replace(worker)
    elif change == "receipt":
        (folder / resources.RECEIPT).unlink()
    else:
        (folder / resources.RECEIPT).write_text("invalid JSON")
    with pytest.raises(resources.ReleaseError, match="bootstrap release"):
        resources.verify_release(data, backend, spec)


@pytest.mark.parametrize("linked", ["cohort", "partial-archive", "cached-bundle"])
def test_release_preparation_refuses_linked_paths_without_changing_the_target(tmp_path, linked):
    data, backend, spec, folder = cohort(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_bytes(b"preserved")
    if linked == "cohort":
        preserved = folder.with_name("preserved")
        folder.rename(preserved)
        folder.symlink_to(outside, target_is_directory=True)
    elif linked == "partial-archive":
        (folder / "VPhone-9.9.0.zip.partial").symlink_to(sentinel)
    else:
        bundle = folder / "app/VPhone.bundle"
        bundle.rename(bundle.with_name("preserved.bundle"))
        bundle.symlink_to(outside, target_is_directory=True)
    with pytest.raises(resources.ReleaseError, match="bootstrap release"):
        if linked == "cached-bundle":
            resources.prepare_release(data, backend, spec)
        else:
            resources._download_directory(data, spec)
    assert sentinel.read_bytes() == b"preserved"


@pytest.mark.parametrize("change", ["missing-receipt", "worker", "resource-link"])
def test_creation_refuses_stale_resources_before_entering_pipeline(tmp_path, monkeypatch, change):
    data, backend, spec, folder = cohort(tmp_path)
    monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
    monkeypatch.setattr(creator, "release_spec", lambda: spec)
    monkeypatch.setattr(resources, "release_spec", lambda: spec)
    monkeypatch.setattr(creator, "_resource_guard", lambda: None)
    monkeypatch.setattr(creator.os, "access", lambda *_args: True)
    options = backend / "qemu/build/meson-info/intro-buildoptions.json"
    options.parent.mkdir(parents=True)
    options.write_text('[{"name":"pvg","value":"enabled"}]')
    if change == "missing-receipt":
        (folder / resources.RECEIPT).unlink()
    elif change == "worker":
        (folder / "app/VPhone.bundle/Contents/Resources/guest-resources/vphoned").write_bytes(b"stale")
    else:
        link = backend / "guest-resources"
        link.unlink()
        link.symlink_to(backend, target_is_directory=True)
    monkeypatch.setattr(creator, "_pipeline", lambda *_args, **_kwargs: pytest.fail("creation started"))
    paths = Paths(data)
    profile = get_profile("iphone-duo")
    candidate = Device("fixture", paths.vms / "fixture", profile.model, profile.os_version)
    with pytest.raises(resources.ReleaseError, match="bootstrap release"):
        creator._create_locked(paths, candidate, profile, {})
    assert not candidate.vm_path.exists()
    assert creator._native_env(paths)["VSETUP_RELEASE"] == str(folder)
    assert creator._native_env(paths)["VPHONE_CLI_PUBLIC"] == spec["version"]


@pytest.mark.parametrize("candidate", ["legacy", "cohort", "firmware-cache", "explicit", "empty"])
def test_native_environment_preserves_cached_gpu_driver_priority(tmp_path, monkeypatch, candidate):
    data, backend, spec, folder = cohort(tmp_path)
    monkeypatch.setenv("LIMULATOR_BACKEND", str(backend))
    monkeypatch.setenv("VSETUP_CACHE", str(tmp_path / "firmware-cache"))
    monkeypatch.delenv("GPU_DRIVER_BUNDLE", raising=False)
    monkeypatch.setattr(creator, "release_spec", lambda: spec)
    name = "AppleParavirtGPUMetalIOGPUFamily.bundle"
    legacy = data / "releases/vphone" / name
    legacy.mkdir()
    if candidate == "cohort":
        (folder / name).mkdir()
    elif candidate == "firmware-cache":
        (tmp_path / "firmware-cache" / name).mkdir(parents=True)
    elif candidate in ("explicit", "empty"):
        monkeypatch.setenv("GPU_DRIVER_BUNDLE", "/selected/driver" if candidate == "explicit" else "")
    environment = creator._native_env(Paths(data))
    if candidate == "legacy":
        assert environment["GPU_DRIVER_BUNDLE"] == str(legacy)
    elif candidate in ("cohort", "firmware-cache"):
        assert "GPU_DRIVER_BUNDLE" not in environment
    else:
        assert environment["GPU_DRIVER_BUNDLE"] == ("/selected/driver" if candidate == "explicit" else "")


@pytest.mark.parametrize("busy", ["data", "host", None])
def test_install_holds_creation_locks_through_setup_and_receipt(tmp_path, monkeypatch, busy):
    data, backend, spec, folder = cohort(tmp_path)
    data_lock, host_lock = data / ".creation.lock", tmp_path / "host.lock"
    for lock in (data_lock, host_lock):
        lock.touch(mode=0o600)
    monkeypatch.setattr(
        resources,
        "host_creation_lock",
        lambda *_args: resources.lock_transport_file(host_lock, os.getuid()),
    )
    calls = []

    def setup(command, *, env, check, pass_fds):
        assert len(pass_fds) == 2
        assert {os.fstat(fd).st_ino for fd in pass_fds} == {
            data_lock.stat().st_ino,
            host_lock.stat().st_ino,
        }
        for lock in (data_lock, host_lock):
            with pytest.raises(RuntimeError, match="shared restore transport"):
                resources.lock_transport_file(lock, os.getuid())
        assert env["VSETUP_SRC"] == str(backend)
        assert env["VSETUP_RELEASE"] == str(folder)
        assert env["VPHONE_CLI_PUBLIC"] == spec["version"]
        calls.append(command)

    monkeypatch.setattr(resources.subprocess, "run", setup)
    before = (folder / resources.RECEIPT).read_bytes()
    if busy is not None:
        lock = data_lock if busy == "data" else host_lock
        with lock.open() as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="shared restore transport"):
                resources.install_release(data, backend, spec)
        assert not calls
        assert (folder / resources.RECEIPT).read_bytes() == before
    else:
        resources.install_release(data, backend, spec)
        assert calls == [[str(backend / "setup/setup.sh"), "release"]]
        resources.verify_release(data, backend, spec)
    for lock in (data_lock, host_lock):
        os.close(resources.lock_transport_file(lock, os.getuid()))


@pytest.mark.parametrize("installed", [False, True], ids=["source", "installed"])
def test_bootstrap_selects_locked_version_and_preserves_legacy_cache(tmp_path, installed):
    if not shutil.which("uv") or not shutil.which("jq") or not shutil.which("unzip"):
        pytest.skip("bootstrap fixture requires uv, jq and unzip")
    package, backend, data = tmp_path / "package", tmp_path / "backend", tmp_path / "data"
    tools = package / ("_tools" if installed else "tools")
    tools.mkdir(parents=True)
    for name in ("bootstrap_backend.sh", "backend_env.sh"):
        source_script = Path(os.environ.get("LIMULATOR_TEST_BOOTSTRAP_SCRIPT", ROOT / "tools" / name))
        shutil.copyfile(
            source_script if name == "bootstrap_backend.sh" else ROOT / "tools" / name, tools / name
        )
    helper = package / ("release_resources.py" if installed else "src/limulator/release_resources.py")
    helper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "src/limulator/release_resources.py", helper)
    source = tmp_path / "public-release.zip"
    source.write_bytes(archive_bytes())
    spec = {
        "version": "9.9.0",
        "url": source.as_uri(),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    (package / "third_party.lock.json").write_text(json.dumps({"release": spec}))
    legacy = data / "releases/vphone/app/VPhone.bundle/legacy"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"preserve this older cohort")
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    uv = prefix / "bin/uv"
    uv.write_text(
        '#!/bin/sh\ncase "$*" in *sync_backend_sources.py*) exit 0 ;; esac\n'
        'case "$*" in *release_resources.py*) '
        f'exec "{shutil.which("uv")}" run --script "$TEST_RELEASE_RUNNER" "$@" ;; esac\n'
        f'exec "{shutil.which("uv")}" "$@"\n'
    )
    uv.chmod(0o755)
    host_lock = tmp_path / "host.lock"
    host_lock.touch(mode=0o600)
    runner = tmp_path / "release-fixture.py"
    runner.write_text("""# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import os
import runpy
import sys
from pathlib import Path
helper = sys.argv[3]
arguments = sys.argv[4:]
namespace = runpy.run_path(helper, run_name="release_fixture")
context = namespace["main"].__globals__
context["host_creation_lock"] = lambda *_args: context["lock_transport_file"](
    Path(os.environ["TEST_HOST_LOCK"]), os.getuid()
)
sys.argv = [helper, *arguments]
namespace["main"]()
""")
    setup = backend / "setup/setup.sh"
    setup.parent.mkdir(parents=True)
    setup.write_text("""#!/bin/bash
set -euo pipefail
version=${VPHONE_CLI_PUBLIC:-2.14.2}
printf '%s\\n' "$version" > "$VERSION_LOG"
target="$VSETUP_RELEASE/app/VPhone.bundle"
if [[ ! -d $target ]]; then
  mkdir -p "$VSETUP_RELEASE/app"
  unzip -q "$VSETUP_RELEASE/VPhone-$version.zip" -d "$VSETUP_RELEASE/app"
fi
resources="$target/Contents/Resources"
ln -sfn "$resources/guest-resources" "$VSETUP_SRC/guest-resources"
for rom in OpenAVB OpenSEP; do
  mkdir -p "$VSETUP_SRC/$rom"
  cp "$resources/$rom/$rom.bin" "$VSETUP_SRC/$rom/$rom.bin"
done
""")
    setup.chmod(0o755)
    log = tmp_path / "version"
    environment = os.environ | {
        "LIMULATOR_HOME": str(data),
        "LIMULATOR_CACHE": str(tmp_path / "cache"),
        "LIMULATOR_BACKEND": str(backend),
        "LIMULATOR_PREFIX": str(prefix),
        "LIMULATOR_JOBS": "2",
        "VERSION_LOG": str(log),
        "TEST_RELEASE_RUNNER": str(runner),
        "TEST_HOST_LOCK": str(host_lock),
        "VSETUP_SRC": str(backend),
        "VSETUP_RELEASE": str(data / "releases/vphone"),
        "VSETUP_DEVICES": str(data / "vms"),
    }
    environment.pop("VPHONE_CLI_PUBLIC", None)
    command = ["bash", str(tools / "bootstrap_backend.sh"), "release"]
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert log.read_text().strip() == "9.9.0"
    assert legacy.read_bytes() == b"preserve this older cohort"
    resources.verify_release(data, backend, spec)
    first_folder = resources.release_directory(data, spec)
    source.write_bytes(archive_bytes(b"second cohort"))
    spec = spec | {"sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    (package / "third_party.lock.json").write_text(json.dumps({"release": spec}))
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    resources.verify_release(data, backend, spec)
    folder = resources.release_directory(data, spec)
    assert folder != first_folder
    assert (
        first_folder / "app/VPhone.bundle/Contents/Resources/guest-resources/vphoned"
    ).read_bytes() == b"worker"
    worker = folder / "app/VPhone.bundle/Contents/Resources/guest-resources/vphoned"
    worker.write_bytes(b"stale complete bundle")
    log.unlink()
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "Cached release payload differs" in result.stderr
    assert not log.exists()
    assert worker.read_bytes() == b"stale complete bundle"
    assert legacy.read_bytes() == b"preserve this older cohort"
