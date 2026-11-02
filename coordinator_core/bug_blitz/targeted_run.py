"""targeted-test-run: select a row's covering tests, admit the argv, run it under a Tier-T slot, write the record.

Selection is files only (the row's failing test file, touched test files, covering tests of touched
sources). Admission is `classify_runner_footprint` == scoped with no precision or suite hit; anything
else is `refused` and nothing runs. An empty selection is `nothing-to-run`: there is no fallback to
any tier. `tests/test_runner_shapes_admitted.py::ADMITTED_HEADS` pins the head table below.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Sequence

from coordinator_core.bug_blitz.contract import (
    EXIT_CODES,
    OUTCOME_FAILED,
    OUTCOME_FOREIGN_UNCOMMITTED,
    OUTCOME_NO_RUNNER,
    OUTCOME_NOTHING_TO_RUN,
    OUTCOME_PASSED,
    OUTCOME_REFUSED,
    RunnerRecord,
)
from coordinator_core.bug_blitz.foreign_edits import foreign_dirty, head_tree, holders_of
from coordinator_core.git.run import run_git
from coordinator_core.win_portability import no_console_creationflags

DEFAULT_TIMEOUT = 300
SLOT_WAIT_S = 300.0
_TIMEOUT_EXIT = 124

_PY_TEST_RE = re.compile(r"^(test_.+|.+_test)\.py$")
_JS_EXT = r"(?:ts|tsx|js|jsx|mjs|cjs|mts|cts)"
_JS_TEST_RE = re.compile(rf"^(?P<stem>.+)\.(?:test|spec)\.{_JS_EXT}$")
_JS_SOURCE_RE = re.compile(rf"^(?P<stem>.+)\.{_JS_EXT}$")
_VITEST_FAIL_RE = re.compile(r"^\s*(?:FAIL|×)\s+(\S.*)$", re.MULTILINE)

# Head argv tables, one per runner; the package manager is picked from the lockfile.
_VITEST_HEADS = {
    "pnpm": ["pnpm", "exec", "vitest", "run"],
    "npm": ["npx", "vitest", "run"],
    "yarn": ["yarn", "exec", "vitest", "run"],
}
_JEST_HEADS = {"pnpm": ["pnpm", "exec", "jest"]}
_LOCKFILES = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm"))


def is_test_file(rel: str) -> bool:
    """True for a Python or JS/TS test file by basename convention."""
    name = PurePosixPath(rel).name
    return bool(_PY_TEST_RE.match(name) or _JS_TEST_RE.match(name))


def _is_py(rel: str) -> bool:
    return rel.endswith(".py")


def _normalize(repo_root: Path, raw: str) -> str:
    p = Path(raw)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(repo_root.resolve())
        except ValueError:
            return ""
    return p.as_posix()


def _load_row(row_path: Path) -> tuple[str, str]:
    """(row id, surface) of a backlog row yaml; the id falls back to the file stem."""
    import yaml

    data = yaml.safe_load(row_path.read_text(encoding="utf-8")) or {}
    row_id = str(data.get("id") or row_path.stem)
    return row_id, str(data.get("surface") or "")


def _js_basename_index(repo_root: Path) -> dict[str, list[str]]:
    proc = run_git(["ls-files", "-z"], cwd=str(repo_root))
    index: dict[str, list[str]] = {}
    for rel in proc.stdout.split("\0"):
        m = _JS_TEST_RE.match(PurePosixPath(rel).name) if rel else None
        if m:
            index.setdefault(m.group("stem"), []).append(rel)
    return index


def select(
    repo_root: Path, row_path: Path, touched: Sequence[str] = ()
) -> tuple[list[str], list[str]]:
    """(selected test files, dropped directories). Files only, sorted, unique, existing."""
    repo_root = Path(repo_root)
    _, surface = _load_row(Path(row_path))
    chosen: set[str] = set()
    dropped: list[str] = []
    py_sources: list[str] = []
    js_stems: list[str] = []

    surface_rel = _normalize(repo_root, surface) if surface else ""
    if surface_rel and is_test_file(surface_rel):
        chosen.add(surface_rel)

    for raw in touched:
        rel = _normalize(repo_root, raw)
        if not rel:
            continue
        if (repo_root / rel).is_dir():
            dropped.append(rel)
        elif is_test_file(rel):
            chosen.add(rel)
        elif _is_py(rel):
            py_sources.append(rel)
        else:
            m = _JS_SOURCE_RE.match(PurePosixPath(rel).name)
            if m:
                js_stems.append(m.group("stem"))

    if py_sources:
        from coordinator_core.source_test_map import map_changed_sources

        chosen.update(map_changed_sources(py_sources, str(repo_root))[0])
    if js_stems:
        index = _js_basename_index(repo_root)
        for stem in js_stems:
            chosen.update(index.get(stem, ()))

    return sorted(rel for rel in chosen if (repo_root / rel).is_file()), dropped


def _package_manager(repo_root: Path) -> str:
    for lockfile, pm in _LOCKFILES:
        if (repo_root / lockfile).is_file():
            return pm
    return "npm"


def _js_runner(repo_root: Path) -> str:
    try:
        pkg = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "vitest"
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    return "jest" if "jest" in deps and "vitest" not in deps else "vitest"


def head_for(repo_root: Path, files: Sequence[str]) -> tuple[str, list[str]] | None:
    """(runner label, head argv) for one same-language file group, or None when no head is admitted."""
    if all(_is_py(f) for f in files):
        return "pytest", [sys.executable, "-m", "pytest"]
    if any(_is_py(f) for f in files):
        return None
    runner = _js_runner(repo_root)
    heads = _JEST_HEADS if runner == "jest" else _VITEST_HEADS
    head = heads.get(_package_manager(repo_root))
    if head is None or shutil.which(head[0]) is None:
        return None
    return runner, list(head)


def admit(argv: Sequence[str], repo_root: Path) -> bool:
    """True only when the shared Tier-T classifier calls `argv` a scoped run with no suite or precision hit."""
    from coordinator_core.bash_guards.check_test_suite_invocation import (
        RUNNER_FOOTPRINT_SCOPED,
        classify_command,
        classify_command_precision,
        classify_runner_footprint,
    )

    cmd = shlex.join(argv)
    cwd = str(repo_root)
    return (
        classify_runner_footprint(cmd, cwd=cwd) == RUNNER_FOOTPRINT_SCOPED
        and not classify_command_precision(cmd, cwd=cwd)
        and not classify_command(cmd, cwd=cwd)
    )


def _failing_ids(output: str) -> list[str]:
    from coordinator_core.testing.suite_run_record import parse_failed_ids, strip_ansi

    text = strip_ansi(output)
    ids = list(parse_failed_ids(text) or [])
    ids += [m.strip() for m in _VITEST_FAIL_RE.findall(text)]
    return list(dict.fromkeys(ids))


def _spawn(argv: Sequence[str], cwd: Path, timeout: int) -> tuple[int, str]:
    resolved = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    try:
        proc = subprocess.run(
            resolved,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            errors="replace",
            **no_console_creationflags(),
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        out = exc.output if isinstance(exc.output, str) else (exc.output or b"").decode("utf-8", "replace")
        return _TIMEOUT_EXIT, out + f"\n[TIMEOUT] exceeded {timeout}s\n"


def _group_by_language(files: Sequence[str]) -> list[list[str]]:
    py = [f for f in files if _is_py(f)]
    js = [f for f in files if not _is_py(f)]
    return [g for g in (py, js) if g]


def run(
    repo_root: Path,
    row_path: Path,
    touched: Sequence[str] = (),
    *,
    timeout: int = DEFAULT_TIMEOUT,
    committed_head: bool = False,
) -> RunnerRecord:
    """Select, admit and run one row's targeted tests; the returned record carries the outcome.

    `committed_head` judges committed HEAD with no uncommitted overlay (the already-fixed and
    NOT_REPRODUCED question); the default judges HEAD plus the `touched` edits.
    """
    from coordinator_core.testing import tier_t_slots

    start = time.monotonic()
    repo_root = Path(repo_root)
    row_id, _ = _load_row(Path(row_path))

    def record(outcome, *, runner="", argv=(), selected=(), exit_code=None, failing=(), foreign_holders=()):
        return RunnerRecord(
            row_id=row_id,
            runner=runner,
            argv=tuple(argv),
            selected=tuple(selected),
            outcome=outcome,
            exit_code=exit_code,
            failing_ids=tuple(failing),
            foreign_holders=tuple(foreign_holders),
            process_s=round(time.monotonic() - start, 3),
        )

    selected, dropped = select(repo_root, Path(row_path), touched)
    for d in dropped:
        print(f"targeted-test-run: dropped directory from touched: {d}", file=sys.stderr)
    if not selected:
        return record(OUTCOME_NOTHING_TO_RUN)

    plans: list[tuple[str, list[str]]] = []
    for group in _group_by_language(selected):
        head = head_for(repo_root, group)
        if head is None:
            return record(OUTCOME_NO_RUNNER, selected=selected)
        plans.append((head[0], [*head[1], *group]))

    for _, argv in plans:
        if not admit(argv, repo_root):
            return record(
                OUTCOME_REFUSED,
                runner=plans[0][0],
                argv=argv,
                selected=selected,
            )

    runner = "+".join(label for label, _ in plans)
    flat_argv = [tok for i, (_, a) in enumerate(plans) for tok in ([";"] if i else []) + a]
    cmd = shlex.join(flat_argv)
    with tier_t_slots.held_slot(tier_t_slots.owner_id(), cmd, SLOT_WAIT_S) as slot:
        if slot is None:
            print("targeted-test-run: no Tier-T slot free within the wait", file=sys.stderr)
            return record(OUTCOME_NO_RUNNER, runner=runner, argv=flat_argv, selected=selected)
        outcome, worst, failing, holders = _judge(repo_root, touched, plans, timeout, committed_head)
    return record(
        outcome,
        runner=runner,
        argv=flat_argv,
        selected=selected,
        exit_code=worst,
        failing=failing,
        foreign_holders=holders,
    )


def _judge_committed(repo_root, plans, timeout):
    """Committed-HEAD verdict: no uncommitted content of any path, the row's own files included, is judged.

    A clean tree is judged in place. A dirty one is judged in a HEAD tree with no overlay; when that
    tree cannot be built the verdict is `no-runner`, never the live tree.
    """
    try:
        status = run_git(["status", "--porcelain", "--untracked-files=all"], cwd=str(repo_root))
        dirty = status.returncode != 0 or bool(status.stdout.strip())
    except Exception:
        dirty = True
    if not dirty:
        worst, failing = _run_plans(plans, repo_root, timeout)
        return (OUTCOME_PASSED if worst == 0 else OUTCOME_FAILED), worst, failing, ()
    try:
        tree = head_tree(repo_root, ())
    except Exception as exc:
        print(f"targeted-test-run: HEAD tree unavailable, refusing to judge the live tree: {exc}", file=sys.stderr)
        return OUTCOME_NO_RUNNER, None, [], ()
    try:
        worst, failing = _run_plans(plans, tree, timeout)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    return (OUTCOME_PASSED if worst == 0 else OUTCOME_FAILED), worst, failing, ()


def _judge(repo_root, touched, plans, timeout, committed_head=False):
    """(outcome, exit code, failing ids, foreign holders): HEAD plus the row's own edits when foreign dirt exists.

    No foreign dirty path (or no HEAD tree obtainable): the live tree is judged as before. A pass at
    HEAD-plus-own is `passed`; a fail there that passes live is `passed-on-foreign-uncommitted`.
    """
    if committed_head:
        return _judge_committed(repo_root, plans, timeout)
    foreign: list[str] = []
    try:
        foreign = foreign_dirty(repo_root, touched)
    except Exception:
        foreign = []
    if not foreign:
        worst, failing = _run_plans(plans, repo_root, timeout)
        return (OUTCOME_PASSED if worst == 0 else OUTCOME_FAILED), worst, failing, ()
    try:
        tree = head_tree(repo_root, touched)
    except Exception as exc:
        print(f"targeted-test-run: HEAD tree unavailable, judging the live tree: {exc}", file=sys.stderr)
        worst, failing = _run_plans(plans, repo_root, timeout)
        return (OUTCOME_PASSED if worst == 0 else OUTCOME_FAILED), worst, failing, ()
    try:
        worst, failing = _run_plans(plans, tree, timeout)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    if worst == 0:
        return OUTCOME_PASSED, 0, failing, ()
    live_worst, _ = _run_plans(plans, repo_root, timeout)
    if live_worst != 0:
        return OUTCOME_FAILED, worst, failing, ()
    from coordinator_core.session import core

    holders = holders_of(repo_root, foreign, core.resolve_session_id(str(repo_root)))
    return (
        OUTCOME_FOREIGN_UNCOMMITTED,
        worst,
        failing,
        tuple((p, tuple(h)) for p, h in holders.items()),
    )


def _run_plans(plans, cwd: Path, timeout: int) -> tuple[int, list[str]]:
    worst = 0
    failing: list[str] = []
    for _, argv in plans:
        code, output = _spawn(argv, cwd, timeout)
        sys.stdout.write(output[-4000:])
        if code != 0:
            worst = worst or code
            failing += _failing_ids(output)
    return worst, list(dict.fromkeys(failing))


def write_record(rec: RunnerRecord, path: Path) -> None:
    """Write `rec` as JSON at `path`, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec.to_json(), indent=2) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI body: exit code per outcome (`contract.EXIT_CODES`), 2 for usage errors."""
    import argparse

    ap = argparse.ArgumentParser(prog="targeted-test-run", description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--row", required=True)
    ap.add_argument("--touched", nargs="+", action="extend", default=[])
    ap.add_argument("--record", required=True)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--committed-head", action="store_true")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    repo_root = Path(args.repo)
    row_path = Path(args.row)
    if not row_path.is_absolute():
        row_path = repo_root / row_path
    record_path = Path(args.record)
    if not record_path.is_absolute():
        record_path = repo_root / record_path
    if not row_path.is_file():
        ap.error(f"row not found: {row_path}")

    rec = run(repo_root, row_path, args.touched, timeout=args.timeout, committed_head=args.committed_head)
    write_record(rec, record_path)
    print(f"targeted-test-run: {rec.outcome} ({len(rec.selected)} file(s)) -> {record_path}")
    return EXIT_CODES[rec.outcome]
