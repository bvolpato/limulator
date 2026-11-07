"""Exercise the backend first-boot gate without a guest or native processes."""

import json
import os
import plistlib
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from limulator import creator
from limulator.profiles import get_profile

ROOT = Path(__file__).resolve().parents[1]
BUILD = "24A94425"
MODEL = "iPhone19,4"
VERSION = "27.1"


def original_manifest(tmp_path):
    return tmp_path / "guest with spaces" / f"{MODEL}_{VERSION}_{BUILD}_Restore/iPhone-BuildManifest.plist"


def firmware_identity():
    return {"ProductBuildVersion": BUILD, "ProductVersion": VERSION, "SupportedProductTypes": [MODEL]}


@pytest.fixture
def backend(tmp_path):
    source = tmp_path / "backend"
    shutil.copytree(ROOT / "tests/fixtures/migration-first-boot", source)
    subprocess.run(
        ["git", "apply", str(ROOT / "patches/0018-first-boot-migration-result.patch")],
        cwd=source,
        check=True,
        capture_output=True,
    )
    (source / "tools/auto-lock-never.py").write_text(
        "import os\nwith open(os.environ['CALLS'], 'a') as stream: stream.write('auto-lock\\n')\n"
    )
    manifest = original_manifest(tmp_path)
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(plistlib.dumps(firmware_identity()))
    (manifest.parent / "BuildManifest.plist").write_bytes(
        plistlib.dumps({"ProductBuildVersion": "23E5207q", "ProductVersion": "26.4"})
    )
    return source


def result(*, success=True, build=BUILD):
    return {
        "id": 1,
        "result": {"values": {"DMLastMigrationResults": {"value": {"success": success, "buildVersion": build}}}},
    }


def first_boot(backend, tmp_path, responses, *, marker=True, pairing=BUILD, ipsw=None):
    calls = tmp_path / "calls"
    replies = tmp_path / "replies"
    replies.mkdir()
    for index, reply in enumerate(responses):
        (replies / str(index)).write_text(json.dumps(reply))
    shutil.copyfile(replies / str(len(responses) - 1), replies / "last")
    count = tmp_path / "count"
    count.write_text("0")
    setup = tmp_path / "setup.json"
    setup.write_text(json.dumps({"id": 1, "result": {"data_migration_done": marker}}, separators=(",", ":")))
    script = r"""
set -Eeuo pipefail
log() { :; }
die() { printf '%s\n' "$*" >&2; exit 1; }
boot_vm() { printf 'boot\n' >> "$CALLS"; }
qemu_alive() { return 0; }
qemu_exit_report() { :; }
sleep() { SECONDS=$((SECONDS + $1)); }
unlock_vm() { printf 'unlock\n' >> "$CALLS"; }
step() { "$2"; printf 'booted\n' >> "$CALLS"; }
vrpc() {
  printf '%s timeout=%s\n' "$1" "${2:-120}" >> "$CALLS"
  case "$1" in
    *setup.status*) cat "$SETUP_REPLY" ;;
    *settings.get*)
      local index
      index=$(cat "$COUNT")
      if [ -f "$REPLIES/$index" ]; then cat "$REPLIES/$index"; else cat "$REPLIES/last"; fi
      printf '%s\n' "$((index + 1))" > "$COUNT"
      ;;
    *setup.skip*) printf '{"id":1,"result":{}}' ;;
    *) return 97 ;;
  esac
}
""" + (backend / "setup/create-vm.sh").read_text()
    env = os.environ | {
        "T": str(backend / "tools"),
        "VM": str(tmp_path / "guest with spaces"),
        "IOS_LOCAL": str(ipsw or tmp_path / f"iPhone_27.1_{BUILD}_Restore.ipsw"),
        "DEVICE": MODEL,
        "IOS": VERSION,
        "PAIR_BUILD": pairing,
        "CID": "41",
        "DRY": "0",
        "SCALE": "3",
        "TRIM": "0",
        "LIMULATOR_MIGRATION_TIMEOUT": "60",
        "CALLS": str(calls),
        "COUNT": str(count),
        "SETUP_REPLY": str(setup),
        "REPLIES": str(replies),
    }
    completed = subprocess.run(["bash"], input=script, env=env, text=True, capture_output=True, timeout=5)
    return completed, calls.read_text() if calls.exists() else "", int(count.read_text())


