"""coordinator_core.ops.op_census_report — "op_census.report" JSON-RPC op.

Purpose: C6 (`docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md` §C6,
`state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C6.md`) —
THE assembly point. Registers `op_census.report`, which composes C1's cached
per-module summary, C2's spawn evidence, C4's timing dispositions, and C5's
line-count ratchet into ONE machine-readable, per-op, four-axis disposition
(spawns / handler_elapsed / invocation_tax / line_count, each `over_bar` /
`under_bar` / `no_data`) — the artifact P2 consumes, never prose. It carries
the corpus root and module count it was computed over (Finding 5, stays OPEN
— see negative-spec below) and asserts its own process-time budget.

`census()` is the pure, IPC-free assembly function; `_op_census_report` is the
thin `register_op("op_census.report")` wrapper around it, matching every
other op module's self-registration shape (`ops/ping.py`).

§ Additive budget table (staff-eng Finding 3), re-verified 2026-08-21 against
this repo's real, NON-TEST `coordinator_core/` tree — the same scope DR-344
and C5's frozen high-water were measured against (1,159 `.py` files,
29,331,453 bytes today; C5's own frozen figures were 1,135 files earlier the
same day — see `_corpus_paths`'s own docstring for why tests/ is excluded
and the corpus-scan-scope-mismatch this avoids):

    | Component                          | Measured (warm, process time) |
    |-------------------------------------|-------------------------------|
    | cache revalidate (C1 summarize_paths, includes sha256 + AST parse-on-miss + line count) | **125.0ms** |
    | summary aggregate (compute_distribution + spawn-evidence resolution over already-parsed summaries) | ~5-15ms |
    | telemetry aggregate (C4 handler_elapsed_by_op over the current op-latency generation) | ~60ms (94/273 ops, 28,117-row spike figure; this repo's live figure varies with sink size) |
    | line count | **0ms — INSIDE cache revalidate.** `module_summary._compute_module_summary` derives `line_count` from the same in-memory text `sites_in_source` reads (C1's own docstring); there is no second pass here. C5's separate "64ms" figure was a standalone measurement of the same cost this row already counts, not an addition to it. |
    | serialization (building + `json.dumps`-ready dict) | <5ms |
    | client door (cost for a client to REACH this op — not paid inside this handler; see `FROZEN_CLIENT_DOOR_MS`) | ~64.1ms (spike reconstruction, out-of-process, reference only) |

    | path re-keying (`Path.resolve()` per corpus module + per op entrypoint) | **0ms — removed 2026-08-21.** See below. |

    Handler-only total (cache revalidate + summary aggregate + telemetry
    aggregate + serialization, excludes client door): **171.9-187.5ms
    measured** on this repo's real non-test tree, under BOTH the 500ms
    brightline (DR-344 constraint 1) and constraint 7's separate 200ms "one
    process over 200ms needs a fix, not a rationale" bar. Reported honestly
    via `self_assessment.under_per_process_bar`, never hedged (DR-344's own
    negative-spec: "a breach is reported as a breach").

    THE DOMINANT TERM WAS NOT WHAT THIS TABLE SAID, and the correction is
    kept here because the wrong causal story is what let the breach sit.
    The table claimed the floor cost was the sha256-over-bytes read C1 calls
    non-negotiable-downward (DR-236 forbids mtime keying for a
    correctness-critical read). Profiled on the warm path, sha256 is **8.7ms**
    per run. The real cost was `Path.resolve()` in the re-keying loops —
    1,177 calls and ~112ms here, plus ~283 calls and ~31ms in
    `spawn_bearing_ops.resolve_op_entrypoints`, each one an
    `nt._getfinalpathname` syscall on Windows. Removing the round-trip
    (`_relpath_under`, which keeps `resolve()` as the second leg) took the
    handler from ~281-328ms to the figure above. Anything re-added to this
    loop is paid ~1,159 times: profile before believing a budget table,
    including this one.

    The byte-denominated assertion below still exists for the reason it
    always did — to route future corpus growth into C5's ratchet, not into a
    widened census budget (anti-scope: "Do not widen any DR-344 bar to make
    the census green") — but note the sha256 read it denominates is now a
    minor term, not the floor.

    2026-08-22 note — a predecessor handoff reported this warm-path
    self-assertion at 531-562ms of process time under real machine load
    (~50 concurrent sessions), a figure NOT re-measured or reproduced on
    this box (own negative-spec above: don't repeat the wrong-causal-story
    mistake by asserting an inherited number as a measurement). The
    handoff's own step had already been discharged before it was picked
    up: `d45e93099` (this module's own entry above) took the warm total
    from 281-328ms to 171.9-187.5ms by removing the `Path.resolve()`
    re-keying, and `850f6906a` (landed same day, orphaned-session cleanup)
    only extended two OTHER tests' Darwin support — neither commit touched
    `test_census_budget`'s assertion, so the pass it reports is not hollow.
    What this pass actually measured, on this box, before touching
    anything: warm handler total 171.9-250ms over several runs
    (`test_census_budget` / `test_census_cold_process_budget` both green
    already, ~250-330ms of margin under the 500ms brightline — not a
    hollow assertion, not thin; the 531-562ms figure simply did not
    reproduce, likely a load-transient measurement, never established here
    as a code regression). Optimised anyway, since the win was real and
    free of tradeoffs once found: `_corpus_paths` walked the corpus with
    `Path.rglob("*.py")`, which descends into EVERY directory first and
    filters `__pycache__`/`tests` out only after listing them — 271 of 378
    directories under `corpus_root` (72%) are exactly those two names, so
    the walk was fully listing subtrees it kept zero files from. A second,
    fully redundant
    `p.stat()` pass then re-touched every kept file to compute
    `bytes_scanned`, on top of the full-body read `compute_stamp` already
    pays per file. Fix: `_walk_corpus` (this module) walks via `os.scandir`
    with in-place directory pruning — a `__pycache__`/`tests` directory is
    never entered, not entered-then-discarded — and yields `(path, size)`
    together off the same `scandir` call, so `bytes_scanned` costs zero
    additional syscalls. Re-measured warm-path handler total: **109-156ms**
    (~350-390ms of margin under the brightline) — a further, measured cut
    on top of an already-passing baseline, not a breach fix.
    `_relpath_under` (this module) and its sibling
    `spawn_bearing_ops._relpath_under_repo_root` also each dropped a
    `PurePath.parts`-based `..`-membership check (a `Sequence`-mixin
    fallback via `__iter__`/`__getitem__` in this interpreter's pathlib, not
    a plain-tuple `in`) in favour of splitting the POSIX string already
    computed for the return value — a smaller, additive win on the same
    profiled path, not the dominant fix.

Byte-denominated assertion (staff-eng Finding 4): `FROZEN_BYTE_SCAN_MS_PER_MB`
freezes the measured cache-revalidate-ms-per-scanned-MB ratio
(125.0ms / 27.97MB ≈ 4.47ms/MB, re-verified 2026-08-21 against the non-test
tree). `census()` computes the SAME ratio live and reports it under
`self_assessment.byte_scan_ms_per_mb` — a caller comparing that live figure
against `FROZEN_BYTE_SCAN_MS_PER_MB` sees corpus-density growth (more bytes
per file, not just more files) even when C5's module/line-count ratchet has
not yet tripped.

Corpus identity (Finding 5, stays OPEN): `census()` refuses (raises
`CorpusIdentityError`) rather than silently computing a ratchet-checked
report over the wrong tree, whenever the resolved corpus root's own name is
not `coordinator_core` — the one concrete, cheap identity check available
today. This does NOT fully resolve Finding 5's open question ("does a
warm-server corpus root ever diverge from a client-tree corpus root for the
SAME repo" — deferred, needs the probe the plan names:
`coordinator-invoke op_census.report` once warm, once with
`COORDINATOR_WARM=0`, comparing resolved corpus root/module count) — it is
the floor this chunk can ship today without inventing an unmeasured
resolution to a question the plan records as open.

Negative-spec:
    - `census()` never spawns a subprocess and never measures wall clock —
      every timing figure inside it is `time.process_time()` (anti-scope:
      "Do not use wall clock anywhere, including in the census's own
      self-assertion").
    - `FROZEN_INVOCATION_TAX_MS` / `FROZEN_CLIENT_DOOR_MS` are NOT
      live-measured on every `census()` call — `timing.measure_invocation_tax_ms`
      spawns child processes importing `coordinator_core.ops`, which would
      blow this op's own budget if paid per request. They are periodic,
      out-of-band reference figures (re-measured via
      `timing.measure_invocation_tax_ms` when this module's own docstring
      figures are re-verified), applied uniformly, exactly as
      `timing.invocation_tax_dispositions` already does for the
      invocation-tax axis itself.
    - CORRECTED 2026-08-23, recorded rather than swapped out.
      `FROZEN_INVOCATION_TAX_MS` was frozen at 343.8 — a bare-interpreter
      `python -S -c "import coordinator_core.ops"` figure, the exact wrong
      shape `coordinator_core/op_census/timing.py`'s own docstring records
      as its THIRD wrong-value incident on this axis. Because this constant
      is `census()`'s default `measured_tax_ms` on every real request, this
      was not only `timing.py`'s bug: every PRODUCTION `op_census.report`
      call inherited it, `invocation_tax_dispositions` stamped `OVER_BAR`
      for every op every time (343.8 >= the 50ms bar), and `_four_axis_report`
      (this module's own emitted-`cleared` boundary, the one `op_census.report`
      clients actually read — `timing.emit_dispositions` is test-only, never
      called from here) reported an empty `cleared` set on every live
      request. Re-frozen at **7.03ms** (`measure_invocation_tax_ms(iterations=20)`,
      this box, this pass) in the shape `timing.py`'s probe now measures —
      the one-shot/trampoline cold path with lazy op registration armed via
      `COORDINATOR_CORE_LAZY_OPS=1`, not a bare interpreter — comfortably
      under the 50ms bar. See `timing.py`'s module docstring for the full
      shape argument.
    - `census()` reads ONLY the current (newest) op-latency generation
      (`op_latency.sink_generations(...)[:1]`) — folding in rotated
      generations is explicitly out of scope (plan § Out of scope).
    - `report["budget_breaches"]` is a HEADLINE, capped at
      `CENSUS_BREACH_TOP_N` ranked rows, computed by
      `op_latency.breach_summary` over the telemetry rows this function has
      ALREADY read — one extra in-memory pass, never a second sink read, and
      no widening of this op's read bound. The full ranked list lives at
      `op_census.breaches` (`coordinator_core.ops.op_budget_breaches`), a
      sibling op rather than a mode of this one: a breach view needs none of
      the sha256/AST corpus scan that dominates this handler's cost, and it
      must not be gated behind `CorpusIdentityError`, which is a fact about
      the source tree and says nothing about a sink.
    - Does not build a second spawn inventory or a second cache — reuses
      `op_census.module_summary` (C1), `op_census.spawn_bearing_ops` (C2),
      and `op_census.timing` (C4) verbatim (anti-scope).
    - Does not kill anything — `census()` produces evidence only (plan
      anti-scope: "Do not kill anything. This plan measures. P2 kills.").
    - Registration-wiring: `op_census.report` is registered in `ipc._REGISTRY`
      (this module's own `@register_op` decorator) AND enrolled in both
      hand-maintained lazy/eager dispatch seams
      (`coordinator_core/ops/__init__.py::_EAGER_OP_MODULES`,
      `coordinator_core/ops/_registry_map.py::OP_MODULE_MAP`) — fully
      reachable via `coordinator-invoke op_census.report`. (Review: staff-eng
      Finding 4 — this bullet previously described the wiring as an
      outstanding gap; both seams were closed in this same diff.) Nothing yet
      guards the ops-side seam against recurrence — `_EAGER_OP_MODULES` has
      no completeness gate the way `coordinator_core/hooks/*.py` does via
      `test_eager_hook_modules_covers_every_register_op.py`; that gap is
      tracked separately, not by this module.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C6.md
               docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md
               docs/decisions/DR-344-the-brightline-process-budget-for-makima.md
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from coordinator_core.engine_root import is_published_engine_mirror
from coordinator_core.ipc import register_op
from coordinator_core.op_census import module_summary
from coordinator_core.op_census.line_count import (
    PER_MODULE_LINE_BAR,
    compute_distribution,
    evaluate_ratchet,
    ratchet_check,
)
from coordinator_core.op_census.spawn_bearing_ops import (
    OpEntrypoint,
    live_registry_op_names,
    load_spawn_index,
    ops_with_spawn_evidence,
    registry_divergence,
    resolve_op_entrypoints,
    save_spawn_index,
)
from coordinator_core.op_census.timing import (
    AxisResult,
    Disposition,
    NoDataReason,
    PROCESS_TIME_BAR_MS,
    UniformInvocationTaxError,
    _raise_if_tax_uniformly_over_bar,
    invocation_tax_dispositions,
    handler_elapsed_by_op,
)
from coordinator_core.telemetry.op_latency import breach_summary

__all__ = [
    "CorpusIdentityError",
    "UniformInvocationTaxError",
    "FROZEN_CORPUS_ROOT_NAME",
    "FROZEN_INVOCATION_TAX_MS",
    "FROZEN_CLIENT_DOOR_MS",
    "FROZEN_BYTE_SCAN_MS_PER_MB",
    "BRIGHTLINE_BUDGET_MS",
    "PER_PROCESS_BAR_MS",
    "MAX_TELEMETRY_ROWS",
    "CENSUS_BREACH_TOP_N",
    "census",
    "measure_census_self_timing_ms",
]

#: The one concrete, cheap identity check `census()` can make today for
#: Finding 5's open corpus-root question — see module docstring.
FROZEN_CORPUS_ROOT_NAME = "coordinator_core"

#: Reference-only, out-of-band figures — see negative-spec above for why
#: these are not live-measured per `census()` call.
#: CORRECTED 2026-08-23: was 343.8, a bare-interpreter figure that stamped
#: OVER_BAR on every op on every real request — see negative-spec above.
FROZEN_INVOCATION_TAX_MS = 7.03
FROZEN_CLIENT_DOOR_MS = 64.1

#: 125.0ms / 27.97MB, re-verified 2026-08-21 against this repo's own
#: non-test coordinator_core/ tree (1,159 files, 29,331,453 bytes). See
#: module docstring's "Byte-denominated assertion" section.
FROZEN_BYTE_SCAN_MS_PER_MB = 4.47

#: DR-344 constraint 1.
BRIGHTLINE_BUDGET_MS = 500.0

#: DR-344 constraint 7 — "one process taking over 200ms needs a fix, not a
#: rationale." `census()` reports against this bar; it does not widen it and
#: does not hide a breach (see module docstring's budget-table discussion).
PER_PROCESS_BAR_MS = 200.0

#: Bound on telemetry rows read from the current op-latency generation —
#: mirrors `cost_census.MAX_ROWS_SCANNED`'s own bounding discipline.
MAX_TELEMETRY_ROWS = 200_000

#: Ranked breach rows folded into this report. Short by design: this is the
#: census's headline, and the full ranked list is `op_census.breaches`
#: (`coordinator_core.ops.op_budget_breaches`) — the surface that exists to
#: carry it. `budget_breaches.totals.breaching_ops` is always the untruncated
#: count, so a short list here can never read as a clean box.
CENSUS_BREACH_TOP_N = 5


class CorpusIdentityError(Exception):
    """Raised when `census()` resolves a corpus root whose name is not
    `FROZEN_CORPUS_ROOT_NAME` — a REFUSAL (never a degrade, never a skip),
    per the plan's own AC: "the ratchet assertions refuse ... when the root
    is not the one the frozen figures were taken against." See module
    docstring's "Corpus identity" section for what this does and does not
    resolve."""


def _corpus_root() -> Path:
    """This module lives at `coordinator_core/ops/op_census_report.py`; the
    corpus this census scans is `coordinator_core/` itself — one directory
    up. Deliberately not dependent on the caller-supplied `repo_root` (which
    names the CONSUMING repo an op dispatched against, not the engine's own
    source tree) — see `coordinator_core.ops.shim_usage_census`'s own
    published-mirror-vs-source-tree distinction for the same class of
    question, resolved the same way: identity is a property of this
    module's own location, not of the caller's cwd."""
    return Path(__file__).resolve().parents[1]


def _relpath_under(path: Path, root: Path, root_resolved: Path) -> Optional[str]:
    """`path` as a POSIX string relative to the engine repo root, or None if outside.

    The re-keying loop below runs once per corpus module — ~1,159 today — and
    each `Path.resolve()` is a filesystem round-trip, one
    `nt._getfinalpathname` syscall per call on Windows. Profiled on the warm
    path it was 1,177 calls and ~112ms of a ~300ms run, the single largest
    term in the census and larger than the sha256 corpus read this module's
    budget table names as the dominant, floor cost. The paths being re-keyed
    come from a walk rooted at `corpus_root`, so they already carry the root's
    prefix and `relative_to` answers without touching disk.

    `root_resolved` is kept as the second leg rather than dropped, and the
    fast leg REFUSES any result still carrying a `..` component. `relative_to`
    is a string operation: it happily answers `repo/pkg/../pkg/mod.py` relative
    to `repo` with `pkg/../pkg/mod.py`, which keys the same module under a
    second, different string. Only `resolve()` collapses that. Dropping the
    second leg as a "simplification" would re-key such a module away from the
    string the axes look it up by — staff-eng Finding 0's exact failure shape,
    where a re-keying voided the line_count axis for every op without raising.

    Known, accepted difference: for a path whose component is a SYMLINK into
    the tree, the fast leg answers by path where `resolve()` answered by
    physical location. The corpus walk this feeds produces physical paths under
    `corpus_root`, so the case does not arise here; were it to, the fast leg's
    answer is the more useful one — the old behaviour dropped such a module
    from the axis silently.

    The `..`-component check reads `relative_str.split("/")` rather than
    `relative.parts` — profiled on the warm path, `PurePath.parts` (a
    `Sequence`-mixin object in this interpreter's pathlib, not a plain
    tuple) falls back to the ABC's `__contains__`, which walks it via
    `__iter__`/`__getitem__` and re-parses on the way; splitting the POSIX
    string already computed for the return value is the same check over a
    real `list[str]`, at native `str.split`/`in` cost.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = None
    if relative is not None:
        relative_str = relative.as_posix()
        if ".." not in relative_str.split("/"):
            return relative_str
    try:
        return path.resolve().relative_to(root_resolved).as_posix()
    except ValueError:
        return None


