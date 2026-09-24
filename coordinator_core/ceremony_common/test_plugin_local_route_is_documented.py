"""
coordinator_core.ceremony_common.test_plugin_local_route_is_documented —
AC11 (docs/plans/2026-09-07-directive-resolution-reaches-a-plugin-local-
cli.md).

Purpose: "the route is documented where dispatch is defined, not only in
this plan" (baton AC2) is a claim about three specific docstrings plus one
reference page, all four checkable, none of them exercised by importing the
modules — `ast.get_docstring` reads the source directly so this test cannot
be satisfied by a docstring that merely happens to be present at import
time under some other mutation.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE_PAGE = _REPO_ROOT / "docs" / "reference" / "plugin-local-cli-dispatch.md"

_TWO_ROOT_MODEL_MARKER = "two-root"


def _module_docstring(relative_path: str) -> str:
    source_path = _REPO_ROOT / relative_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree)
    assert docstring is not None, f"{relative_path} has no module docstring"
    return docstring


def _function_docstring(relative_path: str, function_name: str) -> str:
    source_path = _REPO_ROOT / relative_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            docstring = ast.get_docstring(node)
            assert docstring is not None, (
                f"{relative_path}::{function_name} has no docstring"
            )
            return docstring
    raise AssertionError(f"{function_name} not found in {relative_path}")


def test_reference_page_exists() -> None:
    assert _REFERENCE_PAGE.is_file(), (
        f"{_REFERENCE_PAGE} must exist — AC11 names it as the cited page"
    )


def test_apply_module_docstring_names_model_and_cites_page() -> None:
    docstring = _module_docstring("coordinator_core/workstream_complete/apply.py")
    lowered = docstring.lower()
    assert "plugin-local-cli-dispatch.md" in docstring
    assert _TWO_ROOT_MODEL_MARKER in lowered or "second" in lowered


def test_cli_dispatch_module_docstring_names_model_and_cites_page() -> None:
    docstring = _module_docstring("coordinator_core/ceremony_common/cli_dispatch.py")
    assert "plugin-local-cli-dispatch.md" in docstring
    assert "TWO-ROOT" in docstring or "two-root" in docstring.lower()


def test_resolve_cli_docstring_names_model_and_cites_page() -> None:
    docstring = _function_docstring(
        "coordinator_core/contract/apply_base.py", "resolve_cli"
    )
    assert "plugin-local-cli-dispatch.md" in docstring
    assert "two producer root" in docstring.lower() or "two-root" in docstring.lower()
