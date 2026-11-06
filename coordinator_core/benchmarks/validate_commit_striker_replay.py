"""Falsifier: the `validate-commit` Bash-guard leg on commit-shaped commands at the shapes the machine-a measured.

    python -I coordinator_core/benchmarks/validate_commit_machine_a_replay.py [--row ID ...] [--no-subphases]

Reads `baselines/validate-commit-machine-a-rows.json`; per pinned row it mints a shared clone of this repo
(`bash_guard_commit_path_spawns._fresh_sparse_clone`), shapes the clone's index and worktree to the row's
`shape`, feeds the row's commit-shaped command to `hooks.preuse_bash_dispatch` and prints one JSON object:

    rows[]  {id, validate_commit_reached, validate_commit_process_ms, validate_commit_cold_process_ms,
             validate_commit_spawns, total_process_ms, verdict, sub_phases}
    widest_row / narrowest_row / spawns_flat   the AC1 spawn-count comparison
    all_under_bar                              every row's validate_commit_process_ms < BAR_MS

`validate_commit_process_ms` is the leg's own thread CPU on a WARM second call (the engine is warm);
`..._cold_` is the first call, imports included. Process time and spawn count, never wall clock
(DR-344, DR-356). `sub_phases` comes from a third, instrumented call (inflated by its own brackets; read it as
attribution, never as a figure): `checks` is per-`# Check N` region inclusive CPU of `check_validate_commit`'s own
frame, `functions` the per-function inclusive/exclusive CPU of the guard modules. Brackets live here, in this
file's monkeypatch layer, never in the guard source.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

ROWS_FILE = Path(__file__).resolve().parent / "baselines" / "validate-commit-machine-a-rows.json"
BAR_MS = 500.0
_LEG = "validate-commit"
_SCRATCH_DIR = "docs/research/_machine_a"
_GUARD_MODULES = (
    "coordinator_core.bash_guards._dc_commit",
    "coordinator_core.bash_guards.commit_tripwires",
    "coordinator_core.bash_guards._git_tree_facts",
    "coordinator_core.bash_guards._dc_git_probe",
    "coordinator_core.bash_guards._implausible_index",
)
_GIT_PACKAGE = "coordinator_core.git."


def load_rows(path: Path = ROWS_FILE) -> Dict[str, Any]:
    """The pinned rows document (`rows[]` carry `id`, `command_form`, `shape`)."""
    return json.loads(path.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    from coordinator_core.git.run import run_git

    res = run_git(["-c", "core.fsmonitor=false", "-c", "core.longpaths=true", *args], cwd=str(repo), remote=True)
    if not res.ok:
        raise RuntimeError(f"git {args[0]} failed rc={res.returncode}: {res.stderr.strip()[:300]}")
    return res.stdout


def _tracked_research(repo: Path) -> List[str]:
    return sorted(p for p in _git(repo, "ls-files", "docs/research").splitlines() if p.endswith(".md"))


def _batched(repo: Path, verb: List[str], paths: List[str], chunk: int = 400) -> None:
    for i in range(0, len(paths), chunk):
        _git(repo, *verb, "--", *paths[i : i + chunk])


def inflate_index(repo: Path, total: int) -> int:
    """Commit `bulk/...` index-only entries so HEAD tracks at least `total` paths; return the count tracked.

    Entries share one blob, sit 100 to a directory under two levels of grouping, and are committed
    so they are never part of the staged diff. Nothing is checked out (the worktree stays small).
    """
    have = len(_git(repo, "ls-files", "-z").split("\0")) - 1
    if total <= have:
        return have
    from coordinator_core.git.run import run_git

    hashed = run_git(["hash-object", "-w", "--stdin"], cwd=str(repo), input=b"bulk\n")
    blob = hashed.stdout.strip()
    extra = total - have
    lines = "".join(
        f"100644 {blob} 0\tbulk/g{i // 10000:03d}/d{i // 100:05d}/f{i:07d}.txt\n" for i in range(extra)
    ).encode("utf-8")
    res = run_git(["update-index", "--index-info"], cwd=str(repo), input=lines, remote=True, timeout=120)
    if not res.ok:
        raise RuntimeError(f"update-index failed: {res.stderr.strip()[:300]}")
    _git(repo, "-c", "user.name=machine-a", "-c", "user.email=machine-a@example.invalid",
         "commit", "-q", "--no-verify", "-m", "machine-a-replay bulk index")
    return total


def shape_clone(repo: Path, shape: Dict[str, Any]) -> List[str]:
    """Stage/dirty `repo` to `shape`; return the staged paths the command's pathspec may name.

    Keys (all optional, default 0): tracked_files (floor on HEAD's tracked-path count; 0 keeps the
    clone's own), staged_modified, staged_added, staged_deleted, staged_renamed, dirty_modified
    (tracked, unstaged), untracked. Every touched tracked file is under docs/research (the clone's
    checked-out cone); added/untracked files live under `docs/research/_machine_a`.
    """
    if int(shape.get("tracked_files", 0)):
        inflate_index(repo, int(shape["tracked_files"]))
    tracked = _tracked_research(repo)
    n_mod = int(shape.get("staged_modified", 0))
    n_del = int(shape.get("staged_deleted", 0))
    n_ren = int(shape.get("staged_renamed", 0))
    n_dirty = int(shape.get("dirty_modified", 0))
    need = n_mod + n_del + n_ren + n_dirty
    if need > len(tracked):
        raise RuntimeError(f"shape needs {need} tracked docs/research files, clone has {len(tracked)}")
    it = iter(tracked)
    mod = [next(it) for _ in range(n_mod)]
    dele = [next(it) for _ in range(n_del)]
    ren = [next(it) for _ in range(n_ren)]
    dirty = [next(it) for _ in range(n_dirty)]
    for rel in mod + dirty:
        with (repo / rel).open("a", encoding="utf-8") as fh:
            fh.write("\n<!-- machine-a-replay edit -->\n")
    scratch = repo / _SCRATCH_DIR
    scratch.mkdir(parents=True, exist_ok=True)
    added: List[str] = []
    for i in range(int(shape.get("staged_added", 0))):
        rel = f"{_SCRATCH_DIR}/added-{i:05d}.md"
        (repo / rel).write_text(f"# added {i}\n", encoding="utf-8")
        added.append(rel)
    for i in range(int(shape.get("untracked", 0))):
        (scratch / f"untracked-{i:05d}.md").write_text(f"# untracked {i}\n", encoding="utf-8")
    renamed_to: List[str] = []
    for i, rel in enumerate(ren):
        dest = f"{_SCRATCH_DIR}/renamed-{i:05d}.md"
        _git(repo, "mv", rel, dest)
        renamed_to.append(dest)
    _batched(repo, ["add"], mod + added)
    if dele:
        _batched(repo, ["rm", "-q"], dele)
    return mod + added + renamed_to + dele


def command_for(form: str, staged: List[str]) -> str:
    """The commit-shaped Bash command for `form` (`pathspec`: `git commit -m x -- <3 staged paths>`; `bare`)."""
    if form == "pathspec":
        named = " ".join(p for p in staged[:3])
        return f"git commit -m x -- {named}"
    if form == "bare":
        return "git commit -m x"
    raise ValueError(f"unknown command_form {form!r}")


def _region_map(source_path: Path, first: int, last: int) -> Dict[int, str]:
    """Line -> region label for `# Check N` comment banners between `first` and `last`."""
    pat = re.compile(r"^\s*#\s*(?:---\s*)?(Checks? \d+(?:-\d+)?)\b")
    label = "prelude"
    out: Dict[int, str] = {}
    for no, line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), start=1):
        if no < first or no > last:
            continue
        m = pat.match(line)
        if m:
            label = m.group(1)
        out[no] = label
    return out


class _SubPhases:
    """Per-function and per-Check process-time brackets for one instrumented call."""

    def __init__(self) -> None:
        self.stack: List[List[float]] = []
        self.funcs: Dict[str, List[float]] = {}
        self.checks: Dict[str, float] = {}

    def wrap(self, name: str, fn: Any) -> Any:
        def _wrapped(*a: Any, **k: Any) -> Any:
            frame = [0.0]
            self.stack.append(frame)
            t0 = time.process_time()
            try:
                return fn(*a, **k)
            finally:
                incl = time.process_time() - t0
                self.stack.pop()
                if self.stack:
                    self.stack[-1][0] += incl
                row = self.funcs.setdefault(name, [0.0, 0.0, 0.0])
                row[0] += 1
                row[1] += incl
                row[2] += incl - frame[0]

        _wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
        return _wrapped

    def report(self) -> Dict[str, Any]:
        funcs = [
            {"name": n, "calls": int(c), "incl_ms": round(i * 1000.0, 3), "excl_ms": round(e * 1000.0, 3)}
            for n, (c, i, e) in self.funcs.items()
        ]
        funcs.sort(key=lambda r: -r["incl_ms"])
        checks = {k: round(v * 1000.0, 3) for k, v in sorted(self.checks.items(), key=lambda kv: -kv[1])}
        return {"checks": checks, "functions": [f for f in funcs if f["incl_ms"] >= 0.5]}


@contextlib.contextmanager
def _instrumented(sp: _SubPhases) -> Iterator[None]:
    """Wrap guard-module functions and line-trace `check_validate_commit`'s own frame; restore on exit."""
    import importlib
    import types

    patched: List[Any] = []
    names = list(_GUARD_MODULES)
    names += [n for n in sorted(sys.modules) if n.startswith(_GIT_PACKAGE)]
    for modname in names:
        mod = importlib.import_module(modname)
        for attr, val in list(vars(mod).items()):
            if isinstance(val, types.FunctionType) and val.__module__.startswith("coordinator_core"):
                patched.append((mod, attr, val))
                setattr(mod, attr, sp.wrap(f"{val.__module__.rsplit('.', 1)[-1]}.{val.__name__}", val))

    dc = importlib.import_module("coordinator_core.bash_guards._dc_commit")
    target = getattr(dc.check_validate_commit, "__wrapped__", dc.check_validate_commit).__code__
    first = target.co_firstlineno
    last = max(ln for _, _, ln in target.co_lines() if ln)
    regions = _region_map(Path(dc.__file__), first, last)
    state = {"line": first, "t": 0.0}

    def _charge(now: float) -> None:
        label = regions.get(state["line"], "prelude")
        sp.checks[label] = sp.checks.get(label, 0.0) + (now - state["t"])

    def _local(frame: Any, event: str, arg: Any) -> Any:
        now = time.process_time()
        if event == "line":
            _charge(now)
            state["line"] = frame.f_lineno
            state["t"] = time.process_time()
        elif event == "return":
            _charge(now)
        return _local

    def _global(frame: Any, event: str, arg: Any) -> Any:
        if event == "call" and frame.f_code is target:
            state["line"] = first
            state["t"] = time.process_time()
            return _local
        return None

    old_trace = sys.gettrace()
    sys.settrace(_global)
    try:
        yield
    finally:
        sys.settrace(old_trace)
        for mod, attr, val in patched:
            setattr(mod, attr, val)


def _leg(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return next((l for l in result["legs"] if l["name"] == _LEG), None)


def measure_row(row: Dict[str, Any], subphases: bool = True) -> Dict[str, Any]:
    """Shape a fresh clone to `row`, measure the commit hook cold, warm and (optionally) instrumented."""
    from coordinator_core.benchmarks import bash_guard_commit_path_spawns as bg

    with bg._fresh_sparse_clone(_REPO_ROOT) as repo:
        staged = shape_clone(repo, row["shape"])
        command = command_for(row["command_form"], staged)
        cold = bg.measure(repo, command)
        warm = bg.measure(repo, command)
        leg_c, leg_w = _leg(cold), _leg(warm)
        out: Dict[str, Any] = {
            "id": row["id"],
            "command": command if len(command) < 200 else command[:200] + "...",
            "validate_commit_reached": leg_w is not None,
            "validate_commit_process_ms": leg_w["process_ms"] if leg_w else None,
            "validate_commit_cold_process_ms": leg_c["process_ms"] if leg_c else None,
            "validate_commit_spawns": leg_w["git_spawns"] if leg_w else None,
            "total_process_ms": warm["total_process_ms"],
            "children_ms": warm["children_ms"],
            "verdict": warm["verdict"],
        }
        if subphases:
            sp = _SubPhases()
            with _instrumented(sp):
                bg.measure(repo, command)
            out["sub_phases"] = sp.report()
        return out


def build_report(row_ids: Optional[List[str]] = None, subphases: bool = True) -> Dict[str, Any]:
    """Measure every pinned row (or `row_ids`) and add the AC1 comparison keys."""
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    doc = load_rows()
    rows = [r for r in doc["rows"] if not row_ids or r["id"] in row_ids]
    measured = [measure_row(r, subphases) for r in rows]
    ranked = sorted(
        (m for m in measured if m["validate_commit_spawns"] is not None),
        key=lambda m: _width(next(r for r in rows if r["id"] == m["id"])),
    )
    spawns_flat = bool(ranked) and ranked[0]["validate_commit_spawns"] == ranked[-1]["validate_commit_spawns"]
    return {
        "bar_ms": BAR_MS,
        "rows": measured,
        "narrowest_row": ranked[0]["id"] if ranked else None,
        "widest_row": ranked[-1]["id"] if ranked else None,
        "spawns_flat": spawns_flat,
        "spawns_by_row": {m["id"]: m["validate_commit_spawns"] for m in measured},
        "spawns_all_equal": len({m["validate_commit_spawns"] for m in measured}) == 1,
        "all_reached": all(m["validate_commit_reached"] for m in measured),
        "all_under_bar": all(
            m["validate_commit_process_ms"] is not None and m["validate_commit_process_ms"] < BAR_MS for m in measured
        ),
    }


def _width(row: Dict[str, Any]) -> tuple:
    s = row["shape"]
    staged = sum(int(s.get(k, 0)) for k in ("staged_modified", "staged_added", "staged_deleted", "staged_renamed"))
    return (staged, int(s.get("tracked_files", 0)))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--row", action="append", default=None, help="measure only this row id (repeatable)")
    ap.add_argument("--no-subphases", action="store_true", help="skip the instrumented third call")
    args = ap.parse_args(argv)
    print(json.dumps(build_report(args.row, not args.no_subphases), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
