"""Inspect and prepare native iOS application bundles for Limulator guests."""

from __future__ import annotations

import mmap
import os
import plistlib
import re
import stat
import struct
import subprocess
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator
from xml.parsers.expat import ExpatError


class AppError(ValueError):
    """Raised when an app artifact is invalid or cannot be built."""


@dataclass(frozen=True, slots=True)
class AppInfo:
    """Metadata for a device-compatible iOS application artifact."""

    path: Path
    bundle_id: str
    name: str
    minimum_os: str
    architectures: tuple[str, ...]
    sdk: str | None = None


_MAX_PLIST_BYTES = 8 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_CONTENT_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES = 1024 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 100_000
_MAX_COMPRESSION_RATIO = 1_000
_MAX_LOAD_COMMANDS = 4_096
_MAX_LOAD_COMMAND_BYTES = 64 * 1024 * 1024

_CPU_TYPE_ARM64 = 0x0100000C
_CPU_TYPE_X86 = 7
_CPU_TYPE_X86_64 = 0x01000007
_CPU_SUBTYPE_ARM64E = 2

_MH_MAGIC = 0xFEEDFACE
_MH_MAGIC_64 = 0xFEEDFACF
_FAT_MAGIC = 0xCAFEBABE
_FAT_MAGIC_64 = 0xCAFEBABF
_FAT_CIGAM = 0xBEBAFECA
_FAT_CIGAM_64 = 0xBFBAFECA

_LC_VERSION_MIN_MACOSX = 0x24
_LC_VERSION_MIN_IPHONEOS = 0x25
_LC_ENCRYPTION_INFO = 0x21
_LC_ENCRYPTION_INFO_64 = 0x2C
_LC_BUILD_VERSION = 0x32

_IOS_DEVICE_PLATFORM = 2
_IOS_SIMULATOR_PLATFORM = 7
_MACCATALYST_PLATFORM = 6


@dataclass(frozen=True, slots=True)
class _SliceInfo:
    architecture: str
    platform: int
    minimum_os: tuple[int, int, int]
    sdk: tuple[int, int, int] | None


def inspect_app(path: Path, guest_os: str | None = None) -> AppInfo:
    """Inspect an iOS ``.app`` directory or a single-app ``.ipa`` archive.

    ``AppInfo.path`` is the resolved path supplied by the caller. For an IPA,
    metadata is read directly from the archive without extracting its contents.
    """

    source_path = Path(path)
    try:
        source_mode = source_path.lstat().st_mode
        if stat.S_ISLNK(source_mode):
            raise AppError(f"App artifact must not be a symbolic link: {path}")
        artifact = source_path.resolve(strict=True)
        mode = artifact.lstat().st_mode
    except AppError:
        raise
    except OSError as error:
        raise AppError(f"App artifact does not exist or cannot be accessed: {path}") from error

    if stat.S_ISLNK(mode):
        raise AppError(f"App artifact must not be a symbolic link: {path}")

    if stat.S_ISDIR(mode) and artifact.suffix.lower() == ".app":
        plist, executable, display_path = _read_app_directory(artifact)
        return _build_app_info(display_path, plist, executable, guest_os)

    if stat.S_ISREG(mode) and artifact.suffix.lower() == ".ipa":
        plist, executable_file, display_path = _read_ipa(artifact)
        with executable_file:
            return _build_app_info(display_path, plist, executable_file, guest_os)

    raise AppError(f"Expected an iOS .app directory or .ipa file: {path}")


@contextmanager
def prepared_ipa(path: Path) -> Iterator[tuple[Path, AppInfo]]:
    """Yield an existing IPA or package an app bundle in a private temp dir."""

    artifact = Path(path)
    if artifact.suffix.lower() == ".ipa":
        info = inspect_app(artifact)
        yield info.path, info
        return

    info = inspect_app(artifact)
    if not info.path.is_dir() or info.path.suffix.lower() != ".app":
        raise AppError(f"Expected an iOS .app directory or .ipa file: {path}")

    with tempfile.TemporaryDirectory(prefix="limulator-ipa-") as temporary:
        ipa_path = Path(temporary) / f"{info.path.stem}.ipa"
        _package_app(info.path, ipa_path)
        packaged = inspect_app(ipa_path)
        yield ipa_path, packaged


