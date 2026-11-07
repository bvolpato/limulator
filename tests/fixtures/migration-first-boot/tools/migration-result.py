#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Gate setup changes on the same strict migration result used by Limulator.

Exit 0 permits setup, 1 waits for a current result, and 2 reports a current
explicit failure. This backend helper also runs without Limulator installed.
"""

import json
import plistlib
import sys
from pathlib import Path
from xml.parsers.expat import ExpatError


def expected_build(vm, device, version, pairing_build):
    # This is the unique tree used by restore. fw prepare preserves the original
    # iPhone manifest here before replacing BuildManifest.plist with the hybrid.
    trees = [path for path in Path(vm).glob("iPhone*_Restore") if path.is_dir()]
    if len(trees) != 1:
        raise ValueError("expected one prepared iPhone*_Restore tree to identify the migration build")
    manifest = trees[0] / "iPhone-BuildManifest.plist"
    if trees[0].is_symlink() or manifest.is_symlink() or not manifest.is_file():
        raise ValueError(f"missing regular prepared iPhone manifest: {manifest}")
    with manifest.open("rb") as stream:
        identity = plistlib.load(stream)
    if not isinstance(identity, dict):
        raise ValueError(f"unsupported prepared iPhone manifest: {manifest}")
    products = identity.get("SupportedProductTypes")
    if not isinstance(products, list) or device not in products:
        raise ValueError(f"prepared iPhone manifest does not support {device}: {manifest}")
    actual_version = identity.get("ProductVersion")
    if not isinstance(actual_version, str) or actual_version != version:
        raise ValueError(f"prepared iPhone firmware version {actual_version!r} contradicts requested {version!r}")
    build = identity.get("ProductBuildVersion")
    if not isinstance(build, str) or not build or build == "unknown":
        raise ValueError("prepared iPhone manifest does not identify a build version")
    if pairing_build and pairing_build != "unknown" and pairing_build != build:
        raise ValueError(
            f"compatibility catalog build {pairing_build!r} contradicts firmware build {build!r}"
        )
    return build


def rpc_result(raw):
    reply = json.loads(raw)
    if not isinstance(reply, dict) or ("id" in reply and reply["id"] != 1):
        raise ValueError("unsupported RPC response")
    if reply.get("error") is not None:
        raise ValueError("guest RPC failed")
    return reply.get("result", reply)


def migration_status(migration_raw, setup_raw, build):
    try:
        reply = rpc_result(migration_raw)
        if not isinstance(reply, dict) or not isinstance(reply.get("values"), dict):
            return 1
        entry = reply["values"].get("DMLastMigrationResults")
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), dict):
            return 1
        result = entry["value"]
        if result.get("buildVersion") != build:
            return 1
        if result.get("success") is False:
            print(f"guest DataMigrator reported migration success=false for expected build {build}")
            return 2
        if result.get("success") is not True:
            return 1
        setup = rpc_result(setup_raw)
        return 0 if isinstance(setup, dict) and setup.get("data_migration_done") is True else 1
    except (ValueError, TypeError):
        return 1


def main():
    if len(sys.argv) == 6 and sys.argv[1] == "expected-build":
        try:
            print(expected_build(*sys.argv[2:]))
            return 0
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
            print(error, file=sys.stderr)
            return 2
    if len(sys.argv) == 4 and sys.argv[1] == "check":
        return migration_status(sys.stdin.read(), sys.argv[3], sys.argv[2])
    print("usage: migration-result.py {expected-build VM DEVICE IOS PAIR_BUILD|check BUILD SETUP_JSON}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
