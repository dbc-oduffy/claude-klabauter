"""
coordinator_core.tests.test_no_legacy_touch_record_literal — AC1's
performing guard: no non-test module under ``coordinator_core/`` may carry
the retired-record filename ``"touched.txt"`` as a code ``ast.Constant``
outside a docstring, except at a NAMED, dated, symbol-keyed exemption.

Spec backlink: chunk C10 (this gate), docs/plans/2026-08-25-the-legacy-
touch-record-is-retired-by-repointing-its-writers.md § AC1, § Problem
("The AST scan this plan's Problem section used ... committed as a
guard"). This module IS that committed guard, re-run against the live tree
rather than the plan's one-time measurement.

Why AST, not grep
==================
A substring grep over this corpus returns overwhelmingly comments,
docstrings, and prose (the plan's own § Problem section names this as the
predecessor plan's exact defect: "the predecessor's ``scope:`` was derived
wrong in the first place"). AST-walking for ``ast.Constant`` nodes only
sees values materialized as Python constants; a ``#`` comment or a bare
docstring statement's TEXT is never inspected — only whether the statement
itself IS (or contains) a matching string-constant node. This mirrors
``test_no_bare_chain_terminal_literal.py`` and ``test_no_hardcoded_paths.py``'s
existing AST-gate discipline in this same corpus, not a novel technique.

Allowlist, precisely (see ``_PRODUCTION_EXEMPT_SITES`` below)
===============================================================
Every entry is ``relpath::symbol``, named and dated, never a rationale-fit
match. Re-measured 2026-08-26 against this exact tree, after C0, C1-C4, C5,
C6-C9 landed:

  - ``session/scope.py`` (5 sites, 5 symbols) — the canonical seam module.
    C0 made ``_read_touch_record_as_legacy_lines`` (and its agent-dir
    counterpart, ``_read_agent_touch_record_as_legacy_lines``) a REAL
    union: the jsonl family AND, when a sibling ``touched.txt`` exists,
    its lines prepended. C8 (commit ``95de8f122``) RATIFIED retaining this
    union rather than deleting it, because a reachable writer —
    ``claims.atomic_dedup_append`` via the CLI ``claim-path`` seam — still
    emits the old dialect; the reason is written into the function's own
    docstring, not left in a report. ``_agent_touch_activity``,
    ``release_committed_claims``, and ``compute_scope`` are the module's
    other legacy-dialect-aware call sites, all routed through the same
    union rather than growing their own legacy arm (C0's design; see the
    inline "C0: routed through the real union" comment ahead of
    ``compute_scope``'s Step 3b peer-claim read).
  - ``ipc.py::_record_self_reported_touches`` — examined, not migrated.
    Commit ``1c0fb0a44``'s own message: "``coordinator_core/ipc.py`` is
    examined-and-unchanged: the declared ``writes:`` scope claimed it, the
    executor opened it and found nothing to change, and git agrees it is
    unmodified." The literal here names a LOCK TARGET PATH (shared with
    ``scope.py::touch()``'s own lock naming for re-entrancy safety), not a
    content read of the retired dialect — there is nothing for this AC to
    migrate at this site.
  - ``bash_guards/dispatch_checks.py::_rm_peer_claim_of`` — the plan's
    § Problem names this exact function's role (b), and role (a) was
    independently restored ahead of this plan (commit ``ed031cd43``,
    "restore the peer-claim union on the destructive guard C7b left
    fail-open") on the SAME fact C8 later ratified for ``scope.py``: the
    CLI ``claim-path`` writer still emits the old dialect, so deleting this
    guard's compat union would repeat the exact fail-open the plan's
    § Problem documents. The function's own in-line comment states the
    condition for its removal: "This stays until a chunk migrates that
    writer off the old dialect ... and deletes this union."
  - ``ops/session/migrate_touched_prefix.py::_iter_touched_files``,
    ``ops/session/legacy_touch_corpus_migrate.py`` (module scope),
    ``ops/session/legacy_touch_corpus_drain_check.py`` (module scope) —
    all three are migration/measurement tooling over the legacy corpus BY
    DESIGN (C4's own chunk body: "It must keep working ON the old dialect
    for as long as legacy files exist ... it reads the old dialect BY
    DESIGN"). Their own module docstrings state this purpose; naming the
    literal is the module's job, not a defect.

NOT allowlisted, deliberately
==============================
``session/shape.py::session_shape_magnitude`` still carries the literal
and is NOT exempted here. Commit ``727e1d5ad``'s own message: "``shape.py
:: session_shape_magnitude`` and its test are examined-and-unchanged, not
delivered. C5 reports the divergence rather than claiming the probe. It
stays open." An unresolved gap is not a deliberate retention — allowlisting
it would be exactly the "silent allowlist" this chunk's brief calls a
defect. This gate is therefore expected to be RED on this site until a
future chunk closes it; that red is accurate, not this test's bug.

Negative-spec
=============
  - Does NOT flag docstring/comment prose mentioning ``touched.txt`` (AST
    ``ast.Constant`` docstring-statement position is excluded — see
    ``_is_docstring_statement`` discipline below, identical to
    ``test_no_bare_chain_terminal_literal.py``'s).
  - Does NOT flag any ``test_*.py`` module — AC1 is scoped to non-test
    modules; this plan's own § Problem measurement excludes test files.
  - Does NOT widen an exemption by rationale — each entry above is named,
    dated, and tied to a specific commit; a new site needing the same
    treatment gets its OWN named entry, not a broadened match.
  - Does NOT flag a value merely CONTAINING ``touched.txt`` as a substring
    (e.g. a longer diagnostic sentence) — this is an EXACT ``==`` match.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

_LEGACY_VALUE = "touched.txt"

# Named, dated, symbol-keyed exemptions only — see module docstring
# § Allowlist for the citation behind each entry. "<module>" marks a
# module-scope (not function-body) literal.
_PRODUCTION_EXEMPT_SITES: frozenset[str] = frozenset({
    "coordinator_core/session/scope.py::_read_touch_record_as_legacy_lines",
    "coordinator_core/session/scope.py::_read_agent_touch_record_as_legacy_lines",
    "coordinator_core/session/scope.py::_agent_touch_activity",
    "coordinator_core/session/scope.py::release_committed_claims",
    "coordinator_core/session/scope.py::compute_scope",
    "coordinator_core/ipc.py::_record_self_reported_touches",
    "coordinator_core/bash_guards/dispatch_checks.py::_rm_peer_claim_of",
    "coordinator_core/ops/session/migrate_touched_prefix.py::_iter_touched_files",
    "coordinator_core/ops/session/legacy_touch_corpus_migrate.py::<module>",
    "coordinator_core/ops/session/legacy_touch_corpus_drain_check.py::<module>",
    # This gate itself is a test module and is already excluded by the
    # `_is_test_file` check below; no self-entry is needed the way the
    # WSC-disposition gate needed one, because none of this file's own
    # fixture literals live inside a `_SCAN_ROOT`-rooted non-test module.
})


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_")


class _TouchedTxtLiteralVisitor(ast.NodeVisitor):
    """Single forward pass over one module: flags every ``ast.Constant``
    equal to ``"touched.txt"``, EXCEPT when it occupies a docstring-
    statement position (module/class/function leading string expression
    statement) — the one AST shape where a string constant is textual
    documentation, not a value the runtime compares, joins, or opens."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []  # (lineno, symbol)
        self._fn_stack: list[str] = ["<module>"]
        self._docstring_exempt_nodes: set[int] = set()

    @property
    def _symbol(self) -> str:
        return self._fn_stack[-1]

    def _mark_docstring_exempt(self, body: list[ast.stmt]) -> None:
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            self._docstring_exempt_nodes.add(id(first.value))

    def visit_Module(self, node: ast.Module) -> None:
        self._mark_docstring_exempt(node.body)
        self.generic_visit(node)

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self._mark_docstring_exempt(node.body)  # type: ignore[attr-defined]
        self._fn_stack.append(name)
        self.generic_visit(node)
        self._fn_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_Constant(self, node: ast.Constant) -> None:
        if node.value == _LEGACY_VALUE and id(node) not in self._docstring_exempt_nodes:
            self.violations.append((node.lineno, self._symbol))
        self.generic_visit(node)


