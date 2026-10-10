"""Compressed discovery reads bounded metadata without changing stored history."""

import struct

import pytest
import zstandard
from test_sessions import index, rollout, sid

from codex_mgr.core.backup import create_session_backup, list_session_backups
from codex_mgr.core.bridge import BridgeError, prepare_bridge
from codex_mgr.core.rollouts import (
    CanonicalHistoryError,
    SessionIdentityError,
    read_session_metadata,
)
from codex_mgr.core.sessions import AmbiguousSessionError, discover_sessions, list_sessions
from codex_mgr.utils.paths import get_codex_home


def compressed(number, *, chunks=None, **options):
    plain = rollout(number, **options)
    raw = plain.read_bytes()
    path = plain.with_suffix(".jsonl.zst")
    compressor = zstandard.ZstdCompressor(write_content_size=False, write_checksum=True)
    path.write_bytes(b"".join(compressor.compress(chunk) for chunk in (chunks or [raw])))
    plain.unlink()
    return path, raw


def test_compressed_unindexed_archive_and_workspace_discovery_is_read_only(tmp_path):
    path, _ = compressed(1, metadata={"cwd": str(tmp_path)})
    archived, _ = compressed(
        2, folder=get_codex_home() / "archived_sessions", metadata={"cwd": str(tmp_path)}
    )
    index({"id": sid(1), "thread_name": "Exact compressed title"})
    before = {candidate: candidate.read_bytes() for candidate in (path, archived)}
    items = list_sessions(workspace=tmp_path, include_archived=True)
    assert {item.id for item in items} == {sid(1), sid(2)}
    assert next(item for item in items if item.id == sid(1)).thread_name == "Exact compressed title"
    assert next(item for item in items if item.id == sid(2)).archived
    assert len(list_sessions()) == 1
    for candidate, original in before.items():
        assert candidate.read_bytes() == original
        assert not candidate.with_suffix("").exists()


def test_concatenated_unknown_size_frames_bom_and_filename_fallback():
    plain = rollout(1, metadata={"id": None})
    raw = b"\n\r\n\xef\xbb\xbf" + plain.read_bytes()
    path, _ = compressed(1, chunks=[raw[:4], raw[4:19], raw[19:]], metadata={"id": None})
    metadata = read_session_metadata(path)
    assert metadata.id == sid(1) and metadata.from_filename and metadata.compressed


def test_plain_and_compressed_duplicate_uuid_requires_resolution():
    path, raw = compressed(1)
    path.with_suffix("").write_bytes(raw)
    items = list_sessions()
    assert len(items) == 1 and len(items[0].duplicate_paths) == 2
    with pytest.raises(AmbiguousSessionError):
        items[0].require_rollout()


@pytest.mark.parametrize("fault", ["invalid", "checksum", "truncated", "identity"])
def test_bad_compressed_metadata_is_counted_without_exposing_payload(fault):
    path, _ = compressed(1, metadata={"id": sid(2)} if fault == "identity" else None)
    before = path.read_bytes()
    if fault == "invalid":
        path.write_bytes(b"synthetic-private-metadata")
    elif fault == "checksum":
        path.write_bytes(before[:-1] + bytes([before[-1] ^ 1]))
    elif fault == "truncated":
        path.write_bytes(before[:6])
    with pytest.raises(SessionIdentityError) as caught:
        read_session_metadata(path)
    assert "synthetic-private-metadata" not in str(caught.value)
    catalog = discover_sessions()
    assert not catalog.sessions and catalog.skipped_rollouts == 1


@pytest.mark.parametrize("representation", ["plain", "compressed"])
def test_blank_header_prefix_has_one_total_decoded_limit(representation):
    plain = rollout(1)
    raw = (b" " * 4095 + b"\n") * 257 + plain.read_bytes()
    if representation == "plain":
        plain.write_bytes(raw)
        path = plain
    else:
        path, _ = compressed(1, chunks=[raw])
    with pytest.raises(SessionIdentityError, match="too large"):
        read_session_metadata(path)


def test_skippable_compressed_prefix_cannot_force_unbounded_input_reads():
    path, _ = compressed(1)
    frame = path.read_bytes()
    padding = b"\0" * 4_194_304
    path.write_bytes(struct.pack("<II", 0x184D2A50, len(padding)) + padding + frame)
    before = path.read_bytes()
    with pytest.raises(SessionIdentityError, match="read limit"):
        read_session_metadata(path)
    assert path.read_bytes() == before


def test_oversized_window_is_rejected_before_metadata_allocation():
    path, raw = compressed(1)
    # A valid raw-block frame with unknown size and a declared 128 MiB window.
    frame = b"\x28\xb5\x2f\xfd\x00\x88" + (len(raw) * 8 + 1).to_bytes(3, "little") + raw
    assert zstandard.get_frame_parameters(frame).window_size == 134_217_728
    path.write_bytes(frame)
    with pytest.raises(SessionIdentityError, match="invalid"):
        read_session_metadata(path)
    assert path.read_bytes() == frame


def test_compressed_legacy_history_is_visible_but_file_mutation_is_refused():
    path, _ = compressed(1)
    before = path.read_bytes()
    assert read_session_metadata(path).compressed
    with pytest.raises(CanonicalHistoryError):
        create_session_backup(sid(1), path)
    with pytest.raises(BridgeError, match="compressed"), prepare_bridge(path, "fixture"):
        pytest.fail("Compressed histories require canonical handling before mutation.")
    assert path.read_bytes() == before and not list_session_backups(sid(1))
    assert not path.with_suffix("").exists()


def test_unsupported_similar_suffixes_are_not_mistaken_for_rollouts():
    path = rollout(1)
    path.rename(path.with_suffix(".jsonl.bak"))
    assert not list_sessions()