def build_app(
    project: Path,
    xtool: Path | str = "xtool",
    configuration: str = "debug",
    *,
    env: dict[str, str] | None = None,
) -> Path:
    """Build a SwiftPM iOS app and return the one new IPA produced by xtool."""

    project_path = Path(project)
    try:
        project_path = project_path.resolve(strict=True)
    except OSError as error:
        raise AppError(f"SwiftPM project does not exist: {project}") from error
    if not project_path.is_dir():
        raise AppError(f"SwiftPM project is not a directory: {project}")
    if not (project_path / "Package.swift").is_file():
        raise AppError(f"SwiftPM project is missing Package.swift: {project_path}")
    if configuration not in {"debug", "release"}:
        raise AppError("Build configuration must be 'debug' or 'release'.")

    executable = os.fspath(xtool)
    if isinstance(xtool, Path) or os.sep in executable:
        executable_path = Path(executable).expanduser()
        if not executable_path.is_absolute():
            executable_path = executable_path.resolve()
        executable = os.fspath(executable_path)

    before = _ipa_snapshot(project_path)
    try:
        result = subprocess.run(
            [executable, "dev", "build", "--configuration", configuration, "--ipa"],
            cwd=project_path,
            check=False,
            env=env,
        )
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise AppError(f"Could not run xtool executable: {executable}") from error
        raise AppError(f"Could not run xtool: {error}") from error

    if result.returncode:
        raise AppError(f"xtool build failed with exit code {result.returncode}.")

    after = _ipa_snapshot(project_path)
    changed = [path for path, signature in after.items() if before.get(path) != signature]
    if not changed:
        raise AppError("xtool completed without producing a new .ipa artifact.")
    if len(changed) != 1:
        artifacts = ", ".join(str(path) for path in sorted(changed))
        raise AppError(f"xtool produced multiple new .ipa artifacts: {artifacts}")

    ipa_path = changed[0]
    try:
        mode = ipa_path.lstat().st_mode
    except OSError as error:
        raise AppError(f"xtool IPA artifact cannot be accessed: {ipa_path}") from error
    if not stat.S_ISREG(mode):
        raise AppError(f"xtool IPA artifact is not a regular file: {ipa_path}")

    inspect_app(ipa_path)
    return ipa_path


def _build_app_info(
    artifact: Path,
    plist: dict[str, object],
    executable_source: Path | BinaryIO,
    guest_os: str | None,
) -> AppInfo:
    bundle_id = _required_string(plist, "CFBundleIdentifier")
    name = _optional_string(plist, "CFBundleDisplayName") or _optional_string(plist, "CFBundleName")
    if not name:
        raise AppError("Info.plist is missing CFBundleDisplayName or CFBundleName.")

    executable_name = _required_string(plist, "CFBundleExecutable")
    if (
        executable_name in {".", ".."}
        or "/" in executable_name
        or "\\" in executable_name
        or "\x00" in executable_name
    ):
        raise AppError("Info.plist CFBundleExecutable must be a safe bundle-relative name.")

    slices = _inspect_executable(executable_source)
    architectures = tuple(sorted({item.architecture for item in slices}))
    minimum_os = max(item.minimum_os for item in slices)
    sdk_versions = [item.sdk for item in slices if item.sdk is not None]
    sdk = _format_version(max(sdk_versions)) if sdk_versions else None

    plist_minimum = _optional_string(plist, "MinimumOSVersion")
    if plist_minimum:
        try:
            plist_version = _parse_version(plist_minimum)
        except ValueError as error:
            raise AppError(f"Info.plist MinimumOSVersion is invalid: {plist_minimum!r}.") from error
        minimum_os = max(minimum_os, plist_version)

    if sdk is None:
        sdk = _sdk_from_plist(_optional_string(plist, "DTSDKName"))

    if guest_os is not None:
        try:
            guest_version = _parse_guest_version(guest_os)
        except ValueError as error:
            raise AppError(f"Guest OS version is invalid: {guest_os!r}.") from error
        if minimum_os > guest_version:
            raise AppError(
                f"App requires iOS {_format_version(minimum_os)}, but the guest runs iOS {guest_os}."
            )

    return AppInfo(
        path=artifact,
        bundle_id=bundle_id,
        name=name,
        minimum_os=_format_version(minimum_os),
        architectures=architectures,
        sdk=sdk,
    )


