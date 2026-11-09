"""A refire of a review_blocked halt after a `plan.tasks.mutate add-task` mints one stamp suffix, and the
launcher's abandon batch closes a minted-only run from its `_caller_cwd` envelope.

Reproduction through `op._dispatch_emit` (the refire-by-sizing entry) found no engine path that appends a
second stamp: the sole mint is `warp-<stem>-<stamp>`, and a rebound refire answers the halted id unchanged.
These tests pin that invariant on both the rebound and the diverted (dead emitter) arms.
"""

from __future__ import annotations

import re

from coordinator_core.ops import plan_tasks_mutate as ptm
from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_refire_resumes_halted_run import (  # noqa: F401
    OLDER,
    REL,
    _emit_by,
    _fire,
    repo,
)

PLAN_REL = "docs/plans/2026-10-16-refire-c3.md"
STEM = "2026-10-16-refire-c3"
ONE_STAMP = re.compile(rf"^warp-{STEM}-\d{{8}}T\d{{6}}-[0-9a-f]{{6}}$")


def _halt_review_blocked(repo):
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: TC1\n  disposition: open\n  writes:\n    - some/C1.py\n```\n",
        encoding="utf-8", newline="\n",
    )
    rl.start(repo, OLDER)
    rl.bind(repo, OLDER, REL, ask_text="the ask")
    write_halt_marker(record_dir(repo, OLDER), halted="review_blocked", sizing_rel=REL, plan_rel=PLAN_REL)


def _add_task_c4(repo):
    row = {
        "id": "C4", "title": "TC4", "disposition": "open", "surface": "some/C4.py",
        "change_kind": "code-edit", "writes": ["some/C4.py"], "queue_scope": "project",
    }
    reply = ptm._add_task(PLAN_REL, row, repo, repo)
    assert reply.get("exit_code") == 0, reply


def test_a_rebound_refire_after_add_task_answers_the_halted_id_unchanged(repo):
    _halt_review_blocked(repo)
    _add_task_c4(repo)
    assert _fire(repo)["run_id"] == OLDER


def test_a_refire_after_add_task_whose_halted_id_is_not_rebindable_mints_one_suffix(repo, monkeypatch):
    _halt_review_blocked(repo)
    _emit_by(repo, "dead-peer", False, monkeypatch)
    _add_task_c4(repo)
    reply = _fire(repo)
    assert reply["run_id"] != OLDER
    assert ONE_STAMP.fullmatch(reply["run_id"]), reply["run_id"]
    assert reply["output_path_redirect"]["to"].endswith(f"{reply['run_id']}.workflow.mjs")


def test_a_second_refire_after_add_task_still_mints_one_suffix(repo):
    _halt_review_blocked(repo)
    _add_task_c4(repo)
    first = _fire(repo)["run_id"]
    second = _fire(repo)["run_id"]
    assert first == OLDER and second != first
    assert ONE_STAMP.fullmatch(second), second


def _shell_run(repo, run_id):
    rl.start(repo, run_id)
    return run_id


def test_the_launcher_abandon_envelope_closes_a_minted_only_run_without_a_request_root(repo):
    run = _shell_run(repo, "warp-2026-10-16-refire-c3-20260102T000000-bbbbbb")
    reply = op._dispatch_emit({"_caller_cwd": str(repo), "emits": [{"abandon_run": run}]}, None)
    assert reply["replies"][0].get("error") is None
    assert reply["replies"][0]["abandoned"] is True


def test_an_abandon_batch_with_no_root_and_no_caller_cwd_still_refuses(repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = _shell_run(repo, "warp-2026-10-16-refire-c3-20260102T000000-bbbbbb")
    reply = op._dispatch_emit({"emits": [{"abandon_run": run}]}, None)
    assert "requires repo_root, target_root or a caller cwd" in reply["replies"][0]["error"]


def test_a_sizing_stem_already_carrying_a_run_stamp_mints_one_suffix():
    assert op._unstamped_stem("state/sizings/2026-10-16-refire-c3.yaml") == STEM
    assert op._unstamped_stem(f"state/sizings/{OLDER}.yaml") == STEM
    assert op._unstamped_stem("state/sizings/2026-10-16-refire-c3-20260101T000000-aaaaaa.yaml") == STEM
