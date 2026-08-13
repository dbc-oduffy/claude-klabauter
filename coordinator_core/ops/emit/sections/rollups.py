"""Section porter — Completion Rollups (envelope key: ``completion_rollups``).

Emits the two-grain completion rollup: one WeekRollup + one DayRollup, returned as a single
``records`` list (order-insensitive per the parity harness). The section fans OUT to the
``completion_rollups.{day,week}`` envelope arrays — the envelope layer splits ``records`` on
``grain`` when composing the final object. There is no malformed bucket (returns ``[]``).

Faithful byte/semantic port of bash SECTION 5 (emit-cockpit-snapshot.sh):
  - completion facts from ``query-completions.sh --since 30d --format json``;
  - WEEK facts are computed over the chain-DEDUPED set (group_by chain, first-wins; null
    chains are distinct atoms keyed ``__null_<index>``); DAY facts are computed over the RAW
    today-filtered set (bash does NOT dedup the day grain);
  - ``reviews_conducted`` / ``verdicts`` (WEEK only) are derived from the review-trail valid
    set — the same source as SECTION 3 (list-review-trail-records.sh + the section-3 quarantine
    filter). rollups recomputes it here because each section's collect() is standalone;
  - ISO week (YYYY-Www) via ``date.isocalendar()`` (bash python3→perl fallback collapses to
    the native call); local day via the coordinator-daily-day.sh seam;
  - ``max_commit_sha`` is the lexicographically-greatest valid SHA across ALL entries
    (bash:1040-1043) — an emit-volatile sample, normalized out of parity.

Record sources are now invoked in-process (no node/bash spawn): completions via
``coordinator_core.ops.ceremony.records_query.query_records(record_type="completion",
since=_since_cutoff(ctx))`` — an ISO ``YYYY-MM-DD`` anchored to ``ctx.observed_at``, not the
literal ``"30d"`` — the native records seam (repointed off the ``node query-records.js
--type completion --since 30d`` bridge; the seam ports the oracle's ``since=`` grammar
verbatim, ``created >= cutoff`` with missing-``created`` excluded, applied before
``where``, per query-records.js:1469-1493) — and review-trail listing via the native
``coordinator_core.ops.list_review_trail_records`` module directly.

Port of: emit-cockpit-snapshot.sh (coordinator-claude 07eedcfb, 2026-07-19) § SECTION 5 —
  Completion Rollup. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P05
"""

from __future__ import annotations

import datetime
import os
import re

from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections._shared import (
    _validate_review_trail_file,
    normalize_frontmatter,
)
from coordinator_core.ops.emit.sections.review_trail import _list_review_trail_paths

# Commit-SHA shape filter (bash:1026/1041/1149 ``test("^[0-9a-f]{7,40}$")``).
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


# --------------------------------------------------------------------------- record sources
def _query_completions(ctx: EmitContext) -> list[dict]:
    """Native records seam, ``type="completion"``, ``since=_since_cutoff(ctx)`` → completion records.

    Repoints the retired ``node query-records.js --type completion --since 30d --format
    json`` bridge onto ``coordinator_core.ops.ceremony.records_query.query_records`` — the
    in-process seam that reuses ``ops.records_query``'s ported collect/parse/since-filter
    internals (query-records.js's ``since=`` grammar, ``created >= cutoff`` with
    missing-``created`` excluded, applied before ``where`` — query-records.js:1469-1493).
    ``worktree_root`` mirrors the retired spawn's root resolution exactly: ``subprocess_root``
    (frozen-fixture test isolation) when set, else ``repo_root`` — the same value the spawn's
    ``cwd``/``--root`` resolved to.
    Parity: bash:992 (``|| echo "[]"``) — a query failure (unknown type, unsupported
    ``where``/``since`` grammar, or any other raise) degrades to ``[]``, never aborts the
    emit; the seam's own return shape (``[]`` on a directory-scan failure) is unaffected.

    ``since=`` is passed as an ISO ``YYYY-MM-DD`` literal anchored to ``ctx.observed_at``,
    not the bare relative token ``"30d"``: ``records_query._parse_relative_date`` resolves
    a relative ``since`` against ``datetime.now(timezone.utc)`` at call time (real wall
    clock), which would silently narrow the window as real time advances past this
    emission's own captured instant — anchoring to ``ctx.observed_at`` keeps the 30-day
    window self-consistent with the rest of this emission regardless of when the query
    actually executes.
    """
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    # Review: coordinatorcode-reviewer — cutoff computed BEFORE the try so a malformed
    # ctx.observed_at raises loudly instead of being swallowed by the query-failure except
    # below, which would silently zero completions.
    cutoff = _since_cutoff(ctx)
    try:
        return query_records("completion", root, since=cutoff)
    except (ValueError, SystemExit):
        return []


