"""CLI parse level: repeatable --ask, --target binding, --split/--combine map to dispatch.emit params."""
from __future__ import annotations

import pytest

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit import cli as cli_module


@pytest.fixture
def captured(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    seen: list[dict] = []

    def fake(params, **_kw):
        seen.append(params)
        return {"ok": True, "verdict": "OK", "script_path": "x.js"}

    monkeypatch.setattr(cli_module, "_dispatch_emit", fake)
    return seen


def _run(*argv):
    return cli_module.main(["--research", *argv])


def test_two_asks_with_targets_and_combine(captured):
    code = _run("--ask", "Q1", "--target", "repo=src", "--ask", "Q2", "--ask", "Q3", "--target", "local=uname -a", "--combine")
    assert code == 0
    p = captured[0]
    assert p["ask"] == "Q1"
    assert p[rc.SCOUT_QUESTIONS_PARAM] == [
        {"text": "Q1", "target": "repo", "ref": "src"},
        {"text": "Q2", "target": "web", "ref": ""},
        {"text": "Q3", "target": "local", "ref": "uname -a"},
    ]
    assert p[rc.SCOUT_MODE_PARAM] == "combine"


def test_split_mode_sent(captured):
    assert _run("--ask", "Q1", "--ask", "Q2", "--split") == 0
    assert captured[0][rc.SCOUT_MODE_PARAM] == "split"


def test_single_ask_with_target_sends_one_item(captured):
    assert _run("--ask", "Q1", "--target", "web") == 0
    assert captured[0][rc.SCOUT_QUESTIONS_PARAM] == [{"text": "Q1", "target": "web", "ref": ""}]
    assert rc.SCOUT_MODE_PARAM not in captured[0]


def test_plain_ask_unchanged(captured):
    assert _run("--ask", "Q1") == 0
    assert captured[0]["ask"] == "Q1"
    assert rc.SCOUT_QUESTIONS_PARAM not in captured[0]
    assert rc.SCOUT_MODE_PARAM not in captured[0]


@pytest.mark.parametrize(
    "argv",
    [
        ["--ask", "Q1", "--ask"],
        ["--target", "web", "--ask", "Q1"],
        ["--ask", "Q1", "--target", "web", "--target", "repo=x"],
        ["--ask", "Q1", "--target", "ftp"],
        ["--ask", "Q1", "--target", "repo="],
        ["--ask", "Q1", "--target", "web=x"],
        ["--ask", "Q1", "--ask", "Q2", "--list", "questions=a,b"],
        ["--ask", "Q1", "--target", "web", "--list", "questions=a"],
    ],
)
def test_usage_refusals(captured, capsys, argv):
    assert _run(*argv) == cli_module.EXIT_USAGE
    assert "ERROR" in capsys.readouterr().err
    assert captured == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--ask", "Q1", "--ask", "Q2"],
        ["--ask", "Q1", "--target", "web"],
        ["--ask", "Q1", "--combine"],
        ["--ask", "Q1", "--split"],
    ],
)
def test_refused_without_research(captured, capsys, argv):
    assert cli_module.main(argv) == cli_module.EXIT_USAGE
    assert "requires --research" in capsys.readouterr().err
    assert captured == []


def test_split_and_combine_are_exclusive(captured):
    with pytest.raises(SystemExit):
        _run("--ask", "Q1", "--split", "--combine")
