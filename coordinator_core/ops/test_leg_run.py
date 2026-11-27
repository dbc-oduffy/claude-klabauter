"""test-leg-run: one bounded HEAD pytest over a test leg's scope, in the shape the halt re-verify
and review test legs return as `test`.

Runs pytest once over every target; reruns the reds once together (the ids passing there are
`passed_alone`); measures those reds once at `--base-sha` through reverify_base_measure. One
deadline covers the whole invocation. A bulk test run, outside the op budget by design.

With `--touched <path>` (repeatable; the landing's changed files) the leg runs the touched tests, the targets
and the direct importers of each touched module in the shared-clone mirror at `--head-sha` (default HEAD),
re-runs the reds at `--base-sha`, and reports `new_red`: red post-land, not red at base.

Without `--touched` the targets run in the same mirror at HEAD plus the run-owned blobs, never in the live
checkout, so another session's uncommitted edit cannot change the verdict (`own_tree`; `own_tree_skipped` when
the mirror cannot be prepared and the leg runs in place).

Usage: python -m coordinator_core.ops.test_leg_run [--base-sha <sha>] [--head-sha <sha>]
       [--base-export-out <path>] [--budget-s N] [--touched <path>]... [--repo-root <path>] --out <path> -- <ids and test files...>

`--repo-root` names the run's repo; the leg partitions targets and runs pytest there, not in the cwd's checkout.
Leg prompts pass it because the bound engine command can start in the engine clone. A root that is not a git
checkout is refused (`zero_run_cause` repo-root-unresolved), never replaced by the cwd.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Collection, Optional, Sequence

from coordinator_core.ops import reverify_base_measure, reverify_scope
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit import test_leg_freshness
from coordinator_core.ops.review_reverify import _RED_LINE
from coordinator_core import pyresolve, temp_layout
from coordinator_core.testing import leg_batch, scoped_pytest
from coordinator_core.win_portability import no_console_creationflags

#: Minutes one test leg may occupy the box; the whole command must finish inside the 10-minute
#: Bash tool ceiling the leg prompts set.
TEST_LEG_MINUTES_BUDGET = 9
#: Seconds held back from the cwd leg's deadline for the importer-tripwire phase.
TRIPWIRE_BUDGET_S = 120.0
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
ZERO_RUN_NO_REPO = "repo-root-unresolved"
USAGE = ("usage: test_leg_run [--base-sha <sha>] [--head-sha <sha>] [--base-export-out <path>] "
         "[--budget-s N] [--touched <path>]... [--repo-root <path>] --out <path> -- <ids and test files...>")
_PYTEST = ["-rfE", "-p", "no:cacheprovider"]
_PYTEST_ENCODING = "utf-8"
_PYTEST_STDOUT_LIMIT = 16384
#: Runs pytest over the ids listed one per line in `sys.argv[1]`, with `sys.argv[2:]` as its flags: argv carries
#: no id list, so a list of any size spawns.
_IDS_RUNNER = ("import sys, pytest, importlib.util\n"
               f"spec = importlib.util.spec_from_file_location('leg_reporter', {leg_batch.PLUGIN_FILE!r})\n"
               "rep = importlib.util.module_from_spec(spec)\n"
               "spec.loader.exec_module(rep)\n"
               "ids = [i for i in open(sys.argv[1], encoding='utf-8').read().split('\\n') if i]\n"
               "sys.exit(pytest.main([*sys.argv[2:], *ids], plugins=[rep]))\n")


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
    p.add_argument("--repo-root")
    p.add_argument("--run-id")
    p.add_argument("--out", required=True)
    return p


# (stderr, stdout) of each `_pytest` child since the last `_cwd_phase` start; the CLI runs one phase at a time.
_RAW: list[tuple[str, str]] = []
#: Mirror worktrees (as `str`) the current leg runs pytest in; `_pytest` resolves `coordinator_core` from them alone.
_MIRRORS: set[str] = set()
#: The run's repo root, set by `run`; the interpreter pin is read from it, never from a mirror.
_LEG_ROOT: list[Path] = []
#: The interpreter the last pytest of this leg ran under; `_tally` records it as `interpreter`.
_RAN_PY: list[str] = []


def _python(root: Path) -> str:
    """The interpreter every pytest of this leg runs under: the shared resolver the clean-overlay gate uses,
    so an engine interpreter without pytest never zeroes the leg."""
    return pyresolve.pytest_python(root)


def _declared_writes(root: Path, run_id: Optional[str]) -> list[str]:
    """The paths the run's own manifest declares writing (row writes, doc writes, write baseline); empty with no
    `run_id` or an unreadable manifest, so a peer's file the manifest does not name never reaches the mirror."""
    if not run_id:
        return []
    try:
        from coordinator_core.ops.dispatch_emit import stranded_sibling
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
        from coordinator_core.ops.dispatch_emit.halted_record_request import _manifest_doc

        return sorted(stranded_sibling._declared_paths(_manifest_doc(record_dir(root, run_id))))
    except (OSError, ValueError):
        return []


def _declared_overlay(root: Path, run_id: Optional[str]) -> tuple[list[str], dict[str, bytes]]:
    """`(paths, overrides)` the mirror carries for the run: its declared writes plus its own written paths
    (`cov.own_delta`), each at its worktree bytes, and a path a live peer run also declared at HEAD plus only
    this run's own hunks (`overrides`). A shared path the run declared with no splittable own hunks reaches the
    mirror at its worktree bytes (`cov.own_delta`), never HEAD or absent. Empty with no `run_id` or an
    unreadable delta."""
    if not run_id:
        return [], {}
    try:
        paths, overrides = cov.own_delta(root, run_id, _declared_writes(root, run_id))
    except Exception:  # noqa: BLE001 -- the overlay only widens the mirror; an unreadable delta leaves HEAD
        return [], {}
    return list(dict.fromkeys(paths)), dict(overrides)


def _reviewed_scope(root: Path, run_id: Optional[str]) -> list[str]:
    """Repo-relative paths the run's review prep snapshots froze (the reverify's `edited` source), minus any a
    live peer run declared: a module the run created that no declared write names, which only the snapshot
    manifest carries. Spawn-free; empty with no `run_id` or an unreadable snapshot or session set."""
    if not run_id:
        return []
    try:
        from coordinator_core.ops.dispatch_emit import review_edits

        sibling = {review_edits.norm(p) for p in review_edits.live_sibling_declared(root, run_id)}
        return [p for p in cov._snapshot_scope(root, run_id) if p not in sibling]
    except Exception:  # noqa: BLE001 -- the scope only widens the mirror; an unreadable snapshot leaves HEAD
        return []


