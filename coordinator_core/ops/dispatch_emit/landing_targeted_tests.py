"""Targeted-test verdict reading for the landing gate: ``Finding``, the carried or recorded verdict,
the targeted set, re-verify red findings, the retest remedy line and the in-run retest step. Lowest
module of the landing-integrity split; it imports no sibling at module level."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

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
NO_NODE_RECORDED = "no node recorded"
RETEST_ATTEMPTS_PARAM = "retest_attempts"


@dataclass(frozen=True)
class Finding:
    """One refused landing path: the repo-relative path, the reason name, and a one-line detail."""

    path: str
    reason: str
    detail: str


def tests_failed_finding(path: str, detail: str) -> Finding:
    """The ``targeted-tests-failed`` finding. The remedy is ``retest_step`` (the run retests in-run); this
    finding is the refusal only once that remedy is spent, so it is built as the halt of ``remedy_or_halt``."""
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import remedy_or_halt

    return remedy_or_halt(TARGETED_TESTS_FAILED, _in_leg_attempt, lambda _f: Finding(path, TARGETED_TESTS_FAILED, detail), attempts=1)


def _in_leg_attempt(_n: int) -> Finding:
    raise RuntimeError("the in-run retest leg runs the remedy")


def reverify_failed_finding(path: str, detail: str) -> Finding:
    """The ``review-reverify-failed`` finding; the remedy is ``retest_step``, so this is the halt of ``remedy_or_halt``."""
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import remedy_or_halt

    return remedy_or_halt(
        REVIEW_REVERIFY_FAILED, _in_leg_attempt, lambda _f: Finding(path, REVIEW_REVERIFY_FAILED, detail), attempts=1,
    )


def reverify_indeterminate_finding(detail: str) -> Finding:
    """The ``review-reverify-indeterminate`` finding; the remedy is ``retest_step``, so this is the halt of ``remedy_or_halt``."""
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import remedy_or_halt

    return remedy_or_halt(
        REVIEW_REVERIFY_INDETERMINATE, _in_leg_attempt,
        lambda _f: Finding("", REVIEW_REVERIFY_INDETERMINATE, detail), attempts=1,
    )


def zero_fail_finding(detail: str) -> Finding:
    """The ``test-verdict-names-no-failure`` finding; the remedy is ``retest_step``, so this is the halt of ``remedy_or_halt``."""
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import remedy_or_halt

    return remedy_or_halt(
        ZERO_FAIL_UNATTRIBUTED, _in_leg_attempt, lambda _f: Finding("", ZERO_FAIL_UNATTRIBUTED, detail), attempts=1,
    )


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


_INVARIANT_NODE = re.compile(r"repo-wide invariant (\S+)")


def remedy_nodes(findings: Sequence[Finding], params: Optional[Mapping] = None) -> list[str]:
    """The failing test nodes the in-run retest runs, only those: the nodes the ``TEST_EVIDENCE_REASONS``
    findings name (a repo-wide invariant node, a ``file::test`` id in the detail), else the finding's
    test file, plus the task-output digest's ``named_failures``. Never a tier."""
    out: list[str] = []
    for f in findings:
        if f.reason not in TEST_EVIDENCE_REASONS:
            continue
        inv = _INVARIANT_NODE.search(f.detail)
        if inv:
            out.append(inv.group(1).rstrip(";,"))
            continue
        parts = [t.strip().removeprefix("failing: ") for t in f.detail.split(";") if "::" in t]
        ids = [i for i in parts if _FILE_LIKE.match(_node_file(i))]
        out += ids or ([f.path] if f.path else [])
    if isinstance(params, Mapping) and not out:
        out += named_failures(params.get("task_output_path"))
    return list(dict.fromkeys(out))


def retests_done(params: Optional[Mapping]) -> int:
    """In-run retests already launched for this landing (``params.retest_attempts``), 0 when absent or malformed."""
    raw = params.get(RETEST_ATTEMPTS_PARAM) if isinstance(params, Mapping) else None
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def retests_spent(params: Optional[Mapping]) -> bool:
    """True once the in-run retest budget is used: a still-owed invariant then failed its retest."""
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS

    return retests_done(params) >= MAX_IN_RUN_REMEDY_ATTEMPTS


