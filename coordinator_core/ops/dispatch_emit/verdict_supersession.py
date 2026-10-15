"""coordinator_core.ops.dispatch_emit.verdict_supersession -- read side of the append-only
delivery-verdict records under `state/delivery-verdicts/`.

An import-light leaf: `review_stamp.mint` reads the newest superseding verdict here, and the
heavy writer (`reverify_delivery`, which composes review stages and reaches `dispatch_emit.op`)
stays out of every minter's import closure.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


DIFF_SHA_KEY = "diff_sha"
DELIVERY_NOT_RUN_KEY = "delivery_not_run"


def _newest_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """The newest record superseding `run_record_rel`, recorded no earlier than the run record.

    Trap: a run record's path is plan-scoped, so a later run (a late-delta review) rewrites the
    same path; a verdict recorded before that rewrite superseded the earlier run, and applying it
    would pull the stamp back to the old head and its old tests."""
    found = _newest_supersession_entry(repo_root, run_record_rel)
    return found[1] if found else None


def latest_delivery_binding(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """`{record_path, diff_sha}` of the record `_newest_supersession` selects, `None` when there
    is none. `record_path` is repo-relative posix; `diff_sha` is `None` when the record omits it."""
    found = _newest_supersession_entry(repo_root, run_record_rel)
    if not found:
        return None
    path, fm = found
    diff = fm.get(DIFF_SHA_KEY)
    return {
        "record_path": path.relative_to(repo_root).as_posix(),
        DIFF_SHA_KEY: str(diff) if diff else None,
    }


def _newest_supersession_entry(repo_root: Path, run_record_rel: str) -> Optional[tuple]:
    base = repo_root / VERDICT_DIR
    if not base.is_dir():
        return None
    floor = _run_record_written_at(repo_root / run_record_rel)
    best: Optional[tuple] = None
    for path in base.glob("*/*.md"):
        fm = _frontmatter(path)
        if not fm or fm.get("kind") != RECORD_KIND or fm.get("supersedes") != run_record_rel:
            continue
        if not isinstance(fm.get("delivery"), dict):
            continue
        at = _aware(_instant(fm.get("recorded_at")))
        if floor is not None and (at is None or at < floor):
            continue
        key = (str(fm.get("recorded_at") or ""), path.name)
        if best is None or key > best[0]:
            best = (key, (path, fm))
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


REJUDGE_KIND = "criterion-rejudge"


def _run_record_written_at(path: Path) -> Optional[datetime]:
    """The run record's `recorded_at`, else its mtime: records written before the bookkeeper
    stamped `recorded_at` live gitignored under `.coordinator-local/`, which no checkout rewrites
    and no reverify touches."""
    at = _aware(_instant((_frontmatter(path) or {}).get("recorded_at")))
    if at is not None:
        return at
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _aware(at: Optional[datetime]) -> Optional[datetime]:
    return at if at is None or at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _instant(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def latest_criterion_record(repo_root: Path, plan_id: str, newer_than: datetime) -> Optional[dict]:
    """The newest engine-written record under `VERDICT_DIR` bound to `plan_id` (`plan_id` field)
    that carries a `criterion` status and was recorded strictly after `newer_than`: a
    `criterion-rejudge` record or a delivery-verdict record that re-judged the criterion.
    `None` when there is none."""
    base = repo_root / VERDICT_DIR
    if not base.is_dir():
        return None
    best: Optional[tuple] = None
    for path in base.glob("*/*.md"):
        fm = _frontmatter(path)
        if not fm or fm.get("kind") not in (RECORD_KIND, REJUDGE_KIND) or str(fm.get("plan_id")) != plan_id:
            continue
        criterion = fm.get("criterion")
        at = _instant(fm.get("recorded_at"))
        if not (isinstance(criterion, dict) and criterion.get("status") and at and at > newer_than):
            continue
        key = (at, path.name)
        if best is None or key > best[0]:
            best = (key, {**fm, "record_path": path.relative_to(repo_root).as_posix()})
    return best[1] if best else None
