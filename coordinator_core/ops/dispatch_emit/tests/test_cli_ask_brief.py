"""CLI ask route: --brief/--brief-file/--context reach compose_ask_script and the receipt."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, cli

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_REAL = ask_compose.compose_ask_script


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


@pytest.fixture
def seen(monkeypatch):
    got: list = []

    def spy(**kwargs):
        got.append(kwargs.get("em_brief"))
        if "em_brief" not in inspect.signature(_REAL).parameters:
            kwargs.pop("em_brief", None)
        return _REAL(**kwargs)

    monkeypatch.setattr(ask_compose, "compose_ask_script", spy)
    return got


def _emit(repo, capsys, *argv):
    rc = cli.main([*argv, "--repo-root", str(repo)])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_inline_brief_emits_and_reaches_compose(repo, capsys, seen):
    rc, out, _ = _emit(repo, capsys, "--ask", "add a thing", "--brief", "be careful")
    assert rc == 0
    assert seen[0].text == "be careful" and seen[0].source == "inline"
    receipt = json.loads(Path(json.loads(out)["receipt"]).read_text(encoding="utf-8"))
    assert receipt["em_brief"]["sha256"] == hashlib.sha256(b"be careful").hexdigest()


def test_brief_file_and_context_reach_receipt(repo, capsys, seen):
    (repo / "b.md").write_text("from file", encoding="utf-8")
    (repo / "c.md").write_text("ctx", encoding="utf-8")
    rc, out, _ = _emit(
        repo, capsys, "--ask", "x", "--brief-file", "b.md", "--context", "c.md"
    )
    assert rc == 0
    receipt = json.loads(Path(json.loads(out)["receipt"]).read_text(encoding="utf-8"))
    assert receipt["em_brief"]["source"] == "b.md"
    assert receipt["em_brief"]["context"] == ["c.md"]


def test_no_brief_passes_none(repo, capsys, seen):
    rc, _, _ = _emit(repo, capsys, "--ask", "x")
    assert rc == 0 and seen == [None]


def test_brief_and_brief_file_is_usage(repo, capsys):
    rc, _, err = _emit(repo, capsys, "--ask", "x", "--brief", "t", "--brief-file", "f")
    assert rc == cli.EXIT_USAGE and "--brief-file" in err


def test_brief_file_without_ask_is_usage(repo, capsys):
    rc, _, err = _emit(repo, capsys, "--brief-file", "f", "--plan", "p.md")
    assert rc == cli.EXIT_USAGE and "--brief-file requires" in err


def test_missing_context_refuses_naming_path(repo, capsys):
    rc, _, err = _emit(repo, capsys, "--ask", "x", "--brief", "t", "--context", "nope.md")
    assert rc == cli.EXIT_DATA_ERROR and "nope.md" in err


def test_pipeline_brief_refusal_unchanged_off_ask(repo, capsys):
    rc, _, err = _emit(repo, capsys, "--brief", "x", "--plan", "p.md")
    assert rc == cli.EXIT_USAGE and "--brief require --pipeline" in err


def test_context_still_refused_off_ask_and_research(repo, capsys):
    rc, _, err = _emit(repo, capsys, "--context", "c.md", "--plan", "p.md")
    assert rc == cli.EXIT_USAGE and "--context requires --from-sizing or --research" in err


def test_a_missing_brief_file_refuses_before_the_admission_hold(repo, monkeypatch, capsys):
    from coordinator_core.ops.dispatch_emit import admission

    def _no_wait(*a, **k):
        raise AssertionError("admission reached before the argv refusal")

    monkeypatch.setattr(admission, "await_admission", _no_wait)
    assert cli.main(["--ask", "probe", "--brief-file", "absent.md"]) == cli.EXIT_DATA_ERROR
    assert "--brief-file absent.md" in capsys.readouterr().err