def _index_path(corpus_root: Path) -> Path:
    """Persisted-tier index location — sibling to `op_latency.py`'s own
    `.git/coordinator-sessions/logs/` convention (never tracked, since
    `.git/` itself is never tracked; no `.gitignore` entry needed). Callers
    only persist through this path when `corpus_root.parent/'.git'` already
    exists (see `census()`'s own guard) — a non-git deployment (e.g. the
    published mirror) degrades to a cold build every call rather than
    fabricate a `.git/` directory that isn't there."""
    return (
        corpus_root.parent
        / ".git"
        / "coordinator-sessions"
        / "cache"
        / "op-census-module-index.json"
    )


def _spawn_index_path(corpus_root: Path) -> Path:
    """Persisted-tier spawn-evidence index location -- same directory and
    same non-git-deployment degrade-to-cold-build guard as `_index_path`
    (see that function's own docstring), a sibling file rather than a
    second index shape folded into the module-summary index: spawn
    evidence (C2, `spawn_bearing_ops.SpawnIndex`) and module summaries (C1,
    `module_summary`'s own index) are different value shapes with
    different serializers, kept as separate on-disk files exactly as they
    are separate in-memory index types."""
    return (
        corpus_root.parent
        / ".git"
        / "coordinator-sessions"
        / "cache"
        / "op-census-spawn-index.json"
    )


