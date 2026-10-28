"""Halted-record ledger and kept records: the ledger-halt plan/blocking read, the run-outcome file,
the post-checkpoint candidate set, and the ``kept.json`` / ``returns.json`` / withheld-prints writes.
Imports ``halted_record_request`` only."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import halt_product_gates, halted_run, review_edits
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PLAN_FIELD,
    RECORD_DIR_ROOT,
    RUN_OUTCOME_FILE,
    write_run_outcome,
)
from coordinator_core.ops.dispatch_emit.halted_record_request import (
    _manifest_baseline,
    _manifest_kept_from,
)

#: Evidence kept per blocking item, in UTF-8 bytes, so the marker stays small.
_EVIDENCE_CAP_BYTES = 2000


def _capped(text: str) -> str:
    return text.encode("utf-8")[:_EVIDENCE_CAP_BYTES].decode("utf-8", "ignore")


def _blocking_items(items: list) -> list:
    out = []
    for i in items:
        if not isinstance(i, dict) or not isinstance(i.get("id"), str):
            continue
        rem = i.get("remaining")
        evidence = i.get("evidence")
        out.append({
            "id": i["id"],
            "remaining": [r for r in rem if isinstance(r, str)] if isinstance(rem, list) else [],
            "evidence": _capped(evidence) if isinstance(evidence, str) else "",
        })
    return out


def _ledger_halt_plan_and_blocking(repo_root: Path, halted: dict) -> tuple:
    """(plan_rel, blocking ids, blocking items) of a halt naming a relative, contained plan;
    (None, (), []) otherwise. Each item is `{id, remaining, evidence}` with strings only."""
    plan_rel = halted.get(HALT_PLAN_FIELD)
    if not isinstance(plan_rel, str) or not plan_rel or Path(plan_rel).is_absolute():
        return None, (), []
    if contained_path(repo_root / plan_rel, [repo_root]) is None:
        return None, (), []
    raw = halted.get("items")
    items = _blocking_items(raw if isinstance(raw, list) else [])
    return plan_rel, tuple(i["id"] for i in items), items


def _xs_spine_halt(worktree_root: Path, record_dir_rel: str, halted: dict) -> tuple:
    """A plan-less ledger-check halt's (spine_rel, blocking ids, blocking items), the spine being
    the single ``*.spine.md`` in the run's record dir; (None, (), []) when it holds none or several."""
    from coordinator_core.ops.dispatch_emit.ask_contract import SPINE_SUFFIX

    spines = sorted((worktree_root / record_dir_rel).glob(f"*{SPINE_SUFFIX}"))
    if len(spines) != 1:
        return None, (), []
    raw = halted.get("items")
    items = _blocking_items(raw if isinstance(raw, list) else [])
    return f"{record_dir_rel}/{spines[0].name}", tuple(i["id"] for i in items), items


_OUTCOME_HALT_KINDS = ("commit", "ledger-check")


def _persist_run_outcome(repo_root: Path, run_id: str, halted: dict, params: dict) -> Optional[str]:
    """Write the run's review outcome into its record dir for an all-landed commit or ledger-check
    halt (a dict ``params.run_outcome`` with a dict ``inline_review`` and no incomplete chunk);
    returns its repo-relative posix path, else None."""
    outcome = params.get("run_outcome")
    if halted.get("halted") not in _OUTCOME_HALT_KINDS or not isinstance(outcome, dict):
        return None
    if not isinstance(outcome.get("inline_review"), dict) or outcome.get("incomplete_chunks"):
        return None
    write_run_outcome(repo_root, run_id, outcome)
    return f"{RECORD_DIR_ROOT}/{run_id}/{RUN_OUTCOME_FILE}"


