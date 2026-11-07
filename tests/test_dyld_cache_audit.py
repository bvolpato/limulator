"""Exercise actual cache bytes and CLI outcomes without a guest or mount."""

import hashlib
import importlib.util
import json
import struct
from contextlib import contextmanager
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "dyld_cache_audit", Path(__file__).resolve().parents[1] / "tools/audit_dyld_cache.py"
)
auditor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auditor)

PAGE = 16384
NAME = "dyld_shared_cache_arm64e_x1"


def cache_bytes(uuid=b"A" * 16, subcaches=(), version=0x20400):
    data = bytearray(PAGE * 2)
    data[:16] = b"dyld_v1 arm64eX1"
    struct.pack_into("<II", data, 0x10, 0x200, 1)
    struct.pack_into("<QQQII", data, 0x200, 0x180000000, len(data), 0, 5, 5)
    data[0x58:0x68] = uuid
    if subcaches:
        struct.pack_into("<II", data, 0x188, 0x300, len(subcaches))
        for index, (suffix, sub_uuid) in enumerate(subcaches):
            struct.pack_into(
                "<16sQ32s", data, 0x300 + index * 56, sub_uuid, index * PAGE, suffix.encode() + b"\0"
            )
    # Header, identifier, then exactly two SHA-256 slots.
    cd = bytearray(128 + 64)
    struct.pack_into(
        ">IIIIIIIII4BI", cd, 0, 0xFADE0C02, len(cd), version, 0, 128, 108, 0, 2, len(data), 32, 2, 0, 14, 0
    )
    cd[108:113] = b"dyld\0"
    signature = bytearray(struct.pack(">IIIII", 0xFADE0CC0, 20 + len(cd), 1, 0, 20)) + cd
    struct.pack_into("<QQ", data, 0x28, len(data), len(signature))
    for page in range(2):
        signature[20 + 128 + page * 32 : 20 + 128 + (page + 1) * 32] = hashlib.sha256(
            data[page * PAGE : (page + 1) * PAGE]
        ).digest()
    return data + signature


def write_family(tmp_path):
    main = tmp_path / NAME
    main.write_bytes(cache_bytes(subcaches=((".01", b"B" * 16), (".02.dylddata", b"C" * 16))))
    (tmp_path / (NAME + ".01")).write_bytes(cache_bytes(b"B" * 16))
    (tmp_path / (NAME + ".02.dylddata")).write_bytes(cache_bytes(b"C" * 16))
    return main


