"""Single-home pins for the _dc_* family: the redirection predicate trio and the
grep rewrite primitives are defined once, in `_rewrite_support`, and every other
module reaches them by import. Reads the module sources with `ast`; spawns nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

_DIR = Path(__file__).resolve().parents[1]

_TRIO = ("_BT_REDIRECTION_TOKEN_RE", "_bt_is_redirection_token", "_bt_has_redirection")
_GREP_FUNCS = (
    "_bt_grep_dialect",
    "_bt_grep_pattern_safe_for_dialect",
    "_bt_grep_translate_pattern",
    "_bt_grep_flags_and_operands",
    "_bt_grep_python_rewrite",
)
_GREP_CONSTS = (
    "_GREP_FAMILY_BINARIES_BT",
    "_GREP_SUBSTITUTABLE_SHORT_FLAGS",
    "_GREP_POSIX_BRACKET_CLASS_RE",
    "_GREP_EXTENDED_SAFE_ESCAPES",
    "_GREP_BASIC_SAFE_ESCAPES",
)


def _tree(name: str) -> ast.Module:
    return ast.parse((_DIR / name).read_text(encoding="utf-8"))


def _defined(tree: ast.Module) -> set:
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _imported_from(tree: ast.Module, module_suffix: str) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(module_suffix):
            names.update(a.name for a in node.names)
    return names


def test_trio_and_grep_primitives_are_defined_only_in_rewrite_support():
    homed = _defined(_tree("_rewrite_support.py"))
    for name in _TRIO + _GREP_FUNCS + _GREP_CONSTS:
        assert name in homed, name
    for module in ("_dc_shell_text.py", "_dc_rewrites.py", "dispatch_checks.py", "guard_grep_via_bash.py"):
        dupes = _defined(_tree(module)) & set(_TRIO + _GREP_FUNCS + _GREP_CONSTS)
        assert not dupes, (module, sorted(dupes))


def test_bundled_c_flag_re_has_no_second_definition_in_the_dc_family():
    for module in ("_dc_shell_text.py", "_dc_rewrites.py", "dispatch_checks.py"):
        assert "_BUNDLED_C_FLAG_RE" not in _defined(_tree(module)), module


def test_trio_still_imports_from_dc_shell_text_and_dispatch_checks():
    assert set(_TRIO) <= _imported_from(_tree("_dc_shell_text.py"), "_rewrite_support")
    assert set(_TRIO) <= _imported_from(_tree("dispatch_checks.py"), "_dc_shell_text")

    from coordinator_core.bash_guards import _dc_shell_text, _rewrite_support, dispatch_checks

    for name in _TRIO:
        assert getattr(_dc_shell_text, name) is getattr(_rewrite_support, name)
        assert getattr(dispatch_checks, name) is getattr(_rewrite_support, name)


def test_grep_guard_and_rewrites_take_the_primitives_from_rewrite_support():
    guard_names = _imported_from(_tree("guard_grep_via_bash.py"), "_rewrite_support")
    assert {"_bt_grep_flags_and_operands", "_bt_grep_python_rewrite"} <= guard_names
    assert not _imported_from(_tree("guard_grep_via_bash.py"), "dispatch_checks")
    assert {"_bt_grep_flags_and_operands", "_bt_grep_python_rewrite"} <= _imported_from(
        _tree("_dc_rewrites.py"), "_rewrite_support"
    )


def test_rewrite_support_imports_no_dc_module():
    for node in ast.walk(_tree("_rewrite_support.py")):
        if isinstance(node, ast.ImportFrom):
            reached = [node.module or ""] + [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            reached = [a.name for a in node.names]
        else:
            continue
        for name in reached:
            assert "_dc_" not in name and "dispatch_checks" not in name, name
