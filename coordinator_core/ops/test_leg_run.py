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
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.ops import reverify_base_measure, reverify_scope
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.review_reverify import _RED_LINE
from coordinator_core.testing import scoped_pytest
from coordinator_core.win_portability import no_console_creationflags

#: Minutes one test leg may occupy the box; the whole command must finish inside the 10-minute
#: Bash tool ceiling the leg prompts set.
TEST_LEG_MINUTES_BUDGET = 9
#: Seconds held back from the cwd leg's deadline for the importer-tripwire phase.
TRIPWIRE_BUDGET_S = 120.0
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
USAGE = ("usage: test_leg_run [--base-sha <sha>] [--head-sha <sha>] [--base-export-out <path>] "
         "[--budget-s N] [--touched <path>]... --out <path> -- <ids and test files...>")
_PYTEST = ["-rfE", "-p", "no:cacheprovider"]
#: Runs pytest over the ids listed one per line in `sys.argv[1]`, with `sys.argv[2:]` as its flags: argv carries
#: no id list, so a list of any size spawns.
_IDS_RUNNER = ("import sys, pytest\n"
               "ids = [i for i in open(sys.argv[1], encoding='utf-8').read().split('\\n') if i]\n"
               "sys.exit(pytest.main([*sys.argv[2:], *ids]))\n")


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
    with tempfile.TemporaryDirectory(prefix="test-leg-ids-") as holder:
        id_file = _write_ids(Path(holder), targets)
        proc = subprocess.run([sys.executable, "-c", _IDS_RUNNER, str(id_file), *_PYTEST], cwd=cwd,
                              capture_output=True, text=True, errors="replace", timeout=remaining,
                              **no_console_creationflags())
    return proc.returncode, proc.stdout.splitlines()


def _write_ids(holder: Path, targets: Sequence[str]) -> Path:
    """`targets` one per line in `holder/ids.txt`, the file `_IDS_RUNNER` reads in place of argv."""
    id_file = holder / "ids.txt"
    id_file.write_text("\n".join(targets) + "\n", encoding="utf-8", newline="\n")
    return id_file


def _reds(lines: list[str]) -> dict[str, str]:
    """Red node id -> short message, in first-seen order, from pytest's `-rfE` summary lines."""
    reds: dict[str, str] = {}
    for raw in lines:
        text = raw.strip()
        hit = _RED_LINE.match(text)
        if hit and hit.group(1) not in reds:
            reds[hit.group(1)] = text[hit.end(1):].lstrip(" -")
    return reds


_SECTION = re.compile(r"^_{3,} (.+?) _{3,}$")
_PLAIN_ERR = re.compile(r"^\S+:\d+: \w")


def _file_part(node: str) -> str:
    """The forward-slash path before `::` (or pytest's ` > ` chain) of a node id."""
    return re.split(r"::| > ", node.replace("\\", "/"), maxsplit=1)[0].strip()


def _is_non_test(node: str) -> bool:
    """True when `node` names a `.py` file that is not a test module (`cov._is_test_path` false): pytest collects
    its production `test_*` helpers as fixture-less tests. Options and directory targets are never non-tests."""
    if node.startswith("-"):
        return False
    path = _file_part(node)
    return path.endswith(".py") and not cov._is_test_path(path)


