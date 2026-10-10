"""Shared canonical conversion preserves source stores and discloses new identities."""

import json
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_history_copy import history
from test_history_lineage import compress, cutoff, source

from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.core import resume
from codex_mgr.core.history_copy import read_history_copy
from codex_mgr.core.session_lock import _native_lock
from codex_mgr.core.sessions import SessionItem
from codex_mgr.utils.paths import get_cmgr_settings_path, get_codex_home, get_config_toml_path


def selected(monkeypatch):
    provider = ProviderConfig(
        id="target",
        name="Target",
        base_url="https://target.invalid/v1",
        auth_type="none",
        default_model="exact-target",
        codex_provider_id="native_target",
    )
    monkeypatch.setattr(resume, "find_codex_binary", lambda: "synthetic")
    return CmgrSettings(active_provider_id=provider.id, providers={provider.id: provider})


def session(path):
    from codex_mgr.core.rollouts import read_session_metadata

    metadata = read_session_metadata(path)
    return SessionItem(metadata.id, "Synthetic", "", "", path, metadata.provider)


def copies():
    return list((get_codex_home() / "backups" / "history-copies").glob("*/journal.json"))


@pytest.mark.parametrize("compressed", [False, True])
def test_shared_resume_snapshots_complete_lineage_and_launches_new_uuid(compressed, monkeypatch):
    settings = selected(monkeypatch)
    parent, inherited = history()
    path, _ = history(base=cutoff(parent, inherited))
    if compressed:
        archived = get_codex_home() / "archived_sessions" / parent.name
        archived.parent.mkdir()
        parent.rename(archived)
        parent, path = compress(archived), compress(path)
    original = session(path)
    source_bytes = {source: source.read_bytes() for source in (parent, path)}
    state = get_codex_home() / "synthetic-state.sqlite"
    state.write_bytes(b"synthetic native index")
    preview = resume.preview_resume(original, settings=settings)
    assert preview.creates_copy and preview.changed_records == 1 and not copies()
    activate = resume.apply_settings
    coordinator = get_codex_home() / "thread-writer-locks" / ".coordination.lock"

    def apply(*args, **kwargs):
        # Native config RPC must run outside the source-writer coordinator.
        with _native_lock(coordinator, create=True):
            pass
        assert len(copies()) == 1
        assert read_history_copy(copies()[0].parent.name).state == "prepared"
        assert {source: source.read_bytes() for source in source_bytes} == source_bytes
        return activate(*args, **kwargs)

    launched = []

    def launch(*, session_id):
        with _native_lock(coordinator, create=True):
            pass
        assert session_id != original.id
        info = read_history_copy(session_id)
        assert info.original_id == original.id and info.state == "published"
        assert info.target.exists()
        assert json.loads(get_cmgr_settings_path().read_bytes())["active_provider_id"] == "target"
        assert 'model_provider = "native_target"' in get_config_toml_path().read_text()
        launched.append(session_id)
        return 0

    monkeypatch.setattr(resume, "apply_settings", apply)
    monkeypatch.setattr(resume, "launch_codex", launch)
    code, message = resume.resume_session(
        original, expected_digest=preview.session_digest, settings=settings
    )
    assert code == 0 and launched[0] in message and original.id in message
    assert "Independent session" in message and "preserved" in message
    assert {source: source.read_bytes() for source in source_bytes} == source_bytes
    assert state.read_bytes() == b"synthetic native index"


@pytest.mark.parametrize("consent", [False, 1, "yes", None])
def test_canonical_reasoning_consent_stops_before_snapshot_or_activation(consent, monkeypatch):
    settings = selected(monkeypatch)
    path, _ = history()
    item = {
        "timestamp": "synthetic",
        "ordinal": 3,
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "id": "synthetic",
            "summary": [{"text": "readable"}],
            "encrypted_content": "synthetic opaque",
        },
    }
    with path.open("ab") as output:
        output.write(json.dumps(item).encode() + b"\n")
    original = path.read_bytes()
    activate, launch = Mock(), Mock()
    monkeypatch.setattr(resume, "apply_settings", activate)
    monkeypatch.setattr(resume, "launch_codex", launch)
    preview = resume.preview_resume(session(path), settings=settings)
    assert preview.creates_copy and preview.encrypted_items == 1
    code, message = resume.resume_session(
        session(path), allow_reasoning_loss=consent, settings=settings
    )
    assert code == 1 and "--allow-reasoning-loss" in message and not copies()
    assert path.read_bytes() == original
    activate.assert_not_called()
    launch.assert_not_called()


@pytest.mark.parametrize("status", [0, 9, 130])
def test_explicit_loss_keeps_summary_and_sources_and_reports_native_status(status, monkeypatch):
    settings = selected(monkeypatch)
    path, _ = history()
    summary = [{"type": "summary_text", "text": "  Exact readable summary.\n"}]
    item = {
        "timestamp": "synthetic",
        "ordinal": 3,
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "id": "synthetic",
            "summary": summary,
            "encrypted_content": "synthetic opaque",
        },
    }
    with path.open("ab") as output:
        output.write(json.dumps(item).encode() + b"\n")
    original = session(path)
    raw_original = path.read_bytes()
    identities = []

    def launch(*, session_id):
        info = read_history_copy(session_id)
        reasoning = json.loads(info.target.read_bytes().splitlines()[-1])["payload"]
        assert reasoning["summary"] == summary
        assert reasoning["encrypted_content"] is None and reasoning["id"] is None
        assert info.sanitized_count == 1
        identities.append(session_id)
        return status

    monkeypatch.setattr(resume, "launch_codex", launch)
    code, message = resume.resume_session(original, allow_reasoning_loss=True, settings=settings)
    assert code == status and identities[0] in message and original.id in message
    assert path.read_bytes() == raw_original


