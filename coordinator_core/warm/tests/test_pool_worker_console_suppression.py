"""Pool workers open no console, and nothing breaks when their stdio is gone.

Separate from `test_pool_worker_parent_watchdog.py` because the subject is
different: that file pins how a worker DIES, this one pins how it is SPAWNED
and what its missing stdio costs.

`ProcessPoolExecutor` exposes no `creationflags` seam; multiprocessing builds
its own `CreateProcess` call through `_winapi`, which
`win_portability.install_console_less_spawn_default` wraps. `_suppress_pool_
worker_consoles` points multiprocessing at the console-subsystem `python.exe`
(never the GUI-subsystem `pythonw.exe`, which raises the AppStarting cursor)
and the seam keeps the workers windowless.

THE INVARIANT THAT MATTERS MOST HERE. Whenever a launcher supplies no stdio a
worker's `sys.stdout`/`sys.stderr` are both `None`, and the repo writes
`print(..., file=sys.stderr)` at 200+ call sites — including
`ops/fleet/_common.py::_setup_error`, on the refusal path of every fleet op.
A code-reviewer flagged (2026-08-19, slice B) that this must raise
`AttributeError` and turn a structured `exit_code: 1` refusal into an opaque
INTERNAL_ERROR. For the `print` form it does not, and the reason is worth
pinning rather than re-deriving: CPython's `print` treats a `file` argument of
`None` as "use `sys.stdout`", and when that is `None` too it returns without
writing.

THE REVIEWER WAS RIGHT ABOUT THE OTHER FORM, AND THIS FILE MISSED IT.
`print(..., file=sys.stderr)` survives a `None` stream; `sys.stderr.write(...)`
does not — it reaches for an attribute on `None` and raises. This file pinned
only the form that happens to be safe and read as clearing the whole class, so
the suppression fix shipped resting on a claim that covered maybe half its call
sites: 138 `sys.std*.write`/`.flush()` sites across 34 op modules in
`coordinator_core/ops/` were live and unprotected. Example-cockpit-repo-em hit one
2026-08-20 — `query-records --type plan` returned `-32603 Internal error:
AttributeError`, warm-only, cold-clean, emptying the plans leg of their store
build. `_bind_null_std_streams` now restores writable sinks in every worker, so
the invariant this file pins is no longer "a `None` stream is harmless" (it is
not) but "a worker never has one".

Negative-spec:
    - Does NOT spawn a pool. The end-to-end proof (a detached parent spawning
      real workers, counting visible windows) is a `spawns_process` cadence
      concern; it was run by hand when this landed — 2 windows unsuppressed,
      0 suppressed.
    - Never substitutes a different interpreter: with no `python.exe` beside
      the server's own `sys.executable`, the pool keeps that executable.
"""

from __future__ import annotations

import ast
import json
import os
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core import _claude_klabauter_root
from coordinator_core.telemetry import spawn_counter
from coordinator_core.warm import server

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only console suppression")


def _pe_subsystem(exe: str) -> int:
    with open(exe, "rb") as fh:
        head = fh.read(4096)
    (e_lfanew,) = struct.unpack_from("<I", head, 0x3C)
    # PE signature (4) + COFF header (20) + Subsystem at offset 68 of the optional header.
    (subsystem,) = struct.unpack_from("<H", head, e_lfanew + 24 + 68)
    return subsystem


@windows_only
def test_suppression_points_multiprocessing_at_a_console_exe(monkeypatch) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(server.multiprocessing, "set_executable", recorded.append)
    server._suppress_pool_worker_consoles()

    assert len(recorded) == 1, "the pool's interpreter was never redirected"
    assert Path(recorded[0]).name.lower() != "pythonw.exe"
    assert _pe_subsystem(recorded[0]) == 3, "pool executable must be console subsystem"


@windows_only
def test_a_pythonw_server_gets_its_python_exe_sibling(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "pythonw.exe").write_bytes(b"")
    (tmp_path / "python.exe").write_bytes(b"")
    recorded: list[str] = []
    monkeypatch.setattr(server.multiprocessing, "set_executable", recorded.append)
    monkeypatch.setattr(server.sys, "executable", str(tmp_path / "pythonw.exe"))
    server._suppress_pool_worker_consoles()
    assert recorded == [str(tmp_path / "python.exe")]


