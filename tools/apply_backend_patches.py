#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Validate ordered vendor patches against pinned Git trees before applying them."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath


class PatchError(RuntimeError):
    pass


def git(repository, *arguments, env=None, data=None):
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "--literal-pathspecs",
            "-c",
            "core.splitIndex=false",
            "-C",
            str(repository),
            *arguments,
        ],
        input=data,
        capture_output=True,
        env=env,
    )
    if result.returncode:
        raise PatchError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def safe_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or ".git" in path.parts:
        raise PatchError(f"Unsafe patch path: {name}")
    return path


def tree_state(repository, tree, paths, env):
    entries = git(repository, "ls-tree", "-z", tree, "--", *paths, env=env)
    state = dict.fromkeys(paths)
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise PatchError(f"Unsupported patched file type: {os.fsdecode(name)} ({mode})")
        state[os.fsdecode(name)] = (mode, oid)
    return state


def checkout_state(repository, paths, env):
    state = {}
    for name in paths:
        relative = safe_path(name)
        for parent in relative.parents:
            path = repository / parent
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise PatchError(f"Refusing patched path through a non-directory: {path}")
        path = repository / relative
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            state[name] = None
            continue
        if stat.S_ISLNK(mode):
            kind, data = "120000", os.fsencode(os.readlink(path))
        elif stat.S_ISREG(mode):
            kind = "100755" if mode & stat.S_IXUSR else "100644"
            data = path.read_bytes()
        else:
            raise PatchError(f"Refusing non-file patch target: {path}")
        oid = git(repository, "hash-object", "--no-filters", "--stdin", env=env, data=data).decode().strip()
        state[name] = (kind, oid)
    return state


def stage_stack(repository, base, patches, temporary):
    """Use a private index and object store; never change the checkout's index."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        }
    }
    objects = (
        git(repository, "rev-parse", "--path-format=absolute", "--git-path", "objects", env=environment)
        .decode()
        .strip()
    )
    (temporary / "objects").mkdir()
    environment.update(
        GIT_INDEX_FILE=str(temporary / "index"),
        GIT_OBJECT_DIRECTORY=str(temporary / "objects"),
        GIT_ALTERNATE_OBJECT_DIRECTORIES=objects,
    )
    base_tree = git(repository, "rev-parse", f"{base}^{{tree}}", env=environment).decode().strip()
    git(repository, "read-tree", base_tree, env=environment)
    trees, paths = [base_tree], set()
    for patch in patches:
        git(repository, "apply", "--cached", "--whitespace=nowarn", "-", env=environment, data=patch)
        trees.append(git(repository, "write-tree", env=environment).decode().strip())
        changed = git(
            repository, "diff", "--cached", "--name-only", "--no-renames", "-z", base_tree, env=environment
        )
        paths.update(os.fsdecode(name) for name in changed.split(b"\0") if name)
    paths = sorted(paths)
    for name in paths:
        safe_path(name)
    actual = checkout_state(repository, paths, environment)
    states = [tree_state(repository, tree, paths, environment) for tree in trees]
    matches = [index for index, state in enumerate(states) if state == actual]
    if not matches:
        closest = min(states, key=lambda state: sum(state[name] != actual[name] for name in paths))
        conflicts = ", ".join(name for name in paths if closest[name] != actual[name])
        raise PatchError(f"Conflicting patch targets in {repository}: {conflicts}; no files changed")
    prefix = max(matches)
    delta = git(
        repository,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        trees[prefix],
        trees[-1],
        env=environment,
    )
    return repository, environment, paths, actual, states[-1], delta, prefix, len(patches)


def apply_stacks(lock_path, backend, *, check=False):
    lock_path, backend = Path(lock_path).resolve(), Path(backend).resolve()
    manifest = json.loads(lock_path.read_text())
    targets = {
        "backend": (backend, manifest["backend"]["revision"]),
        "qemu": (backend / "qemu", manifest["submodules"]["qemu"]["revision"]),
    }
    if "vphone_cli" in manifest:
        cli = manifest["vphone_cli"]
        relative = safe_path(f"cpu-tree/vphone-cli-{cli['version']}")
        targets["vphone_cli"] = (backend / relative, cli["revision"])
    patches = {}
    for entry in manifest["patches"]:
        target = entry["target"]
        if target not in targets:
            raise PatchError(f"Unsupported patch target: {target}")
        name = safe_path(entry["path"])
        data = (lock_path.parent / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise PatchError(f"Patch checksum mismatch: {name}")
        patches.setdefault(target, []).append(data)

    with ExitStack() as stack:
        plans = []
        for target, ordered in patches.items():
            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="limulator-patches-")))
            repository, base = targets[target]
            plan = stage_stack(repository, base, ordered, temporary)
            plans.append(plan)
            print(f"{target}: verified patch prefix {plan[-2]}/{plan[-1]}")
        # Validate every target before changing any checkout.
        for repository, environment, paths, actual, _, delta, _, _ in plans:
            if checkout_state(repository, paths, environment) != actual:
                raise PatchError(f"Patch targets changed during validation: {repository}")
            if delta:
                git(repository, "apply", "--check", "--whitespace=nowarn", "-", data=delta, env=environment)
        if not check:
            for repository, environment, paths, actual, expected, delta, _, _ in plans:
                if checkout_state(repository, paths, environment) != actual:
                    raise PatchError(f"Patch targets changed during validation: {repository}")
                if delta:
                    git(repository, "apply", "--whitespace=nowarn", "-", data=delta, env=environment)
                if checkout_state(repository, paths, environment) != expected:
                    raise PatchError(f"Patched files did not match the verified stack: {repository}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="Validate without applying missing patches")
    arguments = parser.parse_args()
    try:
        apply_stacks(arguments.lock, arguments.backend, check=arguments.check)
    except (PatchError, OSError, KeyError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
