"""Behavior pins for the Codex SessionStart hook's worker doctrine pointer."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_ss", BIN / "unimatrix-codex-hook-sessionstart.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

wm_spec = importlib.util.spec_from_file_location(
    "wm", BIN.parents[1] / "coordinator_core" / "agent_pipe" / "worker_mode.py")
wm = importlib.util.module_from_spec(wm_spec)
wm_spec.loader.exec_module(wm)

PAYLOAD = {"session_id": "thread-1", "cwd": "/nowhere"}


def test_env_name_comes_from_worker_mode():
    assert hook.worker_env_name() == wm.WORKER_ENV


def test_marker_set_injects_pointer_alongside_registration(tmp_path, monkeypatch):
    monkeypatch.setenv(wm.WORKER_ENV, "1")
    out = hook.run(PAYLOAD, tmp_path / "peers.json")
    assert out["hookSpecificOutput"] == {"hookEventName": "SessionStart", "additionalContext": hook.WORKER_POINTER}
    assert "systemMessage" in out
    json.dumps(out)


def test_marker_set_injects_pointer_even_without_a_thread_id(tmp_path, monkeypatch):
    monkeypatch.setenv(wm.WORKER_ENV, "1")
    out = hook.run({}, tmp_path / "peers.json")
    assert out == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": hook.WORKER_POINTER}}


@pytest.mark.parametrize("value", [None, "", "  "])
def test_marker_unset_or_blank_injects_nothing(tmp_path, monkeypatch, value):
    monkeypatch.delenv(wm.WORKER_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(wm.WORKER_ENV, value)
    out = hook.run(PAYLOAD, tmp_path / "peers.json")
    assert "hookSpecificOutput" not in out
    assert hook.run({}, tmp_path / "peers.json") is None


def test_pointer_names_doctrine_path_only():
    assert "worker-mode.md" in hook.WORKER_POINTER
