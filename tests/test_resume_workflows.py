"""Shared resume validates and snapshots before selecting and launching."""

import json
from pathlib import Path
from unittest.mock import patch

from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.core.backup import list_session_backups
from codex_mgr.core.resume import preview_resume, resume_session
from codex_mgr.core.sessions import SessionItem
from codex_mgr.utils.paths import get_cmgr_settings_path, get_config_toml_path

SID = "00000000-0000-0000-0000-000000000001"


def setup(tmp_path, *, encrypted=False):
    old = ProviderConfig(
        id="old",
        name="Old",
        base_url="https://old.example/v1",
        auth_type="none",
        default_model="old-model",
    )
    target = ProviderConfig(
        id="friendly",
        codex_provider_id="actual_connection",
        name="Target",
        base_url="https://target.example/v1",
        auth_type="none",
        default_model="exact-target-model",
    )
    settings = CmgrSettings(active_provider_id="old", providers={old.id: old, target.id: target})
    path = tmp_path / f"rollout-2026-10-10T12-00-00-{SID}.jsonl"
    records = [{"type": "session_meta", "payload": {"id": SID, "model_provider": "old"}}]
    if encrypted:
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "id": "rs_synthetic",
                    "summary": [],
                    "encrypted_content": "opaque",
                },
            }
        )
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    session = SessionItem(SID, "Synthetic session", "", "Unknown", path, "old")
    return settings, session


def test_actual_codex_connection_and_model_are_applied_before_launch(tmp_path):
    settings, session = setup(tmp_path)
    original = session.rollout_path.read_bytes()

    def launch(*, session_id):
        assert session_id == SID
        config = get_config_toml_path().read_text()
        assert 'model_provider = "actual_connection"' in config
        assert 'model = "exact-target-model"' in config
        assert json.loads(get_cmgr_settings_path().read_text())["active_provider_id"] == "friendly"
        assert (
            json.loads(session.rollout_path.read_text().splitlines()[0])["payload"][
                "model_provider"
            ]
            == "actual_connection"
        )
        assert any(path.read_bytes() == original for path in list_session_backups(SID))
        return 0

    with (
        patch("codex_mgr.core.resume.find_codex_binary", return_value=Path("synthetic-codex")),
        patch("codex_mgr.core.resume.launch_codex", side_effect=launch) as launched,
    ):
        assert resume_session(session, "friendly", settings=settings)[0] == 0
    launched.assert_called_once_with(session_id=SID)


def test_unknown_target_and_missing_credential_have_no_side_effects(tmp_path):
    settings, session = setup(tmp_path)
    original = session.rollout_path.read_bytes()
    with (
        patch("codex_mgr.core.resume.find_codex_binary", return_value=Path("synthetic-codex")),
        patch("codex_mgr.core.resume.launch_codex") as launched,
    ):
        assert resume_session(session, "absent", settings=settings)[0] == 1
        settings.providers["friendly"].auth_type = "env_var"
        settings.providers["friendly"].auth_token = "$UNSET_SYNTHETIC_KEY"
        assert resume_session(session, "friendly", settings=settings)[0] == 1
    assert session.rollout_path.read_bytes() == original
    assert not get_config_toml_path().exists() and not get_cmgr_settings_path().exists()
    assert not list_session_backups(SID)
    launched.assert_not_called()


def test_reasoning_loss_requires_explicit_opt_in_before_activation(tmp_path):
    settings, session = setup(tmp_path, encrypted=True)
    original = session.rollout_path.read_bytes()
    with (
        patch("codex_mgr.core.resume.find_codex_binary", return_value=Path("synthetic-codex")),
        patch("codex_mgr.core.resume.launch_codex", return_value=0) as launched,
    ):
        code, message = resume_session(session, "friendly", settings=settings)
        assert code == 1 and "--allow-reasoning-loss" in message
        assert not get_config_toml_path().exists() and not list_session_backups(SID)
        assert session.rollout_path.read_bytes() == original
        assert (
            resume_session(session, "friendly", allow_reasoning_loss=True, settings=settings)[0]
            == 0
        )
    launched.assert_called_once()


def test_activation_failure_and_backup_failure_do_not_change_rollout(tmp_path):
    settings, session = setup(tmp_path)
    original = session.rollout_path.read_bytes()
    with (
        patch("codex_mgr.core.resume.find_codex_binary", return_value=Path("synthetic-codex")),
        patch("codex_mgr.core.resume.launch_codex") as launched,
        patch(
            "codex_mgr.core.resume.apply_settings",
            return_value=(False, "synthetic activation failure"),
        ),
    ):
        assert resume_session(session, "friendly", settings=settings) == (
            1,
            "synthetic activation failure",
        )
    assert session.rollout_path.read_bytes() == original
    launched.assert_not_called()
    with (
        patch("codex_mgr.core.resume.find_codex_binary", return_value=Path("synthetic-codex")),
        patch("codex_mgr.core.resume.apply_settings") as activated,
        patch("codex_mgr.core.bridge.create_session_backup", side_effect=OSError),
    ):
        assert resume_session(session, "friendly", settings=settings)[0] == 1
    activated.assert_not_called()
    assert session.rollout_path.read_bytes() == original


def test_changed_preview_stops_before_configuration_or_launch(tmp_path):
    settings, session = setup(tmp_path)
    with (
        patch("codex_mgr.core.resume.find_codex_binary", return_value=Path("synthetic-codex")),
        patch("codex_mgr.core.resume.launch_codex") as launched,
    ):
        preview = preview_resume(session, "friendly", settings=settings)
        assert not list_session_backups(SID)
        session.rollout_path.write_bytes(
            session.rollout_path.read_bytes() + b'{"type":"unknown","external":true}\n'
        )
        code, message = resume_session(
            session, "friendly", expected_digest=preview.session_digest, settings=settings
        )
    assert code == 1 and "changed after preview" in message
    assert not get_config_toml_path().exists() and not list_session_backups(SID)
    launched.assert_not_called()


def test_publication_failure_reports_selected_configuration_without_launch(tmp_path):
    settings, session = setup(tmp_path)
    before = session.rollout_path.read_bytes()
    with (
        patch("codex_mgr.core.resume.find_codex_binary", return_value=Path("synthetic-codex")),
        patch("codex_mgr.core.resume.launch_codex") as launched,
        patch("codex_mgr.core.bridge.PreparedBridge.commit", side_effect=PermissionError),
    ):
        code, message = resume_session(session, "friendly", settings=settings)
    assert code == 1 and "connection was selected" in message
    assert session.rollout_path.read_bytes() == before
    assert list_session_backups(SID)
    launched.assert_not_called()
