"""
coordinator_core.ops.reap_in_progress_memos — return-data survey (plus a
delegated apply path) of cross-repo memos stranded at ``status: in_progress``
whose claiming session is no longer live.

Purpose, per `docs/plans/2026-09-11-handoff-lifecycle-one-legal-state-table.md`
chunk C6: a memo `cs_claim_memo_stamp` moved to ``in_progress`` is never
returned to ``open`` once the session named in its ``picked_up_by`` dies.
Built on the shape of `coordinator_core.ops.reap_in_flight_claims` (the
handoff reaper) and much smaller — a memo has no live-children check, no
ship-detection, and no governed-plan arm.

Liveness is decided by the SAME shared key the handoff reaper uses,
`coordinator_core.session.liveness.session_live`, and the deciding arm is
recorded on every release via `session_verdict` — the same
`_release_liveness_basis` shape — carrying forward the lesson of the
2026-08-22 bug in which the handoff reaper released a live holder's baton
with no record of which liveness arm produced the verdict.

Negative-spec:
    - Does NOT register a JSON-RPC op, and does NOT add an
      `authz/classification.py` row. `reap_in_flight_claims.py`, the shape
      this module mirrors, deliberately has neither — both callers import
      it in-process. Widening either surface here would contradict the
      mirror this module names.
    - Does NOT walk `state/cross-repo/archive/` — a memo already archived
      is historical evidence, never a reap candidate. Only
      `state/cross-repo/inbox/*.md` and `state/cross-repo/outbox/*.md` are
      read.
    - Does NOT port the ship-detection, governed-plan, or completion-index
      arms from `reap_in_flight_claims.py`. A memo that was acted on is
      `actioned`, not `in_progress`, so none of those arms has a
      memo-side question to answer here.
    - Does NOT guess at a malformed record. An `in_progress` memo with an
      empty `picked_up_by` gets a skip disposition, never a release.
    - Does NOT write memo frontmatter itself. `apply_dispositions()`
      delegates every release IN-PROCESS to
      `coordinator_core.archive_stamp.cs_release_memo_revert`.
    - Does NOT spawn a subprocess when there are zero `in_progress`
      candidates — `survey()` never shells out at all; only
      `apply_dispositions()`'s delegated call can write, and it is called
      only for `_VERDICT_RELEASE` rows.
    - Does NOT carry `reap_in_flight_claims.py`'s `_VERDICT_UNDETERMINED`
      posture. That arm exists there because a candidate sha can fail to
      resolve, leaving "no resolvable evidence" as a distinct outcome from
      "resolved evidence says dead". A memo's only evidence source is
      `session_live`/`session_verdict`, which always returns a determinate
      True/False — there is no analogous "didn't resolve" case here, so
      `_survey_dir` releases unconditionally on `session_live() is False`
      with no undetermined arm to add.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from coordinator_core.archive_stamp import cs_release_memo_revert
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.session.liveness import session_live, session_verdict

_VERDICT_RELEASE = "release"
_VERDICT_SKIP_EMPTY_HOLDER = "skip_empty_holder"


@dataclass
class Disposition:

    path: str
    holder: str
    verdict: str
    detail: str


@dataclass
class MemoSurveyResult:
    would_release: int
    dispositions: List[Disposition] = field(default_factory=list)


def _release_liveness_basis(holder: str, repo_root: Path) -> str:
    verdict = session_verdict(holder, cwd=str(repo_root))
    if verdict is None:
        return "unknown"
    return verdict[1]


def _survey_dir(memos_dir: Path, repo_root: Path) -> List[Disposition]:
    dispositions: List[Disposition] = []
    if not memos_dir.is_dir():
        return dispositions

    for entry in sorted(memos_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".md":
            continue
        text = entry.read_text(encoding="utf-8", errors="replace")
        split = split_frontmatter(text)
        if split is None:
            continue
        fm = split.fm_text
        status = read_fm_field_unquoted(fm, "status")
        if status != "in_progress":
            continue

        holder = read_fm_field_unquoted(fm, "picked_up_by")
        if not holder:
            dispositions.append(
                Disposition(
                    str(entry),
                    "",
                    _VERDICT_SKIP_EMPTY_HOLDER,
                    "in_progress memo has an empty picked_up_by -- malformed, "
                    "not guessed at",
                )
            )
            continue

        if session_live(holder, cwd=str(repo_root)):
            continue

        basis = _release_liveness_basis(holder, repo_root)
        dispositions.append(
            Disposition(
                str(entry),
                holder,
                _VERDICT_RELEASE,
                f"holder {holder} is dead (liveness arm: {basis}) -- releasing "
                "in_progress memo back to open",
            )
        )
    return dispositions


def survey(repo_root: Path) -> MemoSurveyResult:
    repo_root = Path(repo_root)
    cross_repo = repo_root / "state" / "cross-repo"

    dispositions: List[Disposition] = []
    dispositions.extend(_survey_dir(cross_repo / "inbox", repo_root))
    dispositions.extend(_survey_dir(cross_repo / "outbox", repo_root))

    would_release = sum(1 for d in dispositions if d.verdict == _VERDICT_RELEASE)
    return MemoSurveyResult(would_release, dispositions)


def apply_dispositions(
    dispositions: List[Disposition],
) -> "tuple[List[str], List[str]]":
    """Perform every `_VERDICT_RELEASE` disposition by calling
    `coordinator_core.archive_stamp.cs_release_memo_revert` IN-PROCESS. A
    skip verdict performs no write. Returns `(applied_paths, failed_details)`
    — a failed release is reported, never swallowed."""
    applied: List[str] = []
    failed: List[str] = []
    for d in dispositions:
        if d.verdict != _VERDICT_RELEASE:
            continue
        try:
            result = cs_release_memo_revert(d.path, return_result=True)
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort the reap
            failed.append(f"{d.path}: release-memo raised: {exc}")
            continue
        rc = int(result.get("exit_code", 1))
        if rc == 0:
            applied.append(d.path)
        else:
            failed.append(
                f"{d.path}: release-memo failed: {result.get('error', f'rc={rc}')}"
            )
    return applied, failed
