"""Standing tripwire: only ``coordinator_core.ops.goal_append`` may open a
goals-log JSONL shard (``goals-log.<machine>.jsonl``) for writing/appending.

``block_goals_log_hand_write.py`` (write_guards/) covers agent hand-edits at
the tool seam (Write/Edit/MultiEdit/NotebookEdit). It does NOT cover in-repo
Python that opens the wire directly via ``open()``/``Path.open()``/
``write_text``/``write_bytes`` — a rogue writer of that shape bypasses the
`goal.append` op's `goal_id` content-hash derivation, status-enum
validation, and `coordinator_root_path` normalization exactly as a hand-edit
would, silently corrupting the append-only wire. This test is the second
leg: the actual answer to "which callers are obliged to route through the
writer."

Modeled closely on
``coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py``: uses
``ast``, not regex, for the same reason that gate does — the goals modules
are full of prose mentioning the log filename (this very docstring, for
one), so a docstring/comment MENTION of "goals-log" must never trip the
detector; only a literal open-for-write call reaching a goals-log-shaped
path argument may. ``ast`` structurally excludes docstrings and comments
(neither materializes as a Call node), so the exclusion is structural, not
pattern-based.

Detects:
  - ``open(<goals-log-shaped-arg>, "a"/"w"/"x"/"ab"/"wb"/"xb"/...)`` (and the
    2-positional-arg form where mode is the second positional).
  - ``Path(...).open("a"/"w"/...)`` where the ``Path(...)`` construction
    argument is goals-log-shaped, or the attribute chain's ultimate base
    argument is.
  - ``<goals-log-shaped-expr>.write_text(...)`` /
    ``.write_bytes(...)`` calls (mode-less by definition — any call is a
    write).

A "goals-log-shaped" argument is a literal string constant containing
``goals-log`` and ending in ``.jsonl`` (matches
``goal_append._LOG_NAME_TEMPLATE``'s literal ``"goals-log.{machine}.jsonl"``
and any f-string/format literal built from it) — NOT a bare Name/attribute
reference, since this scanner only proves a literal goals-log path
argument, never a dynamically-constructed one.

Does NOT cover:
  - A dynamically-constructed path (e.g. one assembled via string
    concatenation across multiple statements, or passed in as an opaque
    variable/parameter with no literal goals-log-shaped string anywhere in
    the call) — this scanner proves literal reaching-paths only, the same
    documented tradeoff `test_no_node_schema_shellout.py` accepts for
    the node+oracle-script pair it looks for.
  - Any writer OUTSIDE `coordinator_core/` (e.g. a DoE-side bash oracle, or
    `coordinator/bin/append-goal-event.py` itself, which is the sanctioned
    CLI trampoline into this same op and lives outside the scanned root).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

# Write/append-shaped open() modes. Read-only modes ("r", "rb") are never a
# hazard and are deliberately excluded so a read-side helper reading the log
# (e.g. the P06 goals reader) never trips this gate.
_WRITE_MODES = {"a", "w", "x", "ab", "wb", "xb", "a+", "w+", "x+", "a+b", "w+b", "x+b"}

_WRITE_TEXT_BYTES_METHODS = {"write_text", "write_bytes"}

# Allowlist: exactly one path, the sole authoritative writer. Reasoned,
# dated exemption in the established house style (mirrors
# test_no_node_schema_shellout.py's _ALLOWLISTED_RELPATHS).
#
# 2026-07-31: coordinator_core/ops/goal_append.py is the authoritative
# writer (append_goal()) that derives the goal_id content hash, validates
# the status enum, and normalizes coordinator_root_path before the row
# reaches disk. coordinator_core/ops/goal_close_day.py legitimately
# re-enters through append_goal() (imports and calls it, per its own
# docstring / import line) rather than writing the shard directly, so it
# needs NO exemption here — and if a future edit makes it write directly,
# this gate SHOULD (and will) fire.
_ALLOWLISTED_RELPATHS = {
    "coordinator_core/ops/goal_append.py",
}


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_excluded_source_path(path: Path) -> bool:
    """Test files are out of scope — this gate governs production code
    only (a test fixture hand-appending to a goals-log path to exercise the
    reader is not the corruption hazard this gate exists to catch)."""
    if path.name.startswith("test_"):
        return True
    return "tests" in path.parts


def _is_goals_log_shaped_string(value: str) -> bool:
    return "goals-log" in value and value.endswith(".jsonl")


def _string_constants(node: ast.expr) -> list[str]:
    """Collect literal string constants reachable from a single expression.

    Handles a bare string literal and an f-string (JoinedStr) whose
    constant pieces are concatenated — enough to catch
    ``f"goals-log.{machine}.jsonl"``-shaped construction, which is exactly
    ``goal_append._LOG_NAME_TEMPLATE``'s own literal shape when written as
    an f-string instead of ``str.format``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        pieces = [
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return ["".join(pieces)] if pieces else []
    return []


def _arg_is_goals_log_shaped(node: ast.expr) -> bool:
    for s in _string_constants(node):
        if _is_goals_log_shaped_string(s):
            return True
    return False


def _call_target_is_goals_log_shaped(call_or_expr: ast.expr) -> bool:
    """Walk the expression tree rooted at ``call_or_expr`` for ANY literal
    goals-log-shaped string constant/f-string — covers both a direct
    ``open("goals-log....jsonl", ...)`` and a chained
    ``Path("goals-log....jsonl").open(...)`` / ``(central_root /
    "goals-log....jsonl").write_text(...)`` shape, where the literal lives
    inside a nested Call/BinOp argument rather than the outermost call's
    own arg list.
    """
    for node in ast.walk(call_or_expr):
        if isinstance(node, (ast.Constant, ast.JoinedStr)):
            if _arg_is_goals_log_shaped(node):
                return True
    return False


class GoalsLogWriteVisitor(ast.NodeVisitor):
    """Collects (lineno, kind) for every write/append-mode call reaching a
    literal goals-log-shaped path argument found in one parsed module."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        # open(path, mode) / open(path, mode="a") — plain builtin or
        # Path(...).open(mode) attribute form.
        func_name = None
        if isinstance(func, ast.Name):
            func_name = func.id
        elif isinstance(func, ast.Attribute):
            func_name = func.attr

        if func_name == "open":
            mode = None
            if len(node.args) >= 2:
                mode_strs = _string_constants(node.args[1])
                mode = mode_strs[0] if mode_strs else None
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode_strs = _string_constants(kw.value)
                    if mode_strs:
                        mode = mode_strs[0]
            if mode is None:
                mode = "r"  # open()'s own default is read-only.
            if mode in _WRITE_MODES:
                # Subject is either the first positional arg (bare open())
                # or, for the Path(...).open() attribute form, the object
                # the .open() is called on.
                if isinstance(func, ast.Name):
                    subject = node.args[0] if node.args else None
                else:
                    subject = func.value  # type: ignore[union-attr]
                if subject is not None and _call_target_is_goals_log_shaped(subject):
                    self.violations.append((node.lineno, f"open(mode={mode!r})"))

        elif func_name in _WRITE_TEXT_BYTES_METHODS and isinstance(func, ast.Attribute):
            subject = func.value
            if _call_target_is_goals_log_shaped(subject):
                self.violations.append((node.lineno, func_name))

        self.generic_visit(node)


def find_goals_log_hand_writes(root: Path) -> list[tuple[str, int, str]]:
    """Walk root for .py files (excluding tests/ and test_*.py) and return
    every (relpath, lineno, kind) tuple that is a literal write/append-mode
    call reaching a goals-log-shaped path, skipping the allowlisted writer.

    Used both against the real coordinator_core/ tree (the standing gate)
    and against an isolated tmp_path fixture (the gate's own self-tests).
    """
    violations: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded_source_path(path):
            continue
        relpath = _relpath(path, root)
        if relpath in _ALLOWLISTED_RELPATHS:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        visitor = GoalsLogWriteVisitor()
        visitor.visit(tree)
        for lineno, kind in visitor.violations:
            violations.append((relpath, lineno, kind))
    return violations


def test_no_goals_log_hand_write_in_production_code():
    """Standing gate: coordinator_core/ non-test code must contain zero
    literal write/append-mode opens of a goals-log-shaped path, except the
    single sanctioned writer (goal_append.py)."""
    violations = find_goals_log_hand_writes(_SCAN_ROOT)
    assert violations == [], (
        "Found goals-log hand-write(s) outside the sanctioned writer "
        f"(coordinator_core/ops/goal_append.py): {violations}"
    )


def test_gate_detects_a_planted_goals_log_hand_append(tmp_path):
    """Proves the gate has teeth: a hand-appended write to a goals-log-
    shaped path outside the allowlisted writer must be detected — passing
    by absence is not acceptable."""
    fixture = tmp_path / "fixture_reintroduced_hand_write.py"
    fixture.write_text(
        "def hand_append(central_state_root, machine, row):\n"
        "    with open(\n"
        "        central_state_root / f'goals-log.{machine}.jsonl', 'a', encoding='utf-8'\n"
        "    ) as fh:\n"
        "        fh.write(row)\n",
        encoding="utf-8",
    )

    violations = find_goals_log_hand_writes(tmp_path)

    assert len(violations) == 1
    relpath, lineno, kind = violations[0]
    assert relpath.endswith("fixture_reintroduced_hand_write.py")
    assert lineno == 2
    assert kind == "open(mode='a')"


def test_gate_ignores_docstring_and_comment_mentions_of_the_filename(tmp_path):
    """Negative control: the exact shape of noise this gate must NOT flag —
    a module docstring and a comment mentioning 'goals-log.<machine>.jsonl'
    (as the goals modules legitimately do throughout), plus a read-only
    open() of a goals-log path, which is never a hazard."""
    fixture = tmp_path / "fixture_benign.py"
    fixture.write_text(
        '"""Reads the per-machine goals-log.<machine>.jsonl shard."""\n'
        "\n"
        "# See goals-log.<machine>.jsonl for the row schema.\n"
        "def read_goals_log(path):\n"
        "    with open('goals-log.example.jsonl', 'r', encoding='utf-8') as fh:\n"
        "        return fh.readlines()\n",
        encoding="utf-8",
    )

    violations = find_goals_log_hand_writes(tmp_path)

    assert violations == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
