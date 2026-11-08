import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("custom_release_base", [None, "custom release base"])
def test_bootstrap_uses_locked_release_for_a_stale_existing_bundle(tmp_path, custom_release_base):
    project = tmp_path / "project"
    tools = project / "tools"
    backend = tmp_path / "backend"
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    archive = tmp_path / "VPhone-3.1.0.zip"
    version = "3.1.0"
    cli_source_version = "2.14.2"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "app/VPhone.bundle/Contents/Resources/OpenAVB/release-id",
            f"locked-{version}\n",
        )
        bundle.writestr(
            "app/VPhone.bundle/Contents/Resources/OpenSEP/release-id",
            f"locked-{version}\n",
        )
    sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    (project / "third_party.lock.json").parent.mkdir(parents=True)
    (project / "third_party.lock.json").write_text(
        json.dumps(
            {
                "release": {
                    "version": version,
                    "url": "https://example.invalid/VPhone-3.1.0.zip",
                    "sha256": sha256,
                },
                "vphone_cli": {"version": cli_source_version},
            }
        )
    )
    tools.mkdir()
    shutil.copy2(ROOT / "tools/bootstrap_backend.sh", tools / "bootstrap_backend.sh")
    shutil.copy2(ROOT / "tools/backend_env.sh", tools / "backend_env.sh")
    fake_bin.mkdir()
    (fake_bin / "uv").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "while (($#)); do\n"
        "  if [[ $1 == -o ]]; then output=$2; shift 2; else shift; fi\n"
        "done\n"
        'cp "$LIMULATOR_TEST_ARCHIVE" "$output"\n'
    )
    for helper in (fake_bin / "uv", fake_bin / "curl"):
        helper.chmod(0o755)

    setup = backend / "setup/setup.sh"
    setup.parent.mkdir(parents=True)
    setup_log = tmp_path / "setup.log"
    setup.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "phase=${1:-}\n"
        "[[ $phase == release ]] || exit 2\n"
        "VPHONE_CLI_PUBLIC=${VPHONE_CLI_PUBLIC:-2.14.2}\n"
        'rel="$VSETUP_RELEASE/app"\n'
        'zip="$VSETUP_RELEASE/VPhone-$VPHONE_CLI_PUBLIC.zip"\n'
        'if [[ ! -d "$rel/VPhone.bundle/Contents/Resources/OpenAVB" ]]; then\n'
        '  [[ -f "$zip" ]] || exit 3\n'
        '  [[ -d "$rel/VPhone.bundle" ]] || unzip -q -n "$zip" -d "$VSETUP_RELEASE"\n'
        '  bundle=$(find "$VSETUP_RELEASE" -type d -name VPhone.bundle -print -quit)\n'
        '  if [[ -n $bundle && $bundle != "$rel/VPhone.bundle" ]]; then\n'
        '    rm -rf "$rel/VPhone.bundle"\n'
        '    mv "$bundle" "$rel/VPhone.bundle"\n'
        "  fi\n"
        "fi\n"
        'printf "version=%s\\npath=%s\\n" "$VPHONE_CLI_PUBLIC" "$VSETUP_RELEASE" > "$LIMULATOR_TEST_SETUP_LOG"\n'
    )
    setup.chmod(0o755)

    limulator_home = home / ".local/share/limulator"
    prefix_bin = limulator_home / "tools/bin"
    prefix_bin.mkdir(parents=True)
    for helper in (fake_bin / "uv", fake_bin / "curl"):
        shutil.copy2(helper, prefix_bin / helper.name)
    release_base = (
        tmp_path / custom_release_base if custom_release_base else limulator_home / "releases/vphone"
    )
    stale_bundle = release_base / "app/VPhone.bundle/Contents/Resources/OpenAVB/release-id"
    stale_bundle.parent.mkdir(parents=True)
    stale_bundle.write_text("stale-legacy\n")
    env = os.environ.copy()
    env.pop("LIMULATOR_RELEASE_BASE", None)
    env.pop("VSETUP_RELEASE", None)
    env.update(
        HOME=str(home),
        PATH=f"{fake_bin}:{env['PATH']}",
        LIMULATOR_HOME=str(limulator_home),
        LIMULATOR_CACHE=str(home / ".cache/limulator"),
        LIMULATOR_BACKEND=str(backend),
        LIMULATOR_PREFIX=str(limulator_home / "tools"),
        LIMULATOR_JOBS="2",
        LIMULATOR_TEST_ARCHIVE=str(archive),
        LIMULATOR_TEST_SETUP_LOG=str(setup_log),
    )
    if custom_release_base:
        env["VSETUP_RELEASE"] = str(release_base)

    result = subprocess.run(
        ["bash", str(tools / "bootstrap_backend.sh"), "release"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    selected_root = release_base / f"{version}-{sha256}"
    selected_bundle_id = selected_root / "app/VPhone.bundle/Contents/Resources/OpenAVB/release-id"
    assert selected_bundle_id.read_text() == f"locked-{version}\n"
    assert stale_bundle.read_text() == "stale-legacy\n"
    assert (selected_root / f"VPhone-{version}.zip").read_bytes() == archive.read_bytes()
    assert setup_log.read_text().splitlines() == [f"version={version}", f"path={selected_root}"]