#: Directory names `_corpus_paths` never descends into — pruned at the
#: `os.scandir` level, not filtered after a full-tree walk. See that
#: function's own docstring for why the distinction is the dominant cost.
_PRUNED_DIR_NAMES = frozenset({"__pycache__", "tests"})


def _walk_corpus(root: Path, skip_counts: Optional[Dict[str, int]] = None) -> Iterable["tuple[Path, int]"]:
    """Yields `(path, size_bytes)` for every NON-TEST `.py` file under
    `root`, using `os.scandir` directly rather than `Path.rglob`.

    Profiled on the warm path (staff-eng finding, this pass): `rglob("*.py")`
    over `corpus_root` walks the WHOLE tree first -- 378 directories, 8,658
    files including every `__pycache__` and `tests` subtree -- and only
    THEN filters down to the ~1,182 non-test modules this census actually
    wants, via a `p.parts` membership check that itself re-parses each
    `Path`. 271 of those 378 directories (72%) are `__pycache__`/`tests`
    subtrees the census never keeps a single file from. `os.scandir` lets a
    directory be excluded from the walk entirely the moment its name is
    seen, before any of its children are ever listed -- the walk never
    enters the pruned subtrees rather than entering and discarding them.

    Each yielded size comes from `DirEntry.stat().st_size` -- on Windows this
    is served from the same `FindNextFile` data the `scandir` call already
    fetched for an ordinary file or directory, not a second `stat()` syscall
    per entry (contrast the old `_corpus_paths` + `p.stat().st_size` shape
    this replaces, which paid one `Path.rglob` walk AND one dedicated
    `stat()` per kept file). Caveat: for a reparse point (e.g. a file-level
    symlink), the default `follow_symlinks=True` on `entry.stat()` DOES issue
    an extra call to resolve the target, so "no extra syscall" holds for the
    common case, not universally.

    Symlinked directories are explicitly NOT followed here
    (`entry.is_dir(follow_symlinks=False)`): a directory reached only via a
    symlink is neither descended into nor yielded. This is a real divergence
    from pre-3.13 `pathlib.Path.rglob`'s `**`, which follows symlinked
    directories by default -- not an approximation of that behaviour. The
    assumption this walk relies on instead is that `corpus_root` contains no
    directory symlinks; VERIFIED as of 2026-08-23 (`find coordinator_core
    -type l` returns nothing). If that ever stops holding, this walk will
    silently under-count relative to the old `rglob`-based one rather than
    raise -- re-verify before trusting a `bytes_scanned`/module-count drop
    as a real corpus shrink.

    Per-entry `OSError` (e.g. a file vanishing or losing permissions between
    `scandir` listing it and this walk's `stat()` call -- not hypothetical on
    a tree many concurrent sessions write to) is swallowed and the entry
    dropped rather than raised, matching `os.scandir(current)`'s own
    directory-listing swallow just below. Optionally pass `skip_counts` (a
    `dict` this function mutates in place, keyed `"scandir_errors"` and
    `"stat_errors"`) to have the caller learn how many entries were dropped
    this way -- see `census()`'s `self_assessment.entries_skipped`, which
    exists so a shrunk corpus is visible rather than indistinguishable from a
    smaller-but-honest one (staff-eng slice-A review, Finding 4: the old
    `_corpus_paths` + `sum(p.stat().st_size for p in paths)` shape had no
    try/except at all and would raise loudly on the same race; this walk
    trades that hard failure for a counted, visible one rather than a purely
    silent one, since a hard failure on a transient TOCTOU race would itself
    be a worse outcome on this box).
    """
    if skip_counts is None:
        skip_counts = {}
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            skip_counts["scandir_errors"] = skip_counts.get("scandir_errors", 0) + 1
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in _PRUNED_DIR_NAMES:
                    stack.append(Path(entry.path))
                continue
            if not entry.name.endswith(".py") or entry.name.startswith("test_"):
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                skip_counts["stat_errors"] = skip_counts.get("stat_errors", 0) + 1
                continue
            yield Path(entry.path), size


