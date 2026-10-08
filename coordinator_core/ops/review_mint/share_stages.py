"""coordinator_core.ops.review_mint.share_stages -- assemble a run's stage
returns from the plan-scoped sidecars already on disk in
``.coordinator-local/subagent-share/<session_id>/``.

``assemble_from_share`` backs ``record-superseding-review --from-share``: it
reads the prep, reviewer, delivery-verifier, test-runner and criterion-judge
sidecars bound to one plan and returns what ``supersede.record_superseding_review``
otherwise takes by hand (``prep_sidecar``, ``wave_sidecar_paths``,
``stage_returns``) plus a ``used`` map naming every sidecar chosen.

A stage's return is read ONLY from its sidecar's frontmatter, under the field
names of ``review-stage.schema.json`` (judge: ``status`` in met/not_met/
indeterminate; delivery: ``verdict`` PASS/FAIL; tests: ``test_verdict_of``
(``test_verdict``, else ``status``) in pass/fail/not_run with optional ``run``/``failed``; prep: ``run_base_sha`` and
``product_files``). A sidecar that carries no such field is not that stage's
return, and a stage with none raises ``ShareStageMissing`` naming the dispatch
that produces it. The judge's verdict must also be newer than the HEAD commit.
Nothing is inferred or defaulted.

Plan binding is positive: a sidecar counts only when it names the plan
(``plan_id``/``plan``/``target_plan``) or is the plan's ``<stem>.delivery``
file; ``wave_bookkeeping._belongs_to_plan`` decides the match.

Spawns: one argv-only git call (HEAD commit time).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.completion_receipts.verdict import test_verdict_of
from coordinator_core.git.run import run_git
from coordinator_core.ops.review_mint.wave_bookkeeping import (
    _DELIVERY_SUFFIX,
    _belongs_to_plan,
    _load_sidecar_fm,
    _load_sidecar_text,
    _stem,
)

_GIT_TIMEOUT_SECS = 30
_JUDGE_STATUSES = ("met", "not_met", "indeterminate")
_TESTS_STATUSES = ("pass", "fail", "not_run")
_DELIVERY_HEADING_RE = re.compile(r"^#\s*Delivery verdict:.*?\b(PASS|FAIL)\b\s*$", re.MULTILINE)

#: Per stage: the dispatch that produces its sidecar, named in a refusal.
_DISPATCH = {
    "prep": "the review prep stage (coordinator:test-runner, review-prep-result) with a frontmatter-bearing sidecar",
    "reviewer": "a coordinator:code-reviewer (or other review-wave lens) dispatch with plan_path set",
    "delivery": "coordinator:delivery-verifier (delivery-verdict) over the frozen diff",
    "tests": "coordinator:test-runner for the plan's tests, recording status pass|fail|not_run and target_plan: <plan id>",
    "criterion": "coordinator:exit-criterion-judge (terminal-judge-result) against HEAD",
}


class ShareStageMissing(ValueError):
    """A required stage has no usable sidecar; ``stage`` names it."""

    def __init__(self, stage: str, detail: str):
        self.stage = stage
        super().__init__(f"no {stage} sidecar: {detail}; produced by {_DISPATCH[stage]}")


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _claimed_plan(fm: Dict[str, Any]) -> Optional[str]:
    for key in ("plan_id", "plan", "target_plan"):
        value = fm.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _binds(path: Path, fm: Dict[str, Any], plan_id: str, plan_stem: Optional[str]) -> bool:
    claimed = _claimed_plan(fm)
    if claimed is None:
        return _stem(path).endswith(_DELIVERY_SUFFIX) and _belongs_to_plan(path, fm, plan_id, plan_stem)
    return _belongs_to_plan(path, {"plan": claimed}, plan_id, plan_stem)


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _head_commit_time(repo_root: Path, head: str) -> float:
    proc = run_git(["log", "-1", "--format=%ct", head], cwd=str(repo_root), timeout=_GIT_TIMEOUT_SECS)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise ValueError(f"could not read the commit time of {head}: {proc.stderr.strip()}")


def _reverify_record(repo_root: Path, share: Path, plan_id: str, head: str) -> Optional[Dict[str, Any]]:
    """Newest delivery-verdict record superseding this plan's run record in ``share``, usable only
    when its ``head_sha`` is an ancestor-or-equal of ``head`` and its delivery verdict is explicit."""
    from coordinator_core.ops.dispatch_emit.reverify_delivery import _run_rel
    from coordinator_core.ops.dispatch_emit.verdict_supersession import _newest_supersession

    best: Optional[Dict[str, Any]] = None
    for path in sorted(share.glob("*.review-wave-bookkeeping.md")):
        fm = _load_sidecar_fm(path)
        if not fm or str(fm.get("plan_id")) != plan_id:
            continue
        rec = _newest_supersession(repo_root, _run_rel(repo_root, path))
        if rec and str(rec.get("recorded_at") or "") >= str((best or {}).get("recorded_at") or ""):
            best = rec
    if best is None or (best.get("delivery") or {}).get("verdict") not in ("PASS", "FAIL"):
        return None
    sha = str(best.get("head_sha") or "")
    if not sha:
        return None
    proc = run_git(["merge-base", "--is-ancestor", sha, head], cwd=str(repo_root), timeout=_GIT_TIMEOUT_SECS)
    return best if proc.returncode == 0 else None


def _bookkeeping_delivery(
    share: Path, plan_id: str, plan_stem: Optional[str]
) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """The newest of this plan's run bookkeeping sidecars in ``share`` whose ``delivery.verdict``
    is PASS/FAIL, as a delivery candidate. Bound by the sidecar's own ``plan_id``, never by name."""
    found: List[Tuple[Path, Dict[str, Any]]] = []
    for path in share.glob("*.review-wave-bookkeeping.md"):
        fm = _load_sidecar_fm(path)
        if not fm or _claimed_plan(fm) is None or not _binds(path, fm, plan_id, plan_stem):
            continue
        block = fm.get("delivery")
        if isinstance(block, dict) and block.get("verdict") in ("PASS", "FAIL"):
            unbacked = block.get("unbacked")
            found.append((path, {"verdict": block["verdict"], "claims_unbacked": unbacked if isinstance(unbacked, list) else []}))
    return _newest(found)


