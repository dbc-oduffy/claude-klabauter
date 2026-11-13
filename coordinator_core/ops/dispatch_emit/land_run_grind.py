"""The grind mode of ``dispatch.land_run``: one commit over a grind's finished rows.

Lands the union of the finished rows' declared product paths as one ``commit_paths`` commit with a
``Grind-Row:`` trailer per landed row. Landing-set membership is closed: only a finished row's own
declared paths enter, so an undeclared dirty file is never staged. Each path lands the owning row's own
bytes (``grind_landing_select``), never a peer run's dirty hunks of the same file; a landing whose
bytes import code HEAD will lack is refused with ``REFUSED_IMPORT_CLOSURE`` before the commit. A path the commit cannot land (a
ledger-root path, a declared deletion of a file that still exists unchanged (tracked and clean, or
gitignored), a file gone from the worktree) is dropped, reported under
``dropped`` and ``stranded`` and named in the commit by one ``UNLANDED_ROW_TRAILER`` line; it is never a
reason to refuse the call. A row with a dropped product path (any reason but a ledger-root path or an
unseen deletion) lands none of its paths, gets no ``Grind-Row:`` trailer, is listed under ``unlanded`` and
keeps its ledger; its on-disk archive move is reversed (``reopened``). A landed row's ledger is
settled afterwards. The run token is redeemed,
consumed on a landed commit and released on every other return. A repeat call after a landing is
answered from the run token's spent receipt; a requested row the receipt lacks lands in one follow-up
commit under the same rules, and a repeat with nothing new lands nothing.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Sequence

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.grind_commit_contract import (
    ALREADY_LANDED_KEY,
    DROP_LEDGER_ROOT,
    DROP_UNSEEN_DELETION,
    GRIND_COMMIT_PARAM,
    LEDGER_ROOT,
    REFUSED_IMPORT_CLOSURE,
    UNLANDED_KEY,
    partition_ledger_paths,
    render_row_trailer,
    validate_grind_params,
)
from coordinator_core.ops.dispatch_emit import grind_landing_select, landing_integrity, review_floor
from coordinator_core.ops.dispatch_emit.land_run_contract import REFUSED_MODE_AMBIGUOUS, UNLANDED_ROW_TRAILER

#: Receipt a code-landing grind commit carries: its review wave runs after landing, so the floor is exempt.
POST_LANDING_RECEIPT = "review: post-landing (grind exemption, DR-grind-review-runs-after-landing)"

#: ``dropped`` reason of a changed, undeclared-deleted path that is no longer a file in the worktree.
DROP_MISSING = "missing-from-worktree"


#: Drops that never hold a row open: a ledger-root path is evidence, not product, and an unseen deletion is already true.
_NON_PRODUCT_DROPS = frozenset({DROP_LEDGER_ROOT, DROP_UNSEEN_DELETION})


def _error(message: str, **extra: object) -> dict:
    return {"committed": False, "sha": None, "error": message, **extra}


def land(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    """Mode entry ``land_run`` imports: land ``params[GRIND_COMMIT_PARAM]``; see ``commit_grind_rows``."""
    return commit_grind_rows(worktree_root, repo_root, params)


def commit_grind_rows(main_root: Path, repo_root: Path, params: dict, ledger_paths: Sequence[str] = ()) -> dict:
    """Land the finished rows in ``params[GRIND_COMMIT_PARAM]`` as one commit; the in-process entry
    ``grind_settle_run`` calls. Reply shape in ``grind_commit_contract``'s docstring plus ``stranded``
    (``[{row_id, path, reason}]``, the dropped paths of finished rows) and ``paths``. ``repo_root`` is
    the caller's common dir; the commit and every read go through ``main_root``.

    ``ledger_paths`` are the run's own manifest-named ledger-root files: the ones still changed ride
    in the same commit, except a landed row's ledger, which settle unlinks. They never hold a row."""
    from coordinator_core.ops.dispatch_emit import run_token

    grind = params[GRIND_COMMIT_PARAM]
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _error(f"params.run_id {run_id!r} is not a single safe path segment", refused="run-id-unsafe")
    if "halted_by" in params:
        return _error("params carry both grind and halted_by", refused=REFUSED_MODE_AMBIGUOUS)
    bad = validate_grind_params(grind)
    if bad is not None:
        return _error(bad, refused="grind-params")

    token = params.get("run_token")
    script_path = params.get("script_path")
    if token is not None and not run_token.redeem(main_root, run_id, token, script_path):
        receipt = run_token.spent_receipt(main_root, run_id, token, script_path)
        if receipt is not None:
            return _land_late_rows(main_root, repo_root, receipt, grind, params, run_id, ledger_paths)
        return _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
    landed = None
    try:
        reply = _land(main_root, repo_root, grind, params.get("deliverable_id"), run_id, ledger_paths)
        if reply.get("committed") is True:
            landed = {"sha": reply.get("sha"), "rows": reply.get("rows") or [], "paths": reply.get("paths") or []}
        return reply
    finally:
        if token is not None:
            if landed is not None:
                run_token.consume(main_root, run_id, landed=landed)
            else:
                run_token.release(main_root, run_id, token, script_path)


