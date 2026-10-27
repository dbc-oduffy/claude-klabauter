"""Targeted-test verdict reading for the landing gate: ``Finding``, the carried or recorded verdict,
the targeted set, re-verify red findings and the retest remedy line. Lowest module of the
landing-integrity split; it imports no landing sibling."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit.self_correction import FOREIGN_FIELD

TARGETED_TESTS_FAILED = "targeted-tests-failed"
REVIEW_REVERIFY_FAILED = "review-reverify-failed"
REVERIFY_INDETERMINATE = "indeterminate"
REVIEW_REVERIFY_INDETERMINATE = "review-reverify-indeterminate"
REVERIFY_KEY = "reverify"
TARGETED_TESTS_PARAM = "targeted_tests"
NOT_RUN_CHECK = "not-run"
ZERO_FAIL_UNATTRIBUTED = "test-verdict-names-no-failure"
ZERO_FAIL_CHECK = "zero-fail-unattributed"

RETEST_LAUNCHER = "emit-dispatch-workflow --retest-targeted"


@dataclass(frozen=True)
class Finding:
    """One refused landing path: the repo-relative path, the reason name, and a one-line detail."""

    path: str
    reason: str
    detail: str


def targeted_test_verdict(params: Mapping) -> Optional[Mapping]:
    """The test verdict a commit's params carry: ``params["targeted_tests"]``, else
    ``params["inline_review"]["tests"]`` or ``params["run_outcome"]["inline_review"]["tests"]``
    mapped to ``{status, tests_failed: failed, tests_run: run, baseline: None}``; None when none is carried."""
    direct = params.get(TARGETED_TESTS_PARAM)
    if isinstance(direct, Mapping) and isinstance(direct.get("status"), str):
        return direct
    run_outcome = params.get("run_outcome")
    for holder in (params, run_outcome if isinstance(run_outcome, Mapping) else {}):
        review = holder.get("inline_review")
        tests = review.get("tests") if isinstance(review, Mapping) else None
        if isinstance(tests, Mapping) and isinstance(tests.get("status"), str):
            return {
                "status": tests["status"], "tests_failed": tests.get("failed"),
                "tests_run": tests.get("run"), "baseline": None,
                **({"status_before": tests["status_before"]} if "status_before" in tests else {}),
            }
    return None


def _node_file(node_id: str) -> str:
    head = re.split(r"::| > ", str(node_id), maxsplit=1)[0].strip().replace("\\", "/")
    return head[2:] if head.startswith("./") else head


def recorded_targeted_set(params: Mapping, verdict: object) -> list[str]:
    """Test paths the run recorded as its targeted set: ``tests`` or ``test_paths`` on the verdict,
    else ``params["targeted_test_paths"]``. Empty when none is recorded."""
    holders = [verdict if isinstance(verdict, Mapping) else {}, params]
    keys = (("tests", "test_paths"), ("targeted_test_paths",))
    for holder, names in zip(holders, keys):
        for name in names:
            raw = holder.get(name)
            if isinstance(raw, list):
                found = [_node_file(str(n)) for n in raw if isinstance(n, str)]
                if any(found):
                    return found
    return []


def baseline_caused_files(verdict: object) -> list[str]:
    """Test files of the verdict's ``baseline.caused`` node ids."""
    baseline = verdict.get("baseline") if isinstance(verdict, Mapping) else None
    if not isinstance(baseline, Mapping):
        return []
    return [_node_file(str(node)) for node in baseline.get("caused") or []]


def targeted_set(params: Mapping, verdict: object, explicit: Sequence[str] = ()) -> list[str]:
    """The run's targeted test paths, deduplicated: ``explicit``, else the baseline's caused files,
    else the recorded set. The one definition the retest launcher and the landing gate share."""
    tests = [*explicit, *baseline_caused_files(verdict)]
    if not any(tests):
        tests = recorded_targeted_set(params, verdict)
    return list(dict.fromkeys(t for t in tests if t))


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


def _indeterminate_finding(error: object, remedy: str) -> Finding:
    cause = error if isinstance(error, str) and error.strip() else "no error recorded"
    return Finding(
        "", REVIEW_REVERIFY_INDETERMINATE,
        f"re-verify did not run ({cause}); engine defect, not a test result; re-run: {remedy}",
    )