def _read_app_directory(
    app_path: Path,
) -> tuple[dict[str, object], Path, Path]:
    try:
        root_mode = app_path.lstat().st_mode
    except OSError as error:
        raise AppError(f"App bundle cannot be accessed: {app_path}") from error
    if not stat.S_ISDIR(root_mode):
        raise AppError(f"App bundle is not a directory: {app_path}")

    root = app_path.resolve(strict=True)
    plist_path = root / "Info.plist"
    plist = _load_plist_file(plist_path, root)
    executable_name = _required_string(plist, "CFBundleExecutable")
    if (
        executable_name in {".", ".."}
        or "/" in executable_name
        or "\\" in executable_name
        or "\x00" in executable_name
    ):
        raise AppError("Info.plist CFBundleExecutable must be a safe bundle-relative name.")

    executable_path = root / executable_name
    _ensure_regular_file_inside(executable_path, root, "CFBundleExecutable")
    return plist, executable_path, root


def _load_plist_file(path: Path, root: Path) -> dict[str, object]:
    _ensure_regular_file_inside(path, root, "Info.plist")
    try:
        size = path.stat().st_size
        if size > _MAX_PLIST_BYTES:
            raise AppError(f"Info.plist exceeds the {_MAX_PLIST_BYTES}-byte size limit.")
        with path.open("rb") as stream:
            return _load_plist(stream)
    except AppError:
        raise
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise AppError(f"Could not read Info.plist: {path}") from error


def _load_plist(stream: BinaryIO) -> dict[str, object]:
    data = stream.read(_MAX_PLIST_BYTES + 1)
    if len(data) > _MAX_PLIST_BYTES:
        raise AppError(f"Info.plist exceeds the {_MAX_PLIST_BYTES}-byte size limit.")
    try:
        value = plistlib.loads(data)
    except (plistlib.InvalidFileException, ExpatError, ValueError, TypeError) as error:
        raise AppError("Info.plist is not a valid property list.") from error
    if not isinstance(value, dict):
        raise AppError("Info.plist must contain a dictionary.")
    return value


