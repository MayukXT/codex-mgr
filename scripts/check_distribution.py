"""Check public archive contents and optionally smoke-test isolated installs."""

from __future__ import annotations

import argparse
import ast
import configparser
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename

_ROOT_FILES = {"README.md", "FORDEV.md", "LICENSE", "pyproject.toml", "PKG-INFO", ".gitignore"}
_FIXTURES = {"README.md", "LICENSE-Codex.txt", "codex-0.159.0-config.schema.json"}
_SECRET = re.compile(
    rb"(?:sk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{20,}|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----)"
)
_PRIVATE_PATH = re.compile(rb"(?i)(?:[A-Z]:\\Users\\|/(?:Users|home)/)[^\s\"'<>]+")


class DistributionError(ValueError):
    pass


def _relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or not path.parts
        or any(ord(c) < 32 for c in name)
    ):
        raise DistributionError("Archive contains an unsafe path.")
    return path


def _public_source(path: PurePosixPath) -> bool:
    parts = path.parts
    if str(path) in _ROOT_FILES:
        return True
    if parts[:2] == ("src", "codex_mgr") and path.suffix == ".py":
        return "__pycache__" not in parts
    if parts[0] == "tests" and path.suffix == ".py":
        return "__pycache__" not in parts
    if parts[:2] == ("tests", "fixtures") and len(parts) == 3:
        return path.name in _FIXTURES
    if parts[:2] == ("docs", "wiki") and len(parts) == 3 and path.suffix == ".md":
        return True
    return str(path) in {"scripts/check_distribution.py", ".github/workflows/ci.yml"}


def _scan(data: bytes) -> None:
    if _SECRET.search(data) or _PRIVATE_PATH.search(data):
        raise DistributionError(
            "Archive content matches a credential or private-home path pattern."
        )