def _split_not_a_test(nodes: Sequence[str], root: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """(tests, not_a_test): `not_a_test` is deduped in first-seen order. With `root`, a non-test file that is
    absent under it stays a target (reported as missing, not as a non-test)."""
    tests: list[str] = []
    non: list[str] = []
    for n in nodes:
        if _is_non_test(n) and (root is None or (root / _file_part(n)).exists()):
            if n not in non:
                non.append(n)
        else:
            tests.append(n)
    return tests, non


def _section_line(lines: Sequence[str], node: str) -> str:
    """The first `E   ` line (else the first `file:line: Error` line) of `node`'s traceback section, or ""."""
    leaf = node.split("::")[-1]
    path = _file_part(node)
    body: list[str] = []
    inside = False
    for raw in lines:
        hit = _SECTION.match(raw.strip())
        if hit:
            title = hit.group(1)
            inside = (title == leaf or title.endswith("." + leaf)
                      or (title.startswith("ERROR collecting") and path in title.replace("\\", "/")))
            continue
        if raw.startswith("====") or raw.startswith("----"):
            inside = False
        elif inside:
            body.append(raw)
    for raw in body:
        if raw.startswith("E ") and raw[1:].strip():
            return raw[1:].strip()
    return next((r.strip() for r in body if _PLAIN_ERR.match(r.strip())), "")


def _detail(text: str, ids: Sequence[str]) -> dict[str, str]:
    """Red id -> non-empty message: the summary line's text, else the id's traceback assertion line, else a
    statement that pytest printed none. Summary keys are matched after stripping, as `failing_ids` strips ids."""
    lines = text.splitlines()
    reds = {k.strip(): v for k, v in _reds(lines).items()}
    out: dict[str, str] = {}
    for i in ids:
        msg = reds.get(i.strip(), "").strip() or _section_line(lines, i)
        out[i] = msg or "FAILED; pytest printed no assertion line"
    return out


_COUNT = re.compile(r"(\d+) (passed|failed)\b")
_SUMMARY_TAIL = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_FRAME = re.compile(r"""^\s*(?:File ")?(\S+?\.py)(?:",\s*line |:)(\d+)\b""")
_IMPORT_FAIL = re.compile(r"^(?:E\s+)?(?:ImportError|ModuleNotFoundError)\b")
_ENGINE_PATH = re.compile(r"(?:^|/)(coordinator_core/.+\.py)$")
IMPORT_ERROR_KEY = "import_error"


def _import_error_path(lines: Sequence[str]) -> Optional[str]:
    """Repo-relative POSIX path of the innermost `coordinator_core` `.py` traceback frame (the importing
    file, not the module the message names) above the first ImportError / ModuleNotFoundError line; None
    when no engine frame precedes one."""
    frame: Optional[str] = None
    for raw in lines:
        hit = _FRAME.match(raw)
        if hit:
            found = _ENGINE_PATH.search(hit.group(1).replace("\\", "/"))
            if found:
                frame = found.group(1)
            continue
        if _IMPORT_FAIL.match(raw.strip()) and frame:
            return frame
    return None


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
    result.pop(IMPORT_ERROR_KEY, None)
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
        dirty = _import_error_path(lines)
        if dirty:
            cause = ltt.ZERO_RUN_IMPORT_ERROR
            result[IMPORT_ERROR_KEY] = {"path": dirty, "run_id": None}
        else:
            cause = ltt.ZERO_RUN_COLLECTION
    result[ltt.ZERO_RUN_CAUSE_KEY] = cause
    result["status"] = "error"


def _argv(root: Path, dest: Path, basetemp: Path, targets: Sequence[str]) -> list:
    """The clone leg's pytest argv with `targets` moved to `basetemp.parent/ids.txt` (the per-run holder, removed
    with the run): `_IDS_RUNNER` takes the flags `cov._pytest_argv` built and reads the ids from that file."""
    argv = cov._pytest_argv(root, dest, basetemp, targets)
    argv.insert(argv.index("--rootdir"), "--continue-on-collection-errors")
    flags = argv[3:len(argv) - len(targets)]
    id_file = _write_ids(basetemp.parent, targets)
    return [argv[0], "-c", _IDS_RUNNER, str(id_file), *flags]


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
        targets, not_a_test = _split_not_a_test(targets, dest)
        present, absent = _partition(targets, dest)
        seeds = tuple(dict.fromkeys([*present, *own]))
        picked, note = cov._with_importers(dest, seeds, rels)
        importers = list(picked[len(seeds):])
        try:
            base_rev = cov._commit_sha(root, base_sha) if base_sha else None
        except cov.ExportError:
            base_rev = None
        picked = cov.narrow_touched_tests(root, dest, base_rev, post, picked, rels)
        out: dict = {"importer_targets": importers, "note": note, "new_red": [], "targets_run": list(picked),
                     "not_a_test": not_a_test}
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
            ids, red_non = _split_not_a_test(cov.failing_ids(text))
            not_a_test.extend(n for n in red_non if n not in not_a_test)
            detail = _detail(text, ids)
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
               failing_detail=[{"node_id": i, "message": detail[i]} for i in ids], not_a_test=not_a_test)
    if ran.returncode not in (0, 1) and not ids and not cov.hygiene_only_exit(ran.returncode, text):
        out["failing_detail"] = [{"node_id": "pytest", "message": f"pytest exit {ran.returncode} with no failing id"}]
        out["status_after"] = "fail"
    else:
        out["status_after"] = "fail" if new_red else "pass"
    _tally(out, ran.returncode, text.splitlines())
    return out


