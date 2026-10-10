"""Independent copies preserve originals through collisions, failures and recovery."""

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from test_history_lineage import compress, cutoff, source

from codex_mgr.core import history_copy
from codex_mgr.core.bridge import BridgeError
from codex_mgr.core.history_copy import (
    HistoryCopyLimits,
    prepare_history_copy,
    read_history_copy,
    recover_history_copy,
)
from codex_mgr.core.history_stream import HistoryReadError
from codex_mgr.core.session_lock import _native_lock
from codex_mgr.utils.files import FileConflictError
from codex_mgr.utils.paths import get_codex_home


def history(*, base=None, identity=None, archived=False):
    path, records = source(get_codex_home(), base=base, identity=identity, archived=archived)
    records[0] = records[0].replace(
        b'"history_mode":', b'"model_provider":"original","history_mode":'
    )
    records[1:] = [
        raw.replace(b'"payload":{"exact"', b'"payload":{"type":"message","exact"')
        for raw in records[1:]
    ]
    path.write_bytes(b"".join(records))
    return path, records


def bundle(identity):
    return get_codex_home() / "backups" / "history-copies" / identity


def journal(identity):
    return bundle(identity) / "journal.json"


def amend_journal(identity, change):
    path = journal(identity)
    record = json.loads(path.read_bytes())
    change(record)
    path.write_bytes(json.dumps(record).encode())


@pytest.mark.parametrize("representation", ["plain", "compressed-archive"])
def test_frozen_copy_snapshots_every_source_and_keeps_private_candidate_independent(representation):
    parent, inherited = history()
    root, local = history(base=cutoff(parent, inherited))
    # Frozen context ignores later ancestor bytes, but the complete raw snapshot retains them.
    parent.write_bytes(parent.read_bytes() + b"synthetic-later-ancestor-tail\n")
    if representation == "compressed-archive":
        archived = get_codex_home() / "archived_sessions" / parent.name
        archived.parent.mkdir()
        parent.rename(archived)
        parent = compress(archived)
        root = compress(root)
    originals = {path: path.read_bytes() for path in (parent, root)}
    database = get_codex_home() / "synthetic-state.sqlite"
    database.write_bytes(b"synthetic database remains untouched")
    with prepare_history_copy(root, "target") as prepared:
        assert prepared.id != prepared.original_id
        assert not prepared.target.exists()
        assert (
            b"".join(inherited[1:] + local[1:])
            == prepared.candidate.read_bytes().split(b"\n", 1)[1]
        )
        info = prepared.snapshot()
        assert info.state == "prepared" and info.source_count == 2
        assert read_history_copy(prepared.id) == info
        for path, data in originals.items():
            assert (
                bundle(prepared.id) / "sources" / path.relative_to(get_codex_home())
            ).read_bytes() == data
        private = bundle(prepared.id) / "candidate.jsonl"
        candidate = private.read_bytes()
        target = prepared.publish()
        assert target.read_bytes() == candidate
        assert read_history_copy(prepared.id).state == "published"
        assert target.stat().st_nlink == private.stat().st_nlink == 1
        with target.open("ab") as output:
            output.write(b'{"timestamp":"synthetic","type":"event_msg","payload":{}}\n')
        assert private.read_bytes() == candidate
        recovered = recover_history_copy(prepared.id)
        assert recovered.state == "published"
        assert target.read_bytes().startswith(candidate) and target.read_bytes() != candidate
        assert str(get_codex_home()) not in repr(prepared) + repr(info)
        assert "Text" not in repr(prepared) + repr(info)
        ephemeral = prepared.candidate
    assert not ephemeral.exists()
    assert {path: path.read_bytes() for path in originals} == originals
    assert database.read_bytes() == b"synthetic database remains untouched"
    assert not (get_codex_home() / "config.toml").exists()
    assert not (get_codex_home() / "cmgr.json").exists()


def test_ancestor_move_compression_and_later_append_preserve_prepared_commitment():
    parent, inherited = history()
    root, _ = history(base=cutoff(parent, inherited))
    with prepare_history_copy(root, "target") as prepared:
        moved = get_codex_home() / "archived_sessions" / parent.name
        moved.parent.mkdir()
        parent.rename(moved)
        parent = compress(moved)
        prepared.snapshot()
        # A new irrelevant tail may appear after the point-in-time raw snapshot.
        with parent.open("ab") as output:
            output.write(b"synthetic-later-frame")
        target = prepared.publish()
        assert target.exists() and root.exists() and parent.exists()


