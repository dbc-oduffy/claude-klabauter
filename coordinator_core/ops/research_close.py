"""
coordinator_core.ops.research_close — JSON-RPC "research.close" op.

Purpose: closes a research run by tier. Scouts first concatenates the run's ``digest-*.md``
files into ``<scratch_dir>/digest.md`` and archives that as its only output. Every tier
copies the run's declared final outputs into ``docs/research/<YYYY-MM-DD>-<topic_slug>/``
and makes exactly one ``commit_paths`` call over the copied paths plus the trail record
``docs/research/trail/<run_id>.yaml`` (built by ``_research_trail``).

Params: ``scratch_dir`` (absolute), ``tier`` (scouts|corpus|deep), ``run_id``,
``topic_slug`` (both safe single path segments), ``value_class`` (optional, in
``rc.VALUE_CLASSES``, default ``tier``), ``sizing`` (optional repo-relative path named in the
record), ``outputs`` (optional list of
scratch-relative file paths; default is every regular file directly in scratch_dir and
directly in each ``segment_dirs`` subdirectory), ``segment_dirs`` (optional safe single
path segments: a multi-pipeline run writes every segment after the first under
``<scratch_dir>/<pipeline>/``, archived at the same relative path), ``local_root``
(optional, absolute: a local-only run archives to ``<local_root>/<YYYY-MM-DD>-<topic_slug>/``
outside every git checkout and commits only its trail record, which carries no paths and no
content; the reply's ``claims_out`` is the ``claims-emit --out`` stem there), ``require_claims`` (optional list of non-empty scratch-relative paths, each of
which must hold a non-empty JSON array; otherwise the close is refused with
``deliverable: false`` before any copy or commit).

Before any copy, every output is scanned for secret-shaped values (a run can read a
third-party tree that commits credentials); a hit refuses the close naming file, line and
pattern, never the value. After the commit, untracked files under ``docs/research/`` written
since the run began and outside the destination are reported as ``strays``: a member that
wrote its output elsewhere left it uncommitted and unarchived.

Idempotency: a re-run whose destination dir already holds byte-equal copies of every
output and the identical trail record returns the sha of the commit that landed them (found
by walking HEAD for the close subject); when no such commit is found, missing files are
written and the commit is made. A destination file or trail record holding different bytes
is refused, never overwritten; a re-close on a later day names a new destination dir, so
its record differs and is refused.

Negative-spec:
  - Never calls ``ceremony.scoped_git_commit`` or ``run_commit_pipeline``; one
    ``commit_paths`` call or none.
  - Zero spawns on the clean path; one (``hash_worktree_blobs_via_spawn``) only on a
    CRLF-pinned path.
  - A missing output is a structured refusal before any copy or commit.
"""

from __future__ import annotations

import asyncio
import datetime
import json
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
from coordinator_core.ops import _research_trail
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


def _build_digest(scratch: Path) -> Optional[str]:
    """Concatenate ``digest-*.md`` into ``scratch/digest.md``; an error string when none exist."""
    digests = sorted(p for p in scratch.glob("digest-*.md") if p.is_file())
    if not digests:
        return f"no digest-*.md under {str(scratch)!r}"
    body = "\n\n".join(p.read_text(encoding="utf-8").rstrip("\n") for p in digests) + "\n"
    (scratch / "digest.md").write_text(body, encoding="utf-8", newline="\n")
    return None


