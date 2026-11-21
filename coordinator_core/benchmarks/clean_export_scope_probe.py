"""Measure the clean-overlay export of a repo at a sha: cold build, warm advance, and the test leg inside it.

Runs `clean_overlay_verify.reset_mirror`, then `run_in_export` twice at default arguments (cold, warm) with
the given test argv, and prints one JSON line per phase: `cold` and `warm` (export prep only: process time and
Popen spawns up to the moment the export is ready, plus the checkout leg's `files_written`), `test` (the warm
run's test leg after prep: process time, `tests_collected`, `exit`), and `sparse_fill` (a scratch clone with a
one-path sparse pattern and `read-tree -m -u <sha>` timed alone). A timeout is recorded as exit "timeout".
Process time is os.times() self+children user+system, never wall clock; os.times() reads children as 0 on
Windows, so there the direct children's GetProcessTimes (sampled at `Popen.wait`) is added and a grandchild's
CPU is not in the figure (`children_source` names which).
Runs as `python coordinator_core/benchmarks/clean_export_scope_probe.py --repo-root <repo> [--sha S] -- <test argv>`.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from collections.abc import Generator

if __package__ in (None, ""):
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov  # noqa: E402

_IS_WINDOWS = sys.platform == "win32"
_REAL_POPEN = subprocess.Popen
_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)\b")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _Meter:
    """Popen constructions and, on Windows, the CPU of direct children reaped through `Popen.wait`."""

    def __init__(self) -> None:
        self.spawns = 0
        self.child_cpu_s = 0.0


_METER = _Meter()


def _handle_cpu_s(handle: int) -> float:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    ft = [wintypes.FILETIME() for _ in range(4)]
    if not k32.GetProcessTimes(handle, *(ctypes.byref(f) for f in ft)):
        return 0.0
    ticks = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime  # noqa: E731
    return (ticks(ft[2]) + ticks(ft[3])) / 1e7


class _CountingPopen(_REAL_POPEN):
    """Probe-only `subprocess.Popen`: counts constructions and samples a Windows child's CPU once it is waited."""

    _sampled = False

    def __init__(self, *args, **kwargs) -> None:
        _METER.spawns += 1
        super().__init__(*args, **kwargs)

    def wait(self, timeout=None) -> int:
        rc = super().wait(timeout)
        if _IS_WINDOWS and not self._sampled:
            self._sampled = True
            try:
                _METER.child_cpu_s += _handle_cpu_s(int(getattr(self, "_handle")))
            except (OSError, AttributeError, ValueError):
                pass
        return rc


def _cpu_ms() -> float:
    t = os.times()
    return (t.user + t.system + t.children_user + t.children_system + _METER.child_cpu_s) * 1000.0


def tests_collected(output: str) -> int:
    """Tests pytest ran, summed from its final summary line (passed, failed, skipped, xfailed, xpassed, errors)."""
    for line in reversed(output.splitlines()):
        found = _COUNT_RE.findall(line)
        if found:
            return sum(int(n) for n, _ in found)
    return 0


@contextlib.contextmanager
def _instrumented(sha: str, marks: dict, legs: dict) -> Generator[None]:
    """Route `run_in_export`'s clean_export through a leg hook and a prep mark; honour `sha`; count Popen."""
    real_export, real_commit = cov.clean_export, cov._commit_sha

    @contextlib.contextmanager
    def leg(name: str) -> Generator[dict]:
        extra: dict = {}
        yield extra
        legs[name] = extra

    @contextlib.contextmanager
    def export(root, run_id, paths, leg_hook=None, argv=()) -> Generator[Path]:
        with real_export(root, run_id, paths, leg, argv=argv) as dest:
            marks["prep_cpu"], marks["prep_spawns"] = _cpu_ms(), _METER.spawns
            yield dest

    setattr(cov, "clean_export", export)
    if sha != "HEAD":
        setattr(cov, "_commit_sha", lambda root, rev: sha if rev == "HEAD" else real_commit(root, rev))
    setattr(subprocess, "Popen", _CountingPopen)
    try:
        yield
    finally:
        setattr(subprocess, "Popen", _REAL_POPEN)
        setattr(cov, "clean_export", real_export)
        setattr(cov, "_commit_sha", real_commit)


