"""Kept holds of ``dispatch.land_credited``: the credited paths the landing leaves out, each named with its reason.

Implements requirements (e) and (f) of the measured plan (docs/research/2026-10-31-land-run-requirements-kept-or-dropped.md):
never-commit, sibling-repo, EM-withheld and live-peer-claimed paths, and paths a still-halted run withholds.
A hold is provenance (DR-474): the path is named in ``withheld`` and the landing proceeds; nothing here refuses.

Cost is bounded by the credited paths and the matching halted runs, never by the halted-run population: the
halted-run read is one listing of the halt-withheld index plus one marker read per run holding a credited path.
No subprocess, no git, no pack open. Every read failure answers no hold for that source.

The halted hold is index-form and name-only: a path indexed against a still-halted run is withheld even when it
already equals HEAD (the incumbent frees it with a spawn). Accepted under DR-474 -- the over-withheld path has
nothing to commit, and the name is provenance. The index is not swept here; cost tracks the index, not the
in-window run count.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Iterable

from coordinator_core.ops.dispatch_emit.land_credited_contract import LandCreditedRequest

_LOG = logging.getLogger(__name__)

#: ``withheld`` reason texts; ``halted:`` and ``peer-held:`` carry the holder ids.
REASON_NEVER_COMMIT = "never-commit"
REASON_SIBLING_REPO = "sibling-repo"
REASON_EM_WITHHELD = "withheld"
REASON_HALTED_PREFIX = "withheld by "
REASON_PEER_PREFIX = "peer-held by "


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def safe_rel(path: str) -> str:
    """``path`` with forward slashes; ValueError unless it is a repo-relative file path."""
    rel = _norm(path)
    parts = rel.split("/")
    if not rel or rel.startswith("/") or ":" in parts[0] or ".." in parts or rel.endswith("/"):
        raise ValueError(f"credited path {path!r} is not a repo-relative file path")
    return rel


def marker_paths(root: Path, script_path: str, rel: Callable[[str], str] = _norm) -> list[str] | None:
    """Sorted paths the commit-request marker of ``script_path`` credits; None when the script carries no marker.

    The marker sits inline in the script or one hop away through the ask-run manifest's ``marker_path``.
    Every repo-relative path read passes through ``rel``; read and parse failures propagate to the caller.
    """
    from coordinator_core.ops.dispatch_emit.commit_request import parse_manifest_marker, parse_marker

    root = Path(root)
    text = (root / rel(script_path)).read_text(encoding="utf-8")
    manifest_rel = parse_manifest_marker(text)
    if manifest_rel is not None:
        manifest = json.loads((root / rel(manifest_rel)).read_text(encoding="utf-8"))
        text = (root / rel(str(manifest["marker_path"]))).read_text(encoding="utf-8")
    marker = parse_marker(text)
    if marker is None:
        return None
    return sorted({rel(p) for chunk in marker.chunks for p in chunk.paths if p})


def credited_paths(root: Path, request: LandCreditedRequest) -> list[str]:
    """Sorted repo-relative paths the run's marker credits; [] when the script or marker is unreadable or unsafe."""
    from coordinator_core.ops.dispatch_emit.commit_request import MalformedCommitRequestError

    try:
        return marker_paths(root, request.script_path, safe_rel) or []
    except (OSError, ValueError, KeyError, TypeError, MalformedCommitRequestError):
        return []


def _em_withheld(root: Path, run_id: str) -> dict[str, str]:
    from coordinator_core.ops.dispatch_emit import run_withhold
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        return run_withhold.read([record_dir(root, run_id)])
    except (OSError, ValueError):
        return {}


def _halted_holders(root: Path, run_id: str, wanted: frozenset) -> dict[str, str]:
    """``{path: halted run id}`` for each wanted path another still-halted run withholds, from the halt-withheld index."""
    from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    out: dict[str, str] = {}
    try:
        runs = halted_withheld_index.entries(root)
    except (OSError, ValueError):
        return out
    for rid in sorted(runs):
        if rid == run_id:
            continue
        mine = wanted.intersection(runs[rid].paths)
        if not mine:
            continue
        try:
            marker = halted_run._read(record_dir(root, rid))
        except (OSError, ValueError):
            continue
        if not marker or marker.get("status") != halted_run.STATUS_HALTED:
            continue
        for path in mine:
            out[path] = rid
    return out


#: Longest the live-claim read waits on the claim index before it answers no hold; sized inside the "holds" leg budget.
CLAIM_READ_WAIT_SECS = 0.08


