"""Exercise exact fork checkouts using local repositories, without backend builds."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "tools/sync_backend_sources.py"
spec = importlib.util.spec_from_file_location("limulator_backend_sources", HELPER)
assert spec is not None and spec.loader is not None
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)


def git(repository, *arguments):
    return (
        subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )


def commit(repository, text):
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        text,
    )
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def pinned(tmp_path):
    upstreams = {}
    for name in ("backend", "qemu", "reims-vgpu", "metal2vulkan", "vphone_cli"):
        repository = tmp_path / "remotes" / name
        repository.mkdir(parents=True)
        git(repository, "init", "--quiet", "--initial-branch=main")
        git(repository, "config", "core.autocrlf", "false")
        (repository / "source.txt").write_text(f"{name} original\n")
        (repository / ".gitignore").write_text("build/\ncpu-tree/\n")
        commit(repository, "initial")
        upstreams[name] = repository
    # The backend advertises the old QEMU gitlink/default branch. The lock must win.
    git(
        upstreams["backend"],
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(upstreams["qemu"]),
        "qemu",
    )
    commit(upstreams["backend"], "upstream submodule")
    git(upstreams["qemu"], "checkout", "-b", "fork-only")
    (upstreams["qemu"] / "source.txt").write_text("fork QEMU change\n")
    qemu_pin = commit(upstreams["qemu"], "fork change")
    git(upstreams["qemu"], "checkout", "main")
    entries = {
        name: {"repository": str(path), "revision": git(path, "rev-parse", "HEAD")}
        for name, path in upstreams.items()
    }
    entries["qemu"]["revision"] = qemu_pin
    entries["vphone_cli"]["version"] = "2.14.2"
    manifest = {
        "backend": entries["backend"],
        "submodules": {name: entries[name] for name in ("qemu", "reims-vgpu", "metal2vulkan")},
        "vphone_cli": entries["vphone_cli"],
    }
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(manifest))
    return lock, tmp_path / "checkout", upstreams


def test_clones_explicit_fork_pins_instead_of_backend_gitlinks(pinned):
    lock, backend, upstreams = pinned
    sources.sync_sources(lock, backend)
    manifest = json.loads(lock.read_text())
    for name, path, entry in sources.source_specs(lock, backend):
        assert git(path, "rev-parse", "HEAD") == entry["revision"]
    assert (backend / "qemu/source.txt").read_text() == "fork QEMU change\n"
    assert git(backend / "qemu", "rev-parse", "HEAD") != git(upstreams["qemu"], "rev-parse", "main")
    assert (
        git(backend / "qemu", "config", "--get", "remote.origin.url")
        == manifest["submodules"]["qemu"]["repository"]
    )
    sources.sync_sources(lock, backend, check=True)


def test_fetches_new_pin_from_explicit_repository_and_preserves_artifacts(pinned):
    lock, backend, upstreams = pinned
    sources.sync_sources(lock, backend)
    qemu = backend / "qemu"
    artifact = qemu / "build/result.bin"
    artifact.parent.mkdir()
    artifact.write_bytes(b"preserved build artifact")
    untracked = backend / "notes.txt"
    untracked.write_text("unfinished work\n")
    git(qemu, "remote", "set-url", "origin", str(upstreams["metal2vulkan"]))
    (upstreams["qemu"] / "source.txt").write_text("new pinned QEMU\n")
    revision = commit(upstreams["qemu"], "new fork pin")
    manifest = json.loads(lock.read_text())
    manifest["submodules"]["qemu"]["revision"] = revision
    lock.write_text(json.dumps(manifest))
    sources.sync_sources(lock, backend)
    assert git(qemu, "rev-parse", "HEAD") == revision
    assert (qemu / "source.txt").read_text() == "new pinned QEMU\n"
    assert artifact.read_bytes() == b"preserved build artifact"
    assert untracked.read_text() == "unfinished work\n"
    sources.sync_sources(lock, backend)


@pytest.mark.parametrize("name,staged", [("backend", False), ("qemu", False), ("vphone_cli", True)])
def test_dirty_sources_are_refused_even_when_pin_matches(pinned, name, staged):
    lock, backend, upstreams = pinned
    sources.sync_sources(lock, backend)
    paths = {name: path for name, path, entry in sources.source_specs(lock, backend)}
    edited = paths[name] / "source.txt"
    edited.write_text("my interrupted edit\n")
    if staged:
        git(paths[name], "add", "source.txt")
    index = git(paths[name], "write-tree")
    before = {key: git(path, "rev-parse", "HEAD") for key, path in paths.items()}
    with pytest.raises(sources.SourceError, match="new LIMULATOR_BACKEND path"):
        sources.sync_sources(lock, backend)
    assert edited.read_text() == "my interrupted edit\n"
    assert git(paths[name], "write-tree") == index
    assert before == {key: git(path, "rev-parse", "HEAD") for key, path in paths.items()}


def test_staged_gitlink_changes_are_not_ignored(pinned):
    lock, backend, upstreams = pinned
    sources.sync_sources(lock, backend)
    revision = git(backend / "qemu", "rev-parse", "HEAD")
    git(backend, "update-index", "--cacheinfo", f"160000,{revision},qemu")
    with pytest.raises(sources.SourceError, match="tracked or staged"):
        sources.sync_sources(lock, backend)
    assert revision in git(backend, "ls-files", "--stage", "qemu")


@pytest.mark.parametrize("name", ["untracked.txt", "build/result.bin"])
def test_pin_update_does_not_overwrite_untracked_or_ignored_files(pinned, name):
    lock, backend, upstreams = pinned
    sources.sync_sources(lock, backend)
    local = backend / "qemu" / name
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("local artifact\n")
    tracked = upstreams["qemu"] / name
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("new tracked source\n")
    git(upstreams["qemu"], "add", "--force", name)
    revision = commit(upstreams["qemu"], "track former artifact")
    manifest = json.loads(lock.read_text())
    manifest["submodules"]["qemu"]["revision"] = revision
    lock.write_text(json.dumps(manifest))
    original = git(backend / "qemu", "rev-parse", "HEAD")
    with pytest.raises(sources.SourceError):
        sources.sync_sources(lock, backend)
    assert local.read_text() == "local artifact\n"
    assert git(backend / "qemu", "rev-parse", "HEAD") == original


def test_check_mode_never_creates_or_changes_sources(pinned):
    lock, backend, upstreams = pinned
    with pytest.raises(sources.SourceError, match="missing"):
        sources.sync_sources(lock, backend, check=True)
    assert not backend.exists()
    sources.sync_sources(lock, backend)
    manifest = json.loads(lock.read_text())
    manifest["submodules"]["qemu"]["revision"] = git(upstreams["qemu"], "rev-parse", "main")
    lock.write_text(json.dumps(manifest))
    current = git(backend / "qemu", "rev-parse", "HEAD")
    with pytest.raises(sources.SourceError, match="Expected commit"):
        sources.sync_sources(lock, backend, check=True)
    assert git(backend / "qemu", "rev-parse", "HEAD") == current


def test_unmanaged_existing_directory_is_preserved(pinned):
    lock, backend, upstreams = pinned
    backend.mkdir()
    artifact = backend / "important.bin"
    artifact.write_bytes(b"existing data")
    with pytest.raises(sources.SourceError, match="not its own Git checkout"):
        sources.sync_sources(lock, backend)
    assert artifact.read_bytes() == b"existing data"
    assert not (backend / ".git").exists()


def test_later_dirty_checkout_prevents_earlier_revision_update(pinned):
    lock, backend, upstreams = pinned
    sources.sync_sources(lock, backend)
    original = git(backend, "rev-parse", "HEAD")
    (upstreams["backend"] / "source.txt").write_text("new backend revision\n")
    manifest = json.loads(lock.read_text())
    manifest["backend"]["revision"] = commit(upstreams["backend"], "new backend")
    lock.write_text(json.dumps(manifest))
    (backend / "qemu/source.txt").write_text("interrupted QEMU change\n")
    with pytest.raises(sources.SourceError, match="tracked or staged"):
        sources.sync_sources(lock, backend)
    assert git(backend, "rev-parse", "HEAD") == original
    assert (backend / "qemu/source.txt").read_text() == "interrupted QEMU change\n"


def test_qemu_nested_library_is_pinned_and_keeps_its_artifacts(pinned):
    lock, backend, upstreams = pinned
    qemu = upstreams["qemu"]
    library = qemu.parent / "mlib"
    library.mkdir()
    git(library, "init", "--quiet", "--initial-branch=main")
    (library / "source.txt").write_text("nested library\n")
    revision = commit(library, "nested library")
    git(qemu, "-c", "protocol.file.allow=always", "submodule", "add", str(library), "util/mlib")
    manifest = json.loads(lock.read_text())
    manifest["submodules"]["qemu"]["revision"] = commit(qemu, "add nested library")
    lock.write_text(json.dumps(manifest))
    sources.sync_sources(lock, backend)
    nested = backend / "qemu/util/mlib"
    assert git(nested, "rev-parse", "HEAD") == revision
    artifact = nested / "unfinished.bin"
    artifact.write_bytes(b"nested artifact")
    sources.sync_sources(lock, backend)
    sources.sync_sources(lock, backend, check=True)
    assert artifact.read_bytes() == b"nested artifact"


def test_user_submodule_recursion_does_not_change_unlisted_checkout(pinned):
    lock, backend, upstreams = pinned
    extra = upstreams["backend"].parent / "extra"
    extra.mkdir()
    git(extra, "init", "--quiet", "--initial-branch=main")
    (extra / ".gitignore").write_text("build/\n")
    initial = commit(extra, "extra source")
    remote = upstreams["backend"]
    git(remote, "-c", "protocol.file.allow=always", "submodule", "add", str(extra), "extra")
    manifest = json.loads(lock.read_text())
    manifest["backend"]["revision"] = commit(remote, "add unlisted module")
    lock.write_text(json.dumps(manifest))
    sources.sync_sources(lock, backend)
    subprocess.run(["git", "clone", str(extra), str(backend / "extra")], check=True, capture_output=True)
    git(backend, "config", "submodule.recurse", "true")
    git(backend, "config", "submodule.extra.active", "true")
    git(backend, "config", "submodule.extra.url", str(extra))
    artifact = backend / "extra/build/result.bin"
    artifact.parent.mkdir()
    artifact.write_bytes(b"local build")
    replacement = extra / "build/result.bin"
    replacement.parent.mkdir()
    replacement.write_bytes(b"new tracked file")
    git(extra, "add", "--force", "build/result.bin")
    updated = commit(extra, "new extra revision")
    git(remote / "extra", "fetch", str(extra), updated)
    git(remote / "extra", "checkout", "--detach", updated)
    manifest["backend"]["revision"] = commit(remote, "update unlisted module")
    lock.write_text(json.dumps(manifest))
    sources.sync_sources(lock, backend)
    assert git(backend / "extra", "rev-parse", "HEAD") == initial
    assert artifact.read_bytes() == b"local build"
    assert git(backend / "qemu", "rev-parse", "HEAD") == manifest["submodules"]["qemu"]["revision"]


def test_standalone_build_refuses_dirty_sources_before_running_tools(pinned, tmp_path):
    if not shutil.which("uv") or not shutil.which("jq"):
        pytest.skip("the public bootstrap requires uv and jq")
    lock, backend, upstreams = pinned
    sources.sync_sources(lock, backend)
    (backend / "source.txt").write_text("my interrupted backend change\n")
    package = tmp_path / "package"
    tools = package / "tools"
    tools.mkdir(parents=True)
    for name in ("bootstrap_backend.sh", "backend_env.sh", "sync_backend_sources.py"):
        shutil.copyfile(HELPER.parent / name, tools / name)
    shutil.copyfile(lock, package / "third_party.lock.json")
    prefix = tmp_path / "prefix"
    binaries = prefix / "bin"
    binaries.mkdir(parents=True)
    marker = tmp_path / "build-started"
    curl = binaries / "curl"
    curl.write_text('#!/bin/sh\ntouch "$BUILD_MARKER"\nexit 99\n')
    curl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(tools / "bootstrap_backend.sh"), "qemu"],
        env=os.environ
        | {
            "LIMULATOR_HOME": str(tmp_path / "data"),
            "LIMULATOR_BACKEND": str(backend),
            "LIMULATOR_PREFIX": str(prefix),
            "LIMULATOR_CACHE": str(tmp_path / "cache"),
            "BUILD_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "new LIMULATOR_BACKEND path" in result.stderr
    assert not marker.exists()
    assert (backend / "source.txt").read_text() == "my interrupted backend change\n"


def test_nested_source_parent_symlink_cannot_update_an_outside_checkout(pinned, tmp_path):
    lock, backend, upstreams = pinned
    sources.sync_sources(lock, backend)
    outside = tmp_path / "outside-cpu-tree"
    shutil.move(backend / "cpu-tree", outside)
    (backend / "cpu-tree").symlink_to(outside, target_is_directory=True)
    original = git(outside / "vphone-cli-2.14.2", "rev-parse", "HEAD")
    with pytest.raises(sources.SourceError, match="through a symlink"):
        sources.sync_sources(lock, backend)
    assert git(outside / "vphone-cli-2.14.2", "rev-parse", "HEAD") == original
    assert (backend / "cpu-tree").is_symlink()
