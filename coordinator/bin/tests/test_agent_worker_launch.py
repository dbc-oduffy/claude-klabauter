"""agent-worker-launch: refusals, branch checkout, env marker and exec argv, on a throwaway repo."""
import importlib.util
from pathlib import Path

import pytest

from coordinator_core.agent_pipe import worker_mode as wm

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("agent_worker_launch", BIN / "agent-worker-launch.py")
awl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(awl)

OK = wm.Preflight(True, "")


def test_main_parses_allow_engine_repo(monkeypatch):
    seen = {}
    monkeypatch.setattr(awl, "launch", lambda *a, **k: seen.update(k) or 0)
    assert awl.main(["codex", "--allow-engine-repo"]) == 0
    assert seen["allow_engine_repo"] is True
    assert awl.main(["codex"]) == 0 and seen["allow_engine_repo"] is False


def test_main_without_args_refuses():
    assert awl.main([]) == awl.REFUSED
