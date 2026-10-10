"""Menu search/page/filter navigation and shared resume handoff."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codex_mgr.core.resume import ResumePreview
from codex_mgr.core.sessions import SessionCatalog, SessionItem
from codex_mgr.tui.session_picker import interactive_resume_session, pick_session


def item(number, title=None):
    return SessionItem(
        f"00000000-0000-0000-0000-{number:012d}",
        title or f"Session {number}",
        "",
        "Unknown",
        Path("synthetic.jsonl"),
        "fixture",
    )


def test_paging_reaches_all_records_without_rescanning():
    items = [item(number) for number in range(30)]
    calls = []

    def menu(message, choices):
        calls.append(choices)
        return "next" if len(calls) == 1 else items[12]

    with (
        patch(
            "codex_mgr.tui.session_picker.discover_sessions", return_value=SessionCatalog(items)
        ) as scan,
        patch("codex_mgr.tui.session_picker.select_menu", side_effect=menu),
        patch("codex_mgr.tui.session_picker.console", SimpleNamespace(height=40, width=80)),
    ):
        assert pick_session() == items[12]
    assert calls[0][0].value == items[0]
    assert calls[1][0].value == items[12]
    scan.assert_called_once_with(include_archived=False)


def test_search_filters_the_full_catalog_and_cancel_returns():
    items = [item(number) for number in range(131)]
    with (
        patch("codex_mgr.tui.session_picker.discover_sessions", return_value=SessionCatalog(items)),
        patch(
            "codex_mgr.tui.session_picker.select_menu", side_effect=["search", items[-1]]
        ) as menu,
        patch("codex_mgr.tui.session_picker.inquirer.text") as search,
    ):
        search.return_value.execute.return_value = "Session 130"
        assert pick_session() == items[-1]
        assert [
            choice.value
            for choice in menu.call_args.args[1]
            if isinstance(choice.value, SessionItem)
        ] == [items[-1]]
    with (
        patch("codex_mgr.tui.session_picker.discover_sessions", return_value=SessionCatalog(items)),
        patch("codex_mgr.tui.session_picker.select_menu", return_value=None),
    ):
        assert pick_session() is None


def test_tui_confirmation_passes_preview_digest_to_shared_resume():
    session = item(1)
    settings = object()
    preview = ResumePreview("actual_connection", "synthetic-digest", 2, 1)
    with (
        patch("codex_mgr.tui.session_picker.pick_session", return_value=session),
        patch("codex_mgr.tui.session_picker.show_session_details"),
        patch("codex_mgr.tui.session_picker.load_settings", return_value=settings),
        patch("codex_mgr.tui.session_picker.preview_resume", return_value=preview),
        patch("codex_mgr.tui.session_picker.resume_session", return_value=(0, "closed")) as resume,
        patch("codex_mgr.tui.session_picker.inquirer.confirm") as confirm,
    ):
        confirm.return_value.execute.return_value = True
        interactive_resume_session("friendly")
    resume.assert_called_once_with(
        session,
        "friendly",
        allow_reasoning_loss=True,
        expected_digest="synthetic-digest",
        settings=settings,
    )


def test_tui_refusal_does_not_activate_or_convert():
    with (
        patch("codex_mgr.tui.session_picker.pick_session", return_value=item(1)),
        patch("codex_mgr.tui.session_picker.show_session_details"),
        patch("codex_mgr.tui.session_picker.load_settings", return_value=object()),
        patch(
            "codex_mgr.tui.session_picker.preview_resume",
            return_value=ResumePreview("fixture", "digest", 2, 1),
        ),
        patch("codex_mgr.tui.session_picker.resume_session") as resume,
        patch("codex_mgr.tui.session_picker.inquirer.confirm") as confirm,
    ):
        confirm.return_value.execute.return_value = False
        interactive_resume_session()
    resume.assert_not_called()


@pytest.mark.parametrize("accepted", [False, True])
def test_tui_discloses_independent_identity_before_confirmation_and_after_launch(accepted):
    original = item(1)
    new_id = "00000000-0000-4000-8000-000000000002"
    message = f"Independent session: {new_id}. Original session: {original.id} (preserved)."
    with (
        patch("codex_mgr.tui.session_picker.pick_session", return_value=original),
        patch("codex_mgr.tui.session_picker.show_session_details"),
        patch("codex_mgr.tui.session_picker.load_settings", return_value=object()),
        patch(
            "codex_mgr.tui.session_picker.preview_resume",
            return_value=ResumePreview("target", "digest", 1, 0, True),
        ),
        patch("codex_mgr.tui.session_picker.console") as console,
        patch("codex_mgr.tui.session_picker.resume_session", return_value=(0, message)) as resume,
        patch("codex_mgr.tui.session_picker.inquirer.confirm") as confirm,
    ):

        def answer(**kwargs):
            assert kwargs["default"] is False
            assert kwargs["message"] == "Save a recovery copy and resume?"
            assert any(original.id in call.args[0] for call in console.print.call_args_list)
            return confirm.return_value

        confirm.side_effect = answer
        confirm.return_value.execute.return_value = accepted
        interactive_resume_session()
    assert resume.call_count == int(accepted)
    assert any(new_id in call.args[0] for call in console.print.call_args_list) == accepted
