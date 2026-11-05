"""Slow-disk falsifier for the engine-ask submit hook's default `{ask}` path.

Drives `submit_engine_ask.evaluate` (launch stubbed, the real `dispatch.emit` behind it) on a default
`{ask: "..."}` Workflow payload in a tmp git repo, and charges `--delay-s` virtual seconds to every
read, write, open and `os.scandir` that touches the repo root (`io.open`, which `Path.read_text` /
`write_text` / `open` route through, `os.scandir`, `locked_write.held_lock`). Nothing sleeps; the wait is
accumulated, so the figure is deterministic and independent of peer load.

Reports the answer kind, the run id it carries, `injected_wait_ms` and `process_ms` before the answer,
and the ordered charged sites (first-seen order, with counts) so the dominant synchronous I/O is named.
A directory walk (`scandir`) charged before the answer is bulk I/O.

    python coordinator_core/benchmarks/submit_slow_disk_profile.py [--delay-s S] [--json]
"""

from __future__ import annotations

import argparse
import builtins
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core import locked_write  # noqa: E402
from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402
from coordinator_core.temp_layout import managed_tempdir  # noqa: E402

DEFAULT_DELAY_S = 2.0
#: The hook answers on its own at this share of the caller's 12 s budget (`submit_engine_ask`).
EMIT_BUDGET_MS = 9600.0
_SESSION_ID = "22222222-3333-4444-5555-666666666666"
_ASK = "profile the default ask path under a slow disk"
_RUN_ID = re.compile(r"\b(?:ask|warp|plan)-[0-9A-Za-z-]*\d{8}T\d{6}-[0-9a-f]{6}\b")
_SKIP_FRAMES = ("submit_slow_disk_profile", "threading.py", "pathlib", "contextlib", "\\io.py", "/io.py", "locked_write")


class Charger:
    """Virtual slow disk: counts and charges `delay_s` per I/O under `root`, in first-seen order."""

    def __init__(self, root: Path, delay_s: float) -> None:
        self.root = os.path.normcase(os.path.realpath(root))
        self.delay_s = delay_s
        self.sites: dict = {}
        self._lock = threading.Lock()

    def _under(self, path) -> str | None:
        try:
            full = os.path.normcase(os.path.realpath(os.fspath(path)))
        except (TypeError, ValueError, OSError):
            return None
        if full == self.root or full.startswith(self.root + os.sep):
            return os.path.relpath(full, self.root).replace(os.sep, "/")
        return None

    def _caller(self) -> str:
        for frame in reversed(traceback.extract_stack(limit=30)):
            if not any(skip in frame.filename for skip in _SKIP_FRAMES):
                return f"{os.path.basename(frame.filename)}:{frame.name}"
        return "?"

    def charge(self, kind: str, path) -> None:
        rel = self._under(path)
        if rel is None:
            return
        key = (kind, self._caller(), _shape(rel))
        with self._lock:
            entry = self.sites.setdefault(key, {"count": 0, "example": rel})
            entry["count"] += 1

    @property
    def waited_ms(self) -> float:
        return self.delay_s * 1000.0 * sum(e["count"] for e in self.sites.values())

    def report(self) -> list:
        return [
            {"kind": k, "caller": c, "path": shape, "count": e["count"], "example": e["example"],
             "bulk": k == "scandir"}
            for (k, c, shape), e in self.sites.items()
        ]

    @contextmanager
    def active(self):
        real_open, real_io_open, real_scandir = builtins.open, io.open, os.scandir
        real_held = locked_write.held_lock
        me = self

        def opened(file, mode="r", *a, **k):
            if isinstance(file, (str, bytes, os.PathLike)):
                writing = isinstance(mode, str) and any(c in mode for c in "wax+")
                me.charge("write" if writing else "read", file)
            return real_open(file, mode, *a, **k)

        def scandir(path="."):
            me.charge("scandir", path)
            return real_scandir(path)

        @contextmanager
        def held(path, *a, **k):
            me.charge("lock", path)
            with real_held(path, *a, **k) as value:
                yield value

        builtins.open = io.open = opened
        os.scandir = scandir
        locked_write.held_lock = held
        try:
            yield self
        finally:
            builtins.open, io.open, os.scandir = real_open, real_io_open, real_scandir
            locked_write.held_lock = real_held


