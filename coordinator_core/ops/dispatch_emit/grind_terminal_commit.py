"""The grind mode of ``dispatch.terminal_commit``: one commit over a grind's finished rows.

Lands the union of the finished rows' declared product paths as one ``ceremony.commit_v2`` call
with a ``Grind-Row:`` trailer per landed row. A path the commit cannot land (a ledger-root path, a
deletion git never saw) is dropped and reported, never a reason to refuse the call. A landed row's
ledger is settled afterwards. The run token is redeemed, consumed on a landed commit and released
on every other return. A repeat call after a landing is answered from the run token's spent
receipt and lands nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.grind_commit_contract import (
    ALREADY_LANDED_KEY,
    DROP_LEDGER_ROOT,
    DROP_UNSEEN_DELETION,
    GRIND_COMMIT_PARAM,
    UNLANDED_KEY,
    partition_ledger_paths,
    render_row_trailer,
    validate_grind_params,
)


def commit_grind_rows(main_root: Path, repo_root: Path, params: dict) -> dict:
    """Land the finished rows in ``params[GRIND_COMMIT_PARAM]`` as one commit; reply shape in
    ``grind_commit_contract``'s docstring."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error

    grind = params[GRIND_COMMIT_PARAM]
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _error(f"params.run_id {run_id!r} is not a single safe path segment", refused="run-id-unsafe")
    if "halted_by" in params:
        return _error("params carry both grind and halted_by", refused="mode-ambiguous")
    bad = validate_grind_params(grind)
    if bad is not None:
        return _error(bad, refused="grind-params")

    token = params.get("run_token")
    script_path = params.get("script_path")
    if token is not None and not run_token.redeem(main_root, run_id, token, script_path):
        receipt = run_token.spent_receipt(main_root, run_id, token, script_path)
        if receipt is not None:
            return _already_landed(receipt, grind)
        return _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
    landed = None
    try:
        reply = _land(main_root, repo_root, params, grind)
        if reply.get("committed") is True:
            landed = {"sha": reply.get("sha"), "rows": reply.get("rows") or [], "paths": reply.get("paths") or []}
        return reply
    finally:
        if token is not None:
            if landed is not None:
                run_token.consume(main_root, run_id, landed=landed)
            else:
                run_token.release(main_root, run_id, token, script_path)


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
        ALREADY_LANDED_KEY: True,
        UNLANDED_KEY: [
            {
                "row_id": i,
                "reason": f"row {i} is not in the commit {sha} this run already landed; it needs a new run",
            }
            for i in requested if i not in landed_rows
        ],
    }


def _land(main_root: Path, repo_root: Path, params: dict, grind: dict) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error, _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    dropped: list = []
    plans: list = []
    for row in grind["rows"]:
        row_id = row["row_id"]
        paths, gone = partition_ledger_paths(list(row.get("paths") or []))
        deleted, gone_deleted = partition_ledger_paths(list(row.get("deleted_paths") or []))
        dropped += [{"row_id": row_id, "path": p, "reason": DROP_LEDGER_ROOT} for p in gone + gone_deleted]
        plans.append((row, paths, deleted))

    literals = [p for _row, paths, deleted in plans for p in paths + deleted]
    base = {"dropped": dropped, "rows": []}
    snapshot = _worktree_snapshot(main_root, literals, [], []) if literals else None
    if literals and snapshot is None:
        return _error("git status over the grind rows failed", **base)
    changed = snapshot.changed if snapshot is not None else set()

    commit_paths: list = []
    commit_deleted: list = []
    landing: list = []
    for row, paths, deleted in plans:
        row_id = row["row_id"]
        live = [p for p in paths if p in changed and p not in deleted]
        removed = []
        for p in deleted:
            if p in changed:
                removed.append(p)
            else:
                dropped.append({"row_id": row_id, "path": p, "reason": DROP_UNSEEN_DELETION})
        if live or removed:
            landing.append(row)
            commit_paths += live
            commit_deleted += removed
    commit_paths = list(dict.fromkeys(commit_paths))
    commit_deleted = list(dict.fromkeys(commit_deleted))
    if not landing:
        return {"committed": False, "sha": None, "nothing_to_commit": True, **base}

    commit_params: dict = {
        "paths": commit_paths,
        "message": _message(grind, landing, len(commit_deleted), params.get("deliverable_id")),
        "drop_ignored": True,
        "skip_missing": True,
        "carry_stopped_runs": False,
    }
    if commit_deleted:
        commit_params["deleted_paths"] = commit_deleted
        commit_params["declared_reverts"] = commit_deleted
    session_id: Optional[str] = params.get("session_id")
    if isinstance(session_id, str) and _UUID_RE.fullmatch(session_id):
        commit_params["session_id"] = session_id
    try:
        reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
    except OpUnavailableError:
        return _error("ceremony.commit_v2 is not registered", **base)
    if not isinstance(reply, dict):
        return _error(f"unexpected commit_v2 reply: {reply!r}", **base)
    if reply.get("committed") is not True:
        return {**reply, **base}

    sha = reply.get("sha")
    rows = [{"row_id": r["row_id"], "outcome": r["outcome"], "sha": sha} for r in landing]
    settle_error = None
    try:
        _settle_ledgers(main_root, grind["profile"], [r["row_id"] for r in landing])
    except OSError as err:
        settle_error = f"ledger settle failed after the commit landed: {err}"
    return {
        **reply, "paths": commit_paths + commit_deleted, "rows": rows, "dropped": dropped,
        **({"settle_error": settle_error} if settle_error else {}),
    }


def _message(grind: dict, landing: list, removed: int, deliverable_id: object) -> str:
    subject = f"grind({' '.join(grind['profile'].split())}): {len(landing)} rows"
    if removed:
        subject += f", {removed} backlog rows removed to archive"
    paragraphs = [subject]
    if grind.get("halted"):
        paragraphs.append(f"Run halted ({grind['halted']}); this lands the rows finished before the halt.")
    trailers = [render_row_trailer(r["row_id"], r["outcome"]) for r in landing]
    if isinstance(deliverable_id, str) and deliverable_id:
        trailers.insert(0, f"Deliverable-Id: {deliverable_id}")
    paragraphs.append("\n".join(trailers))
    return "\n\n".join(paragraphs)


def _settle_ledgers(main_root: Path, profile: str, row_ids: list) -> None:
    from coordinator_core.backlog_grind_assemble.grind_rows import _declare_before_unlink, _ledger_path

    for row_id in row_ids:
        ledger = _ledger_path(main_root, profile, row_id)
        if ledger.is_file():
            _declare_before_unlink(ledger, main_root)
            ledger.unlink()
