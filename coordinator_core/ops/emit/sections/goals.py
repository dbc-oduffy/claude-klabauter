"""Section porter — Goals (envelope key: ``goals_current``).

Emits one record per distinct goal identity, keyed on ``goal_id`` (the writer's content-hash
identity — ``goal_append.py::_goal_id`` mints ``sha1(repo|coordinator_root_path|period|
period_value|text)[:12]``, so distinct goal *text* within the same (repo, root, period,
period_value) mints a distinct id). Multiple goals routinely coexist within one period — e.g.
a ceremony declaring 15 daily goals in one batch all share (repo, root, "day", "2026-06-24")
but differ in text/goal_id — and ALL of them are emitted. Supersession (re-declaring the SAME
goal text, which reproduces the SAME goal_id) still collapses to the latest record by
``declared_at`` (P1-D6 cross-machine latest-wins). Legacy rows lacking ``goal_id`` fall back to
computing the SAME deterministic id the writer would have minted for that content
(``goal_append.py::_goal_id``, identical formula), keyed on the same
``(goal_id, repo, coordinator_root_path)`` tuple as goal_id-bearing rows — not a disjoint
identity space — so a legacy row and a later goal_id-bearing row for the same logical goal
collapse as supersession instead of double-emitting across the migration boundary. Provenance
derivation is ``parsed`` for weekly (PM-declared) goals and ``rolled_up`` for all other periods
(Decision 4 / F10, bash B-F5).

Prior-bug note: before 2026-07-21 the dedup key was (repo, coordinator_root_path, period,
period_value) with NO identity component — every goal sharing a period silently collapsed to
one, discarding all but the latest-declared_at goal per period. This was verified to drop 58%
of declared goals (55 raw rows → 10 keys) against the real state tree. Keying on goal_id (or
its legacy-row fallback) is the fix; see the commit/PR that introduced this note for the
before/after record counts.

Malformed bucket: none — unparseable JSONL lines (bad JSON syntax, or syntactically valid JSON
that isn't an object) are silently skipped, and goals has no ``malformed_records`` key in the
envelope. Because there is no malformed channel for this section (the placement contract's
``"goals": {..., "malformed": []}`` row means any second-tuple-element content is discarded
unread by ``envelope.py``), a scan failure on ``central_state_root`` itself — as opposed to a
plain empty/absent directory — cannot be silently reported through that channel; see
``GoalsStateRootUnreadable`` below.

Port of: emit-cockpit-snapshot.sh (coordinator-claude 07eedcfb, 2026-07-19) — § SECTION 6, Goals.
  Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P06
"""

from __future__ import annotations

import logging

from coordinator_core.goals.wire_read import read_and_collapse
from coordinator_core.ops.emit.context import EmitContext

_LOG = logging.getLogger(__name__)


