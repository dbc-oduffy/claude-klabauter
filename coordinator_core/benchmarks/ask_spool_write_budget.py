"""Ask-spool benchmark: what the engine-ask submit path costs when the engine cannot take the ask.

Three cases, each reporting process ms (never wall clock), spool-entry count, file mode and the
envelope text: the POSIX door's hook leg with no engine listening; the same door against a stub
replying -32004; `submit_engine_ask.evaluate` with an emit raising HandoverPending. Each case then
runs `ask_spool.drain.run` twice against a healthy stub engine (one mint stub, no real emit): the
first drain must mint and notify the entry once, the second must mint nothing.

Door cases are process time of the door's whole child tree (the door's own `wait4` rusage: the door and any cold
guard); the HandoverPending case is in-process CPU of the warm hook, imports excluded. `measure()` is
the entry the pin test drives. POSIX only: the door cases need the macOS/Linux door.

Run: `python -m coordinator_core.benchmarks.ask_spool_write_budget [--runs N] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.win_portability import no_console_creationflags

#: The spool-write bar (plan prime_exit_criterion): process ms per spool case.
BAR_MS = 50.0
DEFAULT_RUNS = 5
CASES = ("unreachable", "dispatch_timeout_32004", "handover_pending")
_DOOR_DIR = Path(__file__).resolve().parents[1] / "warm" / "door"
_ASK_OP = "hooks.submit_engine_ask"
_SESSION_ID = "sess-spool-bench"
_STUB_RUN_ID = "ask-20261104T000000-abc123"
_SILENT_COLD = "import sys\nsys.stdin.read()\nsys.exit(1)\n"
_REPLY_32004 = b'{"jsonrpc":"2.0","id":null,"error":{"code":-32004,"message":"warm dispatch indeterminate"}}\n'


def _door_binary(scratch: Path) -> Path:
    """The built door when it carries the spool writer, else one compiled from source into `scratch`."""
    built = _DOOR_DIR / "door"
    try:
        if built.is_file() and b"spooled as" in built.read_bytes():
            return built
    except OSError:
        pass
    cc = shutil.which("cc") or shutil.which("clang")
    if cc is None:
        raise RuntimeError("no built door with the spool writer and no C compiler")
    out = scratch / "coordinator-invoke"
    subprocess.run(
        [cc, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', '-DBUILD_ENGINE_ROOT=""',
         "-o", str(out), str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


class _ReplyingServer:
    """A Unix socket answering every connection's first request line with `reply`."""

    def __init__(self, path: Path, reply: bytes) -> None:
        self._path = path
        self._reply = reply
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                conn.sendall(self._reply)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        self._thread.join(timeout=5)
        self._path.unlink(missing_ok=True)


def _stub_engine_root(base: Path) -> Path:
    """A directory shaped enough like an engine root for the door; its cold guard is silent."""
    root = base / "stub-engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"ask-spool-bench-{os.getpid()}-{time.time_ns()}\n", encoding="utf-8")
    (root / "coordinator" / "bin").mkdir(parents=True)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(_SILENT_COLD, encoding="utf-8")
    return root.resolve()


def _fresh_repo(base: Path, name: str) -> Path:
    repo = base / name
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    return repo


