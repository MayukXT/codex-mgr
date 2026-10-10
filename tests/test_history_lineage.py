"""Frozen ancestry contributes exact records and excludes unrelated later turns."""

import json
import os
import subprocess
from dataclasses import replace
from uuid import uuid4

import pytest
import zstandard

from codex_mgr.core.history_lineage import (
    LineageError,
    LineageLimits,
    resolve_history_lineage,
)
from codex_mgr.core.history_stream import HistoryReadError
from codex_mgr.utils.files import FileConflictError


def source(home, *, identity=None, revision=None, base=None, count=2, archived=False):
    identity = identity or str(uuid4())
    root = home / ("archived_sessions" if archived else "sessions") / "synthetic"
    root.mkdir(parents=True, exist_ok=True)
    name = identity + ("_" + revision if revision else "")
    path = root / f"rollout-2026-01-01T00-00-00-{name}.jsonl"
    ordinal = base["end_ordinal_exclusive"] if base else 0
    meta = {
        "timestamp": "2026-01-01T00:00:00Z",
        "ordinal": ordinal,
        "type": "session_meta",
        "payload": {"id": identity, "history_mode": "paginated", "history_base": base},
    }
    records = [json.dumps(meta).encode() + b"\n"]
    records += [
        (
            f' {{"timestamp":"synthetic","ordinal":{ordinal + index},"type":"response_item",'
            '"payload":{"exact":"  Text 🌙\\n","decimal":0.1234567890123456789}}\r\n'
        ).encode()
        for index in range(1, count + 1)
    ]
    path.write_bytes(b"".join(records))
    return path, records


def cutoff(path, records, *, included=None):
    included = len(records) if included is None else included
    revision = (
        path.stem.rsplit("_", 1)[1] if "_" in path.stem else json.loads(records[0])["payload"]["id"]
    )
    return {
        "thread_id": revision,
        "end_ordinal_exclusive": json.loads(records[included - 1])["ordinal"] + 1,
        "end_byte_offset": sum(map(len, records[:included])),
    }


def compress(path, *, tail=b""):
    compressed = path.with_suffix(".jsonl.zst")
    compressed.write_bytes(
        zstandard.ZstdCompressor(write_checksum=True).compress(path.read_bytes()) + tail
    )
    path.unlink()
    return compressed


@pytest.mark.parametrize("representation", ["plain", "compressed"])
def test_nested_frozen_replacement_ancestry_preserves_records_and_sources(tmp_path, representation):
    parent, ancestor = source(tmp_path, revision=str(uuid4()), archived=True)
    child, local = source(tmp_path, base=cutoff(parent, ancestor))
    root, current = source(tmp_path, base=cutoff(child, local), count=1)
    # Later valid or malformed parent/child tails cannot enter their frozen prefixes.
    parent.write_bytes(parent.read_bytes() + b'{"synthetic-later-private-tail":true}\n')
    child.write_bytes(child.read_bytes() + b"malformed-synthetic-later-tail\n")
    if representation == "compressed":
        # The cutoff stays in a complete validated frame; an irrelevant later frame is not read.
        parent.write_bytes(b"".join(ancestor))
        parent = compress(parent, tail=b"irrelevant-truncated-later-frame")
        child.write_bytes(b"".join(local))
        child = compress(child, tail=b"irrelevant-truncated-later-frame")
        root = compress(root)
    original = {path: path.read_bytes() for path in (parent, child, root)}
    lineage = resolve_history_lineage(root, home=tmp_path)
    assert [segment.path for segment in lineage.segments] == [parent, child, root]
    assert lineage.segments[0].metadata.rollout_id != lineage.segments[0].metadata.id
    assert list(lineage.iter_records()) == current[:1] + ancestor[1:] + local[1:] + current[1:]
    assert {path: path.read_bytes() for path in original} == original
    assert len(list((tmp_path / "sessions").rglob("*.jsonl*"))) == 2


def test_metadata_only_prefix_and_blank_records_keep_logical_byte_offsets(tmp_path):
    parent, ancestor = source(tmp_path)
    parent.write_bytes(ancestor[0] + b"\n \r\n" + b"".join(ancestor[1:]))
    position = cutoff(parent, ancestor, included=1)
    position["end_byte_offset"] += 4
    root, current = source(tmp_path, base=position, count=0)
    root.write_bytes(b"\n \r\n\xef\xbb\xbf" + b"".join(current))
    lineage = resolve_history_lineage(root, home=tmp_path)
    assert b"".join(lineage.iter_records()) == root.read_bytes() + b"\n \r\n"


