"""`dispatch.objective_check`: run the pinned falsifier after an execute wave and append one loop line.

Deterministic code only: the engine runs the falsifier script (and each plan spine row's
`acceptance:` command) by argv, records the lines, and never judges. Every doubt is `not_met`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import objective_contract as oc
from coordinator_core.win_portability import no_console_creationflags

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_TAIL = 400
ACCEPTANCE_PREFIX = "acceptance:"


def _tail(text: Optional[str]) -> str:
    return (text or "").strip()[-_TAIL:]


def _argv_for(command: str) -> Optional[list]:
    """The argv of a backticked command in `command`, else of the whole string; None when unparseable."""
    match = _BACKTICK_RE.search(command)
    try:
        argv = shlex.split(match.group(1) if match else command)
    except ValueError:
        return None
    if argv and argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    return argv or None


def _spawn(argv: list, root: Path, timeout_s: float):
    """`(proc, None)` on exit, `(None, reason)` on timeout or launch failure. No shell."""
    try:
        proc = subprocess.run(
            argv, cwd=str(root), capture_output=True, text=True, timeout=timeout_s,
            shell=False, **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return None, oc.REASON_TIMEOUT
    except (OSError, ValueError) as exc:
        return None, f"{oc.REASON_UNPARSEABLE}: could not launch: {exc}"
    return proc, None


def _spawn_all(argvs: list, root: Path, timeout_s: float) -> list:
    """`_spawn` outcomes for every argv, in order, run concurrently as one batch."""
    if not argvs:
        return []
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(len(argvs), 4)) as pool:
        return list(pool.map(lambda argv: _spawn(argv, root, timeout_s), argvs))


def _all_failed(axis_ids: list, reason: str, evidence: str = "") -> list:
    return [oc.AxisResult(axis=i, passed=False, reason=reason, evidence=evidence) for i in axis_ids]


def _diff_names(root: Path, diff_range: str) -> Optional[set]:
    """Paths the wave's diff touches (one git spawn); None when the diff cannot be read.

    A range starting with `-` would reach git as an option (`--output=<file>` writes a file): unreadable.
    """
    if diff_range.startswith("-"):
        return None
    from coordinator_core.git.run import run_git
    from coordinator_core.ipc import CEREMONY_BUDGET_SECS

    res = run_git(["diff", "--name-only", diff_range], cwd=str(root), timeout=CEREMONY_BUDGET_SECS)
    if res.returncode != 0:
        return None
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _falsifier_results(objective: oc.Objective, root: Path, run_dir: Path, diff_range: str, timeout_s: float) -> list:
    """The script's per-axis results, or every axis failed with the guarding reason."""
    ids = [a.id for a in objective.axes]
    if not objective.falsifier_sha256 or objective.pinned_at not in oc.PINNED_AT_VALUES:
        return _all_failed(ids, oc.REASON_UNPINNED)
    script = root / objective.falsifier_script
    base = run_dir / oc.BASE_FILE
    if diff_range:
        names = _diff_names(root, diff_range)
        if names is None:
            return _all_failed(ids, oc.REASON_RULER_CHANGED, "diff unreadable")
        if {_rel(root, script), _rel(root, base)} & names:
            return _all_failed(ids, oc.REASON_RULER_CHANGED)
    try:
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
    except OSError as exc:
        return _all_failed(ids, oc.REASON_RULER_CHANGED, f"script unreadable: {exc}")
    if digest != objective.falsifier_sha256:
        return _all_failed(ids, oc.REASON_RULER_CHANGED)
    proc, err = _spawn(oc.falsifier_argv(script), root, timeout_s)
    if proc is None:
        return _all_failed(ids, err or oc.REASON_TIMEOUT)
    results = oc.parse_falsifier_lines(proc.stdout, objective.axes)
    all_pass = all(r.passed for r in results)
    if proc.returncode == 0 and not all_pass:
        return [oc._failed(r, oc.REASON_SUMMARY_DISAGREES) for r in results]
    if proc.returncode != 0:
        if all_pass:
            return [oc._failed(r, oc.REASON_SUMMARY_DISAGREES) for r in results]
        for r in results:
            if not r.passed:
                r.evidence = r.evidence or _tail(proc.stderr)
    return results


