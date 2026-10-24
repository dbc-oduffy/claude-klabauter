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
from typing import Callable, Mapping, Optional, Sequence

PY_UNPARSEABLE = "py-unparseable"
SINCE_BASE_OVERLAP = "since-base-overlap"
TARGETED_TESTS_FAILED = "targeted-tests-failed"
IMPORT_CLOSURE_UNTRACKED = "import-closure-untracked"
REVIEW_REVERIFY_FAILED = "review-reverify-failed"
REVERIFY_INDETERMINATE = "indeterminate"
REVIEW_REVERIFY_INDETERMINATE = "review-reverify-indeterminate"
REVERIFY_KEY = "reverify"
TARGETED_TESTS_PARAM = "targeted_tests"
NOT_RUN_CHECK = "not-run"
NO_TESTS_STATUS = "no-tests"
ZERO_FAIL_UNATTRIBUTED = "test-verdict-names-no-failure"
ZERO_FAIL_CHECK = "zero-fail-unattributed"

RETEST_DIR = ".coordinator-local/retest"
RETEST_LAUNCHER = "emit-dispatch-workflow --retest-targeted"
RETEST_RUNNING = "running"
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
        found = _parse_finding(rel, source)
        if found:
            out.append(found)
    return out


def _parse_finding(rel: str, source: bytes) -> Optional[Finding]:
    try:
        ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return Finding(rel, PY_UNPARSEABLE, f"line {exc.lineno}: {exc.msg}")
    except ValueError as exc:
        return Finding(rel, PY_UNPARSEABLE, f"line 0: {exc}")
    return None


def unparseable_py_at(worktree_root: Path, rev: str, paths: Sequence[str]) -> list[Finding]:
    """Like ``unparseable_py`` but parses each ``.py`` path's blob at ``rev`` (one ``git cat-file --batch``
    spawn). Paths absent at ``rev`` are skipped; an unreadable ``rev`` yields no findings."""
    wanted = [p for p in paths if p.endswith(".py")]
    if not wanted:
        return []
    from coordinator_core.git.run import run_git

    proc = run_git(
        ["-C", str(worktree_root), "cat-file", "--batch"],
        input="".join(f"{rev}:{p}\n" for p in wanted).encode("utf-8"),
        binary=True,
    )
    if proc.returncode != 0:
        return []
    data, pos, out = proc.stdout_bytes, 0, []
    for rel in wanted:
        eol = data.find(b"\n", pos)
        if eol < 0:
            break
        header = data[pos:eol].split()
        pos = eol + 1
        if len(header) != 3 or header[1] != b"blob":
            continue
        size = int(header[2])
        found = _parse_finding(rel, data[pos : pos + size])
        pos += size + 1
        if found:
            out.append(found)
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


def closing_bases(worktree_root: Path, run_id: Optional[str], params: Mapping) -> tuple:
    """The 40-hex bases a run's closing or halt trailer names, deduplicated, in order: the stage
    manifest ``head_sha``, then ``run_base_for``, then ``params["review_run_base"]``.

    The manifest comes first because the ask route's checkpoints carry it as ``Checkpoint-Base``
    (``emit._checkpoint_trailer_js`` stamps ``_RUN_BASE_VAR`` from the emit sha), and it differs
    from the submission ``run_base_sha``. A trailing ``.workflow`` is stripped from ``run_id``.
    A run whose ledger names ``resumed_from`` also closes each predecessor's bases (manifest and
    recorded base), appended after its own, following the chain cycle-safe for at most
    ``requirements_ledger.CARRIED_HOP_CAP`` hops.
    In-process file reads only; empty when nothing resolves."""
    rid = run_id[: -len(".workflow")] if isinstance(run_id, str) and run_id.endswith(".workflow") else run_id
    found: list = [*_own_bases(worktree_root, rid, params), params.get("review_run_base")]
    for prev in _resumed_chain(worktree_root, rid):
        found.extend(_own_bases(worktree_root, prev, {}))
    return tuple(dict.fromkeys(b for b in found if isinstance(b, str) and re.fullmatch(r"[0-9a-f]{40}", b)))


def _own_bases(worktree_root: Path, rid: Optional[str], params: Mapping) -> list:
    """One run's unfiltered candidate bases: ledger manifest ``head_sha`` then its recorded run base."""
    found: list = []
    if rid:
        try:
            from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

            data = json.loads((record_dir(Path(worktree_root), rid) / "manifest.json").read_text(encoding="utf-8"))
            found.append(data.get("head_sha") if isinstance(data, dict) else None)
        except (OSError, ValueError):
            pass
    found.append(run_base_for(worktree_root, rid, params))
    return found


