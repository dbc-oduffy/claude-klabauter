"""Standing tripwire (2026-08-07, F12): no ``re.sub``/``<compiled>.sub`` call in
``coordinator_core/`` production code may pass a non-literal, non-callable
REPLACEMENT argument.

The defect this gate exists to prevent recurring
=================================================
``re.sub``/``Pattern.sub`` interprets backslash escapes in its REPLACEMENT
argument, not just its pattern argument. Passing a runtime value straight
through as the replacement means any value beginning with a recognized
escape sequence -- most commonly a Windows path beginning with a drive
letter followed by ``\\Users`` (``\\U`` is not a valid escape) -- raises ``re.PatternError`` instead of
substituting literally. This is the exact class that made the
destructive-rm guard deny every Windows ``$HOME`` command (F12); three
sites in ``coordinator_core/percolate/`` carried the identical defect
(``rewrite_path.rewrite_paths``, ``substitute._apply_entry_to_line``,
``substitute._recapitalize_sentence_initial``), fixed alongside this gate.
The canonical fix -- ``_expand_home_var``
(``coordinator_core/bash_guards/dispatch_checks.py``) -- passes a callable
(``lambda _m: home``) instead of the raw value as the replacement, which is
immune: a callable's return value is inserted verbatim, with no escape
re-interpretation.

Spec backlink: docs/plans/2026-08-07-install-dogfood-mechanical-residue.md
§ Tasks C1/C2, AC1/AC3.

Scope
=====
Only ``coordinator_core/`` PRODUCTION code is walked -- every ``*/tests/``
directory is excluded. Test fixtures routinely build throwaway replacement
strings (e.g. ``test_no_forked_frontmatter_key_regex.py``'s own fixtures)
that are not part of the operator-facing crash surface this gate protects,
and excluding them keeps the population this repo can hold at zero small
and legible.

Only the REPLACEMENT argument is checked -- never the PATTERN argument.
``re.escape`` on a pattern is already idiomatic throughout this repo (20+
correct uses); flagging pattern arguments would produce a test nobody could
keep green (Anti-scope, plan cited above).

"Callable" is read conservatively: a ``lambda``, or a bare ``Name``
reference that resolves to a function DEFINED IN THE SAME MODULE (a
``def``/``async def`` anywhere in the file, module-level or nested) --
anything else (an f-string, a ``+`` concat, a subscript, an imported name, a
plain variable holding a string) is flagged. A local-variable ``Name`` like
``dst`` in ``pattern.sub(dst, text)`` must NOT be accepted just because it
is syntactically a ``Name`` -- that shape is exactly the F12 defect
(``rewrite_paths``' pre-fix form). Restricting acceptance to names the
module itself defines as functions keeps the common ``lambda m: ...`` and
named-local-helper idioms accepted while refusing to guess at an arbitrary
identifier's runtime type.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

# `re` module functions taking (pattern, repl, string, ...); `<compiled>.sub`
# takes (repl, string, ...) with no leading pattern argument.
_MODULE_SUB_NAMES = {"sub", "subn"}

# Explicit, dated, narrowly-scoped allowlist: (relative_path, enclosing
# function). Each entry is a KNOWN-SAFE non-path replacement, not a
# case this gate declines to reason about -- see each comment for why.
_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "coordinator_core/ops/coordinator_setup_state.py",
        "cmd_record",
    ): "replacement is f'{key}: {now}' -- a milestone name plus an ISO "
       "timestamp, never a filesystem path or store-authored free text.",
    (
        "coordinator_core/tests/test_no_forked_frontmatter_key_regex.py",
        "_render_pattern",
    ): "_VAR is a module-constant \\x00 sentinel used to mark an "
       "interpolated span in a RENDERED PATTERN (never applied as a live "
       "re.sub replacement against real content) -- this call target is "
       "re.sub(r'%[sr]', _VAR, left), substituting a sentinel into a "
       "pattern-source string for later regex construction.",
}


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        # `root` is outside the repo (a tmp_path self-test fixture) -- a
        # root-relative path simply never matches a real allowlist entry.
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_excluded_source_path(relpath: str, path: Path) -> bool:
    return "tests" in path.parts


def _enclosing_function(tree: ast.Module, target: ast.AST) -> str:
    """Name of the innermost function/method containing `target`, or
    "<module>" if it sits at module level."""
    best: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    best = node.name
    return best or "<module>"


def _module_defined_function_names(tree: ast.Module) -> set[str]:
    """Every function name `def`/`async def` anywhere in the module -- the
    population a bare `Name` replacement argument is allowed to reference."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _is_callable_expr(node: ast.expr, defined_function_names: set[str]) -> bool:
    if isinstance(node, ast.Lambda):
        return True
    if isinstance(node, ast.Name) and node.id in defined_function_names:
        return True
    return False