def _since_cutoff(ctx: EmitContext) -> str:
    """ISO ``YYYY-MM-DD`` 30 days before ``ctx.observed_at`` (the emission's own instant)."""
    observed = datetime.datetime.strptime(
        ctx.observed_at, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=datetime.timezone.utc)
    cutoff = observed - datetime.timedelta(days=30)
    return cutoff.strftime("%Y-%m-%d")


def _local_day(ctx: EmitContext) -> str:
    """Local day (YYYY-MM-DD) — native port of the ``coordinator-daily-day.sh`` seam.

    ``coordinator_local_day()`` in the bash lib is ``date -I 2>/dev/null || date +%Y-%m-%d``
    — the LOCAL calendar day, no repo/coordinator-root dependency whatsoever. Python's
    ``date.today()`` (local system clock) is exactly that value; there is nothing left for
    a subprocess to resolve that this call doesn't already give natively, so the bash
    bridge is retired outright rather than kept as a fallback path.
    """
    del ctx  # unused: local-day resolution has no coordinator/repo dependency
    return datetime.date.today().isoformat()


def _iso_week() -> str:
    """Current ISO week ``YYYY-Www`` (bash:1065 python3 one-liner; perl fallback collapsed)."""
    y, w, _ = datetime.date.today().isocalendar()
    return f"{y}-W{w:02d}"


# --------------------------------------------------------------------------- fact helpers
def _fm(entry: dict) -> dict:
    return normalize_frontmatter(entry)


def _loe(fm: dict) -> dict:
    loe = fm.get("loe")
    return loe if isinstance(loe, dict) else {}


def _dedup_by_chain(completions: list[dict]) -> list[dict]:
    """First-wins dedup on chain grain; null chains are distinct atoms (bash:998-1013).

    Each entry gets ``chain_key = chain if non-null else "__null_<index>"``; the first entry
    per key is kept. Returns the deduped frontmatter dicts (order-insensitive downstream).
    """
    seen: set[str] = set()
    kept: list[dict] = []
    for idx, entry in enumerate(completions):
        fm = _fm(entry)
        chain = fm.get("chain")
        chain_key = f"__null_{idx}" if chain is None else chain
        if chain_key in seen:
            continue
        seen.add(chain_key)
        kept.append({"chain_key": chain_key, "fm": fm})
    return kept


def _tshirt_counts(fms: list[dict]) -> dict:
    """group_by non-null ``loe.tshirt`` → {size: count} (bash:1018-1023 / 1141-1146)."""
    counts: dict = {}
    for fm in fms:
        tshirt = _loe(fm).get("tshirt")
        if tshirt is None:
            continue
        counts[tshirt] = counts.get(tshirt, 0) + 1
    return counts


def _opus_sum(fms: list[dict]) -> int:
    """Sum ``loe.opus_dispatches`` (//0, numbers-only) (bash:1024 / 1147)."""
    total = 0
    for fm in fms:
        val = _loe(fm).get("opus_dispatches")
        if val is None or val is False:  # jq ``// 0``
            val = 0
        if isinstance(val, bool):  # jq ``numbers`` drops booleans
            continue
        if isinstance(val, (int, float)):
            total += val
    return total


def _commit_count(fms: list[dict]) -> int:
    """Count SHA-shaped ``commits`` entries (bash:1025-1029 / 1148-1150)."""
    n = 0
    for fm in fms:
        commits = fm.get("commits")
        if not isinstance(commits, list):
            continue
        for c in commits:
            if _SHA_RE.match(str(c)):
                n += 1
    return n


def _max_commit_sha(completions: list[dict]) -> str:
    """Lexicographically-greatest valid SHA across ALL entries; "0000000" default (bash:1040-1043)."""
    shas: list[str] = []
    for entry in completions:
        commits = _fm(entry).get("commits")
        if not isinstance(commits, list):
            continue
        for c in commits:
            s = str(c)
            if _SHA_RE.match(s):
                shas.append(s)
    return max(shas) if shas else "0000000"


def _today_chains(today_fms: list[dict]) -> int:
    """Distinct chains among today's completions; null chains distinct by index (bash:1131-1140)."""
    keys: set[str] = set()
    for idx, fm in enumerate(today_fms):
        chain = fm.get("chain")
        keys.add(f"__null_{idx}" if chain is None else chain)
    return len(keys)


