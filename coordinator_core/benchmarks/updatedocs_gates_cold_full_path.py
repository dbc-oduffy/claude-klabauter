"""Fixture and cold-engine driver for `updatedocs.gates` (and, via `--op`, `updatedocs.sweeps`).

The fixture is a tmp git repo holding one resolved legacy queue entry (state/bug-backlog.md) and
one stale subagent-share sidecar (dead session directory, mtime past the reap age floor). The
op runs once in a fresh interpreter with default params (`repo_root` and `settings_home` only),
so the figure is a cold first call; a parent snapshot of the fixture before and after counts
the files the op wrote. A child-side wrapper times each gate's own process time and spawns
(ops that expose a `_GATES` table).

Prints one JSON object: op, op_class, envelope_code, process_ms (child user+sys CPU, whole
interpreter), wall_ms, spawns (audit-hook count), files_written, files_removed, per_gate.

    python -m coordinator_core.benchmarks.updatedocs_gates_cold_full_path <dir> [--op NAME] [--plans N] [--handoffs N]

`--op refresh-queries` drives `refresh_queries.main([])` in-process from the fixture root (the CLI
has no registered op). The CLI default fixture is a large repo (DEFAULT_PLANS plans under
docs/plans, DEFAULT_HANDOFFS handoffs under archive/handoffs, and as many tasks/ notes as handoffs,
so the fresh-scaffold probe's tasks/ leg runs); `ensure_fixture` itself defaults to the small shape.
`ls_files_spawns` counts child spawns whose argv holds `ls-files`.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from coordinator_core.git.run import run_git

DEFAULT_OP = "updatedocs.gates"
REFRESH_OP = "refresh-queries"
DEFAULT_PLANS = 1000
DEFAULT_HANDOFFS = 300
SIDECAR_AGE_DAYS = 60  # above sidecar_reap.DEFAULT_AGE_FLOOR_DAYS (14)
DEAD_SESSION = "00000000-0000-4000-8000-000000000000"
QUEUE_REL = "state/bug-backlog.md"
SIDECAR_REL = f".coordinator-local/subagent-share/{DEAD_SESSION}/stale-sidecar.md"
_QUEUE_BODY = (
    "# Bug backlog\n\n"
    "## Open\n\n"
    "- 2026-01-01 | still open | high\n"
    "  recurring: 0\n"
    "  resolution: pending\n\n"
    "## Resolved\n\n"
    "- 2025-01-01 | fixed long ago | low\n"
    "  recurring: 0\n"
    "  resolution: resolved in abc123\n"
)
_SIDECAR_BODY = "---\nstatus: complete\n---\n\nstale\n"
_CHILD_FLAG = "--child"


def _git(repo: Path, *args: str) -> None:
    result = run_git(list(args), cwd=str(repo))
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={result.returncode}: {result.stderr.strip()}")


def _write_notes(repo: Path, rel_dir: str, stem: str, count: int, per_dir: int = 100) -> None:
    """`count` small tracked-to-be .md notes, spread `per_dir` to a subdirectory."""
    for i in range(count):
        d = repo / rel_dir / f"{i // per_dir:03d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}-{i:04d}.md").write_text(f"# {stem} {i}\n\nbody\n", encoding="utf-8")


def ensure_fixture(parent: Path, plans: int = 0, handoffs: int = 0) -> Path:
    """Build a fresh fixture repo under `parent` (replacing any earlier one) and return it.

    `plans` and `handoffs` add that many committed .md files under docs/plans and archive/handoffs,
    plus `handoffs` tasks/ notes and a root DIRECTORY.md with one count claim (so the
    directory-index staleness gate runs its check); 0 keeps the small shape.
    """
    import shutil

    repo = parent.resolve() / "fixture"
    if repo.exists():
        shutil.rmtree(repo)
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "bug-backlog.md").write_text(_QUEUE_BODY, encoding="utf-8")
    sidecar = repo / SIDECAR_REL
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(_SIDECAR_BODY, encoding="utf-8")
    old = time.time() - SIDECAR_AGE_DAYS * 86400
    os.utime(sidecar, (old, old))
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    tracked = ["README.md", "state"]
    if plans or handoffs:
        _write_notes(repo, "docs/plans", "plan", plans)
        _write_notes(repo, "archive/handoffs", "handoff", handoffs)
        _write_notes(repo, "tasks", "task", handoffs)
        (repo / "DIRECTORY.md").write_text(
            f"# Directory\n\nLast refreshed: 2026-01-01\n\n- docs/plans: {plans} `*.md` files\n",
            encoding="utf-8",
        )
        tracked += ["docs", "archive", "tasks", "DIRECTORY.md"]
    _git(repo, "add", *tracked)
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def snapshot(repo: Path) -> dict[str, str]:
    """rel path -> content hash for every file outside .git."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = Path(dirpath) / name
            out[p.relative_to(repo).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> tuple[int, int]:
    """(files written: added or changed, files removed)."""
    written = sum(1 for k, v in after.items() if before.get(k) != v)
    removed = sum(1 for k in before if k not in after)
    return written, removed


def _op_module(op: str):
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    return importlib.import_module(OP_MODULE_MAP[op])


def _watch_ls_files() -> list[int]:
    """Install an audit hook counting child spawns whose argv holds `ls-files`; return the cell."""
    cell = [0]

    def hook(event: str, args: tuple) -> None:
        if event == "subprocess.Popen":
            try:
                argv = args[1]
                if "ls-files" in (argv if isinstance(argv, (list, tuple)) else [argv]):
                    cell[0] += 1
            except (IndexError, TypeError):
                pass

    sys.addaudithook(hook)
    return cell


def _child_refresh(repo: Path) -> int:
    """Run `refresh_queries.main([])` (default args) from the fixture root; print metrics."""
    import contextlib
    import io

    from coordinator_core.telemetry.spawn_counter import spawn_count
    from coordinator_core.text import refresh_queries

    ls_files = _watch_ls_files()
    os.chdir(repo)
    out, err = io.StringIO(), io.StringIO()
    s0 = spawn_count()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = refresh_queries.main([])
    print(json.dumps({
        "exit": code,
        "envelope_code": None,
        "has_result": code == 0,
        "spawns": spawn_count() - s0,
        "ls_files_spawns": ls_files[0],
        "per_gate": {},
        "stderr_tail": err.getvalue()[-300:],
    }))
    return 0


def _child(op: str, repo: Path, settings_home: Path) -> int:
    """Runs in the fresh interpreter: dispatch `op` once, print a metrics JSON line."""
    if op == REFRESH_OP:
        return _child_refresh(repo)
    from coordinator_core.telemetry.spawn_counter import spawn_count

    ls_files = _watch_ls_files()
    per_gate: dict[str, dict] = {}
    module = _op_module(op)
    table = getattr(module, "_GATES", None)
    if isinstance(table, dict):
        for gate_id, fn in list(table.items()):
            def timed(*a, _fn=fn, _id=gate_id, **k):
                t0, s0 = time.process_time(), spawn_count()
                try:
                    return _fn(*a, **k)
                finally:
                    per_gate[_id] = {
                        "process_ms": round((time.process_time() - t0) * 1000, 1),
                        "spawns": spawn_count() - s0,
                    }

            table[gate_id] = timed

    from coordinator_core.invoke.__main__ import _dispatch_argv

    params = json.dumps({"repo_root": str(repo), "settings_home": str(settings_home)})
    s0 = spawn_count()
    stdout, stderr, code = _dispatch_argv([op, params, "--allow-unstamped-dispatch"], str(repo))
    spawns = spawn_count() - s0
    try:
        envelope = json.loads(stdout)
    except ValueError:
        envelope = {}
    error = envelope.get("error") if isinstance(envelope, dict) else None
    print(json.dumps({
        "exit": code,
        "envelope_code": error.get("code") if isinstance(error, dict) else None,
        "has_result": isinstance(envelope, dict) and "result" in envelope,
        "spawns": spawns,
        "ls_files_spawns": ls_files[0],
        "per_gate": per_gate,
        "stderr_tail": (stderr or "")[-300:],
    }))
    return 0


def _children_cpu_ms() -> float:
    try:
        import resource

        r = resource.getrusage(resource.RUSAGE_CHILDREN)
        return (r.ru_utime + r.ru_stime) * 1000
    except ImportError:
        return float("nan")


def _op_class(op: str) -> str:
    from coordinator_core.authz.classification import classify

    return classify(op).name


def measure(op: str, parent: Path, plans: int = 0, handoffs: int = 0) -> dict:
    """One cold run of `op` against a fresh fixture; the report dict."""
    repo = ensure_fixture(parent, plans, handoffs)
    settings_home = parent.resolve() / "settings-home"
    (settings_home / "bin").mkdir(parents=True, exist_ok=True)
    before = snapshot(repo)
    env = dict(os.environ)
    cpu0, t0 = _children_cpu_ms(), time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", __spec__.name if __spec__ else "coordinator_core.benchmarks.updatedocs_gates_cold_full_path",
         _CHILD_FLAG, op, str(repo), str(settings_home)],
        capture_output=True, text=True, timeout=120, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    wall_ms = (time.perf_counter() - t0) * 1000
    process_ms = _children_cpu_ms() - cpu0
    child = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else {}
    written, removed = diff_snapshots(before, snapshot(repo))
    return {
        "op": op,
        "op_class": "cli" if op == REFRESH_OP else _op_class(op),
        "plans": plans,
        "handoffs": handoffs,
        "envelope_code": child.get("envelope_code"),
        "has_result": child.get("has_result", False),
        "process_ms": round(process_ms, 1),
        "wall_ms": round(wall_ms, 1),
        "spawns": child.get("spawns"),
        "ls_files_spawns": child.get("ls_files_spawns"),
        "files_written": written,
        "files_removed": removed,
        "per_gate": child.get("per_gate", {}),
        "child_rc": proc.returncode,
        "child_stderr_tail": proc.stderr[-300:],
    }


def main(argv: list[str]) -> int:
    if argv and argv[0] == _CHILD_FLAG:
        return _child(argv[1], Path(argv[2]), Path(argv[3]))
    usage = "usage: updatedocs_gates_cold_full_path <dir> [--op NAME] [--plans N] [--handoffs N]"
    op = DEFAULT_OP
    counts = {"--plans": DEFAULT_PLANS, "--handoffs": DEFAULT_HANDOFFS}
    args = list(argv)
    for flag in ("--op", "--plans", "--handoffs"):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(usage, file=sys.stderr)
                return 2
            value = args[i + 1]
            del args[i:i + 2]
            if flag == "--op":
                op = value
            elif value.isdigit():
                counts[flag] = int(value)
            else:
                print(usage, file=sys.stderr)
                return 2
    if len(args) != 1:
        print(usage, file=sys.stderr)
        return 2
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    report = measure(op, Path(args[0]), counts["--plans"], counts["--handoffs"])
    print(json.dumps(report, indent=2))
    return 0 if report["has_result"] or report["envelope_code"] is not None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
