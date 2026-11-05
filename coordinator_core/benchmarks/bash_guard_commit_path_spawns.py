"""Falsifier for the PreToolUse Bash guard path of a commit-shaped command: git spawns, per-leg process time, warm waits.

    python -I coordinator_core/benchmarks/bash_guard_commit_path_spawns.py [--clone PATH]

At default arguments it builds a fresh shared clone of this repo (directory minted through
`isolated_clone.mkdtemp_for_clone`; full index, only docs/research checked out), feeds `hooks.preuse_bash_dispatch`
the R1 payload -- `git status && python <json read> && git add docs/research/x.md &&
git commit -m x -- docs/research/x.md`, cwd the clone -- and prints one JSON object:

    git_spawns        git child processes the guard path created (Popen constructions)
    git_argv          argv of each, with the leg it was created in
    other_spawns      non-git children, same shape
    legs              [{name, process_ms, git_spawns, warm_waits}] for every bracketed guard leg
    parent_ms         this process's CPU time from the first guard import through the verdict
    children_ms       CPU time of every descendant spawned inside the bracket
    total_process_ms  parent_ms + children_ms
    warm_waits        legs that opened a socket connection (a warm-engine call); [] when none
    verdict           the envelope's permissionDecision, or None
    rewritten         whether the chain's first-wins envelope carried an updatedInput (a rewrite
                      ends the chain: legs after it never run)

Process time, never wall clock (DR-344, DR-368). Interpreter boot is outside the bracket;
engine imports are inside it. Per-leg `process_ms` is the leg's own thread CPU
(`leg_attribution`), so a leg's child-process time shows only in `children_ms`.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

R1_COMMAND = (
    'git status && python -c "import json; print(json.load(open(' + "'docs/research/x.json'" + ')))"'
    " && git add docs/research/x.md && git commit -m x -- docs/research/x.md"
)
_FIXTURE_FILES = {
    "docs/research/x.md": "# x\n",
    "docs/research/x.json": "{}\n",
}
_NO_LEG = "(outside-legs)"


class _Recorder:
    """Per-leg spawn and socket-connect attribution for one measured call."""

    def __init__(self) -> None:
        self.stack: List[str] = []
        self.git: List[Dict[str, Any]] = []
        self.other: List[Dict[str, Any]] = []
        self.connects: List[str] = []

    @property
    def current(self) -> str:
        return self.stack[-1] if self.stack else _NO_LEG

    def record_spawn(self, args: Any) -> None:
        argv = [str(a) for a in args] if isinstance(args, (list, tuple)) else [str(args)]
        head = os.path.basename(argv[0]).lower() if argv else ""
        entry = {"leg": self.current, "argv": argv}
        (self.git if head in ("git", "git.exe") else self.other).append(entry)

    def record_connect(self) -> None:
        self.connects.append(self.current)


@contextlib.contextmanager
def _hooks(rec: _Recorder) -> Iterator[None]:
    """Route Popen construction and socket connects through `rec`; restore both on exit."""
    popen_init = subprocess.Popen.__init__
    sock_connect = socket.socket.connect

    def _popen_init(self, args, *a, **k):
        rec.record_spawn(args)
        return popen_init(self, args, *a, **k)

    def _connect(self, address):
        rec.record_connect()
        return sock_connect(self, address)

    subprocess.Popen.__init__ = _popen_init  # type: ignore[method-assign]
    socket.socket.connect = _connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        subprocess.Popen.__init__ = popen_init  # type: ignore[method-assign]
        socket.socket.connect = sock_connect  # type: ignore[method-assign]


@contextlib.contextmanager
def _tracked_legs(rec: _Recorder, bindings: List[Any]) -> Iterator[None]:
    """Wrap each (module, attribute) `leg` binding so `rec.stack` names the active leg."""
    originals = [(m, attr, getattr(m, attr)) for m, attr in bindings]

    def _wrap(original):
        @contextlib.contextmanager
        def _tracked(name: str):
            rec.stack.append(name)
            try:
                with original(name):
                    yield
            finally:
                rec.stack.pop()

        return _tracked

    try:
        for module, attr, original in originals:
            setattr(module, attr, _wrap(original))
        yield
    finally:
        for module, attr, original in originals:
            setattr(module, attr, original)


def _payload(repo: Path, command: str) -> Dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "bash-guard-commit-path-spawns",
        "cwd": str(repo),
    }


def _write_fixture(repo: Path) -> None:
    for rel, text in _FIXTURE_FILES.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _verdict(out: Any) -> Optional[str]:
    specific = out.get("hookSpecificOutput") if isinstance(out, dict) else None
    return specific.get("permissionDecision") if isinstance(specific, dict) else None


def _rewritten(out: Any) -> bool:
    specific = out.get("hookSpecificOutput") if isinstance(out, dict) else None
    return isinstance(specific, dict) and "updatedInput" in specific


def measure(repo: Path, command: str = R1_COMMAND) -> Dict[str, Any]:
    """Run the hook entry once against `repo` and return the report dict.

    Imports of the guard chain happen inside the bracket, so they are counted.
    """
    from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter

    rec = _Recorder()
    meter = _ChildMeter()
    with _hooks(rec):
        meter.start()
        t0 = time.process_time()
        out: Any = None
        legs: List[Dict[str, Any]] = []
        try:
            from coordinator_core.bash_guards import dispatch, leg_attribution
            from coordinator_core.hooks import preuse_bash_dispatch

            with _tracked_legs(rec, [(dispatch, "_leg"), (preuse_bash_dispatch, "leg")]):
                token = leg_attribution.begin()
                try:
                    out = preuse_bash_dispatch._run_chain(_payload(repo, command))
                finally:
                    legs = leg_attribution.summary(top=10 ** 6, floor_ms=0.0) or []
                    leg_attribution.end(token)
        finally:
            parent_ms = round((time.process_time() - t0) * 1000.0, 3)
            children_ms = meter.stop_ms()

    git_by_leg: Dict[str, int] = {}
    for entry in rec.git:
        git_by_leg[entry["leg"]] = git_by_leg.get(entry["leg"], 0) + 1
    waits = sorted(set(rec.connects))
    rows = [
        {
            "name": row["name"],
            "process_ms": row["ms"],
            "git_spawns": git_by_leg.pop(row["name"], 0),
            "warm_waits": row["name"] in waits,
        }
        for row in legs
    ]
    rows += [
        {"name": name, "process_ms": 0.0, "git_spawns": n, "warm_waits": name in waits}
        for name, n in sorted(git_by_leg.items())
    ]
    return {
        "command": command,
        "verdict": _verdict(out),
        "rewritten": _rewritten(out),
        "git_spawns": len(rec.git),
        "git_argv": rec.git,
        "other_spawns": rec.other,
        "legs": rows,
        "parent_ms": parent_ms,
        "children_ms": children_ms,
        "total_process_ms": round(parent_ms + children_ms, 3),
        "warm_waits": waits,
    }


def build_report(clone: Optional[Path] = None, command: str = R1_COMMAND) -> Dict[str, Any]:
    """Measure against `clone`, or against a fresh clone of this repo when None."""
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    if clone is not None:
        _write_fixture(clone)
        return measure(clone, command)
    with _fresh_sparse_clone(_REPO_ROOT) as repo:
        _write_fixture(repo)
        return measure(repo, command)


@contextlib.contextmanager
def _fresh_sparse_clone(source_root: Path) -> Iterator[Path]:
    """Yield a throwaway shared clone of `source_root` HEAD with only docs/research checked out.

    The index stays full-size (what the guards read); the worktree stays small, so the clone
    does not pay a full-tree checkout. Torn down on exit.
    """
    from coordinator_core.benchmarks import isolated_clone as ic
    from coordinator_core.git.run import run_git

    tmp = ic.mkdtemp_for_clone(source_root, prefix="bgcps-")
    try:
        dest = tmp / "c"
        steps = (
            (tmp, ["clone", "-q", "--shared", "--no-checkout", str(source_root), str(dest)]),
            (dest, ["sparse-checkout", "set", "--cone", "docs/research"]),
            (dest, ["checkout", "-q", "HEAD"]),
        )
        for cwd, args in steps:
            res = run_git(["-c", "core.fsmonitor=false", "-c", "core.longpaths=true", *args],
                          cwd=str(cwd), remote=True)
            if not res.ok:
                raise RuntimeError(f"git {args[0]} failed rc={res.returncode}: {res.stderr.strip()}")
        ic._silence_clone_git_daemons(dest)
        yield dest
    finally:
        reaped = ic.reap_processes_under(tmp)
        ic.rmtree_or_raise(tmp, label="bash_guard_commit_path_spawns", reaped=reaped)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clone", type=Path, default=None, help="measure in this repo instead of a fresh clone")
    ap.add_argument("--command", default=R1_COMMAND, help="Bash command to evaluate (default: the R1 shape)")
    args = ap.parse_args(argv)
    print(json.dumps(build_report(args.clone, args.command), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