def retest_step(findings: Sequence[Finding], params: Optional[Mapping]) -> Optional[dict]:
    """The mechanical remedy step for a test-evidence refusal (``TEST_EVIDENCE_REASONS``: failed targeted
    tests, red or indeterminate re-verify, a verdict naming no failure), or None when no such
    finding exists. ``{kind, remedy, nodes, command, attempt, max_attempts, exhausted}``, ``kind`` the
    first such finding's reason: the run
    launches the detached retest (``command``) on ``nodes``, polls it, and re-invokes the landing
    with ``retest_attempts`` raised by one; ``exhausted`` once ``params.retest_attempts`` reached
    ``MAX_IN_RUN_REMEDY_ATTEMPTS``, when the refusal is the EM's. The landing never spawns pytest."""
    first = next((f for f in findings if f.reason in TEST_EVIDENCE_REASONS), None)
    if first is None:
        return None
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS

    done = retests_done(params)
    nodes = remedy_nodes(findings, params)
    return {
        "kind": first.reason, "remedy": "landing-runs-targeted-retest",
        "nodes": nodes, "command": retest_remedy(params, list(dict.fromkeys(_node_file(n) for n in nodes))),
        "attempt": done + 1, "max_attempts": MAX_IN_RUN_REMEDY_ATTEMPTS,
        "exhausted": done >= MAX_IN_RUN_REMEDY_ATTEMPTS,
    }


TEST_EVIDENCE_REASONS = (
    TARGETED_TESTS_FAILED, REVIEW_REVERIFY_FAILED, REVIEW_REVERIFY_INDETERMINATE, ZERO_FAIL_UNATTRIBUTED,
)


def split_test_advisory(findings: Sequence[Finding]) -> tuple[list[Finding], list[Finding]]:
    """``(refusing, advisory)``: a test-evidence finding leaves the refusal list and is recorded as an
    advisory, unless ``landing_refusal_kinds.is_refusal`` names its reason. A run's own work and a
    peer's dirt are never test evidence against the run."""
    from coordinator_core.ops.dispatch_emit.landing_refusal_kinds import is_refusal

    kept = [f for f in findings if f.reason not in TEST_EVIDENCE_REASONS or is_refusal(f.reason)]
    return kept, [f for f in findings if f not in kept]


def advisory_entries(advisory: Sequence[Finding]) -> list[dict]:
    """The reply shape of advisory test findings, each detail carrying the retest remedy it already names."""
    return [{"path": f.path, "reason": f.reason, "detail": f.detail} for f in advisory]


def _indeterminate_finding(error: object, remedy: str) -> Finding:
    cause = error if isinstance(error, str) and error.strip() else "no error recorded"
    return reverify_indeterminate_finding(
        f"re-verify did not run ({cause}); engine defect, not a test result; re-run: {remedy}",
    )


def _node_note(nodes: Sequence[str]) -> str:
    """``"; failing: a; b"`` (first three) for the nodes a refusal names, else the literal
    ``NO_NODE_RECORDED``: an unattributed refusal always says which tests, or that none were recorded."""
    named = [str(n) for n in dict.fromkeys(nodes) if n]
    return f"; failing: {'; '.join(named[:3])}" if named else f"; {NO_NODE_RECORDED}"


