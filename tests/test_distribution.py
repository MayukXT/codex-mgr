"""Reject private files and misleading metadata in actual archive containers."""

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "distribution_check", Path(__file__).resolve().parents[1] / "scripts/check_distribution.py"
)
distribution = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(distribution)

METADATA = b"Metadata-Version: 2.4\nName: cmgr-cli\nVersion: 1.0.0\n"
ENTRYPOINTS = b"[console_scripts]\ncmgr=codex_mgr.cli:main\ncodex-mgr=codex_mgr.cli:main\n"


def wheel(tmp_path, extras=None, metadata=METADATA, entrypoints=ENTRYPOINTS):
    path = tmp_path / "cmgr_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("codex_mgr/__init__.py", '__version__="1.0.0"\n')
        archive.writestr("cmgr_cli-1.0.0.dist-info/METADATA", metadata)
        archive.writestr("cmgr_cli-1.0.0.dist-info/entry_points.txt", entrypoints)
        for name, data in (extras or {}).items():
            member = zipfile.ZipInfo(name)
            member.filename = name  # Preserve raw names rather than Windows writer normalization.
            archive.writestr(member, data)
    return path


def source_archive(tmp_path, extras=None, *, link=None):
    path = tmp_path / "cmgr_cli-1.0.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in {
            "PKG-INFO": METADATA,
            ".gitignore": b"dist/\n",
            **(extras or {}),
        }.items():
            member = tarfile.TarInfo("cmgr_cli-1.0.0/" + name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if link:
            member = tarfile.TarInfo("cmgr_cli-1.0.0/src/codex_mgr/link.py")
            member.type = tarfile.SYMTYPE
            member.linkname = link
            archive.addfile(member)
    return path


def test_public_archives_accept_metadata_and_entrypoints(tmp_path):
    assert distribution.inspect_archive(wheel(tmp_path), version="1.0.0") == 3
    assert distribution.inspect_archive(source_archive(tmp_path), version="1.0.0") == 2


@pytest.mark.parametrize(
    "name",
    [
        "docs/obsidian/Architecture.md",
        "docs/superpowers/plan.md",
        "design.md",
        "graphify-out/graph.json",
        "output-dump/history.txt",
        "auth.json",
        "src/codex_mgr/__pycache__/local.pyc",
        "../outside.txt",
    ],
)
def test_private_source_members_are_rejected(tmp_path, name):
    with pytest.raises(distribution.DistributionError):
        distribution.inspect_archive(
            source_archive(tmp_path, {name: b"synthetic"}), version="1.0.0"
        )


@pytest.mark.parametrize(
    "name",
    [
        "codex_mgr/auth.json",
        "codex_mgr/cache.pyc",
        "codex_mgr/__pycache__/private.py",
        "codex_mgr/../../outside.py",
        "docs/README.md",
        "codex_mgr\\private.py",
    ],
)
def test_wheel_members_must_be_runtime_source_or_known_metadata(tmp_path, name):
    with pytest.raises(distribution.DistributionError):
        distribution.inspect_archive(wheel(tmp_path, {name: b"synthetic"}), version="1.0.0")


def test_links_are_never_followed_or_packaged(tmp_path):
    with pytest.raises(distribution.DistributionError):
        distribution.inspect_archive(
            source_archive(tmp_path, link="../../../private.txt"), version="1.0.0"
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"sk-" + b"A" * 32,
        b"-----BEGIN " + b"PRIVATE KEY-----",
        b"C:" + bytes([92]) + b"Users" + bytes([92]) + b"synthetic",
        b"/" + b"home/" + b"synthetic",
    ],
)
def test_content_patterns_fail_without_echoing_values(tmp_path, payload):
    with pytest.raises(distribution.DistributionError) as error:
        distribution.inspect_archive(
            wheel(tmp_path, {"codex_mgr/fixture.py": payload}), version="1.0.0"
        )
    assert payload.decode() not in str(error.value)


def test_metadata_version_must_match_source(tmp_path):
    with pytest.raises(distribution.DistributionError, match="metadata"):
        distribution.inspect_archive(
            wheel(tmp_path, metadata=METADATA.replace(b"1.0.0", b"1.0.1")), version="1.0.0"
        )


def test_both_commands_must_have_the_correct_entrypoint(tmp_path):
    with pytest.raises(distribution.DistributionError, match="entry points"):
        distribution.inspect_archive(
            wheel(tmp_path, entrypoints=b"[console_scripts]\ncmgr=wrong:main\n"), version="1.0.0"
        )


def test_normalized_duplicate_member_is_rejected(tmp_path):
    with pytest.raises(distribution.DistributionError, match="duplicate"):
        distribution.inspect_archive(
            wheel(tmp_path, {"./codex_mgr/__init__.py": b"duplicate"}), version="1.0.0"
        )


def test_check_command_returns_nonzero_for_unreadable_archive(tmp_path, capsys):
    path = tmp_path / "cmgr_cli-1.0.0-py3-none-any.whl"
    path.write_bytes(b"invalid archive")
    assert distribution.main([str(path)]) == 1
    assert "failed" in capsys.readouterr().err
