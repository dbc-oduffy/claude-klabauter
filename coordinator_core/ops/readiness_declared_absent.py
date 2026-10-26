"""A plan's declared-absent path set and the classifier that rules an UNRESOLVED premise citation on it.

Two sources: every spine-row ``writes:`` path that does not exist at the base sha (a file the plan
creates), and the plan frontmatter ``absent_at_head: [path, ...]`` list (deletions, retired
surfaces). An ``absent_at_head`` path that does exist at the base sha is a wrong premise, kept apart
as ``present_at_head`` so the classifier blocks on it. Fail-closed: when the base-sha read fails
nothing is declared absent, so nothing is exempted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

EXPECTED_ABSENT = "expected-absent"
BLOCKS = "blocks"
DECLARED_ABSENT_BUT_PRESENT = "declared-absent-but-present-at-head"


@dataclass(frozen=True)
class DeclaredAbsent:
    """``absent``: posix paths the plan declares absent and that are absent at the base sha.
    ``present_at_head``: ``absent_at_head`` paths that exist at the base sha."""

    absent: frozenset = frozenset()
    present_at_head: frozenset = frozenset()


def normalize_path(value: Any) -> str:
    """Posix-normalised repo-relative form of ``value``; "" for anything that is not a usable
    relative path (non-string, empty, absolute, drive-lettered, ``..``-escaping, glob, directory)."""
    if not isinstance(value, str):
        return ""
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text or text.endswith("/") or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return ""
    if any(ch in text for ch in "*?[") or ".." in text.split("/"):
        return ""
    return text


def _row_writes(rows: Any) -> list:
    out: list = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        writes = row.get("writes") if isinstance(row, dict) else None
        if isinstance(writes, str):
            writes = [writes]
        if isinstance(writes, list):
            out.extend(p for p in (normalize_path(w) for w in writes) if p)
    return out


def _existing_at(repo_root: Path, base_sha: str, paths: Iterable[str]) -> Optional[frozenset]:
    """The subset of ``paths`` present in ``base_sha``'s tree, in one ``git ls-tree`` spawn; None
    when git cannot answer."""
    wanted = sorted(set(paths))
    if not wanted:
        return frozenset()
    from coordinator_core.git.run import run_git
    from coordinator_core.ipc import CEREMONY_BUDGET_SECS

    done = run_git(
        ["--literal-pathspecs", "-C", str(repo_root), "ls-tree", "-r", "--name-only", "-z", base_sha, "--", *wanted],
        timeout=CEREMONY_BUDGET_SECS,
        binary=True,
    )
    if done.returncode != 0:
        return None
    return frozenset(p for p in done.stdout_bytes.decode("utf-8", "replace").split("\0") if p)


def declared_absent_paths(
    repo_root: Path, base_sha: Optional[str], spine_rows: Any, frontmatter: Any
) -> DeclaredAbsent:
    """The plan's declared-absent set at ``base_sha``; empty when ``base_sha`` is missing or git
    cannot read it."""
    if not isinstance(base_sha, str) or not base_sha.strip():
        return DeclaredAbsent()
    listed = frontmatter.get("absent_at_head") if isinstance(frontmatter, dict) else None
    listed_paths = [p for p in (normalize_path(v) for v in listed) if p] if isinstance(listed, list) else []
    writes = _row_writes(spine_rows)
    existing = _existing_at(Path(repo_root), base_sha.strip(), [*writes, *listed_paths])
    if existing is None:
        return DeclaredAbsent()
    absent = {p for p in writes if p not in existing}
    absent.update(p for p in listed_paths if p not in existing)
    return DeclaredAbsent(frozenset(absent), frozenset(p for p in listed_paths if p in existing))


def _cites(citation: str, path: str) -> bool:
    return re.search(r"(?<![\w./-])" + re.escape(path) + r"(?![\w/-]|\.\w)", citation) is not None


def classify_unresolved(citation: Any, declared: DeclaredAbsent) -> str:
    """Ruling for one UNRESOLVED premise citation: ``expected-absent`` when it cites a declared-absent
    path, ``declared-absent-but-present-at-head`` when it cites an ``absent_at_head`` path that
    exists (wins over expected-absent), else ``blocks``."""
    if not isinstance(citation, str):
        return BLOCKS
    text = citation.replace("\\", "/")
    if any(_cites(text, p) for p in declared.present_at_head):
        return DECLARED_ABSENT_BUT_PRESENT
    if any(_cites(text, p) for p in declared.absent):
        return EXPECTED_ABSENT
    return BLOCKS
