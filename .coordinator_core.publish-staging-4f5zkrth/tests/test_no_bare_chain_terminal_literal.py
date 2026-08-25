"""
coordinator_core.tests.test_no_bare_chain_terminal_literal — AC1's performing
guard test: no CODE LITERAL of the WSC disposition two-value set
(``"chain-terminal"`` / ``"single-session"``) may appear outside the C1
canonical constants module, ``coordinator_core/ops/ceremony/wsc_disposition.py``.

Spec backlink: chunk C1 (canonical module), chunk C2 (this gate — its owning
task, and its AC1's stated verification), docs/plans/2026-08-01-wsc-
completeness-gate-and-pickup-successor.md.

Why AST, not grep
==================
A substring grep cannot distinguish a code literal (a value the runtime
actually compares/assigns) from PROSE merely containing the same substring
inside a docstring or comment — e.g.
``ops/ceremony/session_instructions.py:132`` glosses the axis in prose using
the exact legacy spelling. AST-walking for ``ast.Constant`` nodes only sees
values that are materialized as Python constants; a ``#`` comment or a bare
docstring statement's TEXT is never inspected, only whether the statement
itself IS (or contains) a matching string constant node — so prose is
structurally invisible to this gate, the same discipline
``test_no_hardcoded_paths.py`` and ``test_no_forked_frontmatter_key_regex.py``
already use.

Scope
=====
Three scan roots, per the dispatch brief: ``coordinator_core/``,
``coordinator/bin/``, ``coordinator/tests/``. An indicative AST measurement
against this exact scan set (2026-08-01, this session — a measurement used to
build the exemption sets below, not the gate's oracle; the gate itself is the
oracle) found exactly 18 files carrying either legacy spelling as a literal
``ast.Constant``:

  - The constants module itself (excluded by module path, see
    ``_CONSTANTS_MODULE`` below) — 2 hits, both are the intentional
    ``SINGLE_SESSION``/``LEGACY_PREDECESSOR_CONSUMED`` definitions.
  - 16 ``test_*.py`` files that legitimately name both spellings as literal
    fixture/assertion values (see ``_TEST_FILE_ALLOWLIST`` below) — the C2
    sweep does NOT touch test files (161 occurrences repo-wide, per the plan's
    own measurement); this gate's named allowlist covers them, not a
    sweep.
  - Exactly ONE non-test production file: ``coordinator/bin/wsc-session-
    disposition.py``. Its own module header states an explicit, pre-existing
    architectural invariant — "Self-contained: no coordinator_core import, no
    cc_invoke/IPC hop" — because this CLI is invoked BOTH in-process (loaded
    by file path from ``workstream_complete/__init__.py``) AND standalone as a
    subprocess (by the DoE skill's bash fence / a bare `python3` invocation)
    in an environment where ``coordinator_core`` may not be on ``sys.path`` at
    all. Repo-wide confirmation: zero files under ``coordinator/bin/`` import
    ``coordinator_core`` (``grep -rl coordinator_core coordinator/bin/*.py`` —
    empty). Importing the C1 constants module here would be the FIRST
    violation of that invariant and risks silently breaking the standalone
    CLI path in a context this gate cannot exercise. See
    ``_PRODUCTION_EXEMPT_SITES`` below for the narrow, symbol-keyed exemption
    (the one remaining bare-legacy-literal site — the ``"single-session"``
    fallback return — lives inside ``resolve_disposition``; the two
    ``"chain-terminal"`` return sites were migrated to the canonical
    ``"predecessor-consumed"`` spelling and no longer need the exemption),
    which is
    self-invalidating the same way ``test_no_hardcoded_paths.py``'s
    ``_EXEMPT_SITES`` is NOT (that one has no re-scan test; this one does, see
    ``test_known_production_exemption_still_describes_a_real_violation``) —
    modelled instead on ``test_no_forked_frontmatter_key_regex.py``'s
    ``_KNOWN_UNFIXED`` self-invalidation discipline, since an exemption that
    silently outlives its cause is worse than no gate. NOTE: the exemption is
    keyed at ``relpath::symbol`` granularity, not per literal-site — the
    re-scan test also asserts an expected per-symbol hit COUNT
    (``_EXPECTED_HIT_COUNT``), so a literal move/rename out of the named
    symbol AND a literal multiplying beyond what a legitimate change
    introduced both fail loudly; only a same-count in-symbol swap of one
    exempted literal for a different one would ride through undetected. The
    count grew from 1 to 3 (2026-08-01, example-market-data-repo WSC_DISPOSITION
    escalate-only override) when ``resolve_disposition`` gained inline
    ``"chain-terminal"``/``"single-session"`` equality checks to recognise
    the env-var override and refuse a downgrade attempt — see that
    function's own docstring for the override's design.

Negative-spec
=============
  - Does NOT flag docstring/comment prose mentioning either spelling (AST-only
    — a bare string-literal EXPRESSION STATEMENT such as a docstring is still
    materialized as an ``ast.Constant`` node by the parser, but this gate
    treats every module-body / function-body / class-body leading string
    exactly like any other statement — see ``_is_docstring_statement`` below,
    which is REQUIRED precisely because a docstring's text ""IS"" an
    ``ast.Constant`` at the AST level; the substring-vs-code-literal
    distinction is therefore NOT "AST sees no docstrings" but "this gate
    explicitly excludes the docstring-statement position").
  - Does NOT flag the constants module's own definitions (``SINGLE_SESSION``,
    ``LEGACY_PREDECESSOR_CONSUMED`` et al) — that module is the single
    permitted home for these literals (AC1).
  - Does NOT widen the production exemption by rationale — the ONE production
    exemption (``wsc-session-disposition.py::resolve_disposition``) is named,
    dated, and self-invalidating; a future site needing the same treatment
    gets its OWN named entry, not a broadened match on "any bin script."
  - Does NOT flag a value merely CONTAINING one of the two spellings as a
    substring (e.g. a longer diagnostic message like ``"...export
    WSC_DISPOSITION=chain-terminal..."``) — this gate is an EXACT ``==``
    match against the legacy tokens, not a substring test; those longer
    strings are prose-shaped diagnostics, not the token itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_ROOTS = (
    _REPO_ROOT / "coordinator_core",
    _REPO_ROOT / "coordinator" / "bin",
    _REPO_ROOT / "coordinator" / "tests",
)

_LEGACY_VALUES = {"chain-terminal", "single-session"}

# The single canonical home for these literals (C1). Excluded by module path,
# not by symbol — every literal in this file is the intentional definition.
_CONSTANTS_MODULE = "coordinator_core/ops/ceremony/wsc_disposition.py"

# Explicit, named test-file allowlist (DEC-4-style discipline: a site is
# exempt because it is NAMED, never because it fits a rationale). Test files
# legitimately assert/fixture both spellings directly — the C2 sweep is
# scoped to CODE literals in non-test files only (see module docstring); this
# allowlist is what the plan's own measurement actually found, enumerated
# rather than guessed at.
_TEST_FILE_ALLOWLIST: frozenset[str] = frozenset({
    "coordinator/bin/tests/test_wsc_session_disposition.py",
    "coordinator/tests/test_complete_entry_rollup.py",
    "coordinator/tests/test_coordinator_complete_entry.py",
    "coordinator_core/ops/ceremony/tests/test_branch_resolution.py",
    "coordinator_core/ops/ceremony/tests/test_pipeline_context.py",
    "coordinator_core/ops/ceremony/tests/test_receipt_emit.py",
    "coordinator_core/ops/ceremony/tests/test_receipt_schema.py",
    "coordinator_core/ops/ceremony/tests/test_resolver_git_provenance.py",
    "coordinator_core/ops/ceremony/tests/test_session_instructions.py",
    "coordinator_core/ops/ceremony/tests/test_wsc_disposition.py",
    "coordinator_core/ops/ceremony/tests/test_wsc_tail_parity.py",
    "coordinator_core/ops/session/tests/test_resolve_chain_terminal_disposition.py",
    "coordinator_core/ops/test_coordinator_complete_entry.py",
    "coordinator_core/workstream_complete/test_apply.py",
    "coordinator_core/workstream_complete/test_workstream_complete.py",
    "coordinator_core/workstream_complete/test_workstream_complete_contract.py",
    # This gate itself: `_LEGACY_VALUES` and every planted-fixture literal
    # below are the detector's OWN target values, not a violation of the
    # invariant they detect.
    "coordinator_core/tests/test_no_bare_chain_terminal_literal.py",
})

# The ONE named, symbol-keyed, non-test production exemption — see module
# docstring § Scope for the full rationale (this CLI's own header states the
# "no coordinator_core import" invariant this gate would otherwise force it
# to break). Keyed ``relpath::symbol`` per DEC-4 (test_no_hardcoded_paths.py's
# ``_EXEMPT_SITES`` shape). Self-invalidated by
# ``test_known_production_exemption_still_describes_a_real_violation`` below —
# an entry that no longer names a real hit fails that test, so this set
# cannot silently outlive its cause.
_PRODUCTION_EXEMPT_SITES: frozenset[str] = frozenset({
    "coordinator/bin/wsc-session-disposition.py::resolve_disposition",
})


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_")


class _DispositionLiteralVisitor(ast.NodeVisitor):
    """Single forward pass over one module: flags every ``ast.Constant``
    equal to a legacy WSC-disposition spelling, EXCEPT when it occupies a
    docstring-statement position (module/class/function leading string
    expression statement) — the one AST shape where a string constant is
    textual documentation, not a value the runtime compares or assigns."""

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
        if node.value in _LEGACY_VALUES and id(node) not in self._docstring_exempt_nodes:
            self.violations.append((node.lineno, self._symbol))
        self.generic_visit(node)


def find_bare_chain_terminal_literals(
    roots: tuple[Path, ...], *, repo_root: Path | None = None
) -> list[tuple[str, int, str]]:
    """Walk every root in ``roots`` and return every ``(relpath, lineno,
    symbol)`` tuple for an unexempted legacy-spelling literal.

    ``repo_root`` defaults to ``_REPO_ROOT``; a caller may override it (as the
    planted-fixture self-tests do) so relpaths and exemption-set matching stay
    meaningful against an isolated ``tmp_path`` fixture tree.
    """
    effective_root = repo_root if repo_root is not None else _REPO_ROOT
    violations: list[tuple[str, int, str]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            relpath = _relpath(path, effective_root)
            if relpath == _CONSTANTS_MODULE:
                continue
            is_test = _is_test_file(path)
            if is_test and relpath in _TEST_FILE_ALLOWLIST:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            visitor = _DispositionLiteralVisitor()
            visitor.visit(tree)
            for lineno, symbol in visitor.violations:
                site = f"{relpath}::{symbol}"
                if site in _PRODUCTION_EXEMPT_SITES:
                    continue
                violations.append((relpath, lineno, symbol))
    return violations


def test_no_bare_chain_terminal_literal_outside_constants_module():
    """Standing gate (AC1): outside ``wsc_disposition.py``, the named test
    allowlist, and the one named production exemption, no ``ast.Constant``
    may equal ``"chain-terminal"`` or ``"single-session"`` anywhere under
    ``coordinator_core/``, ``coordinator/bin/``, or ``coordinator/tests/``.
    """
    violations = find_bare_chain_terminal_literals(_SCAN_ROOTS)
    assert violations == [], (
        "Found bare WSC-disposition legacy-spelling literal(s) outside "
        "wsc_disposition.py — reference SINGLE_SESSION / PREDECESSOR_CONSUMED "
        "/ WRITE_TOKEN, or compare via canonicalize(), instead (or add a "
        "named, dated exemption per DEC-4 discipline if genuinely "
        f"unreachable): {violations}"
    )


def test_gate_detects_a_planted_bare_assignment(tmp_path):
    """Teeth: a bare assignment literal (the Piece-1 sweep's exact target
    shape) is caught."""
    fixture = tmp_path / "fixture_bare_assign.py"
    fixture.write_text(
        "def resolve():\n"
        "    disposition = \"chain-terminal\"\n"
        "    return disposition\n",
        encoding="utf-8",
    )

    violations = find_bare_chain_terminal_literals((tmp_path,), repo_root=tmp_path)

    assert len(violations) == 1, violations
    relpath, lineno, symbol = violations[0]
    assert relpath.endswith("fixture_bare_assign.py")
    assert lineno == 2
    assert symbol == "resolve"


def test_gate_detects_a_planted_equality_comparison(tmp_path):
    """Teeth: a bare equality comparison (the other Piece-1 sweep target
    shape) is caught, distinctly from an assignment."""
    fixture = tmp_path / "fixture_bare_compare.py"
    fixture.write_text(
        "def is_terminal(disposition):\n"
        "    return disposition == \"single-session\"\n",
        encoding="utf-8",
    )

    violations = find_bare_chain_terminal_literals((tmp_path,), repo_root=tmp_path)

    assert len(violations) == 1, violations
    relpath, lineno, symbol = violations[0]
    assert relpath.endswith("fixture_bare_compare.py")
    assert lineno == 2
    assert symbol == "is_terminal"


def test_gate_ignores_docstring_and_comment_mentions(tmp_path):
    """Negative control: a module docstring, a function docstring, and a `#`
    comment all mentioning the exact legacy spelling must NOT trip the gate —
    only a code-position ``ast.Constant`` counts."""
    fixture = tmp_path / "fixture_prose_only.py"
    fixture.write_text(
        '"""Module doc: disposition is "chain-terminal" when a predecessor exists."""\n'
        "\n"
        "# A comment mentioning single-session is not code.\n"
        "def resolve(disposition):\n"
        '    """Docstring: accepts "chain-terminal" or "single-session"."""\n'
        "    return disposition\n",
        encoding="utf-8",
    )

    violations = find_bare_chain_terminal_literals((tmp_path,), repo_root=tmp_path)

    assert violations == []


def test_gate_ignores_a_class_docstring(tmp_path):
    """Negative control: a class-body leading docstring is exempt the same
    way a module/function docstring is."""
    fixture = tmp_path / "fixture_class_docstring.py"
    fixture.write_text(
        "class Gate:\n"
        '    """Resolves to "chain-terminal" or "single-session"."""\n'
        "    def __init__(self):\n"
        "        self.value = \"\"\n",
        encoding="utf-8",
    )

    violations = find_bare_chain_terminal_literals((tmp_path,), repo_root=tmp_path)

    assert violations == []


def test_gate_ignores_a_longer_prose_string_containing_the_substring(tmp_path):
    """Negative control: this gate is an EXACT match against the two legacy
    tokens, not a substring test — a longer diagnostic message that merely
    CONTAINS one of the tokens must not trip the gate."""
    fixture = tmp_path / "fixture_substring_only.py"
    fixture.write_text(
        "def warn():\n"
        "    return \"export WSC_DISPOSITION=chain-terminal before continuing\"\n",
        encoding="utf-8",
    )

    violations = find_bare_chain_terminal_literals((tmp_path,), repo_root=tmp_path)

    assert violations == []


def test_constants_module_itself_is_excluded_by_path():
    """The C1 constants module's own SINGLE_SESSION / LEGACY_PREDECESSOR_
    CONSUMED literal definitions must not trip the gate against itself —
    confirms the module-path exclusion is meaningful, not vacuous."""
    module_path = _REPO_ROOT / _CONSTANTS_MODULE
    assert module_path.is_file()

    # Sanity: the module DOES carry literal ast.Constant hits of both
    # spellings (else this test would prove nothing) —
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    visitor = _DispositionLiteralVisitor()
    visitor.visit(tree)
    assert visitor.violations, "expected wsc_disposition.py to define both legacy literals"

    # But the real gate, scanning the whole tree, must not report it.
    violations = find_bare_chain_terminal_literals(_SCAN_ROOTS)
    assert not any(v[0] == _CONSTANTS_MODULE for v in violations)


def test_known_production_exemption_still_describes_a_real_violation():
    """Self-invalidating exemption (mirrors ``test_no_forked_frontmatter_key_
    regex.py``'s ``_KNOWN_UNFIXED`` discipline): each entry in
    ``_PRODUCTION_EXEMPT_SITES`` must still name a symbol that actually
    contains a legacy-spelling literal, re-scanned with the exemption set
    bypassed — an entry that no longer describes a real hit is stale and must
    be deleted, so a future refactor that moves the literal elsewhere in the
    same file re-trips the gate instead of riding a dead exemption forever."""
    # Count of legacy-spelling literal sites currently found (and exempted)
    # inside the exempted symbol — see the module docstring's § Scope note
    # above for the current count and history. A count MISMATCH means the
    # exemption is silently swallowing a literal beyond what it was written
    # to cover (e.g. a new bare literal added inside the same symbol) — the
    # site-level exemption alone only checks "at least one hit remains,"
    # which cannot catch multiplication within the same symbol; this closes
    # that gap.
    _EXPECTED_HIT_COUNT = {
        # Was 3 (two "chain-terminal" return literals + one "single-session"
        # return literal), dropped to 1 (only the "single-session" fallback
        # return) once the two "chain-terminal" return sites were migrated to
        # the canonical "predecessor-consumed" spelling (no longer a
        # _LEGACY_VALUES member). Back to 3 (2026-08-01) after the
        # WSC_DISPOSITION/WSC_CONSUMED_HANDOFF escalate-only override added
        # two new literal equality checks inside resolve_disposition: a
        # ("predecessor-consumed", "chain-terminal") accept-tuple membership
        # test and a "single-session" downgrade-refusal comparison, alongside
        # the pre-existing "single-session" fallback return.
        "coordinator/bin/wsc-session-disposition.py::resolve_disposition": 3,
    }

    stale: list[str] = []
    for site in _PRODUCTION_EXEMPT_SITES:
        relpath, _, symbol = site.partition("::")
        path = _REPO_ROOT / relpath
        if not path.is_file():
            stale.append(f"{site} (file no longer exists)")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _DispositionLiteralVisitor()
        visitor.visit(tree)
        hits = [sym for _lineno, sym in visitor.violations if sym == symbol]
        if not hits:
            stale.append(f"{site} (no matching violation found)")
            continue
        expected = _EXPECTED_HIT_COUNT.get(site)
        if expected is not None and len(hits) != expected:
            stale.append(
                f"{site} (expected {expected} legacy-literal hit(s), found "
                f"{len(hits)} — exemption is now covering more than it was "
                "written for; re-audit before widening _EXPECTED_HIT_COUNT)"
            )

    assert stale == [], (
        f"Stale _PRODUCTION_EXEMPT_SITES entry(ies) — delete them: {stale}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
