"""Landing-integrity checks a run's terminal or halt commit makes before ``ceremony.commit_v2``.

Three named refusal reasons: a landed ``.py`` that fails to parse, a landing blob that drops
lines a foreign commit added since the run's base, and a targeted-test failure the run caused.
Everything is in-process: no spawn, and no test is run here, only the verdict the run carries
(or the fresh one ``retest_targeted`` wrote).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

PY_UNPARSEABLE = "py-unparseable"
SINCE_BASE_OVERLAP = "since-base-overlap"
TARGETED_TESTS_FAILED = "targeted-tests-failed"
TARGETED_TESTS_PARAM = "targeted_tests"

RETEST_DIR = ".coordinator-local/retest"
RETEST_MODULE = "python -m coordinator_core.ops.dispatch_emit.retest_targeted"
FAILED_TESTS_PARAM = "failed_tests"

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


def retest_remedy(params: Optional[Mapping]) -> str:
    """The retest command for a refusal detail: the exact line when ``params.task_output_path``
    names the run's task-output file, else the module plus the flag to give it. No placeholder."""
    raw = params.get("task_output_path") if isinstance(params, Mapping) else None
    if isinstance(raw, str) and raw.strip():
        path = raw.strip().replace("\\", "/")
        return f'{RETEST_MODULE} --from-task-output {json.dumps(path) if " " in path else path}'
    return f"{RETEST_MODULE} --from-task-output with the run's task-output file"


def failed_targeted_tests(
    verdict: Optional[Mapping], remedy: Optional[str] = None
) -> tuple[list[Finding], str]:
    """``(findings, check)`` for a carried test verdict; ``check`` is ``"no-verdict"``, ``"pass"``,
    ``"pre-existing-only"`` or ``"failed"``.

    A fail/error verdict with ``baseline.caused`` yields one Finding(TARGETED_TESTS_FAILED) per
    distinct caused test file (repo-relative POSIX), the detail naming up to three node ids. A
    fail/error verdict whose baseline is present with nothing caused is ``"pre-existing-only"``.
    Trap: a fail/error verdict with no baseline yields one Finding with ``path=""`` (unattributed,
    fail-closed), so a pre-existing failure with no baseline export refuses by design; the remedy
    is ``remedy`` (default ``retest_remedy(None)``), or a ``failed_tests`` node-id list that
    ``landing_test_gate`` attributes against the run base."""
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
        return [Finding(
            "", TARGETED_TESTS_FAILED,
            f"{shown}tests {status}, no baseline to attribute them; re-run: {remedy or retest_remedy(None)}",
        )], "failed"
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


def fresh_verdict_path(worktree_root: Path, run_id: str) -> Path:
    """Where the retest step writes a run's fresh test verdict."""
    return Path(worktree_root) / RETEST_DIR / f"{run_id}.json"


def landing_fingerprint(worktree_root: Path, paths: Sequence[str]) -> dict[str, str]:
    """Repo-relative POSIX path to the sha256 hex of its bytes, or ``"absent"`` when unreadable.
    In-process; no spawn."""
    out: dict[str, str] = {}
    for rel in paths:
        key = str(rel).replace("\\", "/")
        try:
            out[key] = hashlib.sha256((Path(worktree_root) / key).read_bytes()).hexdigest()
        except OSError:
            out[key] = "absent"
    return out


def fresh_targeted_verdict(worktree_root: Path, run_id: str) -> Optional[Mapping]:
    """The retest verdict for ``run_id``, only while it still describes the tree: ``run_id`` and
    HEAD match and every ``covered`` file hashes as recorded. None otherwise, including for an
    unreadable or malformed file."""
    from coordinator_core.git.git_state import head_sha

    try:
        record = json.loads(fresh_verdict_path(worktree_root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("run_id") != run_id or not isinstance(record.get("status"), str):
        return None
    covered = record.get("covered")
    if not isinstance(covered, dict) or not record.get("head") or record["head"] != head_sha(worktree_root):
        return None
    if landing_fingerprint(worktree_root, list(covered)) != covered:
        return None
    return record


def landing_test_gate(
    worktree_root: Path, params: Mapping, run_id: Optional[str]
) -> tuple[list[Finding], str]:
    """``(findings, check)`` for a landing path: a valid fresh verdict replaces the recorded one.
    ``check`` is ``"pass-on-retest"`` when a fresh pass cleared it; otherwise as
    ``failed_targeted_tests``. A None ``run_id`` skips the fresh read.

    A fresh verdict with ``stale: true`` (or status ``"stale"``) is ``"stale-non-blocking"``: no
    findings. A recorded fail with no baseline whose ``failed_tests`` files are unchanged since
    the run base is ``"pre-existing-only"``; without ``failed_tests`` the refusal names the param."""
    remedy = retest_remedy(params)
    fresh = fresh_targeted_verdict(worktree_root, run_id) if run_id else None
    if fresh is None:
        verdict = targeted_test_verdict(params)
        findings, check = failed_targeted_tests(verdict, remedy)
        if check == "failed" and not verdict.get("baseline") and _preexisting(
            worktree_root, params, run_id, verdict
        ):
            return [], "pre-existing-only"
        if check == "failed" and findings and not findings[0].path and not _failed_files(params, verdict):
            findings = [Finding(findings[0].path, findings[0].reason,
                                f"{findings[0].detail}; or pass params.{FAILED_TESTS_PARAM} (failing node ids)")]
        return findings, check
    if fresh.get("stale") is True or fresh.get("status") == "stale":
        return [], "stale-non-blocking"
    findings, check = failed_targeted_tests(fresh, remedy)
    return findings, "pass-on-retest" if check == "pass" else check


def _failed_files(params: Mapping, verdict: Mapping) -> list[str]:
    nodes = params.get(FAILED_TESTS_PARAM) or verdict.get(FAILED_TESTS_PARAM) or []
    if not isinstance(nodes, (list, tuple)):
        return []
    return list(dict.fromkeys(f for f in (_node_file(str(n)) for n in nodes) if f))


def _unchanged_since_base(worktree_root: Path, base: str, paths: Sequence[str]) -> bool:
    """True when every path is byte-identical in the worktree, HEAD and ``base``; in-process."""
    from coordinator_core.git import ref_cas_retry
    from coordinator_core.git.git_state import head_blobs, head_sha

    head = head_sha(worktree_root)
    if not head or ref_cas_retry.paths_unchanged_between(worktree_root, base, head, paths) is not True:
        return False
    blobs = head_blobs(worktree_root, paths)
    for rel in paths:
        entry = blobs.get(rel)
        try:
            data = (Path(worktree_root) / rel).read_bytes()
        except OSError:
            return entry is None
        if entry is None or hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != entry[1]:
            return False
    return True


def _preexisting(worktree_root: Path, params: Mapping, run_id: Optional[str], verdict: Mapping) -> bool:
    files = _failed_files(params, verdict)
    base = run_base_for(worktree_root, run_id, params) if files else None
    return bool(files and base and _unchanged_since_base(worktree_root, base, files))


def since_base_findings(
    worktree_root: Path, paths: Sequence[str], base_sha: str, *, run_id: Optional[str],
    plan: Optional[str] = None,
) -> list[Finding]:
    """Findings for landing blobs that drop lines a foreign commit added since ``base_sha``;
    ``plan`` lets the run's own re-fire checkpoints read as own."""
    from coordinator_core.git import since_base_overlap

    return list(since_base_overlap.find_overlaps(worktree_root, paths, base_sha, run_id=run_id, plan=plan))
