"""coordinator_core.bash_guards.tests.guard_message_register_lint -- the
RENDERED-message register-lint harness (chunk C3a).

Spec backlink: docs/plans/2026-08-12-agent-facing-messages-not-apology.md,
chunk C3's "TEXT SEAM CORRECTION" and "COVERAGE DERIVATION (AC10)" sections,
split into C3a (this module -- the machinery) and C3b (the fast-tier gate
assertions built on top of it, in a later chunk).

WHY RENDERED TEXT, NOT SOURCE LITERALS (staff-eng Finding 10, verified twice
by chunk C5's own dispatch): linting the raw `.py` string literals in
`bash_guards/`, `hooks/`, `write_guards/` is the wrong worklist --

  - It OVER-fires: a module docstring trips B2 even though it is never
    rendered to an agent; an env-key literal passed as an ARGUMENT to
    `_helpers.operator_override_note(env_var, ...)` trips B6 even though the
    composed render never echoes that key back (see `_helpers.py`'s own
    NEGATIVE SPEC 4/5).
  - It UNDER-fires: an f-string-interpolated key built at call time is
    invisible to a whole-source-string scan.

The correct input is `coordinator_core.message_register.lint_message` run
over what a guard/hook/write_guard actually RENDERS at fire time, reached
through the existing capture seam this module drives rather than
re-implements:

  - `guard_message_corpus.fire_row` / `.fire_write_guard_row` /
    `.fire_hook_row` -- the shipped per-row fire functions, already covering
    ~92+ rows across `bash_guards`/`write_guards`/`hooks`.
  - `_message_size.extract_prose_text` -- the promoted (this chunk) public
    alias of the size leg's own `_extract_prose_text`, pulling
    `additionalContext`/`permissionDecisionReason` text out of an envelope's
    `hookSpecificOutput`. Both this module and `_message_size.measure_envelope`
    now share the one extraction, per this chunk's own "reimplement no rule"
    constraint (the same discipline extends to text extraction, not just the
    B1-B6 rules themselves).

Negative spec: this module never edits guard/hook/write_guard message copy
-- it only captures and reports (see the plan's own C5/C6/C9 chunks, whose
job the fix is). It never re-derives `lint_message`'s B1-B6 rule bodies. It
never hand-duplicates a second corpus walk -- every row it fires comes from
`guard_message_corpus`'s own `*_ROWS` lists.

COVERAGE DERIVATION (AC10) -- three separate seams, not one uniform
registry (this chunk's own dispatch stub, mirroring the plan's C3 body):
  - `bash_guards`: `dispatch._build_guard_chain(...)`, a genuine
    registration seam, called with a dummy payload for structural
    introspection (existing tests already do this shape).
  - `write_guards`: `write_guards.engine.discover_guard_names()`, itself a
    thin wrapper over the same `pkgutil.iter_modules([_PKG_DIR])` +
    `importlib.import_module` walk `engine.py` uses at runtime --
    discovery, not registration.
  - `hooks`: this module's own `pkgutil.iter_modules` walk over
    `coordinator_core/hooks/`, filtered the same way `write_guards.engine.
    _discover_guards` filters its own package (skip dunder/private/test
    modules) -- hooks have NO registry and no discovery seam of their own;
    hook wiring lives in a different repo's settings matchers, out of reach
    here. A module present on disk but absent from the row-covered set is
    reported as a GAP in all three legs -- visible and enumerated, per this
    chunk's brief. Turning that gap into a hard failure is C3b's job, not
    this module's.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._message_size import extract_prose_text
from coordinator_core.bash_guards.tests.guard_message_corpus import (
    ADVISORY_REWRITE_ROWS,
    CONFINEMENT_ROWS,
    HOOK_ROWS,
    PLATFORM_CONDITIONED_ROWS,
    WRITE_GUARD_ROWS,
    fire_hook_row,
    fire_row,
    fire_write_guard_row,
)
from coordinator_core.message_register.register import Finding, lint_message
from coordinator_core.write_guards import engine as write_guards_engine

_HOOKS_PKG_DIR = str(Path(__file__).resolve().parent.parent.parent / "hooks")
_HOOKS_PKG_NAME = "coordinator_core.hooks"

#: Modules on disk under `coordinator_core/hooks/` that are never a message
#: emitter, filtered the same way `write_guards.engine._discover_guards`
#: filters its own sibling package: dunder modules, leading-underscore
#: shared helpers (`_payload.py`, `_envelope.py`), and this package's own
#: `test_*.py` files (pytest test modules, not hook entrypoints).
def _is_hooks_emitter_candidate(name: str) -> bool:
    if name in ("__init__", "__main__"):
        return False
    if name.startswith("_"):
        return False
    if name.startswith("test_"):
        return False
    return True


@dataclass(frozen=True)
class RegisterViolation:
    """One `lint_message` finding, attributed back to the corpus cell that
    produced the rendered text it fired on."""

    package: str
    guard: str
    row_id: str
    rule_id: str
    span: Optional[Tuple[int, int]]
    detail: str
    rendered_text: str


@dataclass(frozen=True)
class UnreachedRow:
    """A corpus row this harness could not fire end-to-end (e.g. a
    `WriteGuardRow.unverified_reason` row -- registered per AC2 but not
    fire-asserted, the same "lighter path" the plan sanctions for
    `nudge_unrouted_sizing`)."""

    package: str
    guard: str
    row_id: str
    reason: str


@dataclass(frozen=True)
class PackageCoverage:
    """AC10's per-package coverage picture. `reachable` is every emitter
    name this package's own discovery/registration seam reports; `covered`
    is every emitter name with at least one corpus row; `gap` is
    `reachable - covered`, the set C3b will later turn into a hard
    failure."""

    package: str
    seam: str
    reachable: List[str] = field(default_factory=list)
    covered: List[str] = field(default_factory=list)
    gap: List[str] = field(default_factory=list)
    import_failed: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SweepResult:
    violations: List[RegisterViolation]
    unreached: List[UnreachedRow]
    coverage: List[PackageCoverage]
    cells_fired: int
    cells_with_rendered_text: int


def _bash_guards_reachable_names() -> List[str]:
    """`dispatch._build_guard_chain`, called with a dummy payload for
    structural introspection only -- never `.fn()`'d here. Mirrors
    `guard_message_corpus.test_corpus_imports_cleanly_and_every_row_guard_
    resolves`'s own use of this exact seam."""
    chain = dispatch._build_guard_chain(
        cmd="git status",
        session_id="guard-message-register-lint-coverage",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "git status"}},
        policy_file=None,
        host_is_windows=False,
    )
    return sorted({entry.name for entry in chain})


