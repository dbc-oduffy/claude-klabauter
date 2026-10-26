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
import shlex
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
    # Outside any git repo: a run here must not leave a last-run pair in the real checkout.
    cwd = Path(env["COORDINATOR_SETTINGS_HOME"])
    cwd.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(_WRAPPER)] + args,
        cwd=str(cwd),
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
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("COORDINATOR_SESSION_ID", None)
    return env


class _PsutilWithProcess:
    """psutil with only `Process` replaced, as seen by the wrapper module.

    Patching `psutil.Process` itself would also blind conftest's foreign-kill
    guard, which resolves a pid's parents through the real class."""

    def __init__(self, real, fake_process):
        self._real = real
        self.Process = lambda _pid: fake_process

    def __getattr__(self, name):
        return getattr(self._real, name)


def _lock_dir(settings_home: Path) -> Path:
    return settings_home / "test-suite-mutex.lock"


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


def test_same_session_concurrent_invocations_do_not_overlap(tmp_path):
    """Two invocations sharing one session id (an admit loop's duplicate gate) serialize."""
    env = _base_env(tmp_path)
    env["CLAUDE_CODE_SESSION_ID"] = "shared-session"
    log_path = tmp_path / "timeline.log"
    script = (
        "import sys, time\n"
        "label, log_path = sys.argv[1], sys.argv[2]\n"
        "with open(log_path, 'a') as f:\n"
        "    f.write(f'{label} start {time.time()}\\n')\n"
        "time.sleep(1.5)\n"
        "with open(log_path, 'a') as f:\n"
        "    f.write(f'{label} end {time.time()}\\n')\n"
    )
    results = {}

    def _launch(label):
        results[label] = _run(
            ["--", sys.executable, "-c", script, label, str(log_path)], env=env, timeout=60
        )

    t1 = threading.Thread(target=_launch, args=("A",))
    t2 = threading.Thread(target=_launch, args=("B",))
    t1.start()
    time.sleep(0.5)
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    for label in ("A", "B"):
        assert results[label].returncode == 0, results[label].stderr
    events = {}
    for line in log_path.read_text().strip().splitlines():
        label, kind, ts = line.split()
        events.setdefault(label, {})[kind] = float(ts)
    a, b = events["A"], events["B"]
    assert not (a["start"] < b["end"] and b["start"] < a["end"]), f"overlapped: {a} {b}"
    assert not _lock_dir(tmp_path).exists()


def test_wrapped_child_with_same_session_id_reacquires_nested(tmp_path):
    """The runner the wrapper spawns (same session owner) still re-enters the held mutex."""
    env = _base_env(tmp_path)
    env["CLAUDE_CODE_SESSION_ID"] = "shared-session"
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    script = (
        "import sys\n"
        "from coordinator_core.testing import suite_mutex\n"
        "sys.exit(0 if suite_mutex.acquire(suite_mutex.mutex_owner('x'), 'nested') else 3)\n"
    )
    result = _run(["--", sys.executable, "-c", script], env=env)
    assert result.returncode == 0, result.stderr


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
        cwd=str(tmp_path),
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
        cwd=str(tmp_path),
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
             mock.patch.object(module, "psutil", _PsutilWithProcess(module.psutil, fake_ps_process)), \
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


@pytest.mark.deliberate_wall_clock(
    reason="bounds the max-runtime ceiling on a child that sleeps forever -- a kill deadline "
    "against wall-clock hang time, invisible to process/CPU time"
)
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