@pytest.mark.parametrize(
    "reply,marker,failure",
    [
        (result(success=False), True, "migration success=false for expected build"),
        (result(success=False), False, "migration success=false for expected build"),
        ({"id": 1, "result": {"values": {}}}, True, "migration timed out"),
        (result(build="24A446"), True, "migration timed out"),
        (result(build="24A446", success=False), True, "migration timed out"),
        (result(success="true"), True, "migration timed out"),
        (result(success=1), True, "migration timed out"),
        (result(), False, "migration timed out"),
        (result() | {"error": {"message": "unsupported"}}, True, "migration timed out"),
        (result() | {"id": 99}, True, "migration timed out"),
        (None, True, "migration timed out"),
    ],
)
def test_marker_alone_cannot_trigger_setup_mutations(backend, tmp_path, reply, marker, failure):
    completed, calls, polls = first_boot(backend, tmp_path, [reply], marker=marker)
    assert completed.returncode != 0
    assert failure in completed.stderr
    assert "setup.skip" not in calls
    assert "unlock" not in calls
    assert "auto-lock" not in calls
    assert "booted" not in calls
    assert polls == (1 if failure.startswith("migration success=false") else 2)


@pytest.mark.parametrize("responses", [[result()], [None, result()]])
def test_current_success_reaches_setup_only_after_strict_result(backend, tmp_path, responses):
    completed, calls, polls = first_boot(backend, tmp_path, responses)
    assert completed.returncode == 0, completed.stderr
    assert calls.index("settings.get") < calls.index("setup.skip") < calls.index("unlock")
    assert calls.endswith("unlock\nauto-lock\nbooted\n")
    assert polls == len(responses)
    creator._verify_migration_result(creator._migration_result_from_settings(responses[-1]["result"]), BUILD)


@pytest.mark.parametrize("pairing", [BUILD, "unknown", "24A446"])
def test_expected_build_matches_pinned_host_contract(backend, tmp_path, pairing):
    profile = get_profile("iphone-duo")
    command = ["python3", str(backend / "tools/migration-result.py"), "expected-build"]
    completed = subprocess.run(
        [*command, str(tmp_path / "guest with spaces"), profile.model, profile.os_version, pairing],
        text=True,
        capture_output=True,
        timeout=5,
    )
    if pairing == "24A446":
        assert completed.returncode != 0
        with pytest.raises(RuntimeError, match="contradicts pinned profile firmware build"):
            creator._expected_migration_build(profile, {"build": pairing})
        failed, calls, polls = first_boot(backend, tmp_path, [result()], pairing=pairing)
        assert failed.returncode != 0
        assert "cannot identify the expected migration build" in failed.stderr
        assert calls == ""
        assert polls == 0
    else:
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == creator._expected_migration_build(profile, {"build": pairing})


@pytest.mark.parametrize("archive_removed", [False, True])
def test_renamed_or_removed_ipsw_uses_preserved_iphone_identity(backend, tmp_path, archive_removed):
    archive = tmp_path / "ios.ipsw"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("BuildManifest.plist", plistlib.dumps(firmware_identity()))
    if archive_removed:
        archive.unlink()
    completed, calls, polls = first_boot(backend, tmp_path, [result()], ipsw=archive)
    assert completed.returncode == 0, completed.stderr
    assert "setup.skip" in calls
    assert calls.endswith("unlock\nauto-lock\nbooted\n")
    assert polls == 1


@pytest.mark.parametrize("invalid", ["missing", "malformed", "device", "version", "build", "ambiguous"])
def test_invalid_prepared_identity_cannot_fall_back_to_other_firmware(backend, tmp_path, invalid):
    manifest = original_manifest(tmp_path)
    identity = firmware_identity()
    if invalid == "missing":
        manifest.unlink()
    elif invalid == "malformed":
        manifest.write_bytes(b"not a property list")
    elif invalid == "ambiguous":
        shutil.copytree(manifest.parent, manifest.parent.with_name("iPhone_other_Restore"))
    else:
        identity.update(
            {
                "device": {"SupportedProductTypes": ["iPhone17,3"]},
                "version": {"ProductVersion": "26.6.2"},
                "build": {"ProductBuildVersion": "24A446"},
            }[invalid]
        )
        manifest.write_bytes(plistlib.dumps(identity))
    archive = tmp_path / f"iPhone_{VERSION}_{BUILD}_Restore.ipsw"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("BuildManifest.plist", plistlib.dumps(firmware_identity()))
    completed, calls, polls = first_boot(backend, tmp_path, [result()], ipsw=archive)
    assert completed.returncode != 0
    assert "cannot identify the expected migration build" in completed.stderr
    assert calls == ""
    assert polls == 0
