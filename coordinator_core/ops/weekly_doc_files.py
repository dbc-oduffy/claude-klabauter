"""Adapter over example-retrieval-repo's doc_files read-contract views, with a git fallback.

The four views (`plans_completed`, `plan_footprint`, `paths_to_docs`, `plan_preview`) are
read-only sqlite views owned by example-retrieval-repo; this module only SELECTs from them. With no
resolvable index the same four functions answer from git: a `git log` + `git diff-tree --stdin`
pair for completions (deletions under docs/plans), one `git cat-file --batch` for previews.

Cost trap: never path-limit `git log` over docs/plans (history simplification costs seconds);
diff-tree with a pathspec prunes the walk. Previews are batched, never one spawn per plan.
`plan_footprint` and `paths_to_docs` are the C10 contract surface with no claude-klabauter caller yet.

Assumed view columns (contract not yet landed; adjust here only):
  plans_completed:  plan, sha, summary, sizing, ranked, completed_at (ISO date or datetime)
  plan_footprint:   plan, path
  paths_to_docs:    path, doc
  plan_preview:     plan, sha, preview
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.win_portability import no_console_creationflags

VIEW_PLANS_COMPLETED = "plans_completed"
VIEW_PLAN_FOOTPRINT = "plan_footprint"
VIEW_PATHS_TO_DOCS = "paths_to_docs"
VIEW_PLAN_PREVIEW = "plan_preview"

PREVIEW_CAP = 10000
INDEX_RELPATH = "project-rag/indexes/claude-klabauter/doc_files.sqlite"
PLANS_DIR = "docs/plans"
_GIT_TIMEOUT = 30
_DOC_PREFIXES = ("docs/wiki/", "docs/atlas/")
_WRITES_RE = re.compile(r"^\s*writes:\s*\[([^\]]*)\]", re.MULTILINE)


def resolve_index_path(index_path: Optional[os.PathLike] = None) -> Optional[Path]:
    """Return the doc_files db to read, or None when the git fallback applies.

    An explicit `index_path` wins; otherwise the settings-home key INDEX_RELPATH. An
    unresolvable settings home or an absent file yields None, never an error.
    """
    if index_path is not None:
        p = Path(index_path)
        return p if p.is_file() else None
    try:
        from coordinator_core._settings_home import settings_home

        p = settings_home() / INDEX_RELPATH
    except Exception:
        return None
    return p if p.is_file() else None


def _select(db: Path, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _git(repo: Path, *args: str, stdin: Optional[str] = None) -> Optional[str]:
    """Run `git -C repo <args>`; stdout on success, None on any failure."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def plans_completed(
    since: str,
    until: str,
    *,
    index_path: Optional[os.PathLike] = None,
    repo: Optional[os.PathLike] = None,
) -> List[Dict[str, object]]:
    """Plans completed in [since, until): dicts of plan, sha, summary, sizing, ranked.

    View path carries sizing/ranked; the git fallback leaves both None.
    """
    db = resolve_index_path(index_path)
    if db is not None:
        rows = _select(
            db,
            f"SELECT plan, sha, summary, sizing, ranked FROM {VIEW_PLANS_COMPLETED} "
            "WHERE completed_at >= ? AND completed_at < ? ORDER BY completed_at",
            (since, until),
        )
        return [dict(r) for r in rows]
    root = Path(repo) if repo is not None else Path.cwd()
    log = _git(
        root, "log", f"--since={since}", f"--until={until}", "--format=%H%x09%s"
    )
    subjects: Dict[str, str] = {}
    for ln in (log or "").splitlines():
        sha, _, summary = ln.partition("\t")
        if sha:
            subjects[sha] = summary
    if not subjects:
        return []
    deleted = _git(
        root, "diff-tree", "--stdin", "-r", "--name-only", "--diff-filter=D", "--",
        PLANS_DIR, stdin="\n".join(subjects) + "\n",
    )
    result: List[Dict[str, object]] = []
    sha = ""
    for ln in (deleted or "").splitlines():
        ln = ln.strip()
        if ln in subjects:
            sha = ln
        elif ln and sha:
            result.append(
                {"plan": ln, "sha": sha, "summary": subjects[sha], "sizing": None, "ranked": None}
            )
    return result


