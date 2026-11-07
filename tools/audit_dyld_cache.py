#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only, whole-family dyld cache page-slot audit. Exit 1 unless coverage is complete.

Formats: vphone-cli 83197152137ddd2aad70eb121d6b88a9afbee36e,
DyldSharedCacheCodeSignature.swift, and its MachOKit dependency f0c6565435659aeb23369810874578d5344d13b7,
MachOKitC/include/{dyld_cache_format.h,codesign.h}. This checks SHA-256 code-page
slots, not CMS trust, special slots, or whether modified instructions are correct.
The exact .atlas sidecar is separately fingerprinted, not signature-audited:
Apple dyld's NewSharedCacheBuilder.cpp builds it as introspection metadata after
codeSign() and returns it separately from CacheBuffers. Its archive contents are
outside this audit's scope.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import struct
from contextlib import contextmanager
from pathlib import Path


class AuditError(ValueError):
    pass


ATLAS_SOURCE = (
    "https://github.com/apple-oss-distributions/dyld/blob/"
    "fd8d0c4d52320ebf64db34f3cb280310d905c5ae/cache_builder/NewSharedCacheBuilder.cpp#L8708-L8812"
)


def require(condition, message):
    if not condition:
        raise AuditError(message)


def read_at(handle, offset, length):
    handle.seek(offset)
    data = handle.read(length)
    require(len(data) == length, f"short read at {offset:#x}: wanted {length}, got {len(data)}")
    return data


def bounded(offset, length, limit, label):
    require(0 <= offset <= limit and 0 <= length <= limit - offset, f"{label} outside bounds")


@contextmanager
def open_chunk(path):
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "chunk is not a regular file")
        yield handle


def identity(handle):
    metadata = os.fstat(handle.fileno())
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def cache_header(handle, size):
    require(size >= 0x68, "truncated dyld header")
    head = read_at(handle, 0, 0x68)
    magic = head[:16]
    require(magic.startswith(b"dyld_v1") and b"arm64" in magic, "unsupported dyld magic/architecture")
    mapping_offset, mapping_count = struct.unpack_from("<II", head, 0x10)
    require(0x68 <= mapping_offset <= 0x10000, "unsupported dyld header size")
    require(1 <= mapping_count <= 64, "invalid mapping count")
    bounded(mapping_offset, mapping_count * 32, size, "mapping table")
    return read_at(handle, 0, mapping_offset)


def inventory(main):
    """Use the declared family, including UUIDs, and reject unlisted chunk files."""
    require(re.fullmatch(r"dyld_shared_cache_[A-Za-z0-9_]+", main.name), "expected a main cache path")
    with open_chunk(main) as handle:
        before = identity(handle)
        head = cache_header(handle, before[2])
        entries = [(main, head[0x58:0x68].hex())]
        # dyld_cache_header uses mappingOffset as its available header size.
        if len(head) >= 0x190:
            offset, count = struct.unpack_from("<II", head, 0x188)
            require(count <= 4096, "unsupported subcache count")
            require(bool(offset) == bool(count), "inconsistent subcache table")
            modern = len(head) >= 0x1CC  # cacheSubType present: entries carry a 32-byte suffix.
            entry_size = 56 if modern else 24
            bounded(offset, count * entry_size, before[2], "subcache table")
            for index in range(count):
                entry = read_at(handle, offset + index * entry_size, entry_size)
                if modern:
                    suffix_bytes, terminator, _ = entry[24:56].partition(b"\0")
                    require(bool(terminator), "unterminated subcache suffix")
                    suffix = suffix_bytes.decode("ascii")
                    require(re.fullmatch(r"\.[A-Za-z0-9_.-]+", suffix), "unsafe subcache suffix")
                    require(
                        ".." not in suffix and suffix not in {".symbols", ".map"}, "invalid subcache suffix"
                    )
                else:
                    suffix = f".{index + 1}"
                entries.append((main.with_name(main.name + suffix), entry[:16].hex()))
        require(identity(handle) == before, "main cache changed during inventory")
    names = [path.name for path, _ in entries]
    require(len(names) == len(set(names)), "duplicate subcache name")
    uuids = [uuid for _, uuid in entries]
    require(all(uuid != "00" * 16 for uuid in uuids), "zero cache UUID")
    require(len(uuids) == len(set(uuids)), "duplicate cache UUID")
    excluded = []
    sidecars = []
    for path in sorted(main.parent.iterdir()):
        if not path.name.startswith(main.name + "."):
            continue
        if path.name.endswith((".symbols", ".map")):
            excluded.append(path.name)
        elif path.name == main.name + ".atlas":
            require(path.name not in names, "atlas is declared as a subcache; refusing exclusion")
            sidecars.append(atlas_metadata(path))
            excluded.append(path.name)
        else:
            require(path.name in names, f"unlisted cache family file: {path.name}")
    return entries, excluded, sidecars


