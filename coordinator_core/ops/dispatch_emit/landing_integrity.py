"""Landing-integrity checks a run's terminal or halt commit makes before ``ceremony.commit_v2``.

Three named refusal reasons: a landed ``.py`` that fails to parse, a landing blob that drops
lines a foreign commit added since the run's base, and a targeted-test failure the run caused.
Everything is in-process: no spawn, and no test is run here, only the verdict the run carries.
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
TARGETED_TESTS_FAILED = "targeted-tests-failed"
TARGETED_TESTS_PARAM = "targeted_tests"

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
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract

    return ask_fanout_contract.recorded_run_base(Path(worktree_root), run_id)


def targeted_test_verdict(params: Mapping) -> Optional[Mapping]:
    """The test verdict a commit's params carry: ``params["targeted_tests"]``, else
    ``params["inline_review"]["tests"]`` or ``params["run_outcome"]["inline_review"]["tests"]``
    mapped to ``{status, tests_failed: failed, baseline: None}``; None when none is carried."""
    direct = params.get(TARGETED_TESTS_PARAM)
    if isinstance(direct, Mapping) and isinstance(direct.get("status"), str):
        return direct
    run_outcome = params.get("run_outcome")
    for holder in (params, run_outcome if isinstance(run_outcome, Mapping) else {}):
        review = holder.get("inline_review")
        tests = review.get("tests") if isinstance(review, Mapping) else None
        if isinstance(tests, Mapping) and isinstance(tests.get("status"), str):
            return {"status": tests["status"], "tests_failed": tests.get("failed"), "baseline": None}
    return None


def _node_file(node_id: str) -> str:
    head = str(node_id).split("::", 1)[0].strip().replace("\\", "/")
    return head[2:] if head.startswith("./") else head


def failed_targeted_tests(verdict: Optional[Mapping]) -> tuple[list[Finding], str]:
    """``(findings, check)`` for a carried test verdict; ``check`` is ``"no-verdict"``, ``"pass"``,
    ``"pre-existing-only"`` or ``"failed"``.

    A fail/error verdict with ``baseline.caused`` yields one Finding(TARGETED_TESTS_FAILED) per
    distinct caused test file (repo-relative POSIX), the detail naming up to three node ids. A
    fail/error verdict whose baseline is present with nothing caused is ``"pre-existing-only"``.
    Trap: a fail/error verdict with no baseline yields one Finding with ``path=""`` (unattributed,
    fail-closed), so a pre-existing failure with no baseline export refuses by design; the remedy
    is the test-runner's baseline export."""
    if not isinstance(verdict, Mapping):
        return [], "no-verdict"
    status = verdict.get("status")
    if status in ("pass", "pass-with-skips"):
        return [], "pass"
    if status not in ("fail", "error"):
        return [], "no-verdict"
    baseline = verdict.get("baseline")
    if not isinstance(baseline, Mapping):
        count = verdict.get("tests_failed")
        shown = f"{count} " if isinstance(count, int) else ""
        return [Finding("", TARGETED_TESTS_FAILED, f"{shown}tests {status}, no baseline to attribute them")], "failed"
    caused = [str(n) for n in (baseline.get("caused") or [])]
    if not caused:
        return [], "pre-existing-only"
    by_file: dict[str, list[str]] = {}
    for node in caused:
        by_file.setdefault(_node_file(node), []).append(node)
    return [
        Finding(path, TARGETED_TESTS_FAILED, "; ".join(nodes[:3]))
        for path, nodes in by_file.items()
    ], "failed"


def since_base_findings(
    worktree_root: Path, paths: Sequence[str], base_sha: str, *, run_id: Optional[str],
    plan: Optional[str] = None,
) -> list[Finding]:
    """Findings for landing blobs that drop lines a foreign commit added since ``base_sha``;
    ``plan`` lets the run's own re-fire checkpoints read as own."""
    from coordinator_core.git import since_base_overlap

    return list(since_base_overlap.find_overlaps(worktree_root, paths, base_sha, run_id=run_id, plan=plan))