def test_root_archive_compression_after_preparation_keeps_the_exact_history():
    root, _ = history()
    with prepare_history_copy(root, "target") as prepared:
        expected = prepared.candidate.read_bytes()
        moved = get_codex_home() / "archived_sessions" / root.name
        moved.parent.mkdir()
        root.rename(moved)
        compressed = compress(moved)
        original = compressed.read_bytes()
        assert prepared.publish().read_bytes() == expected
        assert compressed.read_bytes() == original and not root.exists()


def test_blank_bom_header_and_record_whitespace_are_preserved():
    root, records = history()
    root.write_bytes(b"\n \r\n\xef\xbb\xbf" + b"".join(records))
    original = root.read_bytes()
    with prepare_history_copy(root, "target") as prepared:
        target = prepared.publish()
        assert target.read_bytes().startswith(b"\n \r\n\xef\xbb\xbf")
        assert target.read_bytes().split(b"\n", 3)[3] == b"".join(records[1:])
    assert root.read_bytes() == original


@pytest.mark.parametrize("preserve", [False, True])
def test_encrypted_reasoning_requires_consent_and_preserves_exact_readable_summary(preserve):
    root, records = history()
    records[1] = records[1].replace(
        b'"type":"message"',
        b'"type":"reasoning","summary":[{"type":"summary_text","text":" Exact summary "}],'
        b'"encrypted_content":"synthetic-opaque","id":"synthetic-opaque-id"',
    )
    root.write_bytes(b"".join(records))
    original = root.read_bytes()
    callback = (lambda _: True) if preserve else None
    with prepare_history_copy(root, "target", preserve_reasoning=callback) as prepared:
        assert prepared.sanitized_count == (0 if preserve else 1)
        if not preserve:
            with pytest.raises(BridgeError, match="consent"):
                prepared.publish()
            assert not prepared.target.exists() and not bundle(prepared.id).exists()
        target = prepared.publish(allow_reasoning_loss=not preserve)
        data = target.read_bytes()
        assert b'"text":" Exact summary "' in data
        assert (b'"encrypted_content":null' in data) != preserve
        assert (b"synthetic-opaque-id" in data) == preserve
        assert b"0.1234567890123456789" in data
        if not preserve:
            with pytest.raises(BridgeError, match="consent"):
                recover_history_copy(prepared.id, publish=True)
    assert root.read_bytes() == original


def test_encrypted_compaction_without_readable_replacement_stops_before_snapshot():
    root, records = history()
    records[1] = records[1].replace(
        b'"type":"message"', b'"type":"compaction","encrypted_content":"opaque"'
    )
    root.write_bytes(b"".join(records))
    original = root.read_bytes()
    with (
        pytest.raises(BridgeError, match="encrypted compaction"),
        prepare_history_copy(root, "target"),
    ):
        pytest.fail("Unreconstructable compaction cannot be copied to another connection.")
    assert root.read_bytes() == original
    assert not list((get_codex_home() / "backups" / "history-copies").glob("*/journal.json"))


@pytest.mark.parametrize("change", ["source", "candidate", "home"])
def test_preparation_drift_stops_publication(change, monkeypatch, tmp_path):
    root, _ = history()
    with prepare_history_copy(root, "target") as prepared:
        if change == "source":
            root.write_bytes(root.read_bytes().replace(b"Text", b"Word"))
        elif change == "candidate":
            prepared.candidate.write_bytes(
                prepared.candidate.read_bytes().replace(b"Text", b"Word")
            )
        else:
            monkeypatch.setenv("CODEX_HOME", str(tmp_path / "different-home"))
        preserved = root.read_bytes()
        with pytest.raises(FileConflictError, match="changed"):
            prepared.publish()
        assert not prepared.target.exists() and root.read_bytes() == preserved


def test_source_append_during_raw_snapshot_is_detected(monkeypatch):
    root, _ = history()
    copy = history_copy._copy

    def changed(source_path, destination, limit):
        result = copy(source_path, destination, limit)
        if source_path == root:
            with root.open("ab") as output:
                output.write(b"synthetic concurrent append\n")
        return result

    monkeypatch.setattr(history_copy, "_copy", changed)
    with prepare_history_copy(root, "target") as prepared:
        with pytest.raises(FileConflictError, match="snapshotting"):
            prepared.publish()
        assert not prepared.target.exists() and not journal(prepared.id).exists()
        assert root.read_bytes().endswith(b"synthetic concurrent append\n")


