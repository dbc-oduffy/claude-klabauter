"""Foreign owners of the hunks a halt eviction withholds: attribute, record and notify.

`live_holders` names the other runs declaring a path (the resolver `checkpoint_own_blobs` feeds to
`run_hunk_claims.own_hunks`); `foreign_owners` names, for the hunks the eviction drops from the tree,
the other claimant whose baseline delta also explains each; `notify_owners` writes one peer notice per
owner session. Advisory throughout: nothing here denies, and nothing raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

FOREIGN_KEY = "foreign"
NON_RUN_OWNER = "non-run edit"


def _norm(path: object) -> str:
    return str(path or "").replace("\\", "/")


def live_holders(root: Path, run_id: Optional[str], rel: str) -> dict[str, bool]:
    """`{other run id: senior}` for each other run declaring `rel`, through
    `peer_hunk_gate.co_declared_peer_holds` with every holder senior, as `checkpoint_own_blobs` does.
    Empty when `run_id` is unknown or the read fails (the pre-attribution behaviour)."""
    if not run_id:
        return {}
    try:
        from coordinator_core.ops.dispatch_emit import peer_hunk_gate

        pairs = peer_hunk_gate.co_declared_peer_holds(Path(root), run_id, [rel], [rel])
        return {sibling: True for path, sibling in pairs if _norm(path) == _norm(rel) and sibling != run_id}
    except Exception:  # noqa: BLE001 -- holders unknown: attribution stays the run's
        return {}


def _hunk_span(hunk: Any) -> tuple[int, int]:
    from coordinator_core.ops.dispatch_emit.run_hunk_claims import _new_span

    return _new_span(hunk)


def _explains(spans: Optional[Sequence[tuple[int, int]]], hunk: Any) -> bool:
    lo, hi = _hunk_span(hunk)
    return bool(spans) and any(a <= hi and lo <= b for a, b in spans or ())


def _claimant_ids(root: Path, run_id: str, rel: str, holders: Mapping[str, bool]) -> list[str]:
    """Runs other than `run_id` that may own a hunk of `rel`: `holders` plus each unlanded run whose
    write print names `rel`, less the run's own earlier attempts and holders that left nothing."""
    from coordinator_core.ops.dispatch_emit import run_hunk_claims, run_write_prints, sibling_claims

    ids = {h for h in holders if h != run_id}
    for other, _stamp in sibling_claims._stamped_candidates(root):
        if other != run_id and other not in ids and rel in (run_write_prints.read(root, other) or {}):
            ids.add(other)
    pool = {i: True for i in ids}
    ids -= run_hunk_claims.own_lineage(root, run_id, pool)
    ids -= run_hunk_claims._released_holders(root, run_id, rel, {i: True for i in ids})
    return sorted(ids)


def foreign_owners(
    root: Path, run_id: str, rel: str, dropped: Sequence[object], holders: Mapping[str, bool],
) -> dict[str, list[str]]:
    """`{owner: [hunk labels]}` over `dropped`, the hunks the eviction removes from the tree.

    A dropped hunk is foreign when another claimant's baseline delta also explains it: a run in
    `holders` or an unlanded run whose write print names `rel` (`run_hunk_claims._claimant_delta`);
    else, when the tree differs from the run's own write print there, `NON_RUN_OWNER`. A hunk several
    runs explain is listed under each. Hunks outside `dropped` stay in the tree and are never listed.
    Empty when nothing is foreign or the read fails; never raises."""
    hunks = list(dropped)
    if not hunks:
        return {}
    try:
        from coordinator_core.git.checkpoint_hunks import _hunk_label
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.ops.dispatch_emit import run_hunk_claims

        root = Path(root)
        try:
            tree = (root / rel).read_bytes().splitlines(keepends=True)
        except FileNotFoundError:
            tree = []
        common = resolve_git_common_dir(root)
        deltas = {
            c: run_hunk_claims._claimant_delta(root, common, c, rel, tree, coarse_ok=True)
            for c in _claimant_ids(root, run_id, rel, holders)
        }
        print_spans = run_hunk_claims._foreign_spans(root, common, run_id, rel, tree)
        out: dict[str, list[str]] = {}
        for hunk in hunks:
            owners = [c for c, spans in deltas.items() if _explains(spans, hunk)]
            if not owners and _explains(print_spans, hunk):
                owners = [NON_RUN_OWNER]
            for owner in owners:
                out.setdefault(owner, []).append(_hunk_label(hunk))
        return out
    except Exception:  # noqa: BLE001 -- advisory: an unreadable claimant names no owner
        return {}


def session_of(root: Path, owner: str) -> Optional[str]:
    """The session recorded on run `owner`'s stage record, else None (also for `NON_RUN_OWNER`)."""
    if owner == NON_RUN_OWNER:
        return None
    try:
        from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage

        sid = (read_run_stage(Path(root), owner) or {}).get("session_id")
    except Exception:  # noqa: BLE001 -- unresolvable owner
        return None
    return sid if isinstance(sid, str) and sid else None


def foreign_entry(root: Path, owners: Mapping[str, Sequence[str]]) -> dict[str, dict]:
    """The `FOREIGN_KEY` value of an `evicted.json` entry: `{owner: {"session": sid | None, "hunks": [labels]}}`."""
    return {o: {"session": session_of(root, o), "hunks": list(labels)} for o, labels in owners.items()}


def notify_owners(
    root: Path, record_dir_rel: str, path: str, owners: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    """Write one `peer_notice.send` file per owner session; `{owner: notice path | reason}`.

    The owner's session comes from its run's stage record; an owner without one is named with the
    reason and nothing is written. Owners sharing a session share one notice. Never raises."""
    out: dict[str, str] = {}
    sent: dict[str, str] = {}
    halted = Path(str(record_dir_rel).replace("\\", "/")).name
    message = (
        f"halt of {halted} withheld your hunks in {path}; "
        f'restore: dispatch.restore_foreign_hunks {{"record_dir":"{_norm(record_dir_rel)}"}}'
    )
    for owner in owners:
        sid = session_of(root, owner)
        if not sid:
            out[owner] = f"no session resolvable for {owner}"
            continue
        if sid in sent:
            out[owner] = sent[sid]
            continue
        try:
            from coordinator_core.ops.peer_notice_send import _peer_notice_send

            res = _peer_notice_send(
                {"target_session_id": sid, "artifact_path": _norm(path), "message": message}, Path(root),
            )
            sent[sid] = out[owner] = str(res.get("notice_path"))
        except Exception as exc:  # noqa: BLE001 -- advisory: a failed notice is reported, not raised
            out[owner] = f"notice failed: {type(exc).__name__}: {exc}"[:200]
    return out
