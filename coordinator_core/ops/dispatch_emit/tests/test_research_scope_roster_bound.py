"""A notebooklm research emit states its notebooks roster to the scope stage as a hard constraint."""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import (  # noqa: F401
    _run,
    _write_sizing,
    repo,
)

_TWO = (
    "--research-class", "corpus", "--research-appetite", "medium", "--research-source", "notebooklm",
    "--research-notebooklm-need", "youtube", "--research-target", "notebooklm=A", "--research-target", "notebooklm=B",
)


def _emit(repo, capsys, *flags):
    rel = _write_sizing(repo, *flags)
    capsys.readouterr()
    code, reply = _run(capsys, ["--from-sizing", rel])
    assert reply and reply["ok"], reply
    script = Path(reply["path"]).read_text(encoding="utf-8")
    return rel, script, reply


def _brief_text(repo, script):
    paths = re.findall(r"[\w./-]*brief\.md", script)
    assert paths, "no members brief named in the emitted script"
    return (repo / paths[0]).read_text(encoding="utf-8")


def test_two_notebook_roster_is_stated_in_the_members_brief(repo, capsys):
    _, script, _ = _emit(repo, capsys, *_TWO)
    brief = _brief_text(repo, script)
    assert "## Notebooks roster" in brief
    assert "2 entries: `A`, `B`" in brief
    assert "`worker_count` = 2" in brief and "`notebooks` listing exactly" in brief


def test_halt_guard_trips_on_a_one_scope_reply_and_passes_a_two_scope_reply(repo, capsys):
    _, script, _ = _emit(repo, capsys, *_TWO)
    assert "disagrees with the 2-entry notebooks roster (" in script
    lines = script.splitlines()
    at = next(i for i, ln in enumerate(lines) if "disagrees with the" in ln)
    condition = next(ln for ln in reversed(lines[:at]) if "!== 2" in ln)
    assert "['worker_count']" in condition and "['notebooks']" in condition
    assert condition.count("!== 2") == 2, condition
    assert "!== 1" not in condition


def test_no_notebooklm_leaves_the_brief_unchanged(tmp_path):
    (tmp_path / "scratch/run-1").mkdir(parents=True)
    ask = tmp_path / "scratch/run-1/ask.md"
    ask.write_text("ask\n", encoding="utf-8")
    assert re_.bind_notebooks_roster(tmp_path, "scratch/run-1", "scratch/run-1/ask.md", ()) == "scratch/run-1/ask.md"
    assert ask.read_text(encoding="utf-8") == "ask\n"


def test_roster_precedence_given_then_targets_then_question_defaults():
    targets = [{"source": "notebooklm", "ref": "A"}, {"source": "web", "ref": "x"}, {"source": "notebooklm", "ref": "B"}]
    assert re_.notebooks_roster(targets, ["Z"], ["q"]) == ("Z",)
    assert re_.notebooks_roster(targets, [], ["q"]) == ("A", "B")
    assert re_.notebooks_roster([], [], ["q1", "q2"]) == ("A", "B")


def test_a_sizing_brief_is_wrapped_never_edited(tmp_path):
    sizing = tmp_path / "state/sizings/s.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("x: 1\n", encoding="utf-8")
    rel = re_.bind_notebooks_roster(tmp_path, "scratch/run-1", "state/sizings/s.yaml", ("A", "B"))
    assert rel == "scratch/run-1/brief.md"
    assert sizing.read_text(encoding="utf-8") == "x: 1\n"
    assert "`state/sizings/s.yaml`" in (tmp_path / rel).read_text(encoding="utf-8")