def _post_checkpoint_candidates(
    root: Path, record_dir_rel: str, doc: dict, excluded_owners: dict, review_literals: list,
    run_written: list,
) -> list:
    """Paths this run declared or wrote that a halt may need to keep: manifest row writes (minus those
    still equal to ``write_baseline``), ``review_declared_paths``, review and touch-record writes, and
    the record dir's prior ``post_checkpoint`` paths. Never a record-dir, ``excluded_owners``, scratch
    or out-of-worktree path."""
    from coordinator_core.ops.dispatch_emit import kept_work, write_baseline

    declared: list = []
    for row in doc.get("rows") or ():
        writes = row.get("writes") if isinstance(row, dict) else None
        declared.extend(w for w in writes or () if isinstance(w, str))
    declared.extend(p for p in doc.get("review_declared_paths") or () if isinstance(p, str))
    declared = [review_edits.norm(p) for p in declared]
    baseline = _manifest_baseline(root / record_dir_rel, doc)
    stale = set(write_baseline.unwritten(root, baseline, declared)) if baseline else set()
    prior = kept_work.read(root / record_dir_rel)[kept_work.POST_CHECKPOINT_KEY]
    owned = {review_edits.norm(p) for p in excluded_owners}
    out: dict = {}
    for raw in (*[p for p in declared if p not in stale], *review_literals, *run_written, *prior):
        rel = review_edits.norm(raw)
        if (
            not rel or review_edits.out_of_worktree(rel) or halt_product_gates.is_scratch(rel)
            or rel.startswith(record_dir_rel + "/") or rel in owned
        ):
            continue
        out[rel] = None
    return list(out)


def _carried_returns(root: Path, record_dir: Path, returns: dict) -> dict:
    """``returns`` with each kept-from-prior-run row's text replaced by that run's persisted entry
    (a kept row's reply is a stub); a row with no such entry keeps its own text."""
    from coordinator_core.ops.dispatch_emit import row_returns

    out = dict(returns)
    for row, prior in _manifest_kept_from(record_dir).items():
        if row not in out or not safe_id(prior):
            continue
        entry = row_returns.read(root / RECORD_DIR_ROOT / prior).get(row)
        if entry is not None:
            out[row] = entry["text"]
    return out


def _record_withheld_prints(root: Path, record_dir_rel: str, withheld: dict, paths: list) -> None:
    """Write ``WITHHELD_PRINTS_FILE`` beside the marker: blob id of each withheld path still in the
    tree, so a refire credits a path as the predecessor's only while its bytes are unchanged. The
    file joins ``paths`` (in place) so it lands with the record."""
    import json

    from coordinator_core.atomic_replace import atomic_write_bytes
    from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
    from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE

    prints = {p: f for p in sorted(withheld) if isinstance(p, str) and (f := fingerprint(root, p)) is not None}
    if not prints:
        return
    rel = f"{record_dir_rel}/{WITHHELD_PRINTS_FILE}"
    try:
        atomic_write_bytes(root / rel, (json.dumps(prints, indent=2) + "\n").encode("utf-8"))
    except OSError:
        return
    if rel not in paths:
        paths.append(rel)


def _persist_blobs(root: Path, common_dir: Optional[Path], post: dict) -> set:
    """Write each ``post`` path's bytes as a git blob (in-process, no spawn) when its sha still
    equals the recorded fingerprint, so a reviewed hunk stays recoverable after a refire; returns the
    paths whose blob was written. A path whose read or write raises OSError or whose bytes changed is
    left out; any other exception from ``write_object`` propagates. ``write_object`` is
    content-addressed, so a lost race is not a lost write."""
    from coordinator_core.git import git_objects
    from coordinator_core.ops.dispatch_emit import kept_work

    written: set = set()
    if common_dir is None or not common_dir.is_dir():
        return written
    for rel, want in post.items():
        try:
            data = (root / rel).read_bytes()
            if kept_work.blob_sha(data) != want:
                continue
            git_objects.write_object(common_dir, b"blob", data)
        except OSError:
            continue
        written.add(rel)
    return written


