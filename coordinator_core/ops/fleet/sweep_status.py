"""
coordinator_core.ops.fleet.sweep_status — fleet.archive_sweep_status READ-ONLY op.

Purpose: make the archival-sweep receipt (`_sweep_receipt.py`) OBSERVABLE — the
missing half of AC-3, state/handoffs/2026-08-25_roadmap-archival-sweeps-03.md:
"A failed sweep is observable. The operator learns that archival did not
happen, by some artifact — not by noticing a cluttered directory later."
`_sweep_receipt.record_sweep_outcome` writes that artifact; nothing reads it
until this module. A receipt nobody surfaces is the same silence the baton
exists to end.

WHAT THIS OP COMPUTES: the last row per `sweep` key (current state), plus
enough history to answer "has this been failing?" — a trailing
`consecutive_failures` count (unbroken run of `outcome == "failed"` rows at
the tail, most-recent-first) and `last_success_at` (the timestamp of the most
recent row whose outcome is `applied` or `nothing-to-do` — the two outcomes
that mean the sweep actually ran to completion; `skipped-*` outcomes are
neither success nor failure and do not reset or extend a failure streak).

SILENCE WHEN HEALTHY IS A REQUIREMENT (module docstring, not a nicety — see
`_sweep_receipt.py`'s own docstring on the 2026-07-23 detached-CLI silence
this exists to end, and the parallel risk of a banner nobody reads because it
always prints). This op does not decide what a caller prints — it shapes the
return so that decision is a single flat check: `unhealthy_sweeps` is empty
iff every known sweep's last recorded outcome was not `failed`. A caller
renders one line per entry in `unhealthy_sweeps` and, when that list is
empty, prints nothing at all. `healthy` is the same fact spelled as a bool
for a caller that only wants the gate, not the detail.

Spec backlink: state/handoffs/2026-08-25_roadmap-archival-sweeps-03.md § AC-3.
Sibling artifact: coordinator_core/ops/fleet/_sweep_receipt.py (committed b8795931a).

Negative-spec:
  - Does NOT write anything, ever — pure read of an existing file. Mirrors
    `records_query.py`'s read-only op posture (no side effects, no index, no
    cache).
  - Does NOT spawn git, or any subprocess — the file is read directly via
    `open()`/`read()`; `repo_root` (the handler's engine-supplied common dir,
    matching `archive_terminal_handoffs._handler`'s own
    `_OP_KEY_SCOPE="common_dir"` convention) is used to compute the receipt
    path via `_sweep_receipt.receipt_path`, never by invoking git.
  - Does NOT raise for a missing, unreadable, or malformed receipt — degrades
    to `{"sweeps": [], "unhealthy_sweeps": [], "healthy": True}` in every such
    case (a missing receipt is the NORMAL state on a fresh checkout, not an
    error). A malformed INDIVIDUAL line is skipped, not fatal to the rows
    around it — one line-level `json.loads` failure must not blind this op
    to every other sweep's history.
  - Does NOT rotate, prune, or mutate the receipt file — `_sweep_receipt.py`
    owns that (its own `_MAX_BYTES`/`_KEEP_BYTES` truncation), so the file
    this op reads is already bounded (<= `_sweep_receipt._MAX_BYTES` bytes);
    this op reads it whole, no separate tail-seek needed.
  - Does NOT classify a `skipped-gated`/`skipped-contended` outcome as either
    a success or a failure — those mean "did not run this time", not
    "ran and failed" nor "ran and succeeded"; they neither extend nor reset
    `consecutive_failures`, and folding them into either bucket would either
    mask a real failure streak (folding into success) or manufacture a false
    one out of routine single-flight contention (folding into failure).
  - Does NOT accept a `dry_run` param or any other write-mode toggle — this
    op has exactly one mode, matching `memo.list`'s own "no act mode" posture
    for a pure-read op, but WITHOUT that op's `build_dry_run_result`
    candidates/acted/skipped/failed envelope: that shape is a sweep-preview
    contract (`_common.py` §2.1) this op is not part of — a plain
    `{"sweeps": [...], ...}` dict, matching `records_query.py`'s own
    `{"records": [...]}` return-shape convention for a non-fleet-sweep read
    op, is the correct fit here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet import _sweep_receipt

# Outcomes that mean the sweep actually ran to completion — used both to
# stamp `last_success_at` and to STOP a trailing failure-streak count (any
# outcome other than "failed" breaks the streak; these are simply the two
# outcomes additionally eligible to set `last_success_at`).
_SUCCESS_OUTCOMES = frozenset({"applied", "nothing-to-do"})


def _read_rows(path: Path) -> list:
    """Read every parseable JSONL row from `path`, oldest-first.

    Never raises: an absent file, an unreadable file, and any individual
    unparseable line all degrade to "skip it" rather than aborting the whole
    read — see module negative-spec.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        if not isinstance(row.get("sweep"), str) or not isinstance(row.get("outcome"), str):
            continue
        rows.append(row)
    return rows


