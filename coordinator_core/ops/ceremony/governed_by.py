"""Governed-By provenance: trailer builder, parser, land-check rows and the ``governed_by.ledger`` read op."""

from __future__ import annotations

import functools

import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from coordinator_core.ipc import register_op

OP_NAME = "governed_by.ledger"
KEY = "Governed-By"
LEDGER_HEADING = "this file was edited because of the plans found at the following commits:"
DEFAULT_LIMIT = 200
_PLAN_HISTORY_DEPTH = 20
_RS = "\x1e"
_US = "\x1f"
_DIFF_HEADER_RE = re.compile(r"(?m)^diff --git a/(.+) b/\1\r?$")
_LINE_RE = re.compile(rf"^{KEY}: ([0-9a-f]{{7,40}}) (\S+) (.+?)\s*$")
_RUN_ID_TRAILER_RE = re.compile(r"(?m)^(?:Checkpoint-)?Run-Id:[ \t]*(\S+)")
_RUN_ID_TOKEN_RE = re.compile(r"\bwarp-\d{4}-\d{2}-\d{2}-[A-Za-z0-9][\w-]*")
_PLAN_TRAILER_RE = re.compile(r"(?m)^Plan:[ \t]*(.+)$")
_GOVERNING_PATH_RE = re.compile(r"(?<![\w/.-])((?:docs/plans|state/sizings)/[\w./-]+?\.(?:md|ya?ml))(?![\w/-])")


def format_trailer(sha: str, ts: str, path: str) -> str:
    """One ``Governed-By: <sha> <ISO ts> <path>`` trailer line."""
    return f"{KEY}: {sha} {ts} {path.replace(chr(92), '/')}"


def parse_trailers(message: str) -> List[dict]:
    """The ``Governed-By`` entries ``[{sha, ts, path}]`` in ``message``'s final (trailer) paragraph."""
    paragraphs = message.rstrip("\n").split("\n\n")
    out: List[dict] = []
    for line in paragraphs[-1].split("\n"):
        m = _LINE_RE.match(line)
        if m:
            out.append({"sha": m.group(1), "ts": m.group(2), "path": m.group(3)})
    return out


def governing_commits(root: Path, paths: Sequence[str]) -> List[dict]:
    """``[{sha, ts, path}]``: the newest commit touching each of ``paths``, one ``git log`` spawn."""
    wanted = [p.replace("\\", "/") for p in paths if p]
    if not wanted:
        return []
    from coordinator_core.git.run import run_git

    res = run_git(
        [
            "-C", str(root), "-c", "core.quotepath=off", "log", f"--max-count={_PLAN_HISTORY_DEPTH}",
            f"--format={_RS}%H{_US}%cI", "--name-only", "--no-renames", "--", *wanted,
        ]
    )
    if res.returncode != 0:
        return []
    found: dict = {}
    for block in res.stdout.split(_RS):
        head, _, names = block.partition("\n")
        sha, _, ts = head.partition(_US)
        if not sha or not ts:
            continue
        for name in names.split("\n"):
            name = name.strip()
            if name in wanted and name not in found:
                found[name] = {"sha": sha.strip(), "ts": ts.strip(), "path": name}
    return [found[p] for p in wanted if p in found]


