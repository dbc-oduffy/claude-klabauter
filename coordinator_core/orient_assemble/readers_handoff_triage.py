"""
coordinator_core.orient_assemble.readers_handoff_triage — C2b reader port:
handoff triage query (stale-executing-plan advisory, ready-to-fire listing,
awaiting-gate listing) plus a fourth source, the tiered plan-orphan census
(`_read_orphaned_plans`).

Purpose: import example-doctrine-repo's `workday-start-handoff-triage.py` CLI's
read-only subcommands (`_cmd_stale_plans`, `_cmd_ready`, `_cmd_awaiting_gate`)
AS-IS — no reimplementation of their query/format logic — and translate their
captured stdout into the shared decision-object shape, per
docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md § C2b. The fourth
source, `_read_orphaned_plans`, is a materially different shape: a direct
in-process import of `coordinator_core.ops.draft_plan_aging.list_orphaned`
with this module's own explicit `_REPO_ROOT`, not a ported CLI subcommand
(see docs/plans/2026-07-31-plan-orphan-ownership-resolver.md, chunk C4).

The source CLI lives at `coordinator/bin/workday-start-handoff-triage.py`
(hyphenated filename, not import-statement-addressable) and is loaded via
`importlib.util.spec_from_file_location` rather than a normal `import` —
the functions themselves are consumed unmodified once loaded.

`trim_orphan_sweep_notes` / `_cmd_trim_notes` (the fourth subcommand in the
source CLI) is deliberately OUT OF SCOPE here — it mutates
`tasks/orphan-sweep-notes.md` and stays ceremony/EM-dispatched, never
assembler-owned (AC of this chunk).

Spec backlink: docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md, chunk C2b

Negative-spec:
    - Does NOT call `trim_orphan_sweep_notes` / `_cmd_trim_notes` — that
      subcommand mutates disk and is explicitly excluded from this
      assembler; a finding suggesting "just wire trim-notes here too" is a
      scope violation of this chunk's AC.
    - Does NOT re-implement the query/format logic inside
      `workday-start-handoff-triage.py` — the ported functions are invoked
      as-is and their stdout is captured, not re-derived line-by-line.
    - Does NOT wire these results into `brief()` — `__init__.py`'s cadence
      dispatch is shared write-surface across C2a-C2d (the plan's own
      "same package — serial, write-overlap" note); this chunk lands
      alongside concurrently-dispatched sibling reader ports, so wiring
      `collect()` into `brief()` is left to a follow-up integration pass.
    - Does NOT route the orphan census through `_SOURCE_PATH`/
      `_load_source_module` — see `_read_orphaned_plans`'s own docstring;
      it calls `list_orphaned` directly, in-process, never via the
      hyphenated-filename loader used for the other three sources.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.draft_plan_aging import AGING_THRESHOLD_DAYS, list_orphaned
from coordinator_core.orient_assemble.reader_result import (
    ReaderResult,
    truncate_external_text,
)

#: Cap on `unrecognized_status` diagnostic lines rendered into `detail` —
#: this bucket is NOT spec'd as "loud, one line per plan" the way P1
#: `authorized_orphan` is (it's a diagnostic tally, not an actionable
#: tier), and it grows with disk contents (11 entries on claude-klabauter, 28 on
#: example-doctrine-repo at time of writing) — bound it the way `cap_judgment_points`
#: bounds other unbounded per-item lists (Review: code-reviewer — Finding 3).
_UNRECOGNIZED_STATUS_LINE_CAP = 10

#: Module-locally derived repo root (precedent: readers_health_reaper.py's
#: own `_REPO_ROOT = Path(__file__).resolve().parents[2]`) — passed
#: EXPLICITLY to `list_orphaned` below, in-process, so the production
#: consumer keeps AC14's explicit-repo_root discipline rather than being
#: routed through the bin CLI's deliberately cwd-relative scope resolution
#: (see coordinator/bin/list-orphaned-plans.py's own module docstring).
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The source CLI's absolute path — resolved relative to this file, never a
#: literal device path (portability discipline, AC-16).
_SOURCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "coordinator"
    / "bin"
    / "workday-start-handoff-triage.py"
)


def _load_source_module():
    """Load the hyphenated-filename source CLI as an importable module.

    A normal `import` statement cannot address a `-`-containing filename;
    `importlib.util.spec_from_file_location` is the standard workaround.
    The loaded module's own `_SCRIPT_DIR`/`_LIB_DIR` sys.path wiring
    (needed for its `from records_query import query_records` deferred
    imports) still resolves correctly, since it derives from the module's
    own `__file__`, unaffected by how it was loaded.
    """
    spec = importlib.util.spec_from_file_location(
        "workday_start_handoff_triage", _SOURCE_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load source module at {_SOURCE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_handoff_triage = _load_source_module()
_cmd_stale_plans = _handoff_triage._cmd_stale_plans
_cmd_ready = _handoff_triage._cmd_ready
_cmd_awaiting_gate = _handoff_triage._cmd_awaiting_gate

#: `_cmd_stale_plans` reads `args.plans_dir`/`args.threshold_days` — mirror
#: the source CLI's own argparse defaults (`_DEFAULT_PLANS_DIR`,
#: `_DEFAULT_STALE_THRESHOLD_DAYS`) rather than hardcoding new literals here.
_STALE_PLANS_DEFAULT_ARGS = argparse.Namespace(
    plans_dir=_handoff_triage._DEFAULT_PLANS_DIR,
    threshold_days=_handoff_triage._DEFAULT_STALE_THRESHOLD_DAYS,
)


def _capture_stdout(cmd_func, args: argparse.Namespace) -> tuple[str, int]:
    """Run one of the ported `_cmd_*` functions, capturing its stdout as-is
    (no output-format re-derivation) rather than reimplementing the query
    logic that produces it."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = cmd_func(args)
    return buf.getvalue(), exit_code


