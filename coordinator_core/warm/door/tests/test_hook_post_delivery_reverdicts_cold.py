"""A delivered hook call with no usable answer gets the cold chain's verdict, not the allow advisory.

Drives the built door (Windows twin via build.py on Windows, POSIX twin off Windows) at its
default hook invocation, `hooks.preuse_bash_dispatch`, against an engine root whose cold leg
runs the real engine op. Two stub engines: one reads the request and closes the connection,
one answers -32004. `git clean -fd` over an untracked `state/` path is denied by the cold
chain; `ls` is not denied and is not answered silently.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import make_stub_engine_root
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.warm_tier,
    pytest.mark.cadence,
]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[4]
_OP = "hooks.preuse_bash_dispatch"
_WORKER_DIED = (
    '{"jsonrpc":"2.0","id":1,"error":{"code":-32004,"message":"warm dispatch indeterminate '
    "[worker_died: op='hooks.preuse_bash_dispatch' BrokenProcessPool]\"}}\n"
)
_ADVISORY_TAIL = "guard not evaluated, command allowed."
_MODES = ("closes-after-request", "answers-32004")

_DOOR_BASENAME = "hook-run"
_COLD_ENTRYPOINT = (
    "import runpy, sys\n"
    f"sys.path[:0] = [{str(_REPO / 'coordinator' / 'bin')!r}, {str(_REPO)!r}]\n"
    "from coordinator_core import ipc\n"
    "ipc.allow_unstamped_dispatch()\n"
    f"runpy.run_path({str(_REPO / 'coordinator' / 'bin' / 'hook-run.py')!r}, run_name='__main__')\n"
)


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(path), check=True, capture_output=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "victim-repo"
    _git_init(path)
    (path / "state" / "handoffs").mkdir(parents=True)
    (path / "state" / "handoffs" / "unsaved-baton.md").write_text("load-bearing\n", encoding="utf-8")
    return path


def _engine_root(tmp_path: Path) -> Path:
    root = make_stub_engine_root(tmp_path)
    (root / "coordinator" / "bin" / f"{_DOOR_BASENAME}.py").write_text(_COLD_ENTRYPOINT, encoding="utf-8")
    return root


def _payload(command: str, cwd: Path) -> bytes:
    return json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "session_id": "reverdict-cold-sess",
        "cwd": str(cwd),
        "tool_input": {"command": command},
    }).encode()


def _hook_env(root: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_ENGINE_ROOT=str(_REPO),
        COORDINATOR_DOOR_STDIN_MODE="hook",
        COORDINATOR_SETTINGS_HOME=str(root.parent / "settings-home"),
        **extra,
    )
    return env


def _out(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout.decode("utf-8")
    assert text.strip(), proc.stderr
    return json.loads(text.strip().splitlines()[-1])


_TELEMETRY_PARTS = ("agent-process-ledger", "fail-open.log")


def _bookkeeping(settings_home: Path) -> dict[str, bytes]:
    """Every file the guard chain's recording run appends, minus the per-call telemetry rows."""
    if not settings_home.exists():
        return {}
    return {
        str(p.relative_to(settings_home)): p.read_bytes()
        for p in sorted(settings_home.rglob("*"))
        if p.is_file() and not any(part in p.name for part in _TELEMETRY_PARTS)
    }


def _hso(body: dict) -> dict:
    return body.get("hookSpecificOutput", {})


def _assert_cold_chain_denied(body: dict) -> None:
    hso = _hso(body)
    assert hso.get("permissionDecision") == "deny", body
    assert _ADVISORY_TAIL not in json.dumps(body)
    assert "irreversible-external command is not run unguarded" not in hso["permissionDecisionReason"]
    assert "git clean" in hso["permissionDecisionReason"]


def _assert_loud_allow(body: dict) -> None:
    """Not denied, and not an empty pass: the cold verdict (rewrite or context) or the loud advisory."""
    hso = _hso(body)
    assert hso.get("permissionDecision") != "deny", body
    assert body.get("systemMessage") or hso.get("updatedInput") or hso.get("additionalContext"), body


if os.name == "nt":
    from coordinator_core.warm.door import build as door_build
    from coordinator_core.warm.tests.door_test_support import ReplyingServer, pipe_name_for

    class _ClosingServer(ReplyingServer):
        """Reads the request frame, then drops the connection without answering."""

        def _serve(self) -> None:
            try:
                self._winapi.ConnectNamedPipe(self._handle, False)
                while b"\n" not in self.request:
                    chunk, _ = self._winapi.ReadFile(self._handle, 65536)
                    if not chunk:
                        return
                    self.request += chunk
                self._winapi.CloseHandle(self._handle)
            except OSError:
                pass

    @pytest.fixture(scope="module")
    def built_door(tmp_path_factory: pytest.TempPathFactory):
        work = tmp_path_factory.mktemp("door-src")
        stamp_root = make_stub_engine_root(work)
        out = work / f"{_DOOR_BASENAME}.exe"
        door = door_build.build(stamp_root, python_bin=Path(sys.executable), output=out, windowless=False)
        return lambda _root: door

    def _stub_engine(root: Path, runtime_base: Path, mode: str):
        reply = _WORKER_DIED.encode()
        if mode == "closes-after-request":
            return _ClosingServer(pipe_name_for(root), b"")
        return ReplyingServer(pipe_name_for(root), reply)

    def _run_door(built_door, root: Path, runtime_base: Path, mode: str, command: str, repo: Path):
        door = built_door(root)
        server = _stub_engine(root, runtime_base, mode)
        try:
            return subprocess.run(
                [str(door), _OP], input=_payload(command, repo), capture_output=True,
                env=_hook_env(root), cwd=str(repo), timeout=60, **no_console_creationflags(),
            )
        finally:
            server.close()

    @pytest.fixture
    def runtime_base(tmp_path: Path) -> Path:
        return tmp_path