def _phase(root: Path, sha: str, argv: list, run_id: str) -> tuple:
    """One `run_in_export` call; returns `(prep line fields, test line fields)`."""
    marks: dict = {}
    legs: dict = {}
    exit_, out = 0, ""
    with _instrumented(sha, marks, legs):
        cpu0, sp0 = _cpu_ms(), _METER.spawns
        try:
            done = cov.run_in_export(root, run_id, [], argv)
            exit_, out = done.returncode, (done.stdout or "") + (done.stderr or "")
        except subprocess.TimeoutExpired:
            exit_ = "timeout"
        except cov.ExportError as exc:
            exit_ = "timeout" if "timed out" in str(exc) else "export_error"
            out = str(exc)
    cpu1, sp1 = _cpu_ms(), _METER.spawns
    prepped = "prep_cpu" in marks
    prep_cpu = (marks["prep_cpu"] if prepped else cpu1) - cpu0
    prep_sp = (marks["prep_spawns"] if prepped else sp1) - sp0
    prep = {
        "files_written": legs.get("checkout", {}).get("files_written"),
        "process_time_ms": round(prep_cpu, 1),
        "spawns": prep_sp,
        "prepared": prepped,
        "tests_collected": tests_collected(out),
        "exit": exit_,
    }
    test = {
        "process_time_ms": round(cpu1 - cpu0 - prep_cpu, 1) if prepped else None,
        "spawns": sp1 - sp0 - prep_sp if prepped else None,
        "tests_collected": tests_collected(out),
        "exit": exit_,
    }
    if exit_ not in (0, 5) and out:
        prep["output_tail"] = test["output_tail"] = out[-300:]
    return prep, test


def _sparse_fill(root: Path, sha: str) -> dict:
    """`read-tree -m -u <sha>` alone in a scratch `clone --shared` of `root` carrying a one-path sparse pattern."""
    scratch = Path(tempfile.mkdtemp(prefix="clean-export-scope-probe-"))
    dest = scratch / "clone"
    git = cov._git_exe()
    try:
        subprocess.run([git, "clone", "--shared", "--no-checkout", "-q", os.fspath(root), os.fspath(dest)],
                       check=True, capture_output=True, creationflags=_NO_WINDOW)
        (dest / ".git" / "info").mkdir(exist_ok=True)
        (dest / ".git" / "info" / "sparse-checkout").write_text("/pyproject.toml\n", encoding="utf-8", newline="\n")
        with open(dest / ".git" / "config", "a", encoding="utf-8", newline="\n") as fh:
            fh.write("[core]\n\tsparseCheckout = true\n\tsparseCheckoutCone = false\n")
        cpu0, sp0 = _cpu_ms(), _METER.spawns
        done = subprocess.run([git, "read-tree", "-m", "-u", sha], cwd=dest, capture_output=True, text=True,
                              creationflags=_NO_WINDOW)
        cpu1, sp1 = _cpu_ms(), _METER.spawns
        files = sum(1 for p in dest.rglob("*") if p.is_file() and ".git" not in p.relative_to(dest).parts[:1])
        return {"process_time_ms": round(cpu1 - cpu0, 1), "spawns": sp1 - sp0, "files_written": files,
                "exit": done.returncode}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"process_time_ms": None, "exit": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _emit(phase: str, **fields) -> None:
    print(json.dumps({"phase": phase, **fields}), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--sha", default="HEAD")
    parser.add_argument("--no-sparse-fill", action="store_true", help="skip the sparse_fill spike phase")
    parser.add_argument("test_argv", nargs=argparse.REMAINDER, help="after `--`: the command run in the export")
    args = parser.parse_args(argv)
    test_argv = args.test_argv[1:] if args.test_argv[:1] == ["--"] else args.test_argv
    if not test_argv:
        parser.error("a test argv is required after `--`")
    root = args.repo_root.resolve()
    sha = cov._commit_sha(root, args.sha)
    children = "os.times+GetProcessTimes(direct children)" if _IS_WINDOWS else "os.times"
    common = {"repo": root.name, "sha": sha[:12], "children_source": children}
    cov.reset_mirror(root)
    for name in ("cold", "warm"):
        prep, test = _phase(root, sha, test_argv, f"scope-probe-{name}")
        _emit(name, **common, **prep)
    _emit("test", **common, **test)
    if not args.no_sparse_fill:
        with _instrumented("HEAD", {}, {}):
            spike = _sparse_fill(root, sha)
        _emit("sparse_fill", **common, **spike)
    return 0


if __name__ == "__main__":
    sys.exit(main())