@pytest.mark.parametrize(
    "failure",
    ["preview-drift", "identity", "snapshot", "activation", "publication", "launch", "cancel"],
)
def test_canonical_failure_retains_originals_and_reports_recovery_identity(failure, monkeypatch):
    settings = selected(monkeypatch)
    path, _ = history()
    original = session(path)
    preview = resume.preview_resume(original, settings=settings)
    source = path.read_bytes()
    launch = Mock(return_value=0)
    monkeypatch.setattr(resume, "launch_codex", launch)
    if failure == "preview-drift":
        path.write_bytes(
            source.replace(b'"model_provider":"original"', b'"model_provider":"changed"')
        )
        source = path.read_bytes()
    elif failure == "identity":
        original = replace(original, id=str(uuid4()))
    elif failure == "snapshot":
        monkeypatch.setattr(
            "codex_mgr.core.history_copy.PreparedHistoryCopy.snapshot",
            Mock(side_effect=OSError("synthetic-private")),
        )
    elif failure == "activation":
        monkeypatch.setattr(
            resume, "apply_settings", lambda *_, **__: (False, "Synthetic activation failure.")
        )
    elif failure == "publication":
        monkeypatch.setattr(
            "codex_mgr.core.history_copy.PreparedHistoryCopy.publish",
            Mock(side_effect=OSError("synthetic-private")),
        )
    else:
        launch.side_effect = (
            KeyboardInterrupt() if failure == "cancel" else OSError("synthetic-private")
        )
    code, message = resume.resume_session(
        original, expected_digest=preview.session_digest, settings=settings
    )
    assert code == (130 if failure == "cancel" else 1) and "synthetic-private" not in message
    assert path.read_bytes() == source
    if failure in {"activation", "publication", "launch", "cancel"}:
        assert len(copies()) == 1
        identity = copies()[0].parent.name
        assert identity in message and original.id in message
        info = read_history_copy(identity)
        assert info.state == ("published" if failure in {"launch", "cancel"} else "prepared")
        assert info.target.exists() == (info.state == "published")
    else:
        assert not copies()
        assert not get_config_toml_path().exists() and not get_cmgr_settings_path().exists()
    assert launch.call_count == int(failure in {"launch", "cancel"})


def test_active_ancestor_writer_stops_before_configuration_or_publication(monkeypatch):
    settings = selected(monkeypatch)
    parent, inherited = history()
    path, _ = history(base=cutoff(parent, inherited))
    original = session(path)
    folder = get_codex_home() / "thread-writer-locks"
    folder.mkdir()
    with _native_lock(folder / (session(parent).id + ".lock"), create=True):
        code, _ = resume.resume_session(original, settings=settings)
    assert code == 1 and not copies()
    assert not get_config_toml_path().exists() and not get_cmgr_settings_path().exists()


@pytest.mark.parametrize("when", ["before-resume", "activation"])
def test_replaced_root_refuses_stale_preview_or_publication(when, monkeypatch):
    settings = selected(monkeypatch)
    path, _ = history()
    original = session(path)
    preview = resume.preview_resume(original, settings=settings)
    before = path.read_bytes()
    replacement = None

    def replace_root():
        nonlocal replacement
        replacement, _ = source(get_codex_home(), identity=original.id, revision=str(uuid4()))
        replacement.write_bytes(
            replacement.read_bytes().replace(
                b'"payload":{"exact"', b'"payload":{"type":"message","exact"'
            )
        )
        monkeypatch.setattr(
            "codex_mgr.core.sessions._native_selected_rollout", lambda *_, **__: replacement
        )

    if when == "before-resume":
        replace_root()
    else:
        activate = resume.apply_settings

        def apply(*args, **kwargs):
            result = activate(*args, **kwargs)
            replace_root()
            return result

        monkeypatch.setattr(resume, "apply_settings", apply)
    launch = Mock()
    monkeypatch.setattr(resume, "launch_codex", launch)
    code, message = resume.resume_session(
        original, settings=settings, expected_digest=preview.session_digest
    )
    assert code == 1 and ("changed after preview" in message or "root selection changed" in message)
    assert path.read_bytes() == before and replacement.exists()
    launch.assert_not_called()
    if when == "before-resume":
        assert not copies() and not get_config_toml_path().exists()
    else:
        assert len(copies()) == 1
        info = read_history_copy(copies()[0].parent.name)
        assert info.state == "prepared" and not info.target.exists()


def test_snapshot_interruption_reports_attempt_id_when_valid_bundle_remains(monkeypatch):
    from codex_mgr.core import history_copy

    settings = selected(monkeypatch)
    path, _ = history()
    original = session(path)
    original_bytes = path.read_bytes()
    verify = history_copy._bundle
    interrupted = False

    def interrupt_after_journal(identity, *args, **kwargs):
        nonlocal interrupted
        if not interrupted and copies():
            interrupted = True
            raise KeyboardInterrupt
        return verify(identity, *args, **kwargs)

    monkeypatch.setattr(history_copy, "_bundle", interrupt_after_journal)
    launch = Mock()
    monkeypatch.setattr(resume, "launch_codex", launch)
    code, message = resume.resume_session(original, settings=settings)
    assert code == 130 and len(copies()) == 1
    info = read_history_copy(copies()[0].parent.name)
    assert f"Recovery attempt: {info.id}" in message and original.id in message
    assert "Recovery copy:" not in message
    assert info.state == "prepared" and not info.target.exists()
    assert path.read_bytes() == original_bytes
    assert not get_config_toml_path().exists() and not get_cmgr_settings_path().exists()
    launch.assert_not_called()