def find_legacy_touch_record_literals(
    root: Path, *, repo_root: Path | None = None
) -> list[tuple[str, int, str]]:
    """Walk every ``*.py`` under ``root`` and return every ``(relpath,
    lineno, symbol)`` tuple for an unexempted ``"touched.txt"`` literal in
    a non-test module.

    ``repo_root`` defaults to ``_REPO_ROOT``; a caller may override it (as
    the planted-fixture self-tests do) so relpaths and exemption-set
    matching stay meaningful against an isolated ``tmp_path`` fixture tree.
    """
    effective_root = repo_root if repo_root is not None else _REPO_ROOT
    violations: list[tuple[str, int, str]] = []
    if not root.exists():
        return violations
    for path in sorted(root.rglob("*.py")):
        if _is_test_file(path):
            continue
        relpath = _relpath(path, effective_root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        visitor = _TouchedTxtLiteralVisitor()
        visitor.visit(tree)
        for lineno, symbol in visitor.violations:
            site = f"{relpath}::{symbol}"
            if site in _PRODUCTION_EXEMPT_SITES:
                continue
            violations.append((relpath, lineno, symbol))
    return violations


def test_no_legacy_touch_record_literal_outside_named_exemptions():
    """Standing gate (AC1): outside the named exemption set, no
    ``ast.Constant`` in a non-test module under ``coordinator_core/`` may
    equal ``"touched.txt"``.

    Expected to be RED on ``session/shape.py::session_shape_magnitude``
    until a future chunk closes that named, tracked gap (see module
    docstring § "NOT allowlisted, deliberately") — this test is not
    required to pass at the moment C10 lands; it is required to report the
    true state accurately, which today includes that one open site.
    """
    violations = find_legacy_touch_record_literals(_SCAN_ROOT)
    assert violations == [], (
        "Found legacy touch-record literal(s) ('touched.txt') outside the "
        "named _PRODUCTION_EXEMPT_SITES allowlist — repoint the reader "
        "through the coordinator_core.session.scope union seam, or add a "
        "named, dated, symbol-keyed exemption per this module's own "
        f"discipline if genuinely by design: {violations}"
    )


def test_gate_detects_a_planted_bare_assignment(tmp_path):
    """Teeth: a bare assignment literal is caught."""
    fixture = tmp_path / "fixture_bare_assign.py"
    fixture.write_text(
        "def resolve():\n"
        "    name = \"touched.txt\"\n"
        "    return name\n",
        encoding="utf-8",
    )

    violations = find_legacy_touch_record_literals(tmp_path, repo_root=tmp_path)

    assert len(violations) == 1, violations
    relpath, lineno, symbol = violations[0]
    assert relpath.endswith("fixture_bare_assign.py")
    assert lineno == 2
    assert symbol == "resolve"


def test_gate_detects_a_planted_path_join(tmp_path):
    """Teeth: a filename literal handed to `os.path.join`/`Path.__truediv__`
    (the two shapes seen throughout this corpus) is caught the same way an
    assignment is — the visitor does not special-case the call site."""
    fixture = tmp_path / "fixture_path_join.py"
    fixture.write_text(
        "import os\n\n"
        "def touched_path(sdir):\n"
        "    return os.path.join(sdir, \"touched.txt\")\n",
        encoding="utf-8",
    )

    violations = find_legacy_touch_record_literals(tmp_path, repo_root=tmp_path)

    assert len(violations) == 1, violations
    relpath, lineno, symbol = violations[0]
    assert relpath.endswith("fixture_path_join.py")
    assert lineno == 4
    assert symbol == "touched_path"


def test_gate_ignores_docstring_and_comment_mentions(tmp_path):
    """Negative control: a module docstring, a function docstring, and a
    `#` comment all mentioning the exact retired filename must NOT trip
    the gate — only a code-position ``ast.Constant`` counts."""
    fixture = tmp_path / "fixture_prose_only.py"
    fixture.write_text(
        '"""Module doc: the retired record was named "touched.txt"."""\n'
        "\n"
        "# A comment mentioning touched.txt is not code.\n"
        "def resolve(sdir):\n"
        '    """Docstring: used to read "touched.txt" here."""\n'
        "    return sdir\n",
        encoding="utf-8",
    )

    violations = find_legacy_touch_record_literals(tmp_path, repo_root=tmp_path)

    assert violations == []


def test_gate_ignores_a_class_docstring(tmp_path):
    """Negative control: a class-body leading docstring is exempt the same
    way a module/function docstring is."""
    fixture = tmp_path / "fixture_class_docstring.py"
    fixture.write_text(
        "class Reader:\n"
        '    """Used to read "touched.txt" before the C0 cutover."""\n'
        "    def __init__(self):\n"
        "        self.value = \"\"\n",
        encoding="utf-8",
    )

    violations = find_legacy_touch_record_literals(tmp_path, repo_root=tmp_path)

    assert violations == []


def test_gate_ignores_a_longer_prose_string_containing_the_substring(tmp_path):
    """Negative control: this gate is an EXACT match against the retired
    filename, not a substring test — a longer diagnostic message that
    merely CONTAINS the token must not trip the gate."""
    fixture = tmp_path / "fixture_substring_only.py"
    fixture.write_text(
        "def warn():\n"
        "    return \"legacy record was .git/coordinator-sessions/<sid>/touched.txt.old\"\n",
        encoding="utf-8",
    )

    violations = find_legacy_touch_record_literals(tmp_path, repo_root=tmp_path)

    assert violations == []


def test_gate_ignores_test_modules(tmp_path):
    """Negative control: AC1 is scoped to non-test modules — a `test_*.py`
    fixture carrying the literal must not trip the gate."""
    fixture = tmp_path / "test_fixture_carries_the_literal.py"
    fixture.write_text(
        "def test_something():\n"
        "    assert \"touched.txt\" == \"touched.txt\"\n",
        encoding="utf-8",
    )

    violations = find_legacy_touch_record_literals(tmp_path, repo_root=tmp_path)

    assert violations == []


def test_named_exemption_still_describes_a_real_site():
    """Self-invalidating exemption (mirrors ``test_no_bare_chain_terminal_
    literal.py``'s ``_PRODUCTION_EXEMPT_SITES`` discipline and
    ``test_no_forked_frontmatter_key_regex.py``'s ``_KNOWN_UNFIXED``):
    each entry in ``_PRODUCTION_EXEMPT_SITES`` must still name a symbol
    that actually contains a ``"touched.txt"`` literal at ``HEAD`` — an
    entry that no longer describes a real hit is stale and must be
    deleted, so a future refactor that moves or removes the literal
    re-trips this gate instead of riding a dead exemption forever."""
    stale: list[str] = []
    for site in sorted(_PRODUCTION_EXEMPT_SITES):
        relpath, _, symbol = site.partition("::")
        path = _REPO_ROOT / relpath
        if not path.is_file():
            stale.append(f"{site} (file no longer exists)")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _TouchedTxtLiteralVisitor()
        visitor.visit(tree)
        hits = [sym for _lineno, sym in visitor.violations if sym == symbol]
        if not hits:
            stale.append(f"{site} (no matching literal found)")

    assert stale == [], (
        f"Stale _PRODUCTION_EXEMPT_SITES entry(ies) — delete them: {stale}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
