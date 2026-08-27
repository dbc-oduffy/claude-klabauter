"""
coordinator_core.benchmarks.hook_entry_cost -- names WHERE the PreToolUse(
Bash) guard-chain entrypoint's cost goes, per stage, before anything in that
chain is touched.

Purpose: `bash_dispatch_probe.measure_derived_floor` already establishes
that the chain costs ~137ms process time per call, individually spawned
(its own WALL column is a job-object batching artifact -- see that module's
docstring -- process time is the sound figure there). That single number is
not enough to size a fix against: some of it is interpreter start a warm
engine removes for free, some is import-closure cost a warm engine also
removes, and some is per-call guard work plus filesystem probes that
SURVIVE warmth -- a warm door cannot help with the second kind. This module
discriminates those, and separately partitions the registered guard chain
into per-call-necessary vs session-invariant-candidate, per this chunk's
dispatch brief (C13, state/dispatch-briefs/2026-08-22-a-bash-call-stops-
costing-a-second-and-a-half/C13.md): "the first question is 'does this need
to run here at all'."

NEGATIVE SPEC: this module fixes nothing. It is an instrument and an audit
source, consumed by `state/audits/2026-08-22-what-the-preToolUse-chain-
actually-costs.md`. It does not change `dispatch.py`, does not retire or
re-order any guard, and does not introduce a session-cache for anything --
that is a downstream chunk's job, sized against what this module reports.

Every figure this module returns states its own invocation shape (bare
python3 -c, engine cold path with lazy ops armed [`_arm_lazy_ops()`], or
warm engine) -- `measure_derived_floor`'s own WALL column is a job-object
batching artifact and must never be quoted as per-call latency; this module
avoids that trap structurally by spawning k=1 per sample (see
`measure_stage_costs`), never batching multiple children into one
`batched_process_time_ms` call.

Spec backlink: state/dispatch-briefs/2026-08-22-a-bash-call-stops-costing-
a-second-and-a-half/C13.md
"""

from __future__ import annotations

import builtins
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.benchmarks.bash_dispatch_probe import (
    CORPUS_PAYLOADS,
    _INERT_PAYLOAD,
    _REPO_ROOT,
    _dispatch_cmd,
    _verify_single_invocation_succeeds,
)
from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)

_DISPATCH_PY_PATH = os.path.join(
    _REPO_ROOT, "coordinator_core", "bash_guards", "dispatch.py"
)

STAGE_LABELS: Tuple[str, ...] = ("bare_interpreter", "import_closure", "chain_spawns_nothing")
"""The three floor components `bash_dispatch_probe.measure_derived_floor`
already names (module docstring there): bare interpreter start, plus the
dispatcher's own import closure, plus the marginal cost of reaching and
returning from `evaluate_payload_json` with the guard chain never entered
(an inert `tool_name` short-circuits before `_build_guard_chain` runs).
Reused here verbatim, not re-derived, so a reader comparing this module's
numbers against that one's is comparing the same three legs."""


def _stage_argv_env(label: str) -> Tuple[list, Dict[str, str]]:
    if label == "bare_interpreter":
        return [sys.executable, "-c", "pass"], dict(os.environ)
    if label == "import_closure":
        return (
            [sys.executable, "-c", "import coordinator_core.bash_guards.dispatch"],
            dict(os.environ),
        )
    if label == "chain_spawns_nothing":
        return _dispatch_cmd(_INERT_PAYLOAD)
    raise ValueError(f"hook_entry_cost: unknown stage label {label!r}")


def _round_half_up_percentile(ordered: List[float], pct: float) -> float:
    """Same round-half-up nearest-rank convention as
    `process_time.batched_process_time_quantiles` (that module's own
    docstring on why plain `round()` -- ties-to-even -- is the wrong
    tie-break here), reimplemented locally because that function only
    quantiles its own `process_time_ms` samples and this module additionally
    needs the same convention applied to `wall_ms`."""
    import math

    if len(ordered) == 1:
        return ordered[0]
    idx = math.floor(pct * (len(ordered) - 1) + 0.5)
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


@dataclass(frozen=True)
class StageCost:
    """One stage's process-time AND wall-time quantiles, `n` samples, EACH
    spawned individually (k=1 per `batched_process_time_ms` call -- never
    multiple children inside one job object/wait loop), per the dispatch
    brief's explicit instruction that the wall column must not be a
    batching artifact the way `measure_derived_floor`'s own k=20 wall
    reading is."""

    label: str
    process_time_p50_ms: float
    process_time_p90_ms: float
    wall_p50_ms: float
    wall_p90_ms: float
    n: int