def _hooks_reachable_names() -> List[str]:
    names: List[str] = []
    for mod_info in pkgutil.iter_modules([_HOOKS_PKG_DIR]):
        if _is_hooks_emitter_candidate(mod_info.name):
            names.append(mod_info.name)
    return sorted(names)


def _lint_capture(
    package: str,
    guard: str,
    row_id: str,
    envelope: Optional[Dict[str, object]],
    violations: List[RegisterViolation],
) -> bool:
    """Extract rendered text from one captured envelope and lint it.
    Returns True if there was any non-empty rendered text to lint (used for
    `cells_with_rendered_text` bookkeeping)."""
    text = extract_prose_text(envelope)
    if not text:
        return False
    findings: List[Finding] = lint_message(text, site=f"{package}:{guard}:{row_id}")
    for finding in findings:
        violations.append(
            RegisterViolation(
                package=package,
                guard=guard,
                row_id=row_id,
                rule_id=finding.rule_id,
                span=finding.span,
                detail=finding.detail,
                rendered_text=text,
            )
        )
    return True


def run_sweep() -> SweepResult:
    """Fire every reachable corpus row across `bash_guards`/`write_guards`/
    `hooks`, lint every rendered text through `lint_message`, and derive
    AC10 per-package coverage. This is the one entrypoint C3b's fast-tier
    gate assertions are meant to sit on top of without restructuring."""
    violations: List[RegisterViolation] = []
    unreached: List[UnreachedRow] = []
    cells_fired = 0
    cells_with_text = 0

    for row in CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS:
        capture = fire_row(row)
        cells_fired += 1
        if _lint_capture("bash_guards", row.guard, row.row_id, capture.envelope, violations):
            cells_with_text += 1

    for row in WRITE_GUARD_ROWS:
        if row.unverified_reason is not None:
            unreached.append(
                UnreachedRow(
                    package="write_guards",
                    guard=row.guard,
                    row_id=row.row_id,
                    reason=row.unverified_reason,
                )
            )
            continue
        capture = fire_write_guard_row(row)
        cells_fired += 1
        if _lint_capture("write_guards", row.guard, row.row_id, capture.envelope, violations):
            cells_with_text += 1

    for row in HOOK_ROWS:
        capture = fire_hook_row(row)
        cells_fired += 1
        if _lint_capture("hooks", row.guard, row.row_id, capture.envelope, violations):
            cells_with_text += 1

    bash_reachable = _bash_guards_reachable_names()
    bash_covered = sorted(
        {row.guard for row in CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS}
    )
    bash_coverage = PackageCoverage(
        package="bash_guards",
        seam="dispatch._build_guard_chain(...) -- registration seam",
        reachable=bash_reachable,
        covered=bash_covered,
        gap=sorted(set(bash_reachable) - set(bash_covered)),
    )

    write_reachable, write_import_failed = write_guards_engine.discover_guard_names()
    write_covered = sorted({row.guard for row in WRITE_GUARD_ROWS})
    write_coverage = PackageCoverage(
        package="write_guards",
        seam=(
            "write_guards.engine.discover_guard_names() -- pkgutil.iter_modules "
            "discovery seam engine.py itself uses at runtime"
        ),
        reachable=sorted(write_reachable),
        covered=write_covered,
        gap=sorted(set(write_reachable) - set(write_covered)),
        import_failed=sorted(write_import_failed),
    )

    hooks_reachable = _hooks_reachable_names()
    hooks_covered = sorted({row.guard for row in HOOK_ROWS})
    hooks_coverage = PackageCoverage(
        package="hooks",
        seam=(
            "pkgutil.iter_modules([hooks package dir]) -- no registry/discovery seam "
            "exists for hooks/; this is a package-walk lower bound, not a registration proof"
        ),
        reachable=hooks_reachable,
        covered=hooks_covered,
        gap=sorted(set(hooks_reachable) - set(hooks_covered)),
    )

    return SweepResult(
        violations=violations,
        unreached=unreached,
        coverage=[bash_coverage, write_coverage, hooks_coverage],
        cells_fired=cells_fired,
        cells_with_rendered_text=cells_with_text,
    )


