"""test-leg-run: one bounded HEAD pytest over a test leg's scope, in the shape the halt re-verify
and review test legs return as `test`.

Runs pytest once over every target; reruns the reds once together (the ids passing there are
`passed_alone`); measures those reds once at `--base-sha` through reverify_base_measure. One
deadline covers the whole invocation. A bulk test run, outside the op budget by design.

With `--touched <path>` (repeatable; the landing's changed files) the leg runs the touched tests, the targets
and the direct importers of each touched module in the shared-clone mirror at `--head-sha` (default HEAD),
re-runs the reds at `--base-sha`, and reports `new_red`: red post-land, not red at base.

Usage: python -m coordinator_core.ops.test_leg_run [--base-sha <sha>] [--head-sha <sha>]
       [--base-export-out <path>] [--budget-s N] [--touched <path>]... --out <path> -- <ids and test files...>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.ops import reverify_base_measure
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.review_reverify import _RED_LINE
from coordinator_core.testing import scoped_pytest
from coordinator_core.win_portability import no_console_creationflags

#: Minutes one test leg may occupy the box; the whole command must finish inside the 10-minute
#: Bash tool ceiling the leg prompts set.
TEST_LEG_MINUTES_BUDGET = 9
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
USAGE = ("usage: test_leg_run [--base-sha <sha>] [--head-sha <sha>] [--base-export-out <path>] "
         "[--budget-s N] [--touched <path>]... --out <path> -- <ids and test files...>")
_PYTEST = ["-m", "pytest", "-rfE", "-p", "no:cacheprovider"]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="test_leg_run", add_help=False)
    p.add_argument("--base-sha")
    p.add_argument("--head-sha")
    p.add_argument("--base-export-out")
    p.add_argument("--budget-s", type=float)
    p.add_argument("--touched", action="append", default=[])
    p.add_argument("--out", required=True)
    return p


def _pytest(targets: list[str], cwd: str, remaining: float) -> tuple[int, list[str]]:
    """One pytest subprocess over `targets`: (exit code, stdout lines). Raises
    subprocess.TimeoutExpired once `remaining` seconds are gone; the child is killed by then."""
    if remaining <= 0:
        raise subprocess.TimeoutExpired("pytest", 0)
    proc = subprocess.run([sys.executable, *_PYTEST, *targets], cwd=cwd, capture_output=True, text=True,
                          errors="replace", timeout=remaining, **no_console_creationflags())
    return proc.returncode, proc.stdout.splitlines()


def _reds(lines: list[str]) -> dict[str, str]:
    """Red node id -> short message, in first-seen order, from pytest's `-rfE` summary lines."""
    reds: dict[str, str] = {}
    for raw in lines:
        text = raw.strip()
        hit = _RED_LINE.match(text)
        if hit and hit.group(1) not in reds:
            reds[hit.group(1)] = text[hit.end(1):].lstrip(" -")
    return reds


_COUNT = re.compile(r"(\d+) (passed|failed)\b")
_SUMMARY_TAIL = re.compile(r"\bin \d+(?:\.\d+)?s\b")


def _partition(targets: Sequence[str], root: Path) -> tuple[list[str], list[str]]:
    """(present, absent): a target is absent when its path part (before `::`) does not exist under `root`;
    an option-shaped target is never a path and stays present."""
    present: list[str] = []
    absent: list[str] = []
    for t in targets:
        path = t.split("::", 1)[0]
        (present if t.startswith("-") or (root / path).exists() else absent).append(t)
    return present, absent


def _stamp_all_missing(result: dict, absent: Sequence[str]) -> None:
    """No handed target exists on disk: no pytest ran. Records `missing_targets` and stamps the engine-side
    `missing-target` zero-run cause, its summary naming every absent path."""
    result["missing_targets"] = list(absent)
    result["summary"] = "missing target(s) absent from disk: " + ", ".join(absent)
    result["failing_detail"] = [{"node_id": "test_leg_run", "message": result["summary"]}]
    result["status_after"] = "fail"
    _tally(result, 5, (), missing_only=True)


