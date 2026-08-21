"""coordinator_core.ops.op_census_report — "op_census.report" JSON-RPC op.

Purpose: C6 (`docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md` §C6,
`state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C6.md`) —
THE assembly point. Registers `op_census.report`, which composes C1's cached
per-module summary, C2's spawn evidence, C4's timing dispositions, and C5's
line-count ratchet into ONE machine-readable, per-op, four-axis disposition
(spawns / process_time / invocation_tax / line_count, each `over_bar` /
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
    | telemetry aggregate (C4 process_time_by_op over the current op-latency generation) | ~60ms (94/273 ops, 28,117-row spike figure; this repo's live figure varies with sink size) |
    | line count | **0ms — INSIDE cache revalidate.** `module_summary._compute_module_summary` derives `line_count` from the same in-memory text `sites_in_source` reads (C1's own docstring); there is no second pass here. C5's separate "64ms" figure was a standalone measurement of the same cost this row already counts, not an addition to it. |
    | serialization (building + `json.dumps`-ready dict) | <5ms |
    | client door (cost for a client to REACH this op — not paid inside this handler; see `FROZEN_CLIENT_DOOR_MS`) | ~64.1ms (spike reconstruction, out-of-process, reference only) |

    Handler-only total (cache revalidate + summary aggregate + telemetry
    aggregate + serialization, excludes client door): **~195-215ms measured**
    on this repo's real non-test tree — comfortably under the 500ms
    brightline (DR-344 constraint 1) and close to, but today still just over
    in the worst case, DR-344 constraint 7's separate 200ms "one process
    over 200ms needs a fix, not a rationale" bar. Reported honestly via
    `self_assessment.under_per_process_bar`, never hedged (DR-344's own
    negative-spec: "a breach is reported as a breach"). The dominant, floor
    cost is the sha256-over-bytes read C1's own docstring calls
    non-negotiable-downward (DR-236 forbids mtime keying for a
    correctness-critical read), so it scales with corpus BYTES, which is
    exactly why the byte-denominated assertion below exists — to route
    future corpus growth into C5's ratchet, not into a widened census budget
    (anti-scope: "Do not widen any DR-344 bar to make the census green").

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
      spawns 5 child processes importing `coordinator_core.ops`
      (~300ms+ each), which would blow this op's own budget if paid per
      request. They are periodic, out-of-band reference figures (re-measured
      via `timing.measure_invocation_tax_ms` when this module's own docstring
      figures are re-verified), applied uniformly, exactly as
      `timing.invocation_tax_dispositions` already does for the invocation-tax
      axis itself.
    - `census()` reads ONLY the current (newest) op-latency generation
      (`op_latency.sink_generations(...)[:1]`) — folding in rotated
      generations is explicitly out of scope (plan § Out of scope).
    - Does not build a second spawn inventory or a second cache — reuses
      `op_census.module_summary` (C1), `op_census.spawn_bearing_ops` (C2),
      and `op_census.timing` (C4) verbatim (anti-scope).
    - Does not kill anything — `census()` produces evidence only (plan
      anti-scope: "Do not kill anything. This plan measures. P2 kills.").
    - Registration-wiring gap, named rather than silently worked around:
      registering `op_census.report` in `ipc._REGISTRY` (this module's own
      `@register_op` decorator) is necessary but not sufficient for
      `coordinator-invoke op_census.report` to resolve it — the lazy/eager
      dispatch seam (`coordinator_core/ops/__init__.py::_EAGER_OP_MODULES`,
      `coordinator_core/ops/_registry_map.py::OP_MODULE_MAP`) is a
      hand-maintained list neither of which is in this chunk's declared
      `writes:` scope. Full reachability via `coordinator-invoke` (the
      stated AC) needs a one-line addition to each of those two files by
      whichever chunk/session owns that scope — see this chunk's own report
      Notes.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C6.md
               docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md
               docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.op_census import module_summary
from coordinator_core.op_census.line_count import (
    PER_MODULE_LINE_BAR,
    compute_distribution,
    ratchet_check,
)
from coordinator_core.op_census.spawn_bearing_ops import (
    OpEntrypoint,
    live_registry_op_names,
    ops_with_spawn_evidence,
    registry_divergence,
    resolve_op_entrypoints,
)
from coordinator_core.op_census.timing import (
    AxisResult,
    Disposition,
    NoDataReason,
    invocation_tax_dispositions,
    process_time_by_op,
)

__all__ = [
    "CorpusIdentityError",
    "FROZEN_CORPUS_ROOT_NAME",
    "FROZEN_INVOCATION_TAX_MS",
    "FROZEN_CLIENT_DOOR_MS",
    "FROZEN_BYTE_SCAN_MS_PER_MB",
    "BRIGHTLINE_BUDGET_MS",
    "PER_PROCESS_BAR_MS",
    "MAX_TELEMETRY_ROWS",
    "census",
]

#: The one concrete, cheap identity check `census()` can make today for
#: Finding 5's open corpus-root question — see module docstring.
FROZEN_CORPUS_ROOT_NAME = "coordinator_core"

#: Reference-only, out-of-band figures — see negative-spec above for why
#: these are not live-measured per `census()` call.
FROZEN_INVOCATION_TAX_MS = 343.8
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


def _index_path(corpus_root: Path) -> Path:
    """Persisted-tier index location — sibling to `op_latency.py`'s own
    `.git/coordinator-sessions/logs/` convention (never tracked, since
    `.git/` itself is never tracked; no `.gitignore` entry needed)."""
    return (
        corpus_root.parent
        / ".git"
        / "coordinator-sessions"
        / "cache"
        / "op-census-module-index.json"
    )


def _corpus_paths(corpus_root: Path) -> List[Path]:
    """Every NON-TEST `.py` file under `corpus_root` — excludes
    `__pycache__`, any path with a `tests` segment, and any `test_*.py`
    filename. This is the same scope DR-344 and C5's frozen high-water were
    measured against ("Measured on 2026-08-21, non-test coordinator_core:
    580,560 lines across 1,135 modules" — DR-344 § Problem); scanning the
    WHOLE tree (including tests/) here would compare a ~2,800-file corpus
    against a ~1,135-file frozen figure and trip the ratchet on a scope
    mismatch, not a real regression. Sorted for a deterministic module count
    and a deterministic `module_summary.summarize_paths` iteration order."""
    return sorted(
        p
        for p in corpus_root.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts and not p.name.startswith("test_")
    )


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
    """Per-op spawn-evidence disposition — `over_bar` when the op's owning
    module carries a recognised spawn site (module granularity, per C2's own
    deliberate over-approximation), `under_bar` when it resolved and does
    not, `no_data`/`never_observed` when the op's handler could not be
    resolved to a source file at all (see `OpEntrypoint.unresolved_reason`)."""
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
    process_time: Dict[str, AxisResult],
    invocation_tax: Dict[str, AxisResult],
    line_count: Dict[str, AxisResult],
) -> dict:
    """Serializes the four per-op axes into ONE machine-readable disposition
    per op, and derives `cleared` — ops `under_bar` on all four axes, none
    `no_data`. Dispatches the three `Disposition` states EXHAUSTIVELY, no
    default branch, mirroring `timing.cleared_ops`'s own discipline (staff-
    eng Finding 7): an op with `no_data` on ANY axis can never appear in
    `cleared`, asserted at THIS emitted boundary, not only the in-memory
    enum."""
    ops = sorted(set(spawns) | set(process_time) | set(invocation_tax) | set(line_count))
    axis_maps = {
        "spawns": spawns,
        "process_time": process_time,
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
) -> dict:
    """Assembles the full machine-readable op_census report. Pure of IPC —
    the `register_op` handler below is a thin wrapper.

    `repo_root` defaults to the corpus root's own parent (the engine source
    tree this module lives in); `telemetry_entries` defaults to the current
    op-latency generation read live; `measured_tax_ms` defaults to the
    frozen reference figure (see module docstring negative-spec for why this
    is never live-measured per call).

    Raises `CorpusIdentityError` (a refusal, not a degrade) when the
    resolved corpus root's name is not `coordinator_core`, and re-raises
    `line_count.RatchetError` when the corpus has grown past its frozen
    high-water — both per the plan AC: "the ratchet assertions refuse."
    """
    handler_t0 = time.process_time()

    corpus_root = _corpus_root()
    if corpus_root.name != FROZEN_CORPUS_ROOT_NAME:
        raise CorpusIdentityError(
            f"census() refused: corpus root {corpus_root!s} does not match the frozen "
            f"identity {FROZEN_CORPUS_ROOT_NAME!r} the ratcheted figures were taken "
            "against (Finding 5, stays OPEN)."
        )
    if repo_root is None:
        repo_root = corpus_root.parent

    # --- cache revalidate -------------------------------------------------
    revalidate_t0 = time.process_time()
    index_path = _index_path(corpus_root)
    index = module_summary.load_index(index_path) if persist_index else {}
    paths = _corpus_paths(corpus_root)
    bytes_scanned = sum(p.stat().st_size for p in paths)
    summaries = module_summary.summarize_paths(paths, index=index)
    if persist_index:
        module_summary.save_index(index, index_path)
    revalidate_ms = (time.process_time() - revalidate_t0) * 1000.0

    # --- summary aggregate --------------------------------------------------
    aggregate_t0 = time.process_time()
    distribution = compute_distribution(summaries.values())
    ratchet_check(distribution)  # refuses (raises RatchetError) on growth

    op_names = live_registry_op_names()
    entrypoints = resolve_op_entrypoints(op_names)
    spawn_evidence = ops_with_spawn_evidence(entrypoints)
    divergence = registry_divergence()

    # Re-key by the same repo-relative form `resolve_op_entrypoints` produces
    # (POSIX, relative to the repo root) so `_line_count_axis` can look an
    # op's owning module up directly.
    repo_root_resolved = repo_root.resolve()
    summaries_by_op_relpath: Dict[str, module_summary.ModuleSummary] = {}
    for path_str, summary in summaries.items():
        try:
            relpath = Path(path_str).resolve().relative_to(repo_root_resolved).as_posix()
        except ValueError:
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
    process_time_axis = process_time_by_op(telemetry_entries, op_names)
    invocation_tax_axis = invocation_tax_dispositions(op_names, measured_tax_ms=measured_tax_ms)
    telemetry_ms = (time.process_time() - telemetry_t0) * 1000.0

    # --- serialization -------------------------------------------------------
    serialize_t0 = time.process_time()
    dispositions = _four_axis_report(spawns_axis, process_time_axis, invocation_tax_axis, line_count_axis)
    report = {
        "op": "op_census.report",
        "corpus": {
            "root": corpus_root.name,
            "module_count": distribution.module_count,
            "bytes_scanned": bytes_scanned,
        },
        "line_count": distribution.to_dict(),
        "registry_divergence": {
            "agrees": divergence.agrees,
            "only_in_live": sorted(divergence.only_in_live),
            "only_in_fast_path": sorted(divergence.only_in_fast_path),
        },
        "dispositions": dispositions,
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
    }
    return report


@register_op("op_census.report")
def _op_census_report(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "op_census.report" handler — see module docstring and
    `census()` for the assembled shape. `params` is currently unused (no
    op_census.report parameters are defined yet); accepted for register_op's
    standard handler signature."""
    del params  # unused — see docstring
    return census(repo_root=repo_root)
