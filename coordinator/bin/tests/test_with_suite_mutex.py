"""test_with_suite_mutex.py -- coverage for `coordinator/bin/with-suite-mutex`,
the repo-agnostic acquiring wrapper for the machine-wide test-suite mutex
(`coordinator_core.testing.suite_mutex`, DR-088 layer 6 take side).

Purpose: the mutex's READ side (the `bash_guards` deny check) has always
worked, but its TAKE side was wired into exactly two in-repo callers
(`full_runner.py`, `validate-fast-and-packageability.py`). This wrapper
closes the gap for every other caller (bare pytest, pnpm, a consumer-repo's
own runner). This suite pins:

1. Two concurrent invocations serialize: the second waits for the first
   rather than running alongside it (observed via a start/end timestamp
   file each child appends to -- no overlap in the interval each occupies).
2. The lock's recorded PID is the CHILD's, not the wrapper's own PID --
   the module docstring's PID-LIVENESS contract this wrapper exists to
   satisfy.
3. The wrapper's exit code is the child's, verbatim, on both a zero and a
   non-zero child exit.
4. The lock directory is gone after the wrapper exits, on both the success
   and the failure path.
5. An interrupted (SIGINT/SIGTERM-equivalent) run does not leave the lock
   behind.
6. The `--` separator is required and rejected when absent or empty.
7. A `--max-runtime` ceiling kills a child that stops making progress,
   kills its DESCENDANTS too, releases the mutex, and exits 124 -- the
   wedge that made this necessary is an xdist controller deadlock, where
   pytest's own `--timeout`/`--session-timeout` provably cannot fire
   because no test is running to fire them.
8. Invoked as an ENTRYPOINT via the sanctioned `python3 <path>` invocation
   (mode 100644, no shebang, per the ratified `coordinator/bin/`
   convention, `e167d08d1`) across a real subprocess boundary, the child's
   stdout and stderr both reach the parent's streams.

Each test isolates the mutex under a per-test `COORDINATOR_SETTINGS_HOME`
(tmp_path) so concurrent test runs on this shared machine never contend for
the SAME lock directory the suite is exercising.

Run:
    pytest coordinator/bin/tests/test_with_suite_mutex.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest


# Declared, not excused: this file spawns real processes because the behaviour under
# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
# not the route for a new file -- test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = _REPO_ROOT / "coordinator" / "bin" / "with-suite-mutex"


def _load_wrapper_module():
    """Import the entrypoint (no `.py` suffix, so not import-discoverable)
    in-process, so a fault can be injected between `suspend()` and
    `resume()` -- the exact window a subprocess-boundary test cannot reach
    into.
    """
    import importlib.machinery

    loader = importlib.machinery.SourceFileLoader(
        "with_suite_mutex_under_test", str(_WRAPPER)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run(args, *, env, timeout=60):
    return subprocess.run(
        [sys.executable, str(_WRAPPER)] + args,
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _base_env(settings_home: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_SETTINGS_HOME"] = str(settings_home)
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("COORDINATOR_SESSION_ID", None)
    return env


def _lock_dir(settings_home: Path) -> Path:
    return settings_home / "claude-klabauter" / "test-suite-mutex.lock"


def test_missing_separator_is_rejected(tmp_path):
    env = _base_env(tmp_path)
    result = _run(["python3", "-c", "print(1)"], env=env)
    assert result.returncode == 2
    assert "--" in result.stderr


def test_empty_command_after_separator_is_rejected(tmp_path):
    env = _base_env(tmp_path)
    result = _run(["--"], env=env)
    assert result.returncode == 2
    assert "--" in result.stderr


def test_exit_code_propagates_success(tmp_path):
    env = _base_env(tmp_path)
    result = _run(["--", sys.executable, "-c", "import sys; sys.exit(0)"], env=env)
    assert result.returncode == 0
    assert not _lock_dir(tmp_path).exists()


def test_exit_code_propagates_failure(tmp_path):
    env = _base_env(tmp_path)
    result = _run(["--", sys.executable, "-c", "import sys; sys.exit(7)"], env=env)
    assert result.returncode == 7
    assert not _lock_dir(tmp_path).exists()


def test_recorded_pid_is_childs_not_wrappers(tmp_path):
    """The lock's meta.json pid must be the child's PID, never the wrapper's."""
    env = _base_env(tmp_path)
    marker = tmp_path / "meta_snapshot.json"
    script = (
        "import json, os, pathlib, sys, time\n"
        "lock = pathlib.Path(sys.argv[1])\n"
        "deadline = time.time() + 20\n"
        "meta = None\n"
        "while time.time() < deadline:\n"
        "    p = lock / 'meta.json'\n"
        "    if p.is_file():\n"
        "        try:\n"
        "            meta = json.loads(p.read_text())\n"
        "            break\n"
        "        except Exception:\n"
        "            pass\n"
        "    time.sleep(0.05)\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps({'meta': meta, 'pid': os.getpid()}))\n"
    )
    lock_dir = _lock_dir(tmp_path)
    result = _run(
        ["--", sys.executable, "-c", script, str(lock_dir), str(marker)],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(marker.read_text())
    assert payload["meta"] is not None, "child never observed lock metadata"
    assert payload["meta"]["pid"] == payload["pid"], (
        f"recorded pid {payload['meta']['pid']} != child pid {payload['pid']}"
    )


def test_second_invocation_waits_for_first(tmp_path):
    """Two concurrent invocations must not overlap execution."""
    env = _base_env(tmp_path)
    log_path = tmp_path / "timeline.log"
    script = (
        "import sys, time\n"
        "label = sys.argv[1]\n"
        "log_path = sys.argv[2]\n"
        "with open(log_path, 'a') as f:\n"
        "    f.write(f'{label} start {time.time()}\\n')\n"
        "time.sleep(1.0)\n"
        "with open(log_path, 'a') as f:\n"
        "    f.write(f'{label} end {time.time()}\\n')\n"
    )

    results = {}

    def _launch(label):
        results[label] = _run(
            ["--", sys.executable, "-c", script, label, str(log_path)],
            env=env,
            timeout=30,
        )

    t1 = threading.Thread(target=_launch, args=("A",))
    t2 = threading.Thread(target=_launch, args=("B",))
    t1.start()
    time.sleep(0.2)
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    for label in ("A", "B"):
        assert results[label].returncode == 0, results[label].stderr

    lines = log_path.read_text().strip().splitlines()
    events = {}
    for line in lines:
        label, kind, ts = line.split()
        events.setdefault(label, {})[kind] = float(ts)

    a_start, a_end = events["A"]["start"], events["A"]["end"]
    b_start, b_end = events["B"]["start"], events["B"]["end"]

    overlap = a_start < b_end and b_start < a_end
    assert not overlap, f"intervals overlapped: A=({a_start},{a_end}) B=({b_start},{b_end})"
    assert not _lock_dir(tmp_path).exists()


def test_interrupted_run_releases_lock(tmp_path):
    """A SIGINT/terminate to the wrapper during a child run leaves no lock."""
    env = _base_env(tmp_path)
    started_marker = tmp_path / "started"
    script = (
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('1')\n"
        "time.sleep(30)\n"
    )
    # popup-intentional-last-resort: CTRL_BREAK_EVENT delivery requires a
    # real console process group -- CREATE_NO_WINDOW suppresses the console
    # entirely, which silently drops the signal this test depends on.
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP isolates the wrapper (and its child) into
        # its own console process group, so CTRL_BREAK_EVENT below reaches
        # only this process tree -- not the pytest runner sending it.
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, str(_WRAPPER), "--", sys.executable, "-c", script, str(started_marker)],
        cwd=str(_REPO_ROOT),
        env=env,
        creationflags=creationflags,
    )
    deadline = time.time() + 15
    while time.time() < deadline and not started_marker.exists():
        time.sleep(0.05)
    assert started_marker.exists(), "child never started"

    time.sleep(0.3)
    import signal as _signal

    if sys.platform == "win32":
        proc.send_signal(_signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(_signal.SIGINT)
    proc.wait(timeout=20)

    deadline = time.time() + 10
    while time.time() < deadline and _lock_dir(tmp_path).exists():
        time.sleep(0.1)
    assert not _lock_dir(tmp_path).exists(), "lock directory leaked after interrupt"


def test_entrypoint_executed_directly_streams_output(tmp_path):
    """Run the entrypoint the way a real caller does -- `python3 <path>`
    on POSIX, or the co-located `.cmd` launcher on Windows -- per the
    ratified `coordinator/bin/` convention (`e167d08d1`): NO shebang, NO
    exec bit (mode 100644). This is distinct from every other test in this
    file (which imports the module in-process) in that it goes through a
    real subprocess boundary, the same one a live caller crosses.

    Also pins the output-passthrough contract itself: the child's stdout
    and stderr text must both survive to the wrapper's own stdout/stderr,
    unmodified and uncombined.
    """
    env = _base_env(tmp_path)
    stdout_marker = "CHILD_STDOUT_MARKER_%d" % os.getpid()
    stderr_marker = "CHILD_STDERR_MARKER_%d" % os.getpid()
    # Single-line -c body (`;`-joined, no embedded newline): a `.cmd`
    # shim forwards argv via `%*`, which does not reliably preserve an
    # embedded newline inside one argv element across the batch-file hop --
    # a cmd.exe/batch quirk unrelated to this wrapper's own behavior.
    script = f"import sys; print({stdout_marker!r}); sys.stderr.write({stderr_marker!r} + chr(10))"
    # Windows cannot CreateProcess a shebangless extensionless file directly
    # -- native Windows resolves a bare entrypoint via its co-located
    # `.cmd` shim (generated by gen-launcher-shim.py), the same way a
    # caller typing the bare name in cmd.exe/PowerShell would. POSIX
    # invokes it as `python3 <path>` per the ratified convention.
    if sys.platform == "win32":
        # subprocess.run cannot CreateProcess a .cmd file directly without
        # shell=True (batch files are not standalone Win32 executables) --
        # invoke it via an explicit `cmd /c` argv instead, which resolves
        # and runs the shim exactly as a caller typing the bare name would,
        # with no shell-injection surface (every operand stays a distinct
        # argv element, never string-concatenated).
        argv = ["cmd", "/c", str(_WRAPPER) + ".cmd", "--", sys.executable, "-c", script]
    else:
        argv = [sys.executable, str(_WRAPPER), "--", sys.executable, "-c", script]
    result = subprocess.run(
        argv,
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert stdout_marker in result.stdout, (
        f"child stdout not observed on wrapper stdout: {result.stdout!r}"
    )
    assert stderr_marker in result.stderr, (
        f"child stderr not observed on wrapper stderr: {result.stderr!r}"
    )
    assert stderr_marker not in result.stdout, "stderr leaked onto stdout"
    assert stdout_marker not in result.stderr, "stdout leaked onto stderr"


def test_non_interrupt_exception_between_suspend_and_resume_resumes_and_reaps_child(tmp_path):
    """A fault injected between `suspend()` and `resume()` -- e.g. a lock
    I/O error, corrupt meta.json, anything other than KeyboardInterrupt --
    must not leave the child suspended forever. This exercises the bare
    (non-KeyboardInterrupt) exception path through `main()` directly,
    in-process, since a subprocess boundary can't inject a fault into that
    exact window.
    """
    module = _load_wrapper_module()
    real_process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        resumed = []

        class _FakeHeld:
            def __enter__(self_inner):
                raise RuntimeError("simulated lock I/O failure")

            def __exit__(self_inner, *exc_info):
                return False

        fake_ps_process = mock.Mock()
        fake_ps_process.suspend = mock.Mock()

        def _fake_resume():
            resumed.append(True)
            real_process.terminate()

        fake_ps_process.resume = _fake_resume

        with mock.patch.object(module.subprocess, "Popen", return_value=real_process), \
             mock.patch.object(module.psutil, "Process", return_value=fake_ps_process), \
             mock.patch.object(module.suite_mutex, "held", return_value=_FakeHeld()):
            with pytest.raises(RuntimeError, match="simulated lock I/O failure"):
                module.main(["--", sys.executable, "-c", "print(1)"])

        assert resumed, "resume() was never called on the exception path"
        real_process.wait(timeout=10)
        assert real_process.poll() is not None, "child was never reaped"
    finally:
        if real_process.poll() is None:
            real_process.kill()
            real_process.wait()


def test_owner_label_forwarded(tmp_path):
    env = _base_env(tmp_path)
    marker = tmp_path / "owner_snapshot.json"
    script = (
        "import json, pathlib, sys, time\n"
        "lock = pathlib.Path(sys.argv[1])\n"
        "deadline = time.time() + 20\n"
        "meta = None\n"
        "while time.time() < deadline:\n"
        "    p = lock / 'meta.json'\n"
        "    if p.is_file():\n"
        "        try:\n"
        "            meta = json.loads(p.read_text())\n"
        "            break\n"
        "        except Exception:\n"
        "            pass\n"
        "    time.sleep(0.05)\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps(meta))\n"
    )
    lock_dir = _lock_dir(tmp_path)
    result = _run(
        [
            "--owner-label",
            "my-custom-label",
            "--",
            sys.executable,
            "-c",
            script,
            str(lock_dir),
            str(marker),
        ],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    meta = json.loads(marker.read_text())
    assert meta is not None
    assert meta["owner"] == "my-custom-label-pid-%d" % meta["pid"] or meta["owner"].startswith(
        "my-custom-label"
    )


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="PATHEXT resolution is a Windows-only concern: POSIX execvp already "
    "searches PATH for a bare name, so this regression cannot reproduce there.",
)
def test_bare_name_resolves_a_cmd_shim_on_windows(tmp_path):
    """A bare `pnpm`-style name naming a `.cmd` shim must run, not WinError 2.

    Reported independently by three sibling repos (2026-08-11, 2026-08-16 x2)
    against `main`'s `subprocess.Popen(command)`: CreateProcess performs no
    PATHEXT resolution, so every `.cmd` shim on PATH -- pnpm, npm, yarn, npx --
    failed here while resolving fine from a shell.
    """
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    (shim_dir / "fakepnpm.cmd").write_text("@echo off\r\nexit /b 7\r\n", encoding="ascii")

    env = _base_env(tmp_path)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"

    result = _run(["--", "fakepnpm"], env=env)

    assert "WinError 2" not in result.stderr, result.stderr
    assert result.returncode == 7, (result.returncode, result.stdout, result.stderr)


def test_max_runtime_kills_a_stuck_child_and_releases_the_lock(tmp_path):
    """A child that never exits is killed at the ceiling, not waited on forever.

    Regression for the 2026-08-25 fast-tier hang: an xdist worker died
    uninterruptibly, the controller blocked in `dsession.loop_once` ->
    `queue.get()`, every surviving worker sat idle in `execnet ... serve()`,
    and because NO test was running neither `--timeout` nor
    `--session-timeout` could fire. The run hung indefinitely while holding
    the machine-wide mutex, which blocks every other session's suite -- so
    the lock being gone afterwards is the load-bearing assertion here, not
    the exit code.
    """
    env = _base_env(tmp_path)
    started = time.monotonic()
    result = _run(
        ["--max-runtime", "5", "--", sys.executable, "-c", "import time; exec('while True: time.sleep(5)')"],
        env=env,
        timeout=90,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 124, result.stderr
    assert not _lock_dir(tmp_path).exists(), "ceiling must release the mutex"
    # Generous upper bound: the ceiling plus terminate/kill escalation, sized so
    # a loaded box cannot flake it. The point is bounded-vs-unbounded, not a
    # tight deadline -- the failure this guards against never returned AT ALL.
    assert elapsed < 60, f"ceiling did not bound the run (took {elapsed:.1f}s)"


def test_max_runtime_kills_grandchildren_not_just_the_direct_child(tmp_path):
    """The tree, not the child. pytest-xdist's workers are children of the
    controller, and killing only the controller leaves them alive in
    `execnet ... serve()` holding real memory -- observed 2026-08-25, where
    six orphaned workers had to be killed by hand afterwards.
    """
    psutil = pytest.importorskip("psutil")
    env = _base_env(tmp_path)
    marker = tmp_path / "grandchild_pid.txt"
    result = _run(
        ["--max-runtime", "5", "--", sys.executable, "-c", 'import subprocess, sys, pathlib, time; gc = subprocess.Popen([sys.executable, \'-c\', "import time; exec(\'while True: time.sleep(5)\')"]); pathlib.Path(sys.argv[1]).write_text(str(gc.pid)); exec(\'while True: time.sleep(5)\')', str(marker)],
        env=env,
        timeout=90,
    )

    assert result.returncode == 124, result.stderr
    assert marker.is_file(), "grandchild never started -- test did not exercise the tree"
    grandchild_pid = int(marker.read_text().strip())

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not psutil.pid_exists(grandchild_pid):
            break
        time.sleep(0.25)
    else:
        # Never leak a spinning orphan out of a failing test.
        try:
            psutil.Process(grandchild_pid).kill()
        except psutil.Error:
            pass
        pytest.fail(f"grandchild {grandchild_pid} survived the ceiling -- tree kill regressed")


def test_max_runtime_zero_is_unbounded_and_a_normal_child_is_untouched(tmp_path):
    """`0` disables the ceiling, and a command that finishes normally is never
    interfered with -- the ceiling is a backstop, not a budget.
    """
    env = _base_env(tmp_path)
    result = _run(
        ["--max-runtime", "0", "--", sys.executable, "-c", "import sys; sys.exit(3)"],
        env=env,
    )
    assert result.returncode == 3
    assert not _lock_dir(tmp_path).exists()


def test_max_runtime_rejects_a_non_numeric_value(tmp_path):
    env = _base_env(tmp_path)
    result = _run(
        ["--max-runtime", "soon", "--", sys.executable, "-c", "print(1)"], env=env
    )
    assert result.returncode == 2
    assert "--max-runtime" in result.stderr
