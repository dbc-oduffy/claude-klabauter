"""
coordinator_core.orient_assemble.readers_branch_reconcile — C2c reader port:
local-day/branch-span mismatch assertion + open-handoff auto-reconcile
observation.

Purpose: two independent read-only probes, both ported in-process rather
than shelled out to their source scripts, per
`docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md` § C2c:

1. Span-assert — the read-only comparison half of the fused
   `coordinator/bin/workday-start-day-branch-resolve.py` CLI. That script
   ALSO owns `cmd_reap_log` (a `subprocess.run` call to the co-located
   `reap-sessions.py`, plus a log-file append) — this reader deliberately
   does NOT import or call `cmd_reap_log`/`_run_reap_sessions`; only the
   pure `_span_assert` comparison and its `_current_branch` git read are
   ported into the in-process path.
2. RETIRED: `handoff.reconcile_open` is no longer a registered op (K-026,
   superseded by K-057 -- absent from `ops/_registry_map.py`, and its
   backing module `coordinator_core/ops/handoff_reconcile.py` no longer
   exists). `_read_auto_reconcile` below is a permanent no-op stub, kept
   under its original name only so the reader-family shape and existing
   `collect()`/test monkeypatch seams stay stable; it dispatches nothing
   and always returns an empty `ReaderResult()`. Bug-backlog:
   state/bug-backlog/2026-09-11-orient-assemble-still-probes-the-retired-4775aa35bd49.yaml

A `clear`/`narrow` verdict computed under dry_run=true never reaches
`surfaced[]` (deliberate — see `handoff_reconcile.py`'s `_route_gate_clear`
docstring; routing it through `surfaced[]` would raise a spurious D1
conservation violation on the following run). Left unrendered, that steady
state is indistinguishable from "nothing to do," so `_read_auto_reconcile`
also renders `result.gates_cleared[]` entries where `dry_run` is truthy AND
`blocker_ids` is non-empty — the same discriminator
`coordinator/bin/check-auto-reconcile.py`'s `_render` uses for its own
would-flip line.

Spec backlink: DoE-claude:pln-computed-skills-b2-ceremony-st-e82420, chunk C2c
Spec backlink: cross-repo/inbox/2026-08-13-example-cockpit-repo-em-clear-verdict-invisible-under-dry-run-so-gates-never-announce.md

Negative-spec:
    - Does NOT import or invoke `cmd_reap_log` / `_run_reap_sessions` from
      the source script — that subprocess call to `reap-sessions.py` plus
      log-file append stays OUT of this in-process reader path (this
      chunk's explicit AC).
    - Does NOT pass a `dry_run` override to `check_auto_reconcile.get_response()`
      — the op's own conservative `dry_run=True` default is preserved
      unmodified.
    - Does NOT re-implement `handoff.reconcile_open`'s verdict logic — the
      `surfaced[]` list from its response is translated into
      `judgment_points[]` as-is, one entry per surfaced handoff.
    - Does NOT route a `gates_cleared[]` entry into `surfaced[]` or mutate
      `handoff_reconcile.py` in any way — this module is a read-only
      consumer of an already-computed response; it only renders an
      ADDITIONAL judgment point from a field the op already returns.
    - Does NOT render a `gates_cleared[]` entry whose `dry_run` is falsy or
      whose `blocker_ids` is empty — that shape is a genuine no-op
      (`_route_gate_clear`'s own guard: `if dry_run or not blocker_ids`)
      with nothing to announce.
    - Does NOT wire these results into `brief()` — `__init__.py`'s cadence
      dispatch is shared write-surface across C2a-C2d (the plan's own
      "same package — serial, write-overlap" note); this chunk lands
      alongside concurrently-dispatched sibling reader ports, so wiring
      `collect()` into `brief()` is left to a follow-up integration pass.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

from coordinator_core.orient_assemble.reader_result import ReaderResult

#: The source CLI's absolute path — resolved relative to this file, never a
#: literal device path (portability discipline, AC-16). This file lives at
#: coordinator_core/orient_assemble/, so parents[2] is the claude-klabauter repo root
#: (parents[0]=orient_assemble, parents[1]=coordinator_core, parents[2]=repo
#: root — mirrors `readers_handoff_triage._SOURCE_PATH`'s same parents[2]).
_SOURCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "coordinator"
    / "bin"
    / "workday-start-day-branch-resolve.py"
)

_GIT_TIMEOUT = 10

def _load_source_module():
    """Load the hyphenated-filename source CLI as an importable module (same
    pattern as `readers_handoff_triage._load_source_module`) — a normal
    `import` statement cannot address a `-`-containing filename."""
    spec = importlib.util.spec_from_file_location(
        "workday_start_day_branch_resolve", _SOURCE_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load source module at {_SOURCE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_day_branch_resolve = _load_source_module()
#: The pure comparison function — no side effects, unit-test seam in the
#: source module. Ported as-is, never reimplemented.
_span_assert_compute = _day_branch_resolve._span_assert


def _current_branch(repo_root: str | None = None) -> str:
    """`git branch --show-current` — empty string on detached HEAD or
    failure. In-process read, not a shell-out to the fused CLI; mirrors the
    source module's own `_current_branch` (not reused directly since that
    helper is private to the loaded module and this call is trivial enough
    to keep local rather than reach back into the loaded module's private
    surface a second time).

    `repo_root`, when given, is passed as `cwd=` (DR-382: scan scope is an
    explicit parameter, never re-derived from process cwd inside a reader).
    `None` preserves the prior ambient-cwd behaviour byte-for-byte — this
    reader is not itself the argv entry point, but `collect()`'s own
    `repo_root` keyword was `None` by default before this fix and every
    existing caller (direct `collect()` invocation with no `repo_root`)
    must keep resolving exactly as it did."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            cwd=repo_root,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _read_span_assert(repo_root: str | None = None) -> ReaderResult:
    """Local-day/branch-span mismatch — a directive naming the ceremony's
    own remediation step (re-run `/workday-start` Step 0 or rename inline),
    never a judgment point (the source CLI's own contract: "a TRIPWIRE, not
    a retry — it never renames the branch itself"; the fix is deterministic,
    not an open human branch)."""
    from coordinator_core.daily_branch import format_span_suffix, parse_branch_span
    from coordinator_core.daily_day import local_day
    from coordinator_core.machine_resolver import compute_machine

    branch = _current_branch(repo_root)
    today = local_day()
    msg = _span_assert_compute(
        branch, today, parse_branch_span, format_span_suffix, compute_machine
    )
    if msg is None:
        return ReaderResult()
    return ReaderResult(
        directives=[
            {
                "id": "d-branch-span-mismatch",
                "cli": "workday-start-day-branch-resolve",
                "args": ["span-assert"],
                "depends_on": None,
                "already_satisfied": False,
                "detail": msg,
            }
        ]
    )


