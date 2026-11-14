"""A denied START admission write is bookkeeping failure: the run proceeds and records it."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.codex_runner import workflow as agents
from coordinator_core.group_em import admission_post
from coordinator_core.ops.workflow_fire import headless_agents


@pytest.fixture
def denied_ledger(monkeypatch, tmp_path):
    """Real admission_post.post against a holder, with the ledger write refused."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "01a1b63b-acfb-78d1-86f2-a74ad6ca4572")
    monkeypatch.setattr(admission_post.nomination, "fleet_holder", lambda *a: {"session_id": "holder", "live": True})
    monkeypatch.setattr(admission_post, "local_box_id", lambda *_a: "box")
    monkeypatch.setattr(admission_post, "settings_home", lambda: tmp_path / "settings")

    def refuse(*_a, **_kw):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(admission_post.os, "open", refuse)
    host = SimpleNamespace(admission_file=lambda: "unused", read_recorded_peak=lambda *_a: None)
    monkeypatch.setattr(agents.capacity, "_load_box_memory_host", lambda: host)
    monkeypatch.setattr(agents.capacity, "admit_before_spawn",
                        lambda key, peak, **_kw: SimpleNamespace(admitted=True, peak_gib=peak, reason="ok"))
    monkeypatch.setattr(agents.capacity, "release_spawn", lambda *a, **kw: None)
    monkeypatch.setattr(agents.slot_grant, "check_slot_grant", lambda *a, **kw: (False, None))
    script = tmp_path / "cli.py"
    script.write_text(
        "import sys,json\n"
        "args=sys.argv[1:]\n"
        "sys.stdin.read()\n"
        "open(args[args.index('-o')+1],'w',encoding='utf-8').write('{\"ok\":true}')\n"
        "print(json.dumps({'type':'turn.completed'}))\n", encoding="utf-8")
    monkeypatch.setattr(agents, "resolve_codex", lambda **_kw: [sys.executable, str(script)])
    monkeypatch.setattr(agents, "preflight_auth", lambda **_kw: None)
    return tmp_path


def test_post_returns_undelivered_when_ledger_dir_cannot_be_created(monkeypatch, tmp_path):
    monkeypatch.setattr(admission_post.nomination, "fleet_holder", lambda *a: {"session_id": "holder"})
    monkeypatch.setattr(admission_post, "local_box_id", lambda *_a: "box")

    def refuse(self, *_a, **_kw):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "mkdir", refuse)
    posted = admission_post.post("START", "sid", directory=tmp_path / "group-em")
    assert posted["delivered"] is False and posted["reason"].startswith("write_failed:PermissionError")


def test_post_still_refuses_with_no_holder(monkeypatch, tmp_path):
    monkeypatch.setattr(admission_post.nomination, "fleet_holder", lambda *a: None)
    monkeypatch.setattr(admission_post.nomination, "box_holder", lambda *a: None)
    assert admission_post.post("START", "sid", directory=tmp_path)["reason"] == "no_box_holder"


def test_codex_start_denied_write_runs_child_and_records(denied_ledger, capsys):
    tmp_path = denied_ledger
    assert agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent") == '{"ok":true}'
    record = json.loads((tmp_path / "agent" / "admission-undelivered.json").read_text(encoding="utf-8"))
    assert record["phase"] == "START" and record["reason"].startswith("write_failed:PermissionError")
    assert "undelivered" in capsys.readouterr().err


def test_codex_slot_refusal_stays_fatal(denied_ledger, monkeypatch):
    monkeypatch.setattr(agents.capacity, "admit_before_spawn",
                        lambda key, peak, **_kw: SimpleNamespace(admitted=False, peak_gib=peak, reason="full"))
    with pytest.raises(agents.AgentFailed, match="memory admission refused"):
        agents.run_agent("x", {}, repo_root=denied_ledger, artifact_dir=denied_ledger / "agent")


def test_headless_start_site_is_advisory(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(headless_agents.admission_post, "record_undelivered",
                        lambda d, posted: seen.append((d, posted)))
    monkeypatch.setattr(headless_agents.admission_post, "post",
                        lambda phase, *a, **kw: {"delivered": False, "reason": "write_failed:PermissionError"})
    host = SimpleNamespace(admission_file=lambda: "unused", read_recorded_peak=lambda *_a: None)
    monkeypatch.setattr(headless_agents.capacity, "_load_box_memory_host", lambda: host)
    monkeypatch.setattr(headless_agents.capacity, "admit_before_spawn",
                        lambda key, peak, **_kw: SimpleNamespace(admitted=True, peak_gib=peak, reason="ok"))
    monkeypatch.setattr(headless_agents.capacity, "release_spawn", lambda *a, **kw: None)
    monkeypatch.setattr(headless_agents.slot_grant, "check_slot_grant", lambda *a, **kw: (False, None))
    art = tmp_path / "agent"
    art.mkdir()
    proc = headless_agents._run_admitted(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], "p", env=dict(os.environ),
        repo_root=tmp_path, artifact_dir=art, backend="codex", options={}, parent_session_id="sid",
        timeout_s=30, on_spawn=None, stdout=-3, stderr=-3)
    assert proc.returncode == 0
    assert seen and seen[0][1]["phase"] == "START" and seen[0][0] == art