def _live_only(root: Path, dest: Path, targets: Sequence[str], declared: Sequence[str] = ()) -> list[str]:
    """Repo-relative file parts of `targets`, then the run's `declared` writes, that are files in the live `root`
    but absent from the mirror `dest`: an untracked file the run wrote (a test or the source module it imports),
    which a HEAD mirror cannot hold until it is overlaid."""
    out: dict[str, None] = {}
    for t in targets:
        path = _file_part(t)
        if path and not t.startswith("-") and (root / path).is_file() and not (dest / path).exists():
            out[path] = None
    for path in declared:
        if path and not path.startswith("-") and (root / path).is_file() and not (dest / path).exists():
            out[path] = None
    return list(out)


def _prepare_live(root: Path, mroot: Path, sha: str, overlay: Sequence[str],
                  overrides: Optional[dict[str, bytes]]) -> Path:
    """The mirror with `overlay` laid over `sha`, unguarded: the overlay is the run's own delta, and a peer's
    dirty module it imports stays at HEAD, so `_check_overlay_imports` would refuse the very test whose import
    is absent there. That absence surfaces as the leg's zero-run import-error verdict instead."""
    try:
        return cov._prepare(root, mroot, sha, cov._no_leg, overlay, None, overrides or None)
    except subprocess.TimeoutExpired as exc:
        raise cov.ExportError(f"clean export timed out: {exc}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise cov.ExportError(f"clean export did not complete: {type(exc).__name__}: {exc}") from exc


def _pytest(targets: list[str], cwd: str, remaining: float) -> tuple[int, list[str]]:
    """Return pytest's exit code and output lines, including bounded stderr on abnormal exits.

    On ordinary exits retain only the session-Temp residue diagnostics from stderr. A timeout
    kills the child before raising subprocess.TimeoutExpired. The child's raw stderr and stdout go to
    `_RAW` for the receipt's `pytest_phases`.
    """
    if remaining <= 0:
        raise subprocess.TimeoutExpired("pytest", 0)
    env = pyresolve.with_engine_root(dict(os.environ, PYTHONIOENCODING=_PYTEST_ENCODING))
    if cwd in _MIRRORS:
        env = _mirror_env(env, Path(cwd))
    py = _RAN_PY[0] if _RAN_PY else _python(_LEG_ROOT[0] if _LEG_ROOT else Path(cwd))
    _RAN_PY[:] = [py]
    with tempfile.TemporaryDirectory(prefix="test-leg-ids-") as holder:
        id_file = _write_ids(Path(holder), targets)
        proc = subprocess.run([py, "-c", _IDS_RUNNER, str(id_file), *_PYTEST], cwd=cwd,
                              capture_output=True, encoding=_PYTEST_ENCODING, errors="replace", timeout=remaining,
                              env=env,
                              **no_console_creationflags())
    _RAW.append((proc.stderr, proc.stdout))
    stderr_lines = (
        proc.stderr[-2000:].splitlines()
        if proc.returncode not in (0, 1)
        else cov.temp_residue_lines(proc.stderr)
    )
    return proc.returncode, [*proc.stdout.splitlines(), *stderr_lines]


def _conftest_constants(*names: str) -> dict[str, str]:
    """String constants `names` read from the engine conftest's source: importing it mutates sys.path and
    the environment, so the lines it prints are parsed against its own values without running it."""
    out: dict[str, str] = {}
    try:
        tree = ast.parse((Path(__file__).resolve().parents[1] / "conftest.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return out
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in names and isinstance(node.value, ast.Constant)):
            out[node.targets[0].id] = node.value.value
    return out


def _temp_leaks(text: str) -> list[tuple[str, str]]:
    """(node id, message) per session-Temp leak in `text`, [] when it carries no residue forced exit. A
    `left by <label>: <entry>` line charges the leaving test (the file for an `(after last test)` label); a
    residue with no such line (a conftest older than the seam) is charged per entry as `managed-temp:<entry>`."""
    lines = cov.temp_residue_lines(text)
    if not lines:
        return []
    const = _conftest_constants("MANAGED_TEMP_LEFT_BY_PREFIX", "MANAGED_TEMP_AFTER_LAST_TEST")
    prefix = const.get("MANAGED_TEMP_LEFT_BY_PREFIX")
    after = const.get("MANAGED_TEMP_AFTER_LAST_TEST", "")
    leaks: list[tuple[str, str]] = []
    for ln in lines:
        if prefix and ln.startswith(prefix):
            label, _, entry = ln[len(prefix):].rpartition(": ")
            node = label[:-len(after)] if after and label.endswith(after) else label
            if node.startswith("<no test>"):
                node = f"managed-temp:{entry}"
            leaks.append((node, f"managed-temp leak: {entry} left by {label}"))
    if leaks:
        return leaks
    entry_prefix = f"managed-temp: {cov._RESIDUE_CAUSE}: "
    return [(f"managed-temp:{ln[len(entry_prefix):]}", f"managed-temp leak: {ln[len(entry_prefix):]} {cov._RESIDUE_CAUSE}")
            for ln in lines if ln.startswith(entry_prefix)]


def _stamp_leaks(result: dict, leaks: Sequence[tuple[str, str]]) -> list[str]:
    """Record `leaks` as the leg's failures and return their node ids (deduped, first-seen order)."""
    ids = list(dict.fromkeys(node for node, _ in leaks))
    result["failing"] = ids
    result["failing_detail"] = [{"node_id": node, "message": msg} for node, msg in leaks]
    result["status_after"] = "fail"
    return ids


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


_SECTION = re.compile(r"^_+ (.+?) _+$")
_COLLECTING = "ERROR collecting "
_PLAIN_ERR = re.compile(r"^\S+:\d+: \w")
_EXCEPTION_LINE = re.compile(r"^(?:\w+\.)*\w*(?:Error|Exception|Unresolved)(?::|$)")


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
    """Separate non-test files, retaining directories and missing paths for target resolution."""
    tests: list[str] = []
    non: list[str] = []
    for n in nodes:
        path = _file_part(n)
        if root is None:
            non_test = _is_non_test(n)
        else:
            non_test = (root / path).is_file() and (not path.endswith(".py") or _is_non_test(n))
        if non_test:
            if n not in non:
                non.append(n)
        else:
            tests.append(n)
    return tests, non


def _same_file(path: str, shown: str) -> bool:
    """True when a `ERROR collecting` title path names `path`: pytest prints the title relative to its rootdir
    (`bash_guards/tests/x.py` under `coordinator_core`) while the node id carries the repo-relative path."""
    shown = shown.replace("\\", "/").strip()
    return bool(path) and bool(shown) and (path == shown or path.endswith("/" + shown) or shown.endswith("/" + path))


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
                      or (title.startswith(_COLLECTING) and _same_file(path, title[len(_COLLECTING):])))
            continue
        if raw.startswith("====") or raw.startswith("----"):
            inside = False
        elif inside:
            body.append(raw)
    for raw in body:
        line = raw.strip()
        if line.startswith("E ") and line[1:].strip():
            return line[1:].strip()
        if _EXCEPTION_LINE.match(line):
            return line
    return next((r.strip() for r in body if _PLAIN_ERR.match(r.strip())), "")


def _detail(text: str, ids: Sequence[str]) -> dict[str, str]:
    """Red id -> non-empty message: the summary line's text (the id's traceback line when pytest cut the summary
    with "..."), else the id's traceback assertion line, else a statement that pytest printed none. Summary keys are matched after stripping, as `failing_ids` strips ids."""
    lines = text.splitlines()
    reds = {k.strip(): v for k, v in _reds(lines).items()}
    out: dict[str, str] = {}
    for i in ids:
        msg = reds.get(i.strip(), "").strip()
        if not msg or msg.endswith("..."):  # pytest cuts a summary line to the terminal width with "..."
            msg = _section_line(lines, i) or msg
        if re.search(r"\bSettingsKeyUnresolved:", msg):
            msg = f"environment: machine-local settings key unreadable ({msg})"
        out[i] = msg or cov.collection_traceback(text) or "FAILED; pytest printed no exception or assertion detail"
    return out


def _no_id_message(rc: object, text: str, tail: str = "") -> str:
    """`pytest exit <rc> with no failing id`, then pytest's collection traceback (stdout's ERRORS section) when
    the output holds one, then `tail`."""
    message = f"pytest exit {rc} with no failing id"
    found = cov.kept_collection_traceback(text, tail).rstrip("\n")
    return "\n".join(part for part in (message, found, tail) if part)


# An xfailed/xpassed test ran to its expected outcome; a leg whose only test is xfail-marked is not empty.
_COUNT = re.compile(r"(\d+) (passed|failed|xfailed|xpassed)\b")
_SUMMARY_TAIL = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_SKIPPED = re.compile(r"(\d+) skipped\b")
_ERRORS = re.compile(r"\b\d+ errors?\b")
_FRAME = re.compile(r"""^\s*(?:File ")?(\S+?\.py)(?:",\s*line |:)(\d+)\b""")
_IMPORT_FAIL = re.compile(r"^(?:E\s+)?(?:ImportError|ModuleNotFoundError)\b")
_ENGINE_PATH = re.compile(r"(?:^|/)(coordinator_core/.+\.py)$")
IMPORT_ERROR_KEY = "import_error"
_MISSING_IMPORT = re.compile(r"ImportError: cannot import name '([^']+)' from '([^']+)'")
#: Scratch key `_tally` leaves for `_name_foreign_dependency`; never reaches the verdict.
_MISSING_KEY = "_missing_import"


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


def _tally(result: dict, rc: Optional[int], lines: Sequence[str], stderr: Optional[str] = None,
           *, missing_only: bool = False) -> None:
    """Stamp `tests_run`, `tests_failed`, `status` and, when no test completed, `zero_run_cause` onto `result`.
    `rc` None is a deadline expiry; a negative `rc` is a POSIX signal kill.
    The counts come from pytest's terminal summary line
    (errors are not completed tests). `missing_only`: no pytest ran because every target was absent."""
    if stderr is not None:
        result["pytest_exit_code"] = rc
        result["pytest_stderr"] = stderr[-4096:]
    counts = {"passed": 0, "failed": 0, "xfailed": 0, "xpassed": 0}
    skipped, errored = 0, False
    for raw in reversed(list(lines)):
        if _SUMMARY_TAIL.search(raw):
            for n, word in _COUNT.findall(raw):
                counts[word] = int(n)
            skipped = sum(int(n) for n in _SKIPPED.findall(raw))
            errored = bool(_ERRORS.search(raw))
            break
    run_n = counts["passed"] + counts["failed"] + counts["xfailed"] + counts["xpassed"]
    result["tests_run"] = run_n
    result["tests_failed"] = counts["failed"]
    if _RAN_PY:
        result["interpreter"] = _RAN_PY[0]
    if result.get("status_after") == "pass" and run_n:
        result["status"] = "pass"
    else:
        result["status"] = "fail" if counts["failed"] or result.get("failing") else "error"
    result.pop(ltt.ZERO_RUN_CAUSE_KEY, None)
    result.pop(IMPORT_ERROR_KEY, None)
    if run_n:
        return
    if skipped and rc in (0, 5) and not missing_only and not errored and not result.get("failing"):
        # Selection skipped wholesale (e.g. a POSIX-only module on Windows): not-applicable, not an empty selection.
        result.update(status="pass", status_after="pass", tests_skipped=skipped, selection_skipped=True)
        if [d.get("node_id") for d in result.get("failing_detail") or []] == ["pytest"]:
            result["failing_detail"] = []
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
        missing = next((m.groups() for m in map(_MISSING_IMPORT.search, lines) if m), None)
        if missing:
            result[_MISSING_KEY] = missing
        dirty = _import_error_path(lines)
        if dirty:
            cause = ltt.ZERO_RUN_IMPORT_ERROR
            result[IMPORT_ERROR_KEY] = {"path": dirty, "run_id": None}
        elif cov.collection_error(rc, "\n".join(lines)):
            cause = ltt.ZERO_RUN_COLLECTION
        else:
            cause = ltt.ZERO_RUN_PROCESS_ERROR
    result[ltt.ZERO_RUN_CAUSE_KEY] = cause
    result["status"] = "error"
    # Names where pytest ran: interpreter and process cwd, no spawn.
    result["interpreter"], result["cwd"] = (_RAN_PY[0] if _RAN_PY else sys.executable), os.getcwd()
    no_module = next((ln.strip() for ln in lines if _IMPORT_FAIL.match(ln.strip())), None)
    if no_module:
        result["import_error_text"] = no_module
    # A mirror leg can collect tests and then deliberately exclude their entire tier.
    if rc == 0 and any(line.startswith("every collected test is in an excluded tier (") for line in lines):
        return
    result["status_after"] = "fail"
    if not result.get("failing_detail"):
        message = f"ran zero tests ({cause})"
        found = cov.collection_traceback("\n".join(lines))
        result["failing_detail"] = [
            {"node_id": "test_leg_run", "message": f"{message}\n{found}" if found else message},
        ]


def _mirror_env(env: dict, dest: Path) -> dict:
    """`cov._mirror_env`, keeping the resolved engine root when the mirror carries no `coordinator_core` (a foreign repo)."""
    out = cov._mirror_env(env, dest)
    if not (dest / "coordinator_core").is_dir():
        out[pyresolve.ENGINE_ROOT_ENV] = env[pyresolve.ENGINE_ROOT_ENV]
    return out


def _argv(root: Path, dest: Path, basetemp: Path, targets: Sequence[str]) -> list:
    """The clone leg's pytest argv with `targets` moved to `basetemp.parent/ids.txt` (the per-run holder, removed
    with the run): `_IDS_RUNNER` takes the flags `cov._pytest_argv` built and reads the ids from that file."""
    argv = cov._pytest_argv(root, dest, basetemp, targets)
    _RAN_PY[:] = [argv[0]]
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
    counts = {w: 0 for w in ("passed", "failed", "xfailed", "xpassed")}
    for p in (one, two):
        for n, w in _COUNT.findall(_summary_line(p.stdout)):
            counts[w] += int(n)
    merged = ", ".join(f"{n} {w}" for w, n in counts.items() if n) + " in 0s"
    return subprocess.CompletedProcess(one.args, rc, one.stdout + two.stdout + "\n" + merged + "\n", one.stderr + two.stderr), False


def _summary_line(stdout: str) -> str:
    for raw in reversed(stdout.splitlines()):
        if _SUMMARY_TAIL.search(raw):
            return raw
    return ""


def _can_reconcile_reds(rc: int, lines: Sequence[str]) -> bool:
    if rc not in (0, 1):
        return False
    summary = _summary_line("\n".join(lines))
    return not (re.search(r"\b\d+ errors?\b", summary) and not _COUNT.search(summary))


def _clone_leg(targets: list[str], touched: list[str], base_sha: Optional[str], post_rev: str, root: Path,
               left, run_id: Optional[str] = None) -> dict:
    """The new-red verdict: touched-file tests, `targets` and the direct importers of each touched module run in
    the shared-clone mirror at `post_rev`; the red ids, normalized by `failing_ids`, are re-run at `base_sha` in
    the same mirror. `new_red` is red post-land and not red at base; an unmeasured base excuses nothing.
    Raises ExportError when the mirror cannot be prepared, TimeoutExpired past the deadline."""
    run_holder = None
    with cov._mirror_lock(root, "test-leg-run") as mroot:
        post = cov._commit_sha(root, post_rev)
        owned, overrides = _declared_overlay(root, run_id)
        dest = _prepare_live(root, mroot, post, owned, overrides)
        live = _live_only(root, dest, targets, _reviewed_scope(root, run_id))
        if live:
            dest = _prepare_live(root, mroot, post, [*owned, *live], overrides)
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
            env = _mirror_env(pyresolve.with_engine_root(scoped_pytest.tmp_env(os.environ, tmp)), dest)
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
    leaks = _temp_leaks(text) if ran.returncode == 1 and not ids else []
    if leaks:
        new_red = _stamp_leaks(out, leaks)
        out["new_red"] = new_red
    elif ran.returncode not in (0, 1) and not ids and not cov.hygiene_only_exit(ran.returncode, text):
        out["failing_detail"] = [{"node_id": "pytest", "message": _no_id_message(ran.returncode, text)}]
        out["status_after"] = "fail"
    else:
        out["status_after"] = "fail" if new_red else "pass"
    _tally(out, ran.returncode, text.splitlines(), ran.stderr)
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
               red_non: list[str], batch: Optional[dict] = None) -> tuple[int, list[str], list[str], str, str]:
    """One phase in `cwd`: (rc, lines, charged red ids, raw stderr, raw stdout); folds the phase's reds into the
    shared maps. The targets run as sequential legs sized to the cap (`leg_batch`); over-cap files land in
    `batch["slow"]`."""
    _RAW.clear()
    rc, lines, info = leg_batch.run_legs(targets, cwd, _pytest, left, TEST_LEG_MINUTES_BUDGET * 60.0,
                                         temp_layout.fleet_temp_root() / "test-leg" / scoped_pytest.new_run_slug())
    stderr, stdout = "\n".join(e for e, _ in _RAW if e), "".join(o for _, o in _RAW)
    if batch is not None:
        batch.setdefault("slow", []).extend(info["slow"])
    found = _reds(lines)
    kept, non = _split_not_a_test(list(found))
    red_non.extend(non)
    all_reds.update({n: found[n] for n in kept})
    shown.update(_detail("\n".join(lines), kept))
    return rc, lines, kept, stderr, stdout


