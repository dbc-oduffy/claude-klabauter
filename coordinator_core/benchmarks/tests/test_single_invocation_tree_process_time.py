"""Pins `single_invocation_tree_process_time` against the trap it exists to
close: a spawn-heavy operation's descendants reading as free.

The floor-probe measurement this primitive was built for
(`state/audits/2026-08-25-close-ceremony-floor-probe.md`) reported the close
ceremony at 2312.5ms with `child_cpu_ms` at 0.0 across 47 git spawns -- a
figure that is a FLOOR wearing a total's clothes. These tests fail if the
primitive ever regresses to reporting a parent's own CPU as the tree's cost.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    single_invocation_tree_process_time,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.skipif(
        not (IS_WINDOWS or IS_DARWIN),
        reason="no process-tree accounting primitive on this platform (by design)",
    ),
    # Real processes are not incidental here -- an instrument for measuring
    # process trees cannot be verified against a stubbed spawn.
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# Deliberately CPU-bearing rather than sleep-bearing: a sleeping child burns
# wall clock and no CPU, which would pass a wall-clock assertion while the
# process-time assertion this file exists for read zero.
_SPAWNING_CHILD = textwrap.dedent(
    """
    import subprocess, sys
    for _ in range(int(sys.argv[1])):
        subprocess.run(
            [sys.executable, "-c", "sum(range(200000))"],
            stdout=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    print("child-done")
    """
)


def _write_child(tmp_path) -> str:
    path = tmp_path / "spawner.py"
    path.write_text(_SPAWNING_CHILD, encoding="utf-8")
    return str(path)


def test_descendant_cpu_is_attributed_not_dropped(tmp_path):
    """The whole point: a tree that spawns must cost more than one that does not.

    Asserted as a RATIO between two runs of the same script rather than
    against an absolute millisecond figure -- an absolute threshold would
    encode this box's speed into the suite and go red on the fleet-floor
    MacBook for a reason that has nothing to do with correctness.
    """
    script = _write_child(tmp_path)

    none = single_invocation_tree_process_time(
        [sys.executable, script, "0"],
        stdout_path=str(tmp_path / "none.out"),
    )
    many = single_invocation_tree_process_time(
        [sys.executable, script, "6"],
        stdout_path=str(tmp_path / "many.out"),
    )

    assert none["rc"] == 0 and many["rc"] == 0
    assert many["process_time_ms"] > none["process_time_ms"], (
        "descendant CPU was not attributed to the tree -- this is trap 1 "
        f"(os.times() children == 0.0 on Windows) back again: "
        f"{many['process_time_ms']}ms with 6 spawning children vs "
        f"{none['process_time_ms']}ms with none"
    )


def test_procs_counts_the_root_and_every_descendant(tmp_path):
    """`procs` includes the root, so N spawns report N+1.

    Pinned because the difference between "the ceremony spawned 47" and
    "the tree holds 48 processes" is exactly the off-by-one that turns a
    census into an argument.
    """
    script = _write_child(tmp_path)

    result = single_invocation_tree_process_time(
        [sys.executable, script, "5"],
        stdout_path=str(tmp_path / "five.out"),
    )

    assert result["procs"] == 6, (
        f"expected root + 5 children = 6 processes, got {result['procs']}"
    )
    assert result["k"] == 1


def test_stdout_is_captured_to_the_named_path(tmp_path):
    """For a once-only invocation the output IS the evidence, so it must
    reach disk rather than the batched path's DEVNULL."""
    out = tmp_path / "captured.out"

    result = single_invocation_tree_process_time(
        [sys.executable, "-c", "print('evidence')"],
        stdout_path=str(out),
    )

    assert result["stdout_path"] == str(out)
    assert out.read_text(encoding="utf-8").strip() == "evidence"


def test_a_failing_command_reports_its_rc_rather_than_raising(tmp_path):
    """A ceremony that exits non-zero is still a measured ceremony -- the
    primitive's job is the timing, not process health (same division of
    labour `batched_process_time_ms` states for its own `rc`)."""
    result = single_invocation_tree_process_time(
        [sys.executable, "-c", "raise SystemExit(3)"],
        stdout_path=str(tmp_path / "fail.out"),
        stderr_path=str(tmp_path / "fail.err"),
    )

    assert result["rc"] == 3
    assert result["process_time_ms"] >= 0.0


def test_the_floor_this_primitive_replaces_still_reads_zero_on_windows(tmp_path):
    """Not a test of our code -- a live pin on the PLATFORM fact that
    justifies this module existing.

    If CPython ever starts populating `os.times()` children on Windows,
    this test goes red and the reader is told to re-examine whether the
    job-object route is still the necessary one, rather than discovering
    years later that the docstring's trap 1 became folklore.
    """
    if not IS_WINDOWS:
        pytest.skip("trap 1 is Windows-specific; POSIX populates these fields")

    script = _write_child(tmp_path)
    probe = textwrap.dedent(
        f"""
        import os, subprocess, sys
        subprocess.run(
            [sys.executable, {script!r}, "4"],
            stdout=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        t = os.times()
        print(t.children_user, t.children_system)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    children_user, children_system = (float(v) for v in out.stdout.split())

    assert children_user == 0.0 and children_system == 0.0, (
        "os.times() now reports child CPU on Windows -- trap 1 in "
        "process_time.py's docstring may no longer hold. Re-verify before "
        "trusting any probe built on os.times() here."
    )


def test_absent_stdio_paths_are_reported_as_none():
    """Inheritance is the documented default, and the returned dict says so
    -- a caller reading `stdout_path: None` must not conclude the output was
    discarded."""
    result = single_invocation_tree_process_time([sys.executable, "-c", "pass"])

    assert result["stdout_path"] is None
    assert result["stderr_path"] is None
    assert result["rc"] == 0
    assert os.path.sep  # keeps the os import honest on every platform