def _corpus_paths(corpus_root: Path) -> List[Path]:
    """Every NON-TEST `.py` file under `corpus_root` — excludes
    `__pycache__`, any path with a `tests` segment, and any `test_*.py`
    filename. This is the same scope DR-344 and C5's frozen high-water were
    measured against ("Measured on 2026-08-21, non-test coordinator_core:
    580,560 lines across 1,135 modules" — DR-344 § Problem); scanning the
    WHOLE tree (including tests/) here would compare a ~2,800-file corpus
    against a ~1,135-file frozen figure and trip the ratchet on a scope
    mismatch, not a real regression. Sorted for a deterministic module count
    and a deterministic `module_summary.summarize_paths` iteration order.

    Delegates the actual walk to `_walk_corpus` (pruned-descent `scandir`,
    not `rglob` + post-filter) and discards the sizes it yields -- callers
    wanting `census()`'s own `bytes_scanned` should call `_walk_corpus`
    directly instead of pairing this with a second `stat()` pass."""
    return sorted(path for path, _size in _walk_corpus(corpus_root))


def _read_current_generation_entries(repo_root: Path) -> List[dict]:
    """Reads ONLY the current (newest) op-latency generation — see module
    docstring's negative-spec: rotated generations are out of scope. Never
    raises: a missing/unreadable sink degrades to an empty list, matching
    `cost_census.run_census`'s own failure discipline."""
    from coordinator_core.telemetry.op_latency import sink_generations

    try:
        generations = sink_generations(repo_root)[:1]
    except OSError:
        return []

    entries: List[dict] = []
    for path in generations:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    if len(entries) >= MAX_TELEMETRY_ROWS:
                        return entries
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(entry, dict):
                        entries.append(entry)
        except OSError:
            continue
    return entries


