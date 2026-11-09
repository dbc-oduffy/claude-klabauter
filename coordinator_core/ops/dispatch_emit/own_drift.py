"""Run-owned blob-drift actuals: which drifted paths hold bytes the run itself left. Hash-only, no spawn."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence


def _own_drift_actuals(
    worktree_root: Path, run_dir: Optional[Path], drift: Sequence, declared: Sequence[str],
    session_id: Optional[str], review_writes: Optional[dict], reviewed: Optional[dict] = None,
    record_dir: Optional[Path] = None, run_id: Optional[str] = None,
) -> dict:
    """``{path: actual}`` for each content-sha256 entry of a commit_v2 ``blob_drift`` list whose
    current bytes are the bytes the run's review pass read (``reviewed``, ``{path: sha256}`` of the
    stored review prep: the reviewed bytes are the ones that land, whoever wrote them) or are the
    run's own last recorded write (``drift_attribution.classify``: the run
    session's and the fix-pass agents' touch ledgers, not bounded by the run record's start, so a
    refire keeps the run's earlier own write). The ledgers read are the session's, every review
    writer's and every agent the session dispatched (the reverify, verify and evidence legs write
    under types the review-writer set omits). A path whose bytes are a blob the run's own record
    dir holds (``record_dir``: ``kept.json``, ``measured-rev.json``) is owned too: an executor's
    Write/Edit reaches no ledger, and these are the bytes its chunk left. Those are the bytes the
    run left; a foreign or unrecorded rewrite is absent and keeps refusing. When ``reviewed`` is
    empty or older than the drifted bytes (the commit's prep lookup requires the prep to cover every
    declared path, which a delivery note or sizing record defeats) the run's stored review prep read
    by ``run_id`` alone is credited too: either read owns the bytes.
    Hash-only, no spawn."""
    from coordinator_core.ops.dispatch_emit import drift_attribution
    from coordinator_core.ops.dispatch_emit.terminal_commit_review import REVIEW_RECORD_AGENT

    actual = {
        d["path"]: d["actual"] for d in drift
        if isinstance(d, dict) and d.get("basis") == "content-sha256"
        and isinstance(d.get("path"), str) and isinstance(d.get("actual"), str) and d["actual"]
    }
    if not actual:
        return {}
    commit_read = reviewed if isinstance(reviewed, dict) else {}
    read_equal = {p: a for p, a in actual.items() if commit_read.get(drift_attribution._norm(p)) == a}
    if len(read_equal) < len(actual):
        # The run-keyed prep is read only for paths the commit's own prep read did not settle.
        run_read = drift_attribution.run_reviewed_hashes(worktree_root, session_id, run_id)
        read_equal.update({
            p: a for p, a in actual.items() if run_read.get(drift_attribution._norm(p)) == a
        })
    actual = {p: a for p, a in actual.items() if p not in read_equal}
    if not actual:
        return read_equal
    owned = drift_attribution.classify(
        worktree_root, run_dir if run_dir is not None else worktree_root, list(actual), declared,
        session_id=session_id,
        agent_ids=sorted(
            {a for a in (review_writes or {}).values() if a and a != REVIEW_RECORD_AGENT}
            | set(drift_attribution.session_agent_ids(worktree_root, session_id))
        ),
        own_blobs=drift_attribution.run_recorded_blobs(record_dir, list(actual)),
        run_started_at=0.0,
    )[drift_attribution.RUN_OWNED]
    return {**read_equal, **{p: actual[p] for p in owned}}


def _own_record_dir(worktree_root: Path, art_run_id: object, ask_run_dir: Optional[Path]) -> Optional[Path]:
    """The run's record directory, or None: ``ask_run_dir`` when the manifest named one, else
    ``ask_contract.record_dir`` for a safe run id."""
    if ask_run_dir is not None:
        return ask_run_dir
    if not isinstance(art_run_id, str) or not art_run_id:
        return None
    from coordinator_core.ops.dispatch_emit import ask_contract

    try:
        return ask_contract.record_dir(worktree_root, art_run_id)
    except ValueError:
        return None
