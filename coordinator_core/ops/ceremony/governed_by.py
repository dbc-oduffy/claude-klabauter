"""Governed-By provenance ledger: trailer builder, parser and the ``governed_by.ledger`` read op.

A commit's ``Governed-By: <sha> <ISO ts> <path>`` trailer names the commit that landed the governing plan or
sizing file, never the path alone (distillation deletes plan files; the commit is the durable record). No side
file is stored: the ledger is derived from ``git log`` trailers.

``governed_by.ledger`` (COMPUTE_ONLY): params ``repo_root`` (optional), ``path`` (required, repo-relative),
``limit`` (optional, default 200). One ``git log`` spawn. Reply ``{ok, path, heading, entries}`` where each entry
is ``{sha, ts, path, commit}``.
"""

from __future__ import annotations

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
_LINE_RE = re.compile(rf"^{KEY}: ([0-9a-f]{{7,40}}) (\S+) (.+?)\s*$")


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
    """``[{sha, ts, path}]``: the newest commit that touched each of ``paths`` (one ``git log`` spawn). A path
    never committed contributes nothing, so an unlanded plan yields no unresolvable link."""
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
    """Repo-relative plan and sizing paths the run record names (``run.json`` then ``manifest.json``: keys
    ``plan_rel``, ``plan_path``, ``sizing_rel``); ``[]`` when the run has no readable record."""
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


def trailer_lines(root: Path, run_id: Optional[str]) -> List[str]:
    """The ``Governed-By`` lines for the plan and sizing files governing ``run_id``; ``[]`` when the run id is
    unknown or nothing governing is committed. Never raises: provenance is stamped, not gated, at commit time."""
    if not isinstance(run_id, str) or not run_id:
        return []
    try:
        paths = run_governing_paths(root, run_id)
        return [format_trailer(c["sha"], c["ts"], c["path"]) for c in governing_commits(root, paths)]
    except Exception:  # noqa: BLE001 -- an unreadable record stamps nothing; it never blocks a landing
        return []


def unresolved_shas(root: Path, shas: Iterable[str]) -> List[str]:
    """The ``shas`` that name no commit object, from one ``cat-file --batch-check`` spawn."""
    wanted = sorted({s for s in shas if s})
    if not wanted:
        return []
    from coordinator_core.git.run import run_git

    res = run_git(["-C", str(root), "cat-file", "--batch-check"], input="".join(f"{s}^{{commit}}\n" for s in wanted).encode("ascii", "replace"))
    if res.returncode != 0:
        return wanted
    missing = [ln.split(" ", 1)[0] for ln in res.stdout.splitlines() if ln.endswith(" missing")]
    return sorted({s for s in wanted for m in missing if m == f"{s}^{{commit}}"})


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