def test_valid_family_streams_one_read_only_chunk_at_a_time(tmp_path, monkeypatch, capsys):
    main = write_family(tmp_path)
    (tmp_path / (NAME + ".symbols")).write_bytes(b"excluded local-symbol sidecar")
    contents = {path: path.read_bytes() for path in tmp_path.iterdir()}
    original = auditor.open_chunk
    active = 0

    @contextmanager
    def tracked(path):
        nonlocal active
        assert active == 0
        with original(path) as handle:
            active += 1
            assert not handle.writable()
            try:
                yield handle
            finally:
                active -= 1

    monkeypatch.setattr(auditor, "open_chunk", tracked)
    assert auditor.main([str(main)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["complete"]
    assert report["chunks_expected"] == report["chunks_checked"] == 3
    assert report["pages_declared"] == report["pages_checked"] == 6
    assert report["bytes_checked"] == PAGE * 6
    assert report["excluded_sidecars"] == [NAME + ".symbols"]
    assert {path: path.read_bytes() for path in contents} == contents
    assert active == 0


def test_corrupt_page_reports_exact_mismatch_and_failure(tmp_path, capsys):
    main = write_family(tmp_path)
    chunk = tmp_path / (NAME + ".01")
    data = bytearray(chunk.read_bytes())
    data[PAGE + 9] ^= 0x80
    chunk.write_bytes(data)
    assert auditor.main([str(main)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] and not report["ok"]
    assert report["mismatch_count"] == 1
    mismatch = report["chunks"][1]["mismatches"][0]
    assert mismatch["page"] == 1 and mismatch["file_offset"] == PAGE
    assert mismatch["computed"] != mismatch["stored"]
    assert report["pages_checked"] == 6


@pytest.mark.parametrize("failure", ["missing", "wrong_uuid", "unlisted", "symlink"])
def test_incomplete_or_mixed_family_is_not_clean(tmp_path, failure):
    main = write_family(tmp_path)
    chunk = tmp_path / (NAME + ".01")
    if failure == "missing":
        chunk.unlink()
    elif failure == "wrong_uuid":
        chunk.write_bytes(cache_bytes(b"D" * 16))
    elif failure == "unlisted":
        (tmp_path / (NAME + ".03")).write_bytes(cache_bytes(b"D" * 16))
    else:
        chunk.unlink()
        chunk.symlink_to(main)
    report = auditor.audit(main)
    assert not report["ok"] and not report["complete"]
    assert report["errors"] or any(chunk["errors"] for chunk in report["chunks"])


@pytest.mark.parametrize(
    ("offset", "packing", "value", "error"),
    [
        (PAGE * 2 + 20 + 37, ">B", 1, "unsupported hash"),
        (PAGE * 2 + 20 + 39, ">B", 0, "unsupported page exponent"),
        (PAGE * 2 + 20 + 8, ">I", 0x20700, "unsupported CodeDirectory version"),
        (PAGE * 2 + 20 + 44, ">I", 128, "unsupported scatter"),
        (PAGE * 2 + 20 + 56, ">Q", PAGE * 2, "unsupported 64-bit"),
        (PAGE * 2 + 20 + 28, ">I", 1, "inconsistent code-slot count"),
        (PAGE * 2 + 20 + 32, ">I", PAGE, "code limit"),
        (PAGE * 2 + 20 + 16, ">I", 190, "code hash slots outside bounds"),
        (PAGE * 2 + 20 + 4, ">I", 1, "invalid blob length"),
        (PAGE * 2 + 16, ">I", 8, "overlaps index"),
        (PAGE * 2 + 8, ">I", 0, "invalid superblob count"),
        (0x28, "<Q", 0, "unsigned chunk"),
    ],
)
def test_rejects_unsupported_and_malformed_signature(tmp_path, offset, packing, value, error):
    main = tmp_path / NAME
    data = cache_bytes()
    struct.pack_into(packing, data, offset, value)
    main.write_bytes(data)
    report = auditor.audit(main)
    assert not report["ok"] and not report["complete"]
    assert any(error in message for chunk in report["chunks"] for message in chunk["errors"])


@pytest.mark.parametrize("length", [0x30, PAGE * 2 + 8, PAGE * 2 + 100])
def test_truncated_input_never_claims_complete_coverage(tmp_path, length):
    main = tmp_path / NAME
    main.write_bytes(cache_bytes()[:length])
    report = auditor.audit(main)
    assert not report["ok"] and not report["complete"]


def test_read_error_retains_partial_page_coverage_and_continues_other_chunks(tmp_path, monkeypatch):
    main = write_family(tmp_path)
    original = auditor.open_chunk

    class FailingReader:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, length):
            if length == PAGE and self.handle.tell() == PAGE:
                raise OSError("injected read failure")
            return self.handle.read(length)

    @contextmanager
    def failing(path):
        with original(path) as handle:
            yield FailingReader(handle) if path.name.endswith(".01") else handle

    monkeypatch.setattr(auditor, "open_chunk", failing)
    report = auditor.audit(main)
    assert not report["ok"] and not report["complete"]
    assert report["pages_declared"] == 6 and report["pages_checked"] == 5
    assert report["chunks"][1]["errors"] == ["injected read failure"]
    assert report["chunks"][2]["complete"]


def test_empty_directory_and_zero_coverage_fail(tmp_path):
    assert not auditor.audit(tmp_path / NAME)["ok"]
    main = tmp_path / NAME
    data = cache_bytes()
    struct.pack_into(">II", data, PAGE * 2 + 20 + 28, 0, 0)
    main.write_bytes(data)
    report = auditor.audit(main)
    assert not report["ok"] and report["pages_checked"] == 0
    assert "zero code-page coverage" in report["chunks"][0]["errors"]


def test_alternate_directory_and_overlapping_blobs_are_rejected(tmp_path):
    main = tmp_path / NAME
    for slot, offset, expected in [(0x1000, 28 + 192, "unsupported alternate"), (7, 28, "overlapping blobs")]:
        data = cache_bytes()
        cd = data[PAGE * 2 + 20 :]
        signature = (
            struct.pack(">III", 0xFADE0CC0, 28 + len(cd) * 2, 2)
            + struct.pack(">IIII", 0, 28, slot, offset)
            + cd
            + cd
        )
        struct.pack_into("<Q", data, 0x30, len(signature))
        data[PAGE * 2 :] = signature
        main.write_bytes(data)
        report = auditor.audit(main)
        assert not report["ok"] and not report["complete"]
        assert expected in report["chunks"][0]["errors"][0]


def test_chunk_mutation_during_read_is_not_a_clean_audit(tmp_path, monkeypatch):
    main = write_family(tmp_path)
    original = auditor.identity
    calls = {}

    def changed(handle):
        result = original(handle)
        key = result[:2]
        calls[key] = calls.get(key, 0) + 1
        if calls[key] == 4:
            return (*result[:-1], result[-1] + 1)
        return result

    monkeypatch.setattr(auditor, "identity", changed)
    report = auditor.audit(main)
    assert not report["ok"] and not report["complete"]
    assert any("changed during audit" in message for c in report["chunks"] for message in c["errors"])


def test_atlas_is_explicitly_fingerprinted_metadata_outside_page_audit(tmp_path):
    main = write_family(tmp_path)
    metadata = b"opaque introspection archive fixture"
    (tmp_path / (NAME + ".atlas")).write_bytes(metadata)
    report = auditor.audit(main)
    assert report["ok"] and report["complete"]
    assert report["chunks_checked"] == 3 and report["pages_checked"] == 6
    assert report["excluded_sidecars"] == [NAME + ".atlas"]
    assert report["sidecar_details"] == [
        {
            "chunk": NAME + ".atlas",
            "reason": "unsigned introspection metadata sidecar, separate from signed cache chunks",
            "source": auditor.ATLAS_SOURCE,
            "size": len(metadata),
            "sha256": hashlib.sha256(metadata).hexdigest(),
            "prefix_hex": metadata[:16].hex(),
            "page_signature_audited": False,
            "archive_contents_validated": False,
        }
    ]


@pytest.mark.parametrize("kind", ["cache", "empty", "symlink", "declared"])
def test_atlas_name_cannot_hide_a_cache_or_unreadable_file(tmp_path, kind):
    main = write_family(tmp_path)
    atlas = tmp_path / (NAME + ".atlas")
    if kind == "cache":
        atlas.write_bytes(cache_bytes(b"D" * 16))
    elif kind == "empty":
        atlas.touch()
    elif kind == "symlink":
        atlas.symlink_to(main)
    else:
        main.write_bytes(cache_bytes(subcaches=((".atlas", b"D" * 16),)))
        atlas.write_bytes(b"opaque metadata")
        (tmp_path / (NAME + ".01")).unlink()
        (tmp_path / (NAME + ".02.dylddata")).unlink()
    report = auditor.audit(main)
    assert not report["ok"] and not report["complete"]
    assert report["errors"]


def test_atlas_read_error_is_an_incomplete_audit(tmp_path, monkeypatch):
    main = write_family(tmp_path)
    (tmp_path / (NAME + ".atlas")).write_bytes(b"opaque metadata")
    original = auditor.open_chunk

    class FailingReader:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, length):
            if length == 1024 * 1024:
                raise OSError("injected atlas read failure")
            return self.handle.read(length)

    @contextmanager
    def failing(path):
        with original(path) as handle:
            yield FailingReader(handle) if path.suffix == ".atlas" else handle

    monkeypatch.setattr(auditor, "open_chunk", failing)
    report = auditor.audit(main)
    assert not report["ok"] and not report["complete"]
    assert report["errors"] == ["injected atlas read failure"]