def _merge_phases(rc1: int, lines1: list[str], rc2: int, lines2: list[str]) -> tuple[int, list[str]]:
    """The two phases as one run: first non-green exit, and a summary line carrying the summed counts."""
    rcs = [rc1, rc2]
    rc = next((r for r in rcs if r not in (0, 5)), 0 if 0 in rcs else 5)
    counts = {"passed": 0, "failed": 0, "xfailed": 0, "xpassed": 0}
    for lines in (lines1, lines2):
        for n, word in _COUNT.findall(_summary_line("\n".join(lines))):
            counts[word] += int(n)
    return rc, [*lines1, *lines2, ", ".join(f"{n} {w}" for w, n in counts.items() if n or w in ("passed", "failed")) + " in 0s"]


def _settle(result: dict, reds: list[str], cwd: str, base_sha: Optional[str], head_sha: Optional[str],
            base_export_out: Optional[str], left, exported: dict, run_cwd: Optional[str] = None) -> None:
    """Rerun `reds` alone (in `run_cwd`, default `cwd`) and measure them at `base_sha` from `cwd`, folding into
    `result`; `exported` accumulates the measures so the base export file always holds every phase settled so
    far. Raises TimeoutExpired."""
    if not reds:
        return
    rc, lines = _pytest(list(reds), run_cwd or cwd, left())
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
    if measured["export"] != "ok" or measured["runner_exit"] not in (0, 1, 4):
        unverified = result.setdefault("unverified_at_base", [])
        unverified.extend(n for n in reds if n not in unverified)
    if base_export_out:
        exported.update(export=measured["export"], runner_exit=measured["runner_exit"],
                        red=[*exported.get("red", []), *measured["red"]],
                        passed_at_base=[*exported.get("passed_at_base", []), *measured["passed_at_base"]])
        reverify_base_measure.write_export(exported, Path(base_export_out))


