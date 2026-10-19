"""An unmarked raw ask runs relayed with no turn-log read; an explicit pm marking is verified against the log."""

from __future__ import annotations

import pytest

from coordinator_core.ops import baton_pm_turns as pm_turns
from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_TEXT_FILE, RELAYED_ASK_FILE, record_dir

SID = "11111111-2222-3333-4444-555555555555"
TURN = "please make the widget faster and keep the old behaviour intact"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


def _git_skeleton(root):
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git_skeleton(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _seed(root, text=TURN):
    pm_turns.log_path(SID, str(root)).parent.mkdir(parents=True, exist_ok=True)
    assert pm_turns.append_turn(text, session_id=SID, cwd=str(root))["ok"]


def _emit(repo, **params):
    out = op._dispatch_emit({**params}, repo_root=repo)
    records = record_dir(repo, out["run_id"])
    return out, records


def test_unmarked_ask_with_no_recorded_turn_runs_relayed(repo):
    out, records = _emit(repo, ask="add a thing")
    assert out["ask_source"] == "relayed" and "pm_verification" not in out
    assert (records / RELAYED_ASK_FILE).is_file() and not (records / ASK_TEXT_FILE).exists()


def test_unmarked_ask_equal_to_a_recorded_turn_runs_relayed(repo):
    _seed(repo)
    out, records = _emit(repo, ask=TURN)
    assert out["ask_source"] == "relayed" and "pm_verification" not in out
    assert (records / RELAYED_ASK_FILE).is_file() and not (records / ASK_TEXT_FILE).exists()


def test_unmarked_ask_reads_no_turn_log(repo, monkeypatch):
    _seed(repo)

    def boom(*_a, **_k):
        raise AssertionError("turn log read for an unmarked ask")

    monkeypatch.setattr(pm_turns, "_load", boom)
    monkeypatch.setattr(pm_turns, "log_path", boom)
    out, _ = _emit(repo, ask=TURN)
    assert out["ask_source"] == "relayed"


def test_pm_marked_ask_matching_a_turn_runs_pm_matched(repo):
    _seed(repo)
    out, records = _emit(repo, ask=TURN, ask_source="pm")
    assert out["ask_source"] == "pm" and out["pm_verification"] == "matched"
    assert (records / ASK_TEXT_FILE).is_file() and not (records / RELAYED_ASK_FILE).exists()


def test_pm_marked_ask_quoting_no_turn_is_refused_before_any_run_dir(repo):
    _seed(repo)
    with pytest.raises(ValueError, match="quotes no recorded PM turn"):
        _emit(repo, ask="unrecorded words that nobody said", ask_source="pm")
    assert not list(repo.rglob("ask-*"))


def test_pm_marked_ask_with_no_log_runs_pm_unverifiable(repo):
    out, _ = _emit(repo, ask="unrecorded words", ask_source="pm")
    assert out["ask_source"] == "pm" and out["pm_verification"] == "unverifiable"


def test_session_root_locates_the_turn_log_of_another_repo(repo, tmp_path_factory):
    other = tmp_path_factory.mktemp("target")
    _git_skeleton(other)
    _seed(repo)
    out, _ = _emit(other, ask=TURN, ask_source="pm", session_root=str(repo))
    assert out["pm_verification"] == "matched"
    out, _ = _emit(other, ask=TURN, ask_source="pm")
    assert out["pm_verification"] == "unverifiable"


def test_relayed_is_honoured_whatever_the_log_holds(repo):
    _seed(repo)
    out, _ = _emit(repo, ask=TURN, ask_source="relayed")
    assert out["ask_source"] == "relayed" and "pm_verification" not in out


def test_relayed_beside_a_sizing_path_is_still_refused(repo):
    with pytest.raises(ValueError, match="relayed"):
        op._dispatch_emit(
            {"ask": True, "sizing_path": "state/sizings/x.yaml", "ask_source": "relayed"}, repo_root=repo
        )