def measure_stage_costs(n: int = 15) -> List[StageCost]:
    """Individually-spawned (k=1) process-time AND wall-time quantiles for
    each of `STAGE_LABELS`, `n` samples per stage. Raises `NotImplementedError`
    off both Windows and Darwin, mirroring `process_time.py`'s own platform
    split -- callers must check `IS_WINDOWS`/`IS_DARWIN` first.

    The gap between the process-time and wall-time columns is itself part
    of the finding (dispatch brief: "the gap between them is itself the
    finding ... the right instrument for asking why a 44ms process takes
    longer than 44ms to happen") -- callers must not discard the wall
    column as noise.
    """
    if not (IS_WINDOWS or IS_DARWIN):
        raise NotImplementedError(
            "hook_entry_cost.measure_stage_costs has no process-time primitive "
            "for this platform -- see process_time.py's own Windows/Darwin split"
        )

    inert_argv, inert_env = _stage_argv_env("chain_spawns_nothing")
    _verify_single_invocation_succeeds(inert_argv, inert_env)

    out: List[StageCost] = []
    for label in STAGE_LABELS:
        argv, env = _stage_argv_env(label)
        proc_samples: List[float] = []
        wall_samples: List[float] = []
        for _ in range(n):
            result = batched_process_time_ms(argv, k=1, env=env, cwd=_REPO_ROOT)
            proc_samples.append(result["process_time_ms"])
            wall_samples.append(result["wall_ms"])
        proc_sorted = sorted(proc_samples)
        wall_sorted = sorted(wall_samples)
        out.append(
            StageCost(
                label=label,
                process_time_p50_ms=round(_round_half_up_percentile(proc_sorted, 0.50), 3),
                process_time_p90_ms=round(_round_half_up_percentile(proc_sorted, 0.90), 3),
                wall_p50_ms=round(_round_half_up_percentile(wall_sorted, 0.50), 3),
                wall_p90_ms=round(_round_half_up_percentile(wall_sorted, 0.90), 3),
                n=n,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Guard-registration partition: does each registered entry's own closure
# read the per-call command/payload at all, or could its answer in principle
# be evaluated once per session?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardCallVariance:
    """One `GuardEntry` registration's per-call-dependency classification,
    read off `dispatch.py`'s own `_build_guard_chain` source text -- NOT a
    runtime trace. Every registered entry is built as a zero-arg closure
    over `_build_guard_chain`'s own `cmd`/`session_id`/`cwd`/`payload`
    parameters (module docstring there); this classifies which of those a
    given entry's own registration line references.

    `reads_cmd`/`reads_payload`/`reads_session` are TEXTUAL presence checks
    against that one registration line (or, for a small number of entries
    whose closure body is a bare call with no literal arguments -- e.g.
    `destructive-git-revert`'s `_git_revert_full()` -- against the enclosing
    per-call cache helper's own signature, which still closes over `cmd`/
    `session_id` lexically). A registration with all three `False` is
    reported as `candidate_session_invariant=True`. Measured against
    `dispatch.py` as of this module's writing, exactly TWO of 35
    registrations land here: `destructive-git-revert` and `destructive-
    git-revert-advisory`, both bare `lambda: _git_revert_full()` calls with
    no literal argument on their own registration line. Both are FALSE
    POSITIVES of this textual heuristic, not real candidates:
    `_git_revert_full` is a per-dispatch-call closure (`_build_guard_
    chain`'s own `_git_revert_cache`, keyed `(cmd, session_id)`) that still
    reads `cmd` and `session_id` lexically, one indirection away from the
    registration line this classifier reads. So the honest finding is that
    ZERO of the 35 registered guards is a genuine session-invariant
    candidate AT THE REGISTRATION LEVEL -- every one reads something that
    can change between two calls in the same session, directly or (for this
    one pair) one closure away. `inprocess-search` is the one exception in
    spirit, not in this classification -- it still reads `payload`, but its
    OWN check body (not visible to this textual classifier) already carries
    a session latch that answers "already asked" without recomputing the
    search (dispatch brief, citing this guard by name). The real "run less"
    lever this audit finds is therefore NOT at the per-guard registration
    level -- it is either inside a guard's own body (the `inprocess-search`
    pattern) or at the chain-construction level (`_any_declared_matchers`'s
    own process-lifetime cache is the existing precedent there)."""

    name: str
    reads_cmd: bool
    reads_payload: bool
    reads_session: bool
    candidate_session_invariant: bool


_GUARD_ENTRY_SPLIT = "GuardEntry("
_NAME_RE = re.compile(r'"([a-z][a-z0-9-]*)"')
"""Guard ids in this roster are lowercase-hyphenated (`guard_roster.py`'s
own convention, confirmed against every id in this file); anchoring on that
shape excludes the one non-entry match this split produces -- line 1097's
own docstring comment illustrating the constructor signature
(`GuardEntry(name, fn, fail_closed, band, advisory_value, ...`), whose
first quoted-looking token is prose, not a guard id, and would otherwise be
misread as a 49th registration."""
_ENTRY_WINDOW_LINES = 20
"""Lines of source following each `GuardEntry(` split point this module
reads before giving up on locating that entry's own name/arguments -- see
`classify_guard_registration`'s own docstring. The longest observed
registration (`bump-foreign-repo-write`) closes at line 19 of its own
window."""


def classify_guard_registration() -> List[GuardCallVariance]:
    """Parses `dispatch.py`'s own source text for every `GuardEntry(` call
    site and classifies whether its registration line references `cmd`,
    `payload`, or `session_id`.

    Textual, not AST-based, and scoped to a bounded WINDOW of each
    registration's own source (the first `_ENTRY_WINDOW_LINES` lines
    following its `GuardEntry(` split point) rather than the whole file --
    `dispatch.py` registers entries in three shapes: single-line positional
    (`GuardEntry("name", lambda: ..., True, GuardBand.X, ...)`), multi-line
    positional (name on its own line, args one per line), and multi-line
    keyword (`name="...", fn=lambda: ..., ...`). This module locates the
    entry's NAME as the first double-quoted string literal in the window
    (correct for all three shapes -- the name is always the first quoted
    string a registration's own source contains), and checks `cmd`/
    `payload`/`session_id` presence anywhere in that same bounded window.
    Every registration observed at this module's writing closes within
    `_ENTRY_WINDOW_LINES` lines; a future registration wrapped wider than
    that window could evade this classifier (the same bounded-window
    trade-off `_CRASH_TRIGGER_SUBSTRINGS` in `dispatch.py` itself makes for
    a different textual-derivation purpose there).

    Returns one `GuardCallVariance` per `GuardEntry(` site, in file order
    (registration/precedence order, same as the chain itself runs in).
    """
    with open(_DISPATCH_PY_PATH, encoding="utf-8") as f:
        text = f.read()

    chunks = text.split(_GUARD_ENTRY_SPLIT)[1:]
    out: List[GuardCallVariance] = []
    for chunk in chunks:
        window = "\n".join(chunk.split("\n")[:_ENTRY_WINDOW_LINES])
        m = _NAME_RE.search(window)
        if not m:
            continue
        name = m.group(1)
        reads_cmd = bool(re.search(r"\bcmd\b", window))
        reads_payload = bool(re.search(r"\bpayload\b", window))
        reads_session = bool(re.search(r"\bsession_id\b", window))
        out.append(
            GuardCallVariance(
                name=name,
                reads_cmd=reads_cmd,
                reads_payload=reads_payload,
                reads_session=reads_session,
                candidate_session_invariant=not (reads_cmd or reads_payload or reads_session),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Filesystem-probe enumeration: what does the chain do per call that is not
# CPU (dispatch brief, citing probe-spray's own ~8-fs-op audit)?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FsProbeCount:
    """Filesystem-primitive call counts attributed to one guard, over one
    `CORPUS_PAYLOADS` label. `os.stat`/`os.lstat`/`os.path.exists`/
    `os.path.isfile`/`os.path.isdir`/builtin `open` are patched -- the same
    primitive set `probe-spray`'s own audit counted (dispatch brief: "~8
    filesystem operations on every Bash call"), not a reinvented set."""

    guard_name: str
    payload_label: str
    count: int


class _FsProbeRecorder:
    """Patches `os.stat`, `os.lstat`, `os.path.exists`, `os.path.isfile`,
    `os.path.isdir`, and builtin `open` to tally calls against whichever
    guard name is currently set as "active" (see `enumerate_fs_probes`,
    which sets/clears that name immediately around each guard's own `fn()`
    call) -- the identical wrapping shape `bash_dispatch_probe.
    capture_executed_set_baseline` already uses to attribute verdicts to a
    guard by wrapping `dispatch._build_guard_chain`, applied here to
    filesystem primitives instead of verdicts.

    Attribution is BEST-EFFORT: a probe made from a background thread, or
    from code the active guard calls indirectly through a callback that
    clears `active_guard` first, would misattribute or drop -- no guard in
    this roster is known to do either, but this is not verified exhaustively
    here.
    """

    def __init__(self) -> None:
        self.counts: Dict[Tuple[str, str], int] = {}
        self.active_guard: Optional[str] = None
        self._orig_stat = None
        self._orig_lstat = None
        self._orig_exists = None
        self._orig_isfile = None
        self._orig_isdir = None
        self._orig_open = None

    def _bump(self) -> None:
        if self.active_guard is None:
            return
        key = (self.active_guard, self.payload_label)
        self.counts[key] = self.counts.get(key, 0) + 1

    def __enter__(self) -> "_FsProbeRecorder":
        self.payload_label = ""
        self._orig_stat = os.stat
        self._orig_lstat = os.lstat
        self._orig_exists = os.path.exists
        self._orig_isfile = os.path.isfile
        self._orig_isdir = os.path.isdir
        self._orig_open = builtins.open

        def _patched(orig: Callable) -> Callable:
            def _wrapped(*args, **kwargs):
                self._bump()
                return orig(*args, **kwargs)

            return _wrapped

        os.stat = _patched(self._orig_stat)  # type: ignore[assignment]
        os.lstat = _patched(self._orig_lstat)  # type: ignore[assignment]
        os.path.exists = _patched(self._orig_exists)  # type: ignore[assignment]
        os.path.isfile = _patched(self._orig_isfile)  # type: ignore[assignment]
        os.path.isdir = _patched(self._orig_isdir)  # type: ignore[assignment]
        builtins.open = _patched(self._orig_open)  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info) -> None:
        os.stat = self._orig_stat  # type: ignore[assignment]
        os.lstat = self._orig_lstat  # type: ignore[assignment]
        os.path.exists = self._orig_exists  # type: ignore[assignment]
        os.path.isfile = self._orig_isfile  # type: ignore[assignment]
        os.path.isdir = self._orig_isdir  # type: ignore[assignment]
        builtins.open = self._orig_open  # type: ignore[assignment]


def enumerate_fs_probes_for_corpus() -> List[FsProbeCount]:
    """Runs every `CORPUS_PAYLOADS` entry through the REAL
    `evaluate_payload_json` in-process (same reason as `bash_dispatch_probe.
    enumerate_spawn_set`: a probe inside a spawned child would be invisible
    to a patch on THIS interpreter), wrapping `dispatch._build_guard_chain`
    the same way `capture_executed_set_baseline` does so each guard's own
    `fn()` call is bracketed with `active_guard` set to that guard's name,
    and returns one `FsProbeCount` per `(guard, payload_label)` cell with at
    least one attributed call.
    """
    import dataclasses
    import json

    from coordinator_core.bash_guards import dispatch

    recorder = _FsProbeRecorder()
    results: List[FsProbeCount] = []

    with recorder:
        for label, payload in CORPUS_PAYLOADS.items():
            recorder.payload_label = label
            raw = json.dumps(payload)
            orig_build = dispatch._build_guard_chain

            def _wrapped_build(*args, **kwargs):
                chain = orig_build(*args, **kwargs)
                wrapped = []
                for entry in chain:

                    def _make_fn(_entry=entry):
                        def _fn():
                            recorder.active_guard = _entry.name
                            try:
                                return _entry.fn()
                            finally:
                                recorder.active_guard = None

                        return _fn

                    wrapped.append(dataclasses.replace(entry, fn=_make_fn()))
                return wrapped

            dispatch._build_guard_chain = _wrapped_build  # type: ignore[assignment]
            try:
                dispatch.evaluate_payload_json(raw, collect_advisories=True)
            finally:
                dispatch._build_guard_chain = orig_build  # type: ignore[assignment]

    for (guard_name, payload_label), count in sorted(recorder.counts.items()):
        results.append(FsProbeCount(guard_name=guard_name, payload_label=payload_label, count=count))
    return results
