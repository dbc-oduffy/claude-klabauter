"""Research emit warns, advisory only, when another research run is live on the shared WebSearch budget."""
from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing, repo  # noqa: F401


def _emit(repo, capsys):
    rel = _write_sizing(repo, "--research-class", "corpus", "--research-source", "web")
    capsys.readouterr()
    code = cli_module.main(["--from-sizing", rel])
    return code, json.loads(capsys.readouterr().out)


def _launch(repo, name, *, halted=False):
    run = repo / "scratch" / "warp" / name
    run.mkdir(parents=True)
    (run / re_.SUBMISSION_FILE).write_text("{}", encoding="utf-8")
    if halted:
        (run / re_.HALTED_FILE).write_text("{}", encoding="utf-8")


def test_a_live_sibling_run_puts_the_shared_budget_warning_in_the_reply(repo, capsys):
    _launch(repo, "pipeline-research-20260101T000000-abc123")
    code, reply = _emit(repo, capsys)
    assert code == cli_module.EXIT_OK and reply["ok"]
    warning = reply["websearch_budget_warning"]
    assert str(re_.WEBSEARCH_BUDGET) in warning
    assert "pipeline-research-20260101T000000-abc123" in warning


def test_no_live_sibling_adds_nothing(repo, capsys):
    _launch(repo, "pipeline-research-20260101T000000-abc123", halted=True)
    _launch(repo, "pipeline-other-20260101T000000-def456")
    code, reply = _emit(repo, capsys)
    assert code == cli_module.EXIT_OK and reply["ok"]
    assert "websearch_budget_warning" not in reply


def test_a_submission_older_than_the_window_is_not_live(tmp_path):
    _launch(tmp_path, "pipeline-research-old-abc123")
    now = (tmp_path / "scratch/warp/pipeline-research-old-abc123" / re_.SUBMISSION_FILE).stat().st_mtime
    args = (tmp_path, "scratch/warp", "pipeline-research-", "own")
    assert re_.live_research_runs(*args, now) == ["pipeline-research-old-abc123"]
    assert re_.live_research_runs(*args, now + re_.LIVE_RUN_WINDOW_SECONDS + 1) == []