def _tally(result: dict, rc: Optional[int], lines: Sequence[str], *, missing_only: bool = False) -> None:
    """Stamp `tests_run`, `tests_failed`, `status` and, when no test completed, `zero_run_cause` onto `result`.
    `rc` None is a deadline expiry; a negative `rc` is a POSIX signal kill (a Windows-killed child exits with a
    positive code and reads as collection-failure); the counts come from pytest's terminal summary line
    (errors are not completed tests). `missing_only`: no pytest ran because every target was absent."""
    counts = {"passed": 0, "failed": 0}
    for raw in reversed(list(lines)):
        if _SUMMARY_TAIL.search(raw):
            for n, word in _COUNT.findall(raw):
                counts[word] = int(n)
            break
    run_n = counts["passed"] + counts["failed"]
    result["tests_run"] = run_n
    result["tests_failed"] = counts["failed"]
    if result.get("status_after") == "pass" and run_n:
        result["status"] = "pass"
    else:
        result["status"] = "fail" if counts["failed"] else "error"
    result.pop(ltt.ZERO_RUN_CAUSE_KEY, None)
    if run_n:
        return
    if missing_only:
        cause = ltt.ZERO_RUN_MISSING_TARGET
    elif rc is None:
        cause = ltt.ZERO_RUN_BUDGET
    elif rc < 0:
        cause = ltt.ZERO_RUN_KILLED
    elif rc == 0 or rc == 5:
        cause = ltt.ZERO_RUN_EMPTY
    else:
        cause = ltt.ZERO_RUN_COLLECTION
    result[ltt.ZERO_RUN_CAUSE_KEY] = cause
    result["status"] = "error"


def _argv(root: Path, dest: Path, basetemp: Path, targets: Sequence[str]) -> list:
    argv = cov._pytest_argv(root, dest, basetemp, targets)
    argv.insert(argv.index("--rootdir"), "--continue-on-collection-errors")
    return argv


def _run_phased(root: Path, dest: Path, basetemp: Path, picked: Sequence[str], importers: Sequence[str],
                env: dict, left) -> tuple[subprocess.CompletedProcess, bool]:
    """(result, importer_overrun). The diff-named and handed targets run first; the importer tripwires run second
    under the remaining deadline, and an overrun there keeps the first phase's results instead of zeroing the leg."""
    imp = set(importers)
    first = [t for t in picked if t.split("::", 1)[0] not in imp]
    second = [t for t in picked if t.split("::", 1)[0] in imp]
    if not first or not second:
        return cov._tier_empty_as_green(cov._run_pytest_retrying(_argv(root, dest, basetemp, picked), dest, env)), False
    one = cov._tier_empty_as_green(cov._run_pytest_retrying(_argv(root, dest, basetemp, first), dest, env))
    if left() <= 0:
        return one, True
    try:
        two = cov._tier_empty_as_green(cov._run(_argv(root, dest, basetemp, second), dest, max(1, int(left())), env=env))
    except subprocess.TimeoutExpired:
        return one, True
    rcs = [one.returncode, two.returncode]
    rc = next((r for r in rcs if r not in (0, 5)), 0 if 0 in rcs else 5)
    counts = {w: 0 for w in ("passed", "failed")}
    for p in (one, two):
        for n, w in _COUNT.findall(_summary_line(p.stdout)):
            counts[w] += int(n)
    merged = f"{counts['passed']} passed, {counts['failed']} failed in 0s"
    return subprocess.CompletedProcess(one.args, rc, one.stdout + two.stdout + "\n" + merged + "\n", one.stderr + two.stderr), False


def _summary_line(stdout: str) -> str:
    for raw in reversed(stdout.splitlines()):
        if _SUMMARY_TAIL.search(raw):
            return raw
    return ""


