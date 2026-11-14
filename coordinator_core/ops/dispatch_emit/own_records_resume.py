"""A run's own records read into a landing-resume verdict: manifest, returns and kept.json.

Invariant: a verdict exists only when every manifest row is done (a DONE return in ``returns.json`` or
a kept chunk in ``kept.json``) and the run's own ``run-outcome.json`` shows its review stage passed.
Stdlib only; spawns no process and reads at most five files of one run dir.

Invariant: ``paths`` is the set of normalised repo-relative paths the run itself wrote: kept chunk
paths, post-checkpoint hunks, declared writes of rows with a DONE return, and the halt's withheld set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, kept_work, row_returns

MANIFEST_FILE = "manifest.json"
OUTCOME_FILE = "run-outcome.json"

# A wave review verdict naming one of these means the review stage did not pass; WARN passes.
_FAILED_VERDICTS = frozenset({"FAIL", "BLOCK", "BLOCKED", "REJECT", "REJECTED"})


@dataclass(frozen=True)
class OwnRecordsResume:
    """A run whose rows are all done and reviewed, resumable at landing from its own records."""

    run_id: str
    plan_rel: Optional[str]
    rows: tuple
    paths: frozenset


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record_dir(root: Path, run_id: str) -> Optional[Path]:
    try:
        return ask_contract.record_dir(Path(root), run_id)
    except ValueError:
        return None


def _manifest_rows(rd: Path) -> Optional[list]:
    doc = _read_json(rd / MANIFEST_FILE)
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return None
    out = [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]]
    return out or None


def _done_returns(rd: Path) -> set:
    return {
        row
        for row, entry in row_returns.read(rd).items()
        if isinstance(entry, dict) and row_returns.is_done(entry.get("text"))
    }


def _kept_paths(rd: Path) -> tuple:
    kept = kept_work.read(rd)
    chunks = kept["chunks"]
    paths: set = set()
    for entry in chunks.values():
        inner = entry.get("paths") if isinstance(entry, dict) else None
        if isinstance(inner, dict):
            paths.update(inner)
    paths.update(kept[kept_work.POST_CHECKPOINT_KEY])
    return set(chunks), paths


def _review_passed(rd: Path) -> bool:
    outcome = _read_json(rd / OUTCOME_FILE)
    if not isinstance(outcome, dict) or outcome.get("incomplete_chunks"):
        return False
    review = outcome.get("inline_review")
    if not isinstance(review, dict):
        return False
    tests = review.get("tests")
    if isinstance(tests, dict) and str(tests.get("status", "")).lower() == "fail":
        return False
    for entry in review.get("wave_reviews") or []:
        if isinstance(entry, dict) and str(entry.get("verdict", "")).upper() in _FAILED_VERDICTS:
            return False
    return True


def _normalised(raw) -> frozenset:
    return frozenset(k for k in (kept_work._norm(p) for p in raw) if k is not None)


def own_written_paths(root: Path, run_id: str) -> frozenset:
    """Normalised paths the run wrote per its own records; empty when the record dir is unreadable.
    Needs no complete or reviewed run: a halted run's partial work counts."""
    rd = _record_dir(root, run_id)
    if rd is None:
        return frozenset()
    _, paths = _kept_paths(rd)
    returned = _done_returns(rd)
    for row in _manifest_rows(rd) or []:
        if row["id"] in returned and isinstance(row.get("writes"), list):
            paths.update(row["writes"])
    paths.update(halted_run.halted_withheld(rd))
    return _normalised(paths)


def landing_resume_from_own_records(root: Path, run_id: str) -> Optional[OwnRecordsResume]:
    """The landing-resume verdict, or None while the manifest is missing, any manifest row is neither
    returned DONE nor kept, or the run-outcome shows no passed review."""
    rd = _record_dir(root, run_id)
    if rd is None:
        return None
    rows = _manifest_rows(rd)
    if rows is None:
        return None
    kept_rows, _ = _kept_paths(rd)
    done = _done_returns(rd) | kept_rows
    ids = tuple(r["id"] for r in rows)
    if any(i not in done for i in ids) or not _review_passed(rd):
        return None
    outcome = _read_json(rd / OUTCOME_FILE)
    plan = outcome.get("plan_path") if isinstance(outcome, dict) else None
    return OwnRecordsResume(
        run_id=run_id,
        plan_rel=plan if isinstance(plan, str) and plan else None,
        rows=ids,
        paths=own_written_paths(root, run_id),
    )