def failed_targeted_tests(
    verdict: Optional[Mapping], remedy: Optional[str] = None
) -> tuple[list[Finding], str]:
    """``(findings, check)`` for a carried test verdict; ``check`` is ``"no-verdict"``, ``"pass"``,
    ``"not-run"``, ``"pre-existing-only"`` or ``"failed"``.

    A fail/error verdict whose ``tests_run`` is the integer 0 executed nothing: ``([], NOT_RUN_CHECK)``,
    checked before the baseline, never a TARGETED_TESTS_FAILED finding.

    A fail/error verdict whose ``status_before`` is ``"indeterminate"`` (the re-verify never ran) is
    one Finding(REVIEW_REVERIFY_INDETERMINATE) with check ``"failed"``, checked ahead of the above
    and never ``"pre-existing-only"``.

    A fail/error verdict with ``baseline.caused`` yields one Finding(TARGETED_TESTS_FAILED) per
    distinct caused test file (repo-relative POSIX), the detail naming up to three node ids. A
    fail/error verdict whose baseline is present with nothing caused is ``"pre-existing-only"``.
    A ``fail`` verdict with ``tests_failed`` the integer 0 and no baseline names no failure: with
    ``status_before == "fail"`` it is ``([], "pre-existing-only")``; otherwise one
    Finding(ZERO_FAIL_UNATTRIBUTED) with check ``ZERO_FAIL_CHECK``, never TARGETED_TESTS_FAILED.
    Trap: any other fail/error verdict with no baseline yields one Finding with ``path=""`` (unattributed,
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
    if verdict.get("status_before") == REVERIFY_INDETERMINATE:
        return [_indeterminate_finding(verdict.get("reverify_error"), remedy or retest_remedy(None))], "failed"
    tests_run = verdict.get("tests_run")
    if isinstance(tests_run, int) and not isinstance(tests_run, bool) and tests_run == 0:
        return [], NOT_RUN_CHECK
    baseline = verdict.get("baseline")
    if not isinstance(baseline, Mapping):
        count = verdict.get("tests_failed")
        if status == "fail" and isinstance(count, int) and not isinstance(count, bool) and count == 0:
            if verdict.get("status_before") == "fail":
                return [], "pre-existing-only"
            return [Finding(
                "", ZERO_FAIL_UNATTRIBUTED,
                f"the verdict reads fail with 0 failing tests and no failing node id to attribute "
                f"(status_before not fail); re-run: {remedy or retest_remedy(None)}",
            )], ZERO_FAIL_CHECK
        shown = f"{count} " if isinstance(count, int) else ""
        if status == "error" and count == 0:
            cause = next((str(verdict[k]) for k in ("reason", "summary", "error") if verdict.get(k)), None)
            ran = verdict.get("tests_run")
            return [Finding(
                "", TARGETED_TESTS_FAILED,
                f"the test run errored with 0 failing tests"
                f"{f' of {ran} run' if isinstance(ran, int) and not isinstance(ran, bool) else ''} and named no "
                f"erroring node (collection or runner error); "
                f"{f'recorded cause: {cause}; ' if cause else ''}re-run: {remedy or retest_remedy(None)}",
            )], "failed"
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


def _module_files(module: str) -> list[str]:
    base = module.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _repo_test_path(worktree_root: Path, test_file: str, landed: set[str]) -> Optional[str]:
    """Repo-relative path of a re-verify node's test file. A node id is relative to the directory the
    suite ran in, which may be a subdirectory of the repo: when ``test_file`` is not at the root, it is
    sought under each ancestor directory of a landed path (the root match wins; otherwise the first
    landed path in sorted order)."""
    root = Path(worktree_root)
    if (root / test_file).is_file():
        return test_file
    for path in sorted(landed):
        parts = path.split("/")[:-1]
        for depth in range(len(parts), 0, -1):
            candidate = "/".join(parts[:depth] + [test_file])
            if (root / candidate).is_file():
                return candidate
    return None


def _imports_landed(worktree_root: Path, repo_file: str, landed: set[str]) -> bool:
    """True when the repo-relative ``repo_file`` imports a module whose file is in ``landed``; False
    when unreadable. A module resolves from the repo root and from every ancestor directory of the
    test file, since a suite puts its own directory tree on ``sys.path`` rather than the repo root."""
    from coordinator_core.pyimports import collect_imports, module_name_for_file, resolve_relative_module

    try:
        tree = ast.parse((Path(worktree_root) / repo_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return False
    ancestors = ["/".join(repo_file.split("/")[:i]) for i in range(len(repo_file.split("/")) - 1, 0, -1)]
    records, _ = collect_imports(tree)
    is_pkg = repo_file.rsplit("/", 1)[-1] == "__init__.py"
    for rec in records:
        if rec.level == 0:
            base = rec.module or None
        else:
            base = resolve_relative_module(rec.module, rec.level, module_name_for_file(repo_file), is_pkg)
        if not base:
            continue
        wanted = _module_files(base)
        for name in rec.names:
            if name != "*":
                wanted.extend(_module_files(f"{base}.{name}"))
        if rec.level == 0:
            wanted += [f"{a}/{w}" for a in ancestors for w in wanted[:]]
        if any(p in landed for p in wanted):
            return True
    return False


def reverify_record(params: Mapping) -> Optional[Mapping]:
    """The run's ``inline_review`` re-verify record, or None when absent or not a mapping."""
    review = params.get("inline_review") if isinstance(params, Mapping) else None
    record = review.get(REVERIFY_KEY) if isinstance(review, Mapping) else None
    return record if isinstance(record, Mapping) else None