def render_report_markdown(result: SweepResult) -> str:
    """Render `SweepResult` into the machine-readable-enough markdown shape
    the brief asks for: per-package coverage, per-violation detail, and a
    summary count per rule per package."""
    lines: List[str] = []
    lines.append("# C3a rendered-message lint harness -- violation report")
    lines.append("")
    lines.append(
        "Generated by `coordinator_core/bash_guards/tests/guard_message_register_lint.py`"
        "'s `run_sweep()`, driven off `guard_message_corpus.py`'s `fire_row`/"
        "`fire_write_guard_row`/`fire_hook_row` and `_message_size.extract_prose_text`, "
        "linted via `coordinator_core.message_register.lint_message`."
    )
    lines.append("")
    lines.append(
        f"Cells fired: {result.cells_fired}. Cells with non-empty rendered text: "
        f"{result.cells_with_rendered_text}. Total violations: {len(result.violations)}."
    )
    lines.append("")

    lines.append("## Per-package coverage (AC10)")
    lines.append("")
    for cov in result.coverage:
        lines.append(f"### {cov.package}")
        lines.append("")
        lines.append(f"- Derivation seam: {cov.seam}")
        lines.append(f"- Reachable emitters: {len(cov.reachable)}")
        lines.append(f"- Emitters with at least one corpus row: {len(cov.covered)}")
        lines.append(f"- Gap (reachable, no corpus row): {len(cov.gap)}")
        if cov.gap:
            for name in cov.gap:
                lines.append(f"  - `{name}` -- no corpus row; not exercised by this harness")
        if cov.import_failed:
            lines.append(f"- Import failures during discovery: {cov.import_failed}")
        lines.append("")

    if result.unreached:
        lines.append("## Corpus rows this harness could not fire end-to-end")
        lines.append("")
        for u in result.unreached:
            lines.append(f"- `{u.package}` / `{u.guard}` / `{u.row_id}`: {u.reason}")
        lines.append("")

    lines.append("## Per-violation detail")
    lines.append("")
    if not result.violations:
        lines.append("No violations found across every fired, rendered-text-bearing cell.")
        lines.append("")
    else:
        lines.append("| package | module/guard | site (row_id) | rule | span | rendered text |")
        lines.append("|---|---|---|---|---|---|")
        for v in result.violations:
            span_str = f"{v.span[0]}-{v.span[1]}" if v.span else "-"
            rendered = v.rendered_text.replace("|", "\\|").replace("\n", " / ")
            if len(rendered) > 200:
                rendered = rendered[:200] + "..."
            lines.append(
                f"| {v.package} | {v.guard} | {v.row_id} | {v.rule_id} | {span_str} | {rendered} |"
            )
        lines.append("")

    lines.append("## Summary: violation count per rule per package")
    lines.append("")
    counts: Dict[Tuple[str, str], int] = {}
    for v in result.violations:
        key = (v.package, v.rule_id)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        lines.append("(none)")
    else:
        lines.append("| package | rule | count |")
        lines.append("|---|---|---|")
        for (package, rule_id), count in sorted(counts.items()):
            lines.append(f"| {package} | {rule_id} | {count} |")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test surface (this chunk's own Tier T proof), following
# `guard_message_capture.py`/`guard_message_corpus.py`'s precedent: kept in
# this same module, not auto-discovered by the suite's `python_files =
# ["test_*.py"]` glob (this file is not `test_`-prefixed on purpose, so C3b
# can add its OWN `test_*.py` gate module importing `run_sweep`/`SweepResult`
# from here without a name collision). Run directly via
# `pytest coordinator_core/bash_guards/tests/guard_message_register_lint.py`.
# ---------------------------------------------------------------------------


def test_run_sweep_completes_and_fires_every_row():
    """Smoke proof: `run_sweep()` runs end-to-end, every corpus row that can
    be fired reports a cell, and coverage is derived for all three
    packages."""
    result = run_sweep()
    expected_bash_cells = len(CONFINEMENT_ROWS) + len(ADVISORY_REWRITE_ROWS) + len(
        PLATFORM_CONDITIONED_ROWS
    )
    expected_write_cells = len([r for r in WRITE_GUARD_ROWS if r.unverified_reason is None])
    expected_hook_cells = len(HOOK_ROWS)
    assert result.cells_fired == expected_bash_cells + expected_write_cells + expected_hook_cells
    assert {c.package for c in result.coverage} == {"bash_guards", "write_guards", "hooks"}
    assert result.cells_with_rendered_text > 0


def test_bash_guards_coverage_gap_is_empty_or_named():
    """AC10's bash_guards leg: every guard `_build_guard_chain` reaches
    either has a corpus row (gap is empty) or the gap is at least visible
    here -- this smoke test does not assert zero gap (that hard failure is
    C3b's job), it only asserts the derivation itself does not raise and
    returns a list."""
    result = run_sweep()
    bash_cov = next(c for c in result.coverage if c.package == "bash_guards")
    assert isinstance(bash_cov.gap, list)
    assert set(bash_cov.gap).issubset(set(bash_cov.reachable))


def test_write_guards_coverage_derivation_matches_engine_discovery():
    """AC10's write_guards leg: `discover_guard_names()` (the same seam
    `engine.py` uses at runtime) resolves cleanly with no import failures in
    this tree -- an import failure here would silently shrink the reachable
    set, per `_discover_guards`'s own fail-open-but-report contract."""
    result = run_sweep()
    write_cov = next(c for c in result.coverage if c.package == "write_guards")
    assert write_cov.import_failed == []


def test_hooks_coverage_derivation_finds_the_named_uncovered_modules():
    """AC10's hooks leg: the pkgutil walk over `coordinator_core/hooks/`
    must surface the DR-118-shim-relayed modules `guard_message_corpus.py`'s
    own C10 comment names as uncovered (e.g. `nudge_unauthorized_handoff`,
    `subagent_arrival_check`) -- a hooks gap this harness makes visible, not
    silently absent."""
    result = run_sweep()
    hooks_cov = next(c for c in result.coverage if c.package == "hooks")
    assert "nudge_unauthorized_handoff" in hooks_cov.gap
    assert "subagent_arrival_check" in hooks_cov.gap
    assert set(hooks_cov.covered) == {row.guard for row in HOOK_ROWS}


def test_render_report_markdown_is_non_empty_and_mentions_every_package():
    result = run_sweep()
    report = render_report_markdown(result)
    assert "bash_guards" in report
    assert "write_guards" in report
    assert "hooks" in report
    assert len(report) > 0