def _ensure_regular_file_inside(path: Path, root: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise AppError(f"{label} does not exist: {path}") from error
    if not stat.S_ISREG(mode):
        raise AppError(f"{label} must be a regular file: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AppError(f"{label} cannot be resolved: {path}") from error
    if not resolved.is_relative_to(root):
        raise AppError(f"{label} resolves outside the app bundle: {path}")


def _read_ipa(ipa_path: Path) -> tuple[dict[str, object], BinaryIO, Path]:
    try:
        archive_size = ipa_path.stat().st_size
        if archive_size > _MAX_ARCHIVE_BYTES:
            raise AppError(f"IPA exceeds the {_MAX_ARCHIVE_BYTES}-byte archive size limit.")
        archive = zipfile.ZipFile(ipa_path)
    except AppError:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise AppError(f"IPA is not a readable ZIP archive: {ipa_path}") from error

    temporary_file = tempfile.TemporaryFile(mode="w+b")
    try:
        with archive:
            entries = _validate_archive(archive)
            app_roots = _find_app_roots(entries)
            if len(app_roots) != 1:
                if not app_roots:
                    raise AppError("IPA must contain a real Payload/*.app bundle.")
                raise AppError("IPA must contain exactly one Payload/*.app bundle.")

            app_root = next(iter(app_roots))
            root_entry = entries.get(app_root)
            if root_entry is not None and not root_entry.is_dir():
                raise AppError(f"IPA app bundle path is not a directory: {app_root}")

            plist_name = f"{app_root}/Info.plist"
            plist_entry = entries.get(plist_name)
            if plist_entry is None or plist_entry.is_dir():
                raise AppError(f"IPA app bundle is missing Info.plist: {app_root}")
            if plist_entry.file_size > _MAX_PLIST_BYTES:
                raise AppError(f"Info.plist exceeds the {_MAX_PLIST_BYTES}-byte size limit.")
            with archive.open(plist_entry) as plist_stream:
                plist = _load_plist(plist_stream)

            executable_name = _required_string(plist, "CFBundleExecutable")
            if (
                executable_name in {".", ".."}
                or "/" in executable_name
                or "\\" in executable_name
                or "\x00" in executable_name
            ):
                raise AppError("Info.plist CFBundleExecutable must be a safe bundle-relative name.")
            executable_name_in_archive = f"{app_root}/{executable_name}"
            executable_entry = entries.get(executable_name_in_archive)
            if executable_entry is None or executable_entry.is_dir():
                raise AppError(
                    f"IPA app bundle is missing its CFBundleExecutable file: {executable_name_in_archive}"
                )
            if executable_entry.file_size > _MAX_EXECUTABLE_BYTES:
                raise AppError(f"App executable exceeds the {_MAX_EXECUTABLE_BYTES}-byte size limit.")

            try:
                with archive.open(executable_entry) as source:
                    total = 0
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        if total > _MAX_EXECUTABLE_BYTES:
                            raise AppError(
                                f"App executable exceeds the {_MAX_EXECUTABLE_BYTES}-byte size limit."
                            )
                        temporary_file.write(chunk)
            except AppError:
                raise
            except (OSError, RuntimeError, zipfile.BadZipFile, EOFError) as error:
                raise AppError("Could not read the IPA app executable.") from error
            if total != executable_entry.file_size:
                raise AppError("IPA app executable has an inconsistent uncompressed size.")
            temporary_file.seek(0)
            return plist, temporary_file, ipa_path
    except BaseException:
        temporary_file.close()
        raise


def _validate_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise AppError(f"IPA has more than {_MAX_ARCHIVE_ENTRIES} archive entries.")

    entries: dict[str, zipfile.ZipInfo] = {}
    folded_names: set[str] = set()
    total_content = 0
    for info in infos:
        parts, normalized = _safe_zip_path(info)
        if normalized in folded_names:
            raise AppError(f"IPA contains duplicate archive paths: {info.filename}")
        folded_names.add(normalized)

        if info.flag_bits & 0x1:
            raise AppError(f"IPA contains an encrypted archive entry: {info.filename}")
        mode_type = stat.S_IFMT(info.external_attr >> 16)
        if mode_type == stat.S_IFLNK:
            raise AppError(f"IPA contains an unsafe symbolic link: {info.filename}")
        if mode_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise AppError(f"IPA contains an unsupported file type: {info.filename}")
        if info.is_dir() and mode_type not in {0, stat.S_IFDIR}:
            raise AppError(f"IPA directory entry has an invalid file type: {info.filename}")
        if not info.is_dir() and mode_type == stat.S_IFDIR:
            raise AppError(f"IPA file entry has an invalid directory type: {info.filename}")
        if info.file_size < 0 or info.compress_size < 0:
            raise AppError(f"IPA has invalid size metadata: {info.filename}")
        if info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise AppError(f"IPA archive entry is too large: {info.filename}")
        total_content += info.file_size
        if total_content > _MAX_ARCHIVE_CONTENT_BYTES:
            raise AppError(f"IPA exceeds the {_MAX_ARCHIVE_CONTENT_BYTES}-byte uncompressed content limit.")
        if info.file_size and (
            info.compress_size == 0 or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
        ):
            raise AppError(f"IPA archive entry has an unsafe compression ratio: {info.filename}")

        key = "/".join(parts)
        entries[key] = info

    return entries


def _safe_zip_path(info: zipfile.ZipInfo) -> tuple[tuple[str, ...], str]:
    name = info.filename
    if not name or "\x00" in info.orig_filename or "\\" in name:
        raise AppError(f"IPA contains an unsafe archive path: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise AppError(f"IPA contains an absolute archive path: {name!r}")

    trimmed = name[:-1] if name.endswith("/") else name
    parts = tuple(trimmed.split("/"))
    if not trimmed or any(part in {"", ".", ".."} for part in parts):
        raise AppError(f"IPA contains an unsafe archive path: {name!r}")

    canonical = unicodedata.normalize("NFC", "/".join(parts)).casefold()
    return parts, canonical


def _find_app_roots(entries: dict[str, zipfile.ZipInfo]) -> set[str]:
    roots: set[str] = set()
    for name in entries:
        parts = name.split("/")
        if len(parts) >= 2 and parts[0] == "Payload" and parts[1].lower().endswith(".app"):
            roots.add("/".join(parts[:2]))
    return roots


def _inspect_executable(source: Path | BinaryIO) -> tuple[_SliceInfo, ...]:
    try:
        if isinstance(source, Path):
            size = source.stat().st_size
            if size > _MAX_EXECUTABLE_BYTES:
                raise AppError(f"App executable exceeds the {_MAX_EXECUTABLE_BYTES}-byte size limit.")
            with source.open("rb") as stream:
                return _inspect_executable_stream(stream, size)
        size = _stream_size(source)
        if size > _MAX_EXECUTABLE_BYTES:
            raise AppError(f"App executable exceeds the {_MAX_EXECUTABLE_BYTES}-byte size limit.")
        return _inspect_executable_stream(source, size)
    except AppError:
        raise
    except (OSError, ValueError, struct.error, BufferError) as error:
        raise AppError("Could not inspect the app executable as a Mach-O binary.") from error


def _stream_size(stream: BinaryIO) -> int:
    current = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(current)
    return size


def _inspect_executable_stream(stream: BinaryIO, size: int) -> tuple[_SliceInfo, ...]:
    if size == 0:
        raise AppError("App executable is empty, not a Mach-O binary.")
    with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        view = memoryview(mapped)
        try:
            return _parse_macho(view)
        finally:
            view.release()


def _parse_macho(data: memoryview) -> tuple[_SliceInfo, ...]:
    if len(data) < 4:
        raise AppError("App executable is not a Mach-O binary.")
    magic = int.from_bytes(data[:4], "big")
    if magic in {_FAT_MAGIC, _FAT_MAGIC_64, _FAT_CIGAM, _FAT_CIGAM_64}:
        return _parse_fat_macho(data, magic)
    return (_parse_thin_macho(data, 0, len(data), None),)


def _parse_fat_macho(data: memoryview, magic: int) -> tuple[_SliceInfo, ...]:
    endian = ">" if magic in {_FAT_MAGIC, _FAT_MAGIC_64} else "<"
    is_64 = magic in {_FAT_MAGIC_64, _FAT_CIGAM_64}
    if len(data) < 8:
        raise AppError("App executable has a truncated universal Mach-O header.")
    _fat_magic, count = struct.unpack_from(f"{endian}II", data, 0)
    if not count or count > 32:
        raise AppError(f"App executable has an invalid universal slice count: {count}.")

    entry_format = f"{endian}iiQQII" if is_64 else f"{endian}iiIII"
    entry_size = struct.calcsize(entry_format)
    table_end = 8 + count * entry_size
    if table_end > len(data):
        raise AppError("App executable has a truncated universal architecture table.")

    slices: list[_SliceInfo] = []
    ranges: list[tuple[int, int]] = []
    seen_architectures: set[tuple[int, int]] = set()
    for index in range(count):
        values = struct.unpack_from(entry_format, data, 8 + index * entry_size)
        cpu_type, cpu_subtype = values[:2]
        slice_offset, slice_size = values[2:4]
        alignment = values[4]
        if alignment > 31 or slice_offset < table_end or slice_size < 4:
            raise AppError("App executable has invalid universal architecture metadata.")
        slice_end = slice_offset + slice_size
        if slice_end > len(data):
            raise AppError("App executable has a universal slice outside the file bounds.")
        architecture_key = (cpu_type, cpu_subtype & 0x00FFFFFF)
        if architecture_key in seen_architectures:
            raise AppError("App executable has duplicate universal architecture slices.")
        seen_architectures.add(architecture_key)
        ranges.append((slice_offset, slice_end))
        slices.append(
            _parse_thin_macho(
                data,
                slice_offset,
                slice_end,
                cpu_type,
                cpu_subtype,
            )
        )

    ranges.sort()
    if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
        raise AppError("App executable has overlapping universal architecture slices.")
    return tuple(slices)


def _parse_thin_macho(
    data: memoryview,
    start: int,
    end: int,
    expected_cpu_type: int | None,
    expected_cpu_subtype: int | None = None,
) -> _SliceInfo:
    if end - start < 4:
        raise AppError("App executable contains a truncated Mach-O slice.")
    raw_magic = bytes(data[start : start + 4])
    if raw_magic == b"\xcf\xfa\xed\xfe":
        endian, magic, is_64 = "<", _MH_MAGIC_64, True
    elif raw_magic == b"\xce\xfa\xed\xfe":
        endian, magic, is_64 = "<", _MH_MAGIC, False
    elif raw_magic == b"\xfe\xed\xfa\xcf":
        endian, magic, is_64 = ">", _MH_MAGIC_64, True
    elif raw_magic == b"\xfe\xed\xfa\xce":
        endian, magic, is_64 = ">", _MH_MAGIC, False
    else:
        raise AppError("App executable is not a supported Mach-O binary.")

    header_size = 32 if is_64 else 28
    if end - start < header_size:
        raise AppError("App executable contains a truncated Mach-O header.")
    if is_64:
        unpacked = struct.unpack_from(f"{endian}IiiIIIII", data, start)
    else:
        unpacked = struct.unpack_from(f"{endian}IiiIIII", data, start)
    actual_magic, cpu_type, cpu_subtype, file_type, command_count, command_bytes = unpacked[:6]
    if actual_magic != magic:
        raise AppError("App executable has inconsistent Mach-O header metadata.")
    if expected_cpu_type is not None and cpu_type != expected_cpu_type:
        raise AppError("App executable universal architecture metadata does not match its slice.")
    if expected_cpu_subtype is not None and cpu_subtype & 0x00FFFFFF != expected_cpu_subtype & 0x00FFFFFF:
        raise AppError("App executable universal architecture metadata does not match its slice.")
    if file_type != 2:
        raise AppError("App executable Mach-O file type is not an executable.")

    architecture = _architecture_name(cpu_type, cpu_subtype)
    if architecture not in {"arm64", "arm64e"}:
        raise AppError(
            f"Unsupported app executable architecture {architecture}; an arm64 iOS device slice is required."
        )
    if not is_64:
        raise AppError("App executable must use a 64-bit arm64 Mach-O header.")

    if command_count > _MAX_LOAD_COMMANDS or command_bytes > _MAX_LOAD_COMMAND_BYTES:
        raise AppError("App executable has too many or oversized Mach-O load commands.")
    commands_start = start + header_size
    commands_end = commands_start + command_bytes
    if commands_end > end:
        raise AppError("App executable has load commands outside its Mach-O slice.")

    build_versions: list[tuple[int, tuple[int, int, int], tuple[int, int, int] | None]] = []
    legacy_versions: list[tuple[int, int, int]] = []
    legacy_sdks: list[tuple[int, int, int]] = []
    offset = commands_start
    for _ in range(command_count):
        if offset + 8 > commands_end:
            raise AppError("App executable has a truncated Mach-O load command.")
        raw_command, command_size = struct.unpack_from(f"{endian}II", data, offset)
        command = raw_command & 0x7FFFFFFF
        if command_size < 8 or offset + command_size > commands_end:
            raise AppError("App executable has an invalid Mach-O load command size.")

        if command == _LC_BUILD_VERSION:
            if command_size < 24:
                raise AppError("App executable has a truncated LC_BUILD_VERSION command.")
            _cmd, _size, platform, minimum_raw, sdk_raw, _tool_count = struct.unpack_from(
                f"{endian}IIIIII", data, offset
            )
            build_versions.append((platform, _decode_version(minimum_raw), _decode_optional_version(sdk_raw)))
        elif command == _LC_VERSION_MIN_IPHONEOS:
            if command_size < 16:
                raise AppError("App executable has a truncated LC_VERSION_MIN_IPHONEOS command.")
            _cmd, _size, minimum_raw, sdk_raw = struct.unpack_from(f"{endian}IIII", data, offset)
            legacy_versions.append(_decode_version(minimum_raw))
            sdk_version = _decode_optional_version(sdk_raw)
            if sdk_version is not None:
                legacy_sdks.append(sdk_version)
        elif command == _LC_VERSION_MIN_MACOSX:
            raise AppError("Mac Catalyst and macOS app binaries are not supported.")
        elif command in {_LC_ENCRYPTION_INFO, _LC_ENCRYPTION_INFO_64}:
            if command_size < (24 if command == _LC_ENCRYPTION_INFO_64 else 20):
                raise AppError("App executable has a truncated encryption load command.")
            crypt_id = struct.unpack_from(f"{endian}I", data, offset + 16)[0]
            if crypt_id:
                raise AppError("Encrypted App Store binaries are not supported.")

        offset += command_size

    if offset != commands_end:
        raise AppError("App executable load command sizes do not match the Mach-O header.")

    if build_versions:
        if len(build_versions) != 1:
            raise AppError("App executable has multiple LC_BUILD_VERSION commands.")
        platform, minimum_os, sdk = build_versions[0]
        if platform == _IOS_SIMULATOR_PLATFORM:
            raise AppError("iOS Simulator binaries are not supported; an iOS device app is required.")
        if platform == _MACCATALYST_PLATFORM:
            raise AppError("Mac Catalyst binaries are not supported; an iOS device app is required.")
        if platform != _IOS_DEVICE_PLATFORM:
            raise AppError(f"Unsupported Mach-O platform {platform}; iOS device platform 2 is required.")
    else:
        if not legacy_versions:
            raise AppError("App executable is missing an iOS minimum-version load command.")
        if len(legacy_versions) != 1:
            raise AppError("App executable has multiple LC_VERSION_MIN_IPHONEOS commands.")
        minimum_os = legacy_versions[0]
        sdk = max(legacy_sdks) if legacy_sdks else None

    if minimum_os == (0, 0, 0):
        raise AppError("App executable has an invalid iOS minimum version of 0.0.")
    return _SliceInfo(architecture, _IOS_DEVICE_PLATFORM, minimum_os, sdk)


def _architecture_name(cpu_type: int, cpu_subtype: int) -> str:
    if cpu_type == _CPU_TYPE_ARM64:
        return "arm64e" if cpu_subtype & 0x00FFFFFF == _CPU_SUBTYPE_ARM64E else "arm64"
    if cpu_type == _CPU_TYPE_X86_64:
        return "x86_64"
    if cpu_type == _CPU_TYPE_X86:
        return "i386"
    return f"cpu-{cpu_type:#x}"


def _decode_version(value: int) -> tuple[int, int, int]:
    return (value >> 16, (value >> 8) & 0xFF, value & 0xFF)


def _decode_optional_version(value: int) -> tuple[int, int, int] | None:
    version = _decode_version(value)
    return version if version != (0, 0, 0) else None


def _parse_version(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(".")
    if not 1 <= len(parts) <= 3 or any(not part.isdecimal() for part in parts):
        raise ValueError(value)
    numbers = tuple(int(part) for part in parts)
    return (numbers + (0, 0))[:3]


def _parse_guest_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"\s*(?:iOS\s*)?(\d+(?:\.\d+){0,2})\s*", value, re.IGNORECASE)
    if match is None:
        raise ValueError(value)
    return _parse_version(match.group(1))


def _format_version(value: tuple[int, int, int]) -> str:
    major, minor, patch = value
    if patch:
        return f"{major}.{minor}.{patch}"
    return f"{major}.{minor}"


def _required_string(plist: dict[str, object], key: str) -> str:
    value = plist.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AppError(f"Info.plist is missing a valid {key} value.")
    return value.strip()


def _optional_string(plist: dict[str, object], key: str) -> str | None:
    value = plist.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _sdk_from_plist(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?:iphoneos|iOS)?([0-9]+(?:\.[0-9]+){0,2})", value, re.IGNORECASE)
    if match is None:
        return None
    try:
        return _format_version(_parse_version(match.group(1)))
    except ValueError:
        return None


def _ipa_snapshot(project: Path) -> dict[Path, tuple[int, int, int, int]]:
    snapshot: dict[Path, tuple[int, int, int, int]] = {}
    for current, directories, files in os.walk(project):
        directories[:] = [directory for directory in directories if directory != ".git"]
        for filename in files:
            if Path(filename).suffix.lower() != ".ipa":
                continue
            path = Path(current) / filename
            try:
                metadata = path.lstat()
            except OSError:
                continue
            snapshot[path] = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
    return snapshot


def _package_app(app_path: Path, ipa_path: Path) -> None:
    root = app_path.resolve(strict=True)
    try:
        root_mode = root.lstat().st_mode
    except OSError as error:
        raise AppError(f"App bundle cannot be accessed: {app_path}") from error
    if not stat.S_ISDIR(root_mode) or root.suffix.lower() != ".app":
        raise AppError(f"Expected an iOS .app directory: {app_path}")

    prefix = f"Payload/{root.name}"
    paths = sorted(root.rglob("*"), key=lambda item: item.as_posix())
    if len(paths) + 2 > _MAX_ARCHIVE_ENTRIES:
        raise AppError(f"App bundle has more than {_MAX_ARCHIVE_ENTRIES} resources.")

    resources: list[tuple[Path, str, int]] = []
    total_content = 0
    for path in paths:
        relative = path.relative_to(root)
        parts = relative.parts
        if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
            raise AppError(f"App bundle contains an unsafe resource path: {path}")
        arcname = f"{prefix}/{relative.as_posix()}"
        if len(arcname.encode("utf-8")) > 4_096:
            raise AppError(f"App bundle resource path is too long: {path}")
        try:
            metadata = path.lstat()
        except OSError as error:
            raise AppError(f"App bundle resource cannot be accessed: {path}") from error
        mode_type = stat.S_IFMT(metadata.st_mode)
        if mode_type == stat.S_IFLNK:
            raise AppError(f"App bundle contains an unsafe symbolic link: {path}")
        if mode_type == stat.S_IFREG:
            if metadata.st_size > _MAX_ARCHIVE_MEMBER_BYTES:
                raise AppError(f"App bundle resource is too large: {path}")
            total_content += metadata.st_size
            if total_content > _MAX_ARCHIVE_CONTENT_BYTES:
                raise AppError(f"App bundle exceeds the {_MAX_ARCHIVE_CONTENT_BYTES}-byte size limit.")
        elif mode_type != stat.S_IFDIR:
            raise AppError(f"App bundle contains an unsupported resource type: {path}")
        resources.append((path, arcname, metadata.st_mode))

    try:
        with zipfile.ZipFile(
            ipa_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            strict_timestamps=False,
        ) as archive:
            _write_zip_directory(archive, "Payload/", 0o755)
            _write_zip_directory(archive, f"{prefix}/", stat.S_IMODE(root_mode))
            for path, arcname, mode in resources:
                mode_type = stat.S_IFMT(mode)
                if mode_type == stat.S_IFDIR:
                    _write_zip_directory(archive, f"{arcname}/", stat.S_IMODE(mode))
                elif mode_type == stat.S_IFREG:
                    archive.write(path, arcname=arcname)
    except AppError:
        raise
    except (OSError, zipfile.BadZipFile, ValueError) as error:
        raise AppError(f"Could not package app bundle as an IPA: {error}") from error


def _write_zip_directory(archive: zipfile.ZipFile, name: str, permissions: int) -> None:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = ((stat.S_IFDIR | permissions) << 16) | 0x10
    archive.writestr(info, b"")