def _files(nodes: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(_file_part(n) for n in nodes))


def _split_phases(present: list[str], base_sha: Optional[str], head_sha: Optional[str],
                  cwd: str) -> tuple[list[str], list[str]]:
    """(first, second): `second` holds the targets whose file is an importer tripwire of the diff, capped or
    cut (`tripwires` or `tripwires_not_selected`), so the owned-first split holds past the cap; one phase
    (second empty) with no `base_sha`, an unreadable diff, or nothing on either side of the split."""
    if not base_sha:
        return list(present), []
    try:
        picked = reverify_scope.select_tests(Path(cwd), base_sha, head_sha or "HEAD")
        tripwires = set(picked.get("tripwires") or ()) | set(picked.get("tripwires_not_selected") or ())
    except ValueError:
        return list(present), []
    second = [t for t in present if _file_part(t) in tripwires]
    first = [t for t in present if _file_part(t) not in tripwires]
    return (first, second) if first and second else (list(present), [])


def _cwd_phase(targets: list[str], cwd: str, left, all_reds: dict, shown: dict,
               red_non: list[str]) -> tuple[int, list[str], list[str]]:
    """One pytest phase in `cwd`: (rc, lines, charged red ids); folds the phase's reds into the shared maps."""
    rc, lines = _pytest(targets, cwd, left())
    found = _reds(lines)
    kept, non = _split_not_a_test(list(found))
    red_non.extend(non)
    all_reds.update({n: found[n] for n in kept})
    shown.update(_detail("\n".join(lines), kept))
    return rc, lines, kept


def _merge_phases(rc1: int, lines1: list[str], rc2: int, lines2: list[str]) -> tuple[int, list[str]]:
    """The two phases as one run: first non-green exit, and a summary line carrying the summed counts."""
    rcs = [rc1, rc2]
    rc = next((r for r in rcs if r not in (0, 5)), 0 if 0 in rcs else 5)
    counts = {"passed": 0, "failed": 0}
    for lines in (lines1, lines2):
        for n, word in _COUNT.findall(_summary_line("\n".join(lines))):
            counts[word] += int(n)
    return rc, [*lines1, *lines2, f"{counts['passed']} passed, {counts['failed']} failed in 0s"]


def _settle(result: dict, reds: list[str], cwd: str, base_sha: Optional[str], head_sha: Optional[str],
            base_export_out: Optional[str], left, exported: dict) -> None:
    """Rerun `reds` alone and measure them at `base_sha`, folding into `result`; `exported` accumulates the
    measures so the base export file always holds every phase settled so far. Raises TimeoutExpired."""
    if not reds:
        return
    rc, lines = _pytest(list(reds), cwd, left())
    again = _reds(lines)
    if rc in (0, 1):
        result["passed_alone"].extend(n for n in reds if n not in again)
    if not base_sha:
        return
    measured = reverify_base_measure.measure(base_sha, head_sha, list(reds), cwd, max(left(), 0.0))
    if measured["export"] == "timeout":
        raise subprocess.TimeoutExpired("baseline_export", 0)
    result["pre_existing"].extend(measured["red"])
    result["passed_at_base"].extend(measured["passed_at_base"])
    if base_export_out:
        exported.update(export=measured["export"], runner_exit=measured["runner_exit"],
                        red=[*exported.get("red", []), *measured["red"]],
                        passed_at_base=[*exported.get("passed_at_base", []), *measured["passed_at_base"]])
        reverify_base_measure.write_export(exported, Path(base_export_out))


def _own_import_error(result: dict, root: Path) -> None:
    """Fill `import_error.run_id` with the newest run record under `state/warp` whose touched-blobs entry
    for the dirty path holds the worktree's current blob; None (unowned) when no record claims it."""
    err = result.get(IMPORT_ERROR_KEY)
    if not isinstance(err, dict) or not err.get("path"):
        return
    from coordinator_core.ops.dispatch_emit import kept_work, stranded_sibling

    try:
        blob = kept_work.fingerprint(root, err["path"])
        if blob is None:
            return
        owners = [(mtime, run_id) for run_id, _dir, mtime, rec in stranded_sibling.read_records(root / "state" / "warp")
                  if (rec.get(err["path"]) or {}).get("run") == blob]
    except Exception:  # noqa: BLE001 -- ownership is advisory; the leg's verdict never fails on it
        return
    if owners:
        err["run_id"] = max(owners)[1]