def _hook_bytes(repo: Path, tool_use_id: str) -> bytes:
    body = {
        "hook_event_name": "PreToolUse", "session_id": _SESSION_ID, "cwd": str(repo / "sub"),
        "tool_name": "Bash", "tool_use_id": tool_use_id,
        "tool_input": {"command": "engine-ask submit \"spool bench ask\""},
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _entries(repo: Path) -> list:
    spool = repo / ".coordinator-local" / "ask-spool"
    return sorted(p for p in spool.iterdir() if p.is_file() and p.suffix == ".json") if spool.is_dir() else []


def _clear_spool(repo: Path) -> None:
    shutil.rmtree(repo / ".coordinator-local", ignore_errors=True)
    shutil.rmtree(repo / "scratch", ignore_errors=True)


def _door_call(door: Path, engine_root: Path, runtime_base: Path, repo: Path, payload: bytes) -> tuple[float, str]:
    """One door call; process ms is that child's own `wait4` rusage (its tree), so another thread
    or test reaping children in the same interpreter cannot leak into the figure."""
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(engine_root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    proc = subprocess.Popen(
        [str(door), _ASK_OP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, cwd=str(repo), **no_console_creationflags(),
    )
    watchdog = threading.Timer(60.0, proc.kill)
    watchdog.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(payload)
        proc.stdin.close()
        out = proc.stdout.read()
        proc.stdout.close()
        _, status, ru = os.wait4(proc.pid, 0)
        proc.returncode = os.waitstatus_to_exitcode(status)
    finally:
        watchdog.cancel()
    return (ru.ru_utime + ru.ru_stime) * 1000.0, out.decode("utf-8", "replace")


def _hook_call(repo: Path, tool_use_id: str) -> tuple[float, str]:
    """One warm `evaluate` whose emit raises HandoverPending; in-process CPU ms and the envelope text."""
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit.requirements_ledger import HandoverPending

    def emit(_params, _root):
        raise HandoverPending(5)

    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": "coordinator:engine-ask", "args": {"ask": "spool bench ask", "source": "pm"}},
        "session_id": _SESSION_ID, "tool_use_id": tool_use_id, "cwd": str(repo),
    }
    t0 = time.process_time()
    out = hook.evaluate(payload, emit=emit)
    return (time.process_time() - t0) * 1000.0, json.dumps(out)


def _stub_evaluate(minted: list) -> Callable[[dict], dict]:
    """A healthy engine's submit hook: allow, naming one run; counts mints in `minted`."""
    def evaluate(_payload: dict) -> dict:
        minted.append(_STUB_RUN_ID)
        return {"hookSpecificOutput": {
            "permissionDecision": "allow", "updatedInput": {"scriptPath": "x"},
            "permissionDecisionReason": f"Minted {_STUB_RUN_ID}.",
        }}
    return evaluate


def _drain_twice(repo: Path) -> dict:
    from coordinator_core.ask_spool import drain

    minted: list = []
    first = drain.run(str(repo), evaluate=_stub_evaluate(minted))
    second = drain.run(str(repo), evaluate=_stub_evaluate(minted))
    notices = repo / ".coordinator-local" / "ask-spool" / drain.NOTICES_DIR / f"{_SESSION_ID}.jsonl"
    return {"first": first, "second": second, "stub_mints": len(minted), "notice_lines": (
        len(notices.read_text(encoding="utf-8").splitlines()) if notices.is_file() else 0)}


def _mode(path: Path) -> str | None:
    if os.name == "nt":
        return None
    return format(path.stat().st_mode & 0o777, "04o")


def _measure_case(name: str, call: Callable[[str], tuple[float, str]], repo: Path, runs: int) -> dict:
    """Median of `runs` calls, each on a cleared spool; the last call's spool is kept for the drain."""
    samples, text = [], ""
    for i in range(runs):
        _clear_spool(repo)
        ms, text = call(f"toolu_{name}_{i}")
        samples.append(ms)
    files = _entries(repo)
    return {
        "case": name,
        "process_ms": round(statistics.median(samples), 1),
        "max_ms": round(max(samples), 1),
        "spool_entries": len(files),
        "mode": _mode(files[0]) if files else None,
        "names_spool_id": bool(files) and f"spooled as {files[0].stem}" in text,
        "envelope_reason": text[:200],
    }


def measure(runs: int = DEFAULT_RUNS) -> dict:
    """Run every case and its two drains; `{case: report}` with a `drain` sub-report each."""
    if os.name == "nt":
        raise RuntimeError("ask_spool_write_budget drives the POSIX door; it does not run on Windows")
    from coordinator_core.temp_layout import make_managed_temp_dir

    scratch = make_managed_temp_dir(prefix="ask-spool-bench-")
    # The drains mint run tokens under the settings home; script mode has no pytest quarantine.
    home_scope = contextlib.ExitStack()
    home_scope.enter_context(fixture_settings_home(scratch))
    # AF_UNIX paths cap near 104 bytes on macOS: the socket base must be short, so /tmp not the managed tempdir.
    from coordinator_core.temp_layout import mkdtemp_in
    short = Path(os.path.realpath(mkdtemp_in("asb-", "/tmp")))
    server: Optional[_ReplyingServer] = None
    old_session = os.environ.get("COORDINATOR_SESSION_ID")
    try:
        door = _door_binary(scratch)
        engine_root = _stub_engine_root(scratch)
        from coordinator_core.warm import breadcrumb, election, skew

        os.environ[breadcrumb.RUNTIME_BASE_ENV] = str(short)
        sock = election.socket_path(skew.compute_client_token(engine_root), engine_clone=engine_root)
        report: dict = {}

        repo = _fresh_repo(scratch, "unreachable")
        report["unreachable"] = _measure_case(
            "unreachable",
            lambda tid: _door_call(door, engine_root, short, repo, _hook_bytes(repo, tid)), repo, runs)
        report["unreachable"]["drain"] = _drain_twice(repo)

        repo = _fresh_repo(scratch, "timeout")
        sock.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(sock.parent, 0o700)
            os.chmod(sock.parent.parent, 0o755)
        server = _ReplyingServer(sock, _REPLY_32004)
        report["dispatch_timeout_32004"] = _measure_case(
            "dispatch_timeout_32004",
            lambda tid: _door_call(door, engine_root, short, repo, _hook_bytes(repo, tid)), repo, runs)
        server.close()
        server = None
        report["dispatch_timeout_32004"]["drain"] = _drain_twice(repo)

        repo = _fresh_repo(scratch, "handover")
        os.environ["COORDINATOR_SESSION_ID"] = _SESSION_ID
        _hook_call(repo, "toolu_warmup")  # imports and first-call costs are the cold engine's, not the warm hook's
        report["handover_pending"] = _measure_case("handover_pending", lambda tid: _hook_call(repo, tid), repo, runs)
        report["handover_pending"]["drain"] = _drain_twice(repo)
        return report
    finally:
        home_scope.close()
        if server is not None:
            server.close()
        if old_session is None:
            os.environ.pop("COORDINATOR_SESSION_ID", None)
        else:
            os.environ["COORDINATOR_SESSION_ID"] = old_session
        shutil.rmtree(short, ignore_errors=True)
        rmtree_or_raise(scratch, label="ask-spool-bench")


def case_passes(res: dict) -> bool:
    """The falsifier's expected_when_true for one case."""
    d = res["drain"]
    return (
        res["spool_entries"] == 1 and res["mode"] == "0600" and res["process_ms"] < BAR_MS
        and res["names_spool_id"] and d["first"]["minted"] == 1 and d["first"]["notified"] == 1
        and d["second"]["minted"] == 0
    )


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when any case misses the {BAR_MS:.0f}ms / drain contract")
    args = ap.parse_args()
    report = measure(args.runs)
    failed = False
    for name, res in report.items():
        print(f"== {name}")
        for key, value in res.items():
            print(f"{key}: {value}")
        ok = case_passes(res)
        failed = failed or not ok
        print(f"verdict: {'PASS' if ok else 'FAIL'} (spool_entries=1, mode=0600, process_ms<{BAR_MS:.0f}, drain 1 then 0)")
    return 2 if args.enforce_bar and failed else 0


if __name__ == "__main__":
    sys.exit(main())