def _bookkeeping_tests(
    share: Path, plan_id: str, plan_stem: Optional[str]
) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """The newest of this plan's run bookkeeping sidecars whose ``tests.status`` is ``not_run``:
    the run itself recorded that no test target resolved. A recorded pass/fail is never reused here."""
    found: List[Tuple[Path, Dict[str, Any]]] = []
    for path in share.glob("*.review-wave-bookkeeping.md"):
        fm = _load_sidecar_fm(path)
        if not fm or _claimed_plan(fm) is None or not _binds(path, fm, plan_id, plan_stem):
            continue
        block = fm.get("tests")
        if isinstance(block, dict) and test_verdict_of(block) == "not_run":
            found.append((path, {"status": "not_run"}))
    return _newest(found)


_NEAR_MISS_KIND = {
    "prep": lambda fm: "run_base_sha" in fm,
    "reviewer": lambda fm: "review" in str(fm.get("agent_type") or "") and "exit-criterion" not in str(fm.get("agent_type") or ""),
    "delivery": lambda fm: "delivery" in str(fm.get("agent_type") or ""),
    "tests": lambda fm: "test-runner" in str(fm.get("agent_type") or ""),
    "criterion": lambda fm: "exit-criterion-judge" in str(fm.get("agent_type") or ""),
}


def _newest(items: List[Tuple[Path, Any]]) -> Optional[Tuple[Path, Any]]:
    return max(items, key=lambda it: (it[0].stat().st_mtime, it[0].name)) if items else None