def _own_whole_paths(root: Path, run_id: Optional[str]) -> frozenset[str]:
    """The run's recorded write set minus every path a live peer run also declared: the paths the mirror carries
    at the run's whole worktree bytes, so no foreign hunk of theirs is withheld from it. A path a peer shares
    is excluded (its foreign hunks stay out of the mirror). Spawn-free; empty with no `run_id` or an unreadable record."""
    if not run_id:
        return frozenset()
    try:
        from coordinator_core.ops.dispatch_emit import review_edits

        sibling = {review_edits.norm(p) for p in review_edits.live_sibling_declared(root, run_id)}
        return frozenset(p for p in map(review_edits.norm, cov._own_write_set(root, run_id)) if p not in sibling)
    except Exception as exc:  # noqa: BLE001 -- an unknown own set attributes as before
        print(f"own-whole-paths unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return frozenset()


def _name_foreign_dependency(result: dict, root: Path, own: "Collection[str] | Callable[[], Collection[str]]" = ()) -> None:
    """A zero-run whose ImportError names a symbol defined only in a dirty (uncommitted) worktree file is a
    peer's uncommitted code under this run's committed hunk, not a test failure: restamps the
    `uncommitted-foreign-dependency` cause with `import_error.{symbol,module,source_path}`. Any other
    zero-run keeps its cause. `own` are the run's own whole-overlaid paths (`_own_whole_paths`): a file the
    mirror carries at the run's bytes is never the foreign source. An untracked module file at the imported
    module's path counts as dirty (one index read). Advisory: a git or read failure leaves the verdict as tallied."""
    missing = result.pop(_MISSING_KEY, None)
    if not missing or result.get("tests_run"):
        return
    symbol, module = missing
    defn = re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(symbol)}(?!\w)|^{re.escape(symbol)}\s*[:=]", re.M)
    try:
        out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "HEAD", "--", "*.py"],
                             capture_output=True, text=True, timeout=10,
                             **no_console_creationflags()).stdout
        mod = module.replace(".", "/")
        found = [p for p in out.splitlines() if p.endswith(".py")]
        from coordinator_core.ops.dispatch_emit import test_reach

        tracked = test_reach._tracked(root)
        found += [p for p in (mod + ".py", mod + "/__init__.py")
                  if p not in found and p not in tracked and (root / p).is_file()]
        skip = {p.replace("\\", "/") for p in (own() if callable(own) else own)}
        dirty = sorted((p for p in found if p.replace("\\", "/") not in skip),
                       key=lambda p: not (p == mod + ".py" or p == mod + "/__init__.py"))
        source = next((p for p in dirty[:50] if defn.search((root / p).read_text(encoding="utf-8", errors="replace"))), None)
    except (subprocess.SubprocessError, OSError):
        return
    if source is None:
        return
    result[ltt.ZERO_RUN_CAUSE_KEY] = ltt.ZERO_RUN_FOREIGN_DEPENDENCY
    result["failing"] = []
    if "new_red" in result:
        result["new_red"] = []
    result[IMPORT_ERROR_KEY] = {"path": source, "run_id": None, "symbol": symbol, "module": module,
                                "source_path": source}
    result["failing_detail"] = [{"node_id": "test_leg_run", "message":
                                 f"ran zero tests (uncommitted-foreign-dependency): '{symbol}' is imported from "
                                 f"'{module}' but defined only in uncommitted {source}"}]