def _read_stale_plans() -> ReaderResult:
    """Stale `status: executing` plan advisory — informational only (the
    source CLI's own docstring: "Advisory only — never blocks the
    ceremony"), surfaced as a directive so the EM sees it in the same
    decision-object surface as every other orient finding."""
    text, _exit_code = _capture_stdout(_cmd_stale_plans, _STALE_PLANS_DEFAULT_ARGS)
    if not text.strip():
        return ReaderResult()
    return ReaderResult(
        directives=[
            {
                "id": "d-handoff-triage-stale-plans",
                "cli": "workday-start-handoff-triage",
                "args": ["stale-plans"],
                "depends_on": None,
                "already_satisfied": False,
                "detail": text.rstrip("\n"),
            }
        ]
    )


#: Extracts the markdown link's `(path)` target off one `_display_handoff`
#: rendered line — `- [title](link_path) — state` (query_record_display.py's
#: `_display_handoff`). `link_path` is repo-root-relative (`rel_id`'s own
#: convention), never a `file://` URI or absolute path.
_LINK_PATH_RE = re.compile(r"\]\(([^)]+)\)")


def _suppress_live_ledger_claims(text: str, *, common_dir: Optional[Path]) -> str:
    """Drop lines whose handoff currently holds a LIVE ledger claim.

    AC11: `_read_ready`'s query (`deployment_state=ready_to_fire AND
    status=open`) has no ledger check — the exact surface that advertised
    the already-worked baton for pickup (this plan's own incident). A
    handoff whose branch-independent claim ledger holds a live claim is
    still-worked, even when the tracked-frontmatter mirror has reverted to
    `open` (the branch-switch-revert desync `resolve_claim_state` exists to
    catch) — so it must be suppressed from this listing regardless of what
    the mirror says.

    A ledger claim held by a DEAD holder degrades to "no ledger claim" inside
    `resolve_claim_state` itself (its own negative-spec) — `source` comes
    back "mirror" or "none" for that case, so the line is kept: a
    dead-holder's baton is genuinely available and must still be advertised.

    Post-hoc line filtering (not a query-time predicate) — the ported
    `_cmd_ready`'s query/format logic is invoked as-is per this module's own
    negative-spec; only the already-rendered markdown-list output is
    filtered here, by parsing each line's own `(link_path)` back out.
    """
    lines = text.split("\n")
    kept: list[str] = []
    for line in lines:
        # Take the RIGHTMOST `](...)` match, not the first (Review:
        # code-reviewer — Finding 0): `_display_handoff`'s rendered shape is
        # `- [title](link_path) — state`, and `title` is free text that can
        # itself contain the literal sequence `](` (e.g. a title reading
        # `Fix ](broken) link`). A leftmost `re.search` would then extract
        # garbage from inside the title as `link_path`, causing
        # `resolve_claim_state` to look up a bogus path, find no ledger
        # claim, and fail to suppress a genuinely live-claimed baton — the
        # exact false-negative this suppression exists to prevent. The real
        # link is always the LAST `](...)` occurrence on the line, since
        # nothing follows it but the fixed ` — {state}` suffix (state is
        # drawn from a closed vocabulary that never contains `](`).
        matches = list(_LINK_PATH_RE.finditer(line))
        if matches:
            link_path = matches[-1].group(1)
            handoff_path = _REPO_ROOT / link_path
            claim_state = resolve_claim_state(
                handoff_path, common_dir=common_dir, repo_root=_REPO_ROOT
            )
            if claim_state.source == "ledger":
                # Live ledger claim (resolve_claim_state only resolves
                # source="ledger" for a LIVE holder — a dead holder degrades
                # to "mirror"/"none" per its own negative-spec) — suppress.
                continue
        kept.append(line)
    return "\n".join(kept)


def _ready_common_dir() -> Optional[Path]:
    """Resolve `common_dir` once per `collect()` call (hot-path note: this
    reader runs across the whole handoff corpus on every orientation
    assembly) rather than re-resolving per handoff inside the filter loop —
    `git_common_dir` is itself `lru_cache`d, so this is a cached-dict-lookup
    saving, not a subprocess-avoidance one, matching `resolve_claim_state`'s
    own `common_dir` parameter docstring."""
    try:
        return git_common_dir(_REPO_ROOT)
    except Exception:
        return None