def _clone_leg(targets: list[str], touched: list[str], base_sha: Optional[str], post_rev: str, root: Path,
               left) -> dict:
    """The new-red verdict: touched-file tests, `targets` and the direct importers of each touched module run in
    the shared-clone mirror at `post_rev`; the red ids, normalized by `failing_ids`, are re-run at `base_sha` in
    the same mirror. `new_red` is red post-land and not red at base; an unmeasured base excuses nothing.
    Raises ExportError when the mirror cannot be prepared, TimeoutExpired past the deadline."""
    run_holder = None
    with cov._mirror_lock(root, "test-leg-run") as mroot:
        post = cov._commit_sha(root, post_rev)
        dest = cov._prepare_guarded(root, mroot, post, cov._no_leg)
        rels = [t.replace("\\", "/") for t in touched]
        own = [t for t in rels if cov._is_test_path(t) and (dest / t).is_file()]
        present, absent = _partition(targets, dest)
        seeds = tuple(dict.fromkeys([*present, *own]))
        picked, note = cov._with_importers(dest, seeds, rels)
        importers = list(picked[len(seeds):])
        try:
            base_rev = cov._commit_sha(root, base_sha) if base_sha else None
        except cov.ExportError:
            base_rev = None
        picked = cov.narrow_touched_tests(root, dest, base_rev, post, picked, rels)
        out: dict = {"importer_targets": importers, "note": note, "new_red": [], "targets_run": list(picked)}
        if absent:
            out["missing_targets"] = absent
        if not picked:
            out.update(status_after="pass", failing=[], failing_detail=[], pre_existing=[])
            if absent and not present:
                _stamp_all_missing(out, absent)
            else:
                _tally(out, 5, ())
            return out
        try:
            run_holder, basetemp, tmp = scoped_pytest.per_run_dirs(root, f"test-leg-{os.getpid()}")
            env = cov._mirror_env(scoped_pytest.tmp_env(os.environ, tmp), dest)
            if left() <= 0:
                raise subprocess.TimeoutExpired("pytest", 0)
            ran, overrun = _run_phased(root, dest, basetemp, picked, importers, env, left)
            text = ran.stdout + ran.stderr
            if overrun:
                out["importer_overrun"] = True
            ids = list(cov.failing_ids(text))
            detail = _reds(text.splitlines())
            at_base: frozenset = frozenset()
            if ids and base_sha:
                base = cov._commit_sha(root, base_sha)
                if base != post:
                    at_base = cov._base_failing(dest, mroot, root, f"test-leg-{os.getpid()}", base, ids)
        finally:
            if run_holder is not None:
                shutil.rmtree(run_holder, ignore_errors=True)
    new_red = [i for i in ids if i not in at_base]
    out.update(failing=ids, pre_existing=[i for i in ids if i in at_base], new_red=new_red,
               failing_detail=[{"node_id": i, "message": detail.get(i, "")} for i in ids])
    if ran.returncode not in (0, 1) and not ids and not cov.hygiene_only_exit(ran.returncode, text):
        out["failing_detail"] = [{"node_id": "pytest", "message": f"pytest exit {ran.returncode} with no failing id"}]
        out["status_after"] = "fail"
    else:
        out["status_after"] = "fail" if new_red else "pass"
    _tally(out, ran.returncode, text.splitlines())
    return out