def _imports_from_overlaid_target(result: dict, targets: Sequence[str]) -> bool:
    """True when a target file the mirror overlaid from the run's own uncommitted bytes is what failed to import:
    its missing name can live only in a peer's uncommitted worktree code. A committed importer failing in the mirror
    is a committed break, never a peer's dirt."""
    overlaid = {p.replace("\\", "/") for p in (result.get("own_tree") or {}).get("overlaid", [])}
    return any(t.split("::", 1)[0].replace("\\", "/") in overlaid for t in targets)


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


def _run_blob_paths(root: Path, exclude: str, run_id: Optional[str] = None) -> list[str]:
    """Paths whose worktree bytes are still a blob a run record under `state/warp` wrote, `exclude` left out;
    with `run_id` only that run's record counts, so a peer run's uncommitted blob never reaches the leg."""
    from coordinator_core.ops.dispatch_emit import kept_work, stranded_sibling

    owned: dict[str, None] = {}
    for rid, _dir, _mtime, rec in stranded_sibling.read_records(root / "state" / "warp"):
        if run_id is not None and rid != run_id:
            continue
        for path, entry in rec.items():
            if path != exclude and entry.get("run") and kept_work.fingerprint(root, path) == entry["run"]:
                owned[path] = None
    return list(owned)


def _recorded_sources(root: Path, run_id: Optional[str]) -> tuple[list[str], dict[str, bytes]]:
    """`(paths, overrides)`: paths `run_id`'s own record lists as touched (`touched-blobs.json`, the run's blob
    differing from its base) or withheld (the halt index entry) whose worktree file exists, at their worktree
    bytes even when the path is tracked at HEAD and the record blob no longer matches. A path a live peer run
    also declared stays in, at HEAD plus only the run's own hunks (`overrides`, `cov.own_hunk_overrides`), or
    at its worktree bytes when the hunks cannot be split: it is the run's own write, never dropped to HEAD.
    Spawn-free; empty with no `run_id` or an unreadable record."""
    if not run_id:
        return [], {}
    try:
        from coordinator_core.ops.dispatch_emit import halted_withheld_index, review_edits, stranded_sibling
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        listed = [p for p, e in stranded_sibling.read(record_dir(root, run_id)).items() if e.get("run") != e.get("base")]
        entry = halted_withheld_index.run_entry(root, run_id)
        if entry is not None:
            listed.extend(entry.paths)
        sibling = {review_edits.norm(p) for p in review_edits.live_sibling_declared(root, run_id)}
        out = (review_edits.norm(p) for p in listed)
        paths = [p for p in dict.fromkeys(out)
                 if p and not p.startswith("-") and not review_edits.out_of_worktree(p) and (root / p).is_file()]
        shared = [p for p in paths if p in sibling]
        return paths, (cov.own_hunk_overrides(root, run_id, shared) if shared else {})
    except Exception:  # noqa: BLE001 -- the record only widens the mirror; an unreadable one leaves HEAD
        return [], {}