def _apply_haiku(results: list, haiku_axes: Any) -> None:
    """A Haiku verdict may pass only a behaviour axis the script reported with no command."""
    if not isinstance(haiku_axes, Mapping):
        return
    for r in results:
        verdict = haiku_axes.get(r.axis)
        if isinstance(verdict, Mapping):
            ok, evidence = verdict.get("pass"), str(verdict.get("evidence") or "")
        else:
            ok, evidence = verdict, ""
        if r.kind == oc.KIND_BEHAVIOUR and not r.command and not r.reason and ok is True:
            r.passed, r.evidence = True, evidence or "haiku verdict"


def _spine_acceptance(root: Path, plan_path: str) -> list:
    """`[(row id, acceptance command)]` for every spine row of the plan that carries `acceptance:`."""
    if not plan_path:
        return []
    from coordinator_core.pickup_brief import _spine_rows

    path = Path(plan_path)
    path = path if path.is_absolute() else root / path
    try:
        rows = _spine_rows(path.read_text(encoding="utf-8")) or []
    except OSError:
        return []
    return [(str(r.get("id")), r["acceptance"]) for r in rows
            if isinstance(r.get("acceptance"), str) and r["acceptance"].strip()]


def _acceptance_results(root: Path, plan_path: str, timeout_s: float) -> list:
    out = []
    rows = _spine_acceptance(root, plan_path)
    argvs = [_argv_for(command) for _, command in rows]
    runnable = [a for a in argvs if a is not None]
    outcomes = iter(_spawn_all(runnable, root, timeout_s))
    for (row_id, command), argv in zip(rows, argvs):
        axis = f"{ACCEPTANCE_PREFIX}{row_id}"
        common = dict(axis=axis, kind=oc.KIND_BEHAVIOUR, command=command, comparator="bool")
        if argv is None:
            out.append(oc.AxisResult(passed=False, reason=oc.REASON_UNPARSEABLE, **common))
            continue
        proc, err = next(outcomes)
        if proc is None:
            out.append(oc.AxisResult(passed=False, reason=err or oc.REASON_TIMEOUT, **common))
            continue
        out.append(oc.AxisResult(
            passed=proc.returncode == 0, now=proc.returncode == 0,
            evidence=_tail(proc.stdout + proc.stderr) if proc.returncode else "exit 0",
            reason="" if proc.returncode == 0 else f"exit {proc.returncode}", **common))
    return out


def _append_line(path: Path, line: oc.LoopLine) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line.to_json(), sort_keys=True) + "\n")


@register_op("dispatch.objective_check")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Run the falsifier (and the plan's acceptance commands), append one check line, return the verdict.

    Params: ``run_id``, ``wave``, ``iteration``, ``spend_usd``, ``diff_range``, ``plan_path``,
    optional ``haiku_axes`` {axis: bool | {pass, evidence}}, ``check_cost_usd``, ``check_llm_calls``.
    Returns ``{status: met|not_met, axes, failing}``; ``failing`` is the pass=false axis lines.
    """
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    root = Path(repo_root) if repo_root is not None else Path.cwd()
    try:
        run_dir = record_dir(root, str(params.get("run_id")))
    except ValueError as exc:
        return {"status": oc.STATUS_NOT_MET, "axes": [], "failing": [], "error": str(exc)}
    try:
        objective = oc.Objective.from_json(json.loads((run_dir / oc.OBJECTIVE_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        return {"status": oc.STATUS_NOT_MET, "axes": [], "failing": [], "error": f"objective unreadable: {exc}"}

    timeout_s = float(oc.FALSIFIER_TIMEOUT_S)
    results = _falsifier_results(objective, root, run_dir, str(params.get("diff_range") or ""), timeout_s)
    _apply_haiku(results, params.get("haiku_axes"))
    if not results or results[0].reason not in (oc.REASON_UNPINNED, oc.REASON_RULER_CHANGED):
        results += _acceptance_results(root, str(params.get("plan_path") or ""), timeout_s)

    lines = [r.to_line() for r in results]
    _append_line(run_dir / oc.LOOP_FILE, oc.LoopLine(
        wave=int(params.get("wave") or 0), iteration=int(params.get("iteration") or 0), axes=lines,
        spend_usd=float(params.get("spend_usd") or 0.0),
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"), kind=oc.LINE_CHECK,
        check_cost_usd=float(params.get("check_cost_usd") or 0.0),
        check_llm_calls=int(params.get("check_llm_calls") or 0), falsifier_lines=lines))
    failing = [line for line in lines if not line["pass"]]
    return {"status": "not_met" if failing or not lines else "met", "axes": lines, "failing": failing}
