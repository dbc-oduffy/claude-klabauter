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

A ``grind`` carrying ``per_row: true`` over more than one row lands through ``land_green_rows``: each row
as its own commit instead of one batch. A row carrying
``closed_by: fix`` with no landed commit sha is listed under ``close_refused`` in either mode.
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Optional, Sequence

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


#: A row's ``closed_by`` value claiming a code fix. Trap: ``close_refused`` only reports; it blocks no close, and
#: no composed-script row carries ``closed_by`` today, so the report fires only for a caller that sets it.
CLOSED_BY_FIX = "fix"

#: Reply key listing the ``closed_by: fix`` rows refused for want of a landed sha.
CLOSE_REFUSED_KEY = "close_refused"

#: ``grind`` key carrying the run clone's root; each landed path's bytes then come from that clone.
WORK_ROOT_KEY = "work_root"

#: ``dropped``/``stranded`` reason of a path whose main-worktree copy a peer dirtied while the run edited its clone.
DROP_PEER_DIRTY_IN_MAIN = "peer-dirty-in-main"

#: Drops that never hold a row open: a ledger-root path is evidence, not product, and an unseen deletion is already true.
_NON_PRODUCT_DROPS = frozenset({DROP_LEDGER_ROOT, DROP_UNSEEN_DELETION})


def _error(message: str, **extra: object) -> dict:
    return {"committed": False, "sha": None, "error": message, **extra}


