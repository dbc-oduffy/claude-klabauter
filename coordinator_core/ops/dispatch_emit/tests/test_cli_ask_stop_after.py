"""CLI ask route: --stop-after plan reaches compose_ask_script and is refused off the ask route."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, cli

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _emit(repo, capsys, *argv):
    rc = cli.main([*argv, "--repo-root", str(repo)])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_stop_after_plan_composes_no_execute_phase(repo, capsys):
    rc, out, _ = _emit(repo, capsys, "--ask", "x", "--stop-after", "plan")
    assert rc == 0
    script = Path(json.loads(out)["path"]).read_text(encoding="utf-8")
    assert "phase('plan')" in script
    assert "phase('execute')" not in script
    assert "phase('review')" not in script


def test_without_stop_after_composes_execute_phase(repo, capsys):
    rc, out, _ = _emit(repo, capsys, "--ask", "x")
    assert rc == 0
    assert "phase('execute')" in Path(json.loads(out)["path"]).read_text(encoding="utf-8")


def test_stop_after_without_ask_is_refused_naming_the_flag(repo, capsys):
    rc, _, err = _emit(repo, capsys, "--plan", "p.md", "--stop-after", "plan")
    assert rc != 0
    assert "--stop-after" in err


def test_stop_after_rejects_other_values(repo, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--ask", "x", "--stop-after", "execute", "--repo-root", str(repo)])