@pytest.mark.parametrize("budget", ["candidate", "raw-sources"])
def test_candidate_and_complete_raw_source_budgets_stop_before_publication(budget):
    parent, inherited = history()
    root, _ = history(base=cutoff(parent, inherited))
    original = {path: path.read_bytes() for path in (parent, root)}
    limits = (
        replace(HistoryCopyLimits(), candidate_bytes=100)
        if budget == "candidate"
        else replace(HistoryCopyLimits(), source_bytes=100)
    )
    with (
        pytest.raises(HistoryReadError, match="byte limit"),
        prepare_history_copy(root, "target", limits=limits) as prepared,
    ):
        prepared.publish()
    assert {path: path.read_bytes() for path in original} == original
    assert len(list((get_codex_home() / "sessions").rglob("*.jsonl"))) == 2


def test_active_ancestor_writer_rejects_snapshot_and_preserves_native_lock():
    parent, inherited = history()
    root, _ = history(base=cutoff(parent, inherited))
    identity = json.loads(inherited[0])["payload"]["id"]
    lock = get_codex_home() / "thread-writer-locks" / (identity + ".lock")
    lock.parent.mkdir()
    lock.write_bytes(b"synthetic native lock contents")
    with prepare_history_copy(root, "target") as prepared, _native_lock(lock, create=False):
        with pytest.raises(FileConflictError):
            prepared.publish()
        assert not bundle(prepared.id).exists() and not prepared.target.exists()
    assert lock.read_bytes() == b"synthetic native lock contents"


def test_existing_identity_in_another_filename_is_never_duplicated(monkeypatch):
    existing, _ = history()
    identity = json.loads(existing.read_bytes().splitlines()[0])["payload"]["id"]
    root, _ = history()
    monkeypatch.setattr(history_copy, "uuid4", lambda: UUID(identity))
    with prepare_history_copy(root, "target") as prepared:
        before = existing.read_bytes()
        with pytest.raises(FileConflictError, match="identity"):
            prepared.publish()
        assert existing.read_bytes() == before and not prepared.target.exists()
        assert not bundle(prepared.id).exists()


@pytest.mark.parametrize("failure", ["collision", "unsupported-link"])
def test_exclusive_publication_survives_a_late_collision_or_unsupported_filesystem(
    failure, monkeypatch
):
    root, _ = history()
    original = root.read_bytes()
    link = os.link

    def failed(source_path, target, **kwargs):
        if failure == "collision":
            target.write_bytes(b"synthetic competing session")
            return link(source_path, target, **kwargs)
        raise OSError("synthetic filesystem denied exclusive publication")

    monkeypatch.setattr(history_copy.os, "link", failed)
    with prepare_history_copy(root, "target") as prepared:
        error = FileConflictError if failure == "collision" else HistoryReadError
        with pytest.raises(error):
            prepared.publish()
        assert read_history_copy(prepared.id).state == "prepared"
        assert root.read_bytes() == original
        assert not list(prepared.target.parent.glob(".cmgr-history-*.tmp"))
        if failure == "collision":
            assert prepared.target.read_bytes() == b"synthetic competing session"
            with pytest.raises(FileConflictError):
                recover_history_copy(prepared.id)
        else:
            assert not prepared.target.exists()


def test_recovery_settles_a_crash_after_link_without_replacing_native_appends(monkeypatch):
    root, _ = history()
    write_journal = history_copy._journal

    def interrupted(folder, record, *, expected):
        if record["state"] == "published":
            raise OSError("synthetic interrupted journal update")
        return write_journal(folder, record, expected=expected)

    monkeypatch.setattr(history_copy, "_journal", interrupted)
    with prepare_history_copy(root, "target") as prepared:
        with pytest.raises(OSError):
            prepared.publish()
        assert prepared.target.exists() and read_history_copy(prepared.id).state == "prepared"
        with prepared.target.open("ab") as output:
            output.write(b"synthetic native append\n")
        published = prepared.target.read_bytes()
        monkeypatch.setattr(history_copy, "_journal", write_journal)
        recovered = recover_history_copy(prepared.id)
        assert recovered.state == "published" and recovered.target.read_bytes() == published