def assemble_from_share(
    *,
    repo_root: Path,
    session_id: str,
    plan_id: str,
    plan_stem: Optional[str],
    head: str,
    judge_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """``{prep_sidecar, wave_sidecar_paths, stage_returns, used}`` from the
    session share dir. ``used`` maps each stage to its repo-relative sidecar
    path(s). Raises ``ShareStageMissing`` for the first missing stage, or
    ``ValueError`` when the newest judge verdict is not ``met``."""
    share = repo_root / ".coordinator-local" / "subagent-share" / session_id
    if not share.is_dir():
        raise ShareStageMissing("prep", f"share dir {_rel(share, repo_root)} does not exist")

    bound: List[Tuple[Path, Dict[str, Any]]] = []
    unbound: List[Tuple[Path, Dict[str, Any]]] = []
    for path in sorted(share.glob("*.md")):
        if path.name.endswith(".blocks.md") or not path.is_file():
            continue
        fm = _load_sidecar_fm(path)
        if fm is None:
            fm = {}
        if _stem(path).endswith((".review-wave-bookkeeping", ".superseding")) or fm.get("agent_type") == "engine:review-wave-bookkeeping":
            continue
        if _binds(path, fm, plan_id, plan_stem):
            bound.append((path, fm))
        elif _claimed_plan(fm) is None and not _stem(path).endswith(_DELIVERY_SUFFIX):
            unbound.append((path, fm))

    def missing(stage: str, detail: str) -> ShareStageMissing:
        """``ShareStageMissing`` whose detail also names this stage's sidecars dropped for want of a plan binding."""
        near = [_rel(p, repo_root) for p, fm in unbound if _NEAR_MISS_KIND[stage](fm)]
        if near:
            detail += (
                f"; found {len(near)} {stage} sidecar(s) with no plan binding: {', '.join(near)}; "
                f"set target_plan: {plan_id}"
            )
        return ShareStageMissing(stage, detail)

    def kind(fm: Dict[str, Any]) -> str:
        return str(fm.get("agent_type") or "")

    reverify = _reverify_record(repo_root, share, plan_id, head)

    prep = _newest([(p, fm) for p, fm in bound if "run_base_sha" in fm and _int_or_none(fm.get("product_files")) is not None])
    if prep is None:
        raise missing("prep", "no plan-scoped sidecar carries run_base_sha and product_files")

    # The judge is provisioned no sidecar (it cannot write, by design); its verdict is the
    # terminal-judge-result it returned, passed here. A transcribed sidecar is the fallback.
    judge_cands = [
        (p, fm) for p, fm in bound
        if "exit-criterion-judge" in kind(fm) and fm.get("status") in _JUDGE_STATUSES
    ]
    criterion_rec = (reverify or {}).get("criterion")
    if judge_result is None and not judge_cands and isinstance(criterion_rec, dict) and criterion_rec.get("status"):
        judge_result = criterion_rec
    if judge_result is not None:
        judge_path = None
        if judge_result.get("status") != "met":
            raise ValueError(
                f"judge result is {judge_result.get('status')!r}, not met; "
                "fix the criterion and re-dispatch coordinator:exit-criterion-judge"
            )
        observation = str(judge_result.get("observation") or "").strip()
        if not observation:
            raise missing("criterion", "the judge result records no observation")
    else:
        if not judge_cands:
            raise missing("criterion", "no plan-scoped judge sidecar carries a met/not_met/indeterminate status")
        head_time = _head_commit_time(repo_root, head)
        fresh = [(p, fm) for p, fm in judge_cands if p.stat().st_mtime >= head_time]
        judge = _newest(fresh)
        if judge is None:
            raise missing("criterion", f"the newest judge verdict predates HEAD commit {head[:10]}")
        judge_path, judge_fm = judge
        if judge_fm["status"] != "met":
            raise ValueError(
                f"newest judge verdict {_rel(judge_path, repo_root)} is {judge_fm['status']}, not met; "
                "fix the criterion and re-dispatch coordinator:exit-criterion-judge"
            )
        observation = judge_fm.get("observation")
        if not isinstance(observation, str) or not observation.strip():
            text = _load_sidecar_text(judge_path) or ""
            split_at = text.split("\n---\n", 1)
            observation = (split_at[1] if len(split_at) == 2 else "").strip()
        if not observation:
            raise missing("criterion", f"{_rel(judge_path, repo_root)} records no observation")

    delivery_cands: List[Tuple[Path, Dict[str, Any]]] = []
    for p, fm in bound:
        if "delivery" not in kind(fm) and not _stem(p).endswith(_DELIVERY_SUFFIX):
            continue
        verdict = fm.get("verdict")
        if verdict not in ("PASS", "FAIL"):
            m = _DELIVERY_HEADING_RE.search(_load_sidecar_text(p) or "")
            verdict = m.group(1) if m else None
        if verdict:
            delivery_cands.append((p, {**fm, "verdict": verdict}))
    delivery = _newest(delivery_cands)
    if delivery is None and reverify is not None:
        block = reverify["delivery"]
        rec_path = Path(str(reverify.get("supersedes") or ""))
        delivery = (rec_path, {"verdict": block["verdict"], "claims_unbacked": block.get("unbacked") or []})
    if delivery is None:
        delivery = _bookkeeping_delivery(share, plan_id, plan_stem)
    if delivery is None:
        raise missing("delivery", "no plan-scoped delivery sidecar records a PASS/FAIL verdict")

    tests = _newest([
        (p, fm) for p, fm in bound
        if "test-runner" in kind(fm) and test_verdict_of(fm) in _TESTS_STATUSES
    ])
    if tests is None and isinstance((reverify or {}).get("tests"), dict) and reverify["tests"].get("status") in _TESTS_STATUSES:
        t = reverify["tests"]
        tests = (Path(str(t.get("sidecar") or reverify.get("supersedes") or "")), t)
    if tests is None:
        tests = _bookkeeping_tests(share, plan_id, plan_stem)
    if tests is None:
        raise missing("tests", "no plan-scoped test-runner sidecar carries a pass/fail/not_run status")

    taken = {prep[0], judge_path, delivery[0], tests[0]} - {None}
    waves = [p for p, fm in bound if p not in taken and "review" in kind(fm) and "exit-criterion" not in kind(fm)]
    if not waves:
        raise missing("reviewer", "no plan-scoped reviewer sidecar")

    d_path, d_fm = delivery
    delivery_ret: Dict[str, Any] = {"verdict": d_fm["verdict"], "sidecar": _rel(d_path, repo_root)}
    if _int_or_none(d_fm.get("product_files")) is not None:
        delivery_ret["product_files"] = d_fm["product_files"]
    if isinstance(d_fm.get("claims_unbacked"), list):
        delivery_ret["unbacked"] = d_fm["claims_unbacked"]
    t_path, t_fm = tests
    tests_ret: Dict[str, Any] = {"status": test_verdict_of(t_fm), "sidecar": _rel(t_path, repo_root)}
    for key in ("run", "failed"):
        if key in t_fm:
            tests_ret[key] = t_fm[key]

    return {
        "prep_sidecar": _rel(prep[0], repo_root),
        "wave_sidecar_paths": waves,
        "stage_returns": {
            "delivery": delivery_ret,
            "tests": tests_ret,
            "criterion": {
                "status": "met",
                "observation": observation.strip(),
                "sidecar": _rel(judge_path, repo_root) if judge_path else None,
            },
        },
        "used": {
            "prep": [_rel(prep[0], repo_root)],
            "reviewer": [_rel(p, repo_root) for p in waves],
            "delivery": [_rel(d_path, repo_root)],
            "tests": [_rel(t_path, repo_root)],
            "criterion": [_rel(judge_path, repo_root)] if judge_path else ["(judge result JSON)"],
        },
    }