def test_wait_returns_immediately_when_mutex_is_free(tmp_path):
    env = _base_env(tmp_path)
    result = _run(["--wait"], env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not _lock_dir(tmp_path).exists()


def test_wait_blocks_until_holder_releases_then_exits_zero(tmp_path):
    """The wait the guard's refusal names: unblocks when the holder's run ends."""
    env = _base_env(tmp_path)
    holder = threading.Thread(
        target=lambda: _run(
            ["--", sys.executable, "-c", "import time; time.sleep(2.0)"],
            env=env, timeout=30,
        )
    )
    holder.start()
    deadline = time.monotonic() + 15
    while not _lock_dir(tmp_path).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _lock_dir(tmp_path).exists(), "holder never took the mutex"

    started = time.monotonic()
    result = _run(["--wait"], env=env, timeout=30)
    waited = time.monotonic() - started
    holder.join(timeout=30)

    assert result.returncode == 0, result.stderr
    assert waited > 0.5, f"--wait returned while the mutex was still held ({waited:.2f}s)"
    assert not _lock_dir(tmp_path).exists()


def test_wait_times_out_naming_the_holder(tmp_path):
    env = _base_env(tmp_path)
    holder = threading.Thread(
        target=lambda: _run(
            ["--",
             sys.executable, "-c", "import time; time.sleep(4.0)"],
            env=env, timeout=30,
        )
    )
    holder.start()
    deadline = time.monotonic() + 15
    while not _lock_dir(tmp_path).exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    result = _run(["--wait", "--wait-timeout", "0.5"], env=env, timeout=30)
    holder.join(timeout=30)

    assert result.returncode == 1
    assert "time.sleep(4.0)" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--wait", "--", "true"],
        ["--wait", "--max-runtime", "5"],
        ["--wait", "--wait-timeout", "soon"],
        ["--wait", "--wait-timeout"],
        ["--wait", "--wait-timeout", "-1"],
    ],
)
def test_wait_rejects_malformed_invocations(tmp_path, args):
    result = _run(args, env=_base_env(tmp_path))
    assert result.returncode == 2
    assert "--wait" in result.stderr


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, **_GIT_ENV}
    (repo / "tracked.py").write_text("x = 1\n")
    for args in (["init", "-q"], ["add", "tracked.py"], ["commit", "-q", "-m", "init"]):
        subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return repo


def _run_in(repo: Path, args, *, env, stdout=subprocess.PIPE, timeout=60):
    return subprocess.run(
        [sys.executable, str(_WRAPPER)] + args,
        cwd=str(repo), env=env, stdout=stdout, stderr=subprocess.PIPE,
        timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


_RECORD_FIELDS = (
    "schema", "repo_root", "collection", "command", "head_sha", "started_at", "ended_at",
    "dirty_paths", "duration_s", "exit_code", "outcome", "serialized", "log",
    "log_truncated", "failed_ids",
)


def test_two_consecutive_runs_leave_current_and_previous_records(tmp_path):
    repo = _make_repo(tmp_path)
    env = _base_env(tmp_path / "home")
    (repo / "tracked.py").write_text("x = 2\n")
    first = _run_in(repo, ["--collection", "unit", "--", sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'one\\n')"], env=env)
    second = _run_in(
        repo, ["--collection", "unit", "--", sys.executable, "-c", "import sys; sys.exit(3)"], env=env
    )
    assert first.returncode == 0, first.stderr
    assert second.returncode == 3, second.stderr
    swallowed = (first.stderr + second.stderr).decode("utf-8", "replace")
    assert "not written" not in swallowed, swallowed
    rec = repo / ".coordinator-local" / "suite-runs" / "unit"
    cur = json.loads((rec / "current.json").read_text())
    prev = json.loads((rec / "previous.json").read_text())
    for body in (cur, prev):
        assert set(_RECORD_FIELDS) <= set(body)
        assert len(body["head_sha"]) == 40
        assert body["dirty_paths"] == ["tracked.py"]
        assert body["collection"] == "unit"
        assert body["serialized"] is True
        assert body["log_truncated"] is False
    assert (cur["outcome"], cur["exit_code"]) == ("fail", 3)
    assert (prev["outcome"], prev["exit_code"]) == ("pass", 0)
    assert (rec / "previous.log").read_bytes() == b"one\n"
    assert not list(rec.glob("*.tmp"))


def test_log_bytes_identical_with_wrapper_stdout_piped_or_file(tmp_path):
    repo = _make_repo(tmp_path)
    env = _base_env(tmp_path / "home")
    # Raw byte writes: text-mode stdout translates "\n" to "\r\n" on Windows, which
    # would make the byte-identity assertions below measure the child, not the wrapper.
    child = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'out-a\\n'); sys.stdout.flush(); time.sleep(0.3)\n"
        "sys.stderr.buffer.write(b'err-a\\n'); sys.stderr.flush(); time.sleep(0.3)\n"
        "sys.stdout.buffer.write(b'out-b\\n')\n"
    )
    argv = ["--collection", "pipe", "--", sys.executable, "-c", child]
    rec = repo / ".coordinator-local" / "suite-runs" / "pipe"

    piped = _run_in(repo, argv, env=env)
    assert piped.returncode == 0, piped.stderr
    piped_log = (rec / "current.log").read_bytes()

    out_file = tmp_path / "wrapper-stdout.txt"
    with open(out_file, "wb") as handle:
        redirected = _run_in(repo, argv, env=env, stdout=handle)
    assert redirected.returncode == 0, redirected.stderr
    file_log = (rec / "current.log").read_bytes()

    assert piped_log == file_log
    assert b"out-a\n" in piped_log and b"err-a\n" in piped_log and b"out-b\n" in piped_log
    assert piped.stdout == out_file.read_bytes() == b"out-a\nout-b\n"


