"""The conftest redirect keeps a live-root hub resolution under tmp."""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core import conftest as _cc
from coordinator_core.session import core


def test_env_session_id_with_live_cwd_writes_under_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "live-hub-redirect-probe")
    live_root = Path(_cc._LIVE_HUB).parent.parent
    hub = core.sessions_dir(str(live_root))
    assert hub
    assert os.path.normcase(os.path.abspath(hub)) != os.path.normcase(
        os.path.abspath(_cc._LIVE_HUB)
    )
    entry = Path(hub) / os.environ["COORDINATOR_SESSION_ID"]
    entry.mkdir(parents=True)
    (entry / "touch-record.jsonl").write_text("{}\n", encoding="utf-8")
    assert not (Path(_cc._LIVE_HUB) / "live-hub-redirect-probe").exists()
    assert entry.is_dir()


def test_non_live_repo_resolution_is_untouched(tmp_path):
    (tmp_path / ".git").mkdir()
    core.reset_sessions_dir_cache()
    assert Path(core.sessions_dir(str(tmp_path))) == tmp_path / ".git" / "coordinator-sessions"
