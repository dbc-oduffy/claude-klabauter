"""Standing tripwire (2026-08-07, chunk C7): the STATICALLY-DECIDABLE subset
of the "Windows path in an escaping context" defect family.

Full family, discriminator, and idioms
=======================================
See ``docs/reference/windows-path-escaping-contexts.md`` for the shape, the
four+ manifestations found in a single day, and the "must this string RUN?"
discriminator between a genuine product bug and a fixture-realism problem.
This module enforces ONLY the mechanically checkable subset of that family
-- narrow, purely syntactic AST checks, each proven both green against the
current tree and red against a planted violation (see the module's own
self-tests, below).

Deliberately NOT enforced: a general "no raw Path interpolated into a bash
payload" check. That requires Path-type inference plus payload-reachability
analysis -- not statically decidable -- and any heuristic strong enough to
catch the real incidents would also flag correct code. See the reference
doc's own "What is enforced here" section for the `bash_payload(...)` seam
that would be needed to make the general case decidable, and why that is a
future chunk's shape, not this one's.

Spec backlink: docs/plans/2026-08-07-guard-suite-back-to-a-gate.md chunk C7,
AC7.

Scope
=====
Only ``coordinator_core/`` PRODUCTION code is walked -- every ``*/tests/``
directory is excluded, matching
``test_re_sub_replacement_template_is_literal_or_callable.py``'s own scope
rationale: test fixtures routinely build throwaway payload strings that are
not part of the operator-facing surface this gate protects.

Leg (i) -- `sys.executable` in an unquoted interpolation
----------------------------------------------------------
`sys.executable` resolves to a filesystem path, and on Windows that path is
frequently something like a drive-letter path under the user profile
directory (space- and
backslash-bearing). Interpolating it directly into an f-string / `%`
/ `.format` / `+` concat WITHOUT `shlex.quote`/`json.dumps`/`repr` builds a
string a shell or a generated-program parser can misinterpret, mirroring
`_bt_python3_invocation`'s own docstring rationale (this repo already gets
this right everywhere in production -- this gate keeps it that way).

Leg (ii) -- the existing `re.sub` replacement-template gate
--------------------------------------------------------------
Cited, not re-implemented: see
``test_re_sub_replacement_template_is_literal_or_callable.py``. That module
already proves BOTH directions for its own shape; duplicating its AST logic
here would only create a second copy to keep in sync. This module's own
test suite below re-imports and calls it, so this file's `pytest` run still
covers leg (ii) directly.

Leg (iii) -- a `tmp_path`/`Path(...)`/`.resolve()`-bound name in an f-string
payload
----------------------------------------------------------------------------
Narrow, by design, but implemented as a deliberately OVER-INCLUSIVE textual
heuristic, not an AST-shape match: the detection (see
`_find_tmp_path_fstring_payload_violations`) is a substring match against
raw source text for `_PATH_BINDING_MARKERS`, not a parsed check that a
binding is actually shaped like `<name> = <call ending in
tmp_path/Path(...)/....resolve()>`. A binding like
`x = f"prefix Path(y) suffix"` (a string literal merely containing the text
`Path(`) also marks `x` as path-bound, because the check greps source text
rather than the parsed call shape. This is the SAFE direction for a gate --
false positives over false negatives -- and is currently verified clean
against the post-C2 tree; if this leg ever produces a false positive against
real code (a path that reaches an always-POSIX consumer via
`posixpath.join`, for instance, is legitimate and MUST NOT be flagged),
this leg should be dropped, not tightened into something that also requires
Path-type inference.
# Review: coordinator:code-reviewer (slice D, P3) -- docstring corrected to
# describe the actual substring-match implementation, not an AST-shape claim
# it didn't make good on. Detection logic unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core.tests.test_re_sub_replacement_template_is_literal_or_callable import (
    find_non_literal_sub_replacements,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

_QUOTING_CALL_NAMES = {"quote", "dumps", "repr"}
_PAYLOAD_NAME_MARKERS = ("cmd", "command", "script", "payload")
_PATH_BINDING_MARKERS = ("tmp_path", ".resolve(", "Path(")


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_excluded_source_path(path: Path) -> bool:
    """Excludes every `*/tests/` directory (matching
    `test_re_sub_replacement_template_is_literal_or_callable.py`'s own
    scope) AND every co-located `test_*.py` module -- this repo's own
    convention places many test modules directly beside their production
    sibling (e.g. `coordinator_core/hooks/test_auto_push.py`) rather than
    under a `tests/` subdirectory, so a directory-only check would silently
    treat those as production code."""
    return "tests" in path.parts or path.name.startswith("test_")


def _enclosing_function(tree: ast.Module, target: ast.AST) -> str:
    best: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    best = node.name
    return best or "<module>"


def _is_sys_executable(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "executable"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _find_sys_executable_violations(root: Path) -> list[tuple[str, int, str]]:
    """Leg (i): every f-string/`%`/`.format`/`+`-concat interpolation of
    `sys.executable` in production code that is NOT wrapped in
    `shlex.quote`/`json.dumps`/`repr`."""
    violations: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded_source_path(path):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            # OSError covers a throwaway probe file that another concurrent
            # process created and removed between the rglob() listing and
            # this read -- not this gate's file to own or wait for.
            continue

        # Every `sys.executable` node that sits DIRECTLY inside a
        # quoting-call's arguments -- these are exempt wherever they occur.
        quoted_ids: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_quoting_call = (
                isinstance(func, ast.Attribute) and func.attr in _QUOTING_CALL_NAMES
            ) or (isinstance(func, ast.Name) and func.id in _QUOTING_CALL_NAMES)
            if not is_quoting_call:
                continue
            for arg in node.args:
                for sub in ast.walk(arg):
                    if _is_sys_executable(sub):
                        quoted_ids.add(id(sub))

        # Every `sys.executable` FormattedValue directly wrapped by literal
        # `"..."` in the SAME f-string (the adjacent Constant segments end
        # / start with a `"`) -- the correct idiom for a Windows `.cmd`
        # launcher line, where `shlex.quote` (POSIX-sh quoting) is the wrong
        # tool for the consumer. See `fake_machine_local.write_fake_executable`.
        literal_dquoted_ids: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for i, value in enumerate(node.values):
                if not isinstance(value, ast.FormattedValue):
                    continue
                before = node.values[i - 1] if i > 0 else None
                after = node.values[i + 1] if i + 1 < len(node.values) else None
                before_ok = (
                    isinstance(before, ast.Constant)
                    and isinstance(before.value, str)
                    and before.value.endswith('"')
                )
                after_ok = (
                    isinstance(after, ast.Constant)
                    and isinstance(after.value, str)
                    and after.value.startswith('"')
                )
                if before_ok and after_ok:
                    for sub in ast.walk(value.value):
                        if _is_sys_executable(sub):
                            literal_dquoted_ids.add(id(sub))

        for node in ast.walk(tree):
            interpolation_sites: list[ast.expr] = []
            if isinstance(node, ast.JoinedStr):
                for value in node.values:
                    if isinstance(value, ast.FormattedValue):
                        interpolation_sites.append(value.value)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
                interpolation_sites.append(node.left)
                interpolation_sites.append(node.right)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
            ):
                interpolation_sites.extend(node.args)
                interpolation_sites.extend(kw.value for kw in node.keywords)
            else:
                continue

            for site in interpolation_sites:
                for sub in ast.walk(site):
                    if not _is_sys_executable(sub):
                        continue
                    if id(sub) in quoted_ids or id(sub) in literal_dquoted_ids:
                        continue
                    relpath = _relpath(path, root)
                    func_name = _enclosing_function(tree, sub)
                    violations.append((relpath, sub.lineno, func_name))
    return violations


def _find_tmp_path_fstring_payload_violations(root: Path) -> list[tuple[str, int, str]]:
    """Leg (iii): a name bound to a `tmp_path`/`Path(...)`/`.resolve()`
    expression, later interpolated directly into an f-string assigned to a
    payload-convention name. Deliberately narrow -- see module docstring."""
    violations: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded_source_path(path):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        source_lines = source.splitlines()

        path_bound_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            try:
                segment = ast.get_source_segment("\n".join(source_lines), node.value) or ""
            except Exception:
                segment = ""
            if any(marker in segment for marker in _PATH_BINDING_MARKERS):
                path_bound_names.add(node.targets[0].id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            target_name = node.targets[0].id
            if not any(marker in target_name.lower() for marker in _PAYLOAD_NAME_MARKERS):
                continue
            if not isinstance(node.value, ast.JoinedStr):
                continue
            for value in node.value.values:
                if not isinstance(value, ast.FormattedValue):
                    continue
                if isinstance(value.value, ast.Name) and value.value.id in path_bound_names:
                    relpath = _relpath(path, root)
                    func_name = _enclosing_function(tree, node)
                    violations.append((relpath, node.lineno, func_name))
    return violations


def test_no_unquoted_sys_executable_interpolation():
    """Leg (i): `sys.executable` never lands in a payload-shaped
    interpolation without `shlex.quote`/`json.dumps`/`repr`."""
    violations = _find_sys_executable_violations(_SCAN_ROOT)
    assert violations == [], (
        "Found sys.executable interpolated into an f-string/%/.format/+ "
        "concat without shlex.quote/json.dumps/repr -- on Windows "
        "sys.executable is frequently a space- and backslash-bearing path "
        f"(illustrative shape, not a real host path; abs-path-ok): {violations}"
    )


def test_re_sub_replacement_template_gate_still_clean():
    """Leg (ii): reuse of the existing, already-proven gate."""
    violations = find_non_literal_sub_replacements(_SCAN_ROOT)
    assert violations == [], violations


def test_no_tmp_path_bound_name_interpolated_into_payload_fstring():
    """Leg (iii): kept only while it stays green against the real tree --
    see module docstring for the drop condition."""
    violations = _find_tmp_path_fstring_payload_violations(_SCAN_ROOT)
    assert violations == [], (
        "Found a tmp_path/Path(...)/.resolve()-bound name interpolated "
        f"directly into a payload-named f-string: {violations}"
    )


def test_leg_i_accepts_literal_double_quote_wrapped_sys_executable(tmp_path):
    """Negative control: a `.cmd` launcher line wraps sys.executable in
    literal double-quotes directly in the f-string -- the correct idiom
    for that consumer (Windows cmd.exe, not a POSIX shell), and must not
    be flagged just because it isn't shlex.quote/json.dumps/repr. See
    `coordinator_core/testing/fake_machine_local.py`."""
    fixture = tmp_path / "fixture_cmd_launcher.py"
    fixture.write_text(
        "import sys\n"
        "\n"
        "def build(py_path):\n"
        '    return f\'@echo off\\r\\n"{sys.executable}" "{py_path}" %*\\r\\n\'\n',
        encoding="utf-8",
    )

    assert _find_sys_executable_violations(tmp_path) == []


def test_leg_i_detects_unquoted_sys_executable_in_fstring(tmp_path):
    fixture = tmp_path / "fixture_unquoted_exec.py"
    fixture.write_text(
        "import sys\n"
        "\n"
        "def build(target):\n"
        "    cmd = f'{sys.executable} {target}'\n"
        "    return cmd\n",
        encoding="utf-8",
    )

    violations = _find_sys_executable_violations(tmp_path)

    assert len(violations) == 1, violations
    relpath, lineno, func_name = violations[0]
    assert relpath == "fixture_unquoted_exec.py"
    assert func_name == "build"


def test_leg_i_accepts_shlex_quoted_sys_executable(tmp_path):
    fixture = tmp_path / "fixture_quoted_exec.py"
    fixture.write_text(
        "import shlex\n"
        "import sys\n"
        "\n"
        "def build(target):\n"
        "    cmd = f'{shlex.quote(sys.executable)} {target}'\n"
        "    return cmd\n",
        encoding="utf-8",
    )

    assert _find_sys_executable_violations(tmp_path) == []


def test_leg_i_accepts_json_dumps_wrapped_sys_executable(tmp_path):
    fixture = tmp_path / "fixture_json_exec.py"
    fixture.write_text(
        "import json\n"
        "import sys\n"
        "\n"
        "def build():\n"
        "    return 'interp=%s' % json.dumps(sys.executable)\n",
        encoding="utf-8",
    )

    assert _find_sys_executable_violations(tmp_path) == []


def test_leg_i_ignores_test_directories(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    fixture = tests_dir / "test_something.py"
    fixture.write_text(
        "import sys\n"
        "\n"
        "def build(target):\n"
        "    return f'{sys.executable} {target}'\n",
        encoding="utf-8",
    )

    assert _find_sys_executable_violations(tmp_path) == []


def test_leg_iii_detects_tmp_path_bound_name_in_payload_fstring(tmp_path):
    fixture = tmp_path / "fixture_tmp_path_payload.py"
    fixture.write_text(
        "def build(tmp_path):\n"
        "    target = tmp_path / 'x'\n"
        "    cmd = f'find {target} -name foo'\n"
        "    return cmd\n",
        encoding="utf-8",
    )

    violations = _find_tmp_path_fstring_payload_violations(tmp_path)

    assert len(violations) == 1, violations
    relpath, lineno, func_name = violations[0]
    assert relpath == "fixture_tmp_path_payload.py"
    assert func_name == "build"


def test_leg_iii_accepts_posixpath_joined_payload(tmp_path):
    """Negative control: a posixpath-joined string is the correct idiom
    (see the reference doc) and must never be flagged -- this leg only
    looks at DIRECT interpolation of a raw path-bound name."""
    fixture = tmp_path / "fixture_posixpath_payload.py"
    fixture.write_text(
        "import posixpath\n"
        "\n"
        "def build(tmp_path):\n"
        "    target = tmp_path / 'x'\n"
        "    joined = posixpath.join(str(target).replace(chr(92), '/'), 'y')\n"
        "    cmd = f'find {joined} -name foo'\n"
        "    return cmd\n",
        encoding="utf-8",
    )

    assert _find_tmp_path_fstring_payload_violations(tmp_path) == []


def test_leg_iii_ignores_test_directories(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    fixture = tests_dir / "test_something.py"
    fixture.write_text(
        "def build(tmp_path):\n"
        "    target = tmp_path / 'x'\n"
        "    cmd = f'find {target} -name foo'\n"
        "    return cmd\n",
        encoding="utf-8",
    )

    assert _find_tmp_path_fstring_payload_violations(tmp_path) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
