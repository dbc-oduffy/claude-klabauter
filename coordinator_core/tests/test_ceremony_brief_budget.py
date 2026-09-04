"""
coordinator_core.tests.test_ceremony_brief_budget

Purpose: C14 (docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md)
— a regrowth ratchet on TWO axes (process time, real file-open count) for
the three ceremony assemblers this plan rebuilt: `pickup-assemble brief`,
`baton-assemble brief`, and `workstream-complete-assemble brief`. The
open-count axis is the one that would have caught every defect this plan
fixed (the corpus walks named in the plan's Problem section) — a future
regression on any of the three trips THIS test, not a spike eighteen months
later.

AC4 of that plan requires the open-count axis be counted "via
`_winapi`/`os.open`-level interception, NOT `builtins.open`" — this file's
first landing used a `builtins.open` patch and undercounted every op by
~3x (a `pathlib`-only corpus walk is invisible to it end to end; see the
measurement primitive's own docstring below for why, and why a bare
`os.open` patch turns out to be insufficient too — the actual fix is a
`sys.addaudithook` 'open' listener, which is the one interception point all
three Python-level open call shapes route through in C).

EXTENDS the census plan's C5 line-count mechanism
(`coordinator_core/op_census/line_count.py`) — same shape, not a parallel
invention: a frozen high-water per axis per op, measured now (post-fix,
after C2/C3/C8-C11/C13 landed), and a REFUSAL (never a silent pass or a
degrade) when a later measurement exceeds it. This module does not import
`line_count.py` — its `LineCountDistribution`/`RatchetOutcome` dataclasses
are typed for the line-count axis specifically (module_count/total_lines/
over_bar_count) and do not fit a per-op, two-axis shape — but it reuses the
identical POLICY: shrink-or-hold is free, growth past the frozen figure is
a hard refusal, and bumping the frozen constant is a deliberate, reasoned
code edit, never an automatic or silent one.

Process time is measured via `time.process_time()` deltas around each op's
in-process `brief()` call — per CLAUDE.md's own brightline doctrine ("process
time and spawn count, never wall clock"; a measure a concurrent session can
move is the same bug under a new name) — not the Windows job-object harness
(`coordinator_core/benchmarks/process_time.py :: batched_process_time_ms`),
which measures a *subprocess tree*'s CPU time and is the right instrument for
a CLI-boundary measurement, not an in-process library call with no child
process of its own. Windows' ~15.6ms process-time quantisation (documented in
that module) means a measurement near the tick is noisy; each op's high-water
carries visible headroom above its single-sample measurement for exactly
that reason, not because the ratchet has gone soft.

Precedent for shape: `coordinator_core/tests/test_composition_budget.py`
(a hand-advanced fake clock proving elapsed/count ceilings independently).
`coordinator_core/pickup_assemble/tests/test_brief_open_budget.py` still
uses a `builtins.open` patch (its own separate AC4 pin, out of this
chunk's writes scope) — this file does NOT reuse that idiom, precisely
because it undercounts; see the measurement primitive's docstring below.

Negative-spec — do NOT "fix" while reading this module:
    - This module does NOT assert on wall-clock time anywhere, including
      the `_measure_brief` helper itself — only `time.process_time()`.
    - This module does NOT re-derive `_BUDGETED_ENTRYPOINTS`-style
      enrolment or exemptions — it gates exactly the three ops this plan
      names, no more, no fewer. A fourth ceremony assembler earning this
      same regrowth-ratchet treatment is a future chunk's decision, not an
      automatic consequence of this file existing.
    - This module does NOT touch `coordinator_core/op_census/line_count.py`
      — C14's own writes scope is this file alone; growing that module is
      out of scope here.

Spec backlink: state/dispatch-briefs/2026-08-21-rebuild-the-three-ceremony-assemblers/C14.md
"""

from __future__ import annotations

import builtins
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.pickup_assemble as pa
import coordinator_core.workstream_complete as wsc

