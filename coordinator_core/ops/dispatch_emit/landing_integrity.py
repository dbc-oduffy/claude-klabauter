"""Landing-integrity checks a run's terminal or halt commit makes before ``ceremony.commit_v2``.

Two named refusal reasons: a landed ``.py`` that fails to parse, and a landing blob that drops
lines a foreign commit added since the run's base. Everything is in-process: no spawn.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

PY_UNPARSEABLE = "py-unparseable"
SINCE_BASE_OVERLAP = "since-base-overlap"

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class Finding:
    """One refused landing path: the repo-relative path, the reason name, and a one-line detail."""

    path: str
    reason: str
    detail: str


def unparseable_py(worktree_root: Path, paths: Sequence[str]) -> list[Finding]:
    """A Finding(PY_UNPARSEABLE) per existing ``.py`` path whose worktree bytes fail ``ast.parse``.
    Non-``.py`` and missing paths are skipped."""
    out: list[Finding] = []
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        try:
            source = (Path(worktree_root) / rel).read_bytes()
        except OSError:
            continue
        try:
            ast.parse(source, filename=rel)
        except SyntaxError as exc:
            out.append(Finding(rel, PY_UNPARSEABLE, f"line {exc.lineno}: {exc.msg}"))
        except ValueError as exc:
            out.append(Finding(rel, PY_UNPARSEABLE, f"line 0: {exc}"))
    return out


def _submission_base(worktree_root: Path, run_id: str) -> Optional[str]:
    try:
        record = json.loads(
            (Path(worktree_root) / "scratch" / "warp" / run_id / "submission.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    value = record.get("run_base_sha") if isinstance(record, dict) else None
    return value if isinstance(value, str) and _SHA_RE.match(value) else None


def run_base_for(worktree_root: Path, run_id: Optional[str], params: Mapping) -> Optional[str]:
    """The run's base sha: ``params['run_base_sha']``, else the submission record's, else the
    reserved run's recorded base file; None when none exists."""
    value = params.get("run_base_sha")
    if isinstance(value, str) and _SHA_RE.match(value):
        return value
    if not run_id:
        return None
    found = _submission_base(worktree_root, run_id)
    if found:
        return found
    from coordinator_core.ops.dispatch_emit import op

    return op._recorded_run_base(Path(worktree_root), run_id)


def since_base_findings(
    worktree_root: Path, paths: Sequence[str], base_sha: str, *, run_id: Optional[str]
) -> list[Finding]:
    """Findings for landing blobs that drop lines a foreign commit added since ``base_sha``."""
    from coordinator_core.git import since_base_overlap

    return list(since_base_overlap.find_overlaps(worktree_root, paths, base_sha, run_id=run_id))
