"""Internal imports must be present in the source distribution."""

import ast
from pathlib import Path


def test_internal_import_targets_exist():
    source = Path(__file__).resolve().parents[1] / "src"
    for path in (source / "codex_mgr").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                if module.startswith("codex_mgr"):
                    target = source.joinpath(*module.split("."))
                    assert target.is_dir() or target.with_suffix(".py").is_file(), (
                        path.name,
                        module,
                    )
