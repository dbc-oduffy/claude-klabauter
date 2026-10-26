"""`dispatch.objective_calibrate`: wave-0 falsifier calibration at base and credited shas, then the pin.

No LLM. The falsifier must tell undone from done: red at base on every axis not credited, green at
each credited sha on that sha's credited axes. Only a passing verdict pins the falsifier sha256.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Optional

from coordinator_core.git.run import run_git_stream
from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import objective_contract as oc
from coordinator_core.win_portability import no_console_creationflags

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
_SHA = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _atomic_write(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _run_falsifier(script: Path, cwd: Path, axes: list) -> list:
    """Run the script by argv (no shell) with `cwd` as the tree; any doubt is a failed axis line."""
    try:
        proc = subprocess.run(
            oc.falsifier_argv(script), cwd=str(cwd), capture_output=True, text=True,
            timeout=oc.FALSIFIER_TIMEOUT_S, **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return [oc.AxisResult(axis=oc._axis_id(a), passed=False, reason=oc.REASON_TIMEOUT) for a in axes]
    except OSError as exc:
        return [oc.AxisResult(axis=oc._axis_id(a), passed=False, reason=oc.REASON_UNPARSEABLE, evidence=str(exc)[:200])
                for a in axes]
    return oc.parse_falsifier_lines(proc.stdout, axes)


def _materialise(repo_root: Path, sha: str, dest: Path) -> Optional[str]:
    """One `git archive` spawn extracted with tarfile; returns an error string or None."""
    if not _SHA.match(sha):
        return "bad_sha"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        with run_git_stream(["-C", str(repo_root), "archive", "--format=tar", sha],
                            timeout=oc.FALSIFIER_TIMEOUT_S) as proc:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                tar.extractall(dest, filter="data")
            proc.stdout.read()  # drain the archive padding so git can exit
            rc = proc.wait()
    except (tarfile.TarError, OSError) as exc:
        return f"archive_failed: {exc}"
    return None if rc == 0 else f"archive_failed: git archive exit {rc}"


def _check_credited(repo_root: Path, run_id: str, script: Path, axes: list, entry: dict) -> tuple:
    """One credited sha: materialise, run the falsifier there, return (out_entry, problems)."""
    sha = str(entry.get("sha") or "")
    if not _SHA.match(sha):  # before sha joins a path: the finally below rmtree-s dest
        return {"sha": sha, "lines": []}, [{"where": sha, "axis": None, "problem": "bad_sha"}]
    src_root = Path(entry.get("repo_root") or repo_root)
    dest = repo_root / "scratch" / "objective-calibration" / run_id / sha
    problems: list = []
    try:
        err = _materialise(src_root, sha, dest)
        if err:
            return {"sha": sha, "lines": []}, [{"where": sha, "axis": None, "problem": err}]
        results = _run_falsifier(script, dest, axes)
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    by_axis = {r.axis: r for r in results}
    for axis_id in entry.get("axes") or []:
        r = by_axis.get(axis_id)
        if r is None or not r.passed:
            problems.append({"where": sha, "axis": axis_id,
                             "problem": "red at a credited sha" + (f": {r.reason}" if r and r.reason else "")})
    return {"sha": sha, "lines": _lines(results)}, problems


def _lines(results: list) -> list:
    return [r.to_line() for r in results]


def calibrate(params: dict, repo_root: Path) -> dict:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    run_id = params.get("run_id")
    at = params.get("at", oc.PINNED_AT_WAVE0)
    if at not in oc.PINNED_AT_VALUES:
        return {"calibrated": False, "error": f"at must be one of {oc.PINNED_AT_VALUES}"}
    try:
        run_dir = record_dir(repo_root, run_id)
    except ValueError as exc:
        return {"calibrated": False, "error": str(exc)}
    obj_path = run_dir / oc.OBJECTIVE_FILE
    if not obj_path.is_file():
        return {"calibrated": False, "error": "no objective.json"}
    try:
        objective = oc.Objective.from_json(json.loads(obj_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        return {"calibrated": False, "error": f"objective unreadable: {exc}"}
    script = (repo_root / objective.falsifier_script).resolve()
    if not script.is_file():
        return {"calibrated": False, "error": "falsifier script missing"}
    axes = objective.axes
    ids = [oc._axis_id(a) for a in axes]
    credited_in = [c for c in params.get("credited") or [] if isinstance(c, dict)]
    credited_all = {a for c in credited_in for a in c.get("axes") or []}

    problems: list = []
    base_results = _run_falsifier(script, repo_root, axes)
    for r in base_results:
        if r.reason:
            problems.append({"where": "base", "axis": r.axis, "problem": f"unusable falsifier output: {r.reason}"})
        elif r.axis not in credited_all and r.passed:
            problems.append({"where": "base", "axis": r.axis, "problem": "green at base on an undone axis"})

    credited_out = []
    if credited_in:
        with ThreadPoolExecutor(max_workers=min(len(credited_in), 4)) as pool:
            checked = list(pool.map(partial(_check_credited, repo_root, str(run_id), script, axes), credited_in))
        for out_entry, entry_problems in checked:
            credited_out.append(out_entry)
            problems.extend(entry_problems)

    sha256 = hashlib.sha256(script.read_bytes()).hexdigest()
    candidate = objective.falsifier_candidate_sha256
    if not problems and not objective.falsifier_sha256 and candidate and candidate != sha256:
        # A passing run that cannot pin leaves every later check `unpinned`: fail it so the rewrite leg runs.
        problems.append({"where": "pin", "axis": None, "problem": "candidate_sha_mismatch"})
    verdict = VERDICT_FAIL if problems else VERDICT_PASS
    cal = oc.Calibration(base=_lines(base_results), credited=credited_out, verdict=verdict)

    pin_error = None
    pinned = False
    if verdict == VERDICT_PASS:
        if objective.falsifier_sha256:
            pin_error = "already_pinned"
        else:
            data = json.loads(obj_path.read_text(encoding="utf-8"))
            data["falsifier_sha256"] = sha256
            data["pinned_at"] = at
            _atomic_write(obj_path, data)
            pinned = True
            base_path = run_dir / oc.BASE_FILE
            if not base_path.exists():
                _atomic_write(base_path, {"lines": _lines(base_results)})

    line = oc.LoopLine(
        wave=0, iteration=0, kind=oc.LINE_CALIBRATION, axes=[{"axis": r.axis, "pass": r.passed, "now": r.now}
                                                              for r in base_results],
        at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), falsifier_lines=_lines(base_results),
        reason=verdict,
    ).to_json()
    line["calibration"] = cal.to_json()
    with (run_dir / oc.LOOP_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")

    out = {"calibrated": True, "verdict": verdict, "pinned": pinned, "problems": problems,
           "axes": ids, "calibration": cal.to_json()}
    if pin_error:
        out["pin_error"] = pin_error
    return out


@register_op("dispatch.objective_calibrate")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: ``run_id``, ``at`` (sizing|wave0), ``credited`` [{sha, repo_root, axes}].

    Runs the falsifier at the current tree (base) and at each credited sha; appends a wave-0
    calibration loop line; pins falsifier_sha256 and pinned_at only on a passing verdict.
    """
    return calibrate(params, Path(repo_root) if repo_root else Path.cwd())