def _spawn_axis(
    entrypoints: Dict[str, OpEntrypoint],
    spawn_evidence: Dict[str, tuple],
) -> Dict[str, AxisResult]:
    """Per-op spawn-EVIDENCE disposition — reuses the `Disposition` enum's
    `over_bar`/`under_bar` names but there is no spawn BAR on this axis
    (nothing here is compared against a budget): `over_bar` means "the op's
    owning module carries a recognised spawn site" (module granularity, per
    C2's own deliberate over-approximation, so this can be true for an op
    that is itself perfectly within its own spawn budget), `under_bar` means
    "resolved and carries none", `no_data`/`never_observed` means the op's
    handler could not be resolved to a source file at all (see
    `OpEntrypoint.unresolved_reason`). Consequently `dispositions.cleared`
    excludes every op whose module spawns AT ALL, not only ops that are
    themselves over some enrolled spawn budget — read `cleared` as "no
    spawn evidence found for this op's module," never as "this op is
    verified within its spawn budget." (Review: staff-eng Finding 10 —
    named here rather than renaming the shared `Disposition` enum, which
    would be a cross-cutting rename touching `_four_axis_report`'s
    exhaustive dispatch and every axis/consumer; left to a dedicated pass if
    the confusion proves live in practice.)"""
    results: Dict[str, AxisResult] = {}
    for op_name, ep in entrypoints.items():
        if ep.relpath is None:
            results[op_name] = AxisResult(
                disposition=Disposition.NO_DATA,
                no_data_reason=NoDataReason.NEVER_OBSERVED,
            )
        elif op_name in spawn_evidence:
            results[op_name] = AxisResult(disposition=Disposition.OVER_BAR, sample_count=len(spawn_evidence[op_name]))
        else:
            results[op_name] = AxisResult(disposition=Disposition.UNDER_BAR, sample_count=0)
    return results


def _line_count_axis(
    entrypoints: Dict[str, OpEntrypoint],
    summaries_by_relpath: Dict[str, "module_summary.ModuleSummary"],
) -> Dict[str, AxisResult]:
    """Per-op line-count disposition, keyed through the op's owning module's
    already-computed `ModuleSummary.line_count` (C1) against C5's
    `PER_MODULE_LINE_BAR` — `no_data`/`never_observed` when the op's handler
    did not resolve to a module this census scanned."""
    results: Dict[str, AxisResult] = {}
    for op_name, ep in entrypoints.items():
        summary = summaries_by_relpath.get(ep.relpath) if ep.relpath is not None else None
        if summary is None:
            results[op_name] = AxisResult(
                disposition=Disposition.NO_DATA,
                no_data_reason=NoDataReason.NEVER_OBSERVED,
            )
            continue
        disposition = Disposition.OVER_BAR if summary.line_count > PER_MODULE_LINE_BAR else Disposition.UNDER_BAR
        results[op_name] = AxisResult(disposition=disposition, sample_count=summary.line_count)
    return results


