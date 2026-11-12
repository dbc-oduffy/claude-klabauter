"""Deterministic per-file scan digest: what a distill scan agent reads in place of the full file.

Reduction is lossy only over machine content. Frontmatter keeps the metadata keys the scan
brief uses (status, dates, supersession); fenced ``yaml plan-tasks`` / ``yaml evidence`` blocks
collapse to one ``id: title`` line per item. Every other line — all prose and every heading —
is kept byte-for-byte, so no nugget-bearing line is dropped. Pure and offline: no LLM, no git.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

__all__ = ["build_digest", "digest_name", "write_digests", "DIGEST_DIRNAME"]

DIGEST_DIRNAME = "digest"

_FRONTMATTER_KEYS = frozenset(
    {"status", "date", "created", "updated", "last_updated", "completed", "closed",
     "supersedes", "superseded_by", "superseded", "superseded_on"}
)
_MACHINE_INFO = frozenset({"yaml plan-tasks", "yaml evidence", "plan-tasks", "evidence"})
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*(.*?)\s*$")
_ITEM_ID_RE = re.compile(r"^- id:\s*(.*?)\s*$")
_TITLE_RE = re.compile(r"^ {2}title:\s*(.*?)\s*$")


def digest_name(rel_path: str) -> str:
    """Stable digest file name for a worktree-relative path: ``<16 hex of sha256(path)>.md``."""
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:16] + ".md"


def _keep_key(key: str) -> bool:
    return key in _FRONTMATTER_KEYS or key.endswith("_date") or key.startswith("date_")


def _reduce_frontmatter(lines: list[str]) -> list[str]:
    """Keep each kept top-level key together with its continuation lines (indented or list items)."""
    out: list[str] = []
    keeping = False
    for line in lines:
        match = _KEY_RE.match(line)
        if match:
            keeping = _keep_key(match.group(1))
        elif line.strip() and not line[0].isspace() and not line.startswith("- "):
            keeping = False
        if keeping:
            out.append(line)
    return out


def _collapse_block(info: str, body: list[str], eol: str) -> list[str]:
    items: list[str] = []
    current_id: str | None = None
    for line in body:
        raw = line.rstrip("\r\n")
        id_match = _ITEM_ID_RE.match(raw)
        if id_match:
            current_id = id_match.group(1)
            items.append(f"[{info}] {current_id}{eol}")
            continue
        title_match = _TITLE_RE.match(raw)
        if title_match and current_id is not None and items:
            items[-1] = f"[{info}] {current_id}: {title_match.group(1)}{eol}"
            current_id = None
    return items or [f"[{info}] (no items){eol}"]


def build_digest(text: str) -> str:
    """The digest of one file's text. Deterministic; idempotent over its own output."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    start = 0
    if lines and lines[0].rstrip("\r\n") == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
        if close is not None:
            out.append(lines[0])
            out.extend(_reduce_frontmatter(lines[1:close]))
            out.append(lines[close])
            start = close + 1

    i = start
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line.rstrip("\r\n"))
        info = " ".join(fence.group(2).split()) if fence else ""
        if fence and info in _MACHINE_INFO:
            marker = fence.group(1)
            end = next(
                (j for j in range(i + 1, len(lines))
                 if lines[j].strip().startswith(marker) and set(lines[j].strip()) == {marker[0]}),
                None,
            )
            if end is not None:
                eol = "\r\n" if line.endswith("\r\n") else "\n"
                out.extend(_collapse_block(info, lines[i + 1 : end], eol))
                i = end + 1
                continue
        out.append(line)
        i += 1
    return "".join(out)


def write_digests(worktree_root: Path, run_dir_rel: str, rel_paths: list[str]) -> dict[str, tuple[str, int]]:
    """Write ``<run_dir_rel>/digest/<hash>.md`` for each path; return ``{rel_path: (digest_rel_path, bytes)}``.

    A file that cannot be read is omitted — its consumers fall back to the original path.
    Writes are atomic (mkstemp + os.replace) and confined to the run's digest directory."""
    digest_dir = worktree_root / run_dir_rel / DIGEST_DIRNAME
    digest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, tuple[str, int]] = {}
    for rel in rel_paths:
        try:
            text = (worktree_root / rel).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        body = build_digest(text).encode("utf-8")
        name = digest_name(rel)
        fd, tmp = tempfile.mkstemp(dir=str(digest_dir), suffix=".tmp")
        try:
            try:
                os.write(fd, body)
            finally:
                os.close(fd)
            os.replace(tmp, str(digest_dir / name))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            continue
        out[rel] = (f"{run_dir_rel}/{DIGEST_DIRNAME}/{name}", len(body))
    return out
