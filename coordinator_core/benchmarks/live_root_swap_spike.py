"""Spike: which swap unit makes a staged engine tree live while a process still holds the old one.

Builds throwaway roots under `<repo>/scratch/live-root-swap-spike/` and drives throwaway python
children (never a live engine) to measure, per swap unit, whether a directory rename succeeds under
the open-handle conditions the warm server creates (cwd pinned to the root, modules imported from
the package, a transient append handle), the gap a two-step swap leaves, and whether a pointer
resolved at successor start avoids the rename entirely.

Run: `python -m coordinator_core.benchmarks.live_root_swap_spike [--out result.json] [--keep]`.
Prints one JSON document; exit 0 always (the verdict is the data). Results are platform-specific:
the written conclusions in the synthesis cover Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT

WORK_ROOT = ENGINE_CHECKOUT_ROOT / "scratch" / "live-root-swap-spike"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SWAP_REPEATS = 21  # odd, so the run ends on the staged version and the post-swap views differ

# The child imports the package from `root`, optionally holds one file open, then answers
# stdin commands: `ver` lazily imports `coordinator_core.lazymod` and prints its VERSION,
# anything else (or EOF) exits. `--pointer` resolves the root from a pointer file at start.
_CHILD = r'''
import importlib, os, sys
args = sys.argv[1:]
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
root = opt("--root")
pointer = opt("--pointer")
if pointer:
    root = open(pointer, encoding="utf-8").read().strip()
cwd = opt("--cwd") or root
os.chdir(cwd)
sys.path.insert(0, root)
import coordinator_core.mod as m
held = opt("--hold")
fh = open(held, opt("--hold-mode", "rb")) if held else None
print("READY", root, m.VERSION, flush=True)
for line in sys.stdin:
    if line.strip() == "ver":
        try:
            importlib.invalidate_caches()
            mod = importlib.import_module("coordinator_core.lazymod")
            print("VER", mod.VERSION, flush=True)
        except Exception as exc:
            print("VERERR", type(exc).__name__, flush=True)
    else:
        break
'''


def _err(exc: BaseException) -> dict:
    return {
        "type": type(exc).__name__,
        "winerror": getattr(exc, "winerror", None),
        "errno": getattr(exc, "errno", None),
        "text": str(exc)[:160],
    }


def _timed(fn, *a) -> dict:
    t0 = time.perf_counter()
    try:
        fn(*a)
        return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000, 3)}
    except OSError as exc:
        return {"ok": False, "ms": round((time.perf_counter() - t0) * 1000, 3), "error": _err(exc)}


def build_root(root: Path, version: int, with_git: bool = True) -> Path:
    """A throwaway engine-shaped tree: .git, pyproject, coordinator_core pkg, sibling dirs, state."""
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator" / "bin").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "state").mkdir()
    (root / "pyproject.toml").write_text(f"[project]\nname='spike{version}'\n", encoding="utf-8")
    (root / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "coordinator_core" / "mod.py").write_text(f"VERSION = {version}\n", encoding="utf-8")
    (root / "coordinator_core" / "lazymod.py").write_text(f"VERSION = {version}\n", encoding="utf-8")
    for i in range(40):
        (root / "coordinator_core" / f"filler_{i}.py").write_text(f"X = {i}\n", encoding="utf-8")
    (root / "coordinator" / "bin" / "tool.py").write_text(f"V = {version}\n", encoding="utf-8")
    (root / "docs" / "note.md").write_text(f"v{version}\n", encoding="utf-8")
    (root / "state" / "log.txt").write_text("x\n", encoding="utf-8")
    if with_git:
        subprocess.run(
            ["git", "init", "-q", str(root)], check=False, creationflags=_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return root


class Child:
    def __init__(self, child_py: Path, *argv: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(child_py), *argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=_NO_WINDOW,
        )
        line = self.proc.stdout.readline().strip()
        if not line.startswith("READY"):
            raise RuntimeError(f"child did not start: {line!r} {self.proc.stderr.read()[:200]}")
        self.ready = line

    def ver(self) -> str:
        self.proc.stdin.write("ver\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline().strip()

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 -- cleanup of a throwaway child
            self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass


def _rmtree(path: Path) -> dict:
    def onerr(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)

    hook = {"onexc": onerr} if sys.version_info >= (3, 12) else {"onerror": onerr}
    return _timed(lambda: shutil.rmtree(path, **hook))


def _rename_scenario(name: str, work: Path, child_py: Path, *, unit: str, cwd: str, hold: Optional[str]) -> dict:
    """Rename `unit` ('root' or 'pkg') while one child has `cwd` and its modules loaded from the tree."""
    root = build_root(work / name / "live", 1)
    dest_parent = root.parent if unit == "root" else root
    src = root if unit == "root" else root / "coordinator_core"
    dst = dest_parent / ("live.old" if unit == "root" else "coordinator_core.old")
    cwd_path = {"root": root, "pkg": root / "coordinator_core", "elsewhere": work}[cwd]
    argv = ["--root", str(root), "--cwd", str(cwd_path)]
    if hold:
        held_file = root / hold
        argv += ["--hold", str(held_file)]
        if hold.startswith("state"):
            argv += ["--hold-mode", "a"]
    child = Child(child_py, *argv)
    try:
        result = _timed(os.rename, src, dst)
        restored = None
        if result["ok"]:
            restored = _timed(os.rename, dst, src)
    finally:
        child.stop()
    return {"scenario": name, "unit": unit, "child_cwd": cwd, "held_file": hold, "rename": result, "rename_back": restored}


def _swap_scenario(work: Path, child_py: Path) -> dict:
    """Two-step swap of coordinator_core/ with a child alive (cwd = root): gap, post-swap view."""
    live = build_root(work / "swap" / "live", 1)
    staged = build_root(work / "swap" / "staged", 2, with_git=False)
    pkg, old, new = live / "coordinator_core", live / "coordinator_core.old", staged / "coordinator_core"
    child = Child(child_py, "--root", str(live), "--cwd", str(live))
    gaps: list[float] = []
    failures: list[dict] = []
    try:
        for i in range(SWAP_REPEATS):
            r1 = _timed(os.rename, pkg, old)
            r2 = _timed(os.rename, new, pkg) if r1["ok"] else {"ok": False}
            if not (r1["ok"] and r2["ok"]):
                failures.append({"iter": i, "step1": r1, "step2": r2})
                break
            gaps.append(round(r1["ms"] + r2["ms"], 3))  # the package is absent for both renames
            if i == SWAP_REPEATS - 1:
                break
            # next round: the old dir becomes the staged one
            shutil.rmtree(new.parent, ignore_errors=True)
            new.parent.mkdir(exist_ok=True)
            os.rename(old, new)
        old_child_view = child.ver()
        fresh = Child(child_py, "--root", str(live), "--cwd", str(live))
        try:
            fresh_ready = fresh.ready
            fresh_view = fresh.ver()
        finally:
            fresh.stop()
        cleanup = _rmtree(old) if old.exists() else None
    finally:
        child.stop()
    return {
        "scenario": "pkg_two_step_swap",
        "repeats_ok": len(gaps),
        "gap_ms_min": min(gaps) if gaps else None,
        "gap_ms_median": statistics.median(gaps) if gaps else None,
        "gap_ms_max": max(gaps) if gaps else None,
        "failures": failures,
        "old_process_lazy_import_after_swap": old_child_view,
        "fresh_process_after_swap": fresh_ready,
        "fresh_process_lazy_import": fresh_view,
        "rmtree_old_dir_while_child_alive": cleanup,
    }


def _multi_swap_scenario(work: Path, child_py: Path) -> dict:
    """Swap every top-level directory except .git plus one root file, child cwd = root."""
    live = build_root(work / "multi" / "live", 1)
    staged = build_root(work / "multi" / "staged", 2, with_git=False)
    tops = [p.name for p in staged.iterdir() if p.is_dir()]
    files = [p.name for p in staged.iterdir() if p.is_file()]
    graveyard = live.parent / "graveyard"
    graveyard.mkdir()
    child = Child(child_py, "--root", str(live), "--cwd", str(live))
    steps: list[dict] = []
    t0 = time.perf_counter()
    try:
        for name in tops:
            a = _timed(os.rename, live / name, graveyard / name)
            b = _timed(os.rename, staged / name, live / name) if a["ok"] else {"ok": False}
            steps.append({"dir": name, "out": a, "in": b})
        for name in files:
            steps.append({"file": name, "replace": _timed(os.replace, staged / name, live / name)})
        total_ms = round((time.perf_counter() - t0) * 1000, 3)
        git_alive = subprocess.run(
            ["git", "-C", str(live), "rev-parse", "--git-dir"], capture_output=True, text=True,
            creationflags=_NO_WINDOW,
        ).stdout.strip()
        fresh = Child(child_py, "--root", str(live), "--cwd", str(live))
        fresh_ready = fresh.ready
        fresh.stop()
    finally:
        child.stop()
    return {
        "scenario": "toplevel_dirs_swap",
        "unit_count": len(tops),
        "swap_total_ms": total_ms,
        "all_ok": all(s.get("out", s.get("replace", {})).get("ok") and s.get("in", {"ok": True}).get("ok") for s in steps),
        "steps": steps,
        "git_dir_after": git_alive,
        "fresh_process_after_swap": fresh_ready,
    }


def _pointer_scenario(work: Path, child_py: Path) -> dict:
    """Pointer file resolved at successor start: flip, start a successor, old process keeps v1."""
    base = work / "pointer"
    v1 = build_root(base / "v1", 1)
    v2 = build_root(base / "v2", 2)
    pointer = base / "engine-root.pointer"
    pointer.write_text(str(v1), encoding="utf-8")
    old = Child(child_py, "--pointer", str(pointer))
    out: dict[str, Any] = {"scenario": "pointer_flip"}
    new = None
    try:
        tmp = base / "engine-root.pointer.tmp"
        tmp.write_text(str(v2), encoding="utf-8")
        out["flip_os_replace"] = _timed(os.replace, tmp, pointer)
        new = Child(child_py, "--pointer", str(pointer))
        out["successor_ready"] = new.ready
        out["successor_lazy_import"] = new.ver()
        out["old_ready"] = old.ready
        out["old_lazy_import_after_flip"] = old.ver()
        out["rmtree_v1_while_old_alive"] = _rmtree(v1)
        # a pointer reader holding the file open blocks the flip on Windows
        holder = open(pointer, "rb")
        try:
            tmp.write_text(str(v1), encoding="utf-8")
            out["flip_while_reader_holds_pointer_open"] = _timed(os.replace, tmp, pointer)
        finally:
            holder.close()
    finally:
        old.stop()
        if new:
            new.stop()
    out["rmtree_v1_after_old_exits"] = _rmtree(v1) if v1.exists() else {"ok": True, "note": "already gone"}
    try:
        from coordinator_core.warm import breadcrumb, election

        s1, s2 = breadcrumb.svc_dir(v1), breadcrumb.svc_dir(v2)
        out["svc_dir_differs_per_root"] = s1 != s2
        out["svc_dir_v1_v2_leaf"] = [s1.name, s2.name]
        out["pipe_name_differs_per_root"] = (
            election.pipe_name("tok", engine_clone=v1) != election.pipe_name("tok", engine_clone=v2)
        )
    except Exception as exc:  # noqa: BLE001 -- measurement of a side question must not sink the spike
        out["svc_dir_probe_error"] = repr(exc)
    return out


def run(work: Path, keep: bool = False) -> dict:
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    child_py = work / "child.py"
    child_py.write_text(_CHILD, encoding="utf-8")
    results: list[dict] = []
    try:
        results.append(_rename_scenario("root_rename__cwd_in_root", work, child_py, unit="root", cwd="root", hold=None))
        results.append(_rename_scenario("root_rename__cwd_elsewhere", work, child_py, unit="root", cwd="elsewhere", hold=None))
        results.append(_rename_scenario("root_rename__cwd_elsewhere__append_handle_in_state", work, child_py, unit="root", cwd="elsewhere", hold="state/log.txt"))
        results.append(_rename_scenario("pkg_rename__cwd_root", work, child_py, unit="pkg", cwd="root", hold=None))
        results.append(_rename_scenario("pkg_rename__cwd_inside_pkg", work, child_py, unit="pkg", cwd="pkg", hold=None))
        results.append(_rename_scenario("pkg_rename__open_file_in_pkg", work, child_py, unit="pkg", cwd="root", hold="coordinator_core/filler_0.py"))
        results.append(_swap_scenario(work, child_py))
        results.append(_multi_swap_scenario(work, child_py))
        results.append(_pointer_scenario(work, child_py))
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "scenarios": results,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="also write the JSON here")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway work dir")
    ap.add_argument("--work", default=str(WORK_ROOT))
    ns = ap.parse_args(argv)
    text = json.dumps(run(Path(ns.work), keep=ns.keep), indent=2)
    print(text)
    if ns.out:
        Path(ns.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
