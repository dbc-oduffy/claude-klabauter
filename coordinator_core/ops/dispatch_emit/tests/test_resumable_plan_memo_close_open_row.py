"""An open inbound-memo-close row the halted run never staged still resumes the ledger halt."""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261023T031504-dd822d"
SPINE = f"state/warp/{RUN}/gate.spine.md"
MEMO = "state/cross-repo/inbox/2026-10-23-some-memo.md"


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _fixture(repo, last_row):
    """C1 checkpoint-landed, `last_row` open and unstaged, then a ledger-check halt staging C1 only."""
    _put(repo, tshirt="XS", route="dispatch")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    d = repo / "state" / "warp" / RUN
    d.mkdir(parents=True)
    body = "- id: C1\n  title: T1\n  disposition: open\n  writes:\n    - some/c1.py\n" + last_row
    (d / "gate.spine.md").write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-q", "-m", "checkpoint(wave 1): 1 rows — C1", "-m", f"Checkpoint-Plan: {SPINE}\nCheckpoint-Base: abc1234")
    record = {
        "halted": "ledger-check", "sizing_rel": REL, "plan": SPINE, "blocking": ["R1"],
        "blocking_items": [{"id": "R1", "remaining": ["still to do"], "evidence": "e"}],
    }
    (d / "halted.json").write_text(json.dumps(record), encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1"}], "owed": [], "gated": []}), encoding="utf-8")
    _git(repo, "add", "state")
    _git(repo, "commit", "-q", "-m", halted_record_message(RUN, {"halted": "ledger-check", "plan": SPINE}))


def _sizing(repo):
    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


def test_open_memo_close_row_resumes_the_ledger_and_stays_unlanded(repo):  # noqa: F811
    _fixture(repo, f"- id: C5\n  title: close\n  disposition: open\n  surface: {MEMO}\n  writes:\n    - {MEMO}\n")
    plan = resumable_plan(_sizing(repo), REL, repo, "xs")
    assert plan is not None and plan.incomplete == ()
    assert plan.ledger is not None and plan.ledger.run_id == RUN
    assert "C5" in plan.ledger.unlanded


def test_open_non_memo_row_never_staged_keeps_the_row_resume(repo):  # noqa: F811
    _fixture(repo, "- id: C5\n  title: work\n  disposition: open\n  writes:\n    - some/c5.py\n")
    plan = resumable_plan(_sizing(repo), REL, repo, "xs")
    assert plan is not None and plan.ledger is None and plan.incomplete == ("C5",)