def _serialize_axis(result: Optional[AxisResult]) -> dict:
    if result is None:
        return {"disposition": Disposition.NO_DATA.value, "no_data_reason": NoDataReason.NEVER_OBSERVED.value}
    payload: dict = {"disposition": result.disposition.value}
    if result.disposition is Disposition.NO_DATA:
        payload["no_data_reason"] = result.no_data_reason.value if result.no_data_reason is not None else None
    else:
        payload["p50_ms"] = result.p50_ms
        payload["max_ms"] = result.max_ms
        payload["sample_count"] = result.sample_count
    return payload


def _four_axis_report(
    spawns: Dict[str, AxisResult],
    handler_elapsed: Dict[str, AxisResult],
    invocation_tax: Dict[str, AxisResult],
    line_count: Dict[str, AxisResult],
) -> dict:
    """Serializes the four per-op axes into ONE machine-readable disposition
    per op, and derives `cleared` — ops `under_bar` on all four axes, none
    `no_data`. Dispatches the three `Disposition` states EXHAUSTIVELY, no
    default branch, mirroring `timing.cleared_ops`'s own discipline (staff-
    eng Finding 7): an op with `no_data` on ANY axis can never appear in
    `cleared`, asserted at THIS emitted boundary, not only the in-memory
    enum.

    Uniformity guard, at THIS boundary specifically because it is the real
    one — `op_census.report` clients read `_four_axis_report`'s output,
    never `timing.emit_dispositions` (that function is exercised only by
    `coordinator_core/op_census/tests/test_no_data_is_not_a_pass.py`, it is
    never called from `census()`). Raises `timing.UniformInvocationTaxError`
    if every op with an invocation-tax result reports `OVER_BAR` — see that
    exception's docstring and this module's own 2026-08-23 CORRECTED
    negative-spec block: `FROZEN_INVOCATION_TAX_MS` being frozen at a
    bare-interpreter figure produced exactly this shape on every live
    request before it was corrected, and a broken measurement is
    indistinguishable from a genuinely-uniform fleet from inside this
    function.

    The discriminator itself is `timing._raise_if_tax_uniformly_over_bar`,
    shared with `timing.emit_dispositions` (review finding #6, slice C
    review of c7cb4a565) rather than a second hand-copied literal block —
    the commit that shipped THIS function's own guard duplicated it from
    `emit_dispositions` verbatim, which is precisely the "one cause, both
    halves wrong" shape this axis's history already names."""
    _raise_if_tax_uniformly_over_bar(invocation_tax)
    ops = sorted(set(spawns) | set(handler_elapsed) | set(invocation_tax) | set(line_count))
    axis_maps = {
        "spawns": spawns,
        "handler_elapsed": handler_elapsed,
        "invocation_tax": invocation_tax,
        "line_count": line_count,
    }

    cleared: List[str] = []
    per_op: Dict[str, dict] = {}
    for op_name in ops:
        serialized: Dict[str, dict] = {}
        op_cleared = True
        for axis_name, axis_map in axis_maps.items():
            result = axis_map.get(op_name)
            if result is None:
                serialized[axis_name] = _serialize_axis(result)
                op_cleared = False
                continue
            # Exhaustive dispatch BEFORE serialization -- an unrecognised
            # disposition must raise here, never reach _serialize_axis
            # (which assumes a real Disposition member and would raise its
            # own, unrelated AttributeError first, masking this guard).
            if result.disposition is Disposition.OVER_BAR:
                op_cleared = False
            elif result.disposition is Disposition.NO_DATA:
                op_cleared = False
            elif result.disposition is Disposition.UNDER_BAR:
                pass
            else:
                raise RuntimeError(f"unhandled Disposition {result.disposition!r} in _four_axis_report")
            serialized[axis_name] = _serialize_axis(result)
        per_op[op_name] = serialized
        if op_cleared:
            cleared.append(op_name)

    return {"ops": per_op, "cleared": sorted(cleared)}