@pytest.mark.parametrize(
    "fault", ["middle", "past", "ordinal", "zero", "boolean", "negative", "overflow"]
)
def test_invalid_frozen_cutoffs_fail_without_writing(tmp_path, fault):
    parent, ancestor = source(tmp_path)
    position = cutoff(parent, ancestor)
    if fault == "middle":
        position["end_byte_offset"] -= 1
    elif fault == "past":
        position["end_byte_offset"] += 1
    elif fault == "ordinal":
        position["end_ordinal_exclusive"] += 1
    else:
        position["end_byte_offset"] = {
            "zero": 0,
            "boolean": True,
            "negative": -1,
            "overflow": 1 << 64,
        }[fault]
    root, _ = source(tmp_path, base=position)
    before = root.read_bytes(), parent.read_bytes()
    with pytest.raises(HistoryReadError):
        list(resolve_history_lineage(root, home=tmp_path).iter_records())
    assert (root.read_bytes(), parent.read_bytes()) == before


@pytest.mark.parametrize(
    "fault",
    ["missing", "duplicate", "representations", "cycle", "legacy", "foreign", "metadata-ordinal"],
)
def test_invalid_lineage_resolution_does_not_guess_a_source(tmp_path, fault):
    parent, ancestor = source(tmp_path)
    root, _ = source(tmp_path, base=cutoff(parent, ancestor))
    if fault == "missing":
        parent.unlink()
    elif fault == "duplicate":
        copy = parent.parent / "nested" / parent.name
        copy.parent.mkdir()
        copy.write_bytes(parent.read_bytes())
    elif fault == "representations":
        parent.with_suffix(".jsonl.zst").write_bytes(
            zstandard.ZstdCompressor().compress(parent.read_bytes())
        )
    elif fault == "cycle":
        meta = json.loads(ancestor[0])
        meta["payload"]["history_base"] = cutoff(parent, ancestor, included=1)
        meta["ordinal"] = meta["payload"]["history_base"]["end_ordinal_exclusive"]
        parent.write_bytes(json.dumps(meta).encode() + b"\n")
    else:
        meta = json.loads(ancestor[0])
        if fault == "legacy":
            meta["payload"]["history_mode"] = "legacy"
        elif fault == "foreign":
            meta["payload"]["id"] = str(uuid4())
        else:
            meta["ordinal"] = 1
        parent.write_bytes(json.dumps(meta).encode() + b"\n")
    with pytest.raises(LineageError):
        resolve_history_lineage(root, home=tmp_path)


@pytest.mark.parametrize(
    "fault",
    [
        "gap",
        "duplicate",
        "boolean",
        "missing",
        "repeat-meta",
        "bad-json",
        "duplicate-key",
        "nonfinite",
        "unknown-type",
        "unhashable-type",
    ],
)
def test_invalid_contributing_records_fail_with_fixed_diagnostics(tmp_path, fault):
    root, records = source(tmp_path)
    changed = json.loads(records[1])
    if fault in {"gap", "duplicate", "boolean", "missing"}:
        changed["ordinal"] = {"gap": 4, "duplicate": 0, "boolean": True, "missing": None}[fault]
    elif fault == "repeat-meta":
        changed = json.loads(records[0])
    elif fault == "unknown-type":
        changed["type"] = "synthetic-private-type"
    elif fault == "unhashable-type":
        changed["type"] = {"synthetic-private": True}
    raw = json.dumps(changed).encode() + b"\n"
    if fault == "bad-json":
        raw = b'{"synthetic-private":"unfinished}\n'
    elif fault == "duplicate-key":
        raw = records[1].replace(b'"ordinal":1', b'"ordinal":1,"ordinal":1')
    elif fault == "nonfinite":
        raw = records[1].replace(b"0.1234567890123456789", b"NaN")
    root.write_bytes(records[0] + raw)
    with pytest.raises(LineageError) as caught:
        list(resolve_history_lineage(root, home=tmp_path).iter_records())
    assert "synthetic-private" not in str(caught.value)


@pytest.mark.parametrize("field", ["segments", "directory_entries", "decoded_bytes", "records"])
def test_lineage_resource_bounds_cover_multiple_sources(tmp_path, field):
    parent, records = source(tmp_path)
    root, _ = source(tmp_path, base=cutoff(parent, records))
    with pytest.raises(LineageError, match="limit"):
        list(
            resolve_history_lineage(
                root, home=tmp_path, limits=replace(LineageLimits(), **{field: 1})
            ).iter_records()
        )


