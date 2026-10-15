"""cli._print_workflow_invocation: the queue-route Workflow call carries a run_token that admits for its own run_stamp and script_path."""

from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit import cli, run_token

_STAMP_RE = re.compile(r"run_stamp: '([^']+)'")
_SCRIPT_RE = re.compile(r"script_path: (\"(?:[^\"\\]|\\.)*\")")
_TOKEN_RE = re.compile(r"run_token: '([^']+)'")


def _print(capsys, repo_root, script_path):
    cli._print_workflow_invocation(
        {"path": script_path}, is_queue_route=True, profile_dir="p", repo_root=repo_root
    )
    return capsys.readouterr().err


def test_printed_token_admits_for_the_printed_stamp_and_script(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    script = str(tmp_path / "scratch" / "grind with \"quote\".workflow.mjs")
    err = _print(capsys, tmp_path, script)

    stamp = _STAMP_RE.search(err).group(1)
    printed_script = json.loads(_SCRIPT_RE.search(err).group(1))
    token = _TOKEN_RE.search(err).group(1)

    assert printed_script == script
    assert run_token.TOKEN_RE.match(token)
    assert run_token.admits(tmp_path, stamp, token, printed_script)
    assert not run_token.admits(tmp_path, stamp, token, printed_script + "x")


def test_no_repo_root_prints_no_token(capsys):
    err = _print(capsys, None, "scratch/g.workflow.mjs")
    assert "run_stamp" in err
    assert "run_token" not in err


def test_no_git_dir_prints_no_token(tmp_path, capsys):
    err = _print(capsys, tmp_path, "scratch/g.workflow.mjs")
    assert "run_stamp" in err
    assert "run_token" not in err