def _land_late_rows(
    main_root: Path, repo_root: Path, receipt: dict, grind: dict, params: dict, run_id: str,
    ledger_paths: Sequence[str],
) -> dict:
    """A repeat call on a spent token: rows the receipt lacks (finished after the first landing, or
    omitted from its params) land in one follow-up commit scoped to this run's own requested rows; the
    token stays spent. Nothing new to land answers from the receipt alone."""
    landed_ids = {r.get("row_id") for r in receipt.get("rows") or [] if isinstance(r, dict)}
    late = [r for r in grind["rows"] if r["row_id"] not in landed_ids]
    if not late:
        return _already_landed(receipt, grind)
    reply = _land(main_root, repo_root, {**grind, "rows": late}, params.get("deliverable_id"), run_id, ledger_paths)
    if reply.get("committed") is not True:
        answered = _already_landed(receipt, grind)
        if reply.get("error"):
            return {**answered, "committed": False, "error": reply["error"], "refused": reply.get("refused")}
        answered[UNLANDED_KEY] = reply.get(UNLANDED_KEY) or answered[UNLANDED_KEY]
        answered["dropped"] = reply.get("dropped") or []
        answered["stranded"] = reply.get("stranded") or []
        answered["reopened"] = reply.get("reopened") or []
        return answered
    earlier = {r.get("row_id"): r for r in receipt.get("rows") or [] if isinstance(r, dict)}
    by_id = {**earlier, **{r["row_id"]: r for r in reply["rows"]}}
    return {**reply, "rows": [by_id[r["row_id"]] for r in grind["rows"] if r["row_id"] in by_id]}


def _already_landed(receipt: dict, grind: dict) -> dict:
    """Answer a repeat call from the spent receipt: nothing lands, nothing settles."""
    sha = receipt.get("sha")
    landed_rows = {r.get("row_id"): r for r in receipt.get("rows") or [] if isinstance(r, dict)}
    requested = [r["row_id"] for r in grind["rows"]]
    return {
        "committed": True,
        "sha": sha,
        "rows": [landed_rows[i] for i in requested if i in landed_rows],
        "dropped": [],
        "stranded": [],
        ALREADY_LANDED_KEY: True,
        UNLANDED_KEY: [
            {
                "row_id": i,
                "reason": f"row {i} is not in the commit {sha} this run already landed; it needs a new run",
            }
            for i in requested if i not in landed_rows
        ],
    }


