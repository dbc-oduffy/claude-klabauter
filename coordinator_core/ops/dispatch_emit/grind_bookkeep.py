"""Grind run bookkeeping without agents: close, land and ledger-sweep a no-product-edit run in-process.

`bookkeep` settles a run that changed no product file through the verbs the close, commit and drain agents ran:
`grind-row close`, `land_run_grind.commit_grind_rows` and `grind-row run-record --sweep`. Op
`dispatch.grind_bookkeep` is `bookkeep` over the op's params.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import quote

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import record_paths

OP_NAME = "dispatch.grind_bookkeep"

#: `grind-row close --closed-by` for a row whose own `closed_by` is absent: the refute-close stage's value.
DEFAULT_CLOSED_BY = "refute-close"

#: Reply `refused` values; each is returned before any file is written.
REFUSED_PRODUCT_PATH = "product-path"
REFUSED_PARAMS = "params"

_RUN_STAMP_RE = re.compile(r"\d{8}T\d{6}Z?")
#: `grind_rows.evidence_rel` joins profile, run id and row id into a written path unchecked, so the op
#: validates each here: one segment, no separator, no leading dot.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ROW_FIELDS = ("row_id", "path", "digest", "evidence")


def _reply(**extra: Any) -> dict:
    return {"closed": [], "committed": False, "sha": None, "swept": False, "error": None, **extra}


def _product_path(rows: Sequence[Mapping[str, Any]]) -> Optional[tuple[str, str]]:
    for row in rows:
        for path in row.get("touchedFiles") or ():
            if not record_paths.is_record_path(path):
                return str(row.get("row_id")), str(path)
    return None


def _shape_error(rows: Sequence[Any]) -> Optional[str]:
    if not rows:
        return "no rows to settle"
    for row in rows:
        if not isinstance(row, Mapping):
            return f"row {row!r} is not an object"
        missing = [k for k in _ROW_FIELDS if not isinstance(row.get(k), str) or not row.get(k)]
        if missing:
            return f"row {row.get('row_id')!r} lacks {', '.join(missing)}"
        if not _SEGMENT_RE.fullmatch(row["row_id"]):
            return f"row_id {row['row_id']!r} is not a single path segment"
        posix = PurePosixPath(row["path"].replace("\\", "/"))
        if posix.is_absolute() or Path(row["path"]).is_absolute() or ".." in posix.parts:
            return f"row {row['row_id']!r} path {row['path']!r} is not repo-relative"
    return None


#: `redirect_stdout` swaps process-global streams: two overlapping swaps restore each other's buffer and
#: leave `sys.stdout` pointing at a StringIO, so every verb run takes this lock.
_STREAM_LOCK = threading.Lock()


def _run_verb(verb, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with _STREAM_LOCK, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = verb(argv)
    return code, out.getvalue(), err.getvalue().strip()


def _relative(path: str, root: Path) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix() if p.is_absolute() else PurePosixPath(path).as_posix()
    except ValueError:
        return PurePosixPath(path.replace("\\", "/")).as_posix()


def _close_rows(
    root: Path, run_id: str, rows: Sequence[Mapping[str, Any]], profile: str, profile_dir: str, stamp: str,
) -> tuple[list[dict], list[str]]:
    """`grind-row close` per row, in row order; returns `(closed, errors)` where `closed` is
    `[{row_id, path, new, touched}]`. A refused close leaves the row open and is named in `errors`."""
    from coordinator_core.backlog_grind_assemble import grind_rows

    closed: list[dict] = []
    errors: list[str] = []
    for row in rows:
        row_id = row["row_id"]
        evidence_path = root / grind_rows.evidence_rel(profile, run_id, row_id, "verify")
        try:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(f"{row.get('verdict') or 'triage'}: {row['evidence']}\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            errors.append(f"{row_id}: evidence write failed: {exc}")
            continue
        code, out, err = _run_verb(grind_rows.cmd_close, [
            "--profile-dir", profile_dir, "--profile", profile, "--row", row["path"], "--digest", row["digest"],
            "--verdict", "refute-close", "--evidence-file", str(evidence_path),
            "--closed-by", str(row.get("closed_by") or DEFAULT_CLOSED_BY), "--run-stamp", stamp,
            "--repo-root", str(root),
        ])
        if code != 0:
            errors.append(f"{row_id}: {err[-300:] or f'close exit {code}'}")
            continue
        try:
            new = _relative(json.loads(out.strip().splitlines()[-1])["new"], root)
        except (ValueError, KeyError, IndexError, TypeError):
            errors.append(f"{row_id}: close printed no destination")
            continue
        closed.append({"row_id": row_id, "path": row["path"], "new": new, "touched": list(row.get("touchedFiles") or ())})
    return closed, errors


def _land(root: Path, run_id: str, profile: str, closed: Sequence[Mapping[str, Any]], extra: Mapping[str, Any]) -> dict:
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import land_run_grind

    grind_rows = [
        {
            "row_id": c["row_id"], "outcome": "closed",
            "paths": list(dict.fromkeys([*c["touched"], c["new"]])), "deleted_paths": [c["path"]],
        }
        for c in closed
    ]
    params = {"run_id": run_id, "grind": {"profile": profile, "rows": grind_rows}, **extra}
    return land_run_grind.commit_grind_rows(root, resolve_git_common_dir(root), params)


def _record_flags(flag: str, payload: Any) -> list[str]:
    from coordinator_core.ops.dispatch_emit.grind_commit_contract import relay_digest

    token = quote(json.dumps(payload, sort_keys=True), safe="")
    length, digest = relay_digest(token)
    return [f"--{flag}", token, f"--{flag.split('-')[0]}-len", str(length), f"--{flag.split('-')[0]}-sha256", digest]


def _sweep_and_record(
    root: Path, run_id: str, profile: str, profile_dir: str, queue: Sequence[str],
    record: Mapping[str, Any], declared: Optional[Sequence[str]],
) -> tuple[bool, Optional[str], dict]:
    from coordinator_core.backlog_grind_assemble import grind_rows

    argv = ["--profile", profile, "--run-id", run_id, "--repo-root", str(root), "--sweep", "--profile-dir", profile_dir]
    for q in queue:
        argv += ["--queue", q]
    argv += _record_flags("record-urlenc", record)
    if declared:
        argv += _record_flags("declared-urlenc", {"paths": list(declared)})
    code, out, err = _run_verb(grind_rows.cmd_run_record, argv)
    try:
        printed = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except ValueError:
        printed = {}
    if code != 0:
        return False, err[-300:] or f"run-record exit {code}", printed
    swept = printed.get("swept", True) is not False
    return swept, None if swept else str(printed.get("sweep_reason") or "sweep did not complete"), printed


def bookkeep(
    repo_root: Path | str,
    run_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    profile: Optional[str] = None,
    profile_dir: Optional[str] = None,
    queue: Sequence[str] = (),
    record: Optional[Mapping[str, Any]] = None,
    declared: Optional[Sequence[str]] = None,
    run_stamp: Optional[str] = None,
    deliverable_id: Optional[str] = None,
    run_token: Optional[str] = None,
    script_path: Optional[str] = None,
) -> dict:
    """Close, land and ledger-sweep a no-product-edit grind run in-process.

    `rows` are the run's settled rows, each carrying `row_id`, `path`, `digest`, `evidence`,
    `verdict` and `touchedFiles`. Returns `{closed, committed, sha, swept, error}` plus `refused`
    when nothing was written: a row with a product path is refused (`product-path`, row and path named),
    never landed. A row whose close is refused stays open and is named in `error`; the rest still land.
    With `record` the run's ledgers are swept and its run record written and committed whatever the
    close and land did; without it the sweep is skipped (`swept` False, `sweep_skipped` set).
    """
    started = time.perf_counter()
    root = Path(repo_root)
    shape = _shape_error(rows)
    if shape is not None:
        return _reply(error=f"refused: {shape}", refused=REFUSED_PARAMS)
    hit = _product_path(rows)
    if hit is not None:
        return _reply(
            error=f"refused: row {hit[0]} touches product path {hit[1]}; bookkeep settles record paths only",
            refused=REFUSED_PRODUCT_PATH, row_id=hit[0], path=hit[1],
        )
    match = _RUN_STAMP_RE.search(run_id)
    stamp = run_stamp or (match.group(0) if match else "")
    if not profile or not stamp:
        return _reply(error="refused: profile and a run stamp are required", refused=REFUSED_PARAMS)
    if not all(isinstance(s, str) and _SEGMENT_RE.fullmatch(s) and ".." not in s for s in (profile, run_id)):
        return _reply(error="refused: profile and run_id must each be one path segment", refused=REFUSED_PARAMS)
    if profile_dir is None:
        from coordinator_core.resolve_coordinator_clone import resolve_content_root_spawn_free

        content = resolve_content_root_spawn_free()
        profile_dir = str(Path(content) / "queue-profiles") if content else None
    if not profile_dir:
        return _reply(error="refused: profile_dir is required", refused=REFUSED_PARAMS)
    queue_dirs = list(queue) or sorted({PurePosixPath(r["path"].replace("\\", "/")).parent.as_posix() for r in rows})

    closed, errors = _close_rows(root, run_id, rows, profile, profile_dir, stamp)
    out = _reply(closed=[c["row_id"] for c in closed])
    if closed:
        extra = {k: v for k, v in (("deliverable_id", deliverable_id), ("run_token", run_token), ("script_path", script_path)) if v is not None}
        landed = _land(root, run_id, profile, closed, extra)
        out.update(committed=bool(landed.get("committed")), sha=landed.get("sha"))
        for key in ("dropped", "unlanded", "refused"):
            if landed.get(key):
                out[key] = landed[key]
        if landed.get("error"):
            errors.append(f"land: {landed['error']}")
    if record is not None:
        swept, why, printed = _sweep_and_record(root, run_id, profile, profile_dir, queue_dirs, record, declared)
        out["swept"] = swept
        out["record_commit"] = printed.get("commit")
        if why:
            errors.append(f"sweep: {why}")
    else:
        out["sweep_skipped"] = "no run record supplied"
    out["error"] = "; ".join(errors) or None
    out["ms"] = round((time.perf_counter() - started) * 1000, 1)
    return out


#: `schema` of the grind script's returned handback, whose `next_action` this module's completion seam runs.
HANDBACK_SCHEMA = "queue-grind-handback/1"

_NOTIFICATION_RE = re.compile(
    r"<task-notification>.*?<status>completed</status>.*?<result>(?P<result>.*?)</result>.*?</task-notification>",
    re.DOTALL,
)


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _result_object(text: str) -> Optional[Mapping[str, Any]]:
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, Mapping) else None


def completed_next_actions(tail: str) -> list[dict]:
    """The `dispatch.grind_bookkeep` `next_action` of every completed `<task-notification>` in the
    transcript `tail` whose result is a grind handback (`HANDBACK_SCHEMA`); a transcript line is read as
    JSON and its string values searched, a non-JSON line as plain text. Never raises."""
    found: list[dict] = []
    for line in (tail or "").splitlines():
        if HANDBACK_SCHEMA not in line:
            continue
        try:
            texts = list(_strings(json.loads(line)))
        except ValueError:
            texts = [line]
        for text in texts:
            for match in _NOTIFICATION_RE.finditer(text):
                handback = _result_object(match.group("result"))
                action = handback.get("next_action") if handback and handback.get("schema") == HANDBACK_SCHEMA else None
                if isinstance(action, Mapping) and action.get("op") == OP_NAME and isinstance(action.get("params"), Mapping):
                    found.append({"op": OP_NAME, "params": dict(action["params"])})
    return found


def run_next_action(action: Mapping[str, Any]) -> dict:
    """Run one `completed_next_actions` entry through the op. The named `repo_root` must be an absolute
    git work tree; any other op is refused."""
    params = action.get("params") if action.get("op") == OP_NAME else None
    root = params.get("repo_root") if isinstance(params, Mapping) else None
    if not isinstance(params, Mapping) or not isinstance(root, str) or not Path(root).is_absolute() or not (Path(root) / ".git").exists():
        return _reply(error="refused: next_action names no git work tree", refused=REFUSED_PARAMS)
    return _handler(dict(params))


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Close, land and ledger-sweep a no-product-edit grind run in process, with no agent.

    `bookkeep` over the op's params: `repo_root` (worktree path; else the main worktree of the
    dispatch's common dir), `run_id`, `rows`, plus optional `profile` (else `record.profile`),
    `profile_dir`, `queue`, `record`, `declared`, `run_stamp`, `deliverable_id`, `run_token`,
    `script_path`. A deferred run close (`run_close_deferred` false) writes no record."""
    record = params.get("record") if params.get("run_close_deferred", True) else None
    root = params.get("repo_root")
    if root is None and repo_root is not None:
        from coordinator_core.lifecycle import main_worktree_root

        root = main_worktree_root(repo_root)
    run_id = params.get("run_id")
    if root is None or not isinstance(run_id, str) or not run_id:
        return _reply(error="refused: repo_root and run_id are required", refused=REFUSED_PARAMS)
    profile = params.get("profile") or (record or {}).get("profile")
    return bookkeep(
        root, run_id, params.get("rows") or [],
        profile=profile, profile_dir=params.get("profile_dir"), queue=params.get("queue") or (),
        record=record, declared=params.get("declared"), run_stamp=params.get("run_stamp"),
        deliverable_id=params.get("deliverable_id"), run_token=params.get("run_token"),
        script_path=params.get("script_path"),
    )