def run(targets: list[str], *, base_sha: Optional[str], head_sha: Optional[str],
        base_export_out: Optional[str], budget_s: float, cwd: str, touched: Sequence[str] = ()) -> dict:
    """The `test`-shaped verdict for `targets`; `over_budget` true when the deadline expired. With `touched`
    (the landing's changed paths) the leg runs in the shared-clone mirror instead of `cwd` and adds `new_red`,
    `importer_targets`, `targets_run`; `head_sha` is then the post-land rev (default HEAD)."""
    started = time.monotonic()

    def left() -> float:
        return budget_s - (time.monotonic() - started)

    result: dict = {"status_after": "fail", "failing": [], "failing_detail": [], "pre_existing": [],
                    "reverted": [], "passed_alone": [], "passed_at_base": [], "diff_named": [],
                    "not_a_test": []}
    phase_one: Optional[tuple[int, list[str]]] = None
    unreached: list[str] = []
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
            _own_import_error(result, Path(cwd))
            result["elapsed_s"] = round(time.monotonic() - started, 3)
            result["budget_s"] = budget_s
            return result
        targets, result["not_a_test"] = _split_not_a_test(targets, Path(cwd))
        if not targets:
            result["status_after"] = "pass"
            _tally(result, 5, ())
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
        first, second = _split_phases(present, base_sha, head_sha, cwd)
        reserve = TRIPWIRE_BUDGET_S if second and budget_s > TRIPWIRE_BUDGET_S else 0.0

        def left_first() -> float:
            return left() - reserve

        all_reds: dict[str, str] = {}
        shown: dict[str, str] = {}
        red_non: list[str] = []
        exported: dict = {}
        rc, lines, reds = _cwd_phase(first, cwd, left_first, all_reds, shown, red_non)
        phase_one = (rc, lines)
        unreached = _files(second)
        _settle(result, reds, cwd, base_sha, head_sha, base_export_out, left_first, exported)
        if second:
            try:
                rc2, lines2, reds2 = _cwd_phase(second, cwd, left, all_reds, shown, red_non)
            except subprocess.TimeoutExpired:
                result[ltt.IMPORTER_OVERRUN_KEY] = True
                result[ltt.TRIPWIRES_NOT_RUN_KEY] = _files(second)
            else:
                rc, lines = _merge_phases(rc, lines, rc2, lines2)
                reds = [*reds, *reds2]
                try:
                    _settle(result, reds2, cwd, base_sha, head_sha, base_export_out, left, exported)
                except subprocess.TimeoutExpired:
                    result[ltt.IMPORTER_OVERRUN_KEY] = True
                    result[ltt.TRIPWIRES_NOT_RUN_KEY] = _files(reds2)
        result["not_a_test"].extend(n for n in dict.fromkeys(red_non) if n not in result["not_a_test"])
        result["failing"] = list(reds)
        result["failing_detail"] = [{"node_id": n, "message": all_reds[n].strip() or shown[n]} for n in reds]
        if rc == 0 or (red_non and not reds and rc == 1):
            result["status_after"] = "pass"
        elif not reds:
            result["failing_detail"] = [{"node_id": "pytest", "message": f"pytest exit {rc} with no failing id"}]
        result["over_budget"] = False
        _tally(result, rc, lines)
    except subprocess.TimeoutExpired:
        result["status_after"] = "fail"
        if phase_one is None:
            _tally(result, None, ())
        else:
            _tally(result, *phase_one)
            if unreached:
                result[ltt.IMPORTER_OVERRUN_KEY] = True
                result[ltt.TRIPWIRES_NOT_RUN_KEY] = unreached
        result["over_budget"] = True
        result["summary"] = f"timeout: {budget_s:g}s"
        result["failing_detail"] = [{"node_id": "test_leg_run", "message": result["summary"]}]
    except OSError as exc:
        result["status_after"] = "fail"
        result["over_budget"] = False
        result["summary"] = f"test leg spawn failed: {type(exc).__name__}: {exc}"
        result["failing_detail"] = [*result["failing_detail"], {"node_id": "test_leg_run", "message": result["summary"]}]
        if touched:
            result.setdefault("new_red", [])
        _tally(result, 2, ())
    _own_import_error(result, Path(cwd))
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
