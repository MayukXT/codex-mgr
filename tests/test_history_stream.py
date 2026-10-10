"""Whole-history reads reject corrupt tails that identity discovery may not read."""

import os
import struct
from dataclasses import replace

import pytest
import zstandard

from codex_mgr.core.history_stream import HistoryLimits, HistoryReadError, iter_history_records


def history(tmp_path, data, *, compressed=True):
    path = tmp_path / ("synthetic.jsonl.zst" if compressed else "synthetic.jsonl")
    path.write_bytes(data)
    return path


def frames(*parts, checksum=True, known_size=False):
    compressor = zstandard.ZstdCompressor(write_checksum=checksum, write_content_size=known_size)
    return b"".join(compressor.compress(part) for part in parts)


@pytest.mark.parametrize("known_size", [False, True])
@pytest.mark.parametrize("checksum", [False, True])
def test_exact_records_cross_frame_boundaries_and_skip_seek_metadata(
    tmp_path, known_size, checksum
):
    raw = '\ufeff{"synthetic":"  Exact 🌙\\n"}\r\n\n{"ordinal":1}\n'.encode()
    skipped = struct.pack("<II", 0x184D2A5E, 9) + b"synthetic"
    encoded = frames(raw[:13], b"", raw[13:19], raw[19:], known_size=known_size, checksum=checksum)
    path = history(tmp_path, skipped + encoded + skipped)
    assert list(iter_history_records(path)) == raw.splitlines(keepends=True)
    assert path.read_bytes() == skipped + encoded + skipped
    assert not path.with_suffix("").exists()


@pytest.mark.parametrize("representation", ["plain", "compressed"])
def test_exact_record_and_total_boundaries(representation, tmp_path):
    raw = b' {"synthetic": 0.1234567890123456789}\r\n\n'
    compressed = representation == "compressed"
    data = frames(raw, known_size=True) if compressed else raw
    path = history(tmp_path, data, compressed=compressed)
    limits = HistoryLimits(
        record_bytes=len(raw) - 1, decoded_bytes=len(raw), source_bytes=len(data)
    )
    assert b"".join(iter_history_records(path, limits=limits)) == raw
    for field in ("record_bytes", "decoded_bytes", "source_bytes"):
        bound = len(raw) - 2 if field == "record_bytes" else getattr(limits, field) - 1
        with pytest.raises(HistoryReadError):
            list(iter_history_records(path, limits=replace(limits, **{field: bound})))


@pytest.mark.parametrize("spare", [1, 2, 3])
def test_source_limit_allows_eof_when_fewer_than_four_budget_bytes_remain(tmp_path, spare):
    raw = b'{"synthetic":1}\n'
    encoded = frames(raw)
    path = history(tmp_path, encoded)
    assert (
        b"".join(
            iter_history_records(path, limits=HistoryLimits(source_bytes=len(encoded) + spare))
        )
        == raw
    )


@pytest.mark.parametrize("cut", [1, 2, 3, 4, 5, 6, 7, 8])
def test_valid_header_and_record_do_not_make_a_truncated_compressed_tail_valid(tmp_path, cut):
    first = frames(b'{"type":"session_meta"}\n')
    tail = frames(b'{"type":"synthetic-private-tail"}\n')
    path = history(tmp_path, first + tail[:-cut])
    with pytest.raises(HistoryReadError) as caught:
        list(iter_history_records(path))
    assert "synthetic-private" not in str(caught.value)
    assert path.read_bytes() == first + tail[:-cut]


@pytest.mark.parametrize("fault", ["checksum", "trailing", "skippable", "reserved", "block"])
def test_invalid_compressed_tails_are_fixed_errors(fault, tmp_path):
    encoded = frames(b'{"synthetic-private-record":1}\n')
    if fault == "checksum":
        encoded = encoded[:-1] + bytes([encoded[-1] ^ 1])
    elif fault == "trailing":
        encoded += b"synthetic-private-trailing"
    elif fault == "skippable":
        encoded += struct.pack("<II", 0x184D2A5E, 99) + b"short"
    elif fault == "reserved":
        encoded = encoded[:4] + bytes([encoded[4] | 8]) + encoded[5:]
    else:
        encoded = b"\x28\xb5\x2f\xfd\x00\x00\x07\x00\x00"
    path = history(tmp_path, encoded)
    with pytest.raises(HistoryReadError) as caught:
        list(iter_history_records(path))
    assert "synthetic-private" not in str(caught.value)