def atlas_metadata(path):
    """Account for the known metadata file without claiming its archive is valid."""
    with open_chunk(path) as handle:
        before = identity(handle)
        require(before[2] > 0, "empty atlas sidecar")
        prefix = handle.read(16)
        require(not prefix.startswith(b"dyld"), "atlas has a dyld cache header; refusing exclusion")
        handle.seek(0)
        digest = hashlib.sha256()
        total = 0
        while data := handle.read(1024 * 1024):
            total += len(data)
            digest.update(data)
        require(total == before[2] and identity(handle) == before, "atlas changed or truncated during audit")
    return {
        "chunk": path.name,
        "reason": "unsigned introspection metadata sidecar, separate from signed cache chunks",
        "source": ATLAS_SOURCE,
        "size": total,
        "sha256": digest.hexdigest(),
        "prefix_hex": prefix.hex(),
        "page_signature_audited": False,
        "archive_contents_validated": False,
    }


def terminated_string(handle, blob_offset, offset, end, header_size, label):
    require(header_size <= offset < end, f"invalid {label} offset")
    while offset < end:
        data = read_at(handle, blob_offset + offset, min(4096, end - offset))
        if b"\0" in data:
            return
        offset += len(data)
    raise AuditError(f"unterminated {label}")


def code_directory(handle, head, size):
    signature_offset, signature_size = struct.unpack_from("<QQ", head, 0x28)
    require(signature_offset > 0 and signature_size > 0, "unsigned chunk or unsupported zero-size signature")
    bounded(signature_offset, signature_size, size, "signature")
    require(signature_offset + signature_size == size, "uncovered bytes after signature")
    magic, length, count = struct.unpack(">III", read_at(handle, signature_offset, 12))
    require(magic == 0xFADE0CC0, "unsupported signature superblob magic")
    require(1 <= count <= 256, "invalid superblob count")
    require(12 + count * 8 <= length <= signature_size, "invalid superblob length")
    index = read_at(handle, signature_offset + 12, count * 8)
    blobs = []
    directory = None
    seen_types = set()
    for slot, offset in struct.iter_unpack(">II", index):
        require(slot not in seen_types, "duplicate superblob slot")
        seen_types.add(slot)
        require(offset >= 12 + count * 8, "superblob entry overlaps index")
        bounded(offset, 8, length, "blob header")
        blob_magic, blob_length = struct.unpack(">II", read_at(handle, signature_offset + offset, 8))
        require(blob_length >= 8, "invalid blob length")
        bounded(offset, blob_length, length, "blob")
        require(
            all(offset + blob_length <= start or offset >= end for start, end in blobs), "overlapping blobs"
        )
        blobs.append((offset, offset + blob_length))
        if slot == 0:
            require(blob_magic == 0xFADE0C02, "invalid CodeDirectory magic")
            directory = (signature_offset + offset, blob_length)
        elif blob_magic == 0xFADE0C02 or 0x1000 <= slot < 0x1005:
            raise AuditError("unsupported alternate CodeDirectory")
    require(directory is not None, "missing CodeDirectory")
    blob_offset, blob_length = directory
    require(blob_length >= 44, "truncated CodeDirectory")
    cd = read_at(handle, blob_offset, min(blob_length, 108))
    version = struct.unpack_from(">I", cd, 8)[0]
    header_sizes = {
        0x20000: 44,
        0x20001: 44,
        0x20100: 48,
        0x20200: 52,
        0x20300: 64,
        0x20400: 88,
        0x20500: 96,
        0x20600: 108,
    }
    require(version in header_sizes, f"unsupported CodeDirectory version {version:#x}")
    header_size = header_sizes[version]
    require(blob_length >= header_size, "truncated versioned CodeDirectory")
    hash_offset, ident_offset, special, slots, code_limit = struct.unpack_from(">IIIII", cd, 16)
    hash_size, hash_type, _, page_log2 = cd[36:40]
    require(hash_type == 2 and hash_size == 32, f"unsupported hash algorithm/size: {hash_type}/{hash_size}")
    require(12 <= page_log2 <= 20, f"unsupported page exponent {page_log2}")
    require(struct.unpack_from(">I", cd, 40)[0] == 0, "nonzero CodeDirectory spare2")
    if version >= 0x20100:
        require(struct.unpack_from(">I", cd, 44)[0] == 0, "unsupported scatter CodeDirectory")
    if version >= 0x20300:
        require(struct.unpack_from(">I", cd, 52)[0] == 0, "nonzero CodeDirectory spare3")
        require(struct.unpack_from(">Q", cd, 56)[0] == 0, "unsupported 64-bit code limit")
    if version >= 0x20500:
        require(struct.unpack_from(">I", cd, 92)[0] == 0, "unsupported pre-encryption hashes")
    if version >= 0x20600:
        require(cd[96:108] == bytes(12), "unsupported linkage data")
    require(code_limit > 0 and slots > 0, "zero code-page coverage")
    page_size = 1 << page_log2
    require(code_limit == signature_offset, "code limit does not cover all bytes before signature")
    require(slots * page_size == code_limit, "unsupported partial page or inconsistent code-slot count")
    require(hash_offset - special * hash_size >= header_size, "special hashes overlap CodeDirectory header")
    bounded(hash_offset, slots * hash_size, blob_length, "code hash slots")
    strings_end = hash_offset - special * hash_size
    terminated_string(handle, blob_offset, ident_offset, strings_end, header_size, "identifier")
    if version >= 0x20200:
        team_offset = struct.unpack_from(">I", cd, 48)[0]
        if team_offset:
            terminated_string(handle, blob_offset, team_offset, strings_end, header_size, "team identifier")
    return {
        "blob_offset": blob_offset,
        "blob_length": blob_length,
        "hash_offset": hash_offset,
        "hash_size": hash_size,
        "code_slot_count": slots,
        "code_limit": code_limit,
        "page_size": page_size,
        "version": version,
    }


