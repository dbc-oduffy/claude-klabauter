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
RETEST_LAUNCHER = "emit-dispatch-workflow --retest-targeted"
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
    head = re.split(r"::| > ", str(node_id), maxsplit=1)[0].strip().replace("\\", "/")
    return head[2:] if head.startswith("./") else head


_FILE_LIKE = re.compile(r"^[\w@./+-]+\.\w+$")
_GATE_BLOCKER = re.compile(r"gate-blocker:\s*outside-footprint:\s*(\S.*)$")


def named_failures(task_output_path: object) -> list[str]:
    """Failing test ids a task-output digest names: ``gate-blocker: outside-footprint: <id>`` in
    ``result.deviations[]`` and ``tests.per_row.failed[]`` anchors. Empty for an unreadable file
    or a digest naming none; ids whose file part is not a path are dropped."""
    if not isinstance(task_output_path, str) or not task_output_path.strip():
        return []
    try:
        data = json.loads(Path(task_output_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict) and "deviations" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict):
        return []
    tests = data.get("tests")
    per_row = tests.get("per_row") if isinstance(tests, Mapping) else None
    failed = per_row.get("failed") if isinstance(per_row, Mapping) else None
    entries = [*(data.get("deviations") or []), *(failed if isinstance(failed, list) else [])]
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        for field in ("anchor", "reason"):
            found = _GATE_BLOCKER.search(str(entry.get(field) or ""))
            if found and _FILE_LIKE.match(_node_file(found.group(1))):
                out.append(found.group(1).strip())
    return list(dict.fromkeys(out))


def retest_remedy(params: Optional[Mapping], files: Sequence[str] = ()) -> str:
    """The retest command for a refusal detail, runnable as printed on any OS: the settings-home
    ``emit-dispatch-workflow --retest-targeted`` launcher (no PYTHONPATH), with the exact
    ``--from-task-output`` line when ``params.task_output_path`` names the run's task-output file,
    and one ``--tests`` per file in ``files``."""
    raw = params.get("task_output_path") if isinstance(params, Mapping) else None
    if isinstance(raw, str) and raw.strip():
        path = raw.strip().replace("\\", "/")
        line = f'{RETEST_LAUNCHER} --from-task-output {json.dumps(path) if " " in path else path}'
    else:
        line = f"{RETEST_LAUNCHER} --from-task-output with the run's task-output file"
    return line + "".join(f" --tests {f}" for f in files)


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
    unreadable or malformed file.

    A ``stale`` verdict (no test ran: it records only that the run carries no targeted set) needs
    the ``run_id`` match alone, since HEAD and tree drift cannot outdate a result nothing measured;
    a pass or fail verdict keeps the full tree match."""
    from coordinator_core.git.git_state import head_sha

    try:
        record = json.loads(fresh_verdict_path(worktree_root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("run_id") != run_id or not isinstance(record.get("status"), str):
        return None
    if record.get("stale") is True or record["status"] == "stale":
        return record
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
    findings. A recorded fail with no baseline names its failing ids from ``failed_tests`` (top
    level or under ``params.params``), the verdict's, and the task output's digest
    (``params.task_output_path``); each id's file unchanged since the run base is pre-existing
    (``"pre-existing-only"`` when all are), and each other file is its own refusal whose remedy
    retests that file. With no id known the refusal names the ``failed_tests`` param."""
    fresh = fresh_targeted_verdict(worktree_root, run_id) if run_id else None
    if fresh is None:
        verdict = targeted_test_verdict(params)
        nodes = _failed_nodes(params, verdict)
        files = list(dict.fromkeys(f for f in (_node_file(n) for n in nodes) if f))
        findings, check = failed_targeted_tests(verdict, retest_remedy(params, files))
        if check != "failed" or verdict.get("baseline"):
            return findings, check
        if not files:
            return [Finding(findings[0].path, findings[0].reason,
                            f"{findings[0].detail}; or pass params.{FAILED_TESTS_PARAM} (failing node ids)")], check
        changed = _changed_since_base(worktree_root, params, run_id, files)
        if not changed:
            return [], "pre-existing-only"
        return [
            Finding(f, TARGETED_TESTS_FAILED,
                    "; ".join([n for n in nodes if _node_file(n) == f][:3])
                    + f"; not proven pre-existing (changed since the run base, or no base); "
                    f"re-run: {retest_remedy(params, [f])}")
            for f in changed
        ], "failed"
    if fresh.get("stale") is True or fresh.get("status") == "stale":
        return [], "stale-non-blocking"
    findings, check = failed_targeted_tests(fresh, retest_remedy(params))
    return findings, "pass-on-retest" if check == "pass" else check


def _failed_nodes(params: Mapping, verdict: Optional[Mapping]) -> list[str]:
    """Failing node ids from every source the gate trusts, in order, deduplicated: the caller's
    ``failed_tests`` (top level, then ``params.params``), the verdict's, the digest's named ones."""
    nested = params.get("params")
    holders = (params, nested if isinstance(nested, Mapping) else {}, verdict if isinstance(verdict, Mapping) else {})
    out: list[str] = []
    for holder in holders:
        raw = holder.get(FAILED_TESTS_PARAM)
        out += [str(n) for n in raw] if isinstance(raw, (list, tuple)) else [raw] if isinstance(raw, str) else []
    out += named_failures(params.get("task_output_path"))
    return list(dict.fromkeys(n.strip() for n in out if n.strip()))


def _changed_since_base(worktree_root: Path, params: Mapping, run_id: Optional[str], files: list[str]) -> list[str]:
    """The ``files`` not proven unchanged since the run base (all of them when no base is known)."""
    base = run_base_for(worktree_root, run_id, params)
    if not base:
        return files
    if _unchanged_since_base(worktree_root, base, files):
        return []
    if len(files) == 1:
        return files
    return [f for f in files if not _unchanged_since_base(worktree_root, base, [f])]


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


def since_base_findings(
    worktree_root: Path, paths: Sequence[str], base_sha: str, *, run_id: Optional[str],
    plan: Optional[str] = None,
) -> list[Finding]:
    """Findings for landing blobs that drop lines a foreign commit added since ``base_sha``;
    ``plan`` lets the run's own re-fire checkpoints read as own."""
    from coordinator_core.git import since_base_overlap

    return list(since_base_overlap.find_overlaps(worktree_root, paths, base_sha, run_id=run_id, plan=plan))