def _resumed_chain(worktree_root: Path, rid: Optional[str]) -> list:
    """The ``resumed_from`` predecessors of ``rid``, nearest first; empty without a ledger link."""
    if not rid:
        return []
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    out: list = []
    visited = {rid}
    cur = rid
    for _ in range(rl.CARRIED_HOP_CAP):
        try:
            prev = (rl.read_run(rl.run_dir(Path(worktree_root), cur)) or {}).get(rl.RESUMED_FROM_FIELD)
        except rl.LedgerRefused:
            break
        if not (isinstance(prev, str) and safe_id(prev)) or prev in visited:
            break
        visited.add(prev)
        out.append(prev)
        cur = prev
    return out


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
    failure implicating no landed path yields []. In-process; one read per implicated test file."""
    record = reverify_record(params)
    if record is None:
        return []
    error = record.get("reverify_error")
    if reverify_verdict(record) == REVERIFY_INDETERMINATE or (isinstance(error, str) and error.strip()):
        return [_indeterminate_finding(error, retest_remedy(params, []))]
    if reverify_verdict(record) != "fail":
        return []
    landed = {str(p).replace("\\", "/") for p in landing_paths}
    nodes: list[str] = [str(n) for n in (record.get("failing") or []) if n]
    for entry in record.get("reverted") or []:
        if isinstance(entry, Mapping):
            nodes += [str(n) for n in (entry.get("tests") or []) if n]
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

    A ``stale`` record (``stale: true`` or status ``"stale"``) and a ``running`` one measured
    nothing, so neither is ever a fresh verdict."""
    from coordinator_core.git.git_state import head_sha

    try:
        record = json.loads(fresh_verdict_path(worktree_root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("run_id") != run_id or not isinstance(record.get("status"), str):
        return None
    if record.get("stale") is True or record["status"] in ("stale", RETEST_RUNNING):
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
    ``failed_targeted_tests`` (including ``"not-run"``, returned before any failure attribution).
    A None ``run_id`` skips the fresh read.

    A ``running`` retest record refuses (check ``"retest-running"``) with the poll command.
    A recorded fail with no baseline names its failing ids from ``failed_tests`` (top
    level or under ``params.params``), the verdict's, and the task output's digest
    (``params.task_output_path``); each id's file unchanged since the run base is pre-existing
    (``"pre-existing-only"`` when all are), and each other file is its own refusal whose remedy
    retests that file. With no id known the refusal names the ``failed_tests`` param."""
    if _retest_record_status(worktree_root, run_id) == RETEST_RUNNING:
        return [Finding("", TARGETED_TESTS_FAILED,
                        "the run's retest is still running; poll: " + retest_remedy(params)
                        + " --poll; if poll reports it abandoned past deadline, re-run: "
                        + retest_remedy(params))], "retest-running"
    fresh = fresh_targeted_verdict(worktree_root, run_id) if run_id else None
    if fresh is not None and fresh.get("status") == "error" and fresh.get("tests_run") != 0:
        reason = fresh.get("reason") or "launcher produced no test results"
        return [Finding("", TARGETED_TESTS_FAILED,
                        f"the run's retest errored without running tests ({reason}); "
                        f"fix the launcher and re-run: {retest_remedy(params)}")], "failed"
    if fresh is not None and _ran_no_tests(fresh):
        fresh = None
    if fresh is not None and fresh.get("status") == "fail" and not isinstance(fresh.get("baseline"), Mapping):
        verdict, fresh = fresh, None
    else:
        verdict = None
    if fresh is None:
        verdict = verdict or targeted_test_verdict(params)
        if failed_targeted_tests(verdict)[1] == NOT_RUN_CHECK:
            return [], NOT_RUN_CHECK
        nodes = _failed_nodes(params, verdict)
        if nodes and failed_targeted_tests(verdict)[1] == ZERO_FAIL_CHECK:
            verdict = {**verdict, "tests_failed": None}
        files =list(dict.fromkeys(f for f in (_node_file(n) for n in nodes) if f))
        findings, check = failed_targeted_tests(verdict, retest_remedy(params, files))
        if any(f.reason == REVIEW_REVERIFY_INDETERMINATE for f in findings):
            return findings, check
        if check != "failed" or verdict.get("baseline"):
            return findings, check
        if not files:
            if _explicit_empty_failed_tests(params) and _retest_record_status(worktree_root, run_id) == "pass":
                return [], "pre-existing-only"
            return [Finding(findings[0].path, findings[0].reason,
                            f"{findings[0].detail}; or pass params.{FAILED_TESTS_PARAM} (failing node ids)"
                            f"{_stale_retest_note(worktree_root, run_id)}")], check
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
    findings, check = failed_targeted_tests(fresh, retest_remedy(params))
    return findings, "pass-on-retest" if check == "pass" else check


def _ran_no_tests(record: Mapping) -> bool:
    """True for a fresh ``no-tests`` record (every target dropped or none collected): it measured
    nothing and did not crash, so it is no verdict and the recorded verdict plus ``failed_tests``
    attribution decide. A launcher crash (``error`` without ``tests_run``) is not this."""
    return record.get("status") == NO_TESTS_STATUS


def _explicit_empty_failed_tests(params: Mapping) -> bool:
    """True when the caller passed ``failed_tests`` as an empty list (top level or ``params.params``)
    and no non-empty one."""
    nested = params.get("params")
    lists = [h.get(FAILED_TESTS_PARAM) for h in (params, nested if isinstance(nested, Mapping) else {})]
    lists = [x for x in lists if isinstance(x, (list, tuple))]
    return bool(lists) and not any(lists)


def _retest_record_status(worktree_root: Path, run_id: Optional[str]) -> Optional[str]:
    """Status in the run's retest record on disk, valid or not; None when absent or unreadable."""
    if not run_id:
        return None
    try:
        record = json.loads(fresh_verdict_path(worktree_root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    status = record.get("status") if isinstance(record, dict) else None
    return status if isinstance(status, str) else None


def _stale_retest_note(worktree_root: Path, run_id: Optional[str]) -> str:
    """Refusal suffix naming a retest record the tree has drifted from (HEAD moved or a covered
    file changed), with the route that clears it."""
    status = _retest_record_status(worktree_root, run_id)
    if status is None:
        return ""
    if status == "stale":
        return (
            f"; the run's retest record (stale) ran no tests: re-run {RETEST_LAUNCHER}, "
            f"or pass params.{FAILED_TESTS_PARAM}=[] after a passing retest"
        )
    return (
        f"; the run's retest record ({status}) no longer matches the tree (HEAD or a covered file "
        f"changed since it was written), so it is ignored: re-run {RETEST_LAUNCHER}, "
        f"or pass params.{FAILED_TESTS_PARAM}=[] after a passing retest"
    )


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


def import_closure_findings(
    worktree_root: Path, paths: Sequence[str], deleted: Sequence[str],
    landed_text: Optional[Callable[[str], Optional[str]]] = None,
) -> list[Finding]:
    """Findings(IMPORT_CLOSURE_UNTRACKED) for landing ``.py`` paths that import a module or name
    HEAD will lack once the landing commits. ``landed_text(rel)`` supplies the bytes the commit
    lands (None = absent); without it the worktree is judged. ``deleted`` paths read as absent.
    A detector exception is one finding naming it (fail closed). Zero spawns."""
    from coordinator_core.authoring_leaks import HeadView, import_closure

    gate = [*paths, *deleted]
    deleted_set = set(deleted)
    text = landed_text
    if text is not None and deleted_set:
        inner = text

        def text(rel: str) -> Optional[str]:
            return None if rel in deleted_set else inner(rel)

    try:
        head = HeadView(worktree_root, import_closure.candidates(gate))
        found = import_closure.detect(Path(worktree_root), gate, head, landed_text=text)
    except Exception as exc:
        return [Finding("", IMPORT_CLOSURE_UNTRACKED, f"import-closure detector failed: {type(exc).__name__}: {exc}")]
    return [Finding(f.path, IMPORT_CLOSURE_UNTRACKED, f"{f.line}: {f.detail}") for f in found]


def since_base_findings(
    worktree_root: Path, paths: Sequence[str], base_sha: str, *, run_id: Optional[str],
    plan: Optional[str] = None,
) -> list[Finding]:
    """Findings for landing blobs that drop lines a foreign commit added since ``base_sha``;
    ``plan`` lets the run's own re-fire checkpoints read as own."""
    from coordinator_core.git import since_base_overlap

    return list(since_base_overlap.find_overlaps(worktree_root, paths, base_sha, run_id=run_id, plan=plan))
