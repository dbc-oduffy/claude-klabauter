"""Shared-path landing: a met row's product path that a live sibling holds, and the refusal naming it.

``land_run`` builds ``(row, path, reason)`` triples for every credited row path it leaves out; the
helpers here join the ``peer-held`` ones to the ``{path: [holders]}`` map and word the refusal. No
spawn. Stdlib only at module top (terminal_commit cold-import pin); readers are imported lazily.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Optional

#: ``refused`` value of a landing whose met row's product path a live sibling holds.
SHARED_PATH_SIBLING_HELD = "shared-path-sibling-held"

#: Reply key: sorted unique paths a landing left out that a credited row declared.
LEDGER_WITHHELD_PRODUCT = "ledger_withheld_product"

_PEER_HELD = "peer-held"


def released_holder(root: Path, holder: str, own_run_id: Optional[str], own_plan: Optional[str] = None) -> bool:
    """True when ``holder`` names a run record whose claim no longer holds against ``own_run_id``'s landing.

    A halted or resumed run that ``halted_hold_release.release_reason`` frees (successor, superseded or
    shipped sizing, recorded landed) is released; a holder that is not a run record (a session id) or
    whose record is unreadable (OSError or ValueError) is not."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_hold_release
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        if not (record_dir(Path(root), holder) / "halted.json").is_file():
            return False
        return halted_hold_release.release_reason(Path(root), holder, own_run_id, own_plan) is not None
    except (OSError, ValueError):  # unreadable record: the holder holds
        return False


def holds_path(
    root: Path, holder: str, path: str, own_run_id: Optional[str], own_plan: Optional[str] = None,
) -> bool:
    """False when ``holder`` is a halted run whose ``halted_hold_release.held_paths`` omits ``path``: a
    declared-but-not-withheld path no longer holds. A holder with no halt record, or a resumed one, holds."""
    try:
        from coordinator_core.ops import warp_run_deliverable
        from coordinator_core.ops.dispatch_emit import halted_hold_release

        if not warp_run_deliverable._halted(Path(root), holder):
            return True
        return str(path).replace("\\", "/") in halted_hold_release.held_paths(Path(root), holder, own_run_id, own_plan)
    except (OSError, ValueError):  # unreadable record: the holder holds
        return True


def sibling_held_product(
    unlanded: Iterable, peer_held: Mapping, *, root: Optional[Path] = None, own_session: Optional[str] = None,
    own_run_id: Optional[str] = None, own_plan: Optional[str] = None,
) -> list[tuple[str, str, list[str]]]:
    """Sorted ``(row, path, holders)`` for each ``peer-held`` unlanded triple, holders from ``peer_held``.

    A triple whose path ``peer_held`` does not name carries empty holders. A malformed triple raises:
    this gate refuses a landing, so a shape bug must surface rather than let tests land without product.

    With ``own_session`` the landing session is never its own holder; with ``root`` a holder that is a
    released run (``released_holder``) is dropped. A path whose every named holder was dropped is not held."""
    out: set[tuple[str, str, tuple[str, ...]]] = set()
    for row, path, reason in unlanded or ():
        if reason != _PEER_HELD:
            continue
        named = tuple(sorted(str(h) for h in (peer_held or {}).get(path, ()) or ()))
        holders = tuple(
            h for h in named
            if h != own_session and h != own_run_id and not (
                root is not None
                and (released_holder(root, h, own_run_id, own_plan) or not holds_path(root, h, path, own_run_id, own_plan))
            )
        )
        if named and not holders:
            continue
        out.add((str(row), str(path), holders))
    return [(row, path, list(holders)) for row, path, holders in sorted(out)]


def withheld_product_paths(unlanded: Iterable) -> list[str]:
    """Sorted unique paths of every unlanded ``(row, path, reason)`` triple.

    A malformed triple raises, as in ``sibling_held_product``: swallowing it would leave ``ledger_met``
    set over a withheld product path."""
    return sorted({str(path) for _row, path, _reason in unlanded or ()})


def _own_seniority(root: Path, run_id: str) -> Optional[tuple]:
    """``(minted_at, run_id)`` of the landing run's claimed token; None when it does not resolve."""
    from coordinator_core.ops.dispatch_emit import run_token

    record = run_token._read(run_token._record_path(Path(root), run_id, claimed=True))
    if record is not None and isinstance(record.get("minted_at"), (int, float)):
        return (record["minted_at"], run_id)
    return None


