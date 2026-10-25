"""Which historical base plus transcript edits reproduces a kept blob byte for byte?"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from coordinator_core.ops.kept_blob_transcripts import ToolEdit

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ZERO_OID = "0" * 40


@dataclass(frozen=True)
class Base:
    """A replay starting point; label is "empty" | "<commit10>:<blob10>" | "base_sha:<blob10>"."""

    label: str
    data: bytes
    since: Optional[datetime]  # None for empty


@dataclass(frozen=True)
class ReplayHit:
    data: bytes
    base: Base
    applied: int
    last_src: Optional[str]


def _git(repo: Path, args: List[str], data: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )


def _parse_time(iso: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(iso.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _log_commits(repo_root: Path, rels: List[str]):
    """One `git log` spawn: [(commit, time, {rel: new_oid})] newest first."""
    out = _git(repo_root, ["-c", "core.quotepath=false", "log", "--all", "--no-abbrev", "--raw", "--no-renames",
                           "--format=%x00%H %cI", "--", *rels])
    commits = []
    if out.returncode != 0:
        return commits
    wanted = set(rels)
    for chunk in out.stdout.decode("utf-8", "replace").split("\x00"):
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n")
        parts = head.split(" ", 1)
        if len(parts) != 2:
            continue
        when = _parse_time(parts[1])
        blobs: Dict[str, str] = {}
        for line in rest.splitlines():
            if not line.startswith(":") or "\t" not in line:
                continue
            meta, _, path = line.partition("\t")
            fields = meta[1:].split()
            if len(fields) < 5 or path not in wanted:
                continue
            new = fields[3]
            if new == _ZERO_OID:
                continue
            blobs[path] = new
        commits.append((parts[0], when, blobs))
    return commits


def _cat_file_batch(repo_root: Path, specs: List[str]) -> List[Optional[bytes]]:
    """Blob bytes for each `<rev>:<rel>` spec via one cat-file --batch spawn; None when missing."""
    out = _git(repo_root, ["cat-file", "--batch"], ("\n".join(specs) + "\n").encode("utf-8"))
    buf = out.stdout
    pos = 0
    result: List[Optional[bytes]] = []
    for _ in specs:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            result.append(None)
            continue
        header = buf[pos:nl].split()
        pos = nl + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            result.append(buf[pos:pos + size])
            pos += size + 1
        else:
            result.append(None)
    return result


def history_bases(repo_root: Path, rels: Iterable[str], base_sha: Optional[str] = None) -> Dict[str, List[Base]]:
    """Per rel path, every historical blob of it plus the empty file."""
    from coordinator_core.git import git_dir, git_objects

    rels = list(rels)
    repo_root = Path(repo_root)
    bases: Dict[str, List[Base]] = {r: [Base("empty", b"", None)] for r in rels}
    if not rels:
        return bases
    commits = _log_commits(repo_root, rels)
    earliest: Dict[str, Dict[str, tuple]] = {r: {} for r in rels}
    for commit, when, blobs in commits:
        for rel, oid in blobs.items():
            prev = earliest[rel].get(oid)
            if prev is None or (when is not None and (prev[1] is None or when < prev[1])):
                earliest[rel][oid] = (commit, when)
    common = git_dir.resolve_git_common_dir(repo_root)
    for rel in rels:
        for oid, (commit, when) in earliest[rel].items():
            obj = git_objects.read_object(common, oid)
            if obj is None or obj[0] != "blob":
                continue
            bases[rel].append(Base(f"{commit[:10]}:{oid[:10]}", obj[1], when))
    if base_sha:
        found = [(c, w) for c, w, _ in commits if c.startswith(base_sha)]
        since = found[0][1] if found else None
        datas = _cat_file_batch(repo_root, [f"{base_sha}:{rel}" for rel in rels])
        for rel, data in zip(rels, datas):
            if data is not None:
                oid = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
                bases[rel].append(Base(f"base_sha:{oid[:10]}", data, since))
    return bases


def _apply_one(text: str, old: str, new: str, replace_all: bool) -> Optional[str]:
    if not old or old not in text:
        return None
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def apply_edit(text: str, edit: ToolEdit) -> Optional[str]:
    """The text after the edit, or None when the edit must be skipped (old_string absent)."""
    inp = edit.input
    if edit.name == "Write":
        content = inp.get("content")
        return content if isinstance(content, str) else None
    if edit.name == "Edit":
        return _apply_one(text, inp.get("old_string") or "", inp.get("new_string") or "",
                          bool(inp.get("replace_all")))
    if edit.name == "MultiEdit":
        work = text
        for sub in inp.get("edits") or []:
            work = _apply_one(work, sub.get("old_string") or "", sub.get("new_string") or "",
                              bool(sub.get("replace_all")))
            if work is None:
                return None
        return work
    return None


def replay_exact(bases: List[Base], edits: List[ToolEdit], kept_sha: str) -> Optional[ReplayHit]:
    """First exact match of kept_sha after any step from any base, else None (lost)."""
    from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

    for base in bases:
        if blob_sha(base.data) == kept_sha:
            return ReplayHit(base.data, base, 0, None)
        try:
            text = base.data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        applied = 0
        last_src: Optional[str] = None
        for edit in edits:
            if base.since is not None and edit.ts <= base.since:
                continue
            nxt = apply_edit(text, edit)
            if nxt is None:
                continue
            text = nxt
            applied += 1
            last_src = edit.src
            data = text.encode("utf-8")
            if blob_sha(data) == kept_sha:
                return ReplayHit(data, base, applied, last_src)
    return None
