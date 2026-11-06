"""Validate vendor patch stacks without commits or changes to the user's index."""

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "tools/apply_backend_patches.py"
spec = importlib.util.spec_from_file_location("limulator_backend_patches", HELPER)
assert spec is not None and spec.loader is not None
patches = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patches)


def git(repository, *arguments):
    return subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True).stdout


@pytest.fixture
def stack(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    qemu = backend / "qemu"
    qemu.mkdir()
    cli = backend / "cpu-tree/vphone-cli-2.14.2"
    cli.mkdir(parents=True)
    repositories = {"backend": backend, "qemu": qemu, "vphone_cli": cli}
    bases = {}
    for target, repository in repositories.items():
        git(repository, "init", "--quiet")
        git(repository, "config", "core.autocrlf", "false")
        (repository / "state.txt").write_text("before\noriginal\nafter\n")
        (repository / "unrelated.txt").write_text("unrelated base\n")
        if target == "backend":
            (repository / "obsolete.txt").write_text("removed by patch two\n")
            (repository / ".gitignore").write_text("qemu/\ncpu-tree/\n")
        git(repository, "add", ".")
        bases[target] = git(repository, "write-tree").decode().strip()

    entries = []
    previous = dict(bases)
    for number, (target, content) in enumerate(
        (
            ("backend", "first patch"),
            ("backend", "overlapping second patch"),
            ("qemu", "qemu patch"),
            ("vphone_cli", "cli patch"),
        ),
        1,
    ):
        repository = repositories[target]
        (repository / "state.txt").write_text(f"before\n{content}\nafter\n")
        if number == 2:
            (repository / "created.sh").write_text("#!/bin/sh\necho created\n")
            (repository / "created.sh").chmod(0o755)
            (repository / "obsolete.txt").unlink()
        git(repository, "add", ".")
        data = git(repository, "diff", "--cached", "--binary", previous[target])
        previous[target] = git(repository, "write-tree").decode().strip()
        path = tmp_path / f"{number:04d}.patch"
        path.write_bytes(data)
        entries.append({"target": target, "path": path.name, "sha256": hashlib.sha256(data).hexdigest()})

    # Restore only fixture-owned files. Keep the indexes staged to prove they are not altered.
    for repository in repositories.values():
        (repository / "state.txt").write_text("before\noriginal\nafter\n")
    (backend / "created.sh").unlink()
    (backend / "obsolete.txt").write_text("removed by patch two\n")
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "backend": {"revision": bases["backend"]},
                "submodules": {"qemu": {"revision": bases["qemu"]}},
                "vphone_cli": {"revision": bases["vphone_cli"], "version": "2.14.2"},
                "patches": entries,
            }
        )
    )
    return lock, backend


def test_overlapping_stack_applies_then_reruns_with_modes_and_deletions(stack):
    lock, backend = stack
    index = git(backend, "write-tree")
    patches.apply_stacks(lock, backend)
    assert (backend / "state.txt").read_text() == "before\noverlapping second patch\nafter\n"
    assert (backend / "qemu/state.txt").read_text() == "before\nqemu patch\nafter\n"
    assert (backend / "cpu-tree/vphone-cli-2.14.2/state.txt").read_text() == "before\ncli patch\nafter\n"
    assert os.access(backend / "created.sh", os.X_OK)
    assert not (backend / "obsolete.txt").exists()
    patches.apply_stacks(lock, backend)
    assert git(backend, "write-tree") == index


def test_partial_prefix_resumes_and_preserves_unrelated_staged_edits(stack):
    lock, backend = stack
    (backend / "state.txt").write_text("before\nfirst patch\nafter\n")
    (backend / "unrelated.txt").write_text("my staged work\n")
    git(backend, "add", "unrelated.txt")
    index = git(backend, "write-tree")
    patches.apply_stacks(lock, backend)
    assert (backend / "state.txt").read_text() == "before\noverlapping second patch\nafter\n"
    assert (backend / "unrelated.txt").read_text() == "my staged work\n"
    assert git(backend, "write-tree") == index


def test_diff_display_settings_do_not_change_the_applied_patch(stack):
    lock, backend = stack
    git(backend, "config", "diff.noprefix", "true")
    git(backend, "config", "color.ui", "always")
    patches.apply_stacks(lock, backend)
    assert (backend / "state.txt").read_text() == "before\noverlapping second patch\nafter\n"
    patches.apply_stacks(lock, backend)


@pytest.mark.parametrize("target", ["backend", "qemu", "vphone_cli"])
def test_conflicting_target_refuses_before_any_checkout_changes(stack, target):
    lock, backend = stack
    repository = {
        "backend": backend,
        "qemu": backend / "qemu",
        "vphone_cli": backend / "cpu-tree/vphone-cli-2.14.2",
    }[target]
    (repository / "state.txt").write_text("my conflicting edit\n")
    (backend / "unrelated.txt").write_text("my unrelated edit\n")
    index = git(backend, "write-tree")
    with pytest.raises(patches.PatchError, match="Conflicting patch targets"):
        patches.apply_stacks(lock, backend)
    assert (repository / "state.txt").read_text() == "my conflicting edit\n"
    if target != "backend":
        assert (backend / "state.txt").read_text() == "before\noriginal\nafter\n"
    assert not (backend / "created.sh").exists()
    assert (backend / "obsolete.txt").exists()
    assert (backend / "unrelated.txt").read_text() == "my unrelated edit\n"
    assert git(backend, "write-tree") == index


def test_check_mode_and_bad_checksum_do_not_apply(stack):
    lock, backend = stack
    patches.apply_stacks(lock, backend, check=True)
    assert (backend / "state.txt").read_text() == "before\noriginal\nafter\n"
    manifest = json.loads(lock.read_text())
    manifest["patches"][-1]["sha256"] = "0" * 64
    lock.write_text(json.dumps(manifest))
    with pytest.raises(patches.PatchError, match="checksum mismatch"):
        patches.apply_stacks(lock, backend)
    assert not (backend / "created.sh").exists()


def test_unsupported_target_is_refused_before_changes(stack):
    lock, backend = stack
    manifest = json.loads(lock.read_text())
    manifest["patches"][-1]["target"] = "other"
    lock.write_text(json.dumps(manifest))
    with pytest.raises(patches.PatchError, match="Unsupported patch target"):
        patches.apply_stacks(lock, backend)
    assert not (backend / "created.sh").exists()