def reverify_verdict(record: Mapping) -> object:
    """``status_after``, else ``status_before`` when after is null."""
    after = record.get("status_after")
    return after if after is not None else record.get("status_before")


def reverify_red_findings(
    worktree_root: Path, params: Mapping, landing_paths: Sequence[str]
) -> list[Finding]:
    """One Finding(REVIEW_REVERIFY_INDETERMINATE) when the re-verify record reads ``indeterminate`` or
    carries a non-empty ``reverify_error``: it never ran, an engine defect rather than a test result.
    Otherwise a Finding(REVIEW_REVERIFY_FAILED) per test file the run's review re-verify left red that the
    landing touches: ``params["inline_review"]["reverify"]`` reading ``fail`` (``status_after``, else
    ``status_before``), whose ``failing`` node ids and ``reverted[].tests`` name a file that is itself
    landing or imports a landing module. An absent or non-dict re-verify, a non-fail verdict, or a
    failure implicating no landed path yields []. ``failing`` ids also listed in ``pre_existing`` (red at the
    run base) or under the record's ``foreign`` key (reds the run does not own) are dropped; ``reverted[].tests``
    are kept. A fail whose every ``failing`` id is pre_existing or foreign and
    that has no reverted tests yields []; a fail naming no test at all still refuses. In-process; one read per implicated test file."""
    record = reverify_record(params)
    if record is None:
        return []
    error = record.get("reverify_error")
    if reverify_verdict(record) == REVERIFY_INDETERMINATE or (isinstance(error, str) and error.strip()):
        return [_indeterminate_finding(error, retest_remedy(params, []))]
    if reverify_verdict(record) != "fail":
        return []
    landed = {str(p).replace("\\", "/") for p in landing_paths}
    failing = [str(n) for n in (record.get("failing") or []) if n]
    pre = {str(n) for n in (record.get("pre_existing") or [])}
    pre |= {str(n) for n in (record.get(FOREIGN_FIELD) or [])}
    nodes: list[str] = [n for n in failing if n not in pre]
    for entry in record.get("reverted") or []:
        if isinstance(entry, Mapping):
            nodes += [str(n) for n in (entry.get("tests") or []) if n]
    if failing and not nodes:
        return []
    by_file: dict[str, list[str]] = {}
    for node in nodes:
        by_file.setdefault(_node_file(node), []).append(node)
    out: list[Finding] = []
    if not any(by_file):
        # Trap: a red verdict naming no test file cannot be shown off-landing, so it must not land.
        return [Finding(
            "", REVIEW_REVERIFY_FAILED,
            f"re-verify ended red naming no failing test; re-run: {retest_remedy(params, [])}",
        )]
    for test_file, ids in by_file.items():
        repo_file = (_repo_test_path(worktree_root, test_file, landed) or test_file) if test_file else None
        if not repo_file or not (repo_file in landed or _imports_landed(worktree_root, repo_file, landed)):
            continue
        shown = "; ".join(list(dict.fromkeys(ids))[:3])
        out.append(Finding(
            test_file, REVIEW_REVERIFY_FAILED,
            f"{shown}; re-run: {retest_remedy(params, [test_file])}",
        ))
    return out