@pytest.mark.skipif(sys.platform == "win32", reason="os.fork double-fork orphan is POSIX-only")
@pytest.mark.deliberate_wall_clock(
    reason="a max-runtime kill plus a bounded tee join are wall-clock bounds by definition"
)
def test_grandchild_holding_the_pipe_does_not_hang_the_wrapper(tmp_path):
    repo = _make_repo(tmp_path)
    env = _base_env(tmp_path / "home")
    gc_pid = tmp_path / "gc.pid"
    # Double fork: the orphaned grandchild is no longer a descendant, so the
    # process-tree kill misses it while it still holds the inherited pipe write ends.
    child = (
        "import os, sys, pathlib, time\n"
        "if os.fork() == 0:\n"
        "    if os.fork() == 0:\n"
        "        pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "        time.sleep(15)\n"
        "    os._exit(0)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = _run_in(
        repo,
        ["--collection", "gc", "--max-runtime", "2", "--", sys.executable, "-c", child, str(gc_pid)],
        env=env, timeout=45,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 124, result.stderr
    assert elapsed < 14
    body = json.loads((repo / ".coordinator-local" / "suite-runs" / "gc" / "current.json").read_text())
    assert body["outcome"] == "timeout"
    assert body["log_truncated"] is True


def test_collection_must_be_a_slug(tmp_path):
    result = _run(["--collection", "Bad Name", "--", sys.executable, "-c", "pass"], env=_base_env(tmp_path))
    assert result.returncode == 2
    assert "--collection" in result.stderr


_COUNTER_CHILD = (
    "import sys, time\n"
    "time.sleep(0.3)\n"
    "open(sys.argv[1], 'a').write('x')\n"
    "sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)\n"
)


def _counter_setup(tmp_path: Path):
    repo = _make_repo(tmp_path)
    # A python stub is an unclassifiable runner footprint; declaring the repo's fast tier
    # scoped lets the real gate pass it, so only the Tier-U shapes below are refused.
    (repo / "coordinator.local.md").write_text("---\nfast_tier_shape: scoped\n---\n")
    (repo / "child.py").write_text(_COUNTER_CHILD)
    # Committed: an untracked source file makes every record non-fresh.
    env = {**os.environ, **_GIT_ENV}
    for args in (["add", "child.py", "coordinator.local.md"], ["commit", "-q", "-m", "stub"]):
        subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    counter = tmp_path / "counter.txt"
    counter.write_text("")
    return repo, [sys.executable, "child.py", str(counter)], counter


def _current(repo: Path, collection: str = "reuse") -> Path:
    return repo / ".coordinator-local" / "suite-runs" / collection / "current.json"


def _reuse_args(cmd, collection: str = "reuse"):
    return ["--collection", collection, "--reuse-fresh", "--", *cmd]


def test_reuse_fresh_hit_spawns_nothing_and_leaves_record_untouched(tmp_path):
    repo, cmd, counter = _counter_setup(tmp_path)
    env = _base_env(tmp_path / "home")
    first = _run_in(repo, _reuse_args(cmd), env=env)
    assert first.returncode == 0, first.stderr
    assert counter.read_text() == "x"
    assert b"suite-run: ran reuse@" in first.stderr
    before = _current(repo).read_bytes()
    second = _run_in(repo, _reuse_args(cmd), env=env)
    assert second.returncode == 0, second.stderr
    assert counter.read_text() == "x"
    assert b"suite-run: reused reuse@" in second.stderr
    assert b"outcome=pass" in second.stderr
    assert _current(repo).read_bytes() == before


def test_reuse_fresh_hit_on_a_failing_record_exits_with_its_code(tmp_path):
    repo, cmd, counter = _counter_setup(tmp_path)
    env = _base_env(tmp_path / "home")
    failing = [*cmd, "3"]
    assert _run_in(repo, _reuse_args(failing), env=env).returncode == 3
    again = _run_in(repo, _reuse_args(failing), env=env)
    assert again.returncode == 3
    assert counter.read_text() == "x"
    assert b"outcome=fail" in again.stderr


def test_reuse_fresh_command_mismatch_runs_once_and_rewrites_current(tmp_path):
    repo, cmd, counter = _counter_setup(tmp_path)
    env = _base_env(tmp_path / "home")
    assert _run_in(repo, _reuse_args(cmd), env=env).returncode == 0
    started = json.loads(_current(repo).read_text())["started_at"]
    time.sleep(1.1)
    other = [*cmd, "0"]
    result = _run_in(repo, _reuse_args(other), env=env)
    assert result.returncode == 0, result.stderr
    assert counter.read_text() == "xx"
    body = json.loads(_current(repo).read_text())
    assert body["started_at"] != started
    assert body["command"][1:] == other[1:]


def test_reuse_fresh_stale_record_runs_once(tmp_path):
    repo, cmd, counter = _counter_setup(tmp_path)
    env = _base_env(tmp_path / "home")
    assert _run_in(repo, _reuse_args(cmd), env=env).returncode == 0
    (repo / "tracked.py").write_text("x = 99\n")
    result = _run_in(repo, _reuse_args(cmd), env=env)
    assert result.returncode == 0, result.stderr
    assert counter.read_text() == "xx"
    assert b"suite-run: ran" in result.stderr


def test_reuse_fresh_gate_sees_quoted_argv_and_refuses_ungranted(tmp_path):
    repo, cmd, counter = _counter_setup(tmp_path)
    env = _base_env(tmp_path / "home")
    result = _run_in(
        repo,
        _reuse_args([sys.executable, "-m", "pytest", "-m", "not docs"]),
        env=env,
    )
    assert result.returncode == 77, result.stderr
    assert not _current(repo).exists()


def _in_process_main(module, monkeypatch, repo: Path, args):
    monkeypatch.chdir(repo)
    return module.main(args)


def test_reuse_fresh_gate_refusal_exits_77_without_spawning(tmp_path, monkeypatch):
    repo, cmd, counter = _counter_setup(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    module = _load_wrapper_module()
    seen = []

    def refuse(text, **kwargs):
        seen.append(text)
        from coordinator_core.session.tier_u_gate import TierUGateResult

        return TierUGateResult(proceed=False, refusal_message="refused-by-gate")

    monkeypatch.setattr(module, "enforce_tier_u_gate", refuse)
    assert _in_process_main(module, monkeypatch, repo, _reuse_args(cmd)) == 77
    assert seen and seen[0].startswith(shlex.quote(cmd[0]))
    assert counter.read_text() == ""


def test_reuse_fresh_held_mutex_exits_75_with_wait_advice(tmp_path, monkeypatch, capfd):
    repo, cmd, counter = _counter_setup(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    module = _load_wrapper_module()
    monkeypatch.setattr(module.suite_mutex, "holder", lambda *a, **k: {"owner": "peer"})
    assert _in_process_main(module, monkeypatch, repo, _reuse_args(cmd)) == 75
    assert "--wait" in capfd.readouterr().err
    assert counter.read_text() == ""


def test_contended_acquire_timeout_refuses_exit_75_and_never_runs_child(
    tmp_path, monkeypatch, capfd
):
    repo, cmd, counter = _counter_setup(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    module = _load_wrapper_module()
    monkeypatch.setattr(module.suite_mutex, "holder", lambda *a, **k: {"owner": "peer-x"})
    monkeypatch.setattr(module.suite_mutex, "acquire", lambda *a, **k: False)
    assert _in_process_main(module, monkeypatch, repo, ["--", *cmd]) == 75
    err = capfd.readouterr().err
    assert "peer-x" in err and "--wait" in err
    assert "proceeding unserialized" not in err
    time.sleep(0.6)
    assert counter.read_text() == ""


def test_reuse_fresh_lost_acquire_race_kills_the_unrun_child_and_exits_75(
    tmp_path, monkeypatch
):
    repo, cmd, counter = _counter_setup(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    module = _load_wrapper_module()
    monkeypatch.setattr(module.suite_mutex, "holder", lambda *a, **k: None)
    timeouts = []

    def lose(owner, cmd_str, timeout=0.0, **kwargs):
        timeouts.append(timeout)
        return False

    monkeypatch.setattr(module.suite_mutex, "acquire", lose)
    assert _in_process_main(module, monkeypatch, repo, _reuse_args(cmd)) == 75
    assert timeouts == [0.0]
    time.sleep(0.6)
    assert counter.read_text() == ""
    assert not _current(repo).exists()


def _last_pair(repo: Path, name: str):
    out = repo / "state" / "test-runs"
    return (out / f"{name}-last.txt"), json.loads((out / f"{name}-last.json").read_text())


def test_pass_and_fail_runs_overwrite_the_last_run_pair_with_ansi_stripped_text(tmp_path):
    repo = _make_repo(tmp_path)
    env = _base_env(tmp_path / "home")
    ok = _run_in(
        repo, ["--collection", "lr", "--", sys.executable, "-c", "print('\\x1b[31mred\\x1b[0m one')"],
        env=env,
    )
    assert ok.returncode == 0, ok.stderr
    txt, summary = _last_pair(repo, "lr")
    assert txt.read_text() == "red one\n"
    assert summary["exit"] == 0 and len(summary["sha"]) == 40

    bad = _run_in(
        repo, ["--collection", "lr", "--", sys.executable, "-c", "import sys; print('two'); sys.exit(3)"],
        env=env,
    )
    assert bad.returncode == 3, bad.stderr
    txt, summary = _last_pair(repo, "lr")
    assert txt.read_text() == "two\n"
    assert summary["exit"] == 3
    assert {"sha", "branch", "started_at", "duration_s", "exit", "counts", "failing"} <= set(summary)


def test_timeout_run_leaves_the_last_run_pair(tmp_path):
    repo = _make_repo(tmp_path)
    env = _base_env(tmp_path / "home")
    result = _run_in(
        repo,
        ["--collection", "to", "--max-runtime", "2", "--", sys.executable, "-c",
         "import time; print('started', flush=True); time.sleep(300)"],
        env=env, timeout=90,
    )
    assert result.returncode == 124, result.stderr
    txt, summary = _last_pair(repo, "to")
    assert summary["exit"] == 124
    assert "started" in txt.read_text()


def test_absent_collection_names_the_suite_from_label_or_command(tmp_path):
    repo = _make_repo(tmp_path)
    env = _base_env(tmp_path / "home")
    py = Path(sys.executable).name.lower().removesuffix(".exe")
    assert _run_in(repo, ["--owner-label", "My Label!", "--", sys.executable, "-c", "pass"], env=env).returncode == 0
    _last_pair(repo, "my-label")
    assert _run_in(repo, ["--", sys.executable, "-c", "pass"], env=env).returncode == 0
    _last_pair(repo, py)


def test_default_collection_is_a_valid_deterministic_slug():
    module = _load_wrapper_module()
    assert module._default_collection(None, ["pnpm", "run", "tier:fast"]) == "pnpm-run-tier-fast"
    assert module._default_collection(None, ["/x/Python.EXE", "-c", "print(1)"]) == "python"
    assert module._default_collection("Release Gate", ["x"]) == "release-gate"
    assert module._default_collection(None, ["--", "-x"]) == "default"