def _read_ready() -> ReaderResult:
    """Actionable-now handoffs (`deployment_state=ready_to_fire AND
    status=open`) — a directive naming the query subcommand that produced
    the listing, carrying the captured markdown-list with any live-ledger-
    claimed handoff suppressed (AC11 — see `_suppress_live_ledger_claims`)."""
    text, _exit_code = _capture_stdout(_cmd_ready, argparse.Namespace())
    if not text.strip():
        return ReaderResult()
    text = _suppress_live_ledger_claims(text, common_dir=_ready_common_dir())
    if not text.strip():
        return ReaderResult()
    return ReaderResult(
        directives=[
            {
                "id": "d-handoff-triage-ready",
                "cli": "workday-start-handoff-triage",
                "args": ["ready"],
                "depends_on": None,
                "already_satisfied": False,
                "detail": text.rstrip("\n"),
            }
        ]
    )


def _read_awaiting_gate() -> ReaderResult:
    """Gated handoffs (`deployment_state=awaiting_gate AND status=open`)
    plus the coarse `--older-than 6d` stale subset — a directive naming the
    query subcommand, carrying the captured listing as-is. (The
    load-bearing 14d/7d force-recheck predicate is the separate
    `handoff-gate-aging` mechanized tool, not reproduced here — matches the
    source CLI's own documented scope.)"""
    text, _exit_code = _capture_stdout(_cmd_awaiting_gate, argparse.Namespace())
    if not text.strip():
        return ReaderResult()
    return ReaderResult(
        directives=[
            {
                "id": "d-handoff-triage-awaiting-gate",
                "cli": "workday-start-handoff-triage",
                "args": ["awaiting-gate"],
                "depends_on": None,
                "already_satisfied": False,
                "detail": text.rstrip("\n"),
            }
        ]
    )


def _read_orphaned_plans() -> ReaderResult:
    """Tiered orphan census (P1 `authorized_orphan`, P3 `parked` count,
    plus the `legacy_unjoinable`/`unrecognized_status` diagnostic buckets) —
    calls `list_orphaned` in-process with this module's own explicit
    `_REPO_ROOT` (AC14), never through the bin CLI's cwd-relative scope
    resolution (see this module's own docstring "Purpose" note above and
    coordinator/bin/list-orphaned-plans.py's docstring for why that
    distinction matters).

    No parameters, cadence-insensitive — matches `_read_stale_plans`'s
    arity; every cadence runs the same census.
    """
    result = list_orphaned(_REPO_ROOT, AGING_THRESHOLD_DAYS)

    lines: list[str] = []
    # P1 authorized_orphan is spec'd "loud, one line per plan, no age gate"
    # (list_orphaned's own docstring) — deliberately left uncapped/
    # untruncated-in-count here; P1 orphans are expected to stay rare, and
    # capping this tier would defeat its whole purpose (Review:
    # code-reviewer — Finding 3). Its external string field is still routed
    # through truncate_external_text() below (size bound only, not count).
    for entry in result["authorized_orphan"]:
        lines.append(
            f"P1 authorized_orphan: {entry['path']} "
            f"(execution_authorized_by={truncate_external_text(entry['execution_authorized_by'])})"
        )
    if result["parked_count"]:
        lines.append(
            f"P3 parked: {result['parked_count']} unowned plan(s), no authorization, "
            f"age >= {AGING_THRESHOLD_DAYS}d"
        )
    if result["legacy_unjoinable_count"]:
        lines.append(
            f"legacy_unjoinable: {result['legacy_unjoinable_count']} plan(s) predate the "
            "carry-observability fix — unjoinable, excluded from P1/P3"
        )
    unrecognized = result["unrecognized_status"]
    for entry in unrecognized[:_UNRECOGNIZED_STATUS_LINE_CAP]:
        lines.append(
            f"unrecognized_status: {entry['path']} "
            f"(status={truncate_external_text(str(entry['status']))!r})"
        )
    if len(unrecognized) > _UNRECOGNIZED_STATUS_LINE_CAP:
        lines.append(
            f"unrecognized_status: +{len(unrecognized) - _UNRECOGNIZED_STATUS_LINE_CAP} more"
        )

    if not lines:
        return ReaderResult()

    return ReaderResult(
        directives=[
            {
                "id": "d-plan-orphan-tiers",
                "cli": "list-orphaned-plans",
                "args": [],
                "depends_on": None,
                "already_satisfied": False,
                "detail": "\n".join(lines),
            }
        ]
    )


def collect(cadence: str) -> ReaderResult:
    """Compute this reader family's directives/judgment_points.

    `cadence` is accepted for signature parity with sibling reader families
    (`readers_clean_ops.collect`) but unused here — this reader's severity
    does not vary by cadence; all four sources run for every cadence.
    """
    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for result in (
        _read_stale_plans(),
        _read_ready(),
        _read_awaiting_gate(),
        _read_orphaned_plans(),
    ):
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    return ReaderResult(directives=directives, judgment_points=judgment_points)