def census(
    *,
    repo_root: Optional[Path] = None,
    telemetry_entries: Optional[Iterable[dict]] = None,
    measured_tax_ms: Optional[float] = FROZEN_INVOCATION_TAX_MS,
    persist_index: bool = True,
    strict: bool = False,
) -> dict:
    """Assembles the full machine-readable op_census report. Pure of IPC —
    the `register_op` handler below is a thin wrapper.

    `repo_root` defaults to the corpus root's own parent (the engine source
    tree this module lives in); `telemetry_entries` defaults to the current
    op-latency generation read live; `measured_tax_ms` defaults to the
    frozen reference figure (see module docstring negative-spec for why this
    is never live-measured per call).

    Raises `CorpusIdentityError` (a refusal, not a degrade) when the
    resolved corpus root's name is not `coordinator_core` — this refusal is
    a property of the CORPUS ROOT, and always fires regardless of `strict`.

    The line-count RATCHET is different (staff-eng Finding 5): a measurement
    instrument must still produce a measurement when its verdict is "over",
    so `census()` ALWAYS assembles and emits its report even when the
    corpus has grown past `line_count.FROZEN_HIGH_WATER_*` — the verdict is
    carried as data at `report["line_count"]["ratchet"]`
    (`line_count.evaluate_ratchet`'s own `.to_dict()` shape: `tripped` plus
    `frozen`/`measured` for each of `module_count`/`total_lines`/
    `over_bar_count`). Passing `strict=True` restores today's raising
    behaviour — `line_count.RatchetError` — for callers (e.g. the gate
    test) that still want the refusal as control flow.
    """
    handler_t0 = time.process_time()

    corpus_root = _corpus_root()
    # Review: staff-eng Finding 1 -- a bare directory-name comparison cannot
    # distinguish the published mirror from the live source tree (both are
    # named `coordinator_core`), which is exactly the confusion this refusal
    # exists to prevent. Key on the resolved published-mirror identity too:
    # the FROZEN_HIGH_WATER_* figures were measured over the live tree, not
    # the mirror, so reading a mirror through this path is also a refusal.
    is_mirror = is_published_engine_mirror(str(corpus_root.parent))
    if corpus_root.name != FROZEN_CORPUS_ROOT_NAME or is_mirror:
        raise CorpusIdentityError(
            f"census() refused: corpus root {corpus_root!s} does not match the frozen "
            f"identity {FROZEN_CORPUS_ROOT_NAME!r} the ratcheted figures were taken "
            f"against (is_published_mirror={is_mirror})."
        )
    if repo_root is None:
        repo_root = corpus_root.parent

    # --- cache revalidate -------------------------------------------------
    revalidate_t0 = time.process_time()
    # Review: staff-eng Finding 8 -- persisting under `<corpus_root.parent>/
    # .git/...` fabricates a `.git/` directory in the published mirror (and
    # any non-git deployment), where none exists. Persist only when a real
    # `.git` directory is already there; otherwise degrade to a cold build
    # every call rather than manufacture git-tree structure that isn't ours.
    persist_index = persist_index and (corpus_root.parent / ".git").is_dir()
    index_path = _index_path(corpus_root)
    index = module_summary.load_index(index_path) if persist_index else {}
    # `_walk_corpus` yields (path, size) together off ONE pruned-descent
    # scandir walk -- see its docstring. Sorting here (rather than inside
    # `_corpus_paths`, which now exists only as a thin, path-only wrapper
    # kept for callers that want the walk without the sizes) keeps
    # `summarize_paths`'s iteration order the same deterministic sort this
    # module has always used.
    walk_skip_counts: Dict[str, int] = {}
    walked = sorted(_walk_corpus(corpus_root, skip_counts=walk_skip_counts), key=lambda pair: pair[0])
    paths = [path for path, _size in walked]
    bytes_scanned = sum(size for _path, size in walked)
    summaries = module_summary.summarize_paths(paths, index=index)
    if persist_index:
        module_summary.save_index(index, index_path)
    revalidate_ms = (time.process_time() - revalidate_t0) * 1000.0

    # --- summary aggregate --------------------------------------------------
    aggregate_t0 = time.process_time()
    distribution = compute_distribution(summaries.values())
    # Measurement first, always (staff-eng Finding 5) -- ratchet_outcome is
    # emitted below at report["line_count"]["ratchet"] regardless of
    # `tripped`. `strict=True` additionally re-raises via ratchet_check,
    # preserving today's refusal as control flow for callers that want it.
    ratchet_outcome = evaluate_ratchet(distribution)
    if strict:
        ratchet_check(distribution)

    op_names = live_registry_op_names()
    entrypoints = resolve_op_entrypoints(op_names)
    spawn_index_path = _spawn_index_path(corpus_root)
    spawn_index = load_spawn_index(spawn_index_path) if persist_index else {}
    spawn_evidence = ops_with_spawn_evidence(entrypoints, index=spawn_index)
    if persist_index:
        save_spawn_index(spawn_index, spawn_index_path)
    divergence = registry_divergence()

    # Re-key by the same repo-relative form `resolve_op_entrypoints` produces
    # (POSIX, relative to the ENGINE repo root -- corpus_root.parent, never
    # the caller-supplied `repo_root`, which names the consuming repo an op
    # dispatched against, not the engine source tree `resolve_op_entrypoints`
    # resolved paths against). Review: staff-eng Finding 0 -- re-keying
    # against the dispatch-time repo_root made every relative_to() raise on
    # the real wire path, voiding the line_count axis for all ops silently.
    engine_repo_root = corpus_root.parent
    engine_repo_root_resolved = engine_repo_root.resolve()
    summaries_by_op_relpath: Dict[str, module_summary.ModuleSummary] = {}
    for path_str, summary in summaries.items():
        relpath = _relpath_under(Path(path_str), engine_repo_root, engine_repo_root_resolved)
        if relpath is None:
            continue
        summaries_by_op_relpath[relpath] = summary

    spawns_axis = _spawn_axis(entrypoints, spawn_evidence)
    line_count_axis = _line_count_axis(entrypoints, summaries_by_op_relpath)
    aggregate_ms = (time.process_time() - aggregate_t0) * 1000.0

    # --- telemetry aggregate -----------------------------------------------
    telemetry_t0 = time.process_time()
    if telemetry_entries is None:
        telemetry_entries = _read_current_generation_entries(repo_root)
    telemetry_entries = list(telemetry_entries)
    handler_elapsed_axis = handler_elapsed_by_op(telemetry_entries, op_names)
    invocation_tax_axis = invocation_tax_dispositions(op_names, measured_tax_ms=measured_tax_ms)
    # One extra pass over rows already in memory, never a second read. The
    # process_time axis says WHETHER an op is over the bar; this says how
    # much it took past it, how often, and which way it is going -- and it
    # keeps the caller-timeout and vanished populations separate, which the
    # p50/max axis structurally cannot (see op_latency.breach_summary).
    breaches = breach_summary(telemetry_entries, bar_ms=PROCESS_TIME_BAR_MS, top_n=CENSUS_BREACH_TOP_N)
    telemetry_ms = (time.process_time() - telemetry_t0) * 1000.0

    # --- serialization -------------------------------------------------------
    serialize_t0 = time.process_time()
    dispositions = _four_axis_report(spawns_axis, handler_elapsed_axis, invocation_tax_axis, line_count_axis)
    report = {
        "op": "op_census.report",
        "corpus": {
            "root": str(corpus_root),
            "root_name": corpus_root.name,
            "is_published_mirror": is_mirror,
            "module_count": distribution.module_count,
            "bytes_scanned": bytes_scanned,
        },
        "line_count": {**distribution.to_dict(), "ratchet": ratchet_outcome.to_dict()},
        "registry_divergence": {
            "agrees": divergence.agrees,
            "only_in_live": sorted(divergence.only_in_live),
            "only_in_fast_path": sorted(divergence.only_in_fast_path),
        },
        "dispositions": dispositions,
        "budget_breaches": breaches,
    }
    serialize_ms = (time.process_time() - serialize_t0) * 1000.0

    handler_total_ms = (time.process_time() - handler_t0) * 1000.0
    mb_scanned = bytes_scanned / (1024.0 * 1024.0)
    byte_scan_ms_per_mb = (revalidate_ms / mb_scanned) if mb_scanned > 0 else 0.0

    report["self_assessment"] = {
        "budget_ms": {
            "cache_revalidate_ms": round(revalidate_ms, 3),
            "summary_aggregate_ms": round(aggregate_ms, 3),
            "telemetry_aggregate_ms": round(telemetry_ms, 3),
            "line_count_ms": 0.0,  # inside cache_revalidate_ms — see module docstring
            "serialization_ms": round(serialize_ms, 3),
            "client_door_ms_reference_only": FROZEN_CLIENT_DOOR_MS,
        },
        "handler_total_ms": round(handler_total_ms, 3),
        "handler_plus_client_door_ms": round(handler_total_ms + FROZEN_CLIENT_DOOR_MS, 3),
        "brightline_budget_ms": BRIGHTLINE_BUDGET_MS,
        "per_process_bar_ms": PER_PROCESS_BAR_MS,
        "under_brightline": (handler_total_ms + FROZEN_CLIENT_DOOR_MS) < BRIGHTLINE_BUDGET_MS,
        "under_per_process_bar": handler_total_ms < PER_PROCESS_BAR_MS,
        "byte_scan_ms_per_mb": round(byte_scan_ms_per_mb, 4),
        "frozen_byte_scan_ms_per_mb": FROZEN_BYTE_SCAN_MS_PER_MB,
        "byte_scan_within_frozen_ratio": byte_scan_ms_per_mb <= FROZEN_BYTE_SCAN_MS_PER_MB * 1.10,
        # Review: staff-eng slice-A Finding 4 -- the per-file `entry.stat()`
        # OSError swallow in `_walk_corpus` is new versus the old
        # `_corpus_paths` + `sum(p.stat().st_size for p in paths)` shape,
        # which had no try/except and raised loudly on the same race. A
        # dropped entry can only push module_count/bytes_scanned/line_count
        # DOWN, which can only suppress a real ratchet trip -- never
        # manufacture a false one -- so this count exists to make that
        # shrinkage visible rather than indistinguishable from an honestly
        # smaller corpus.
        "entries_skipped": {
            "scandir_errors": walk_skip_counts.get("scandir_errors", 0),
            "stat_errors": walk_skip_counts.get("stat_errors", 0),
        },
    }
    return report


