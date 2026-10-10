"""Literal prompts, native wrapper resolution and foreground exit status."""

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_mgr.core import runner
from codex_mgr.utils import paths

SID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def binary(tmp_path, monkeypatch):
    path = tmp_path / "codex.exe"
    path.write_bytes(b"synthetic executable")
    monkeypatch.setattr(runner, "find_codex_binary", lambda: path)
    return path


@pytest.mark.parametrize("session_id", [None, SID])
def test_prompt_is_an_exact_positional_argument_with_no_shell(session_id, binary, monkeypatch):
    prompt = '-literal & echo "quoted" | redirect > file %VALUE%\nsecond line'
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(subprocess, "run", run)
    assert runner.launch_codex(session_id, prompt, ["--no-alt-screen"]) == 0
    expected = (
        [str(binary)] + (["resume", SID] if session_id else []) + ["--no-alt-screen", "--", prompt]
    )
    run.assert_called_once_with(expected, check=False)


def test_invalid_uuid_never_launches(binary, monkeypatch):
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    assert runner.launch_codex("short-id") == 1
    run.assert_not_called()


@pytest.mark.parametrize("returncode,expected", [(0, 0), (7, 7), (-2, 130), (-15, 143)])
def test_native_exit_or_signal_is_preserved(returncode, expected, binary, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=returncode))
    assert runner.launch_codex() == expected


@pytest.mark.parametrize(
    "error,expected", [(KeyboardInterrupt(), 130), (OSError("private-path-and-token"), 1)]
)
def test_interruption_and_startup_error_are_not_success(
    error, expected, binary, monkeypatch, capsys
):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    assert runner.launch_codex() == expected
    assert "private-path-and-token" not in capsys.readouterr().out


def test_unresolved_windows_shell_wrapper_is_not_executed(binary, monkeypatch):
    shim = binary.with_suffix(".cmd")
    shim.write_bytes(b"synthetic")
    monkeypatch.setattr(runner, "find_codex_binary", lambda: shim)
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    assert runner.launch_codex(prompt="literal & prompt") == 1
    run.assert_not_called()


@pytest.mark.parametrize("layout", ["nested", "flat", "bundled"])
@pytest.mark.parametrize(
    "architecture,triple,suffix",
    [
        ("AMD64", "x86_64-pc-windows-msvc", "x64"),
        ("ARM64", "aarch64-pc-windows-msvc", "arm64"),
    ],
)
def test_npm_layouts_resolve_native_windows_binary(
    layout, architecture, triple, suffix, tmp_path, monkeypatch
):
    shim = tmp_path / "npm" / "codex.cmd"
    shim.parent.mkdir()
    shim.write_bytes(b"synthetic shim")
    namespace = shim.parent / "node_modules" / "@openai"
    package = namespace / "codex"
    root = (
        (package / "node_modules" / "@openai" / f"codex-win32-{suffix}")
        if layout == "nested"
        else namespace / f"codex-win32-{suffix}"
        if layout == "flat"
        else package
    )
    native = root / "vendor" / triple / "bin" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"synthetic native executable")
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setattr(paths.platform, "machine", lambda: architecture)
    monkeypatch.setattr(paths.shutil, "which", lambda _: str(shim))
    assert paths.find_codex_binary() == native.resolve()