def _own_tree(stack: contextlib.ExitStack, root: Path, head_sha: Optional[str], result: dict,
              run_id: Optional[str] = None, targets: Sequence[str] = (), declared_run: Optional[str] = None) -> str:
    """The directory the no-touched leg runs pytest in: the shared-clone mirror at `head_sha` (default HEAD) plus
    the run-owned blobs, held under `stack`, so another session's uncommitted edit in `root` never reaches the
    verdict. A non-git `root` has no foreign dirt to exclude and runs in place; a mirror that cannot be prepared
    runs in place too, noted as `own_tree_skipped` (the leg never refuses over it). `own_tree` records the
    commit and the overlaid paths."""
    if not (root / ".git").exists():
        return str(root)
    try:
        mroot = stack.enter_context(cov._mirror_lock(root, "test-leg-run"))
        sha = cov._commit_sha(root, head_sha or "HEAD")
        owned, overrides = _declared_overlay(root, declared_run)
        recorded, recorded_overrides = _recorded_sources(root, declared_run)
        overrides = {**recorded_overrides, **overrides}
        overlay = list(dict.fromkeys([*_run_blob_paths(root, "", run_id), *owned, *recorded, *overrides]))
        dest = _prepare_live(root, mroot, sha, overlay, overrides)
        live = _live_only(root, dest, targets, _reviewed_scope(root, declared_run))
        if live:
            overlay = [*overlay, *live]
            dest = _prepare_live(root, mroot, sha, overlay, overrides)
    except (cov.ExportError, OSError, ValueError, subprocess.SubprocessError) as exc:
        stack.close()
        result["own_tree_skipped"] = f"{type(exc).__name__}: {exc}"[:300]
        return str(root)
    _MIRRORS.add(str(dest))
    stack.callback(_MIRRORS.discard, str(dest))
    result["own_tree"] = {"at": sha, "overlaid": overlay}
    return str(dest)


def _overlay_fallback(result: dict, targets: Sequence[str], base_sha: Optional[str], root: Path, left,
                      run_id: Optional[str] = None, declared_run: Optional[str] = None) -> None:
    """A zero-run caused by an import error in a file no run owns says nothing about the run's own change: re-run
    `targets` on the `base_sha` export overlaid with the run-owned blobs (the foreign file at its base bytes) and
    fold that verdict into `result`. Any failure to measure there keeps the cwd verdict, noted as
    `overlay_rerun_skipped`."""
    err = result.get(IMPORT_ERROR_KEY)
    if (result.get("tests_run") or not base_sha or not isinstance(err, dict) or err.get("run_id") is not None
            or not err.get("path")):
        return
    run_holder = None
    try:
        if left() <= 0:
            raise subprocess.TimeoutExpired("pytest", 0)
        owned, overrides = _declared_overlay(root, declared_run)
        recorded, recorded_overrides = _recorded_sources(root, declared_run)
        overrides = {k: v for k, v in {**recorded_overrides, **overrides}.items() if k != err["path"]}
        overlay = list(dict.fromkeys([*_run_blob_paths(root, err["path"], run_id),
                                      *(p for p in [*owned, *recorded, *overrides] if p != err["path"])]))
        with cov._mirror_lock(root, "test-leg-run") as mroot:
            base_rev = cov._commit_sha(root, base_sha)
            dest = _prepare_live(root, mroot, base_rev, overlay, overrides)
            live = _live_only(root, dest, targets, [p for p in _reviewed_scope(root, declared_run) if p != err["path"]])
            if live:
                dest = _prepare_live(root, mroot, base_rev, [*overlay, *live], overrides)
            present, _absent = _partition(targets, dest)
            if not present:
                return
            run_holder, basetemp, tmp = scoped_pytest.per_run_dirs(root, f"test-leg-{os.getpid()}")
            env = _mirror_env(pyresolve.with_engine_root(scoped_pytest.tmp_env(os.environ, tmp)), dest)
            ran = cov._tier_empty_as_green(cov._run_pytest_retrying(_argv(root, dest, basetemp, present), dest, env))
    except (cov.ExportError, subprocess.SubprocessError, OSError) as exc:
        result["overlay_rerun_skipped"] = f"{type(exc).__name__}: {exc}"[:300]
        return
    finally:
        if run_holder is not None:
            shutil.rmtree(run_holder, ignore_errors=True)
    text = ran.stdout + ran.stderr
    ids, red_non = _split_not_a_test(cov.failing_ids(text))
    detail = _detail(text, ids)
    result["failing"] = ids
    result["failing_detail"] = [{"node_id": i, "message": detail[i]} for i in ids]
    result["not_a_test"].extend(n for n in red_non if n not in result["not_a_test"])
    leaks = _temp_leaks(text) if ran.returncode == 1 and not ids else []
    if leaks:
        _stamp_leaks(result, leaks)
    elif ids or (ran.returncode not in (0, 5) and not red_non):
        result["status_after"] = "fail"
        if not ids:
            result["failing_detail"] = [{"node_id": "pytest", "message": _no_id_message(ran.returncode, text)}]
    else:
        result["status_after"] = "pass"
    result["overlay_rerun"] = {"from": err["path"], "overlaid": overlay}
    _tally(result, ran.returncode, text.splitlines(), ran.stderr)


def _stamp_slow(result: dict, slow: list[dict]) -> None:
    """Name the over-cap files the plan excluded: typed test_infra, never a pass; the test owner splits them."""
    result["slow_targets"] = slow
    result["unrun_targets"] = [s["file"] for s in slow]
    result["leg_type"] = "test_infra"
    result["status_after"] = "fail"
    if result.get("status") == "pass":
        result["status"] = "error"
    result["failing_detail"] = [
        *result["failing_detail"],
        *({"node_id": s["file"], "message": f"slow target: {s['seconds']:g}s alone exceeds the "
           f"{TEST_LEG_MINUTES_BUDGET * 60}s leg cap; split it (test_infra)"} for s in slow)]


