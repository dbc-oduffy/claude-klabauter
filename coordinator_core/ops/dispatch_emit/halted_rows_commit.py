"""``dispatch.commit_halted_rows``: lands the delivered rows of an already-halted run, one commit per row.

Params: ``{script_path, delivered: [row id], open_rows?: [{id, reason}], row_diffs?: {path: unified diff}}``.
Rows and their declared paths come from the run script's commit-request marker; ``delivered`` names the
rows to land and ``open_rows`` the withheld ones, each carried as a ``Requirement-Open`` trailer so the
ledger stays open for them. A path a withheld row also declares is overlapped: it lands only through
``row_diffs[path]`` (``git apply --cached``, then ``prefer_staged``), never as the whole file; an
overlapped path with no recorded diff refuses that row. A row whose paths are already clean, or whose
diff is already in HEAD, is a no-op, so a second call changes nothing.
Reply: ``{ok, landed: [{row, sha, paths}], noop: [row], refused: [{row, reason}]}``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.commit_halted_rows"

_DIFF_PATH = re.compile(r"^(?:---|\+\+\+) (?:a/|b/)?(?P<p>\S.*?)(?:\t.*)?$", re.M)


def diff_names_only(diff: str, path: str) -> bool:
    """True when every ``---``/``+++`` header of ``diff`` names ``path`` (or ``/dev/null``). Pure."""
    names = {m.group("p") for m in _DIFF_PATH.finditer(diff)} - {"/dev/null"}
    return names == {path}


def row_message(row_id: str, title: str, open_rows: list) -> str:
    """Commit message for one landed row: subject, ``Halted-Run-Row`` trailer, one
    ``Requirement-Open`` trailer per withheld row. Pure."""
    lines = [f"halted-run row {row_id}: {title}".rstrip(": "), "", f"Halted-Run-Row: {row_id}"]
    lines += [
        "Requirement-Open: " + " ".join((str(r["id"]) + " " + str(r.get("reason", ""))).split())
        for r in open_rows
    ]
    return "\n".join(lines)


def _stage_hunks(root: Path, path: str, diff: str) -> Optional[str]:
    """Stage only ``diff`` for ``path``. ``None`` staged, ``"noop"`` already in HEAD, else the failure text."""
    data = (diff if diff.endswith("\n") else diff + "\n").encode("utf-8")
    args = ["-C", str(root), "apply", "--cached", "--whitespace=nowarn", "-"]
    if run_git(args, input=data).ok:
        return None
    if run_git([*args[:-1], "--reverse", "--check", "-"], input=data).ok:
        return "noop"
    return run_git(args, input=data).stderr.strip() or "git apply --cached failed"


def commit_rows(root: Path, repo_root: Path, params: dict) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops._path_guard import contained_path
    from coordinator_core.ops.dispatch_emit.commit_request import MalformedCommitRequestError, parse_marker
    from coordinator_core.ops.dispatch_emit.terminal_commit import _tracked_at_head, _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    out: dict = {"ok": True, "landed": [], "noop": [], "refused": []}

    def fail(message: str) -> dict:
        return {**out, "ok": False, "error": message}

    script_rel = params.get("script_path")
    delivered = params.get("delivered")
    open_rows = params.get("open_rows") or []
    diffs = params.get("row_diffs") or {}
    if not isinstance(script_rel, str) or not script_rel:
        return fail("params.script_path is required")
    if not isinstance(delivered, list) or not delivered or not all(isinstance(r, str) for r in delivered):
        return fail("params.delivered must be a non-empty list of row ids")
    if not isinstance(open_rows, list) or not all(isinstance(r, dict) and r.get("id") for r in open_rows):
        return fail("params.open_rows must be a list of {id, reason}")
    if not isinstance(diffs, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in diffs.items()):
        return fail("params.row_diffs must map a path to a unified diff")

    script = Path(script_rel)
    guarded = contained_path(script if script.is_absolute() else root / script, [root])
    text = None
    if guarded is not None:
        try:
            text = guarded.read_text(encoding="utf-8")
        except OSError:
            text = None
    if text is None:
        return fail(f"cannot read params.script_path {script_rel!r}")
    try:
        request = parse_marker(text)
    except MalformedCommitRequestError as exc:
        return fail(f"malformed commit-request marker: {exc}")
    if request is None:
        return fail(f"{script_rel!r} carries no commit-request marker")
    chunks = {c.id: c for c in request.chunks}
    unknown = [r for r in delivered if r not in chunks]
    if unknown:
        return fail(f"rows {unknown} are not chunks of {script_rel!r}")

    withheld_ids = [str(r["id"]) for r in open_rows]
    withheld_paths = {p for rid in withheld_ids if rid in chunks for p in chunks[rid].paths}
    wanted = [p for rid in delivered for p in chunks[rid].paths]
    snapshot = _worktree_snapshot(root, wanted, [], [])
    if snapshot is None:
        return fail("git status over the delivered rows' paths failed")

    session_id = params.get("session_id")
    for rid in delivered:
        chunk = chunks[rid]
        dirty = [p for p in chunk.paths if p in snapshot.changed]
        if not dirty:
            out["noop"].append(rid)
            continue
        staged: list = []
        hunk_noop: list = []
        reason = None
        for path in dirty:
            diff = diffs.get(path)
            if diff is None:
                if path in withheld_paths:
                    reason = f"{path} is also declared by a withheld row and has no recorded diff in params.row_diffs"
                    break
                continue
            if not diff_names_only(diff, path):
                reason = f"row_diffs[{path!r}] names a file other than {path}"
                break
            result = _stage_hunks(root, path, diff)
            if result == "noop":
                hunk_noop.append(path)
            elif result is not None:
                reason = f"git apply --cached {path}: {result}"
                break
            else:
                staged.append(path)
        if reason is not None:
            out["refused"].append({"row": rid, "reason": reason})
            continue
        paths = [p for p in dirty if p not in hunk_noop and (root / p).exists()]
        deleted = _tracked_at_head(root, [p for p in dirty if not (root / p).exists()])
        if not paths and not deleted:
            out["noop"].append(rid)
            continue
        commit_params: dict = {"paths": paths, "message": row_message(rid, chunk.title, open_rows)}
        if staged:
            commit_params["prefer_staged"] = staged
        if deleted:
            commit_params["deleted_paths"] = deleted
        if isinstance(session_id, str) and _UUID_RE.fullmatch(session_id):
            commit_params["session_id"] = session_id
        try:
            reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
        except OpUnavailableError:
            return fail("ceremony.commit_v2 is not registered")
        if not isinstance(reply, dict) or not reply.get("committed"):
            out["refused"].append({"row": rid, "reason": str(reply.get("error") if isinstance(reply, dict) else reply)})
            continue
        out["landed"].append({"row": rid, "sha": reply.get("sha"), "paths": paths + deleted})
    out["ok"] = not out["refused"]
    return out


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.commit_halted_rows": one status read, one ``commit_v2`` call per landed row."""
    if repo_root is None:
        return {"ok": False, "error": f"{OP_NAME} requires repo_root"}
    params = params if isinstance(params, dict) else {}
    return commit_rows(main_worktree_root(Path(repo_root)), Path(repo_root), params)