class GoalsStateRootUnreadable(Exception):
    """Raised when ``ctx.central_state_root`` itself cannot be enumerated.

    Distinct from "no goal logs found" — ``Path.glob()`` silently swallows
    ``PermissionError`` while walking (an unreadable dir yields an empty iterator, no
    exception), which would otherwise make a permission-denied root indistinguishable
    from a genuinely empty one and collapse into the zero-goals ``([], [])`` shape.
    Raised (rather than returned via the malformed bucket) because this section has no
    envelope-visible malformed channel — see module docstring.
    """


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build the goals_current records (records=[goal, ...], malformed=[]).

    Raises:
        GoalsStateRootUnreadable: ``ctx.central_state_root`` exists but cannot be listed
            (e.g. permission-denied) — probed via ``os.scandir`` before trusting
            ``glob()``. See ``GoalsStateRootUnreadable``.
    """
    # The glob+parse+latest-wins-collapse lives exactly once, in
    # coordinator_core/goals/wire_read.py — this section consumes it and keeps only
    # its emit-specific work (schema shaping, provenance stamping, the
    # GoalsStateRootUnreadable raise below).
    central_state_root = ctx.central_state_root
    result = read_and_collapse(central_state_root, default_repo=ctx.repo_name)

    # --- Tier 2 (behaviour change — PM sign-off required) ---
    # An unscannable central_state_root FAILS the emit loud (raise) rather than
    # silently degrading to the zero-goals ([], []) shape. Chosen over stamping a
    # degraded flag into the malformed bucket because this section's malformed return
    # value is discarded unread by envelope.py's placement table ("goals": {...,
    # "malformed": []}) — raising is the only channel that actually surfaces the failure.
    # This is an emit-path POLICY choice, not something the shared reader decides — the
    # reader stays policy-neutral and only reports the unreadable-root signal back.
    # Review: code-reviewer — knowingly-accepted blast-radius trade: this raise aborts
    # the WHOLE cockpit-emission.json build (all 21 sections + post-collect enrichment
    # discarded), not just goals_current, so a transient permission hiccup on this root
    # now blocks the entire artifact refresh where it previously wouldn't have.
    if result.unreadable_error is not None:
        _LOG.warning(
            "goals: cannot scan central_state_root %s — %s; goals_current would "
            "otherwise wrongly report zero goals for an unscannable root",
            central_state_root,
            result.unreadable_error,
        )
        raise GoalsStateRootUnreadable(
            f"{central_state_root}: {result.unreadable_error}"
        ) from result.unreadable_error
    # --- end Tier 2 ---

    records: list[dict] = []
    for row in result.rows:
        record, log_path = row.record, row.shard_path
        # Weekly goals are PM-declared (parsed from HEADER); daily/other goals are
        # synthesized from activity (rolled_up) — bash B-F5 / Decision 4.
        period = record.get("period", "")
        derivation = "parsed" if period == "week" else "rolled_up"

        emitted = {
            # Review: code-reviewer (Finding 1) — legacy rows (no goal_id key on the raw
            # record) fall through to the reader's own deterministic-hash fallback
            # (row.goal_id, computed by wire_read.read_and_collapse via
            # goal_append._goal_id), instead of silently emitting "". Without this, a
            # legacy row's resolved identity here would diverge from the close-out op's
            # (which reads row.goal_id directly) for the same wire row.
            "goal_id": record.get("goal_id") or row.goal_id,
            "repo": record.get("repo", ctx.repo_name),
            "coordinator_root_path": record.get("coordinator_root_path", "."),
            "period": period,
            "period_value": record.get("period_value", ""),
            "declared_by_machine": record.get("declared_by_machine", "unknown"),
            # Review: code-reviewer (Finding 7) — this defaults missing declared_at to
            # ctx.observed_at, while the dedup comparison above defaults it to "" instead.
            # Harmless to the dedup outcome (an empty string always loses a comparison
            # against a real timestamp), but the two defaults intentionally differ: ""
            # is the identity/comparison sentinel (must sort below any real value),
            # while ctx.observed_at is the best available stand-in for a genuinely
            # missing timestamp on the emitted wire record.
            "declared_at": record.get("declared_at", ctx.observed_at),
            "text": record.get("text", ""),
            "status": record.get("status", "active"),
            # parent_goal_id follows the normal D9 present-as-null rule: always emitted,
            # carrying None when the raw record has no parent (schema: nullable, not optional).
            "parent_goal_id": record.get("parent_goal_id"),
            # Provenance names the concrete shard the surviving record came from (not
            # the glob pattern used to find it) so content_hash stamping — which
            # resolves provenance.path against disk — can find a real file.
            "provenance": ctx.provenance(
                "coordinator_artifact",
                path=f"state/{log_path.name}",
                derivation=derivation,
            ),
        }

        # weekly_perceptible and key_results_status are `.optional()` (absent-when-absent),
        # NOT `.nullable()` (present-as-null) — this is a DELIBERATE EXCEPTION to the D9
        # present-as-null default, specified by the source memo for these two fields only.
        # Do NOT "correct" them toward present-as-null on a future re-vendor; the exception
        # is not derivable from D9 generally, it is schema-pinned per-field.
        if "weekly_perceptible" in record:
            emitted["weekly_perceptible"] = record["weekly_perceptible"]

        # Review: code-reviewer (Finding 1) — guard against a malformed key_results_status
        # shape (non-list, or list-of-non-dicts from producer-side JSONL drift) so a bad
        # record quarantines only this record's key_results_status, not the whole
        # collect() call (mirrors the file's per-record quarantine posture elsewhere).
        # The writer (goal_append.py) already stores the projected field under
        # ``key_results_status`` (2026-07-13 field map) — re-project here defensively
        # (not merely pass through) to defend against a non-list ``key_results_status``,
        # non-dict items, or stray extra keys, satisfying ``extra="forbid"`` on
        # ``GoalKeyResultStatus``.
        #
        # A dict item that is individually MISSING a required sub-field (e.g. ``kind``)
        # is NOT quarantined here — it is intentionally defaulted to ``""``, matching the
        # upstream producer's contract (``_flush_kr()``, Port of: emit-goal-from-artifact.sh,
        # coordinator-claude 3d785330, 2026-07-21), which emits ``{id,text,kind,status}`` via ``jq --arg``
        # with ``""`` for any individually-missing sub-field and only fail-loud-warns when
        # ALL four are empty). Quarantining
        # such items here would diverge from that producer contract and silently drop real
        # KRs whose only defect is one blank sub-field.
        key_results_status_raw = record.get("key_results_status")
        if isinstance(key_results_status_raw, list) and key_results_status_raw:
            kr_status = [
                {
                    "id": kr.get("id", ""),
                    "text": kr.get("text", ""),
                    "kind": kr.get("kind", ""),
                    "status": kr.get("status", ""),
                }
                for kr in key_results_status_raw
                if isinstance(kr, dict)
            ]
            # Review: code-reviewer (Finding 2) — deliberate choice: if every item in
            # key_results_status is malformed (non-dict), kr_status is [] and the key is
            # OMITTED (not emitted as []), preserving absent-when-absent semantics keyed
            # on "did anything survive projection", not merely "was the key present".
            if kr_status:
                emitted["key_results_status"] = kr_status

        records.append(emitted)

    return records, []