def test_explicit_republication_uses_verified_bundle_when_originals_are_unavailable():
    root, _ = history()
    with prepare_history_copy(root, "target") as prepared:
        prepared.snapshot()
        private = (bundle(prepared.id) / "candidate.jsonl").read_bytes()
        identity, target = prepared.id, prepared.target
    root.unlink()
    assert recover_history_copy(identity).state == "prepared" and not target.exists()
    assert recover_history_copy(identity, publish=True).state == "published"
    assert target.read_bytes() == private
    target.unlink()
    assert recover_history_copy(identity).state == "published" and not target.exists()
    assert recover_history_copy(identity, publish=True).target.read_bytes() == private


@pytest.mark.parametrize(
    "fault",
    [
        "candidate",
        "source",
        "duplicate-json",
        "outside",
        "wrong-target",
        "unknown-state",
        "boolean-version",
        "boolean-size",
        "wrong-thread",
        "fingerprint",
        "missing-source",
        "duplicate-source",
        "candidate-envelope",
        "candidate-hardlink",
        "source-hardlink",
        "journal-hardlink",
    ],
)
def test_corrupt_or_unsafe_bundle_never_changes_existing_history(fault, tmp_path):
    root, _ = history()
    with prepare_history_copy(root, "target") as prepared:
        target = prepared.publish()
        private = bundle(prepared.id)
        raw_source = next((private / "sources").rglob("*.jsonl"))
        candidate = private / "candidate.jsonl"
        outside = tmp_path / "outside-sentinel"
        outside.write_bytes(b"synthetic outside sentinel")
        if fault in {"candidate", "source"}:
            selected = candidate if fault == "candidate" else raw_source
            selected.write_bytes(selected.read_bytes().replace(b"Text", b"Word"))
        elif fault == "duplicate-json":
            journal(prepared.id).write_bytes(
                journal(prepared.id)
                .read_bytes()
                .replace(b'"version":1', b'"version":1,"version":1')
            )
        elif fault == "missing-source":
            raw_source.unlink()
        elif fault.endswith("hardlink"):
            selected = {
                "candidate-hardlink": candidate,
                "source-hardlink": raw_source,
                "journal-hardlink": journal(prepared.id),
            }[fault]
            try:
                os.link(selected, tmp_path / "linked-private-data")
            except OSError:
                pytest.skip("Hard links unavailable on this temporary filesystem.")
        elif fault == "candidate-envelope":
            candidate.write_bytes(
                candidate.read_bytes().replace(
                    b'"type":"response_item"', b'"type":"unsupported_item"'
                )
            )
            amend_journal(
                prepared.id,
                lambda record: record["candidate"].update(
                    sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    size=candidate.stat().st_size,
                ),
            )
        else:

            def corrupt(record):
                if fault == "outside":
                    record["sources"][0]["path"] = "sessions/../../outside-sentinel"
                elif fault == "wrong-target":
                    record["target"] = record["target"].replace(prepared.id, str(uuid4()))
                elif fault == "unknown-state":
                    record["state"] = "completed"
                elif fault == "boolean-version":
                    record["version"] = True
                elif fault == "boolean-size":
                    record["candidate"]["size"] = True
                elif fault == "wrong-thread":
                    record["sources"][0]["thread_id"] = str(uuid4())
                elif fault == "fingerprint":
                    record["fingerprint"] = "0" * 64
                elif fault == "duplicate-source":
                    record["sources"].append(record["sources"][0])

            amend_journal(prepared.id, corrupt)
        original, published = root.read_bytes(), target.read_bytes()
        with pytest.raises((HistoryReadError, OSError)):
            recover_history_copy(prepared.id, publish=True)
        assert root.read_bytes() == original and target.read_bytes() == published
        assert outside.read_bytes() == b"synthetic outside sentinel"


def test_external_prefix_edit_survives_recovery_with_private_candidate_intact():
    root, _ = history()
    with prepare_history_copy(root, "target") as prepared:
        target = prepared.publish()
        target.write_bytes(target.read_bytes().replace(b"Text", b"Word"))
        changed = target.read_bytes()
        private = (bundle(prepared.id) / "candidate.jsonl").read_bytes()
        with pytest.raises(FileConflictError, match="preserved"):
            recover_history_copy(prepared.id, publish=True)
        assert target.read_bytes() == changed
        assert (bundle(prepared.id) / "candidate.jsonl").read_bytes() == private


