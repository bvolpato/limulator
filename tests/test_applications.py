from __future__ import annotations

import plistlib
import stat
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from limulator.applications import AppError, build_app, inspect_app, prepared_ipa


def _version(major: int, minor: int = 0, patch: int = 0) -> int:
    return (major << 16) | (minor << 8) | patch


def _macho(
    *,
    architecture: str = "arm64",
    cpu_subtype: int = 0,
    platform: int | None = 2,
    minimum_os: tuple[int, int, int] = (17, 0, 0),
    sdk: tuple[int, int, int] = (27, 2, 0),
    encrypted: bool = False,
    include_version: bool = True,
) -> bytes:
    cpu_type = {"arm64": 0x0100000C, "x86_64": 0x01000007}[architecture]
    commands: list[bytes] = []
    if include_version:
        if platform is None:
            commands.append(
                struct.pack(
                    "<IIII",
                    0x25,
                    16,
                    _version(*minimum_os),
                    _version(*sdk),
                )
            )
        else:
            commands.append(
                struct.pack(
                    "<IIIIII",
                    0x32,
                    24,
                    platform,
                    _version(*minimum_os),
                    _version(*sdk),
                    0,
                )
            )
    if encrypted:
        commands.append(struct.pack("<IIIIII", 0x2C, 24, 0, 16, 1, 0))
    load_commands = b"".join(commands)
    header = struct.pack(
        "<IiiIIIII",
        0xFEEDFACF,
        cpu_type,
        cpu_subtype,
        2,
        len(commands),
        len(load_commands),
        0,
        0,
    )
    return header + load_commands + b"app-code"


def _fat_macho(slices: tuple[tuple[int, int, bytes], ...]) -> bytes:
    table_end = 8 + len(slices) * 20
    offsets: list[int] = []
    cursor = table_end
    for _cpu_type, _cpu_subtype, binary in slices:
        cursor = (cursor + 3) & ~3
        offsets.append(cursor)
        cursor += len(binary)

    result = bytearray(cursor)
    struct.pack_into(">II", result, 0, 0xCAFEBABE, len(slices))
    for index, ((cpu_type, cpu_subtype, binary), offset) in enumerate(zip(slices, offsets)):
        struct.pack_into(
            ">iiIII",
            result,
            8 + index * 20,
            cpu_type,
            cpu_subtype,
            offset,
            len(binary),
            2,
        )
        result[offset : offset + len(binary)] = binary
    return bytes(result)


def _plist(executable: str = "CounterLab", **overrides: object) -> bytes:
    values: dict[str, object] = {
        "CFBundleIdentifier": "com.example.counterlab",
        "CFBundleName": "CounterLab",
        "CFBundleDisplayName": "Counter Lab",
        "CFBundleExecutable": executable,
        "MinimumOSVersion": "17.0",
        "DTSDKName": "iphoneos27.2",
    }
    values.update(overrides)
    return plistlib.dumps(values, fmt=plistlib.FMT_BINARY)


def _make_app(
    directory: Path,
    *,
    name: str = "CounterLab",
    executable: str = "CounterLab",
    binary: bytes | None = None,
    plist: bytes | None = None,
) -> Path:
    app = directory / f"{name}.app"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plist or _plist(executable))
    (app / executable).write_bytes(binary or _macho())
    (app / executable).chmod(0o755)
    return app


def _write_ipa(
    path: Path,
    *,
    apps: tuple[tuple[str, bytes, bytes], ...] | None = None,
    extra_entries: tuple[tuple[str, bytes], ...] = (),
) -> Path:
    if apps is None:
        apps = (("CounterLab", _plist(), _macho()),)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Payload/", b"")
        for name, info, executable in apps:
            root = f"Payload/{name}.app"
            archive.writestr(f"{root}/", b"")
            archive.writestr(f"{root}/Info.plist", info)
            archive.writestr(f"{root}/CounterLab", executable)
        for name, data in extra_entries:
            archive.writestr(name, data)
    return path