# Real-git spawn in the pickup fixture (git init/add/commit) — `pa.brief()`'s
# classifiers read actual git-tracked repo state, same idiom and same
# declaration as `pickup_assemble/tests/test_brief_open_budget.py`. Neither
# the baton nor the workstream-complete fixture below spawns a process:
# both call `brief(..., repo_root=tmp_path)` with an explicit repo root,
# which bypasses git discovery on the paths this test exercises (mirrors
# `test_baton_assemble.py :: TestDecisionObjectKeyShapes` and
# `test_workstream_complete.py`'s own bare-`tmp_path` fixtures).
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


# ---------------------------------------------------------------------------
# Shared measurement primitive — process time + a real open COUNT, never
# wall clock (module docstring; DR-344's own brightline doctrine).
#
# The open-count half of this primitive is a `sys.addaudithook` 'open'
# listener, NOT a `builtins.open` monkeypatch. This is a correction to this
# file's own first landing: `builtins.open = _counting_open` counts calls
# made through the `builtins.open` NAME specifically, but `pathlib.Path`'s
# `open`/`read_text`/`read_bytes` call `io.open` — a reference `io` bound to
# the same underlying function at interpreter start, but bound to `io`'s OWN
# attribute slot, which rebinding `builtins.open` never touches. Measured
# directly (see this file's own dispatch correction): a `builtins.open`
# patch counts ZERO for `pathlib.Path.read_text()`; a real corpus-walking
# op's `pathlib`-only reads are invisible to it end to end. This is the
# exact defect DR-344's AC4 names: a `builtins.open` probe counted 3,067
# opens for `pickup-assemble brief` pre-fix where an audit-hook census
# counted 9,818 — the missing 6,751 were `claim_index`'s agent-dir walk,
# reached entirely through `pathlib`.
#
# `sys.addaudithook`'s 'open' event (PEP 578) fires for `os.open`,
# `io.open`/`builtins.open`, AND every `pathlib.Path` read/open method,
# because CPython raises this event from the C-level file-open primitive
# itself, below all three Python-level entry points — there is no fourth
# entry point left to miss. Confirmed empirically (see
# `test_open_audit_primitive_catches_what_builtins_open_patch_misses`
# below): a bare `os.open` monkeypatch ALSO measures zero for
# `Path.read_text()` on this interpreter (CPython's `_io.FileIO` calls the
# platform open syscall directly in C, never Python's `os.open` function),
# so `os.open`-patching is not actually the fix either — only the audit-hook
# event is a true C-level interception point that every one of the three
# Python-level call shapes routes through.
#
# `sys.addaudithook` cannot be un-installed once added (deliberate CPython
# API design — a hook that could remove itself couldn't be trusted for
# security auditing). This module therefore installs exactly ONE hook, once,
# for the whole test process, and gates counting through a thread-local
# mutable cell (`_open_audit_active`) that is armed only for the duration of
# one `fn()` call and disarmed (never removed) immediately after — the hook
# itself is a permanent no-op between measurements, not a per-call cost.
# ---------------------------------------------------------------------------

_open_audit_lock = threading.Lock()
_open_audit_installed = False
_open_audit_active = threading.local()


def _install_open_audit_hook_once() -> None:
    """Installs the module's single `sys.addaudithook` 'open' listener,
    idempotently — safe to call from every `_measure_brief` invocation.
    Guarded by `_open_audit_lock` rather than a bare module-level flag check
    because pytest can run this module's tests under `pytest-xdist` worker
    threads sharing one interpreter; the lock keeps install-once genuinely
    once, not merely once-per-race-winner."""
    global _open_audit_installed
    with _open_audit_lock:
        if _open_audit_installed:
            return

        def _on_open_event(event: str, args: tuple) -> None:
            if event != "open":
                return
            counter = getattr(_open_audit_active, "counter", None)
            if counter is not None:
                counter[0] += 1

        sys.addaudithook(_on_open_event)
        _open_audit_installed = True