def _reopen_held_rows(main_root: Path, repo_root: Path, held_plans: list) -> list:
    """Undo ``grind_rows close``'s on-disk archive move for each unlanded row: write the removed source
    row back from HEAD's blob, then delete the untracked archive copy (same file name, absent from HEAD).
    A source that cannot be restored leaves the archive copy in place. Returns the reopened source paths."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_state import head_blobs

    reopened: list = []
    for _row, paths, deleted in held_plans:
        in_head = head_blobs(main_root, list(deleted) + list(paths))
        for src in deleted:
            target = main_root / src
            if src not in in_head:
                continue
            if not target.exists():
                obj = git_objects.read_object(repo_root, in_head[src][1])
                if obj is None or obj[0] != "blob":
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(obj[1])
            for p in paths:
                copy = main_root / p
                if p not in in_head and copy.name == target.name and copy.is_file():
                    copy.unlink()
            reopened.append(src)
    return reopened


def _is_run_evidence(path: str, profile: str, run_id: str) -> bool:
    """True for a file under this run's own ``<LEDGER_ROOT><profile>/evidence/<run_id>/`` dir; another run's stays out."""
    prefix = f"{LEDGER_ROOT}{profile}/evidence/{run_id}/"
    return path.startswith(prefix) and ".." not in path[len(prefix):].split("/")


def _land(
    main_root: Path, repo_root: Path, grind: dict, deliverable_id: object, run_id: str,
    ledger_paths: Sequence[str] = (),
) -> dict:
    from coordinator_core.git.commit import CommitRefused, NothingToCommit, commit_paths, hash_worktree_blobs_via_spawn
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import worktree_snapshot

    dropped: list = []
    plans: list = []
    own_evidence: list = []
    for row in grind["rows"]:
        row_id = row["row_id"]
        paths, gone = partition_ledger_paths(list(row.get("paths") or []))
        deleted, gone_deleted = partition_ledger_paths(list(row.get("deleted_paths") or []))
        own = [p for p in gone if _is_run_evidence(p, grind["profile"], run_id)]
        own_evidence += own
        dropped += [
            {"row_id": row_id, "path": p, "reason": DROP_LEDGER_ROOT}
            for p in gone + gone_deleted if p not in own
        ]
        plans.append((row, paths, deleted))
    ledger_paths = [*ledger_paths, *own_evidence]

    from coordinator_core.backlog_grind_assemble.grind_rows import _ledger_path

    settled = {
        _ledger_path(main_root, grind["profile"], row["row_id"]).relative_to(main_root).as_posix()
        for row in grind["rows"]
    }
    ledger_extra = [p for p in dict.fromkeys(ledger_paths) if p.startswith(LEDGER_ROOT) and p not in settled]
    literals = [p for _row, paths, deleted in plans for p in paths + deleted] + ledger_extra
    snapshot = worktree_snapshot(main_root, literals, [], []) if literals else None
    if literals and snapshot is None:
        return _error("git status over the grind rows failed", dropped=dropped, stranded=[], rows=[])
    changed = snapshot.changed if snapshot is not None else set()

    staged: list = []
    for row, paths, deleted in plans:
        row_id = row["row_id"]
        live = []
        for p in paths:
            if p in deleted or p not in changed:
                continue
            if (main_root / p).is_file():
                live.append(p)
            else:
                dropped.append({"row_id": row_id, "path": p, "reason": DROP_MISSING})
        removed = []
        for p in deleted:
            if p in changed:
                removed.append(p)
            elif not (main_root / p).exists():
                continue  # absent from HEAD (unchanged) and from the worktree: the deletion is already true
            else:
                dropped.append({"row_id": row_id, "path": p, "reason": DROP_UNSEEN_DELETION})
        staged.append((row, live, removed))

    owners: dict = {}
    for row, live, _removed in staged:
        for p in live:
            rows_of = owners.get(p)
            if rows_of is None:
                owners[p] = [row["row_id"]]
            elif row["row_id"] not in rows_of:
                rows_of.append(row["row_id"])
    sel = grind_landing_select.select(main_root, run_id, owners) if owners else None

    selected: list = []
    for row, live, removed in staged:
        if sel is not None:
            kept = []
            for p in live:
                reason = sel.dropped_for(row["row_id"], p)
                if reason is None:
                    kept.append(p)
                else:
                    dropped.append({"row_id": row["row_id"], "path": p, "reason": reason})
            live = kept
        selected.append((row, live, removed))

    # A row with any dropped path is incomplete: it neither lands, settles nor reports committed.
    held = {d["row_id"] for d in dropped if d["reason"] not in _NON_PRODUCT_DROPS}
    commit_files: list = []
    commit_deleted: list = []
    landing: list = []
    for row, live, removed in selected:
        if row["row_id"] in held:
            continue
        if live or removed:
            landing.append(row)
            commit_files += live
            commit_deleted += removed
    commit_deleted = list(dict.fromkeys(commit_deleted))
    ledger_riders = [p for p in ledger_extra if landing and p in changed and (main_root / p).is_file()]
    commit_files = list(dict.fromkeys(commit_files + ledger_riders))
    stranded = [dict(d) for d in dropped]
    unlanded = [
        {"row_id": i, "reason": "a declared product path was dropped from the landing; the row stays open"}
        for i in sorted(held)
    ]
    reopened = _reopen_held_rows(main_root, repo_root, [(r, p, d) for r, p, d in plans if r["row_id"] in held])
    base = {"dropped": dropped, "stranded": stranded, "rows": [], UNLANDED_KEY: unlanded, "reopened": reopened}
    if not landing:
        return {"committed": False, "sha": None, "nothing_to_commit": True, **base}

    findings = landing_integrity.import_closure_findings(
        main_root, commit_files, commit_deleted,
        landed_text=sel.landed_text if sel is not None else None,
    )
    if findings:
        return _error(
            "the landing's import closure reaches code HEAD will lack: "
            + "; ".join(f"{f.path}: {f.detail}" for f in findings),
            refused=REFUSED_IMPORT_CLOSURE,
            integrity=[{"path": f.path, "reason": f.reason, "detail": f.detail} for f in findings],
            **base,
        )

    message = _message(
        grind, landing, len(commit_deleted), stranded, deliverable_id,
        code_landed=bool(review_floor.code_paths(commit_files + commit_deleted)),
    )
    try:
        outcome = commit_paths(
            main_root,
            commit_files,
            message,
            deleted_paths=commit_deleted,
            declared_reverts=commit_deleted,
            supplied_blobs=sel.supplied_blobs if sel is not None else None,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=main_root),
        )
    except NothingToCommit:
        return {"committed": False, "sha": None, "nothing_to_commit": True, **base}
    except CommitRefused as err:
        return _error(str(err), refused="commit-refused", **base)

    sha = outcome.sha
    rows = [{"row_id": r["row_id"], "outcome": r["outcome"], "sha": sha} for r in landing]
    settle_error = None
    try:
        _settle_ledgers(main_root, grind["profile"], [r["row_id"] for r in landing])
    except OSError as err:
        settle_error = f"ledger settle failed after the commit landed: {err}"
    return {
        "committed": True, "sha": sha, "paths": commit_files + commit_deleted, "rows": rows,
        "dropped": dropped, "stranded": stranded, UNLANDED_KEY: unlanded, "reopened": reopened,
        **({"settle_error": settle_error} if settle_error else {}),
    }


def _message(
    grind: dict, landing: list, removed: int, stranded: list, deliverable_id: object, code_landed: bool = False,
) -> str:
    subject = f"grind({' '.join(grind['profile'].split())}): {len(landing)} rows"
    if removed:
        subject += f", {removed} backlog rows removed to archive"
    paragraphs = [subject]
    if grind.get("halted"):
        paragraphs.append(f"Run halted ({grind['halted']}); this lands the rows finished before the halt.")
    trailers = [render_row_trailer(r["row_id"], r["outcome"]) for r in landing]
    if isinstance(deliverable_id, str) and deliverable_id:
        trailers.insert(0, f"Deliverable-Id: {deliverable_id}")
    if code_landed:
        trailers.append(f"Ledger-Receipt: {POST_LANDING_RECEIPT}")
    trailers += [
        f"{UNLANDED_ROW_TRAILER}: {' '.join(str(s['row_id']).split())} {s['path']} {s['reason']}"
        for s in stranded
    ]
    paragraphs.append("\n".join(trailers))
    return "\n\n".join(paragraphs)


def _settle_ledgers(main_root: Path, profile: str, row_ids: list) -> None:
    from coordinator_core.backlog_grind_assemble.grind_rows import _declare_before_unlink, _ledger_path

    for row_id in row_ids:
        ledger = _ledger_path(main_root, profile, row_id)
        if ledger.is_file():
            _declare_before_unlink(ledger, main_root)
            ledger.unlink()
