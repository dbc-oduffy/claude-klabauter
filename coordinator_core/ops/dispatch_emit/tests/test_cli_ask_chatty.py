"""CLI ask route: --chatty composes a chatty planBlitz call and replies with the run's EM mailbox."""

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
    # A bare ask chains into its {sizing} script and embeds no planBlitz call; the wave args ride
    # that call, so the script under test is the one an M+ sizing composes.
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_M_PLUS)
    return tmp_path


def _emit(repo, capsys, *argv):
    rc = cli.main([*argv, "--repo-root", str(repo)])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_chatty_ask_carries_wave_args_and_mailbox(repo, capsys):
    rc, out, err = _emit(repo, capsys, "--ask", "x", "--chatty")
    assert rc == 0, err
    reply = json.loads(out)
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert '"chatty": true' in script
    assert '"chattyReview"' in script
    mailbox = Path(reply["em_mailbox"])
    assert mailbox.is_file()
    assert mailbox.name == "_em.jsonl" and mailbox.parent.name == "mail"
    assert mailbox.parent.parent.name == reply["run_id"]
    assert reply["em_mailbox"] in reply["em_watch"]
    assert reply["em_mailbox"] in reply["em_rewatch"]


def test_chatty_reply_mailbox_is_the_brief_escalate_target(repo, capsys):
    from coordinator_core.ops.dispatch_emit import chatty

    _, out, _ = _emit(repo, capsys, "--ask", "x", "--chatty")
    reply = json.loads(out)
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    mailbox = Path(reply["em_mailbox"])
    assert mailbox == record_dir(repo, reply["run_id"]) / "mail" / chatty.EM_MAILBOX
    briefs = chatty.review_wave_args(reply["run_id"])["memberBriefs"]
    assert briefs
    assert all(f"<run-dir>/mail/{chatty.EM_MAILBOX}" in b for b in briefs.values())


def test_chatty_roster_names_the_run(repo, capsys):
    _, out, _ = _emit(repo, capsys, "--ask", "x", "--chatty")
    reply = json.loads(out)
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert reply["run_id"] in script


def test_without_chatty_no_mailbox_and_no_chatty_args(repo, capsys):
    rc, out, _ = _emit(repo, capsys, "--ask", "x")
    assert rc == 0
    reply = json.loads(out)
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert "em_mailbox" not in reply
    assert '"chatty": true' not in script
    assert '"chattyReview"' not in script
