#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Check out the exact dependency commits without modifying local source edits."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


class SourceError(RuntimeError):
    pass


def git(repository, *arguments, check=True):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    command = ["git", "--no-optional-locks"]
    if repository is not None:
        command += ["-C", str(repository)]
    result = subprocess.run(command + list(arguments), capture_output=True, env=environment)
    if check and result.returncode:
        raise SourceError(result.stderr.decode(errors="replace").strip())
    return result


def source_specs(lock, backend):
    manifest = json.loads(Path(lock).read_text())
    version = manifest["vphone_cli"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise SourceError("Invalid pinned vphone-cli version")
    sources = [("backend", backend, manifest["backend"])]
    sources += [
        (name, backend / name, manifest["submodules"][name])
        for name in ("qemu", "reims-vgpu", "metal2vulkan")
    ]
    sources.append(("vphone_cli", backend / "cpu-tree" / f"vphone-cli-{version}", manifest["vphone_cli"]))
    for name, path, entry in sources:
        if not isinstance(entry.get("revision"), str) or not re.fullmatch(r"[0-9a-f]{40}", entry["revision"]):
            raise SourceError(f"{name} must pin a full Git commit")
        repository = entry.get("repository")
        if (
            not isinstance(repository, str)
            or not repository
            or repository.startswith("-")
            or any(c in repository for c in "\r\n")
        ):
            raise SourceError(f"{name} must specify its repository URL")
    return sources


def refusal(path, reason):
    return SourceError(
        f"{reason}: {path}. Existing sources and build artifacts were preserved. "
        "Commit or move your source changes, or choose a new LIMULATOR_BACKEND path "
        "and run `limulator bootstrap sources`; no reset or patch migration is performed."
    )


def inspect(path):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise refusal(path, "Refusing a source checkout through a symlink")
    if not path.exists():
        return False
    if not path.is_dir():
        raise refusal(path, "Source path is not a directory")
    if not (path / ".git").exists():
        if any(path.iterdir()):
            raise refusal(path, "Source path is not its own Git checkout")
        return False
    top = git(path, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if Path(top).resolve() != path.resolve():
        raise refusal(path, "Source path belongs to another Git checkout")
    # Nested fork pins may intentionally differ from the parent's recorded gitlinks.
    # Ignore that worktree difference, but never ignore a staged gitlink edit.
    worktree = git(path, "diff", "--quiet", "--ignore-submodules=all", "--", check=False)
    index = git(path, "diff", "--cached", "--quiet", "--ignore-submodules=none", "--", check=False)
    if worktree.returncode or index.returncode:
        raise refusal(path, "Refusing tracked or staged source changes")
    return True


def checkout(path, entry, exists):
    revision, repository = entry["revision"], entry["repository"]
    if not exists:
        path.parent.mkdir(parents=True, exist_ok=True)
        git(
            None,
            "clone",
            "--no-checkout",
            "--no-hardlinks",
            "--no-recurse-submodules",
            "--",
            repository,
            str(path),
        )
    if git(path, "cat-file", "-e", f"{revision}^{{commit}}", check=False).returncode:
        git(path, "fetch", "--no-tags", "--recurse-submodules=no", "--", repository, revision)
    resolved = git(path, "rev-parse", f"{revision}^{{commit}}").stdout.decode().strip()
    if resolved != revision:
        raise SourceError(f"Pinned revision is not a commit: {revision}")
    current = git(path, "rev-parse", "HEAD").stdout.decode().strip()
    if not exists or current != revision:
        # Git normally overwrites ignored files during checkout. Explicitly refuse it.
        git(path, "checkout", "--detach", "--no-overwrite-ignore", "--no-recurse-submodules", revision)


def sync_sources(lock, backend, *, check=False):
    backend = Path(backend).absolute()
    sources = source_specs(lock, backend)
    # Validate every existing checkout before fetching or moving any source revision.
    existing = {name: inspect(path) for name, path, entry in sources}
    mlib = backend / "qemu/util/mlib"
    if (mlib / ".git").exists():
        inspect(mlib)
    for name, path, entry in sources:
        if check:
            if not existing[name]:
                raise refusal(path, "Pinned source checkout is missing")
            actual = git(path, "rev-parse", "HEAD").stdout.decode().strip()
            if actual != entry["revision"]:
                raise refusal(path, f"Expected commit {entry['revision']}, found {actual}")
        else:
            checkout(path, entry, existing[name])
    qemu = backend / "qemu"
    link = git(qemu, "ls-tree", "HEAD", "--", "util/mlib").stdout.decode().split()
    if link:
        if link[:2] != ["160000", "commit"]:
            raise SourceError("QEMU util/mlib is not a pinned submodule")
        repository = (
            git(qemu, "config", "--blob", "HEAD:.gitmodules", "--get", "submodule.util/mlib.url")
            .stdout.decode()
            .strip()
        )
        if not check:
            checkout(mlib, {"repository": repository, "revision": link[2]}, inspect(mlib))
        if not inspect(mlib) or git(mlib, "rev-parse", "HEAD").stdout.decode().strip() != link[2]:
            raise refusal(mlib, "QEMU util/mlib does not match its pinned commit")
    for name, path, entry in sources:
        print(f"{name}: verified {entry['revision']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        sync_sources(args.lock, args.backend, check=args.check)
    except (OSError, ValueError, KeyError, SourceError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