#: Repeats per op inside `_measure_brief` — a single `time.process_time()`
#: sample on this box swings from ~15ms to 400ms+ run to run (measured
#: 2026-08-21 while sizing this file's own frozen constants: pickup 0-16ms,
#: baton 0-312ms, workstream-complete 15-1062ms, ALL against the identical
#: fixture and identical code path). That is not this box being "busy" in
#: the sense CLAUDE.md's brightline warns against wall-clock gates for —
#: process time is per-process CPU-seconds, immune to a concurrent peer's
#: wall-clock — but it is genuinely noisy at this box's load in absolute
#: terms (GC pauses and scheduler jitter both count toward CPU time). A
#: single-sample gate near any of these ops' true cost would be exactly the
#: "measures tick noise, not cost" trap `benchmarks/process_time.py`'s own
#: docstring names for the job-object primitive; this module has no
#: subprocess tree to amortise via that primitive, so it amortises the same
#: way in-process — repeat and take the median, never the first sample.
_MEASURE_REPEATS: int = 15


def _measure_brief(fn: Callable[[], Any]) -> tuple[Any, int, float]:
    """Runs `fn` `_MEASURE_REPEATS` times, counting every real file open
    (`os.open`, `io.open`/`builtins.open`, and every `pathlib.Path`
    read/open method alike — see the audit-hook block above) on the FIRST
    call only (deterministic for a fixed, read-only fixture — none of the
    three ops mutate their own input) and taking the MEDIAN
    `time.process_time()` elapsed across all calls (see `_MEASURE_REPEATS`'s
    docstring for why median-of-repeats, not a single sample). Returns
    `(last_result, open_count, median_process_time_ms)`. The counter is
    armed only around the first call — mirrors
    `test_brief_open_budget.py`'s own idiom, generalised to any zero-arg
    callable so all three ops share one measurement path."""
    _install_open_audit_hook_once()
    counter = [0]
    _open_audit_active.counter = counter
    try:
        result = fn()
    finally:
        _open_audit_active.counter = None
    open_count = counter[0]

    samples = []
    for _ in range(_MEASURE_REPEATS - 1):
        t0 = time.process_time()
        result = fn()
        samples.append((time.process_time() - t0) * 1000.0)
    samples.sort()
    median_process_time_ms = samples[len(samples) // 2]
    return result, open_count, median_process_time_ms


def test_open_audit_primitive_catches_what_builtins_open_patch_misses(tmp_path):
    """Self-test of the counting instrument itself, not of any ceremony op —
    this file's whole value rests on this primitive being trustworthy, so
    the primitive gets its own regression guard. Proves TWO things against
    an identical `Path.read_text()` call: (1) a `builtins.open` patch (the
    instrument this file's first landing used, and the one AC4 explicitly
    forbids) measures ZERO opens, because `pathlib.Path.read_text` calls
    `io.open` — a name bound to `io`'s own attribute slot at interpreter
    start, never touched by reassigning `builtins.open` — never the
    `builtins.open` name itself; and (2) this module's `sys.addaudithook`
    'open'-event primitive measures ONE, because CPython raises that event
    from the C-level file-open primitive every one of `os.open`,
    `io.open`/`builtins.open`, and `pathlib.Path`'s own read/open methods
    bottoms out in — there is no fourth entry point for a corpus walk to
    hide behind. If this test ever fails, do NOT weaken the assertions —
    it means either CPython's `pathlib` internals changed shape (investigate
    before touching this file) or someone "simplified" `_measure_brief` back
    toward a `builtins.open` patch (revert that, don't loosen this test)."""
    probe = tmp_path / "probe.txt"
    probe.write_text("payload", encoding="utf-8")

    real_builtins_open = builtins.open
    builtins_open_count = 0

    def _counting_builtins_open(*args, **kwargs):
        nonlocal builtins_open_count
        builtins_open_count += 1
        return real_builtins_open(*args, **kwargs)

    builtins.open = _counting_builtins_open
    try:
        probe.read_text(encoding="utf-8")
    finally:
        builtins.open = real_builtins_open

    assert builtins_open_count == 0, (
        "expected the builtins.open patch to miss pathlib.Path.read_text() "
        "entirely (the exact undercount this file's instrument replaces) — "
        f"got {builtins_open_count}, meaning CPython's pathlib internals "
        "changed shape; investigate before trusting this file's ratchet"
    )

    _install_open_audit_hook_once()
    counter = [0]
    _open_audit_active.counter = counter
    try:
        probe.read_text(encoding="utf-8")
    finally:
        _open_audit_active.counter = None

    assert counter[0] == 1, (
        f"expected the sys.addaudithook 'open' primitive to catch exactly "
        f"one open for a single Path.read_text() call, got {counter[0]!r}"
    )


class RatchetError(Exception):
    """Raised when a ceremony assembler's measured process time or open
    count has grown past its frozen high-water — a REFUSAL, never a silent
    pass or a degrade (mirrors `op_census.line_count.RatchetError`'s own
    contract). Bumping the frozen constant below is a deliberate, reasoned
    edit to this module, not an automatic consequence of a failing run."""


def _ratchet_check(op_name: str, axis: str, measured: float, frozen: float) -> None:
    if measured <= frozen:
        return
    raise RatchetError(
        f"Refused: {op_name}'s {axis} {measured!r} exceeds its frozen "
        f"high-water {frozen!r}. Bump the FROZEN_HIGH_WATER_* constant for "
        f"{op_name} in coordinator_core/tests/test_ceremony_brief_budget.py "
        "deliberately, with a stated reason, if this growth is intended -- "
        "this ratchet does not degrade or skip."
    )


# ---------------------------------------------------------------------------
# Frozen high-waters — RE-measured 2026-08-21 against the `sys.addaudithook`
# instrument above (replacing this file's first landing, which measured the
# open-count axis with a `builtins.open` patch and reported pickup=2,
# baton=4, wsc=0 — all three UNDER-counted, per this module's own docstring).
# process time unaffected by the instrument swap (same code path, same
# `time.process_time()` measurement); those constants are carried over
# unchanged. Six repeated live runs of the full file, post-fix (after
# C2/C3/C8-C11/C13 landed on this branch), against the fixtures below, one
# `_MEASURE_REPEATS`-median `brief()` call per run:
#
#   pickup-assemble brief:      open_count=11 (stable across 6 runs),        median process time 0ms
#   baton-assemble brief:       open_count=16-20 (min-max across 6 runs),    median process time 31-47ms
#   workstream-complete brief:  open_count=18-22 (min-max across 6 runs),    median process time 31-78ms
#
# Each frozen open-count constant below is ~2x the observed maximum across
# those six runs — enough headroom to absorb the swing actually observed
# (a 4-6-count spread run to run, this box's normal concurrent-session
# jitter touching genuinely-open-adjacent code paths, e.g. handoff-index
# lookups) without absorbing a REINTRODUCED corpus walk: the defect class
# this ratchet exists to catch moved these ops into the THOUSANDS pre-fix
# (pickup 3,067 opens under the old, under-counting instrument; baton
# 941.4ms; workstream-complete 5,008 opens under the old instrument) — an
# order of magnitude past any of the headroom figures below, so a real
# regrowth still trips this ratchet immediately. Process-time constants are
# untouched from this file's first landing — carried at 1/4 to 1/2 of the
# DR-344 500ms brightline, still comfortably above every measured median
# above, and not part of this correction (only the open-count axis's
# INSTRUMENT changed; the process-time axis and its own headroom reasoning
# were never wrong).
# ---------------------------------------------------------------------------

FROZEN_HIGH_WATER_PICKUP_OPEN_COUNT: int = 30
FROZEN_HIGH_WATER_PICKUP_PROCESS_TIME_MS: float = 200.0

FROZEN_HIGH_WATER_BATON_OPEN_COUNT: int = 40
FROZEN_HIGH_WATER_BATON_PROCESS_TIME_MS: float = 250.0

#: WSC brief loads the frontmatter schema corpus exactly once per call
#: (measured: one `load_schemas()` invocation, opening every file in
#: `coordinator_core/frontmatter/schemas/`). That is one legitimate read, not
#: redundancy -- there is nothing to delete at the call site. But it means the
#: raw open count is `brief's own opens + len(schema corpus)`, and the corpus
#: grows whenever anyone adds a record type. Frozen as a bare integer, this
#: gate reddened at 56/45 with NOTHING in the close path having regressed:
#: the schemas grew. A high-water over a quantity someone else's unrelated
#: work increments is not a ratchet on this brief, so the corpus size is
#: subtracted out and the frozen number below covers only what the brief
#: itself opens.
#:
#: Deliberately NOT a bump of the old constant. The refusal text invites a
#: bump "with a stated reason"; "the number went up and I do not know why" is
#: the absence of a reason, and raising it would have silenced a live signal.
#: The reason is stated here because it was measured: 40 of the 56 opens were
#: the corpus, 1:1 with the 40 files on disk.
def _schema_corpus_open_allowance() -> int:
    """Files `load_schemas()` opens for the frontmatter schema corpus."""
    schemas_dir = (
        Path(__file__).resolve().parents[1] / "frontmatter" / "schemas"
    )
    # EXACTLY the two globs `load_schemas()` reads (`*.yaml` and
    # `*.schema.json`), never a bare `iterdir()`: any other file in that
    # directory would inflate the allowance and quietly make this gate more
    # permissive than it reads.
    try:
        return sum(
            1
            for f in schemas_dir.iterdir()
            if f.is_file() and (f.name.endswith(".yaml") or f.name.endswith(".schema.json"))
        )
    except OSError:
        return 0


FROZEN_HIGH_WATER_WSC_OWN_OPEN_COUNT: int = 20
FROZEN_HIGH_WATER_WSC_PROCESS_TIME_MS: float = 600.0


# ---------------------------------------------------------------------------
# pickup-assemble brief
# ---------------------------------------------------------------------------


from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


def _seed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def test_pickup_brief_stays_under_frozen_high_water(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")

    result, open_count, process_time_ms = _measure_brief(
        lambda: pa.brief("state/handoffs/h1.md", repo_root=repo)
    )

    assert result.exit_code == pa.EXIT_OK
    _ratchet_check(
        "pickup-assemble brief", "open count", open_count, FROZEN_HIGH_WATER_PICKUP_OPEN_COUNT
    )
    _ratchet_check(
        "pickup-assemble brief",
        "process time (ms)",
        process_time_ms,
        FROZEN_HIGH_WATER_PICKUP_PROCESS_TIME_MS,
    )


# ---------------------------------------------------------------------------
# baton-assemble brief
# ---------------------------------------------------------------------------


def _write_artifact(path: Path, fm_lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = "".join(f"{line}\n" for line in fm_lines)
    path.write_text(f"---\n{fm}---\n\n# Artifact\n\nBody.\n", encoding="utf-8")
    return path


def test_baton_brief_stays_under_frozen_high_water(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ba,
        "resolve_operator_config",
        lambda: {
            "settings_home": "/fake/settings-home",
            "claude_klabauter_bin": "/fake/settings-home/bin",
            "doe_root": "/fake/doe-root",
        },
    )
    artifact = _write_artifact(
        tmp_path / "state" / "handoffs" / "h1.md",
        ["deliverable_id: DEL-C14-1", 'predecessor: "none"'],
    )

    result, open_count, process_time_ms = _measure_brief(
        lambda: ba.brief("handoff", str(artifact), repo_root=tmp_path)
    )

    assert result.decision_object
    _ratchet_check(
        "baton-assemble brief", "open count", open_count, FROZEN_HIGH_WATER_BATON_OPEN_COUNT
    )
    _ratchet_check(
        "baton-assemble brief",
        "process time (ms)",
        process_time_ms,
        FROZEN_HIGH_WATER_BATON_PROCESS_TIME_MS,
    )


# ---------------------------------------------------------------------------
# workstream-complete-assemble brief
# ---------------------------------------------------------------------------


def test_wsc_brief_stays_under_frozen_high_water(tmp_path, monkeypatch):
    gate = wsc.SessionShapeGate(
        sid="testsid123",
        disposition="single-session",
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: gate)

    result, open_count, process_time_ms = _measure_brief(
        lambda: wsc.brief(decisions={}, repo_root=tmp_path)
    )

    assert result
    own_open_count = open_count - _schema_corpus_open_allowance()
    _ratchet_check(
        "workstream-complete brief",
        "own open count (total minus the frontmatter schema corpus)",
        own_open_count,
        FROZEN_HIGH_WATER_WSC_OWN_OPEN_COUNT,
    )
    _ratchet_check(
        "workstream-complete brief",
        "process time (ms)",
        process_time_ms,
        FROZEN_HIGH_WATER_WSC_PROCESS_TIME_MS,
    )