def test_inspect_app_reads_device_slice_and_bundle_metadata(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    info = inspect_app(app, guest_os="iOS 18.1")

    assert info.path == app.resolve()
    assert info.bundle_id == "com.example.counterlab"
    assert info.name == "Counter Lab"
    assert info.minimum_os == "17.0"
    assert info.architectures == ("arm64",)
    assert info.sdk == "27.2"


def test_inspect_app_accepts_universal_arm64_device_slices(tmp_path: Path) -> None:
    app = _make_app(
        tmp_path,
        binary=_fat_macho(
            (
                (0x0100000C, 0, _macho()),
                (0x0100000C, 2, _macho(cpu_subtype=2)),
            )
        ),
    )

    info = inspect_app(app)

    assert info.architectures == ("arm64", "arm64e")


def test_inspect_app_rejects_universal_x86_device_slice(tmp_path: Path) -> None:
    app = _make_app(
        tmp_path,
        binary=_fat_macho(
            (
                (0x0100000C, 0, _macho()),
                (0x01000007, 0, _macho(architecture="x86_64")),
            )
        ),
    )

    with pytest.raises(AppError, match="Unsupported app executable architecture x86_64"):
        inspect_app(app)


def test_inspect_app_accepts_legacy_ios_version_command(tmp_path: Path) -> None:
    app = _make_app(
        tmp_path,
        binary=_macho(platform=None, minimum_os=(12, 1)),
        plist=_plist(MinimumOSVersion="12.1"),
    )

    info = inspect_app(app, guest_os="12.2")

    assert info.minimum_os == "12.1"


@pytest.mark.parametrize(
    ("binary", "guest_os", "message"),
    [
        (_macho(platform=7), None, "Simulator binaries"),
        (_macho(platform=6), None, "Mac Catalyst binaries"),
        (_macho(architecture="x86_64"), None, "Unsupported app executable architecture x86_64"),
        (b"not a Mach-O executable", None, "not a supported Mach-O binary"),
        (_macho(encrypted=True), None, "Encrypted App Store binaries"),
        (_macho(include_version=False), None, "missing an iOS minimum-version"),
        (_macho(minimum_os=(18, 0)), "17.7", "requires iOS 18.0"),
    ],
)
def test_inspect_app_rejects_unsupported_binary_or_guest(
    tmp_path: Path, binary: bytes, guest_os: str | None, message: str
) -> None:
    app = _make_app(tmp_path, binary=binary)

    with pytest.raises(AppError, match=message):
        inspect_app(app, guest_os=guest_os)


def test_inspect_app_rejects_unsafe_executable_path(tmp_path: Path) -> None:
    app = _make_app(tmp_path, plist=_plist("../CounterLab"))

    with pytest.raises(AppError, match="safe bundle-relative name"):
        inspect_app(app)


def test_inspect_app_reports_malformed_xml_plist_as_app_error(tmp_path: Path) -> None:
    app = _make_app(tmp_path, plist=b'<?xml version="1.0"?><plist><dict>')

    with pytest.raises(AppError, match="not a valid property list"):
        inspect_app(app)


def test_inspect_app_rejects_executable_symlink(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    executable = app / "CounterLab"
    payload = executable.read_bytes()
    executable.unlink()
    target = tmp_path / "outside"
    target.write_bytes(payload)
    executable.symlink_to(target)

    with pytest.raises(AppError, match="CFBundleExecutable must be a regular file"):
        inspect_app(app)


def test_inspect_app_rejects_bundle_symlink(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    alias = tmp_path / "Alias.app"
    alias.symlink_to(app, target_is_directory=True)

    with pytest.raises(AppError, match="App artifact must not be a symbolic link"):
        inspect_app(alias)


def test_inspect_app_reads_ipa_without_extracting_it(tmp_path: Path) -> None:
    ipa = _write_ipa(tmp_path / "counterlab.ipa")

    info = inspect_app(ipa, guest_os="17.0")

    assert info.path == ipa.resolve()
    assert info.bundle_id == "com.example.counterlab"
    assert info.architectures == ("arm64",)
    assert not (tmp_path / "Payload").exists()


@pytest.mark.parametrize(
    "unsafe_path",
    ["../outside", "/absolute", "Payload\\CounterLab.app\\evil"],
)
def test_inspect_ipa_rejects_unsafe_paths(tmp_path: Path, unsafe_path: str) -> None:
    ipa = _write_ipa(
        tmp_path / "unsafe.ipa",
        extra_entries=((unsafe_path, b"bad"),),
    )

    with pytest.raises(AppError, match="unsafe archive path|absolute archive path"):
        inspect_app(ipa)


def test_inspect_ipa_rejects_duplicate_paths(tmp_path: Path) -> None:
    ipa = tmp_path / "duplicate.ipa"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(ipa, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Payload/CounterLab.app/Info.plist", _plist())
            archive.writestr("Payload/CounterLab.app/Info.plist", _plist())
            archive.writestr("Payload/CounterLab.app/CounterLab", _macho())

    with pytest.raises(AppError, match="duplicate archive paths"):
        inspect_app(ipa)


def test_inspect_ipa_rejects_multiple_app_bundles(tmp_path: Path) -> None:
    ipa = _write_ipa(
        tmp_path / "multiple.ipa",
        apps=(
            ("CounterLab", _plist(), _macho()),
            ("Other", _plist(), _macho()),
        ),
    )

    with pytest.raises(AppError, match=r"exactly one Payload/\*\.app"):
        inspect_app(ipa)


def test_inspect_ipa_rejects_symbolic_links(tmp_path: Path) -> None:
    ipa = tmp_path / "symlink.ipa"
    link = zipfile.ZipInfo("Payload/CounterLab.app/linked")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(ipa, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Payload/CounterLab.app/Info.plist", _plist())
        archive.writestr("Payload/CounterLab.app/CounterLab", _macho())
        archive.writestr(link, "../../outside")

    with pytest.raises(AppError, match="unsafe symbolic link"):
        inspect_app(ipa)


def test_inspect_ipa_reports_malformed_xml_plist_as_app_error(tmp_path: Path) -> None:
    ipa = _write_ipa(
        tmp_path / "malformed-plist.ipa",
        apps=(("Broken", b'<?xml version="1.0"?><plist><dict>', _macho()),),
    )

    with pytest.raises(AppError, match="not a valid property list"):
        inspect_app(ipa)


def test_inspect_ipa_rejects_high_compression_ratio(tmp_path: Path) -> None:
    ipa = _write_ipa(
        tmp_path / "bomb.ipa",
        extra_entries=(("Payload/repetitive.txt", b"A" * 2_000_000),),
    )

    with pytest.raises(AppError, match="unsafe compression ratio"):
        inspect_app(ipa)


def test_prepared_ipa_packages_resources_and_preserves_executable_mode(
    tmp_path: Path,
) -> None:
    app = _make_app(tmp_path)
    resource = app / "Resources" / "message.txt"
    resource.parent.mkdir()
    resource.write_text("Hello from CounterLab\n", encoding="utf-8")
    resource.chmod(0o640)

    with prepared_ipa(app) as (ipa, info):
        assert ipa.is_file()
        assert info.path == ipa.resolve()
        assert info.name == "Counter Lab"
        with zipfile.ZipFile(ipa) as archive:
            executable = archive.getinfo("Payload/CounterLab.app/CounterLab")
            packed_resource = archive.getinfo("Payload/CounterLab.app/Resources/message.txt")
            assert stat.S_IMODE(executable.external_attr >> 16) == 0o755
            assert stat.S_IMODE(packed_resource.external_attr >> 16) == 0o640
            assert archive.read(packed_resource) == b"Hello from CounterLab\n"
        temporary_path = ipa

    assert not temporary_path.exists()


def test_prepared_ipa_passes_through_existing_ipa(tmp_path: Path) -> None:
    ipa = _write_ipa(tmp_path / "counterlab.ipa")

    with prepared_ipa(ipa) as (prepared, info):
        assert prepared == ipa.resolve()
        assert info.bundle_id == "com.example.counterlab"


def test_prepared_ipa_rejects_app_resource_symlink(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (app / "linked.txt").symlink_to(target)

    with pytest.raises(AppError, match="unsafe symbolic link"):
        with prepared_ipa(app):
            pytest.fail("unsafe app should not be packaged")


def test_build_app_runs_xtool_and_returns_the_new_ipa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "CounterLab"
    project.mkdir()
    (project / "Package.swift").write_text("// test fixture\n", encoding="utf-8")
    calls: list[tuple[list[str], Path]] = []

    def fake_run(args: list[str], *, cwd: Path, check: bool, env=None) -> SimpleNamespace:
        calls.append((args, cwd))
        output = cwd / "xtool" / "CounterLab.ipa"
        output.parent.mkdir()
        _write_ipa(output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("limulator.applications.subprocess.run", fake_run)

    ipa = build_app(project, xtool="/tools/xtool", configuration="release")

    assert calls == [
        (
            ["/tools/xtool", "dev", "build", "--configuration", "release", "--ipa"],
            project.resolve(),
        )
    ]
    assert ipa == (project / "xtool" / "CounterLab.ipa")
    assert inspect_app(ipa).bundle_id == "com.example.counterlab"


def test_build_app_rejects_missing_or_ambiguous_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "CounterLab"
    project.mkdir()
    (project / "Package.swift").write_text("// test fixture\n", encoding="utf-8")
    monkeypatch.setattr(
        "limulator.applications.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    with pytest.raises(AppError, match=r"without producing a new \.ipa"):
        build_app(project, xtool="xtool")

    def create_multiple(args: list[str], *, cwd: Path, check: bool, env=None) -> SimpleNamespace:
        _write_ipa(cwd / "first.ipa")
        _write_ipa(cwd / "second.ipa")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("limulator.applications.subprocess.run", create_multiple)
    with pytest.raises(AppError, match=r"multiple new \.ipa artifacts"):
        build_app(project, xtool="xtool")


def test_build_app_reports_xtool_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "CounterLab"
    project.mkdir()
    (project / "Package.swift").write_text("// test fixture\n", encoding="utf-8")
    monkeypatch.setattr(
        "limulator.applications.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=19),
    )

    with pytest.raises(AppError, match="xtool build failed with exit code 19"):
        build_app(project)


def test_cli_explicit_data_reaches_the_private_build_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import shlex

    from limulator.cli import main

    data = tmp_path / "selected installation"
    executable = data / "tools/bin/xtool"
    executable.parent.mkdir(parents=True)
    template = tmp_path / "template.ipa"
    _write_ipa(template)
    executable.write_text(
        "#!/bin/sh\nset -eu\n"
        f'[ "$LIMULATOR_HOME" = {shlex.quote(str(data))} ] || exit 71\n'
        "mkdir -p xtool\n"
        f"cp {shlex.quote(str(template))} xtool/CounterLab.ipa\n"
    )
    executable.chmod(0o755)
    project = tmp_path / "CounterLab"
    project.mkdir()
    (project / "Package.swift").write_text("// fixture\n")
    monkeypatch.setenv("LIMULATOR_HOME", str(tmp_path / "inherited installation"))
    monkeypatch.delenv("LIMULATOR_PREFIX", raising=False)
    monkeypatch.delenv("LIMULATOR_XTOOL", raising=False)

    assert main(["--data", str(data), "build", str(project)]) == 0
    assert inspect_app(project / "xtool/CounterLab.ipa").bundle_id == "com.example.counterlab"
