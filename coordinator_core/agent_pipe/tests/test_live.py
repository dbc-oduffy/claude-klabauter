"""find_live against fixture ps/lsof output and a tmp_path home."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.agent_pipe import live

AGY_CONV = "11111111-aaaa-4bbb-8ccc-222222222222"
THREAD = "33333333-dddd-4eee-8fff-444444444444"
THREAD2 = "55555555-dddd-4eee-8fff-666666666666"
CWD = "/work/proj"

PS = "\n".join([
    "  100 ttys001 /opt/bin/agy",
    "  200 ttys000 /cx/bin/codex",
    "  300 ??      /cx/bin/codex app-server --listen unix:// --managed-daemon",
    "  400 ??      /cx/bin/codex app-server proxy",
    "  500 ttys002 /usr/bin/vim notes",
])


def _lsof(home: Path, codex_threads: list[str], agy_cwd: str = CWD, codex_cwd: str = CWD) -> str:
    daemon = ["p300", "fcwd", "n/"]
    for t in codex_threads:
        daemon += ["f9", f"n{home}/.codex/thread-writer-locks/{t}.lock",
                   "f10", f"n{home}/.codex/sessions/2026/11/03/rollout-2026-11-03T00-00-00-{t}.jsonl"]
    daemon += ["f11", f"n{home}/.codex/thread-writer-locks/.coordination.lock"]
    return "\n".join([
        "p100", "fcwd", f"n{agy_cwd}", "f5", f"n{home}/.gemini/antigravity-cli/brain/{AGY_CONV}",
        "f6", f"n{home}/.gemini/antigravity-cli/brain/{AGY_CONV}/scratch",
        "p200", "fcwd", f"n{codex_cwd}", "f3", "n/dev/ttys000",
        *daemon,
    ]) + "\n"


def _write_rollout(home: Path, thread: str, cwd: str) -> None:
    d = home / ".codex" / "sessions" / "2026" / "11" / "03"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"rollout-2026-11-03T00-00-00-{thread}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": cwd}}) + "\n{}\n", encoding="utf-8")


class Runner:
    def __init__(self, lsof_text: str):
        self.calls: list[list[str]] = []
        self.lsof_text = lsof_text

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = PS if argv[0] == "ps" else self.lsof_text
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


@pytest.fixture(autouse=True)
def _posix(monkeypatch):
    monkeypatch.setattr(live.sys, "platform", "darwin")
    monkeypatch.delenv("CODEX_HOME", raising=False)


def test_agy_pid(tmp_path):
    r = Runner(_lsof(tmp_path, []))
    out = live.find_live(pid=100, run=r, home=tmp_path)
    assert out == {"sessions": [{"agent": "antigravity", "pid": 100, "cwd": CWD, "conversation": AGY_CONV}]}
    assert [c[0] for c in r.calls] == ["ps", "lsof"]
    assert "100" in r.calls[1] and "300" not in r.calls[1][2]


def test_codex_pid_via_daemon_rollout(tmp_path):
    _write_rollout(tmp_path, THREAD, CWD)
    r = Runner(_lsof(tmp_path, [THREAD]))
    out = live.find_live(pid=200, run=r, home=tmp_path)
    assert out == {"sessions": [{"agent": "codex", "pid": 200, "cwd": CWD, "conversation": THREAD}]}
    assert len(r.calls) == 2


def test_codex_lock_dir_fallback_when_no_daemon_lsof_names(tmp_path):
    locks = tmp_path / ".codex" / "thread-writer-locks"
    locks.mkdir(parents=True)
    (locks / f"{THREAD}.lock").write_text("")
    (locks / ".coordination.lock").write_text("")
    _write_rollout(tmp_path, THREAD, CWD)
    out = live.find_live(pid=200, run=Runner(_lsof(tmp_path, [])), home=tmp_path)
    assert [s["conversation"] for s in out["sessions"]] == [THREAD]


def test_cwd_match_returns_both_agents_and_filters_others(tmp_path):
    _write_rollout(tmp_path, THREAD, CWD)
    out = live.find_live(cwd=CWD, run=Runner(_lsof(tmp_path, [THREAD])), home=tmp_path)
    assert {(s["agent"], s["conversation"]) for s in out["sessions"]} == {
        ("antigravity", AGY_CONV), ("codex", THREAD)}
    assert live.find_live(cwd="/elsewhere", run=Runner(_lsof(tmp_path, [THREAD])), home=tmp_path) == {"sessions": []}


def test_no_match_is_empty_list(tmp_path):
    assert live.find_live(pid=999, run=Runner(""), home=tmp_path) == {"sessions": []}
    assert live.find_live(pid=500, run=Runner(""), home=tmp_path) == {"sessions": []}


def test_two_threads_one_cwd_one_tui_flags_ambiguous(tmp_path):
    _write_rollout(tmp_path, THREAD, CWD)
    _write_rollout(tmp_path, THREAD2, CWD)
    out = live.find_live(pid=200, run=Runner(_lsof(tmp_path, [THREAD, THREAD2])), home=tmp_path)
    assert len(out["sessions"]) == 2 and all(s["ambiguous"] for s in out["sessions"])


def test_codex_thread_in_other_cwd_not_attributed(tmp_path):
    _write_rollout(tmp_path, THREAD, "/other")
    out = live.find_live(pid=200, run=Runner(_lsof(tmp_path, [THREAD])), home=tmp_path)
    assert out == {"sessions": []}


def test_lsof_absent_is_unsupported(tmp_path):
    def run(argv, **kw):
        if argv[0] == "lsof":
            raise FileNotFoundError("lsof")
        return SimpleNamespace(returncode=0, stdout=PS, stderr="")

    out = live.find_live(pid=100, run=run, home=tmp_path)
    assert out["sessions"] == [] and "lsof" in out["unsupported"]


def test_ps_timeout_is_unsupported(tmp_path):
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)

    assert "ps" in live.find_live(run=run, home=tmp_path)["unsupported"]


def test_windows_is_unsupported_without_spawning(tmp_path, monkeypatch):
    monkeypatch.setattr(live.sys, "platform", "win32")

    def run(*a, **k):
        raise AssertionError("spawned")

    assert live.find_live(pid=1, run=run, home=tmp_path)["unsupported"]


def test_agy_two_brain_ids_picks_newest(tmp_path):
    old, new = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"
    brain = tmp_path / ".gemini" / "antigravity-cli" / "brain"
    for i, c in enumerate((new, old)):
        (brain / c).mkdir(parents=True)
    import os
    os.utime(brain / old, (1, 1))
    os.utime(brain / new, (2000, 2000))
    text = "p100\nfcwd\nn/w\nf5\nn%s/%s\nf6\nn%s/%s\n" % (brain, old, brain, new)
    out = live.find_live(pid=100, run=Runner(text), home=tmp_path)
    assert out["sessions"][0]["conversation"] == new