def run_governing_paths(root: Path, run_id: str) -> List[str]:
    """Plan and sizing paths the run's ``run.json`` / ``manifest.json`` name; ``[]`` without a record."""
    import json

    from coordinator_core.session import record_homes

    run_dir = Path(record_homes.home_dir(str(root), "warp")) / run_id
    paths: List[str] = []
    for name in ("run.json", "manifest.json"):
        try:
            data = json.loads((run_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ("plan_rel", "plan_path", "sizing_rel"):
            val = data.get(key)
            if isinstance(val, str) and val and val not in paths:
                paths.append(val)
    return paths


def message_run_ids(message: str) -> List[str]:
    """Run ids a message names: ``Run-Id:`` / ``Checkpoint-Run-Id:`` trailers, then ``warp-<date>-...`` tokens."""
    ids: List[str] = []
    for value in [*_RUN_ID_TRAILER_RE.findall(message), *_RUN_ID_TOKEN_RE.findall(message)]:
        if value not in ids:
            ids.append(value)
    return ids


def message_governing_paths(message: str) -> List[str]:
    """Plan and sizing paths named on the message's ``Plan:`` trailer lines; a prose mention does not govern."""
    paths: List[str] = []
    plan_lines = "\n".join(m.group(1) for m in _PLAN_TRAILER_RE.finditer(message))
    for value in _GOVERNING_PATH_RE.findall(plan_lines.replace("\\", "/")):
        if value not in paths:
            paths.append(value)
    return paths


def trailer_lines(root: Path, run_id: Optional[str], extra_paths: Sequence[str] = ()) -> List[str]:
    """``Governed-By`` lines for the files governing ``run_id`` plus ``extra_paths``; never raises."""
    try:
        paths = run_governing_paths(root, run_id) if isinstance(run_id, str) and run_id else []
        paths.extend(p for p in extra_paths if p not in paths)
        return [format_trailer(c["sha"], c["ts"], c["path"]) for c in governing_commits(root, paths)]
    except Exception:  # noqa: BLE001
        return []


def stamp(message: str, root: Path, run_id: Optional[str] = None) -> str:
    """`message` with ``Governed-By`` lines for its run (``run_id`` or the first run id it names) and ``Plan:`` paths."""
    from coordinator_core.ops.ceremony.commit_message import append_trailer_lines

    run = run_id or next((r for r in message_run_ids(message) if run_governing_paths(root, r)), None)
    return append_trailer_lines(message, trailer_lines(root, run, message_governing_paths(message)))


def unresolved_shas(root: Path, shas: Iterable[str]) -> List[str]:
    """The ``shas`` naming no commit object, one ``cat-file --batch-check`` spawn; ``[]`` when git fails."""
    wanted = sorted({s for s in shas if s})
    if not wanted:
        return []
    from coordinator_core.git.run import run_git

    request = "".join(f"{s}^{{commit}}\n" for s in wanted).encode("ascii", "replace")
    res = run_git(["-C", str(root), "cat-file", "--batch-check"], input=request)
    if res.returncode != 0:
        return []
    missing = [ln.split(" ", 1)[0] for ln in res.stdout.splitlines() if ln.endswith(" missing")]
    return sorted({s for s in wanted for m in missing if m == f"{s}^{{commit}}"})


@functools.lru_cache(maxsize=4)
def pending_rows(log_text: str) -> List[dict]:
    """``[{commit, paths, shas}]`` for each agent commit in ``log_text`` that touches code."""
    from coordinator_core.commenting.added_types import SUFFIXES
    from coordinator_core.commenting.authorship import is_agent_commit

    pending: List[dict] = []
    for chunk in log_text.split(_RS):
        parts = chunk.split(_US, 2)
        if len(parts) != 3 or not is_agent_commit(parts[1]):
            continue
        code = sorted({m.group(1) for m in _DIFF_HEADER_RE.finditer(parts[2]) if Path(m.group(1)).suffix.lower() in SUFFIXES})
        if code:
            pending.append({"commit": parts[0].strip(), "paths": code, "shas": [e["sha"] for e in parse_trailers(parts[1])]})
    return pending


def pending_shas(log_text: str) -> List[str]:
    """The sorted distinct trailer shas that `provenance_findings` must resolve for ``log_text``."""
    return sorted({s for row in pending_rows(log_text) for s in row["shas"]})


def unresolved_from_batch(out: bytes, skip: int, shas: Sequence[str]) -> Optional[set]:
    """The ``shas`` that are not commits in ``cat-file --batch`` output, past ``skip`` foreign entries; None when short."""
    pos = 0
    bad: set = set()
    for i in range(skip + len(shas)):
        eol = out.find(b"\n", pos)
        if eol < 0:
            return None
        header = out[pos:eol].split()
        pos = eol + 1
        if len(header) == 3:
            pos += int(header[2]) + 1
        if i >= skip and not (len(header) == 3 and header[1] == b"commit"):
            bad.add(shas[i - skip])
    return bad


def provenance_findings(root: Path, log_text: str, unresolved: Optional[Iterable[str]] = None) -> List[dict]:
    """Major rows ``{commit, paths, severity, scope, reason: missing|unresolved, shas}`` for agent code commits
    lacking a resolvable ``Governed-By``; ``unresolved`` is the pre-resolved bad shas, None spawns one lookup."""
    pending = pending_rows(log_text)
    bad = set(unresolved) if unresolved is not None else set(unresolved_shas(root, [s for row in pending for s in row["shas"]]))
    rows: List[dict] = []
    for row in pending:
        reason = "missing" if not row["shas"] else "unresolved" if bad.intersection(row["shas"]) else None
        if reason:
            rows.append({**row, "severity": "major", "scope": "default-path", "reason": reason})
    return rows


def ledger(root: Path, path: str, limit: int = DEFAULT_LIMIT) -> List[dict]:
    """``[{sha, ts, path, commit}]`` for every ``Governed-By`` trailer on a commit touching ``path``, newest
    first, ``commit`` being the editing commit; one ``git log`` spawn, no per-commit fan-out."""
    from coordinator_core.git.run import run_git

    res = run_git(
        [
            "-C", str(root), "log", f"--max-count={max(1, int(limit))}",
            f"--format={_RS}%H{_US}%B", "--", path.replace("\\", "/"),
        ]
    )
    if res.returncode != 0:
        return []
    entries: List[dict] = []
    for block in res.stdout.split(_RS):
        commit, _, body = block.partition(_US)
        if not commit.strip():
            continue
        for e in parse_trailers(body):
            entries.append({**e, "commit": commit.strip()})
    return entries


def render_ledger(path: str, entries: Sequence[dict]) -> str:
    """The human text: the heading line then one ``<sha> <ts> <path>`` line per entry."""
    return "\n".join([f"{path} {LEDGER_HEADING}", *(f"{e['sha']} {e['ts']} {e['path']}" for e in entries)])


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    params = params or {}
    path = params.get("path")
    if not isinstance(path, str) or not path:
        return {"ok": False, "error": "path must be a non-empty repo-relative string", "refused": "params"}
    given = params.get("repo_root")
    if isinstance(given, str) and given:
        root: Optional[Path] = Path(given)
    elif repo_root is not None:
        from coordinator_core.lifecycle import main_worktree_root

        root = main_worktree_root(repo_root)
    else:
        root = None
    if root is None:
        return {"ok": False, "error": "repo_root is required", "refused": "params"}
    limit = params.get("limit")
    entries = ledger(root, path, limit if isinstance(limit, int) and limit > 0 else DEFAULT_LIMIT)
    return {"ok": True, "path": path, "heading": LEDGER_HEADING, "entries": entries, "text": render_ledger(path, entries)}
