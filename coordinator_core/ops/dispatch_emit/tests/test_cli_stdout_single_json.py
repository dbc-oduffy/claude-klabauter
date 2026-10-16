"""emit-dispatch-workflow stdout is exactly one JSON document on the fire and hand-back paths."""
from __future__ import annotations

import json

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingHandBack


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    return tmp_path


def _plan(repo):
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Do a thing\n  writes: [\"a.txt\"]\n  body: |\n"
        "    Write a.txt.\n```\n",
        encoding="utf-8",
    )
    return plan


def test_plan_fire_prints_one_document_with_the_fire_record_nested(repo, monkeypatch, capsys):
    plan = _plan(repo)
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.fire.fire_workflow",
        lambda path, cwd=None: {"fire_id": "f1"},
    )
    out = repo / "p.workflow.mjs"
    assert cli.main(["--plan", str(plan), "--out", str(out), "--fire"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["fire"] == {"fire_id": "f1"}
    assert "Workflow(" not in captured.out
    assert "Workflow(" in captured.err


def test_plan_without_fire_has_no_fire_key(repo, capsys):
    plan = _plan(repo)
    assert cli.main(["--plan", str(plan), "--out", str(repo / "p.workflow.mjs")]) == cli.EXIT_OK
    assert "fire" not in json.loads(capsys.readouterr().out)


def test_sizing_hand_back_is_a_json_object_on_stdout_and_text_on_stderr(repo, monkeypatch, capsys):
    line = "sizing routed to the PM: S-L"

    def _raise(params, repo_root=None):
        raise SizingHandBack(line)

    monkeypatch.setattr(cli, "_dispatch_emit", _raise)
    assert cli.main(["--sizing", "state/sizings/x.yaml"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"hand_back": line}
    assert line in captured.err
