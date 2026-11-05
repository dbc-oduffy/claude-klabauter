"""The POSIX door spools a `hooks.submit_engine_ask` payload when the engine cannot take it.

Pins both sides of the decision: an ask on either hook failure path (engine
unreachable and the cold guard silent; engine took the frame and died) is kept
as one 0600 spool entry and the envelope names it; any other op on the same
paths is passed loudly and writes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm.tests.test_door_read_deadline_posix import (  # noqa: F401 -- fixture
    _PeerDeathServer,
    _make_socket_dir,
    _make_stub_engine_root,
    _socket_path_for,
    runtime_base,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name == "nt", reason="door_posix is a macOS/Linux binary"),
]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_ASK = "hooks.submit_engine_ask"
_SILENT_COLD = "import sys\nsys.stdin.read()\nsys.exit(1)\n"


@pytest.fixture(scope="module")
def door(tmp_path_factory) -> Path:
    cc = shutil.which("cc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("door") / "coordinator-invoke"
    subprocess.run(
        [cc, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', '-DBUILD_ENGINE_ROOT=""',
         "-o", str(out), str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    return repo


def _payload(repo: Path, tool_use_id: str | None = "toolu_1") -> bytes:
    body = {
        "hook_event_name": "PreToolUse",
        "session_id": "sess-1",
        "cwd": str(repo / "sub"),
        "tool_name": "Bash",
        "tool_input": {"command": "engine-ask submit \"the PM said \\\"hi\\\"\""},
    }
    if tool_use_id:
        body["tool_use_id"] = tool_use_id
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _run(door: Path, root: Path, runtime_base: Path, repo: Path, payload: bytes, op: str = _ASK):
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(_SILENT_COLD, encoding="utf-8")
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    return subprocess.run(
        [str(door), op], input=payload, capture_output=True, env=env, cwd=str(repo), timeout=60,
        **no_console_creationflags(),
    )


def _spool_files(repo: Path) -> list[Path]:
    spool = repo / ".coordinator-local" / "ask-spool"
    return sorted(spool.iterdir()) if spool.is_dir() else []


def _assert_spooled(proc, repo: Path, payload: bytes) -> str:
    assert proc.returncode == 0
    files = _spool_files(repo)
    assert len(files) == 1
    entry = files[0]
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    header_line, _, raw = entry.read_bytes().partition(b"\n")
    assert raw == payload
    header = json.loads(header_line)
    assert header["op"] == _ASK
    assert header["session_id"] == "sess-1"
    assert header["repo_root"] == str(repo)
    assert entry.name == header["spool_id"] + ".json"
    body = json.loads(proc.stdout.decode("utf-8"))
    for text in (body["systemMessage"], body["hookSpecificOutput"]["additionalContext"]):
        assert f"spooled as {header['spool_id']}" in text
    assert "permissionDecision" not in json.dumps(body)
    return header["spool_id"]


def test_an_unreachable_engine_spools_the_ask(door, tmp_path, runtime_base):
    root = _make_stub_engine_root(tmp_path)
    repo = _repo(tmp_path)
    payload = _payload(repo)
    _assert_spooled(_run(door, root, runtime_base, repo, payload), repo, payload)


def test_an_engine_that_took_the_frame_and_died_spools_the_ask(door, tmp_path, runtime_base):
    root = _make_stub_engine_root(tmp_path)
    repo = _repo(tmp_path)
    payload = _payload(repo)
    sock = _socket_path_for(runtime_base, root)
    _make_socket_dir(sock)
    server = _PeerDeathServer(sock)
    try:
        proc = _run(door, root, runtime_base, repo, payload)
    finally:
        server.close()
    assert server.request.endswith(b"\n")
    _assert_spooled(proc, repo, payload)


def test_the_same_ask_twice_is_one_entry(door, tmp_path, runtime_base):
    root = _make_stub_engine_root(tmp_path)
    repo = _repo(tmp_path)
    payload = _payload(repo)
    first = _run(door, root, runtime_base, repo, payload)
    spool_id = _assert_spooled(first, repo, payload)
    second = _run(door, root, runtime_base, repo, payload)
    assert second.returncode == 0
    assert [p.name for p in _spool_files(repo)] == [spool_id + ".json"]
    assert f"spooled as {spool_id}" in json.loads(second.stdout)["systemMessage"]


def test_two_asks_without_a_tool_use_id_get_distinct_entries(door, tmp_path, runtime_base):
    root = _make_stub_engine_root(tmp_path)
    repo = _repo(tmp_path)
    first = _payload(repo, tool_use_id=None)
    second = first.replace(b"the PM said", b"the PM then said")
    _run(door, root, runtime_base, repo, first)
    _run(door, root, runtime_base, repo, second)
    assert len(_spool_files(repo)) == 2


def test_another_op_on_the_same_failure_path_is_passed_loudly_and_not_spooled(door, tmp_path, runtime_base):
    root = _make_stub_engine_root(tmp_path)
    repo = _repo(tmp_path)
    proc = _run(door, root, runtime_base, repo, _payload(repo), op="hooks.preuse_bash_dispatch")
    assert proc.returncode == 0
    assert "guard did not run" in json.loads(proc.stdout)["systemMessage"]
    assert _spool_files(repo) == []


def test_the_spool_id_matches_the_python_contract(door, tmp_path, runtime_base):
    from coordinator_core.ask_spool.record import spool_id_for

    root = _make_stub_engine_root(tmp_path)
    repo = _repo(tmp_path)
    payload = _payload(repo)
    spool_id = _assert_spooled(_run(door, root, runtime_base, repo, payload), repo, payload)
    assert spool_id == spool_id_for("sess-1", "toolu_1", payload)