def audit_chunk(path, expected_uuid):
    result = {
        "chunk": path.name,
        "pages_declared": 0,
        "pages_checked": 0,
        "bytes_checked": 0,
        "mismatches": [],
        "errors": [],
    }
    try:
        with open_chunk(path) as handle:
            before = identity(handle)
            head = cache_header(handle, before[2])
            require(head[0x58:0x68].hex() == expected_uuid, "cache UUID differs from main-cache declaration")
            directory = code_directory(handle, head, before[2])
            result["code_directory"] = directory
            result["pages_declared"] = directory["code_slot_count"]
            page_size = directory["page_size"]
            for first in range(0, directory["code_slot_count"], 1024):
                count = min(1024, directory["code_slot_count"] - first)
                hashes = read_at(
                    handle, directory["blob_offset"] + directory["hash_offset"] + first * 32, count * 32
                )
                handle.seek(first * page_size)
                for page in range(first, first + count):
                    data = handle.read(page_size)
                    require(
                        len(data) == page_size, f"short code page {page}: wanted {page_size}, got {len(data)}"
                    )
                    computed = hashlib.sha256(data).digest()
                    start = (page - first) * 32
                    stored = hashes[start : start + 32]
                    result["pages_checked"] += 1
                    result["bytes_checked"] += page_size
                    if computed != stored:
                        result["mismatches"].append(
                            {
                                "page": page,
                                "file_offset": page * page_size,
                                "computed": computed.hex(),
                                "stored": stored.hex(),
                            }
                        )
            require(identity(handle) == before, "chunk changed during audit")
    except (OSError, ValueError) as error:
        result["errors"].append(str(error))
    result["complete"] = not result["errors"] and result["pages_checked"] == result["pages_declared"] > 0
    return result


def audit(main):
    report = {
        "main_cache": str(main),
        "scope": "SHA-256 code-page slots; excludes CMS trust and special slots",
        "excluded_sidecars": [],
        "sidecar_details": [],
        "chunks": [],
        "errors": [],
    }
    try:
        entries, report["excluded_sidecars"], report["sidecar_details"] = inventory(main)
        report["chunks_expected"] = len(entries)
        for path, uuid in entries:
            report["chunks"].append(audit_chunk(path, uuid))
    except (OSError, ValueError) as error:
        report["errors"].append(str(error))
    chunks = report["chunks"]
    report["chunks_checked"] = len(chunks)
    report["pages_declared"] = sum(chunk["pages_declared"] for chunk in chunks)
    report["pages_checked"] = sum(chunk["pages_checked"] for chunk in chunks)
    report["bytes_checked"] = sum(chunk["bytes_checked"] for chunk in chunks)
    report["mismatch_count"] = sum(len(chunk["mismatches"]) for chunk in chunks)
    report["complete"] = (
        not report["errors"]
        and bool(chunks)
        and len(chunks) == report["chunks_expected"]
        and all(c["complete"] for c in chunks)
    )
    report["ok"] = report["complete"] and report["pages_checked"] > 0 and report["mismatch_count"] == 0
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_cache", type=Path, help="explicit dyld_shared_cache_<architecture> main file")
    arguments = parser.parse_args(argv)
    report = audit(arguments.main_cache)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
