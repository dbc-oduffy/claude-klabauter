"""
coordinator_core.ops.emit.backlog_history — backlog-history block assembly (C5).

Purpose: aggregate latest-per-(repo,date) across all per-machine backlog-snapshot JSONL
shards (written by recorder.py) into the snake_case ``backlog_history`` envelope block
that opticon consumes.

Block shape (snake_case — DoE+PM convention call + Zolí review; contract v2.7.0 landed
the block as ``backlog_history``; cross-repo memo 2026-07-06-backlog-history-landed-v270-makima.md):
    {
        "generated_at": "<ISO datetime>",    # null in D9 default
        "series": [                           # [] in D9 default
            {
                "repo": "owner/repo",
                "points": [
                    {"date": "YYYY-MM-DD", "bug": N, "improvement": N, "lessons": N}
                ]
            }
        ],
        "provenance": { ... }                 # NON-null even in D9 default (see D6)
    }

D9 default (contract-gated hold or no shard data): when the vendored contract does NOT
declare a concrete ``backlog_history`` block, or when no shard data is found, the block
uses the D9 empty-series shape:
    {"generated_at": null, "series": [], "provenance": <non-null envelope>}

The D9 hold is a schema-declaration-level guard, gated on contract-presence
(``validate.contract_declares_backlog_history()``) rather than on any specific version
sentinel. The probe rejects a ``{"anyOf":[{},{"type":"null"}]}`` forward-declaration
placeholder, so a not-yet-concrete schema cannot prematurely self-activate the block. This
self-activates at the re-vendor that lands a concrete ``backlog_history`` schema — whatever
version the coordinator assigns — without hardcoding a version number. There is no runtime
``cockpit-revendor-pending-*`` master sentinel enforcing a bilateral hold at emit time: the
producer-side runtime emit-hold has been removed (reader-first is a consumer responsibility,
not a producer gate); this module emits freely and the gate above is the only thing standing
between "block absent from the contract" and "block populated."

opticon's store has a ``provenance_fk NOT NULL`` constraint (source memo line 60), so the
``provenance`` object MUST be fully populated (non-null) even in the D9 path — emitting
null provenance would strand opticon's FK.

This module does not touch ``schema_version``; it only populates the ``backlog_history``
block per the contract-presence gate above.

Aggregation (latest-per-(repo,date)): all shard files matching
``backlog-snapshots.*.jsonl`` are read in sorted filename order; for each (repo, date) pair
the last-seen row wins (append-only shards — later rows are newer). Malformed lines are
silently skipped; a shard that cannot be read is silently skipped (graceful-degrade).

Spec backlink: pln-backloghistory-emit-gate-decou-22d451 § Design decision → Option C
Amends: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § D6 (v2.5.0 sentinel → contract-presence gate)
Amends: docs/plans/2026-07-08-producer-emit-hold-removal-reader-first-consumer-owned.md (producer-side runtime emit-hold removed; contract-presence self-activation gate is unaffected and stays)
Parity oracle: n/a (new block — no bash equivalent; D5 defines the shape).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ops.emit import validate
from coordinator_core.ops.emit.context import EmitContext

_LOG = logging.getLogger(__name__)

# Shard glob pattern — matches recorder.py's _SHARD_NAME_TEMPLATE.
_SHARD_GLOB = "backlog-snapshots.*.jsonl"


class ShardRootUnreadable(Exception):
    """Raised when ``central_state_root`` itself cannot be enumerated.

    Distinct from "no shards found" — ``Path.glob()`` silently swallows
    ``PermissionError`` while walking (verified: an unreadable dir yields an empty
    iterator, no exception), so a permission-denied root would otherwise be
    indistinguishable from a genuinely empty one and collapse into the D9 empty-series
    shape as if it were "no shard data" rather than "could not check for shard data".
    """


def _read_shards(central_state_root: Path) -> dict[tuple[str, str], dict]:
    """Read all machine shards and return the latest-per-(repo, date) map.

    Each JSONL line: {"repo":"owner/repo","date":"YYYY-MM-DD","bug":N,"improvement":N,"lessons":N}

    Processing order: shards are sorted by filename (deterministic across machines).
    Within each shard, lines are read top-to-bottom (append-order = time-order). A later
    line for the same (repo, date) overwrites an earlier one — last-wins semantics.
    Across shards, ordering is lexicographic by filename (``backlog-snapshots.<machine>.jsonl``);
    if two machines recorded the same (repo, date), the alphabetically-later shard's last
    entry wins. This is consistent and reproducible.

    Malformed lines (invalid JSON, missing repo/date keys) are silently skipped.
    OSError on a shard is silently skipped (graceful-absent, never abort).

    Raises:
        ShardRootUnreadable: ``central_state_root`` exists but cannot be listed (e.g.
            permission-denied) — probed via ``os.scandir`` before trusting ``glob()``,
            which would otherwise silently return an empty iterator for the same failure.
    """
    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # An unscannable central_state_root now FAILS the emit loud (raise) rather than
    # silently degrading to the D9 empty-series shape. Chosen over a degraded-flag field
    # because BacklogHistory is `extra="forbid"` (frozen cross-repo contract) — there is
    # no room to add a flag without a contract change, so raising is the only channel
    # left to distinguish "no shard data" from "could not check for shard data".
    # Review: code-reviewer — knowingly-accepted blast-radius trade: this raise aborts
    # the WHOLE cockpit-emission.json build (all 21 sections + post-collect enrichment),
    # not just this block, so a transient permission hiccup here now blocks the entire
    # artifact refresh where it previously wouldn't have.
    if central_state_root.is_dir():
        try:
            with os.scandir(central_state_root) as it:
                next(iter(it), None)
        except OSError as exc:
            _LOG.warning(
                "backlog_history: cannot scan central_state_root %s — %s; "
                "shard data may be missing (not the same as no shards present)",
                central_state_root,
                exc,
            )
            raise ShardRootUnreadable(f"{central_state_root}: {exc}") from exc
    # --- end Tier 2 ---

    latest: dict[tuple[str, str], dict] = {}
    for shard in sorted(central_state_root.glob(_SHARD_GLOB)):
        try:
            text = shard.read_text(encoding="utf-8")
        except OSError:
            # Graceful-absent per the docstring contract above -- never abort the merge.
            continue
        for raw_line in text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                # Malformed line, silently skipped per the docstring contract above.
                continue
            if not isinstance(row, dict):
                continue
            repo = row.get("repo")
            date = row.get("date")
            if not repo or not date or not isinstance(repo, str) or not isinstance(date, str):
                continue
            latest[(repo, date)] = row
    return latest


def _build_series(latest: dict[tuple[str, str], dict]) -> list[dict]:
    """Convert the latest-per-(repo,date) map to the ``series`` list.

    Groups points by repo; sorts each repo's points by date ascending; returns repos in
    ascending alphabetical order. The ``series`` and ``points`` inner keys are already plain
    snake/plain shape — no renaming needed. Only the block-level ``generated_at`` and the
    envelope block key ``backlog_history`` are snake_case (contract v2.7.0, 2026-07-06 memo).
    """
    def _safe_int(val: object) -> int:
        """Return int(val or 0), or 0 on ValueError/TypeError (graceful-absent posture).

        Shard rows are append-only writes from recorder.py and always write integer counts,
        but defensive: if a future format change or manual edit writes a non-integer value
        (e.g. "N/A"), propagating ValueError through collect() would abort the whole block.
        Consistent with the 'malformed lines silently skipped' guarantee in _read_shards.
        """
        try:
            return int(val or 0)
        except (ValueError, TypeError):
            return 0

    by_repo: dict[str, list[dict]] = {}
    for (repo, date), row in latest.items():
        point = {
            "date": date,
            "bug": _safe_int(row.get("bug")),
            "improvement": _safe_int(row.get("improvement")),
            "lessons": _safe_int(row.get("lessons")),
        }
        by_repo.setdefault(repo, []).append(point)

    series: list[dict] = []
    for repo in sorted(by_repo):
        points = sorted(by_repo[repo], key=lambda p: p["date"])
        series.append({"repo": repo, "points": points})
    return series


def collect(ctx: EmitContext) -> dict:
    """Build the snake_case ``backlog_history`` block for the envelope.

    Returns the D9 default shape (``generated_at=null``, ``series=[]``) when:
      - the vendored contract does NOT declare a concrete ``backlog_history`` block
        (``validate.contract_declares_backlog_history()`` → False; current 2.5.0 state), OR
      - ``ctx.central_state_root`` is enumerable and simply has no shard files in it.

    Raises:
        ShardRootUnreadable: ``ctx.central_state_root`` exists but cannot be listed (e.g.
            permission-denied). This is deliberately NOT degraded to the D9 empty-series
            shape — the ``BacklogHistory`` pydantic model is ``extra="forbid"`` (no room
            for a degraded-flag field without a contract change), so a scan failure here
            fails the emit loud rather than silently reporting "no shard data" for what
            may actually be data this machine could not see.

    The block-level ``provenance`` is ALWAYS non-null — opticon's store has a
    ``provenance_fk NOT NULL`` constraint; emitting ``provenance=null`` would strand the FK.

    Returns:
        A dict with snake_case keys ``generated_at`` (str|None), ``series`` (list),
        ``provenance`` (dict, non-null). Block key and timestamp key are snake_case per
        contract v2.7.0 (DoE+PM convention call + Zolí review; 2026-07-06 cross-repo memo).
    """
    # Block-level provenance — populated regardless of D9 state (provenance_fk NOT NULL, D6).
    provenance = ctx.provenance(
        source_kind="local_fs",
        path="",
        derivation="parsed",
    )

    # Contract-presence gate — D9 hold until the vendored contract declares the block (Option C).
    # The probe returns False for the current 2.5.0 bundle (backlog_history absent).
    # Self-activates when the re-vendored bundle (≥2.7.0) first declares the block.
    if not validate.contract_declares_backlog_history():
        return {
            "generated_at": None,
            "series": [],
            "provenance": provenance,
        }

    latest = _read_shards(ctx.central_state_root)
    if not latest:
        # No shard data on this machine — D9 default; provenance still non-null.
        return {
            "generated_at": None,
            "series": [],
            "provenance": provenance,
        }

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    series = _build_series(latest)
    return {
        "generated_at": generated_at,
        "series": series,
        "provenance": provenance,
    }