else:
    from coordinator_core.warm.tests.test_door_read_deadline_posix import (  # noqa: F401 -- fixture
        _make_socket_dir,
        _socket_path_for,
        runtime_base,
    )

    @pytest.fixture(scope="module")
    def built_door(tmp_path_factory: pytest.TempPathFactory):
        cc = shutil.which("cc") or shutil.which("clang")
        if cc is None:
            pytest.skip("no C compiler")
        out = tmp_path_factory.mktemp("door") / _DOOR_BASENAME
        subprocess.run(
            [cc, "-O2", "-std=c11", f'-DPYTHON_BIN="{sys.executable}"',
             "-o", str(out), str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
            check=True, capture_output=True, **no_console_creationflags(),
        )
        return lambda _root: out

    class _StubServer:
        def __init__(self, sock_path: Path, reply: bytes) -> None:
            self._path = sock_path
            self._reply = reply
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(sock_path))
            self._sock.listen(1)
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()

        def _serve(self) -> None:
            try:
                conn, _ = self._sock.accept()
                seen = b""
                while b"\n" not in seen:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    seen += chunk
                if self._reply:
                    conn.sendall(self._reply)
                conn.close()
            except OSError:
                pass

        def close(self) -> None:
            self._thread.join(timeout=10)
            try:
                self._sock.close()
            except OSError:
                pass
            self._path.unlink(missing_ok=True)

    def _run_door(built_door, root: Path, runtime_base: Path, mode: str, command: str, repo: Path):
        sock = _socket_path_for(runtime_base, root)
        _make_socket_dir(sock)
        server = _StubServer(sock, b"" if mode == "closes-after-request" else _WORKER_DIED.encode())
        try:
            return subprocess.run(
                [str(built_door(root)), _OP], input=_payload(command, repo), capture_output=True,
                env=_hook_env(root, COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base)),
                cwd=str(repo), timeout=60, **no_console_creationflags(),
            )
        finally:
            server.close()


@pytest.mark.parametrize("mode", _MODES)
def test_git_clean_over_untracked_state_is_denied_by_the_cold_chain(
    built_door, tmp_path: Path, runtime_base: Path, repo: Path, mode: str
) -> None:
    # The cold re-verdict runs on the hook deadline's remaining budget; a cold start that
    # outlives it yields the post-delivery advisory, not a deny.
    root = _engine_root(tmp_path)
    proc = _run_door(built_door, root, runtime_base, mode, "git clean -fd", repo)
    _assert_cold_chain_denied(_out(proc))
    assert (repo / "state" / "handoffs" / "unsaved-baton.md").exists()


@pytest.mark.parametrize("mode", _MODES)
def test_a_non_irreversible_command_is_not_denied_and_is_not_silent(
    built_door, tmp_path: Path, runtime_base: Path, repo: Path, mode: str
) -> None:
    root = _engine_root(tmp_path)
    proc = _run_door(built_door, root, runtime_base, mode, "ls", repo)
    _assert_loud_allow(_out(proc))


@pytest.mark.parametrize("mode", _MODES)
def test_the_cold_re_verdict_appends_no_guard_bookkeeping(
    built_door, tmp_path: Path, runtime_base: Path, repo: Path, mode: str
) -> None:
    root = _engine_root(tmp_path)
    settings_home = tmp_path / "settings-home"
    before = _bookkeeping(settings_home)
    _assert_cold_chain_denied(_out(_run_door(built_door, root, runtime_base, mode, "git clean -fd", repo)))
    assert _bookkeeping(settings_home) == before


def test_a_recording_cold_run_does_write_the_bookkeeping_the_door_leg_suppresses(
    tmp_path: Path, repo: Path
) -> None:
    """Control: the same payload through the same cold entrypoint, minus the verdict-only env."""
    root = _engine_root(tmp_path)
    settings_home = tmp_path / "settings-home"
    env = _hook_env(root)
    env.pop("COORDINATOR_GUARD_VERDICT_ONLY", None)
    proc = subprocess.run(
        [sys.executable, "-c", _COLD_ENTRYPOINT, _OP], input=_payload("git clean -fd", repo),
        capture_output=True, env=env, cwd=str(repo), timeout=60, **no_console_creationflags(),
    )
    _assert_cold_chain_denied(_out(proc))
    assert any("chain-arrival" in name for name in _bookkeeping(settings_home))