def _senior_claims(root: Path, run_id: str, paths: Iterable[str]) -> Optional[dict]:
    """``{path: [(minted_at, holder_run_id, holder_session)]}`` oldest first of live runs senior to
    ``run_id`` declaring each path, same-session runs included (one token pass). None when the contest
    or the landing run's seniority is unknown."""
    from coordinator_core.ops import warp_run_deliverable

    seniority = _own_seniority(root, run_id)
    if seniority is None:
        return None
    return warp_run_deliverable._live_claim_scan(
        Path(root), paths, "", str(root), seniority, run_id,
    )


def _minted_iso(minted_at: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(minted_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _holder_labels(holders: list[str], senior: list[tuple]) -> str:
    """Senior run holders oldest first as ``<run_id> (minted <iso>)``, then each named holder no senior
    run's session accounts for (a touch-claim session) in its session wording."""
    sessions = {sid for _m, _r, sid in senior}
    labels = [f"{run} (minted {_minted_iso(m)})" for m, run, _sid in senior]
    labels.extend(h for h in holders if h not in sessions)
    return ", ".join(labels) or "a live sibling"


def hunk_landable(
    root: Path, own_run_id: str, held: Mapping, diffs: Mapping,
) -> tuple[dict, list[str]]:
    """``(stage, still_held)``: ``stage`` is ``{path: [Hunk]}`` of own hunks that land now, ``still_held``
    the sorted paths that stay held.

    ``held`` is ``{path: [holders]}`` (``_peer_held``'s map), ``diffs`` ``own_hunk_stage.read_diff``'s
    answer. A path is stageable only when no senior holder's attributed hunk overlaps an own hunk, no
    hunk is unattributed, every named holder is a senior run or released, and the path has a diff.
    Junior runs never hold a senior. Unknown (seniority, claims, attribution) holds."""
    from coordinator_core.ops.dispatch_emit import run_hunk_claims

    root = Path(root)
    stage: dict = {}
    still: set[str] = set()
    paths = sorted(str(p) for p in held)
    claims = _senior_claims(root, own_run_id, paths)
    for path in paths:
        diff = diffs.get(path)
        if claims is None or diff is None:
            still.add(path)
            continue
        senior = claims.get(path, [])
        sessions = {sid for _m, _r, sid in senior}
        holders = [str(h) for h in (held.get(path) or ())]
        unaccounted = [
            h for h in holders
            if h not in sessions and h != own_run_id
            and not (released_holder(root, h, own_run_id) or not holds_path(root, h, path, own_run_id))
        ]
        if unaccounted:
            still.add(path)
            continue
        hunks = list(diff.hunks)
        if not senior:
            if hunks:
                stage[path] = hunks
            continue
        split = run_hunk_claims.own_hunks(root, own_run_id, path, hunks, {run: True for _m, run, _s in senior})
        if split.overlapping or split.unattributed or not split.own:
            still.add(path)
            continue
        stage[path] = list(split.own)
    return stage, sorted(still)


def refusal_message(holds: Iterable, run_id: str, root: Optional[Path] = None) -> str:
    """One register line naming each held path, its row and holder(s), then the route.

    With ``root``, each path's holders are the live runs senior to ``run_id`` (not the shared session),
    oldest first as ``held by <run_id> (minted <iso>)``; a touch-claim session keeps its session wording.
    Route one: wait for the holder to land, then re-fire (the run's refire command when ``root``
    resolves its sizing). Route two: leave the paths out via ``peer_claim_contract.WITHHOLD_CALL``."""
    from coordinator_core.ops.dispatch_emit import peer_claim_contract

    holds = sorted(holds)
    claims: dict = {}
    if root is not None:
        try:
            claims = _senior_claims(Path(root), run_id, {p for _r, p, _h in holds}) or {}
        except Exception:  # noqa: BLE001 -- unresolved runs: the session wording stands
            claims = {}
    named = "; ".join(
        f"{path} (row {row}) held by "
        + (_holder_labels(list(holders), claims[path]) if claims.get(path) else ", ".join(holders) or "a live sibling")
        for row, path, holders in holds
    )
    wait = "Wait for the holder to land, then re-fire."
    sizing = ""
    if root is not None:
        try:
            from coordinator_core.ops import warp_run_deliverable

            sizing = warp_run_deliverable.record_sizing_rel(Path(root), run_id)
        except Exception:  # noqa: BLE001 -- no resolvable sizing: the generic wait route stands
            sizing = ""
    if sizing:
        from coordinator_core.ops.dispatch_emit import sibling_claims

        wait = f"Wait for the holder to land, then {sibling_claims.reland_remedy(sizing)}"
    withhold = peer_claim_contract.WITHHOLD_CALL.format(run_id=run_id)
    return (
        f"{SHARED_PATH_SIBLING_HELD}: {named}. A met row's product path lands with its tests or not at all. "
        f"{wait} Or leave them out: {withhold}"
    )