def _read_auto_reconcile(repo_root: str | None = None) -> ReaderResult:
    """RETIRED no-op stub -- `handoff.reconcile_open` is no longer a
    registered op (K-026, superseded by K-057) and this reader must never
    dispatch it. Always returns an empty `ReaderResult()` without touching
    `coordinator_core.ops.check_auto_reconcile` at all. Kept under its
    original name/signature (including the now-unused `repo_root`
    parameter) purely so `collect()` and every existing test's
    `monkeypatch.setattr(rbr, "_read_auto_reconcile", ...)` seam keep
    working unchanged.

    Bug-backlog: state/bug-backlog/2026-09-11-orient-assemble-still-probes-
    the-retired-4775aa35bd49.yaml
    """
    return ReaderResult()


def collect(cadence: str, *, repo_root: str | None = None) -> ReaderResult:
    """Compute this reader family's directives/judgment_points.

    `cadence` is accepted for signature parity with sibling reader families
    (`readers_clean_ops.collect`) but unused here — neither probe's
    severity varies by cadence.

    `repo_root` is keyword-only and threaded through to `_read_span_assert`
    (C12 of the orient-assemble repo-scope plan, DR-382) via its own
    `repo_root`-accepting `_current_branch` internal. `_read_auto_reconcile`
    also accepts `repo_root` for signature parity but is a permanent no-op
    (see its own docstring) and ignores it. `None` — the default, and every
    existing caller's prior behaviour — preserves the prior ambient-cwd
    resolution byte-for-byte at the `_read_span_assert` leaf. Still unwired
    into `__init__.py`'s cadence dispatch (`brief()`) — that remains shared
    write-surface across C2a-C2d and this module's own negative-spec already
    excludes wiring `collect()`'s results into `brief()` from this chunk;
    this only closes the gap between `collect()`'s own `repo_root` parameter
    and the leaves it calls.
    """
    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for result in (
        _read_span_assert(repo_root),
        _read_auto_reconcile(repo_root),
    ):
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    return ReaderResult(directives=directives, judgment_points=judgment_points)