def run(targets: list[str], *, base_sha: Optional[str], head_sha: Optional[str],
        base_export_out: Optional[str], budget_s: float, cwd: str, touched: Sequence[str] = (),
        run_id: Optional[str] = None, declared_run: Optional[str] = None) -> dict:
    """The `test`-shaped verdict for `targets`; `over_budget` true when the deadline expired. With `touched`
    (the landing's changed paths) the leg runs in the shared-clone mirror instead of `cwd` and adds `new_red`,
    `importer_targets`, `targets_run`; `head_sha` is then the post-land rev (default HEAD). `run_id` limits the run-owned overlay to that run's
    record; None overlays every run record. The untracked paths the manifest of `declared_run` (default `run_id`)
    declares are overlaid too, sources and tests alike."""
    decl = declared_run or run_id
    started = time.monotonic()
    _LEG_ROOT[:] = [Path(cwd)]
    _RAN_PY.clear()

    def left() -> float:
        return budget_s - (time.monotonic() - started)

    result: dict = {"status_after": "fail", "failing": [], "failing_detail": [], "pre_existing": [],
                    "reverted": [], "passed_alone": [], "passed_at_base": [], "diff_named": [],
                    "not_a_test": []}
    phase_one: Optional[tuple[int, list[str], str]] = None
    unreached: list[str] = []
    stack = contextlib.ExitStack()
    try:
        if touched:
            try:
                result.update(_clone_leg(
                    list(targets), list(touched), base_sha, head_sha or "HEAD", Path(cwd), left, decl))
            except cov.ExportError as exc:
                result["summary"] = f"clone unavailable: {exc}"
                result["failing_detail"] = [{"node_id": "test_leg_run", "message": result["summary"]}]
                result["new_red"] = []
                _tally(result, 2, ())
            result["over_budget"] = False
            _name_foreign_dependency(result, Path(cwd), lambda: _own_whole_paths(Path(cwd), decl))
            _own_import_error(result, Path(cwd))
            result["elapsed_s"] = round(time.monotonic() - started, 3)
            result["budget_s"] = budget_s
            return result
        run_cwd = _own_tree(stack, Path(cwd), head_sha, result, run_id, targets, decl)
        targets, result["not_a_test"] = _split_not_a_test(targets, Path(run_cwd))
        if not targets:
            _tally(result, 5, ())
            result["over_budget"] = False
            result["elapsed_s"] = round(time.monotonic() - started, 3)
            result["budget_s"] = budget_s
            return result
        present, absent = _partition(targets, Path(run_cwd))
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
        batch: dict = {}
        rc, lines, reds, stderr, stdout = _cwd_phase(first, run_cwd, left_first, all_reds, shown, red_non, batch)
        result["pytest_phases"] = [{"exit_code": rc, "stderr": stderr[-4096:],
                                    "stdout": stdout[-_PYTEST_STDOUT_LIMIT:], "encoding": _PYTEST_ENCODING}]
        phase_one = (rc, lines, stderr)
        unreached = _files(second)
        if reds and _can_reconcile_reds(rc, lines):
            _settle(result, reds, cwd, base_sha, head_sha, base_export_out, left_first, exported, run_cwd)
        if second:
            try:
                rc2, lines2, reds2, stderr2, stdout2 = _cwd_phase(second, run_cwd, left, all_reds, shown, red_non, batch)
            except subprocess.TimeoutExpired:
                result[ltt.IMPORTER_OVERRUN_KEY] = True
                result[ltt.TRIPWIRES_NOT_RUN_KEY] = _files(second)
            else:
                result["pytest_phases"].append({"exit_code": rc2, "stderr": stderr2[-4096:],
                                                "stdout": stdout2[-_PYTEST_STDOUT_LIMIT:], "encoding": _PYTEST_ENCODING})
                rc, lines = _merge_phases(rc, lines, rc2, lines2)
                stderr = "\n".join(filter(None, (stderr, stderr2)))
                reds = [*reds, *reds2]
                try:
                    if reds2 and _can_reconcile_reds(rc2, lines2):
                        _settle(result, reds2, cwd, base_sha, head_sha, base_export_out, left, exported, run_cwd)
                except subprocess.TimeoutExpired:
                    result[ltt.IMPORTER_OVERRUN_KEY] = True
                    result[ltt.TRIPWIRES_NOT_RUN_KEY] = _files(reds2)
        result["not_a_test"].extend(n for n in dict.fromkeys(red_non) if n not in result["not_a_test"])
        result["failing"] = list(reds)
        result["failing_detail"] = [{"node_id": n, "message": shown.get(n) or all_reds[n].strip()} for n in reds]
        if rc == 0 or (red_non and not reds and rc == 1):
            result["status_after"] = "pass"
        elif not reds:
            leaks = _temp_leaks("\n".join(lines)) if rc == 1 else []
            if leaks:
                _stamp_leaks(result, leaks)
            else:
                joined = "\n".join(lines)
                message = _no_id_message(rc, joined, joined[-2000:] if rc not in (0, 1) and lines else "")
                result["failing_detail"] = [{"node_id": "pytest", "message": message}]
        result["over_budget"] = False
        _tally(result, rc, lines, stderr)
        if batch.get("slow"):
            _stamp_slow(result, batch["slow"])
        if result.get(IMPORT_ERROR_KEY):
            _own_import_error(result, Path(cwd))
            if "own_tree" not in result:
                _overlay_fallback(result, present, base_sha, Path(cwd), left, run_id, decl)
    except subprocess.TimeoutExpired as exc:
        result["status_after"] = "fail"
        credit = getattr(exc, "credit", None)
        if credit and credit["done"] and phase_one is None:
            _tally(result, None, ())
            result.update(leg_batch.credit_fields(credit))
            result["status"] = "fail" if result["failing"] else "error"
            result.pop(ltt.ZERO_RUN_CAUSE_KEY, None)
            result["leg_type"] = "test_infra"
        elif phase_one is None:
            _tally(result, None, ())
            if credit:
                # Trap: a deadline with no finished test still names every file it never reached.
                result.update(unrun_targets=list(dict.fromkeys(credit["unrun"])), slow_targets=credit["slow"])
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
    finally:
        stack.close()
    if "own_tree" in result and not _imports_from_overlaid_target(result, targets):
        result.pop(_MISSING_KEY, None)
    else:
        _name_foreign_dependency(result, Path(cwd), lambda: _own_whole_paths(Path(cwd), decl))
    _own_import_error(result, Path(cwd))
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    result["budget_s"] = budget_s
    return result


#: Result keys: `fixture_watch` maps a node id recorded failing to `{file, sha256, recorded_at}`; `flagged_fixture_edits`
#: lists each later change to such a node's file. Both ride in `--out` and are read back from it by the next leg.
FIXTURE_WATCH_KEY = "fixture_watch"
FIXTURE_EDITS_KEY = "flagged_fixture_edits"


def _file_sha(root: Path, rel: str) -> Optional[str]:
    """sha256 of `rel` under `root`; None when absent or unreadable (a deleted test file reads as a change)."""
    try:
        return hashlib.sha256((root / rel).read_bytes()).hexdigest()
    except OSError:
        return None


