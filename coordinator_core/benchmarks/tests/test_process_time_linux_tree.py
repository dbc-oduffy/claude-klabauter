"""Pins the Linux arm of `single_invocation_tree_process_time`: the whole
descendant tree of an arbitrary command, measured once.

Falsifiable against the two ways a Linux tree primitive silently lies: a
parent-only CPU figure (descendants read as free) and an audit-hook-style
count that cannot see a non-Python binary's own forking.
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_LINUX,
    single_invocation_tree_process_time,
)

pytestmark = [
    pytest.mark.skipif(not IS_LINUX, reason="this arm is Linux-only"),
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SPAWNER = textwrap.dedent(
    """
    import subprocess, sys
    for _ in range(int(sys.argv[1])):
        subprocess.run([sys.executable, "-c", "sum(range(1000000))"])
    """
)

# The grandchild outlives the root: it is orphaned, then re-parented to the
# measuring supervisor by PR_SET_CHILD_SUBREAPER. Without that, its CPU is
# lost (Darwin's permanent hole, module docstring).
_ORPHANER = textwrap.dedent(
    """
    import subprocess, sys
    subprocess.Popen([sys.executable, "-c", "sum(range(8000000))"])
    """
)


def _script(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_descendant_cpu_is_attributed_not_dropped(tmp_path):
    script = _script(tmp_path, "spawner.py", _SPAWNER)
    none = single_invocation_tree_process_time(
        [sys.executable, script, "0"], stdout_path=str(tmp_path / "a.out")
    )
    many = single_invocation_tree_process_time(
        [sys.executable, script, "6"], stdout_path=str(tmp_path / "b.out")
    )
    assert none["rc"] == 0 and many["rc"] == 0
    assert many["process_time_ms"] > none["process_time_ms"] + 20.0, (
        f"descendant CPU not attributed: {many['process_time_ms']}ms with 6 "
        f"children vs {none['process_time_ms']}ms with none"
    )


def test_procs_counts_the_root_and_every_descendant(tmp_path):
    script = _script(tmp_path, "spawner.py", _SPAWNER)
    result = single_invocation_tree_process_time(
        [sys.executable, script, "5"], stdout_path=str(tmp_path / "c.out")
    )
    assert result["procs"] == 6, f"root + 5 children expected, got {result['procs']}"
    assert result["k"] == 1


def test_non_python_root_tree_is_counted(tmp_path):
    """A shell root that forks twice: invisible to an audit hook, counted here."""
    result = single_invocation_tree_process_time(
        ["sh", "-c", "true; /bin/true; /bin/true"], stdout_path=str(tmp_path / "d.out")
    )
    assert result["rc"] == 0
    assert result["procs"] >= 3, result


def test_orphaned_descendant_cpu_and_count_are_included(tmp_path):
    script = _script(tmp_path, "orphaner.py", _ORPHANER)
    baseline = single_invocation_tree_process_time(
        [sys.executable, "-c", "pass"], stdout_path=str(tmp_path / "e0.out")
    )
    result = single_invocation_tree_process_time(
        [sys.executable, script], stdout_path=str(tmp_path / "e.out")
    )
    assert result["procs"] == 2, result
    assert result["process_time_ms"] > baseline["process_time_ms"] + 30.0, (
        "an orphaned descendant's CPU was lost: "
        f"{result['process_time_ms']}ms vs bare-interpreter {baseline['process_time_ms']}ms"
    )


def test_stdout_is_captured_and_rc_is_reported(tmp_path):
    out = tmp_path / "captured.out"
    err = tmp_path / "captured.err"
    result = single_invocation_tree_process_time(
        [sys.executable, "-c", "import sys; print('evidence'); sys.stderr.write('e'); raise SystemExit(3)"],
        stdout_path=str(out),
        stderr_path=str(err),
    )
    assert result["rc"] == 3
    assert out.read_text(encoding="utf-8").strip() == "evidence"
    assert err.read_text(encoding="utf-8") == "e"
    assert result["stdout_path"] == str(out)


def test_cwd_and_env_reach_the_child(tmp_path):
    out = tmp_path / "ce.out"
    single_invocation_tree_process_time(
        [sys.executable, "-c", "import os; print(os.getcwd(), os.environ['TREE_PROBE'])"],
        env={**os.environ, "TREE_PROBE": "yes"},
        cwd=str(tmp_path),
        stdout_path=str(out),
    )
    assert out.read_text(encoding="utf-8").split() == [os.path.realpath(tmp_path), "yes"]


def test_missing_executable_raises_rather_than_reporting_a_figure():
    with pytest.raises(FileNotFoundError):
        single_invocation_tree_process_time(["/nonexistent/definitely-not-here"])


def test_sigchld_ignore_refuses_to_under_report():
    saved = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(RuntimeError, match="SIGCHLD"):
            single_invocation_tree_process_time([sys.executable, "-c", "pass"])
    finally:
        signal.signal(signal.SIGCHLD, saved)


def test_caller_is_not_made_a_subreaper_or_tracer(tmp_path):
    """The subreaper/ptrace state lives in the supervisor, not the caller."""
    single_invocation_tree_process_time(
        [sys.executable, "-c", "pass"], stdout_path=str(tmp_path / "f.out")
    )
    with open("/proc/self/status", encoding="ascii") as fh:
        tracer = next(line for line in fh if line.startswith("TracerPid:"))
    assert tracer.split()[1] == "0"
    # Caller has no leftover children for the supervisor to have orphaned onto it.
    with pytest.raises(ChildProcessError):
        os.wait()
