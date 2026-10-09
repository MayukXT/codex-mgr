"""Diagnostics report evidence without mutating files or revealing credentials."""

import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console

from codex_mgr.cli import main
from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.core import doctor
from codex_mgr.core.auth import LoginStatus
from codex_mgr.tui import doctor_view
from codex_mgr.utils.paths import get_codex_home


@pytest.fixture
def local_checks(monkeypatch):
    monkeypatch.setattr(doctor, "find_codex_binary", lambda: Path("synthetic-codex"))
    monkeypatch.setattr(
        doctor, "codex_login_status", lambda: LoginStatus(True, "chatgpt", "synthetic")
    )
    network = Mock(return_value=(True, "raw provider text must stay private", 12))
    monkeypatch.setattr(doctor, "test_provider_connectivity", network)
    return network


def by_name(results):
    return {result.name: result for result in results}


def saved_api(*, token="synthetic-secret", protocol="responses"):
    provider = ProviderConfig(
        id="fixture",
        name="Synthetic connection",
        base_url="https://fixture.invalid/v1",
        auth_token=token,
        wire_api=protocol,
        default_model="exact-fixture-model",
    )
    settings = CmgrSettings(active_provider_id=provider.id, providers={provider.id: provider})
    home = get_codex_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "cmgr.json").write_text(settings.model_dump_json(), encoding="utf-8")
    return settings


def test_empty_home_is_not_created_and_unknown_checks_are_explicit(local_checks):
    home = get_codex_home()
    assert not home.exists()
    results = by_name(doctor.run_diagnostics())
    assert not home.exists()
    local_checks.assert_not_called()
    assert results["Local configuration"].passed
    assert results["Connection application"].status == "warning"
    assert results["Effective Codex configuration"].status == "not_checked"
    assert results["Coding workflow"].status == "not_checked"
    assert "safe" not in results["Session writer ownership"].details.lower()


@pytest.mark.parametrize(
    "configuration", ["", "# valid empty config\n", 'profile="coding"\n[profiles.coding]\n']
)
def test_valid_empty_and_selected_profile_configuration_passes(configuration, local_checks):
    home = get_codex_home()
    home.mkdir(parents=True)
    path = home / "config.toml"
    path.write_text(configuration, encoding="utf-8")
    assert by_name(doctor.run_diagnostics())["Local configuration"].passed
    assert path.read_text(encoding="utf-8") == configuration
    assert not (home / "cmgr.json").exists()
    assert not (home / "backups").exists()


@pytest.mark.parametrize("configuration", ["broken = [", 'profile="missing"'])
def test_bad_configuration_does_not_abort_other_checks(configuration, local_checks):
    home = get_codex_home()
    home.mkdir(parents=True)
    (home / "config.toml").write_text(configuration, encoding="utf-8")
    results = by_name(doctor.run_diagnostics(network=True))
    assert results["Local configuration"].status == "failed"
    assert results["Manager settings"].status == "not_checked"
    assert "Session catalog" in results
    assert "Activation recovery" in results
    local_checks.assert_not_called()


def test_malformed_settings_and_errors_never_echo_private_values(local_checks):
    home = get_codex_home()
    home.mkdir(parents=True)
    raw = b'{"providers": "private-value"}'
    (home / "cmgr.json").write_bytes(raw)
    results = doctor.run_diagnostics()
    assert by_name(results)["Manager settings"].status == "failed"
    assert "private-value" not in repr(results)
    assert (home / "cmgr.json").read_bytes() == raw


def test_selected_missing_env_cannot_borrow_other_provider_keys(local_checks, monkeypatch):
    saved_api(token="$CMGR_MISSING_FIXTURE_KEY")
    monkeypatch.delenv("CMGR_MISSING_FIXTURE_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-synthetic-key")
    results = by_name(doctor.run_diagnostics(network=True))
    assert results["Selected authentication"].status == "failed"
    assert results["Provider catalog"].status == "not_checked"
    local_checks.assert_not_called()


def test_api_keys_are_never_printed_and_catalog_access_is_opt_in(local_checks):
    saved_api()
    before = (get_codex_home() / "cmgr.json").read_bytes()
    assert by_name(doctor.run_diagnostics())["Provider catalog"].status == "not_checked"
    local_checks.assert_not_called()
    results = doctor.run_diagnostics(network=True)
    assert by_name(results)["Provider catalog"].passed
    assert by_name(results)["Coding workflow"].status == "not_checked"
    assert "synthetic-secret" not in repr(results)
    assert "raw provider text" not in repr(results)
    assert (get_codex_home() / "cmgr.json").read_bytes() == before
    assert not (get_codex_home() / "backups").exists()


@pytest.mark.parametrize("failure", [None, RuntimeError("private-exception")])
def test_failed_catalog_is_not_workflow_success(failure, local_checks):
    saved_api()
    if failure:
        local_checks.side_effect = failure
    else:
        local_checks.return_value = (False, "private-server-message", 5)
    results = doctor.run_diagnostics(network=True)
    assert by_name(results)["Provider catalog"].status == "failed"
    assert "private-" not in repr(results)