def _own_sessions(root: Path, run_id: str) -> set[str]:
    """Sessions that are this landing's own hand: the caller's session and the session on the run's token."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.session import core as session_core

    own: set[str] = set()
    try:
        sid = session_core.resolve_session_id(str(root))
        if sid:
            own.add(sid)
    except Exception:  # noqa: BLE001 -- an unresolved caller holds nothing of its own
        pass
    for claimed in (True, False):
        try:
            record = run_token._read(run_token._record_path(root, run_id, claimed=claimed))
        except Exception:  # noqa: BLE001 -- an unreadable token names no extra self session
            continue
        sid = record.get("session_id") if isinstance(record, dict) else None
        if isinstance(sid, str) and sid:
            own.add(sid)
    return own


def _bounded_lookup(claim_index, paths: list[str], root: Path):
    """``claim_index.lookup`` answered within CLAIM_READ_WAIT_SECS, else None (the fold lock is held elsewhere)."""
    import threading

    box: list = []

    def _run() -> None:
        try:
            box.append(claim_index.lookup(list(paths), cwd=str(root)))
        except Exception:  # noqa: BLE001 -- a failed lookup answers no hold
            pass

    worker = threading.Thread(target=_run, name="land-credited-claim-read", daemon=True)
    worker.start()
    worker.join(CLAIM_READ_WAIT_SECS)
    if not box:
        _LOG.warning("land_credited: claim read not answered in %.2fs; no peer hold taken", CLAIM_READ_WAIT_SECS)
        return None
    return box[0]


def _peer_claims(root: Path, paths: list[str], run_id: str = "") -> dict[str, list[str]]:
    """``{path: [live peer session ids]}`` whose touch claim blocks a peer commit; {} on any failure or slow read.

    Advisory hold: the claim read is bounded and a timeout holds nothing. The caller's session and the run's
    token session are self, never a peer, for the run's own credited paths."""
    held: dict[str, list[str]] = {}
    if not paths:
        return held
    try:
        from coordinator_core.session import claim_index, liveness, touch_record

        own = _own_sessions(root, run_id)
        found = _bounded_lookup(claim_index, paths, root)
        if found is None:
            return {}
        for path in paths:
            claimants = found.get(path, [])
            if claim_index.UNANSWERABLE in claimants:
                continue
            kinds = (found.recorded_kind or {}).get(path, {})
            for sid in claimants:
                if sid in own or not touch_record.kind_blocks_a_peer_commit(kinds.get(sid)):
                    continue
                try:
                    if liveness.session_live(sid, str(root)) and not liveness.session_abandoned(sid, str(root)):
                        held.setdefault(path, []).append(sid)
                except Exception:  # noqa: BLE001 -- indeterminate liveness holds nothing
                    continue
    except Exception:  # noqa: BLE001 -- advisory: a failed lookup holds nothing
        return {}
    return held


def holds_for(root: Path, request: LandCreditedRequest, paths: Iterable[str]) -> dict[str, str]:
    """``{path: reason}`` for each of ``paths`` the landing leaves out. Never raises; first matching source names the reason.

    Order: never-commit, sibling-repo, EM-withheld (the run's own ``withhold.jsonl``), a still-halted run's
    withheld path, a live peer's blocking touch claim.
    """
    from coordinator_core.ops.ceremony.commit_gates import is_never_commit
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import sibling_write

    root = Path(root)
    wanted = sorted({_norm(p) for p in paths if p})
    out: dict[str, str] = {}
    rest: list[str] = []
    for p in wanted:
        if is_never_commit(p):
            out[p] = REASON_NEVER_COMMIT
        elif sibling_write(p, root) is not None:
            out[p] = REASON_SIBLING_REPO
        else:
            rest.append(p)
    if not rest:
        return out
    em = _em_withheld(root, request.run_id)
    halted = _halted_holders(root, request.run_id, frozenset(rest))
    rest2: list[str] = []
    for p in rest:
        if p in em:
            out[p] = REASON_EM_WITHHELD
        elif p in halted:
            out[p] = REASON_HALTED_PREFIX + halted[p]
        else:
            rest2.append(p)
    for p, sids in sorted(_peer_claims(root, rest2, request.run_id).items()):
        out[p] = REASON_PEER_PREFIX + ",".join(sorted(sids))
    return out


def holds(root: Path, request: LandCreditedRequest) -> dict[str, str]:
    """The contract's ``HoldsHook``: holds over the paths the request's marker credits."""
    return holds_for(root, request, credited_paths(root, request))