def land(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    """Mode entry ``land_run`` imports: land ``params[GRIND_COMMIT_PARAM]``; see ``commit_grind_rows``."""
    return commit_grind_rows(worktree_root, repo_root, params)


def _fix_close_refusals(grind: object, reply: dict) -> list:
    """``[{row_id, reason}]`` for each requested row carrying ``closed_by: fix`` that the reply shows
    with no landed commit sha. Such a row's close is refused: a fix claim needs a commit to point at."""
    rows = grind.get("rows") if isinstance(grind, dict) else None
    if not isinstance(rows, list):
        return []
    landed = {
        r.get("row_id") for r in reply.get("rows") or [] if isinstance(r, dict) and r.get("sha")
    }
    return [
        {"row_id": r["row_id"], "reason": f"closed_by {CLOSED_BY_FIX} carries no landed commit sha"}
        for r in rows
        if isinstance(r, dict) and r.get("closed_by") == CLOSED_BY_FIX and r.get("row_id") not in landed
    ]


#: ``grind`` key a composed script sets to land each finished row as its own commit.
PER_ROW_KEY = "per_row"


def _wants_per_row(grind: object) -> bool:
    rows = grind.get("rows") if isinstance(grind, dict) else None
    return isinstance(grind, dict) and grind.get(PER_ROW_KEY) is True and isinstance(rows, list) and len(rows) > 1


def commit_grind_rows(main_root: Path, repo_root: Path, params: dict, ledger_paths: Sequence[str] = ()) -> dict:
    """Land the finished rows in ``params[GRIND_COMMIT_PARAM]`` as one commit; the in-process entry
    ``grind_settle_run`` calls. Reply shape in ``grind_commit_contract``'s docstring plus ``stranded``
    (``[{row_id, path, reason}]``, the dropped paths of finished rows), ``paths`` and ``close_refused``
    (``[{row_id, reason}]``: a ``closed_by: fix`` row with no landed sha). ``repo_root`` is
    the caller's common dir; the commit and every read go through ``main_root``.

    ``ledger_paths`` are the run's own manifest-named ledger-root files: the ones still changed ride
    in the same commit, except a landed row's ledger, which settle unlinks. They never hold a row."""
    reply = _commit_grind_rows_once(main_root, repo_root, params, ledger_paths)
    _reap_clone(main_root, params, reply)
    return reply


def _reap_clone(main_root: Path, params: dict, reply: dict) -> None:
    """Remove the run's clone once a call carrying ``grind.work_root`` has landed its commit."""
    grind = params.get(GRIND_COMMIT_PARAM)
    run_id = params.get("run_id")
    if not (isinstance(grind, dict) and grind.get(WORK_ROOT_KEY) and reply.get("committed") is True):
        return
    if isinstance(run_id, str) and safe_id(run_id):
        from coordinator_core.bug_blitz import worker_clone

        worker_clone.reap(main_root, run_id)


def _commit_grind_rows_once(main_root: Path, repo_root: Path, params: dict, ledger_paths: Sequence[str]) -> dict:
    """``commit_grind_rows`` without the clone reap: ``land_green_rows`` reaps once after its last row."""
    grind = params.get(GRIND_COMMIT_PARAM)
    if _wants_per_row(grind):
        return land_green_rows(main_root, repo_root, params, ledger_paths)
    reply = _commit_grind_rows(main_root, repo_root, params, ledger_paths)
    refused = _fix_close_refusals(grind, reply)
    if refused:
        reply = {**reply, CLOSE_REFUSED_KEY: refused}
    return reply


def land_green_rows(main_root: Path, repo_root: Path, params: dict, ledger_paths: Sequence[str] = ()) -> dict:
    """Land each row of ``params[GRIND_COMMIT_PARAM]`` as its own scoped commit, in row order, so a stop
    leaves only the rows not yet reached unlanded. Each commit carries only that row's declared paths
    (the worker-reported touched files) and its own ``Grind-Row:`` trailer. ``run_token`` is redeemed by
    the first row that lands; later rows land as late rows of the spent token. Reply: ``commit_grind_rows``'s shape merged over every
    row, plus ``commits`` (``[{row_id, sha}]``, one per landed row) and ``sha`` of the last commit.

    Entered from ``commit_grind_rows`` when ``grind.per_row`` is true, which the composed script sets.
    Trap: ``ledger_paths`` go to every row's call, so the first commit also carries the ledger files of
    rows not yet reached."""
    grind = params[GRIND_COMMIT_PARAM]
    bad = validate_grind_params(grind)
    if bad is not None:
        return _error(bad, refused="grind-params")
    base_grind = {k: v for k, v in grind.items() if k != PER_ROW_KEY}
    commits: list = []
    merged: dict = {"dropped": [], "stranded": [], "rows": [], UNLANDED_KEY: [], "reopened": []}
    refused: list = []
    error: Optional[dict] = None
    for row in grind["rows"]:
        reply = _commit_grind_rows_once(
            main_root, repo_root, {**params, GRIND_COMMIT_PARAM: {**base_grind, "rows": [row]}}, ledger_paths,
        )
        for key in ("dropped", "stranded", "rows", UNLANDED_KEY, "reopened"):
            merged[key] += reply.get(key) or []
        refused += reply.get(CLOSE_REFUSED_KEY) or []
        if reply.get("committed") is True:
            commits.append({"row_id": row["row_id"], "sha": reply["sha"]})
        elif reply.get("error") and error is None:
            error = reply
    out = {"committed": bool(commits), "sha": commits[-1]["sha"] if commits else None, "commits": commits, **merged}
    if error is not None:
        out.update(error=error["error"], refused=error.get("refused"))
    if refused:
        out[CLOSE_REFUSED_KEY] = refused
    return out


def _commit_grind_rows(main_root: Path, repo_root: Path, params: dict, ledger_paths: Sequence[str]) -> dict:
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
        if receipt is None:
            minted_in = _minting_checkout(main_root, run_id, token, script_path)
            if minted_in is not None:
                return _commit_grind_rows(minted_in, run_token._common_dir(minted_in), params, ledger_paths)
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


def _minting_checkout(main_root: Path, run_id: str, token: object, script_path: object) -> Optional[Path]:
    """The checkout holding this run's token when the call was routed to another one (a caller whose cwd is
    not the consumer repo): the git worktree enclosing an absolute ``script_path``, only when it differs
    from ``main_root`` and its own record admits or has spent this exact token and script. The token check
    itself is unchanged; None when no such checkout exists."""
    from coordinator_core.ops.dispatch_emit import run_token

    if not isinstance(script_path, str) or not os.path.isabs(script_path):
        return None
    here = os.path.realpath(main_root)
    cur = os.path.dirname(os.path.realpath(script_path))
    while not os.path.exists(os.path.join(cur, ".git")):
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    if os.path.normcase(cur) == os.path.normcase(here):
        return None
    root = Path(cur)
    if run_token.admits(root, run_id, token, script_path) or run_token.spent_receipt(root, run_id, token, script_path):
        return root
    return None


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


class _CloneView:
    """The run clone's bytes for ``paths`` against HEAD, read in-process: ``blobs`` (checkin form; None = absent
    in the clone), ``head`` (``{path: (mode, sha)}``) and ``changed`` (paths whose clone bytes differ from HEAD)."""

    def __init__(self, main_root: Path, clone: Path, paths: Sequence[str]) -> None:
        from coordinator_core.bug_blitz import worker_clone
        from coordinator_core.git.git_state import head_blobs

        self.main_root = main_root
        self.head = head_blobs(main_root, list(paths))
        self.blobs: dict = {}
        self.changed: set = set()
        for p, raw in worker_clone.carried_blobs(clone, list(paths)).items():
            data = None if raw is None else grind_landing_select._checkin(main_root, p, raw)
            self.blobs[p] = data
            if raw is None:
                if p in self.head:
                    self.changed.add(p)
            elif data is None or p not in self.head or _blob_sha(data) != self.head[p][1]:
                self.changed.add(p)  # a check-in form git cannot reproduce counts as changed and drops as missing

    def main_matches_head(self, p: str) -> bool:
        """True when main's copy of ``p`` is absent (and HEAD lacks it) or equals the pre-land HEAD blob."""
        target = self.main_root / p
        if not target.exists():
            return p not in self.head
        try:
            return p in self.head and _blob_sha(grind_landing_select._checkin(self.main_root, p, target.read_bytes()) or b"\0") == self.head[p][1]
        except OSError:
            return False


def _blob_sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\x00" + data).hexdigest()


def _land(
    main_root: Path, repo_root: Path, grind: dict, deliverable_id: object, run_id: str,
    ledger_paths: Sequence[str] = (),
) -> dict:
    from coordinator_core.git.commit import CommitRefused, NothingToCommit, commit_paths, hash_worktree_blobs_via_spawn
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import worktree_snapshot

    clone: Optional[Path] = None
    if grind.get(WORK_ROOT_KEY) is not None:
        from coordinator_core.bug_blitz import worker_clone

        named = grind[WORK_ROOT_KEY]
        clone = worker_clone.clone_root(main_root, run_id)
        if clone is None or not isinstance(named, str) or Path(named).resolve() != clone.resolve():
            return _error(f"params.grind.{WORK_ROOT_KEY} is not run {run_id}'s clone", refused="work-root")

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
    view: Optional[_CloneView] = None
    if clone is not None:
        view = _CloneView(main_root, clone, literals)
        changed = view.changed
    else:
        snapshot = worktree_snapshot(main_root, literals, [], []) if literals else None
        if literals and snapshot is None:
            return _error("git status over the grind rows failed", dropped=dropped, stranded=[], rows=[])
        changed = snapshot.changed if snapshot is not None else set()

    def is_file(p: str) -> bool:
        return view.blobs.get(p) is not None if view is not None else (main_root / p).is_file()

    def deletion_already_true(p: str) -> bool:
        return p not in view.head if view is not None else not (main_root / p).exists()

    staged: list = []
    for row, paths, deleted in plans:
        row_id = row["row_id"]
        live = []
        for p in paths:
            if p in deleted or p not in changed:
                continue
            if is_file(p):
                live.append(p)
            else:
                dropped.append({"row_id": row_id, "path": p, "reason": DROP_MISSING})
        removed = []
        for p in deleted:
            if p in changed:
                if view is not None and not view.main_matches_head(p):
                    dropped.append({"row_id": row_id, "path": p, "reason": DROP_PEER_DIRTY_IN_MAIN})
                else:
                    removed.append(p)
            elif deletion_already_true(p):
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
    sel = grind_landing_select.select(main_root, run_id, owners) if owners and view is None else None

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
    ledger_riders = [p for p in ledger_extra if landing and p in changed and is_file(p)]
    commit_files = list(dict.fromkeys(commit_files + ledger_riders))
    stranded = [dict(d) for d in dropped]
    unlanded = [
        {"row_id": i, "reason": "a declared product path was dropped from the landing; the row stays open"}
        for i in sorted(held)
    ]
    reopened = (
        [] if view is not None
        else _reopen_held_rows(main_root, repo_root, [(r, p, d) for r, p, d in plans if r["row_id"] in held])
    )
    base = {"dropped": dropped, "stranded": stranded, "rows": [], UNLANDED_KEY: unlanded, "reopened": reopened}
    if not landing:
        return {"committed": False, "sha": None, "nothing_to_commit": True, **base}

    supplied: Optional[dict] = sel.supplied_blobs if sel is not None else None
    landed_text = sel.landed_text if sel is not None else None
    if view is not None:
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_objects import write_object

        common = resolve_git_common_dir(main_root)
        supplied = {p: write_object(common, b"blob", view.blobs[p]) for p in commit_files}
        landed_text = landing_integrity.committed_image_text(
            main_root, {**{p: view.blobs[p] for p in commit_files}, **{p: None for p in commit_deleted}},
        )
    findings = landing_integrity.import_closure_findings(
        main_root, commit_files, commit_deleted, landed_text=landed_text,
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
    # A deletion main still holds clean must leave the worktree first: `commit_paths` refuses a declared
    # deletion of a present file. The bytes come back if the commit does not land.
    unlinked: dict = {}
    if view is not None:
        for p in commit_deleted:
            target = main_root / p
            if target.is_file():
                unlinked[p] = target.read_bytes()
                target.unlink()

    def _restore() -> None:
        for p, data in unlinked.items():
            (main_root / p).parent.mkdir(parents=True, exist_ok=True)
            (main_root / p).write_bytes(data)

    try:
        outcome = commit_paths(
            main_root,
            commit_files,
            message,
            deleted_paths=commit_deleted,
            declared_reverts=commit_deleted,
            supplied_blobs=supplied,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=main_root),
        )
    except NothingToCommit:
        _restore()
        return {"committed": False, "sha": None, "nothing_to_commit": True, **base}
    except CommitRefused as err:
        _restore()
        return _error(str(err), refused="commit-refused", **base)
    except BaseException:
        _restore()
        raise

    sha = outcome.sha
    if view is not None:
        owner_of = {p: r["row_id"] for r, live, _removed in selected for p in live}
        stranded += [
            {"row_id": owner_of.get(p, ""), "path": p, "reason": DROP_PEER_DIRTY_IN_MAIN}
            for p in outcome.left_alone if p in (supplied or {})
        ]
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
