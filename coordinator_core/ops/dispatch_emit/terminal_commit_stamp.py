"""Coded-commit and plan stamping for dispatch.terminal_commit: archive moves, plan implemented stamp, trailers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from coordinator_core.session.claimed_write import replace_text
from coordinator_core.ops.dispatch_emit.plan_row_coding import (
    yaml_load as _yaml_load,
    close_memo_rows_delivered as _close_memo_rows_delivered,
    NOOP_DETAIL as _NOOP_DETAIL,
    flip_rows_coded as _flip_rows_coded,
    outside_landing_detail as _outside_landing_detail,
)
from coordinator_core.ops.dispatch_emit.worktree_snapshot import (
    tracked_at_head as _tracked_at_head,
    Snapshot as _Snapshot,
    read_rel as _read_rel,
)


_PYTEST_NODE = re.compile(r"[\w./-]+\.py::[\w.\[\]-]+(?:::[\w.\[\]-]+)*")


def _archive_in_run(worktree_root: Path, repo_root: Path, rels: tuple) -> tuple:
    """Rename the run's shipped sizing (``rels[0]``) and consumed baton (``rels[1:]``) under
    ``archive/`` in-process, through the sweep families' own classification. Returns
    ``([(old_rel, new_rel, Move)], [{"id", "reason"}])``: the renames done, and the targets a
    rail left live for the boot sweep. Spawns nothing and commits nothing."""
    from coordinator_core.ops.fleet import archive_sizings, archive_terminal_handoffs

    root = worktree_root.resolve()
    planned = []
    skipped = []
    for index, rel in enumerate(rels):
        if index == 0:
            move, reason = archive_sizings.plan_in_run_move(worktree_root, rel)
        else:
            move, reason = archive_terminal_handoffs.plan_in_run_move(worktree_root, repo_root, rel)
        if move is None:
            skipped.append({"id": rel, "reason": reason})
        else:
            planned.append((rel, move))
    done = []
    for rel, move in planned:
        _acted, failed = archive_terminal_handoffs.apply_sweep([move])
        if failed:
            skipped.extend(failed)
            continue
        done.append((rel, move.dst.relative_to(root).as_posix(), move))
    return done, skipped


def _undo_archive(archived: list) -> None:
    """Put back what ``_archive_in_run`` renamed when the carrying commit did not land. A
    byte-identical duplicate (``force``) already lived at the destination, so it is copied back."""
    import shutil

    for _old, _new, move in archived:
        try:
            if move.force:
                shutil.copy2(move.dst, move.src)
            else:
                os.replace(move.dst, move.src)
        except OSError:
            continue


#: Artifact classes the product commit carries; ``sizing`` rides the stamp commit (it is rewritten after
#: the product commit), ``plan`` only for an ask run (a plan-route DAG run stamps its plan itself).
_PRODUCT_ARTIFACT_CLASSES = frozenset({
    "record-dir", "evidence-sidecar", "seam-sidecar", "baton", "slice-sizing",
    "superseded-record", "docs-stage", "delivery-note", "mise-inventory",
})


def _differs_from_commit(worktree_root: Path, sha: str, rel: str) -> bool:
    """True when ``rel`` exists on disk and its content is not the blob ``sha`` holds for it.

    In-process (no spawn); a CRLF working copy of an LF blob counts as equal. Hashes sha1 only: a
    sha256-format repo always reads as differing.
    """
    import hashlib

    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit

    try:
        data = (worktree_root / rel).read_bytes()
    except OSError:
        return False
    head = _blob_at_commit(resolve_git_common_dir(worktree_root), sha, rel)
    if not isinstance(head, str):
        return True
    for body in (data, data.replace(b"\r\n", b"\n")):
        if hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest() == head:
            return False
    return True


def _stamp_coded_commit(
    commit_v2, worktree_root: Path, repo_root: Path, source_rows: dict,
    sha: str, session_id: Optional[str], also_commit: tuple = (),
    noop_rows: Optional[dict] = None, checkpoint_rows: Optional[dict] = None,
    snapshot: Optional[_Snapshot] = None, flip_plan: Optional[str] = None,
    receipt: Optional[str] = None,
    trailers: tuple = (), commit: bool = True, archive_after_flip: tuple = (),
    flip_reason: Optional[str] = None,
    outside_rows: Optional[dict] = None,
    ledger_met: Optional[str] = None,
    memo_closes: Optional[dict] = None,
) -> dict:
    """Second, plan-only ``commit_v2`` call stamping the product commit's
    rows ``coded`` with ``disposition_ref: <sha>`` -- a SHA only exists once
    the product commit has landed, so this cannot ride in it. One call per
    run, never per row. ``noop_rows`` (same shape as ``source_rows``) are done rows
    with no hunk in the commit: they close ``coded`` against the run's sha with a
    no-change ``disposition_detail``, so none stays ``open`` beneath a met run.
    ``checkpoint_rows`` (``{checkpoint sha: {plan: {row id}}}``) are done rows the run's
    own checkpoint commits landed: they close ``coded`` at that checkpoint's sha.
    ``outside_rows`` (``{plan: {row id: [landing sha, newest first]}}``) are done rows whose
    writes commits outside the run landed: they close ``coded`` at the newest landing sha
    with a no-op detail naming every landing sha. An edit that would make a schema-valid plan invalid
    is refused before any write; on any failure every plan edit is restored.
    ``memo_closes`` (``{plan: {row id: (receipt ref, receiver)}}``) are receipted memo-send rows:
    they close ``delivered_elsewhere`` via ``memo``, never ``coded``, in the same commit.
    ``flip_plan`` names the plan whose ``implemented`` status is written to disk without
    committing (``_stamp_plan_implemented`` reads the rows just written ``coded``), so
    the one commit below carries rows, minted stamp and status together; its reply
    returns under ``_flip``. A commit that does not land restores the flip with the rows.
    ``commit=False`` (an ask run whose plan is gitignored or unknown to the snapshot) leaves the
    stamps on disk and lands nothing. ``_committed`` names the paths the commit carried.
    ``receipt`` names the file holding the run's minted ``review_stamp``. When it is gitignored
    (a minted XS spine) the commit cannot carry it and the receipt dies with the scratch dir, so
    the reply adds ``coded_stamp_error`` beside the rest of the commit's result.
    ``archive_after_flip`` (the run's shipped sizing, then its consumed baton) is renamed under
    ``archive/`` only once the flip above reads ``implemented``; both ends of each rename ride this
    commit (``archived`` in the reply, a refusal in ``archive_skipped``). A plan that did not flip
    leaves them live for the boot sweep.
    Returns ``{"rows_coded", "coded_sha"}`` or ``{"coded_stamp_error"}``.
    """
    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source

    originals: dict = {}
    rows_coded: dict = {}
    archived: list = []

    def restore() -> None:
        for rel, text in originals.items():
            replace_text(worktree_root / rel, text)
        _undo_archive(archived)

    noop_rows = noop_rows or {}
    checkpoint_rows = checkpoint_rows or {}
    outside_rows = outside_rows or {}
    memo_closes = memo_closes or {}
    plans = set(source_rows) | set(noop_rows) | set(outside_rows) | set(memo_closes)
    for by_plan in checkpoint_rows.values():
        plans |= set(by_plan)
    for plan_rel in sorted(plans):
        original = _read_rel(worktree_root, plan_rel)
        if original is None:
            continue
        noop_ids = set(noop_rows.get(plan_rel, ())) - set(source_rows.get(plan_rel, ()))
        updated, flipped = _flip_rows_coded(
            original, set(source_rows.get(plan_rel, ())) | noop_ids, sha,
            {rid: _NOOP_DETAIL for rid in noop_ids},
        )
        for csha, by_plan in sorted(checkpoint_rows.items()):
            if plan_rel in by_plan:
                updated, more = _flip_rows_coded(updated, set(by_plan[plan_rel]), csha)
                flipped = sorted(set(flipped) | set(more))
        for rid, shas in sorted(outside_rows.get(plan_rel, {}).items()):
            updated, more = _flip_rows_coded(updated, {rid}, shas[0], {rid: _outside_landing_detail(shas)})
            flipped = sorted(set(flipped) | set(more))
        if plan_rel in memo_closes:
            updated, more = _close_memo_rows_delivered(updated, memo_closes[plan_rel])
            flipped = sorted(set(flipped) | set(more))
        if not flipped:
            continue
        invalid = check_plan_tasks_source(updated)
        if invalid is not None and check_plan_tasks_source(original) is None:
            restore()
            return {"coded_stamp_error": f"{plan_rel}: stamped spine fails schema: {invalid}"}
        replace_text(worktree_root / plan_rel, updated)
        originals[plan_rel] = original
        rows_coded[plan_rel] = flipped

    if not commit:
        return {"rows_coded": rows_coded, "coded_sha": None}
    # A plan `review_stamp.mint` just wrote rides this commit even when none of
    # its rows flipped (a re-fire over already-coded rows).
    paths = sorted(set(rows_coded) | set(also_commit))
    flip_result: Optional[dict] = None
    if flip_plan is not None and paths:
        before = _read_rel(worktree_root, flip_plan)
        from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

        flip_result = _tc._stamp_plan_implemented(
            worktree_root, flip_plan, sha, reason=flip_reason, ledger_met=ledger_met,
        )
        if flip_result.get("plan_status") == "implemented":
            if before is not None:
                originals.setdefault(flip_plan, before)
            paths = sorted(set(paths) | {flip_plan})
    archive_skipped: list = []
    archive_deleted: list = []
    if archive_after_flip and flip_result is not None and flip_result.get("plan_status") == "implemented":
        archived, archive_skipped = _archive_in_run(worktree_root, repo_root, archive_after_flip)
        old = {o for o, _n, _m in archived}
        paths = sorted((set(paths) - old) | {n for _o, n, _m in archived})
        if snapshot is not None:
            snapshot.note_written([n for _o, n, _m in archived])
        archive_deleted = _tracked_at_head(worktree_root, sorted(old))
    # A path is stageable unless it is an untracked gitignored plan (a warp run
    # spine), which is never staged; the snapshot answers that without a spawn.
    ignored = set(snapshot.ignored_among(paths)) if snapshot is not None else set()
    paths = [p for p in paths if p not in ignored]
    uncovered = [p for p in paths if snapshot is None or not snapshot.covers(p)]
    if uncovered:
        from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

        stageable = _tc._changed_paths(worktree_root, uncovered)
        if stageable is not None:
            paths = [p for p in paths if p not in uncovered or p in stageable]
    lost_receipt = (
        {"coded_stamp_error": f"{receipt}: review receipt is untracked and gitignored, so no commit "
         "can carry it; the run is unreceipted"}
        if receipt is not None and receipt not in paths else {}
    )
    if not paths:
        return {"rows_coded": {}, "coded_sha": None, **lost_receipt}
    n = sum(len(v) for v in rows_coded.values())
    message = f"mark {n} rows coded ({sha[:7]})" if n else f"review stamp ({sha[:7]})"
    if archive_deleted:
        message += "\n\narchived (moved under archive/): " + ", ".join(archive_deleted)
    if trailers:
        message += "\n\n" + "\n".join(trailers)
    params: dict = {"paths": paths, "message": message}
    if archive_deleted:
        params["deleted_paths"] = archive_deleted
    if snapshot is not None and not uncovered:
        params["ignored_untracked"] = []
    if session_id is not None:
        params["session_id"] = session_id
    try:
        reply = commit_v2(params, repo_root)
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        reply = {"committed": False, "error": repr(exc)}
    if not isinstance(reply, dict) or not reply.get("committed"):
        restore()
        detail = reply.get("error") if isinstance(reply, dict) else reply
        return {"coded_stamp_error": f"coded-stamp commit did not land: {detail!r}"}
    out = {"rows_coded": rows_coded, "coded_sha": reply.get("sha"), "_committed": paths, **lost_receipt}
    if flip_result is not None:
        out["_flip"] = flip_result
    if archived:
        out["archived"] = {o: n for o, n, _m in archived}
    if archive_skipped:
        out["archive_skipped"] = archive_skipped
    if reply.get("index_stale"):
        out["coded_index_stale"] = list(reply["index_stale"])
    return out


def _ask_trailers(
    worktree_root: Path, plan_path: Optional[str], record_abs: Optional[Path], receipts: list
) -> list:
    """The trailers an ask run's product commit carries in place of the process records it
    leaves out: ``Falsifier: <pytest node>`` (the plan's falsifier), ``Review: <sha256>`` (the
    review record under ``.coordinator-local/``) and one ``Completion-Receipt: <id>`` each."""
    import hashlib

    from coordinator_core.completion_receipts.model import RECEIPT_TRAILER

    out = []
    plan_text = _read_rel(worktree_root, plan_path) if plan_path else None
    if plan_text is not None:
        from coordinator_core.ops.dispatch_emit.emit import _prime_exit_criterion_falsifier

        falsifier = _prime_exit_criterion_falsifier(plan_text) or {}
        node = _PYTEST_NODE.search(str(falsifier.get("how") or ""))
        if node:
            out.append(f"Falsifier: {node.group(0)}")
    if record_abs is not None:
        try:
            out.append(f"Review: {hashlib.sha256(record_abs.read_bytes()).hexdigest()}")
        except OSError:
            pass
    out.extend(f"{RECEIPT_TRAILER}: {fm['receipt_id']}" for fm, _prose in receipts)
    return out


def _falsifier_broken_tells(marks: object, plan_path: Optional[str]) -> Optional[list]:
    """The fired tells when the run's pre-dispatch review called ``plan_path``'s
    falsifier BROKEN, else None. ``marks`` is the digest's ``[{plan, tells}]``;
    plans are matched as repo-relative POSIX paths, so a ``./`` or backslash
    spelling on either side still matches."""
    if not plan_path or not isinstance(marks, list):
        return None

    def norm(p: str) -> str:
        p = p.replace("\\", "/")
        return p[2:] if p.startswith("./") else p

    want = norm(plan_path)
    for mark in marks:
        key = mark.get("plan") if isinstance(mark, dict) else None
        tells = mark.get("tells") if isinstance(mark, dict) else None
        if isinstance(key, str) and norm(key) == want:
            return [str(t) for t in tells] if isinstance(tells, list) else []
    return None


def _criterion_reason(inline_review: object) -> Optional[str]:
    """The judge's ``reason`` off the run record's criterion, or None when absent."""
    criterion = inline_review.get("criterion") if isinstance(inline_review, dict) else None
    reason = criterion.get("reason") if isinstance(criterion, dict) else None
    return reason.strip() or None if isinstance(reason, str) else None


def _stamp_plan_implemented(
    worktree_root: Path, plan_rel: str, sha: str, *, reason: Optional[str] = None,
    ledger_met: Optional[str] = None,
) -> dict:
    """Write the plan's ``implemented`` status to disk, uncommitted, when the
    review stamp it now carries records a ``met`` criterion; the caller's commit
    carries it. The verdict is read off the plan on disk, never a param, so only
    a minted stamp can discharge it. A refusal (open spine rows, the goal gate)
    is reported and writes nothing. The refusal text is kept from its head: the
    cause leads it and the override boilerplate trails. ``reason`` is the judge's own reason,
    appended to the stamp prose only; the met check never reads it. ``ledger_met`` (the
    ``_ledger_met_observation`` line) discharges an undecided criterion (absent or not ``met``/
    ``not_met``) with source "requirements ledger"; a ``not_met`` criterion still refuses. An
    ``indeterminate`` stamp is likewise discharged at the transition's ``criterion_refusal`` by
    the same ``ledger_met``, forwarded to it."""
    import contextlib
    import io

    import yaml

    from coordinator_core.archive_stamp import cs_stamp_plan_implemented
    from coordinator_core.ops.plan_status_transition import _FALSIFIER_OUTPUT_MAX

    plan_abs = worktree_root / plan_rel
    text = _read_rel(worktree_root, plan_rel)
    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(text.replace("\r\n", "\n")) if text is not None else None
    try:
        fm = (_yaml_load(split.fm_text) or {}) if split is not None else {}
    except yaml.YAMLError:
        fm = {}
    stamp = fm.get("review_stamp") if isinstance(fm, dict) else None
    criterion = stamp.get("criterion") if isinstance(stamp, dict) else None
    status = criterion.get("status") if isinstance(criterion, dict) else None
    if status == "met":
        observation = str(criterion.get("observation") or "").strip()
        source = criterion.get("sidecar") or "the run's criterion leg"
    elif ledger_met and ledger_met.strip() and status != "not_met":
        observation, source = ledger_met.strip(), "requirements ledger"
    else:
        return {"plan_status": "not-stamped", "plan_status_reason": "review_stamp criterion is not met"}
    if not observation:
        return {"plan_status": "not-stamped", "plan_status_reason": "criterion observation is empty"}
    err = io.StringIO()
    recorded = isinstance(fm, dict) and fm.get("exit_criterion_met") is not None
    record = (
        {}
        if recorded
        else {
            "falsifier_verdict": "pass",
            "falsifier_output": observation[:_FALSIFIER_OUTPUT_MAX],
            "prose": f"criterion met at {sha} per {source}" + (f"; {reason}" if reason else ""),
        }
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = cs_stamp_plan_implemented(
            str(plan_abs), **record, refuse_open_spine_rows=True, commit=False, ship_sha=sha,
            ledger_met=ledger_met if status != "met" else None,
        )
    if rc != 0:
        reason = err.getvalue().strip()[:600]
        return {"plan_status": "refused", "plan_status_refusal": reason, "plan_status_reason": reason}
    return {"plan_status": "implemented"}


def _subject(contributing: list, fallback: str) -> str:
    ids = ", ".join(c.id for c in contributing)
    titles = "; ".join(c.title for c in contributing if c.title.strip())
    if not ids:
        return fallback
    return f"{ids}: {titles}" if titles else ids


def _regenerate_landing_allowlist(worktree_root: Path, paths: list) -> Optional[str]:
    """Repo-relative ``setup/publish-targets.portable`` when the commit's paths
    add a published-tree top-level name the landing allowlist does not
    classify and this call regenerated it (written to the worktree, to ride the
    commit); else ``None``.

    The pre-filter is `commit_paths`' own: a commit touching neither
    generator-owned dir, or a tree without the declarations yaml, pays no read
    and no spawn. A generator failure leaves the worktree untouched, so
    ``commit_paths`` refuses with its own message.
    """
    from coordinator_core.git import published_tree_classification as ptc

    touched = ptc.touched_published_names(paths)
    if not touched or not (worktree_root / ptc.DECLARATIONS_PATH).is_file():
        return None

    def read_worktree(rel: str) -> Optional[str]:
        try:
            return (worktree_root / rel).read_text(encoding="utf-8")
        except OSError:
            return None

    if not ptc.unclassified(touched, read_worktree):
        return None

    import importlib.util

    generator = worktree_root / "coordinator" / "bin" / "publish-allowlist-generate.py"
    try:
        from coordinator_core.bin_lib_binding import ensure_bin_lib_bound

        ensure_bin_lib_bound(str(generator.parent))
        spec = importlib.util.spec_from_file_location("publish_allowlist_generate", generator)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pending = tuple(p for p in paths if (worktree_root / p).is_file())
        regenerated = module.regenerate_portable(worktree_root, pending)
        portable = worktree_root / ptc.PORTABLE_PATH
        if regenerated == portable.read_text(encoding="utf-8"):
            return None
        portable.write_text(regenerated, encoding="utf-8", newline="\n")
    except Exception:  # noqa: BLE001 -- commit_paths refuses with its own message
        return None
    return ptc.PORTABLE_PATH
