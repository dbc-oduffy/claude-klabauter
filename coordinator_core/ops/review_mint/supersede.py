"""coordinator_core.ops.review_mint.supersede -- the stranded-run superseding
review record writer (D8 of the completion-receipts plan).

``record_superseding_review`` builds the review bookkeeping record with
``bookkeep_wave`` and copies it, append-only, to the durable
``state/superseding-reviews/<YYYY-MM>/<record_stem>.md`` with the keys
``kind``, ``plan_id``, ``commit_range`` and ``supersedes`` added. The record
names its commit range explicitly, so a peer's commit inside the range never
trips a Session-Id attribution refusal; ``review_stamp.mint`` reads it.

Refusals: ``head`` not an ancestor of HEAD, ``base`` not an ancestor of
``head``, ``base == head``, or an existing record at the target path. At most
two argv-only git spawns of its own; ``bookkeep_wave``'s spawns are additional.

DR-208 five-question affirmation (MUTATING):
  1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
     Creates the superseding record; bookkeep_wave also writes its record
     and stamps plan_id onto each wave sidecar.
  2. Writes into rag's relational store?                                 No.
  3. Opens any file for write (including sentinel creation)?             YES.
     Exclusive create of the superseding record.
  4. Mutates shared mutable state outside its own module?                YES.
     review_stamp.mint reads the record it writes.
  5. Persistent state changes observable across process boundaries?      YES.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops.review_mint.wave_bookkeeping import (
    bookkeep_wave,
    review_wave_bookkeeping_stem,
)
from coordinator_core.win_portability import leaf_spawn_creationflags


_GIT_TIMEOUT_SECS = 30


class SupersedeRefused(ValueError):
    """The superseding record was refused; no superseding record was written."""


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECS,
        stdin=subprocess.DEVNULL,
        **leaf_spawn_creationflags(),
    )
    if proc.returncode not in (0, 1):
        raise SupersedeRefused(
            f"git merge-base --is-ancestor {ancestor} {descendant} failed: "
            f"{proc.stderr.strip()}"
        )
    return proc.returncode == 0


def record_superseding_review(
    *,
    repo_root: Path,
    plan: str,
    commit_range: Dict[str, str],
    wave_sidecar_paths: List[Path],
    prep_sidecar: Optional[str],
    stage_returns: Optional[Dict[str, Any]],
    session_id: str,
    supersedes: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the superseding record; returns ``{record_path, record}`` with
    ``record_path`` repo-root-relative. Raises ``SupersedeRefused``."""
    base = str(commit_range.get("base") or "")
    head = str(commit_range.get("head") or "")
    if not base or not head:
        raise SupersedeRefused("commit_range needs both base and head")
    if base == head:
        raise SupersedeRefused("commit_range base equals head: empty range")
    if not _is_ancestor(repo_root, head, "HEAD"):
        raise SupersedeRefused(f"head {head} is not an ancestor of HEAD")
    if not _is_ancestor(repo_root, base, head):
        raise SupersedeRefused(f"base {base} is not an ancestor of head {head}")

    record_stem = review_wave_bookkeeping_stem(plan, session_id) + ".superseding"
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    rel = Path("state") / "superseding-reviews" / month / f"{record_stem}.md"
    target = repo_root / rel
    if target.exists():
        raise SupersedeRefused(f"superseding record already exists: {rel.as_posix()}")

    record = bookkeep_wave(
        [Path(p) for p in wave_sidecar_paths],
        repo_root=repo_root,
        session_id=session_id,
        plan_id=plan,
        prep_sidecar=prep_sidecar,
        record_stem=record_stem,
        stage_returns=stage_returns,
    )
    src = Path(record["sidecar_path"])
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    split = split_frontmatter(text)
    fm = (yaml.safe_load(split.fm_text) or {}) if split is not None else {}
    fm = {
        "kind": "superseding-review",
        "plan_id": plan,
        "commit_range": {"base": base, "head": head},
        "supersedes": supersedes,
        **{k: v for k, v in fm.items() if k not in ("kind", "plan_id", "commit_range", "supersedes")},
    }
    body = text.split("\n---\n", 1)[1] if "\n---\n" in text else ""
    out = (
        "---\n"
        + yaml.safe_dump(fm, default_flow_style=False, sort_keys=False).strip()
        + "\n---\n"
        + body
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(out)
    except FileExistsError:
        raise SupersedeRefused(f"superseding record already exists: {rel.as_posix()}")
    return {"record_path": rel.as_posix(), "record": fm}


@register_op("review_mint.record_superseding_review")
def _record_superseding_review_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: plan, commit_range {base, head}, wave_sidecar_paths,
    prep_sidecar, stage_returns, session_id, optional supersedes."""
    root = repo_root or Path(params.get("repo_root") or ".")
    try:
        result = record_superseding_review(
            repo_root=root,
            plan=params["plan"],
            commit_range=params["commit_range"],
            wave_sidecar_paths=[Path(p) for p in params.get("wave_sidecar_paths") or []],
            prep_sidecar=params.get("prep_sidecar"),
            stage_returns=params.get("stage_returns"),
            session_id=params["session_id"],
            supersedes=params.get("supersedes"),
        )
    except SupersedeRefused as exc:
        return {"status": "refused", "reason": str(exc)}
    return {"status": "recorded", **result}
