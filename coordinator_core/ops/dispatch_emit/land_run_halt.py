"""``dispatch.land_run`` halted mode: land a halted run's record and the product it finished.

``land(worktree_root, repo_root, params)`` writes the halt marker, then commits the run's record dir,
its ``run_artifacts`` candidates, its superseded predecessors' record dirs and the declared writes of
every chunk it finished, in one ``git.commit.commit_paths`` commit with the halt in the message.
Membership is closed: a path enters only as a finished chunk's declared write, a run-artifact
candidate or a superseded predecessor's record file, so a peer run's dirty file is never read, never
staged and never listed. Differences from HEAD are read in process from the object store: no
``git status``, no spawn. The run token is only checked, never spent, so the resumed run's product
landing can still redeem it.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core import dispatch_abandon
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import process_artifact_policy, run_artifacts
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_DEPENDENTS_UNSTARTED,
    HALT_PLAN_FIELD,
    HALT_REASON_FIELDS,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_OUTCOME_FILE,
    LedgerHalt,
    halted_record_message,
    ledger_halt_path,
    write_ledger_halt,
    write_run_outcome,
)
from coordinator_core.ops.dispatch_emit.commit_request import PREFIX_CLAIM_LABEL
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY

# One report grammar for both landing modes: `land_run` imports this module lazily, so no cycle.
from coordinator_core.ops.dispatch_emit.land_run import _DELIVERED_REPORT_RE, _UNDELIVERED_REPORT_RE

#: ``withheld`` reason for a finished chunk's product when the halt lands the record only.
HALT_RECORD_ONLY = "halt-record-only"
#: ``withheld`` reason for a chunk with at least one declared write withheld for another reason.
CHUNK_PARTIAL = "chunk-write-set-partial"
#: ``withheld`` reason for a declared path whose peer claim could not be read.
PEER_INDETERMINATE = "peer-claim-indeterminate"
#: ``withheld`` reason for a path a live peer session holds.
PEER_HELD = "peer-held"

_EVIDENCE_CAP_BYTES = 2000


def _error(message: str, **extra: object) -> dict:
    return {"committed": False, "sha": None, "error": message, **extra}


# ---- reading the tree in process --------------------------------------------------------------


def _worktree_blob_sha(root: Path, rel: str) -> Optional[str]:
    from coordinator_core.ops.dispatch_emit.land_run import _worktree_blob

    return _worktree_blob(root, rel)


def _differs(root: Path, entries: dict, rels: Sequence[str]) -> tuple[set, set]:
    """``(differ, gone)`` over ``rels`` against ``entries`` (``{rel: (mode, sha)}`` of a tree):
    ``differ`` are files on disk absent from the tree or with other bytes; ``gone`` are tree entries
    missing on disk."""
    differ: set = set()
    gone: set = set()
    for rel in rels:
        entry = entries.get(rel)
        if (root / rel).is_file():
            if entry is None or _worktree_blob_sha(root, rel) != entry[1]:
                differ.add(rel)
        elif entry is not None:
            gone.add(rel)
    return differ, gone


def _entries_at(root: Path, tree_sha: Optional[str], rels: Sequence[str]) -> Optional[dict]:
    """``{rel: (mode, sha)}`` of the blobs ``rels`` name in tree ``tree_sha`` (HEAD's when None);
    None when a tree object on the way is unreadable."""
    from coordinator_core.git.git_state import read_tree_spine

    spine = read_tree_spine(root, list(rels), root_tree_sha=tree_sha)
    if spine is None:
        return None
    out: dict = {}
    for rel in rels:
        head, _, name = rel.rpartition("/")
        entry = spine.get(head, {}).get(name)
        if entry is not None and entry[0] != 0o40000:
            out[rel] = entry
    return out


def _tracked_names_in(root: Path, dir_rel: str) -> list:
    """Top-level file paths the HEAD tree holds under ``dir_rel``; empty when unreadable."""
    from coordinator_core.git.git_state import read_tree_spine

    spine = read_tree_spine(root, [f"{dir_rel}/x"])
    listing = spine.get(dir_rel) if spine else None
    return [f"{dir_rel}/{n}" for n, (mode, _sha) in sorted((listing or {}).items()) if mode != 0o40000]


# ---- reading the run's promises ---------------------------------------------------------------


def within_handoffs_home(worktree_root: Path, artifacts: run_artifacts.RunArtifacts) -> run_artifacts.RunArtifacts:
    """``artifacts`` without any ``baton`` candidate outside the handoffs home: a sizing's
    ``baton:`` naming another directory is never landed."""
    from coordinator_core.session.record_homes import home_dir

    home = [Path(home_dir(str(worktree_root), "handoffs"))]
    batons = artifacts.by_class.get("baton", ())
    kept = tuple(p for p in batons if contained_path(worktree_root / p, home) is not None)
    if kept == batons:
        return artifacts
    return run_artifacts.RunArtifacts(
        run_id=artifacts.run_id,
        by_class={**artifacts.by_class, "baton": kept},
        record_dirs=artifacts.record_dirs,
        root=artifacts.root,
    )


def _run_request(worktree_root: Path, script_path: object):
    """``(commit request, manifest-withheld chunk ids)`` an emitted script promises; ``(None, set())``
    when nothing is readable."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, StageManifest
    from coordinator_core.ops.dispatch_emit.commit_request import (
        MalformedCommitRequestError,
        parse_manifest_marker,
        parse_marker,
    )

    if not isinstance(script_path, str) or not script_path:
        return None, set()
    script = contained_path(worktree_root / script_path, [worktree_root])
    if script is None:
        return None, set()
    try:
        text = script.read_text(encoding="utf-8")
        manifest_rel = parse_manifest_marker(text)
        if manifest_rel is None:
            return parse_marker(text), set()
        manifest_abs = contained_path(
            worktree_root / manifest_rel, [worktree_root / RECORD_DIR_ROOT, worktree_root / RUN_DIR_ROOT]
        )
        if manifest_abs is None:
            return None, set()
        manifest = StageManifest.from_json(json.loads(manifest_abs.read_text(encoding="utf-8")))
        marker = contained_path(worktree_root / manifest.marker_path, [worktree_root / RUN_DIR_ROOT])
        if marker is None:
            return None, set()
        withheld = {g.id for g in manifest.gated} | {o.id for o in manifest.owed}
        return parse_marker(marker.read_text(encoding="utf-8")), withheld
    except (OSError, ValueError, KeyError, TypeError, MalformedCommitRequestError):
        return None, set()


def _report_text(worktree_root: Path, rel: str, cache: dict) -> Optional[str]:
    if rel not in cache:
        target = contained_path(worktree_root / rel, [worktree_root]) if rel else None
        try:
            cache[rel] = target.read_text(encoding="utf-8") if target is not None else None
        except OSError:
            cache[rel] = None
    return cache[rel]


def _own_prefix_files(worktree_root: Path, chunk, cache: dict) -> list:
    """Files ``chunk``'s own report claims under its own declared prefixes (whole path segments);
    empty when it declares no prefix, claims none or its report is unreadable."""
    if not chunk.prefixes:
        return []
    text = _report_text(worktree_root, chunk.report, cache)
    if text is None:
        return []
    claims: list = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if line.startswith(PREFIX_CLAIM_LABEL):
            rest = line[len(PREFIX_CLAIM_LABEL):].strip().strip("`'\"")
            if rest and rest.lower() != "none":
                claims.append(rest)
    bounded = [(p, p if p.endswith("/") else p + "/") for p in chunk.prefixes]
    return [c for c in claims if any(c == p or c.startswith(under) for p, under in bounded)]


def done_chunks(worktree_root: Path, params: dict) -> list:
    """The chunks a halted run finished. With ``params.incomplete_chunks`` the rest are done; without
    it a chunk is done only when its own report reads DONE and not PARTIAL/BLOCKED. A
    manifest-withheld chunk and an unreadable request are never done."""
    request, withheld = _run_request(worktree_root, params.get("script_path"))
    if request is None:
        return []
    listed = params.get("incomplete_chunks")
    incomplete = set(listed) if isinstance(listed, list) else None
    cache: dict = {}
    out: list = []
    for chunk in request.chunks:
        if chunk.id in withheld or (incomplete is not None and chunk.id in incomplete):
            continue
        if incomplete is None:
            text = _report_text(worktree_root, chunk.report, cache)
            if text is None or not _DELIVERED_REPORT_RE.search(text) or _UNDELIVERED_REPORT_RE.search(text):
                continue
        out.append(chunk)
    return out


def _held_reason(halted: dict) -> Optional[str]:
    for field in HALT_REASON_FIELDS:
        text = " ".join(str(halted.get(field) or "").split())
        if text:
            return text
    return None


# ---- halt-side record writes ------------------------------------------------------------------


def _capped(text: str) -> str:
    return text.encode("utf-8")[:_EVIDENCE_CAP_BYTES].decode("utf-8", "ignore")


def _blocking_items(items: list) -> list:
    out = []
    for i in items:
        if not isinstance(i, dict) or not isinstance(i.get("id"), str):
            continue
        rem, evidence = i.get("remaining"), i.get("evidence")
        out.append({
            "id": i["id"],
            "remaining": [r for r in rem if isinstance(r, str)] if isinstance(rem, list) else [],
            "evidence": _capped(evidence) if isinstance(evidence, str) else "",
        })
    return out


def _ledger_halt_plan_and_blocking(repo_root: Path, halted: dict) -> tuple:
    """``(plan_rel, blocking ids, blocking items)`` of a halt naming a relative, contained plan;
    ``(None, (), [])`` otherwise."""
    plan_rel = halted.get(HALT_PLAN_FIELD)
    if not isinstance(plan_rel, str) or not plan_rel or Path(plan_rel).is_absolute():
        return None, (), []
    if contained_path(repo_root / plan_rel, [repo_root]) is None:
        return None, (), []
    raw = halted.get("items")
    items = _blocking_items(raw if isinstance(raw, list) else [])
    return plan_rel, tuple(i["id"] for i in items), items


def _persist_run_outcome(repo_root: Path, run_id: str, halted: dict, params: dict) -> Optional[str]:
    """Write the run's review outcome into its record dir for an all-landed ``commit`` halt (a dict
    ``params.run_outcome`` with a dict ``inline_review`` and no incomplete chunk); its repo-relative
    path, else None."""
    outcome = params.get("run_outcome")
    if halted.get("halted") != "commit" or not isinstance(outcome, dict):
        return None
    if not isinstance(outcome.get("inline_review"), dict) or outcome.get("incomplete_chunks"):
        return None
    write_run_outcome(repo_root, run_id, outcome)
    return f"{RECORD_DIR_ROOT}/{run_id}/{RUN_OUTCOME_FILE}"


def _settle_ledger_pointer(
    repo_root: Path, run_id: str, halted: dict, sizing_rel: Optional[str], outcome_rel: Optional[str]
) -> None:
    """A landed ``ledger-check`` halt writes its plan's ledger-halt pointer; a ``commit`` halt with a
    persisted outcome writes a ``halted="commit"`` pointer naming it; any other landed halt for a
    known plan unlinks a stale pointer. A halt without a relative, contained plan touches nothing."""
    plan_rel, blocking, _items = _ledger_halt_plan_and_blocking(repo_root, halted)
    if plan_rel is None:
        return
    path = ledger_halt_path(repo_root, plan_rel)
    if halted["halted"] == "commit" and outcome_rel and sizing_rel:
        write_ledger_halt(path, LedgerHalt(run_id, plan_rel, sizing_rel, (), halted="commit", outcome=outcome_rel))
    elif halted["halted"] != "ledger-check":
        path.unlink(missing_ok=True)
    elif sizing_rel:
        write_ledger_halt(path, LedgerHalt(run_id, plan_rel, sizing_rel, blocking))


def _stamp_run_baton(
    worktree_root: Path, run_id: str, kind: str, reason: Optional[str]
) -> tuple[Optional[str], Optional[dict]]:
    """Stamp the run's recorded baton with ``status_reason`` naming the halt; ``status`` stays as it
    is, so a refire still picks it up. ``(baton_rel, error)``: ``baton_rel`` is None when the run
    recorded no readable baton in the handoffs home, ``error`` is ``{path, error}`` for a refused write."""
    from coordinator_core.frontmatter.primitives import (
        insert_fm_field,
        read_fm_field,
        rebuild,
        replace_fm_field,
        split_frontmatter,
    )
    from coordinator_core.locked_write import LockTimeout, locked_rmw
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton
    from coordinator_core.session.record_homes import home_dir

    rel = read_run_baton(worktree_root, run_id)
    if rel is None:
        return None, None
    abs_path = contained_path(worktree_root / rel, [Path(home_dir(str(worktree_root), "handoffs"))])
    if abs_path is None or not abs_path.is_file():
        return None, None
    value = f"run {run_id} halted: {kind}" + (f": {reason}" if reason else "")

    def compose(old: str) -> str:
        split = split_frontmatter(old)
        if split is None:
            raise ValueError("baton has no frontmatter")
        fm = split.fm_text
        fm = insert_fm_field(fm, "status_reason", value) if read_fm_field(fm, "status_reason") is None \
            else replace_fm_field(fm, "status_reason", value)
        return old if fm == split.fm_text else rebuild(split, fm)

    try:
        locked_rmw(abs_path, compose, repo_root=worktree_root)
    except (LockTimeout, OSError, ValueError) as exc:
        return rel, {"path": rel, "error": str(exc)}
    return rel, None


def _live_run_ids(repo_root: Path) -> list:
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    try:
        return [r["run_id"] for r in abandoned_runs._token_records(repo_root)]
    except Exception:  # noqa: BLE001 -- a read failure names none live
        return []


def _superseded_candidates(root: Path, run_id: str, sizing_rel: Optional[str]) -> list:
    """Names of earlier-stamped record dirs that could be this run's superseded predecessors:
    ``warp-<sizing stem>-*`` and ``ask-*`` dirs with a stamp below this run's. One directory name
    scan, no per-run read; ``run_artifacts.superseded_runs`` then filters them by ``run.json``."""
    import os
    from pathlib import PurePosixPath

    from coordinator_core.ops.dispatch_emit.run_artifacts import _STAMP_TAIL

    mine = _STAMP_TAIL.search(run_id)
    if mine is None or not sizing_rel:
        return []
    prefixes = (f"warp-{PurePosixPath(sizing_rel).stem}-", "ask-")
    names = []
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as it:
            for entry in it:
                if not entry.name.startswith(prefixes) or entry.name == run_id:
                    continue
                tail = _STAMP_TAIL.search(entry.name)
                if tail and tail.group(0) < mine.group(0) and entry.is_dir():
                    names.append(entry.name)
    except OSError:
        return []
    return sorted(names)


# ---- the landing ------------------------------------------------------------------------------


def land(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    """Land a halted run's record (``params.halted_by``) and the product it finished; see the module."""
    from coordinator_core.ops.dispatch_emit import run_token

    halted = params[HALTED_RECORD_PARAM]
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _error(f"params.run_id {run_id!r} is not a single safe path segment", refused="run-id-unsafe")
    if contained_path(worktree_root / RECORD_DIR_ROOT / run_id, [worktree_root / RECORD_DIR_ROOT]) is None:
        return _error(f"record dir for {run_id!r} escapes {RECORD_DIR_ROOT}", refused="record-dir-escapes")
    record_dir_rel = f"{RECORD_DIR_ROOT}/{run_id}"

    from coordinator_core.ops.dispatch_emit.run_close import sibling_release

    siblings = sibling_release(worktree_root, run_id, None)
    token = params.get("run_token")
    script_path = params.get("script_path")
    if token is not None and not run_token.admits(worktree_root, run_id, token, script_path):
        refusal = _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
        if siblings is not None:
            refusal["siblings"] = siblings
        return refusal
    reply = _land(worktree_root, repo_root, params, run_id, record_dir_rel, halted)
    if token is not None:
        reply = _reconcile_landing(worktree_root, run_id, token, script_path, halted, reply)
    if siblings is not None:
        reply["siblings"] = siblings
    return reply


def _reconcile_landing(
    worktree_root: Path, run_id: str, token: object, script_path: object, halted: dict, reply: dict
) -> dict:
    """Record a landed record commit beside the live token (never spending it); answer a re-run that
    finds nothing left to commit as ``already_landed`` with the recorded sha when it is on HEAD and
    the halt kind is the same."""
    from coordinator_core.ops.dispatch_emit import run_token

    kind = halted.get("halted")
    if reply.get("committed") and isinstance(reply.get("sha"), str):
        run_token.note_halt_landing(
            worktree_root, run_id, token, script_path,
            {"sha": reply["sha"], "paths": list(reply.get("paths") or []), "halted": kind},
        )
        return reply
    if reply.get("committed") is False and reply.get("nothing_to_commit") and not reply.get("error"):
        landed = run_token.halt_landing(worktree_root, run_id, token, script_path)
        sha = landed.get("sha") if landed else None
        from coordinator_core.ops.dispatch_emit.land_run import landed_on_head

        if landed and landed.get("halted") == kind and isinstance(sha, str) and landed_on_head(worktree_root, sha):
            answer = {k: v for k, v in reply.items() if k != "nothing_to_commit"}
            return {**answer, "committed": True, "sha": sha, ALREADY_LANDED_KEY: True,
                    "paths": list(landed.get("paths") or [])}
    return reply


def _land(
    worktree_root: Path, repo_root: Path, params: dict, run_id: str, record_dir_rel: str, halted: dict
) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit import landing_integrity
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import split_sibling_paths
    from coordinator_core.ops.dispatch_emit.halted_run import record_withheld, write_halt_marker
    from coordinator_core.ops.warp_run_deliverable import record_sizing_rel

    root = worktree_root
    base = {"record_dir": record_dir_rel, "halted": halted["halted"]}
    kind = halted["halted"]
    halt_reason = _held_reason(halted) if kind == HALT_DEPENDENTS_UNSTARTED else None
    run_base = landing_integrity.run_base_for(root, run_id, params)
    session_id = params.get("session_id")
    if not (isinstance(session_id, str) and _UUID_RE.fullmatch(session_id)):
        session_id = None

    owner: dict = {}  # declared path -> owning chunk id, first declarer wins
    cache: dict = {}
    for chunk in done_chunks(root, params):
        for path in [*chunk.paths, *_own_prefix_files(root, chunk, cache)]:
            owner.setdefault(path, chunk.id)
    home, _siblings = split_sibling_paths(list(owner), root)
    declared = [p for p in home if not p.startswith(record_dir_rel + "/")]
    sizing_rel = record_sizing_rel(root, run_id)
    if (
        sizing_rel and not Path(sizing_rel).is_absolute()
        and contained_path(root / sizing_rel, [root]) is not None
        and sizing_rel not in owner and not sizing_rel.startswith(record_dir_rel + "/")
    ):
        declared.append(sizing_rel)

    outcome_rel = _persist_run_outcome(root, run_id, halted, params)
    if (root / record_dir_rel).is_dir():
        plan_rel, blocking, blocking_items = (
            _ledger_halt_plan_and_blocking(repo_root, halted) if kind == "ledger-check" else (None, (), [])
        )
        write_halt_marker(
            root / record_dir_rel,
            halted=kind, sizing_rel=sizing_rel, plan_rel=plan_rel,
            blocking=blocking if plan_rel else None,
            blocking_items=blocking_items if plan_rel else None,
            run_base_sha=run_base, reason=halt_reason,
            next_action=halted.get("next_action") if isinstance(halted.get("next_action"), dict) else None,
        )
    raw_docs = params.get(run_artifacts.DOC_PATHS_PARAM)
    artifacts = within_handoffs_home(root, run_artifacts.candidate_paths(
        root, run_id, sizing_rel=sizing_rel,
        plan_rel=halted.get(HALT_PLAN_FIELD) if isinstance(halted.get(HALT_PLAN_FIELD), str) else None,
        doc_paths=[d for d in raw_docs if isinstance(d, str)] if isinstance(raw_docs, list) else (),
    ))
    artifacts, process_withheld = process_artifact_policy.withhold_process_artifacts(root, artifacts)
    if process_withheld:
        declared = [p for p in declared if p != sizing_rel]
        if sizing_rel:
            process_withheld[sizing_rel] = process_artifact_policy.WITHHELD_REASON
    baton_rel, baton_error = _stamp_run_baton(root, run_id, kind, halt_reason)
    if process_withheld:
        if baton_rel is not None:
            process_withheld[baton_rel] = process_artifact_policy.WITHHELD_REASON
    elif baton_rel is not None and baton_rel not in artifacts.by_class.get("baton", ()) and baton_rel not in declared:
        declared.append(baton_rel)
    baton_reply = {"run_baton": baton_error} if baton_error is not None else {}

    tracked_record = _tracked_names_in(root, record_dir_rel)
    probe = sorted({*declared, *artifacts.paths(), *tracked_record})
    superseded = run_artifacts.superseded_runs(
        root, run_id, sizing_rel,
        [f"{RECORD_DIR_ROOT}/{n}/x" for n in _superseded_candidates(root, run_id, sizing_rel)],
        live_run_ids=_live_run_ids(repo_root),
    )
    artifacts = artifacts.with_superseded(superseded, RECORD_DIR_ROOT)
    artifacts, more_withheld = process_artifact_policy.withhold_process_artifacts(root, artifacts)
    process_withheld.update(more_withheld)
    extra_tracked = [p for n in superseded for p in _tracked_names_in(root, f"{RECORD_DIR_ROOT}/{n}")]
    probe = sorted({*probe, *artifacts.paths(), *extra_tracked})
    entries = _entries_at(root, None, probe)
    if entries is None:
        return _error(f"HEAD tree under {record_dir_rel} is unreadable", **base)
    differ, gone = _differs(root, entries, probe)
    changed = differ | gone

    paths = sorted(p for p in differ if p.startswith(record_dir_rel + "/") and "/" not in p[len(record_dir_rel) + 1:])
    deleted = sorted(p for p in gone if p.startswith(record_dir_rel + "/") and "/" not in p[len(record_dir_rel) + 1:])

    if process_withheld:
        process_withheld.update({p: process_artifact_policy.WITHHELD_REASON for p in (*paths, *deleted)})
        paths, deleted = [], []

    product = [p for p in declared if p in differ]
    removed = [p for p in declared if p in gone]
    withheld: dict = dict(process_withheld)
    handled = set(paths) | set(deleted) | set(declared)
    own_record = {p for p in artifacts.paths() if artifacts.class_of(p) == "record-dir"}
    rest = run_artifacts.RunArtifacts(
        run_id=run_id,
        by_class={c: tuple(p for p in ps if p not in handled and p not in own_record) for c, ps in artifacts.by_class.items()},
        record_dirs=artifacts.record_dirs, root=artifacts.root,
    )
    extra = run_artifacts.landable(root, rest, changed, (), session_id=session_id)
    withheld.update(extra.withheld)
    art_paths, art_deleted = list(extra.present), list(extra.deleted)

    from coordinator_core.ops.ceremony import commit_v2

    holds: list = []
    warnings = commit_v2._peer_claim_warnings(
        root, product + removed + art_paths + art_deleted, holds, own_sid=session_id
    )
    held = {p for p, _sid in holds}
    withheld.update({p: PEER_HELD for p in sorted(held)})
    peer_held_by: dict = {}
    if holds:
        from coordinator_core.ops.dispatch_emit import peer_claim_contract

        peer_held_by = {"peer_held_by": peer_claim_contract.group(holds)}
    indeterminate = [w for w in warnings if w.startswith("claim state indeterminate")]
    if indeterminate:
        everywhere = any(w.startswith("claim state indeterminate (") for w in indeterminate)
        for p in product + removed:
            if p not in held and (everywhere or f"claim state indeterminate for {p!r}" in indeterminate):
                withheld[p] = PEER_INDETERMINATE
    pending = product + removed
    product = [p for p in product if p not in withheld]
    removed = [p for p in removed if p not in withheld]
    art_paths = [p for p in art_paths if p not in withheld]
    art_deleted = [p for p in art_deleted if p not in withheld]

    integrity: list = []
    targeted: Optional[dict] = None
    tfind: list = []
    tcheck = ""
    if kind == "ledger-check":
        tfind, tcheck = landing_integrity.landing_test_gate(root, params, run_id)
        targeted = {
            "check": tcheck,
            "findings": [{"path": f.path, "reason": f.reason, "detail": f.detail} for f in tfind],
        }
    if kind != "ledger-check" or tcheck in ("no-verdict", landing_integrity.NOT_RUN_CHECK):
        # Trap: with no verdict, or a verdict where no test executed (not-run), the scoped-test leg
        # never ran, so landing would ship untested product; a resumed run re-runs the withheld
        # chunks and its terminal commit carries the verdict.
        withheld.update({p: HALT_RECORD_ONLY for p in product + removed if p in owner})
    else:
        from coordinator_core.ops.dispatch_emit import halt_closure

        partial_chunks = halt_closure.partial_chunks(owner, pending, [p for p in product + removed if p not in withheld])
        withheld.update({p: CHUNK_PARTIAL for p in product + removed if owner.get(p) in partial_chunks})
        plan = halted.get("plan") if isinstance(halted.get("plan"), str) else None
        if not plan:
            request, _ = _run_request(root, params.get("script_path"))
            plan = getattr(request, "plan_path", None)
        live = [p for p in product if p not in withheld]
        integrity = landing_integrity.unparseable_py(root, live)
        integrity += landing_integrity.import_closure_findings(
            root, live, [p for p in removed if p not in withheld])
        if run_base is not None and live:
            integrity += landing_integrity.since_base_findings(root, live, run_base, run_id=run_id, plan=plan)
        bad = {owner[f.path]: f.reason for f in reversed(integrity) if f.path in owner}
        if any(f.path not in owner and f.reason == landing_integrity.IMPORT_CLOSURE_UNTRACKED for f in integrity):
            # A detector failure names no path: fail closed over every chunk still landing.
            for p in product + removed:
                if p not in withheld and p in owner:
                    bad.setdefault(owner[p], landing_integrity.IMPORT_CLOSURE_UNTRACKED)
        withheld.update({p: bad[owner[p]] for p in product + removed if p not in withheld and owner.get(p) in bad})
        if tfind:
            landing = {p for p in product + removed if p not in withheld}
            landing_chunks = {owner[p] for p in landing if p in owner}
            hit = {owner[f.path] for f in tfind if f.path in owner and owner[f.path] in landing_chunks}
            everything = any(f.path not in owner or owner[f.path] not in landing_chunks for f in tfind)
            withheld.update({
                p: landing_integrity.TARGETED_TESTS_FAILED for p in landing
                if p in owner and (everything or owner[p] in hit)
            })
    product = [p for p in product if p not in withheld]
    removed = [p for p in removed if p not in withheld]
    unlanded_chunks = {owner[p] for p in withheld if p in owner}
    landed_chunks = sorted({owner[p] for p in product + removed if p in owner} - unlanded_chunks)
    paths = paths + product + art_paths
    deleted = deleted + removed + art_deleted
    landed_classes: dict = {}
    for p in art_paths + art_deleted:
        cls = artifacts.class_of(p)
        if cls:
            landed_classes[cls] = landed_classes.get(cls, 0) + 1

    if process_withheld:
        process_artifact_policy.exclude_locally(root, process_withheld, artifacts.record_dirs)

    if not paths and not deleted:
        _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel, outcome_rel)
        return {
            "committed": False, "sha": None, "nothing_to_commit": True,
            **({"withheld": withheld} if withheld else {}), **peer_held_by,
            **({"targeted_tests": targeted} if targeted is not None else {}), **baton_reply, **base,
        }

    message = halted_record_message(run_id, halted)
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_halted_trailer_lines

    for line in run_halted_trailer_lines(landing_integrity.closing_bases(root, run_id, params)):
        message += "\n" + line
    if landed_chunks:
        message += "\nLands-Chunks: " + ", ".join(landed_chunks)
    if landed_classes:
        message += "\nLands-Artifacts: " + ", ".join(f"{c}={n}" for c, n in sorted(landed_classes.items()))
    if deleted:
        message += "\nRemoves: " + ", ".join(deleted)
    if (root / record_dir_rel).is_dir():
        record_withheld(root / record_dir_rel, withheld)
    from coordinator_core.ops.dispatch_emit.halted_record_commit import _record_kept

    kept_reply = _record_kept(root, record_dir_rel, owner, withheld, halted, paths)
    left = dispatch_abandon.remaining()
    if left is not None and left - dispatch_abandon.REPLY_MARGIN_SECS <= 0:
        return _error(
            f"commit step not started: {left:.1f}s left before the dispatch deadline; nothing committed", **base
        )
    from coordinator_core.git.commit import (
        CommitRefused,
        NothingToCommit,
        commit_paths,
        hash_worktree_blobs_via_spawn,
    )

    try:
        outcome = commit_paths(
            root, paths, message, deleted_paths=deleted, declared_reverts=deleted,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=root),
        )
    except NothingToCommit:
        return {"committed": False, "sha": None, "nothing_to_commit": True, **peer_held_by, **baton_reply, **base}
    except CommitRefused as exc:
        return _error(str(exc), refused="commit-refused", **base)
    _settle_ledger_pointer(repo_root, run_id, halted, sizing_rel, outcome_rel)
    out: dict = {"committed": True, "sha": outcome.sha, "error": None, "refused": None, **peer_held_by, **baton_reply, **kept_reply}
    left_over = run_artifacts.residue(artifacts, changed - set(paths) - set(deleted), paths + deleted, withheld)
    if left_over:
        out["run_artifact_residue"] = left_over
    out["run_artifacts"] = {"landed": sorted(paths + deleted), "withheld": withheld}
    if landed_chunks:
        out["landed_chunks"] = landed_chunks
    if withheld:
        out["withheld"] = withheld
    if targeted is not None:
        out["targeted_tests"] = targeted
    if integrity:
        out["integrity"] = [{"path": f.path, "reason": f.reason, "detail": f.detail} for f in integrity]
    return {**out, "paths": paths + deleted, **base}