def _summarize(rows: list) -> list:
    """Reduce oldest-first `rows` to one summary dict per `sweep` key.

    `rows` are assumed to already be in the file's on-disk (append) order —
    oldest first — since `_sweep_receipt.record_sweep_outcome` only ever
    appends. Per-sweep state is folded in that same order so "last" naturally
    means "most recently appended", with no separate sort step.
    """
    by_sweep: dict = {}
    order: list = []

    for row in rows:
        sweep = row["sweep"]
        outcome = row["outcome"]
        at = row.get("at")

        if sweep not in by_sweep:
            order.append(sweep)
            by_sweep[sweep] = {
                "sweep": sweep,
                "last_outcome": None,
                "last_at": None,
                "last_detail": None,
                "last_success_at": None,
                "consecutive_failures": 0,
            }
        entry = by_sweep[sweep]

        entry["last_outcome"] = outcome
        entry["last_at"] = at
        entry["last_detail"] = row.get("detail")

        if outcome == "failed":
            entry["consecutive_failures"] += 1
        else:
            entry["consecutive_failures"] = 0
            if outcome in _SUCCESS_OUTCOMES and at:
                entry["last_success_at"] = at

    summaries = []
    for sweep in order:
        entry = by_sweep[sweep]
        entry["unhealthy"] = entry["last_outcome"] == "failed"
        summaries.append(entry)
    return summaries


@register_op("fleet.archive_sweep_status")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'fleet.archive_sweep_status' handler — read-only.

    Params: none. `params` is accepted (standard handler signature) but
    unused — this op has no filter/mode surface today; a caller wanting one
    sweep's status reads `sweeps`/`unhealthy_sweeps` and filters client-side.

    `repo_root` is the engine-supplied git common dir (this op is registered
    with the same `_OP_KEY_SCOPE="common_dir"` convention as
    `fleet.archive_completed_handoffs` — see `archive_terminal_handoffs
    ._handler`'s own docstring), used only to compute
    `_sweep_receipt.receipt_path`. `repo_root is None` degrades to the same
    empty-and-healthy answer as a missing file (never raises) — a caller
    invoking this op with no resolvable repo gets "no history" rather than an
    error, matching the "missing receipt is the normal state" negative-spec.

    Returns:
        {
          "exit_code": 0,
          "sweeps": [ {sweep, last_outcome, last_at, last_detail,
                       last_success_at, consecutive_failures, unhealthy}, ... ],
          "unhealthy_sweeps": [ same dicts, filtered to unhealthy==True ],
          "healthy": bool,  # True iff unhealthy_sweeps is empty
        }
        Always `exit_code: 0` — there is no failure mode a caller must
        distinguish (see module negative-spec: every degraded case still
        answers with an empty, healthy result).
    """
    sweeps: list = []
    if repo_root is not None:
        path = _sweep_receipt.receipt_path(Path(repo_root))
        rows = _read_rows(path)
        sweeps = _summarize(rows)

    unhealthy_sweeps = [s for s in sweeps if s["unhealthy"]]

    return {
        "exit_code": 0,
        "sweeps": sweeps,
        "unhealthy_sweeps": unhealthy_sweeps,
        "healthy": not unhealthy_sweeps,
    }