def test_wrong_codex_auth_method_does_not_satisfy_oauth(local_checks, monkeypatch):
    monkeypatch.setattr(
        doctor, "codex_login_status", lambda: LoginStatus(True, "api_key", "private-key")
    )
    results = by_name(doctor.run_diagnostics(network=True))
    assert results["Selected authentication"].status == "failed"
    assert results["Provider catalog"].status == "not_checked"
    local_checks.assert_not_called()


def test_unsupported_transport_is_reported_before_activation(local_checks):
    saved_api(protocol="anthropic_proxy")
    results = by_name(doctor.run_diagnostics())
    assert results["Connection application"].status == "failed"
    assert results["Selected authentication"].passed


def test_full_catalog_and_snapshot_counts_exclude_sidecars_and_journals(local_checks):
    home = get_codex_home()
    home.mkdir(parents=True)
    index = [json.dumps({"id": f"00000000-0000-0000-0000-{i:012d}"}) for i in range(131)]
    (home / "session_index.jsonl").write_text("\n".join(index), encoding="utf-8")
    snapshot = home / "backups" / "sessions" / "synthetic" / "copy.rollout.jsonl"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"synthetic")
    snapshot.with_suffix(".sha256").write_bytes(b"synthetic")
    journal = home / "backups" / "transactions" / "synthetic" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"state":"prepared"}', encoding="utf-8")
    results = by_name(doctor.run_diagnostics())
    assert "131 sessions" in results["Session catalog"].details
    assert results["Session catalog"].status == "warning"
    assert results["Backup inventory"].details.startswith("1 snapshot files")
    assert results["Activation recovery"].status == "failed"
    assert journal.read_text(encoding="utf-8") == '{"state":"prepared"}'


def test_invalid_recovery_record_returns_failure_without_repair(local_checks):
    path = get_codex_home() / "backups" / "transactions" / "fixture" / "journal.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"private invalid record")
    results = by_name(doctor.run_diagnostics())
    assert results["Backup inventory / recovery"].status == "failed"
    assert "private invalid" not in repr(results)
    assert path.read_bytes() == b"private invalid record"


def test_nonstring_recovery_state_is_a_reported_failure(local_checks):
    path = get_codex_home() / "backups" / "transactions" / "fixture" / "journal.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"state":[]}')
    results = by_name(doctor.run_diagnostics())
    assert results["Backup inventory / recovery"].status == "failed"


def test_diagnostics_reject_external_transaction_links(local_checks, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    journal = external / "journal.json"
    journal.write_bytes(b'{"state":"prepared","private":"synthetic"}')
    link = get_codex_home() / "backups" / "transactions" / "linked"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable for this user.")
    results = by_name(doctor.run_diagnostics())
    assert results["Backup inventory / recovery"].status == "failed"
    assert "Activation recovery" not in results
    assert journal.read_bytes() == b'{"state":"prepared","private":"synthetic"}'


def test_escaped_journal_is_rejected_before_read_even_without_symlink_privileges(
    local_checks, tmp_path, monkeypatch
):
    journal = get_codex_home() / "backups" / "transactions" / "fixture" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b'{"state":"prepared"}')
    real_resolve, real_read = Path.resolve, Path.read_bytes

    def resolve(path, *args, **kwargs):
        return (
            tmp_path / "outside" / "journal.json"
            if path == journal
            else real_resolve(path, *args, **kwargs)
        )

    def read(path):
        if path == journal:
            raise AssertionError("An escaped journal must not be opened.")
        return real_read(path)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "read_bytes", read)
    assert by_name(doctor.run_diagnostics())["Backup inventory / recovery"].status == "failed"


@pytest.mark.parametrize("width", [40, 60, 80, 120])
def test_report_wraps_plain_text_at_terminal_width(width, monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(
        doctor_view, "console", Console(file=stream, width=width, color_system=None)
    )
    monkeypatch.setattr(
        doctor_view,
        "run_diagnostics",
        lambda **_: [
            doctor.DiagnosticResult(
                "Label [literal]\nwith control", "failed", "details " * 30, "repair " * 12
            ),
            doctor.DiagnosticResult("Workflow", "not_checked", "No inference was sent."),
        ],
    )
    assert doctor_view.render_doctor_report() is False
    output = stream.getvalue()
    assert "[literal]" in output
    assert "1 failed; 0 warnings; 1 not checked." in output
    assert all(len(line) <= width for line in output.splitlines())
    assert output.isascii()
    assert "seamless" not in output


def test_doctor_exit_status_and_network_flag(monkeypatch):
    render = Mock(return_value=False)
    monkeypatch.setattr("codex_mgr.cli.render_doctor_report", render)
    assert main(["doctor", "--network"]) == 1
    render.assert_called_once_with(network=True)
    render.return_value = True
    assert main(["doctor"]) == 0