@pytest.mark.parametrize("component", ["candidate", "source-directory", "target"])
def test_recovery_rejects_linked_storage_without_touching_the_link_target(component, tmp_path):
    root, _ = history()
    with prepare_history_copy(root, "target") as prepared:
        target = prepared.publish()
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"synthetic outside sentinel")
        if component == "candidate":
            selected, linked = bundle(prepared.id) / "candidate.jsonl", sentinel
        elif component == "source-directory":
            selected, linked = bundle(prepared.id) / "sources" / "sessions", outside
        else:
            selected, linked = target, sentinel
        saved = selected.with_name(selected.name + ".saved")
        selected.rename(saved)
        try:
            selected.symlink_to(linked, target_is_directory=linked.is_dir())
        except OSError:
            saved.rename(selected)
            pytest.skip("Symlink creation is unavailable.")
        try:
            with pytest.raises(HistoryReadError):
                recover_history_copy(prepared.id, publish=True)
            assert sentinel.read_bytes() == b"synthetic outside sentinel"
            assert list(outside.iterdir()) == [sentinel]
        finally:
            selected.unlink()
            saved.rename(selected)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract.")
@pytest.mark.parametrize("snapshotted", [False, True])
def test_target_parent_junction_is_rechecked_before_lock_or_publication(tmp_path, snapshotted):
    root, _ = history()
    with prepare_history_copy(root, "target") as prepared:
        if snapshotted:
            prepared.snapshot()
        else:
            prepared.target.parent.mkdir(parents=True)
        parent = prepared.target.parent
        saved = parent.with_name(parent.name + "-saved")
        parent.rename(saved)
        outside = tmp_path / "outside"
        outside.mkdir()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(parent), str(outside)],
            capture_output=True,
            check=True,
            timeout=10,
        )
        try:
            with pytest.raises(HistoryReadError):
                prepared.publish()
            assert not list(outside.iterdir())
            assert root.exists()
        finally:
            assert parent.parent.resolve().is_relative_to(get_codex_home())
            assert parent.resolve() == outside.resolve() and outside.resolve().is_relative_to(
                tmp_path.resolve()
            )
            os.rmdir(parent)
            saved.rename(parent)


@pytest.mark.parametrize("fault", ["declared-limit", "too-many-bytes", "truncated-source"])
def test_recovery_resource_claims_are_verified_with_bounded_reads(fault):
    root, _ = history()
    with prepare_history_copy(root, "target") as prepared:
        prepared.snapshot()
        if fault == "declared-limit":
            amend_journal(prepared.id, lambda record: record["candidate"].update(size=2**50))
        elif fault == "too-many-bytes":
            with (bundle(prepared.id) / "candidate.jsonl").open("ab") as output:
                output.write(b"synthetic unclaimed tail\n")
        else:
            raw_source = next((bundle(prepared.id) / "sources").rglob("*.jsonl"))
            raw_source.write_bytes(raw_source.read_bytes()[:-1])
        with pytest.raises(HistoryReadError):
            recover_history_copy(prepared.id, publish=True)
        assert not prepared.target.exists() and root.exists()


@pytest.mark.parametrize("consent", [1, "yes", None])
def test_truthy_nonboolean_values_cannot_authorize_encrypted_loss(consent):
    root, records = history()
    records[1] = records[1].replace(
        b'"type":"message"', b'"type":"reasoning","summary":[],"encrypted_content":"opaque"'
    )
    root.write_bytes(b"".join(records))
    with prepare_history_copy(root, "target") as prepared:
        with pytest.raises(BridgeError, match="consent"):
            prepared.publish(allow_reasoning_loss=consent)
        prepared.snapshot()
        with pytest.raises(BridgeError, match="consent"):
            recover_history_copy(prepared.id, publish=True, allow_reasoning_loss=consent)
        assert not prepared.target.exists()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100"])
def test_invalid_copy_limits_reject(value):
    with pytest.raises(HistoryReadError, match="limits"):
        HistoryCopyLimits(source_bytes=value)


@pytest.mark.parametrize("provider", ["", "bad.provider", "../outside", "x" * 129, None])
def test_invalid_provider_rejects_without_creating_storage(provider):
    root, _ = history()
    with pytest.raises(BridgeError), prepare_history_copy(root, provider):
        pytest.fail("Invalid provider cannot prepare a copy.")
    assert not (get_codex_home() / "backups").exists()
