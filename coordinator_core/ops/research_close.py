"""
coordinator_core.ops.research_close — JSON-RPC "research.close" op.

Purpose: closes a research run by tier. Scouts concatenates the run's ``digest-*.md``
files into ``<scratch_dir>/digest.md`` and commits nothing. Corpus and deep copy the
run's declared final outputs into ``docs/research/<YYYY-MM-DD>-<topic_slug>/`` and make
exactly one ``commit_paths`` call over the copied paths.

Params: ``scratch_dir`` (absolute), ``tier`` (scouts|corpus|deep), ``run_id``,
``topic_slug`` (both safe single path segments), ``outputs`` (optional list of
scratch-relative file paths; default is every regular file directly in scratch_dir).

Before any copy, every output is scanned for secret-shaped values (a run can read a
third-party tree that commits credentials); a hit refuses the close naming file, line and
pattern, never the value. After the commit, untracked files under ``docs/research/`` written
since the run began and outside the destination are reported as ``strays``: a member that
wrote its output elsewhere left it uncommitted and unarchived.

Idempotency: a re-run whose destination dir already holds byte-equal copies of every
output returns the sha of the commit that landed them (found by walking HEAD for the
close subject); when no such commit is found, missing copies are written and the commit
is made. A destination file holding different bytes is refused, never overwritten.

Negative-spec:
  - Never calls ``ceremony.scoped_git_commit`` or ``run_commit_pipeline``; one
    ``commit_paths`` call or none.
  - Zero spawns on the clean path; one (``hash_worktree_blobs_via_spawn``) only on a
    CRLF-pinned path.
  - Scouts writes nothing outside ``scratch_dir``.
  - A missing output is a structured refusal before any copy or commit.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import re
import shutil
from functools import partial
from pathlib import Path
from typing import Optional

from coordinator_core.git import commit_walk
from coordinator_core.git.commit import (
    CommitRefused,
    FilterUnsupported,
    NothingToCommit,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_index import IndexParseError, parse_index_stat
from coordinator_core.git.git_state import head_sha
from coordinator_core.git.index_write import IndexStaleAfterCommit, IndexWriteError
from coordinator_core.ipc import register_op
from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.fleet._common import main_worktree_root

_RESEARCH_DIR = Path("docs") / "research"
_WALK_LIMIT = 200

#: Secret-shaped values, by provider format or by an assignment to a secret-named key whose
#: value mixes letters and digits. Names only: a doc that records a key's NAME passes.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("OpenAI/Anthropic key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}")),
    (
        "secret-named assignment",
        re.compile(
            r"(?i)\b[\w.-]*(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
            r"[\w.-]*[\"']?\s*[:=]\s*[\"']?(?=[A-Za-z0-9/+_-]*\d)(?=[A-Za-z0-9/+_-]*[A-Za-z])"
            r"[A-Za-z0-9/+_-]{16,}"
        ),
    ),
)


def _err(error: str, **extra) -> dict:
    out = {"exit_code": 1, "committed": False, "error": error}
    out.update(extra)
    return out


def _close_scouts(scratch: Path) -> dict:
    digests = sorted(p for p in scratch.glob("digest-*.md") if p.is_file())
    if not digests:
        return _err(f"no digest-*.md under {str(scratch)!r}")
    body = "\n\n".join(p.read_text(encoding="utf-8").rstrip("\n") for p in digests) + "\n"
    target = scratch / "digest.md"
    target.write_text(body, encoding="utf-8", newline="\n")
    return {"exit_code": 0, "committed": False, "digest_path": target.as_posix()}


def _resolve_outputs(scratch: Path, outputs: Optional[list]) -> tuple[list[Path], Optional[str]]:
    if outputs is None:
        found = sorted(p for p in scratch.iterdir() if p.is_file())
        if not found:
            return [], f"no output files directly under {str(scratch)!r}"
        return found, None
    resolved: list[Path] = []
    for rel in outputs:
        if not isinstance(rel, str) or not rel:
            return [], f"outputs entry is not a non-empty string: {rel!r}"
        p = contained_path(scratch / rel, [scratch])
        if p is None:
            return [], f"output escapes scratch_dir: {rel!r}"
        if not p.is_file():
            return [], f"missing scratch output: {rel!r}"
        resolved.append(p)
    if not resolved:
        return [], "outputs is empty"
    return resolved, None


def _secret_hits(sources: list[Path], scratch: Path) -> list[str]:
    hits: list[str] = []
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append(f"{src.relative_to(scratch).as_posix()}:{lineno}: {name}")
                    break
    return hits


def _strays(worktree: Path, dest_dir: Path, since: float) -> list[str]:
    """Untracked files under docs/research/, outside `dest_dir`, modified at or after `since`."""
    root = worktree / _RESEARCH_DIR
    if not root.is_dir():
        return []
    try:
        tracked = set(parse_index_stat(worktree))
    except (IndexParseError, OSError):
        return []
    found: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        here = Path(dirpath)
        if here == dest_dir or dest_dir in here.parents:
            continue
        for name in files:
            path = here / name
            rel = path.relative_to(worktree).as_posix()
            try:
                if rel not in tracked and path.stat().st_mtime >= since:
                    found.append(rel)
            except OSError:
                continue
    return sorted(found)


def _landed_sha(worktree: Path, subject: str) -> Optional[str]:
    common = resolve_git_common_dir(worktree)
    head = head_sha(worktree)
    if head is None:
        return None
    for i, (sha, commit) in enumerate(commit_walk.walk(common, head)):
        if i >= _WALK_LIMIT:
            break
        if (commit.get("message") or "").split("\n", 1)[0].strip() == subject:
            return sha
    return None


def _close_committed(
    worktree: Path, scratch: Path, run_id: str, topic_slug: str, outputs: Optional[list]
) -> dict:
    sources, problem = _resolve_outputs(scratch, outputs)
    if problem:
        return _err(problem)
    hits = _secret_hits(sources, scratch)
    if hits:
        return _err(
            "secret-shaped values in the run's outputs; redact them in scratch (record key names "
            "only) and re-run research.close",
            secret_hits=hits,
        )
    since = min(p.stat().st_mtime for p in sources)

    research_root = worktree / _RESEARCH_DIR
    dest_dir = research_root / f"{datetime.date.today().isoformat()}-{topic_slug}"
    if contained_path(dest_dir, [research_root]) is None:
        return _err(f"destination escapes docs/research: {str(dest_dir)!r}")

    pairs = [(src, dest_dir / src.relative_to(scratch)) for src in sources]
    rel_paths = [dst.relative_to(worktree).as_posix() for _, dst in pairs]
    subject = f"research({topic_slug}): close {run_id}"

    if dest_dir.exists():
        for src, dst in pairs:
            if dst.exists() and (not dst.is_file() or dst.read_bytes() != src.read_bytes()):
                return _err(
                    f"destination exists with different content, refusing to overwrite: {str(dst)!r}",
                    dest=str(dest_dir),
                )
        if all(dst.is_file() for _, dst in pairs):
            sha = _landed_sha(worktree, subject)
            if sha is not None:
                return _with_strays(
                    {"exit_code": 0, "committed": True, "sha": sha, "paths": rel_paths},
                    worktree, dest_dir, since,
                )

    for src, dst in pairs:
        if dst.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    message = apply_missing_trailers(subject + "\n", worktree, rel_paths)
    try:
        outcome = commit_paths(
            worktree,
            rel_paths,
            message,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree),
        )
        sha = outcome.sha
    except IndexStaleAfterCommit as exc:
        sha = getattr(getattr(exc, "outcome", None), "sha", None)
    except NothingToCommit:
        sha = head_sha(worktree)
    except (CommitRefused, FilterUnsupported, IndexWriteError) as exc:
        return _err(f"commit refused: {exc}", paths=rel_paths)
    return _with_strays(
        {"exit_code": 0, "committed": True, "sha": sha or "", "paths": rel_paths}, worktree, dest_dir, since
    )


def _with_strays(result: dict, worktree: Path, dest_dir: Path, since: float) -> dict:
    strays = _strays(worktree, dest_dir, since)
    if strays:
        result["strays"] = strays
        result["warning"] = (
            f"{len(strays)} untracked docs/research file(s) written during this run sit outside "
            f"{dest_dir.relative_to(worktree).as_posix()}; move each into the run's scratch dir "
            "and re-close, or delete it"
        )
    return result


def _close_sync(worktree: Path, params: dict) -> dict:
    tier = str(params.get("tier") or "")
    run_id = str(params.get("run_id") or "")
    topic_slug = str(params.get("topic_slug") or "")
    scratch_raw = str(params.get("scratch_dir") or "")
    outputs = params.get("outputs")

    if tier not in rc.TIERS:
        return _err(f"tier must be one of {list(rc.TIERS)}: {tier!r}")
    if not scratch_raw:
        return _err("missing required param: scratch_dir")
    scratch = Path(scratch_raw)
    if not scratch.is_dir():
        return _err(f"scratch_dir is not a directory: {scratch_raw!r}")
    if tier == "scouts":
        return _close_scouts(scratch)
    for name, value in (("run_id", run_id), ("topic_slug", topic_slug)):
        if not value or not safe_id(value):
            return _err(f"{name} is not a safe path segment: {value!r}")
    if outputs is not None and not isinstance(outputs, list):
        return _err("outputs must be a list of scratch-relative paths")
    return _close_committed(worktree, scratch, run_id, topic_slug, outputs)


@register_op("research.close")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "research.close" handler.

    Params: ``scratch_dir`` (absolute), ``tier`` (scouts|corpus|deep), ``run_id``,
    ``topic_slug`` (both safe single path segments), ``outputs`` (optional list of
    scratch-relative file paths; default every regular file directly in scratch_dir).

    Returns (exit_code 0): scouts -> ``{committed: false, digest_path}``; corpus/deep ->
    ``{committed: true, sha, paths}``. Returns (exit_code 1): ``{committed: false, error}``.
    """
    if repo_root is None:
        return _err("repo_root (engine common_dir) is required for research.close")
    worktree = main_worktree_root(repo_root)
    return await asyncio.to_thread(_close_sync, worktree, params)