@windows_only
def test_no_console_sibling_leaves_the_executable_alone(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "pythonw.exe").write_bytes(b"")
    recorded: list[str] = []
    monkeypatch.setattr(server.multiprocessing, "set_executable", recorded.append)
    monkeypatch.setattr(server.sys, "executable", str(tmp_path / "pythonw.exe"))
    server._suppress_pool_worker_consoles()
    assert recorded == [str(tmp_path / "pythonw.exe")]


def test_server_entry_installs_the_seam_and_seeds_before_any_pool() -> None:
    main = next(
        n for n in ast.parse(Path(server.__file__).read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "_run_guarded"
    )
    order: list[str] = []
    for node in ast.walk(main):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in {
                "_suppress_pool_worker_consoles",
                "install_console_less_spawn_default",
                "_seed_worker_environment",
                "ProcessPoolExecutor",
            }:
                order.append((node.lineno, name))
    names = [n for _, n in sorted(order)]
    assert names[:3] == [
        "_suppress_pool_worker_consoles",
        "install_console_less_spawn_default",
        "_seed_worker_environment",
    ]
    assert "ProcessPoolExecutor" not in names[:3]


_WINDOW_PROBE = textwrap.dedent(
    """
    import ctypes, multiprocessing, sys, time
    from ctypes import wintypes
    from pathlib import Path

    def _count_visible(pid):
        u = ctypes.windll.user32
        found = []
        cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid and u.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True
        u.EnumWindows(cb_t(cb), 0)
        return len(found)

    if __name__ == "__main__":
        sys.path.insert(0, sys.argv[1])
        from coordinator_core.win_portability import console_python, install_console_less_spawn_default
        install_console_less_spawn_default()
        multiprocessing.set_executable(console_python(sys.executable))
        proc = multiprocessing.Process(target=time.sleep, args=(4,))
        proc.start()
        time.sleep(1.5)
        visible = _count_visible(proc.pid)
        k = ctypes.windll.kernel32
        proc.terminate()
        Path(sys.argv[2]).write_text(f"{visible} {k.GetConsoleCP()}")
    """
)


@windows_only
@pytest.mark.parametrize("parent", ["console-less", "create-no-window"])
def test_a_real_worker_opens_no_visible_window(tmp_path: Path, parent: str) -> None:
    script = tmp_path / "probe.py"
    script.write_text(_WINDOW_PROBE)
    out = tmp_path / "out.txt"
    root = str(Path(server.__file__).parents[2])
    if parent == "console-less":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            pytest.skip("no pythonw.exe beside the interpreter to make a console-less parent")
        argv, kw = [str(pythonw), "-I", str(script), root, str(out)], {}
    else:
        argv = [sys.executable, "-I", str(script), root, str(out)]
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW}
    subprocess.run(argv, timeout=60, check=True, stdin=subprocess.DEVNULL, **kw)
    visible, _console_cp = out.read_text().split()
    assert visible == "0", f"worker opened {visible} visible window(s) under a {parent} parent"


def test_seeded_lookup_spawns_nothing(monkeypatch) -> None:
    _claude_klabauter_root.clear_machine_local_cache()
    monkeypatch.setenv(
        _claude_klabauter_root.MACHINE_LOCAL_SEED_ENV, json.dumps({"repos.claude_klabauter": "/seeded/klabauter"})
    )
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(Path(__file__)))  # exists; would spawn if consulted
    before = spawn_counter.spawn_count()
    calls: list[object] = []
    monkeypatch.setattr(_claude_klabauter_root.subprocess, "run", lambda *a, **k: calls.append(a))

    assert _claude_klabauter_root._machine_local_get("repos.claude_klabauter") == "/seeded/klabauter"
    assert calls == []
    assert spawn_counter.spawn_count() == before


def test_seed_miss_still_spawns_once_and_memoizes(monkeypatch, tmp_path: Path) -> None:
    _claude_klabauter_root.clear_machine_local_cache()
    impl = tmp_path / "_machine_local.py"
    impl.write_text("print('from-spawn')\n")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl))
    monkeypatch.setenv(_claude_klabauter_root.MACHINE_LOCAL_SEED_ENV, json.dumps({"other.key": "x"}))
    before = spawn_counter.spawn_count()

    assert _claude_klabauter_root._machine_local_get("repos.claude_klabauter") == "from-spawn"
    assert _claude_klabauter_root._machine_local_get("repos.claude_klabauter") == "from-spawn"
    assert spawn_counter.spawn_count() - before == 1
    _claude_klabauter_root.clear_machine_local_cache()


