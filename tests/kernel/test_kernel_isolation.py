"""
tests/kernel/test_kernel_isolation.py

Ensures that app/kernel/ never imports FastAPI or aiogram.
This is a pure-Python import inspection test — no DB required.
"""

import ast
import pathlib

import pytest


KERNEL_DIR = pathlib.Path(__file__).parent.parent.parent / "app" / "kernel"

FORBIDDEN_IMPORTS = {"fastapi", "aiogram", "starlette"}


def _get_py_files(directory: pathlib.Path):
    return list(directory.rglob("*.py"))


def _extract_imports(source: str) -> list[str]:
    """Return all top-level module names imported in a Python source string."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("py_file", _get_py_files(KERNEL_DIR))
def test_kernel_file_has_no_forbidden_imports(py_file: pathlib.Path):
    """
    Every file in app/kernel/ must not import fastapi, aiogram, or starlette.
    Business logic must never depend on HTTP or bot frameworks.
    """
    source = py_file.read_text(encoding="utf-8")
    imports = set(_extract_imports(source))
    violations = imports & FORBIDDEN_IMPORTS
    assert not violations, (
        f"{py_file.relative_to(KERNEL_DIR.parent.parent)} imports forbidden modules: "
        f"{violations}. Business logic in app/kernel/ must not import HTTP or bot frameworks."
    )