def _metadata(data: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(data)
    if (
        canonicalize_name(metadata.get("Name", "")) != "cmgr-cli"
        or metadata.get("Version") != version
    ):
        raise DistributionError("Distribution metadata does not match the package/version.")


def inspect_archive(path: Path, *, version: str) -> int:
    """Reject private/unexpected members, links, duplicates and suspicious content."""
    seen = set()
    if path.suffix == ".whl":
        name, archive_version, _, _ = parse_wheel_filename(path.name)
        if name != "cmgr-cli" or str(archive_version) != version:
            raise DistributionError("Wheel filename does not match the package/version.")
        with zipfile.ZipFile(path) as archive:
            metadata = entrypoints = None
            for member in archive.infolist():
                relative = _relative(member.orig_filename)
                if member.is_dir():
                    continue
                if str(relative) in seen or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise DistributionError("Wheel has duplicate or linked members.")
                seen.add(str(relative))
                if member.file_size > 16_777_216:
                    raise DistributionError("Wheel member exceeds the supported size.")
                parts = relative.parts
                runtime = (
                    parts[0] == "codex_mgr"
                    and relative.suffix == ".py"
                    and "__pycache__" not in parts
                )
                info = parts[0] == f"cmgr_cli-{version}.dist-info" and (
                    len(parts) == 2
                    and parts[1] in {"METADATA", "WHEEL", "RECORD", "entry_points.txt"}
                    or len(parts) == 3
                    and parts[1:] == ("licenses", "LICENSE")
                )
                if not (runtime or info):
                    raise DistributionError("Wheel contains a non-public or unexpected member.")
                data = archive.read(member)
                _scan(data)
                if relative.name == "METADATA":
                    metadata = data
                elif relative.name == "entry_points.txt":
                    entrypoints = data
            if metadata is None or entrypoints is None:
                raise DistributionError("Wheel is missing package metadata or entry points.")
            _metadata(metadata, version)
            parser = configparser.ConfigParser()
            parser.read_string(entrypoints.decode("utf-8"))
            expected = {"cmgr": "codex_mgr.cli:main", "codex-mgr": "codex_mgr.cli:main"}
            if (
                not parser.has_section("console_scripts")
                or dict(parser["console_scripts"]) != expected
            ):
                raise DistributionError("Installed command entry points do not match.")
    else:
        name, archive_version = parse_sdist_filename(path.name)
        if name != "cmgr-cli" or str(archive_version) != version:
            raise DistributionError("Source archive filename does not match the package/version.")
        with tarfile.open(path, "r:gz") as archive:
            metadata = None
            for member in archive:
                full = _relative(member.name)
                if full.parts[0] != f"cmgr_cli-{version}":
                    raise DistributionError("Source archive has an unexpected root.")
                if member.isdir():
                    continue
                if len(full.parts) < 2:
                    raise DistributionError("Source archive has an invalid file path.")
                relative = PurePosixPath(*full.parts[1:])
                if not member.isfile() or str(full) in seen or not _public_source(relative):
                    raise DistributionError(
                        "Source archive contains a non-public, duplicate or linked member."
                    )
                seen.add(str(full))
                if member.size > 16_777_216:
                    raise DistributionError("Source member exceeds the supported size.")
                handle = archive.extractfile(member)
                if handle is None:
                    raise DistributionError("Source archive contains unreadable data.")
                with handle:
                    data = handle.read()
                _scan(data)
                if relative.name == "PKG-INFO":
                    metadata = data
            if metadata is None:
                raise DistributionError("Source archive is missing metadata.")
            _metadata(metadata, version)
    if not seen:
        raise DistributionError("Distribution is empty.")
    return len(seen)


def source_version(root: Path) -> str:
    tree = ast.parse((root / "src/codex_mgr/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise DistributionError("Source package version is missing.")


def smoke_install(archive: Path, *, version: str) -> None:
    """Install each archive in its own environment, with no developer config access."""
    with tempfile.TemporaryDirectory(prefix="cmgr-install-") as directory:
        root = Path(directory)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        interpreter = scripts / ("python.exe" if os.name == "nt" else "python")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(root / "codex-home")
        env["CODEX_SQLITE_HOME"] = str(root / "codex-sqlite")
        env.pop("PYTHONPATH", None)
        for key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "CMGR_NATIVE_CONTRACTS",
        ):
            env.pop(key, None)
        result = subprocess.run(
            [
                str(interpreter),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(archive.resolve()),
            ],
            env=env,
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
        if result.returncode:
            raise DistributionError("Archive could not be installed in its clean environment.")
        code = "import codex_mgr,importlib.metadata,json,sys;print(json.dumps([codex_mgr.__version__,importlib.metadata.version('cmgr-cli'),codex_mgr.__file__,sys.prefix]))"
        result = subprocess.run(
            [str(interpreter), "-c", code],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            raise DistributionError("Installed package import failed.")
        runtime, distribution, module, prefix = json.loads(result.stdout)
        if (
            runtime != version
            or distribution != version
            or Path(prefix).resolve() != environment.resolve()
            or not Path(module).resolve().is_relative_to(environment.resolve())
        ):
            raise DistributionError("Installed package came from another environment or version.")
        for command in ("cmgr", "codex-mgr"):
            executable = scripts / (command + ".exe" if os.name == "nt" else command)
            for flag in ("--version", "--help"):
                result = subprocess.run(
                    [str(executable), flag],
                    env=env,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if result.returncode or (
                    flag == "--version" and result.stdout.strip() != f"codex-mgr {version}"
                ):
                    raise DistributionError("Installed command smoke check failed.")
        if (root / "codex-home").exists() or (root / "codex-sqlite").exists():
            raise DistributionError("Help/version commands changed isolated runtime storage.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--install-smoke", action="store_true")
    args = parser.parse_args(argv)
    try:
        version = source_version(Path(__file__).resolve().parents[1])
        for archive in args.archives:
            count = inspect_archive(archive, version=version)
            if args.install_smoke:
                smoke_install(archive, version=version)
            print(
                f"Verified {archive.name}: {count} public members"
                + ("; clean install/entry points passed" if args.install_smoke else "")
            )
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile):
        print(
            "Distribution verification failed. Inspect archive contents, metadata or the isolated installation.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