@pytest.mark.parametrize("raw", ["not json", "[1]", '{"repos.claude_klabauter": ""}'])
def test_malformed_seed_falls_through(monkeypatch, tmp_path: Path, raw: str) -> None:
    _claude_klabauter_root.clear_machine_local_cache()
    impl = tmp_path / "_machine_local.py"
    impl.write_text("print('from-spawn')\n")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl))
    monkeypatch.setenv(_claude_klabauter_root.MACHINE_LOCAL_SEED_ENV, raw)
    assert _claude_klabauter_root._machine_local_get("repos.claude_klabauter") == "from-spawn"
    _claude_klabauter_root.clear_machine_local_cache()


def test_seed_worker_environment_exports_lookup_and_git_non_interactive(monkeypatch, tmp_path: Path) -> None:
    _claude_klabauter_root.clear_machine_local_cache()
    impl = tmp_path / "_machine_local.py"
    impl.write_text("print('/the/klabauter')\n")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl))
    for name in (_claude_klabauter_root.MACHINE_LOCAL_SEED_ENV, "GIT_TERMINAL_PROMPT"):
        monkeypatch.setenv(name, "x")  # registers the restore
        monkeypatch.delenv(name)
    monkeypatch.setenv("GCM_INTERACTIVE", "always")

    server._seed_worker_environment()

    assert json.loads(os.environ[_claude_klabauter_root.MACHINE_LOCAL_SEED_ENV]) == {
        "repos.claude_klabauter": "/the/klabauter"
    }
    assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
    assert os.environ["GCM_INTERACTIVE"] == "always", "an operator's own setting is kept"
    _claude_klabauter_root.clear_machine_local_cache()


def test_suppression_is_a_no_op_off_windows(monkeypatch) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(server.os, "name", "posix")
    monkeypatch.setattr(server.multiprocessing, "set_executable", recorded.append)
    server._suppress_pool_worker_consoles()
    assert recorded == []


def test_stderr_print_is_a_silent_no_op_when_stdio_is_none(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    print("this must not raise", file=sys.stderr, flush=True)


def test_stderr_write_DOES_raise_when_stdio_is_none(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    with pytest.raises(AttributeError):
        sys.stderr.write("this DOES raise\n")


def test_worker_init_binds_writable_streams_when_stdio_is_none(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    server._bind_null_std_streams()

    assert sys.stdout is not None and sys.stderr is not None
    sys.stderr.write("records.query: anomalous plan filename excluded: INDEX.md\n")
    sys.stderr.flush()


def test_binding_leaves_an_already_bound_stream_alone(monkeypatch) -> None:
    import io

    real_out, real_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", real_out)
    monkeypatch.setattr(sys, "stderr", real_err)
    server._bind_null_std_streams()

    assert sys.stdout is real_out
    assert sys.stderr is real_err


def test_worker_init_calls_the_binder_before_anything_else(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(server, "_bind_null_std_streams", lambda: calls.append("bind"))
    monkeypatch.setattr(server, "_preload_op_registry", lambda: calls.append("preload"))

    class _StubThread:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            calls.append("watchdog")

    monkeypatch.setattr(server.threading, "Thread", _StubThread)
    server._worker_process_init()

    assert calls[0] == "bind", f"binder did not run first: {calls}"
    assert "preload" not in calls, f"worker init preloads the op registry: {calls}"


def test_fleet_setup_error_still_returns_its_envelope_with_no_stdio(monkeypatch) -> None:
    """The refusal path a suppressed pool worker actually takes.

    `_setup_error` writes to stderr unconditionally (the cold-caller channel)
    before `_emit_warm_diagnostic` (the warm one). If the stderr write raised,
    the warm channel would never run and the caller would get an opaque
    INTERNAL_ERROR instead of this envelope.
    """
    from coordinator_core.ops.fleet._common import _setup_error

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    envelope = _setup_error("send", False, "memo.send: scoped_to.sha does not resolve")

    assert envelope["exit_code"] == 1
    assert envelope["acted"] == []
