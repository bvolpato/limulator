import os
import subprocess
from pathlib import Path

import pytest


def _render_wrapper(tmp_path):
    bootstrap = Path(__file__).parents[1] / "tools/bootstrap_xtool.sh"
    source = bootstrap.read_text()
    opening = 'cat > "$TOOL_TMP" <<EOF\n'
    closing = '\nEOF\nchmod 0755 "$TOOL_TMP"'
    start = source.index(opening) + len(opening)
    end = source.index(closing, start)
    wrapper_template = source[start:end]

    wrapper = tmp_path / "prefix/bin/xtool"
    wrapper.parent.mkdir(parents=True)
    harness = (
        "set -eu\n"
        'printf -v DEFAULT_DATA_ROOT_QUOTED "%q" "$INSTALL_ROOT"\n'
        "SWIFT_VERSION=6.4.1\n"
        "TOOL_REVISION=fixture-revision\n"
        'TOOL_TMP="$WRAPPER_PATH"\n'
        'cat > "$TOOL_TMP" <<EOF\n'
        f"{wrapper_template}\n"
        "EOF\n"
        'chmod 0755 "$TOOL_TMP"\n'
    )
    subprocess.run(
        ["bash", "-c", harness],
        check=True,
        env={
            **os.environ,
            "INSTALL_ROOT": str(tmp_path / "installed-data"),
            "WRAPPER_PATH": str(wrapper),
        },
        capture_output=True,
        text=True,
    )
    return wrapper, tmp_path / "installed-data"


@pytest.mark.parametrize("config_override", [None, "custom"])
def test_generated_xtool_wrapper_uses_its_installation_root(tmp_path, config_override):
    wrapper, installation = _render_wrapper(tmp_path)
    swift_bin = installation / "swiftly/toolchains/6.4.1/usr/bin"
    swift_lib = installation / "swiftly/toolchains/6.4.1/usr/lib/swift/linux"
    swift_bin.mkdir(parents=True)
    swift_lib.mkdir(parents=True)
    swift = swift_bin / "swift"
    swift.write_text("#!/bin/sh\necho fixture Swift\n")
    swift.chmod(0o755)

    prefix = wrapper.parent.parent
    private_tool = prefix / "libexec/limulator/xtool/fixture-revision/xtool"
    private_tool.parent.mkdir(parents=True)
    private_tool.write_text(
        "#!/usr/bin/env bash\n"
        'printf "PATH=%s\\nXDG_CONFIG_HOME=%s\\nHOME=%s\\nLIMULATOR_HOME=%s\\nSWIFT=%s\\n" '
        '"$PATH" "$XDG_CONFIG_HOME" "$HOME" "$LIMULATOR_HOME" "$(command -v swift)"\n'
    )
    private_tool.chmod(0o755)

    guest_data = tmp_path / "vm-data"
    user_home = tmp_path / "user-home"
    environment = {**os.environ, "HOME": str(user_home), "LIMULATOR_HOME": str(guest_data)}
    if config_override == "custom":
        environment["LIMULATOR_XTOOL_CONFIG"] = str(tmp_path / "custom-sdk-config")
    else:
        environment.pop("LIMULATOR_XTOOL_CONFIG", None)

    result = subprocess.run([str(wrapper), "sdk", "status"], env=environment, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert values["SWIFT"] == str(swift)
    assert values["PATH"].split(":", 1)[0] == str(swift_bin)
    expected_config = (
        tmp_path / "custom-sdk-config" if config_override == "custom" else installation / "xtool/config"
    )
    assert values["XDG_CONFIG_HOME"] == str(expected_config)
    assert values["HOME"] == str(user_home)
    assert values["LIMULATOR_HOME"] == str(guest_data)