def _shape(rel: str) -> str:
    """The path with run-specific stems collapsed so one site counts once across runs."""
    rel = re.sub(r"\d{8}t\d{6}-[0-9a-f]{6}", "<ts>-<id>", rel, flags=re.IGNORECASE)
    return re.sub(r"(prompts/)[^/]+$", r"\1*", rel)


def build_repo(root: Path) -> None:
    """A minimal committed git tree: the ask path needs a repo, not a history."""
    def git(*args: str) -> None:
        res = run_git(["-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.example", *args])
        if not res.ok:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {res.stderr.strip()}")

    git("init", "-q", "-b", "main")
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="\n")
    git("add", "README.md")
    git("commit", "-q", "-m", "seed")


def default_payload(root: Path) -> dict:
    return {
        "hook_event_name": "PreToolUse", "tool_name": "Workflow", "session_id": _SESSION_ID,
        "cwd": str(root), "tool_use_id": "slow-disk-profile",
        "tool_input": {"name": "coordinator:engine-ask", "args": {"ask": _ASK}},
    }


def measure(delay_s: float = DEFAULT_DELAY_S) -> dict:
    """Drive the default-ask submission under the injected delay; return the report dict."""
    import pytest

    from coordinator_core.hooks import submit_engine_ask as hook

    mp = pytest.MonkeyPatch()
    scratch = Path(tempfile.mkdtemp(prefix="submit-slow-disk-", dir=str(managed_tempdir())))
    try:
        root = scratch / "repo"
        root.mkdir()
        build_repo(root)
        mp.setenv("COORDINATOR_SESSION_ID", _SESSION_ID)
        mp.delenv("DELIVERABLE_ID", raising=False)
        mp.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")  # the measured path is the full emit, not the degraded refusal
        mp.chdir(root)
        launched: list = []

        def stub_launch(*a, **k):
            launched.append((a, k))
            return root / "scratch" / "bench-launcher.workflow.mjs"

        charger = Charger(root, delay_s)
        with charger.active():
            t0 = time.process_time()
            out = hook.evaluate(default_payload(root), launch=stub_launch)
            process_ms = (time.process_time() - t0) * 1000.0
    finally:
        mp.undo()
        rmtree_or_raise(scratch, label="submit-slow-disk")
    hso = out.get("hookSpecificOutput") or {}
    reason = str(hso.get("permissionDecisionReason") or "")
    blob = json.dumps(out)
    found = _RUN_ID.search(blob)
    sites = charger.report()
    injected = charger.waited_ms
    return {
        "delay_s": delay_s,
        "answer_kind": hso.get("permissionDecision") or "none",
        "reason": reason[:240],
        "run_id": found.group(0) if found else "",
        "injected_wait_ms": round(injected, 1),
        "process_ms": round(process_ms, 1),
        "pre_answer_ms": round(injected + process_ms, 1),
        "under_budget": injected + process_ms < EMIT_BUDGET_MS,
        "bulk_charged": sum(s["count"] for s in sites if s["bulk"]),
        "charged_sites": sites,
    }


def render(result: dict) -> str:
    lines = [f"{k}: {result[k]}" for k in (
        "delay_s", "answer_kind", "run_id", "injected_wait_ms", "process_ms", "pre_answer_ms",
        "under_budget", "bulk_charged")]
    lines.append(f"reason: {result['reason']}")
    lines.append("charged sites (first-seen order):")
    for n, s in enumerate(result["charged_sites"], 1):
        lines.append(f"  {n:>2}. {s['kind']:<7} x{s['count']:<3} {s['caller']}  {s['example']}{'  [BULK]' if s['bulk'] else ''}")
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delay-s", type=float, default=DEFAULT_DELAY_S)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = measure(args.delay_s)
    print(json.dumps(result, indent=2) if args.json else render(result))
    passed = result["answer_kind"] == "allow" and bool(result["run_id"]) and result["under_budget"]
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