def failed_targeted_tests(
    verdict: Optional[Mapping], remedy: Optional[str] = None, measured: bool = True,
    nodes: Sequence[str] = (),
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
    fail/error verdict whose baseline has nothing caused is ``"pre-existing-only"`` only when every
    failing id (``pre_existing``, ``foreign``, ``unverified``) is in ``evidenced_clear_ids``; otherwise
    each unevidenced id is a TARGETED_TESTS_FAILED finding (counted caused), and a baseline naming no
    failing id at all is one unattributed finding.
    A ``fail`` verdict with ``tests_failed`` the integer 0 and no baseline names no failure and no
    evidence: one Finding(ZERO_FAIL_UNATTRIBUTED) with check ``ZERO_FAIL_CHECK``, never TARGETED_TESTS_FAILED.
    Trap: any other fail/error verdict with no baseline yields one Finding with ``path=""`` (unattributed,
    fail-closed), so a pre-existing failure with no baseline export refuses by design; the remedy
    is ``remedy`` (default ``retest_remedy(None)``), or a ``failed_tests`` node-id list that
    ``landing_test_gate`` attributes against the run base.
    Every ``path=""`` finding's detail names ``nodes`` plus the verdict's ``failed_tests``, else
    ``NO_NODE_RECORDED``.
    ``measured`` is False for a verdict the caller carried in params: its ``baseline.evidence`` is
    self-written, not read from a retest record the engine wrote, so no id clears on it."""
    if not isinstance(verdict, Mapping):
        return [], "no-verdict"
    carried = verdict.get("failed_tests")
    note = _node_note([*nodes, *(carried if isinstance(carried, list) else [])])
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
            return [zero_fail_finding(
                f"the verdict reads fail with 0 failing tests and no failing node id to attribute "
                f"(no baseline evidence){note}; re-run: {remedy or retest_remedy(None)}",
            )], ZERO_FAIL_CHECK
        shown = f"{count} " if isinstance(count, int) else ""
        if status == "error" and count == 0:
            cause = next((str(verdict[k]) for k in ("reason", "summary", "error") if verdict.get(k)), None)
            ran = verdict.get("tests_run")
            return [tests_failed_finding("",
                f"the test run errored with 0 failing tests"
                f"{f' of {ran} run' if isinstance(ran, int) and not isinstance(ran, bool) else ''} and named no "
                f"erroring node (collection or runner error); "
                f"{f'recorded cause: {cause}' if cause else ''}{note}; re-run: {remedy or retest_remedy(None)}",
            )], "failed"
        return [tests_failed_finding("",
            f"{shown}tests {status}, no baseline to attribute them{note}; re-run: {remedy or retest_remedy(None)}",
        )], "failed"
    from coordinator_core.completion_receipts.verdict import evidenced_clear_ids

    caused = [str(n) for n in (baseline.get("caused") or [])]
    if not caused:
        evidenced = evidenced_clear_ids(baseline, measured=True) if measured else set()
        failing = [
            str(i.get("id") if isinstance(i, Mapping) else i)
            for key in ("pre_existing", "foreign", "unverified") for i in (baseline.get(key) or [])
        ]
        unevidenced = [n for n in dict.fromkeys(failing) if n not in evidenced]
        if failing and not unevidenced:
            return [], "pre-existing-only"
        if not failing:
            return [tests_failed_finding("",
                f"tests {status} with no failing node id and no baseline evidence of a red pre-change{note}; "
                f"re-run: {remedy or retest_remedy(None)}",
            )], "failed"
        return [
            tests_failed_finding(path,
                    "; ".join(nodes[:3]) + "; no baseline evidence of red pre-change, counted caused")
            for path, nodes in _group_by_file(unevidenced).items()
        ], "failed"
    by_file: dict[str, list[str]] = {}
    for node in caused:
        by_file.setdefault(_node_file(node), []).append(node)
    return [
        tests_failed_finding(path, "; ".join(nodes[:3]))
        for path, nodes in by_file.items()
    ], "failed"


def _group_by_file(nodes: Sequence[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for node in nodes:
        out.setdefault(_node_file(node), []).append(node)
    return out


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


FAILING_AT_BASE = "failing_at_base"
BASE_RED_KEYS = ("pre_existing", "foreign", FAILING_AT_BASE)


def _record_base_red(record: Optional[Mapping]) -> set[str]:
    """Node ids a record names red at the run base (``BASE_RED_KEYS``)."""
    if not isinstance(record, Mapping):
        return set()
    return {str(n) for key in BASE_RED_KEYS for n in (record.get(key) or []) if n}


def base_red_ids(params: Mapping) -> set[str]:
    """Node ids the run's own records name red at the run base: the re-verify record's
    ``BASE_RED_KEYS`` and a ``failing_at_base`` list on ``params`` (top level or ``params.params``).
    The one attribution the re-verify and the recorded-fail paths share."""
    nested = params.get("params") if isinstance(params, Mapping) else None
    out = _record_base_red(reverify_record(params))
    for holder in (params, nested):
        raw = holder.get(FAILING_AT_BASE) if isinstance(holder, Mapping) else None
        out |= {str(n) for n in raw if n} if isinstance(raw, (list, tuple)) else set()
    return out


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
    pre = _record_base_red(record)
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
        return [reverify_failed_finding(
            "", f"re-verify ended red naming no failing test; re-run: {retest_remedy(params, [])}",
        )]
    for test_file, ids in by_file.items():
        repo_file = (_repo_test_path(worktree_root, test_file, landed) or test_file) if test_file else None
        if not repo_file or not (repo_file in landed or _imports_landed(worktree_root, repo_file, landed)):
            continue
        shown = "; ".join(list(dict.fromkeys(ids))[:3])
        out.append(reverify_failed_finding(
            test_file, f"{shown}; re-run: {retest_remedy(params, [test_file])}",
        ))
    return out