def _record_kept(
    root: Path, record_dir_rel: str, owner: dict, withheld: dict, halted: dict, paths: list,
    consumed_returns: Optional[dict] = None, post_candidates: Iterable[str] = (), landed: Iterable[str] = (),
    common_dir: Optional[Path] = None, manifest_doc: Optional[dict] = None,
) -> dict:
    """Write ``kept.json`` beside the withheld map and ``returns.json`` beside it when
    ``consumed_returns`` (row id to reply) holds a str entry; append their paths to ``paths`` (in
    place) so they land with the record, and stamp ``kept.json`` on the marker as
    ``next_action.resume.stage``. ``post_candidates`` that are in ``withheld``, not in ``landed`` and
    owned by no kept chunk are fingerprinted under ``post_checkpoint`` when their blob was persisted;
    the rest are named under ``post_checkpoint_unpersisted``. Every kept chunk path's blob is
    persisted too; a chunk with a path that could not be is left out of ``kept.json`` and its
    unpersisted paths are named under ``kept_unpersisted``.
    Returns ``{"kept": {...}}`` (plus ``"returns": [ids]``) when anything was recorded, else ``{}``."""
    from coordinator_core.git import kept_pin
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import kept_work, row_returns

    record_dir = root / record_dir_rel
    if not record_dir.is_dir():
        return {}
    if common_dir is None:
        common_dir = resolve_git_common_dir(root)
    returns = {
        k: v for k, v in (consumed_returns.items() if isinstance(consumed_returns, dict) else ())
        if isinstance(k, str) and isinstance(v, str)
    }
    persisted: dict = {}
    if returns:
        returns = _carried_returns(root, record_dir, returns)
        if row_returns.write(record_dir, returns) is not None:
            persisted = {k: v["sha256"] for k, v in row_returns.read(record_dir).items()}
            returns_rel = row_returns.persisted_path(record_dir_rel)
            if returns_rel not in paths:
                paths.append(returns_rel)
    owning = set(owner.values())
    baseline = _manifest_baseline(record_dir, manifest_doc)
    chunks = kept_work.kept_chunks(
        root, owner, withheld, baseline,
        returns=persisted, finished=[r for r in persisted if r not in owning],
    )
    gate = kept_work.plan_gate(halted, root)
    # A path owned by a chunk that was not kept (a peer co-declares it) is still recorded under
    # post_checkpoint, so the terminal landing has this run's own blob for it.
    recorded = {p for entry in chunks.values() for p in entry["paths"]}
    wanted = {
        p: blob
        for p, blob in kept_work.post_checkpoint_hunks(
            root, post_candidates, landed=landed, recorded=recorded, baseline=baseline
        ).items()
        if p in withheld
    }
    stored = _persist_blobs(root, common_dir, wanted)
    post = {p: blob for p, blob in wanted.items() if p in stored}
    kept_paths = {p: fp for entry in chunks.values() for p, fp in entry["paths"].items()}
    kept_stored = _persist_blobs(root, common_dir, kept_paths)
    unpersisted = set(kept_paths) - kept_stored
    pin_blobs = {post[p] for p in post} | {kept_paths[p] for p in kept_stored}
    unpinned: list = []
    if pin_blobs and not kept_pin.pin_kept(common_dir, PurePosixPath(record_dir_rel).name, pin_blobs):
        unpinned = sorted(set(post) | kept_stored)
    if unpersisted:
        chunks = {c: e for c, e in chunks.items() if not unpersisted.intersection(e["paths"])}
    written = kept_work.write(record_dir, chunks, gate, post)
    reply: dict = {"returns": sorted(persisted)} if persisted else {}
    if unpersisted:
        reply["kept_unpersisted"] = sorted(unpersisted)
    if unpinned:
        reply["kept_unpinned"] = unpinned
    if len(post) != len(wanted):
        reply["post_checkpoint_unpersisted"] = sorted(set(wanted) - set(post))
    if written is None:
        return reply
    rel = f"{record_dir_rel}/{kept_work.KEPT_FILE}"
    if rel not in paths:
        paths.append(rel)
    kept = {"chunks": sorted(chunks), "plan_gate": gate is not None}
    if post:
        kept[kept_work.POST_CHECKPOINT_KEY] = sorted(post)
    stage = {"kept": rel, **kept}
    if persisted:
        stage["returns"] = sorted(persisted)
    halted_run.record_resume_stage(record_dir, stage)
    return {"kept": kept, **reply}