@pytest.mark.parametrize("compressed", [False, True])
def test_incomplete_jsonl_record_is_never_silently_dropped(tmp_path, compressed):
    raw = b'{"complete":true}\n{"unterminated":true}'
    path = history(tmp_path, frames(raw) if compressed else raw, compressed=compressed)
    with pytest.raises(HistoryReadError, match="incomplete"):
        list(iter_history_records(path))


@pytest.mark.parametrize("field", ["frames", "blocks", "frame_bytes", "window_bytes"])
def test_compression_resource_limits_reject_before_unbounded_decoding(tmp_path, field):
    raw = b'{"synthetic":true}\n' * 100
    data = frames(raw, raw) if field == "frames" else frames(raw)
    limit = 1 if field != "window_bytes" else 1024
    if field == "blocks":
        data = b"\x28\xb5\x2f\xfd\x00\x00\x00\x00\x00\x01\x00\x00"
    path = history(tmp_path, data)
    with pytest.raises(HistoryReadError):
        list(iter_history_records(path, limits=replace(HistoryLimits(), **{field: limit})))


@pytest.mark.parametrize("kind", ["raw", "rle"])
def test_raw_and_rle_blocks_use_the_same_exact_record_contract(tmp_path, kind):
    raw = b'{"raw":true}\n' if kind == "raw" else b"\n" * 13
    value = len(raw) * 8 + (1 if kind == "raw" else 3)
    frame = b"\x28\xb5\x2f\xfd\x00\x00" + value.to_bytes(3, "little")
    frame += raw if kind == "raw" else b"\n"
    path = history(tmp_path, frame)
    assert b"".join(iter_history_records(path)) == raw


@pytest.mark.parametrize("dictionary_bytes", [1, 2, 4])
def test_zero_dictionary_id_widths_do_not_require_an_external_dictionary(
    tmp_path, dictionary_bytes
):
    raw = b'{"synthetic":true}\n'
    flag = {1: 1, 2: 2, 4: 3}[dictionary_bytes]
    frame = b"\x28\xb5\x2f\xfd" + bytes([flag, 0]) + b"\0" * dictionary_bytes
    frame += (len(raw) * 8 + 1).to_bytes(3, "little") + raw
    assert b"".join(iter_history_records(history(tmp_path, frame))) == raw


def test_four_and_eight_byte_declared_sizes_use_the_same_record_contract(tmp_path):
    raw = b"\n" * 70_000
    encoded = frames(raw, known_size=True)
    assert encoded[4] >> 6 == 2
    assert b"".join(iter_history_records(history(tmp_path, encoded))) == raw
    raw = b'{"synthetic":true}\n'
    frame = b"\x28\xb5\x2f\xfd\xe0" + len(raw).to_bytes(8, "little")
    frame += (len(raw) * 8 + 1).to_bytes(3, "little") + raw
    assert b"".join(iter_history_records(history(tmp_path, frame))) == raw


@pytest.mark.parametrize("known_size", [False, True])
def test_expanded_frame_is_rejected_even_when_compressed_bytes_fit(tmp_path, known_size):
    raw = b'{"synthetic":true}\n' * 1000
    encoded = frames(raw, known_size=known_size)
    assert len(encoded) < 128 < len(raw)
    path = history(tmp_path, encoded)
    with pytest.raises(HistoryReadError):
        list(iter_history_records(path, limits=HistoryLimits(frame_bytes=128)))


def test_record_limit_spans_frames_without_truncating_the_record(tmp_path):
    path = history(tmp_path, frames(b"1234", b"567\n"))
    with pytest.raises(HistoryReadError, match="record read limit"):
        list(iter_history_records(path, limits=HistoryLimits(record_bytes=7)))


def test_block_budget_covers_the_whole_file_instead_of_resetting_each_frame(tmp_path):
    path = history(tmp_path, frames(b"\n", b"\n"))
    with pytest.raises(HistoryReadError, match="block limit"):
        list(iter_history_records(path, limits=HistoryLimits(blocks=1)))


def test_known_frame_size_cannot_force_unbounded_allocation(tmp_path):
    # Single-segment content size would otherwise instruct the decoder to allocate 128 MiB.
    encoded = b"\x28\xb5\x2f\xfd\xe0" + (128 * 1024 * 1024).to_bytes(8, "little")
    path = history(tmp_path, encoded)
    with pytest.raises(HistoryReadError, match="unsupported decoder"):
        list(iter_history_records(path))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO contract.")
def test_a_fifo_cannot_block_complete_history_reading(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    os.mkfifo(path)
    with pytest.raises(HistoryReadError, match="regular file"):
        list(iter_history_records(path))
