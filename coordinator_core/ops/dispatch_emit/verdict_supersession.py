"""coordinator_core.ops.dispatch_emit.verdict_supersession -- read side of the append-only
delivery-verdict records under `state/delivery-verdicts/`.

An import-light leaf: `review_stamp.mint` reads the newest superseding verdict here, and the
heavy writer (`reverify_delivery`, which composes review stages and reaches `dispatch_emit.op`)
stays out of every minter's import closure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter

VERDICT_DIR = Path("state") / "delivery-verdicts"
RECORD_KIND = "delivery-verdict"


def _frontmatter(path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        data = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _newest_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    base = repo_root / VERDICT_DIR
    if not base.is_dir():
        return None
    best: Optional[tuple] = None
    for path in base.glob("*/*.md"):
        fm = _frontmatter(path)
        if not fm or fm.get("kind") != RECORD_KIND or fm.get("supersedes") != run_record_rel:
            continue
        if not isinstance(fm.get("delivery"), dict):
            continue
        key = (str(fm.get("recorded_at") or ""), path.name)
        if best is None or key > best[0]:
            best = (key, fm)
    return best[1] if best else None


def latest_delivery_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """The `delivery` block of the newest delivery-verdict record superseding `run_record_rel`,
    else `None`, with the record's `head_sha` (the HEAD it verified) added when it has one.
    Newest is by `recorded_at`."""
    fm = _newest_supersession(repo_root, run_record_rel)
    if not fm:
        return None
    head = fm.get("head_sha")
    return {**fm["delivery"], "head_sha": str(head)} if head else fm["delivery"]


def latest_criterion_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """The `criterion` block of that same newest record; `None` when there is no record or it
    predates criterion re-judging."""
    fm = _newest_supersession(repo_root, run_record_rel)
    criterion = fm.get("criterion") if fm else None
    return criterion if isinstance(criterion, dict) and criterion.get("status") else None


def latest_tests_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """The `tests` block of that same newest record; `None` when there is no record or it
    predates test re-running."""
    fm = _newest_supersession(repo_root, run_record_rel)
    tests = fm.get("tests") if fm else None
    return tests if isinstance(tests, dict) and tests.get("status") else None


def latest_foreign_claims_supersession(repo_root: Path, run_record_rel: str) -> Optional[List[str]]:
    """The still-live `foreign_claims` of that same newest record; `None` for an old-shape record
    (the frozen `prep.foreign_claims` then stands)."""
    fm = _newest_supersession(repo_root, run_record_rel)
    claims = fm.get("foreign_claims") if fm else None
    return [str(c) for c in claims] if isinstance(claims, list) else None