def _resolve_outputs(
    scratch: Path, outputs: Optional[list], segment_dirs: list[str]
) -> tuple[list[Path], Optional[str]]:
    if outputs is None:
        found = sorted(p for p in scratch.iterdir() if p.is_file())
        for name in segment_dirs:
            seg = scratch / name
            if not seg.is_dir():
                return [], f"segment output dir missing: {name!r}"
            found.extend(sorted(p for p in seg.iterdir() if p.is_file()))
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
    worktree: Path,
    scratch: Path,
    run_id: str,
    topic_slug: str,
    outputs: Optional[list],
    segment_dirs: list[str],
    meta: dict,
    local_root_raw: Optional[str] = None,
) -> dict:
    """Archive the outputs and commit the trail record: the one commit site of this op.

    A repo run commits the copies and the record in one ``commit_paths`` call; a local-only
    run archives under its ``local_root`` and commits only the record.
    """
    local = bool(local_root_raw)
    if local:
        from coordinator_core.ops import _research_local

        try:
            archive_root = _research_local.outside_checkouts(Path(local_root_raw), "local_root")
        except _research_local.LocalRootError as exc:
            return _err(str(exc))
    else:
        archive_root = worktree / _RESEARCH_DIR
    sources, problem = _resolve_outputs(scratch, outputs, segment_dirs)
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

    dest_dir = archive_root / f"{datetime.date.today().isoformat()}-{topic_slug}"
    if contained_path(dest_dir, [archive_root]) is None:
        return _err(f"destination escapes {str(archive_root)!r}: {str(dest_dir)!r}")

    pairs = [(src, dest_dir / src.relative_to(scratch)) for src in sources]
    rel_paths = [] if local else [dst.relative_to(worktree).as_posix() for _, dst in pairs]
    try:
        trail_rel = _research_trail.trail_rel(run_id)
        trail_text = _research_trail.render(
            _research_trail.record(
                run_id=run_id,
                sizing=meta["sizing"],
                value_class=meta["value_class"],
                tier=meta["tier"],
                destination="local-only" if local else "repo",
                paths=rel_paths,
                output_count=len(sources),
            )
        )
    except ValueError as exc:
        return _err(f"trail record refused: {exc}")
    trail_path = worktree / trail_rel
    trail_bytes = trail_text.encode("utf-8")
    if trail_path.exists() and (not trail_path.is_file() or trail_path.read_bytes() != trail_bytes):
        return _err(
            f"trail record exists with different content, refusing to overwrite: {trail_rel}",
            dest=trail_rel,
        )
    for src, dst in pairs:
        if dst.exists() and (not dst.is_file() or dst.read_bytes() != src.read_bytes()):
            return _err(
                f"destination exists with different content, refusing to overwrite: {str(dst)!r}",
                dest=str(dest_dir),
            )
    subject = f"research({topic_slug}): close {run_id}"

    landed = trail_path.is_file() and all(dst.is_file() for _, dst in pairs)
    sha = _landed_sha(worktree, subject) if landed else None
    if sha is None:
        for src, dst in pairs:
            if not dst.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        trail_path.parent.mkdir(parents=True, exist_ok=True)
        if not trail_path.is_file():
            trail_path.write_bytes(trail_bytes)
        commit_rel = [*rel_paths, trail_rel]
        message = apply_missing_trailers(subject + "\n", worktree, commit_rel)
        try:
            outcome = commit_paths(
                worktree,
                commit_rel,
                message,
                blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree),
            )
            sha = outcome.sha
        except IndexStaleAfterCommit as exc:
            sha = getattr(getattr(exc, "outcome", None), "sha", None)
        except NothingToCommit:
            sha = head_sha(worktree)
        except (CommitRefused, FilterUnsupported, IndexWriteError) as exc:
            return _err(f"commit refused: {exc}", paths=commit_rel)
    if local:
        return {
            "exit_code": 0,
            "committed": True,
            "sha": sha or "",
            "local_only": True,
            "destination": dest_dir.as_posix(),
            "paths": [dst.as_posix() for _, dst in pairs],
            "trail": trail_rel,
            "claims_out": (dest_dir / topic_slug).as_posix(),
            "run_id": run_id,
            "deliverable": True,
        }
    return _with_strays(
        {
            "exit_code": 0, "committed": True, "deliverable": True, "sha": sha or "",
            "paths": rel_paths, "trail": trail_rel,
        },
        worktree, dest_dir, since,
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


def _missing_claims(scratch: Path, require_claims) -> Optional[dict]:
    """A refusal reply when a ``require_claims`` path is not a non-empty JSON array, else None."""
    if require_claims is None:
        return None
    if not isinstance(require_claims, list) or not all(
        isinstance(r, str) and r for r in require_claims
    ):
        return _err("require_claims must be a list of non-empty scratch-relative paths")
    for rel in require_claims:
        p = contained_path(scratch / rel, [scratch])
        if p is None:
            return _err(f"require_claims entry escapes scratch_dir: {rel!r}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, list) or not data:
            return _err(f"run is not deliverable: {rel} holds no claims", deliverable=False)
    return None


def _close_sync(worktree: Path, params: dict) -> dict:
    tier = str(params.get("tier") or "")
    run_id = str(params.get("run_id") or "")
    topic_slug = str(params.get("topic_slug") or "")
    scratch_raw = str(params.get("scratch_dir") or "")
    outputs = params.get("outputs")
    value_class = str(params.get("value_class") or tier)
    sizing = params.get("sizing")

    if tier not in rc.TIERS:
        return _err(f"tier must be one of {list(rc.TIERS)}: {tier!r}")
    if value_class not in rc.VALUE_CLASSES:
        return _err(f"value_class must be one of {list(rc.VALUE_CLASSES)}: {value_class!r}")
    if sizing is not None and not isinstance(sizing, str):
        return _err(f"sizing must be a repo-relative path string: {sizing!r}")
    if not scratch_raw:
        return _err("missing required param: scratch_dir")
    scratch = Path(scratch_raw)
    if not scratch.is_dir():
        return _err(f"scratch_dir is not a directory: {scratch_raw!r}")
    for name, value in (("run_id", run_id), ("topic_slug", topic_slug)):
        if not value or not safe_id(value):
            return _err(f"{name} is not a safe path segment: {value!r}")
    if outputs is not None and not isinstance(outputs, list):
        return _err("outputs must be a list of scratch-relative paths")
    segment_dirs = params.get("segment_dirs") or []
    if not isinstance(segment_dirs, list) or not all(
        isinstance(d, str) and safe_id(d) for d in segment_dirs
    ):
        return _err(f"segment_dirs must be a list of safe path segments: {segment_dirs!r}")
    problem = _missing_claims(scratch, params.get("require_claims"))
    if problem:
        return problem
    if tier == "scouts":
        problem = _build_digest(scratch)
        if problem:
            return _err(problem)
        outputs, segment_dirs = ["digest.md"], []
    meta = {"tier": tier, "value_class": value_class, "sizing": sizing}
    local_root_raw = params.get("local_root")
    return _close_committed(
        worktree, scratch, run_id, topic_slug, outputs, segment_dirs, meta,
        str(local_root_raw) if local_root_raw else None,
    )


@register_op("research.close")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "research.close" handler.

    Params: see the module docstring; ``run_id`` and ``topic_slug`` are required for every tier.

    Returns (exit_code 0): ``{committed: true, deliverable: true, sha, paths, trail}``;
    local-only adds ``local_only``, ``destination``, ``claims_out``. Returns (exit_code 1):
    ``{committed: false, error}``; a ``require_claims`` refusal adds ``deliverable: false``.
    """
    if repo_root is None:
        return _err("repo_root (engine common_dir) is required for research.close")
    worktree = main_worktree_root(repo_root)
    return await asyncio.to_thread(_close_sync, worktree, params)