# --------------------------------------------------------------------------- review trail (week)
def _review_trail_facts(ctx: EmitContext) -> tuple[int, dict]:
    """Return ``(reviews_conducted, verdicts)`` from the review-trail VALID set.

    Delegates quarantine filtering to ``_validate_review_trail_file`` from ``_shared`` so
    the counted valid set matches what ``review_trail.collect`` emits: a record is valid iff
    its filename timestamp segment decodes a legal HHMMSS clock, the body is a JSON object,
    ``sha_range`` and ``reviewer`` are non-empty strings, and ``verdict`` maps through
    VERDICT_MAP. ``verdicts`` is the group_by-verdict count (bash:1073-1077).
    Delegates file listing to ``review_trail._list_review_trail_paths`` — the same
    in-process native lister SECTION 3 uses — so both sections read the identical
    live+archive union via one implementation (no bash spawn either side).
    """
    paths = _list_review_trail_paths(ctx)

    count = 0
    verdicts: dict = {}
    for filepath in paths:
        if not filepath or not os.path.isfile(filepath):
            continue

        validated, _reason = _validate_review_trail_file(filepath)
        if validated is None:
            continue  # quarantined — excluded from valid set

        count += 1
        verdict = validated["verdict"]
        verdicts[verdict] = verdicts.get(verdict, 0) + 1

    return count, verdicts


# --------------------------------------------------------------------------- collect
def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build [DayRollup, WeekRollup] (records); no malformed bucket (bash SECTION 5)."""
    completions = _query_completions(ctx)
    today = _local_day(ctx)
    iso_week = _iso_week()
    max_commit_sha = _max_commit_sha(completions)

    # --- WEEK: chain-deduped facts (bash:998-1052) ---
    deduped = _dedup_by_chain(completions)
    deduped_fms = [e["fm"] for e in deduped]
    chains_completed = sum(
        1 for e in deduped if not e["chain_key"].startswith("__null_")
    )
    # jq counts distinct NON-null chain slugs; deduped already holds one per key, so the count
    # of non-__null_ keys IS the distinct-named-chain count. null chains add one each.
    null_chains = sum(1 for e in deduped if e["chain_key"].startswith("__null_"))
    total_chains = chains_completed + null_chains

    week_reviews, week_verdicts = _review_trail_facts(ctx)
    has_today = any(_fm(c).get("created") == today for c in completions)
    week_freshness = "current" if has_today else "stale"

    week_rollup = {
        "grain": "week",
        "period": iso_week,
        "repo": ctx.repo_name,
        "coordinator_root_path": ".",
        "deterministic_facts": {
            "chains_completed": total_chains,
            "tshirt_counts": _tshirt_counts(deduped_fms),
            "opus_dispatches": _opus_sum(deduped_fms),
            "commits": _commit_count(deduped_fms),
            "reviews_conducted": week_reviews,
            "verdicts": week_verdicts,
        },
        "narrative": None,
        "input_watermark": {
            "max_observed_at": ctx.observed_at,
            "max_commit_sha": max_commit_sha,
            "source_count": len(deduped),
        },
        "freshness": week_freshness,
        "provenance": ctx.provenance(
            "coordinator_artifact", path="archive/completed", derivation="rolled_up"
        ),
    }

    # --- DAY: raw today-filtered facts, NOT deduped (bash:1127-1193) ---
    today_completions = [c for c in completions if _fm(c).get("created") == today]
    today_fms = [_fm(c) for c in today_completions]
    today_source_count = len(today_completions)
    today_freshness = "current" if today_source_count > 0 else "stale"

    day_rollup = {
        "grain": "day",
        "period": today,
        "repo": ctx.repo_name,
        "coordinator_root_path": ".",
        "deterministic_facts": {
            "chains_completed": _today_chains(today_fms),
            "tshirt_counts": _tshirt_counts(today_fms),
            "opus_dispatches": _opus_sum(today_fms),
            "commits": _commit_count(today_fms),
        },
        "narrative": None,
        "input_watermark": {
            "max_observed_at": ctx.observed_at,
            "max_commit_sha": max_commit_sha,
            "source_count": today_source_count,
        },
        "freshness": today_freshness,
        "provenance": ctx.provenance(
            "coordinator_artifact", path="archive/completed", derivation="rolled_up"
        ),
    }

    return [day_rollup, week_rollup], []