def run(targets: list[str], *, base_sha: Optional[str], head_sha: Optional[str],
        base_export_out: Optional[str], budget_s: float, cwd: str, touched: Sequence[str] = ()) -> dict:
    """The `test`-shaped verdict for `targets`; `over_budget` true when the deadline expired. With `touched`
    (the landing's changed paths) the leg runs in the shared-clone mirror instead of `cwd` and adds `new_red`,
    `importer_targets`, `targets_run`; `head_sha` is then the post-land rev (default HEAD)."""
    started = time.monotonic()

    def left() -> float:
        return budget_s - (time.monotonic() - started)

    result: dict = {"status_after": "fail", "failing": [], "failing_detail": [], "pre_existing": [],
                    "reverted": [], "passed_alone": [], "passed_at_base": [], "diff_named": []}
    try:
        if touched:
            try:
                result.update(_clone_leg(list(targets), list(touched), base_sha, head_sha or "HEAD", Path(cwd), left))
            except cov.ExportError as exc:
                result["summary"] = f"clone unavailable: {exc}"
                result["failing_detail"] = [{"node_id": "test_leg_run", "message": result["summary"]}]
                result["new_red"] = []
                _tally(result, 2, ())
            result["over_budget"] = False
            result["elapsed_s"] = round(time.monotonic() - started, 3)
            result["budget_s"] = budget_s
            return result
        present, absent = _partition(targets, Path(cwd))
        if absent:
            result["missing_targets"] = absent
        if not present:
            _stamp_all_missing(result, absent)
            result["over_budget"] = False
            result["elapsed_s"] = round(time.monotonic() - started, 3)
            result["budget_s"] = budget_s
            return result
        rc, lines = _pytest(present, cwd, left())
        reds = _reds(lines)
        result["failing"] = list(reds)
        result["failing_detail"] = [{"node_id": n, "message": m} for n, m in reds.items()]
        if rc == 0:
            result["status_after"] = "pass"
        elif not reds:
            result["failing_detail"] = [{"node_id": "pytest", "message": f"pytest exit {rc} with no failing id"}]
        if reds:
            _rc2, lines2 = _pytest(list(reds), cwd, left())
            again = _reds(lines2)
            if _rc2 in (0, 1):
                result["passed_alone"] = [n for n in reds if n not in again]
            if base_sha:
                measured = reverify_base_measure.measure(base_sha, head_sha, list(reds), cwd, max(left(), 0.0))
                if measured["export"] == "timeout":
                    raise subprocess.TimeoutExpired("baseline_export", budget_s)
                result["pre_existing"] = list(measured["red"])
                result["passed_at_base"] = list(measured["passed_at_base"])
                if base_export_out:
                    reverify_base_measure.write_export(measured, Path(base_export_out))
        result["over_budget"] = False
        _tally(result, rc, lines)
    except subprocess.TimeoutExpired:
        result["status_after"] = "fail"
        _tally(result, None, ())
        result["over_budget"] = True
        result["summary"] = f"timeout: {budget_s:g}s"
        result["failing_detail"] = [{"node_id": "test_leg_run", "message": result["summary"]}]
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    result["budget_s"] = budget_s
    return result


def _repo_root(start: Path) -> Path:
    """The nearest ancestor of `start` (itself included) holding a `.git` entry, else `start`. Targets are
    repo-relative, so a leg started below the root (an agent shell left in `coordinator/bin`) must still
    partition and run pytest at the root; a directory walk spawns nothing."""
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def main(argv: list[str]) -> int:
    argv = list(argv)
    if "--" not in argv:
        print(f"test_leg_run: bad arguments\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE
    cut = argv.index("--")
    targets = argv[cut + 1:]
    try:
        args = _parser().parse_args(argv[:cut])
    except ValueError:
        args = None
    if args is None or not targets:
        print(f"test_leg_run: bad arguments\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE
    budget_s = args.budget_s if args.budget_s is not None else TEST_LEG_MINUTES_BUDGET * 60.0
    cwd = str(_repo_root(Path(os.path.realpath(os.getcwd()))))
    result = run(targets, base_sha=args.base_sha, head_sha=args.head_sha,
                 base_export_out=args.base_export_out, budget_s=budget_s, cwd=cwd, touched=args.touched)
    body = json.dumps(result)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body + "\n", encoding="utf-8")
    print(body)
    return EXIT_TIMEOUT if result["over_budget"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
