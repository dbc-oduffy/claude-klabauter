"""cli._print_workflow_invocation mints the queue-route run token with kind "grind"."""

from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit import cli, run_token

_STAMP_RE = re.compile(r"run_stamp: '([^']+)'")


def test_queue_route_token_record_is_kind_grind(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    cli._print_workflow_invocation(
        {"path": "scratch/g.workflow.mjs"}, is_queue_route=True, profile_dir="p", repo_root=tmp_path
    )
    err = capsys.readouterr().err
    stamp = _STAMP_RE.search(err).group(1)
    record = json.loads(run_token._record_path(tmp_path, stamp).read_text(encoding="utf-8"))
    assert record["kind"] == "grind"
    assert record["script_path"] == "scratch/g.workflow.mjs"


def test_default_mint_kind_is_ask(tmp_path):
    (tmp_path / ".git").mkdir()
    run_token.mint(tmp_path, "20260101T000000Z", "s.mjs")
    record = json.loads(run_token._record_path(tmp_path, "20260101T000000Z").read_text(encoding="utf-8"))
    assert record["kind"] == "ask"