#: The measurement script run by `measure_census_self_timing_ms` -- reports
#: the CHILD's own `report["self_assessment"]["handler_total_ms"]` (already
#: `time.process_time()`-based inside `census()` itself), mirroring
#: `timing._TAX_PROBE_SCRIPT`'s shape: a minimal in-process call whose own
#: process time is what the parent batches and averages.
_SELF_TIMING_PROBE_SCRIPT = (
    "import json, sys\n"
    "from coordinator_core.ops.op_census_report import census\n"
    "report = census(telemetry_entries=[], persist_index=False)\n"
    "sys.stdout.write(json.dumps({'handler_total_ms': report['self_assessment']['handler_total_ms']}))\n"
)


def measure_census_self_timing_ms(*, k: int = 5, timeout_secs: float = 60.0) -> dict:
    """Periodic, out-of-band re-verification of `census()`'s own handler
    process-time cost, using the Windows job-object batched primitive
    (`coordinator_core.benchmarks.process_time.batched_process_time_ms`) —
    the correct pattern for a sub-150ms `time.process_time()` reading, which
    a single sample cannot resolve past the ~15.625ms Windows scheduler
    tick (see module docstring's budget table and
    `timing.measure_invocation_tax_ms`'s own 5-iteration-mean worked
    example, which this mirrors).

    NOT called on the live `census()`/`op_census.report` request path:
    running the full handler `k` times per real request would itself blow
    the very DR-344 budget this measures. This is a benchmark/reference
    helper only, re-run when this module's own docstring budget-table
    figures are re-verified -- exactly as `measure_invocation_tax_ms` is
    for `FROZEN_INVOCATION_TAX_MS` (see module docstring negative-spec).

    Returns `batched_process_time_ms`'s own dict shape
    (`process_time_ms`/`wall_ms`/`procs_per_call`/`rc`/`k`). This is the ONE
    caller of `batched_process_time_ms` that does not guard on `IS_WINDOWS`
    (every other call site skips off-Windows) -- it runs unconditionally,
    reaching the job-object accounting path on Windows and the kqueue +
    per-pid `wait4` path on Darwin (see `batched_process_time_ms`'s own
    module docstring for both); it still raises `NotImplementedError` on any
    OTHER platform, and propagates any primitive-level failure rather than
    silently degrading to a wrong unit.
    """
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    return batched_process_time_ms(
        [sys.executable, "-c", _SELF_TIMING_PROBE_SCRIPT],
        k=k,
    )


@register_op("op_census.report")
def _op_census_report(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "op_census.report" handler — see module docstring and
    `census()` for the assembled shape. `params` is currently unused (no
    op_census.report parameters are defined yet); accepted for register_op's
    standard handler signature."""
    del params  # unused — see docstring
    return census(repo_root=repo_root)