def _sub_repl_arg(call: ast.Call) -> ast.expr | None:
    """The replacement argument of a `re.sub`/`re.subn` or
    `<compiled>.sub`/`<compiled>.subn` call, keyword or positional.

    Argument position differs by shape: the module-level function is
    `re.sub(pattern, repl, string, ...)` (repl at index 1); a compiled
    pattern's bound method is `pattern.sub(repl, string, ...)` (repl at
    index 0). Distinguished by whether the call target is `re.sub(...)`
    (an Attribute on a Name `re`) vs `<expr>.sub(...)` on anything else.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _MODULE_SUB_NAMES:
        return None

    for kw in call.keywords:
        if kw.arg == "repl":
            return kw.value

    is_module_level = isinstance(func.value, ast.Name) and func.value.id == "re"
    repl_index = 1 if is_module_level else 0
    if len(call.args) > repl_index:
        return call.args[repl_index]
    return None


def find_non_literal_sub_replacements(root: Path) -> list[tuple[str, int, str]]:
    """Walk `root` for .py files and return every
    `(relpath, lineno, enclosing_function)` where a `re.sub`/`re.subn` or
    `<compiled>.sub`/`<compiled>.subn` call's replacement argument is
    neither a string literal nor a callable reference.

    Excludes `*/tests/` directories (see module docstring § Scope) and
    entries in `_ALLOWLIST`.
    """
    violations: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        relpath = _relpath(path, root)
        if _is_excluded_source_path(relpath, path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        defined_function_names = _module_defined_function_names(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            repl = _sub_repl_arg(node)
            if repl is None:
                continue
            if isinstance(repl, ast.Constant) and isinstance(repl.value, str):
                continue
            if _is_callable_expr(repl, defined_function_names):
                continue

            func_name = _enclosing_function(tree, node)
            if (relpath, func_name) in _ALLOWLIST:
                continue
            violations.append((relpath, node.lineno, func_name))
    return violations


def test_no_re_sub_call_takes_a_non_literal_non_callable_replacement():
    """Standing gate (AC1/AC3): every `re.sub`/`<compiled>.sub` replacement
    in `coordinator_core/` production code is a string literal or a
    callable, so a runtime value is never re-interpreted for backslash
    escapes."""
    violations = find_non_literal_sub_replacements(_SCAN_ROOT)
    assert violations == [], (
        "Found re.sub/<compiled>.sub call(s) whose REPLACEMENT argument is "
        "neither a string literal nor a callable -- a runtime value passed "
        "this way has its backslash escapes re-interpreted by re.sub, which "
        "raises re.PatternError on a Windows path (e.g. a drive-letter path "
        "beginning `\\Users` "
        "begins `\\U`, not a valid escape). Convert to a callable "
        f"(`lambda _m: value`): {violations}"
    )


def test_gate_detects_a_raw_value_replacement(tmp_path):
    fixture = tmp_path / "fixture_raw_value.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def rewrite(pattern, dst, text):\n"
        "    return pattern.sub(dst, text)\n",
        encoding="utf-8",
    )

    violations = find_non_literal_sub_replacements(tmp_path)

    assert len(violations) == 1, violations
    relpath, lineno, func_name = violations[0]
    assert relpath == "fixture_raw_value.py"
    assert lineno == 4
    assert func_name == "rewrite"


def test_gate_detects_a_backreference_concat_replacement(tmp_path):
    fixture = tmp_path / "fixture_concat.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def recap(text, escaped, capitalized):\n"
        "    return re.sub(r'([.?!] )' + escaped, r'\\1' + capitalized, text)\n",
        encoding="utf-8",
    )

    violations = find_non_literal_sub_replacements(tmp_path)

    assert len(violations) == 1, violations
    assert violations[0][2] == "recap"


def test_gate_accepts_a_string_literal_replacement(tmp_path):
    fixture = tmp_path / "fixture_literal.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def collapse(text):\n"
        "    return re.sub(r'\\b(?:The|the) (the) ', r'\\1 ', text)\n",
        encoding="utf-8",
    )

    assert find_non_literal_sub_replacements(tmp_path) == []


def test_gate_accepts_a_lambda_replacement(tmp_path):
    fixture = tmp_path / "fixture_lambda.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def rewrite(pattern, dst, text):\n"
        "    return pattern.sub(lambda _m: dst, text)\n",
        encoding="utf-8",
    )

    assert find_non_literal_sub_replacements(tmp_path) == []


def test_gate_accepts_a_named_function_reference_replacement(tmp_path):
    fixture = tmp_path / "fixture_named_ref.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def _expand(m):\n"
        "    return m.group(0).upper()\n"
        "\n"
        "def rewrite(pattern, text):\n"
        "    return pattern.sub(_expand, text)\n",
        encoding="utf-8",
    )

    assert find_non_literal_sub_replacements(tmp_path) == []


def test_gate_does_not_flag_the_pattern_argument(tmp_path):
    """Negative control: an f-string or concatenated PATTERN argument is
    never flagged -- only the replacement position matters."""
    fixture = tmp_path / "fixture_pattern_only.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def find(key, text):\n"
        "    escaped = re.escape(key)\n"
        "    return re.sub(r'^' + escaped + r'\\b', 'FIXED', text)\n",
        encoding="utf-8",
    )

    assert find_non_literal_sub_replacements(tmp_path) == []


def test_gate_ignores_test_directories(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    fixture = tests_dir / "test_something.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def build(pattern, dst, text):\n"
        "    return pattern.sub(dst, text)\n",
        encoding="utf-8",
    )

    assert find_non_literal_sub_replacements(tmp_path) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