def _plan_texts(specs: List[Tuple[str, Optional[str]]], repo: Path) -> List[str]:
    """Plan text per (plan, sha) via one `git cat-file --batch`; "" when unreadable.

    A plan deleted at `sha` is read from its parent; sha None reads HEAD.
    """
    if not specs:
        return []
    wanted: List[List[str]] = [
        [f"{sha}:{plan}", f"{sha}^:{plan}"] if sha else [f"HEAD:{plan}"] for plan, sha in specs
    ]
    flat = [w for group in wanted for w in group]
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            input="\n".join(flat).encode("utf-8") + b"\n",
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return [""] * len(specs)
    data, pos = r.stdout, 0
    texts: List[str] = []
    for _ in flat:
        eol = data.find(b"\n", pos)
        if eol < 0:
            texts.append("")
            continue
        header = data[pos:eol].split()
        pos = eol + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            texts.append(data[pos : pos + size].decode("utf-8", errors="replace"))
            pos += size + 1
        else:
            texts.append("")
    out: List[str] = []
    i = 0
    for group in wanted:
        chunk = texts[i : i + len(group)]
        i += len(group)
        out.append(next((t for t in chunk if t), ""))
    return out


def _plan_text(plan: str, sha: Optional[str], repo: Path) -> str:
    """Single-plan read; with no sha, a plan deleted at HEAD is found by one path-limited log.

    The path-limited log is slow, so it stays out of the batched picker path.
    """
    text = _plan_texts([(plan, sha)], repo)[0]
    if text or sha:
        return text
    last = _git(repo, "log", "-1", "--format=%H", "--", plan)
    if last and last.strip():
        return _plan_texts([(plan, last.strip())], repo)[0]
    return ""


def plan_footprint(
    plan: str,
    *,
    index_path: Optional[os.PathLike] = None,
    repo: Optional[os.PathLike] = None,
) -> List[str]:
    """Paths the plan writes. The git fallback reads its `writes: [...]` rows."""
    db = resolve_index_path(index_path)
    if db is not None:
        rows = _select(db, f"SELECT path FROM {VIEW_PLAN_FOOTPRINT} WHERE plan = ?", (plan,))
        return [r["path"] for r in rows]
    root = Path(repo) if repo is not None else Path.cwd()
    paths: List[str] = []
    for m in _WRITES_RE.finditer(_plan_text(plan, None, root)):
        for item in m.group(1).split(","):
            item = item.strip().strip("\"'")
            if item and item not in paths:
                paths.append(item)
    return paths


def paths_to_docs(
    paths: List[str],
    *,
    index_path: Optional[os.PathLike] = None,
) -> Dict[str, List[str]]:
    """Map each path to the wiki pages / atlas systems that cover it.

    The git fallback has no coverage graph: only a path already under a docs prefix maps (to itself).
    """
    db = resolve_index_path(index_path)
    result: Dict[str, List[str]] = {p: [] for p in paths}
    if db is not None:
        for p in paths:
            rows = _select(db, f"SELECT doc FROM {VIEW_PATHS_TO_DOCS} WHERE path = ?", (p,))
            result[p] = [r["doc"] for r in rows]
        return result
    for p in paths:
        if p.startswith(_DOC_PREFIXES):
            result[p] = [p]
    return result


def plan_previews(
    specs: List[Tuple[str, Optional[str]]],
    *,
    index_path: Optional[os.PathLike] = None,
    repo: Optional[os.PathLike] = None,
) -> List[str]:
    """`plan_preview` for many (plan, sha) pairs; the git fallback costs one spawn in total."""
    db = resolve_index_path(index_path)
    if db is not None:
        return [plan_preview(p, s, index_path=db) for p, s in specs]
    root = Path(repo) if repo is not None else Path.cwd()
    return [t[:PREVIEW_CAP] for t in _plan_texts(specs, root)]


def plan_preview(
    plan: str,
    sha: Optional[str] = None,
    *,
    index_path: Optional[os.PathLike] = None,
    repo: Optional[os.PathLike] = None,
) -> str:
    """Plan text capped at PREVIEW_CAP chars; reads a deleted plan at `sha`."""
    db = resolve_index_path(index_path)
    if db is not None:
        if sha:
            rows = _select(
                db,
                f"SELECT preview FROM {VIEW_PLAN_PREVIEW} WHERE plan = ? AND sha = ?",
                (plan, sha),
            )
        else:
            rows = _select(db, f"SELECT preview FROM {VIEW_PLAN_PREVIEW} WHERE plan = ?", (plan,))
        return (rows[0]["preview"] or "")[:PREVIEW_CAP] if rows else ""
    root = Path(repo) if repo is not None else Path.cwd()
    return _plan_text(plan, sha, root)[:PREVIEW_CAP]