def _flag_fixture_edits(result: dict, prior: Optional[dict], root: Path, run_id: Optional[str],
                        measured_at: str) -> None:
    """Record the files of this leg's failing node ids and flag a recorded-failing id whose file changed since.

    `prior` is the run's previous `--out` record: each `fixture_watch` entry whose file now hashes differently
    is appended to `flagged_fixture_edits` (naming test, file and this leg), sticky across later legs. A flag is
    advisory: status and verdict are untouched, and a hash is read per failing file only, no git spawn.
    """
    old = prior.get(FIXTURE_WATCH_KEY) if isinstance(prior, dict) else None
    old = old if isinstance(old, dict) else {}
    carried = prior.get(FIXTURE_EDITS_KEY) if isinstance(prior, dict) else None
    edits = [e for e in carried if isinstance(e, dict)] if isinstance(carried, list) else []
    failing = [n for n in result.get("failing", ()) if isinstance(n, str)]
    watch: dict[str, dict] = {}
    for node, entry in old.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            continue
        now = _file_sha(root, entry["file"])
        if now != entry.get("sha256"):
            edits.append({"test": node, "file": entry["file"], "recorded_at": entry.get("recorded_at"),
                          "recorded_sha256": entry.get("sha256"), "now_sha256": now,
                          "now_failing": node in failing,
                          "leg": {"run_id": run_id, "measured_at": measured_at}})
        else:
            watch[node] = entry
    for node in failing:
        path = _file_part(node)
        if node not in watch and "::" in node and path.endswith(".py"):
            sha = _file_sha(root, path)
            if sha is not None:
                watch[node] = {"file": path, "sha256": sha, "recorded_at": measured_at}
    if watch:
        result[FIXTURE_WATCH_KEY] = watch
    if edits:
        result[FIXTURE_EDITS_KEY] = edits


def _prior_record(out: Path) -> Optional[dict]:
    """The `--out` file's previous JSON object (the run's last leg), None when absent or unreadable."""
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _repo_root(start: Path) -> Path:
    """The nearest ancestor of `start` (itself included) holding a `.git` entry, else `start`. Targets are
    repo-relative, so a leg started below the root (an agent shell left in `coordinator/bin`) must still
    partition and run pytest at the root; a directory walk spawns nothing."""
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def _leg_root(explicit: Optional[str]) -> tuple[Optional[Path], str]:
    """(root, error). An explicit `--repo-root` is the run's repo and is used as given: the bound engine command
    starts in whatever cwd the agent holds (the engine clone for a consumer-repo run), and the cwd walk would
    then partition the targets against the engine. An explicit root that is not a git checkout is refused, never
    replaced by the cwd walk. Without one the cwd walk applies."""
    if explicit is None:
        return _repo_root(Path(os.path.realpath(os.getcwd()))), ""
    root = Path(os.path.realpath(explicit))
    if not (root / ".git").exists():
        return None, f"--repo-root {explicit} is not a git checkout"
    return root, ""


def _derive_targets(base_sha: str, head_sha: Optional[str], cwd: str) -> list[str]:
    """The diff's touched tests then their owners (never tripwires), for a call that named no target; empty
    when the diff is unreadable or touches no test."""
    try:
        return list(reverify_scope.select_tests(Path(cwd), base_sha, head_sha or "HEAD")["tests"])
    except ValueError:
        return []


def _run_id(args) -> Optional[str]:
    """`--run-id`, else the `--out` directory's name when it holds a touched-blobs record (the run's own dir)."""
    if args.run_id:
        return args.run_id
    from coordinator_core.ops.dispatch_emit import stranded_sibling

    out_dir = Path(args.out).resolve().parent
    return out_dir.name if (out_dir / stranded_sibling.TOUCHED_FILE).is_file() else None


def _declared_run(args) -> Optional[str]:
    """The run whose manifest declares the writes the overlay carries: `--run-id`, else the `--out` directory's
    name when it holds a manifest. Kept apart from `_run_id`, which also narrows the recorded-blob overlay."""
    if args.run_id:
        return args.run_id
    out_dir = Path(args.out).resolve().parent
    return out_dir.name if (out_dir / "manifest.json").is_file() else None


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
    if args is None or (not targets and not args.base_sha):
        print(f"test_leg_run: bad arguments\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE
    budget_s = args.budget_s if args.budget_s is not None else TEST_LEG_MINUTES_BUDGET * 60.0
    root, root_error = _leg_root(args.repo_root)
    if root is None:
        refused = {"status": "error", "status_after": "error", "tests_run": 0, "zero_run_cause": ZERO_RUN_NO_REPO,
                   "error": root_error, "over_budget": False}
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(refused) + "\n", encoding="utf-8")
        print(json.dumps(refused))
        return EXIT_USAGE
    cwd = str(root)
    derived = not targets
    if derived:
        targets = _derive_targets(args.base_sha, args.head_sha, cwd)
    if not targets:
        refused = {"status": "error", "status_after": "fail", "tests_run": 0, "zero_run_cause": ltt.ZERO_RUN_EMPTY,
                   "failing_detail": [{"node_id": "test_leg_run", "message": "no test target given and none derivable "
                                       f"from the diff {args.base_sha}..{args.head_sha or 'HEAD'}"}],
                   "over_budget": False}
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(refused) + "\n", encoding="utf-8")
        print(json.dumps(refused))
        return EXIT_USAGE
    measured_at, head_at_start = test_leg_freshness.started_stamp(root, args.head_sha)
    prior = _prior_record(Path(args.out))
    result = run(targets, base_sha=args.base_sha, head_sha=args.head_sha,
                 base_export_out=args.base_export_out, budget_s=budget_s, cwd=cwd, touched=args.touched,
                 run_id=_run_id(args), declared_run=_declared_run(args))
    if derived:
        result["derived_targets"] = targets
    _flag_fixture_edits(result, prior, root, _run_id(args) or _declared_run(args), measured_at)
    mirror = result.get("own_tree")
    mirror_at = mirror.get("at") if isinstance(mirror, dict) else None
    result.update(test_leg_freshness.stamp({}, measured_at=measured_at, head_sha=mirror_at or head_at_start))
    body = json.dumps(result)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body + "\n", encoding="utf-8")
    if "pytest_phases" in result:
        relay = {**result, "pytest_phases": [
            {key: value for key, value in phase.items() if key not in ("stdout", "encoding")}
            for phase in result["pytest_phases"]
        ]}
        body = json.dumps(relay)
    print(body)
    return EXIT_TIMEOUT if result["over_budget"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