@pytest.mark.parametrize("phase", ["resolution", "replay"])
def test_blank_metadata_prefix_records_are_part_of_the_total_budget(tmp_path, phase):
    root, records = source(tmp_path, count=0)
    root.write_bytes(b"\n" * 5 + b"".join(records))
    limits = LineageLimits(records=2)
    with pytest.raises(LineageError, match="limit"):
        if phase == "resolution":
            resolve_history_lineage(root, home=tmp_path, limits=limits)
        else:
            lineage = resolve_history_lineage(root, home=tmp_path)
            list(replace(lineage, limits=limits).iter_records())


def test_metadata_changes_after_resolution_are_conflicts(tmp_path):
    root, _ = source(tmp_path)
    lineage = resolve_history_lineage(root, home=tmp_path)
    root.write_bytes(
        root.read_bytes().replace(b'"history_mode": "paginated"', b'"history_mode": "legacy"')
    )
    with pytest.raises(FileConflictError, match="changed"):
        list(lineage.iter_records())


def test_compaction_window_and_parallel_metadata_records_remain_exact(tmp_path):
    root, records = source(tmp_path, count=0)
    checkpoint = (
        b' {"timestamp":"synthetic","ordinal":1,"type":"compacted","payload":{'
        b'"message":"  Exact summary\\n","window_number":3,"window_id":"synthetic-window",'
        b'"replacement_history":[{"type":"message","role":"assistant","content":[]}],'
        b'"replacement_history_metadata":[{"synthetic_precision":0.1234567890123456789}],'
        b'"resume_metadata":{"synthetic":"  Exact spacing "}}}\r\n'
    )
    root.write_bytes(records[0] + checkpoint)
    assert list(resolve_history_lineage(root, home=tmp_path).iter_records()) == records + [
        checkpoint
    ]


def test_compressed_root_tail_requires_complete_integrity(tmp_path):
    root, records = source(tmp_path, count=0)
    compressor = zstandard.ZstdCompressor(write_checksum=True)
    encoded = compressor.compress(records[0]) + compressor.compress(b"\n")[:-1]
    compressed = root.with_suffix(".jsonl.zst")
    compressed.write_bytes(encoded)
    root.unlink()
    with pytest.raises(HistoryReadError):
        list(resolve_history_lineage(compressed, home=tmp_path).iter_records())
    assert compressed.read_bytes() == encoded


def test_outside_managed_storage_is_refused(tmp_path):
    root, _ = source(tmp_path / "outside")
    with pytest.raises(LineageError, match="managed storage"):
        resolve_history_lineage(root, home=tmp_path)


@pytest.mark.parametrize("directory", [False, True])
def test_links_cannot_supply_an_inherited_source(tmp_path, directory):
    outside, records = source(tmp_path / "outside")
    home = tmp_path / "managed"
    root, _ = source(home, base=cutoff(outside, records))
    link = root.parent / ("linked-folder" if directory else outside.name)
    try:
        link.symlink_to(outside.parent if directory else outside, target_is_directory=directory)
    except OSError:
        pytest.skip("Symlink creation is unavailable.")
    with pytest.raises(LineageError, match="missing or ambiguous"):
        resolve_history_lineage(root, home=home)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract.")
def test_windows_junction_is_not_followed_for_root_or_ancestry(tmp_path):
    outside, records = source(tmp_path / "outside")
    home = tmp_path / "managed"
    root, _ = source(home, base=cutoff(outside, records))
    junction = root.parent / "linked-folder"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside.parent)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    try:
        with pytest.raises(LineageError, match="missing or ambiguous"):
            resolve_history_lineage(root, home=home)
        with pytest.raises(LineageError, match="managed storage"):
            resolve_history_lineage(junction / outside.name, home=home)
    finally:
        assert junction.parent.resolve().is_relative_to(tmp_path.resolve())
        assert junction.resolve() == outside.parent.resolve()
        os.rmdir(junction)
    assert outside.read_bytes() == b"".join(records)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO contract.")
def test_nonregular_root_cannot_block_lineage_resolution(tmp_path):
    root = tmp_path / "sessions" / f"rollout-synthetic-{uuid4()}.jsonl"
    root.parent.mkdir()
    os.mkfifo(root)
    with pytest.raises(HistoryReadError, match="regular"):
        resolve_history_lineage(root, home=tmp_path)
