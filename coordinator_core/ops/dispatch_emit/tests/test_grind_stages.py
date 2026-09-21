"""
coordinator_core.ops.dispatch_emit.tests.test_grind_stages

Purpose: pins C5's three named assertions (plan
docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md, Tasks § C5) over
`grind_stages.py`'s composers:

  1. Only the commit composers (`compose_commit_call`,
     `compose_commit_ledger_only_call`) mention `grind-row settle` or
     staging -- every other composer's call text stays clear of both.
  2. The commit prompt (`compose_commit_call`) names `--declared-revert`
     for every removed path it is handed.
  3. The op-runner verify composer's `agentType` equals
     `grind_vocab.OP_RUNNER_AGENT_TYPE`.

Negative-spec: does not repeat the banned-token/determinism assertions
overengineering-reviewer #8 places at C7's golden/falsifier over the
COMPOSED script -- this module tests each composer in isolation only.
"""
from __future__ import annotations

from coordinator_core.contract.grind_vocab import OP_RUNNER_AGENT_TYPE
from coordinator_core.ops.dispatch_emit import grind_stages


def _all_non_commit_calls() -> dict:
    return {
        "triage": grind_stages.compose_triage_call(
            label="triage:b1",
            phase_title="Triage",
            run_dir="state/queue-grind/run1",
            batch_id="b1",
            triage_depth="standard",
        ),
        "refute-close": grind_stages.compose_refute_close_call(
            label="refute-close:b1",
            phase_title="Refute-close",
        ),
        "fix": grind_stages.compose_fix_call(
            label="fix:row1",
            phase_title="Fix",
            row_id="row1",
            locked_files=["a.py"],
        ),
        "verify-agent": grind_stages.compose_verify_agent_call(
            label="verify:row1",
            phase_title="Verify",
        ),
        "verify-op": grind_stages.compose_verify_op_call(
            label="verify-op:b1",
            phase_title="Verify",
            op="lessons.verify_extraction",
            run_dir="state/queue-grind/run1",
            batch_id="b1",
        ),
        "undo": grind_stages.compose_undo_call(
            label="undo:row1",
            phase_title="Undo",
            touched_files=["a.py"],
            created_files=["b.py"],
        ),
    }


def test_only_commit_composers_mention_settle_or_staging():
    # "stage" alone collides with the stage-kind role noun every composer's
    # prompt uses ("you are the X stage") -- the forbidden tokens are the
    # STAGING-ACTION markers (the git-staging verb and the settle verb),
    # never the role noun.
    forbidden = ("grind-row settle", "stage exactly", "you stage")

    for kind, call_text in _all_non_commit_calls().items():
        lowered = call_text.lower()
        for token in forbidden:
            assert token not in lowered, (
                f"{kind} composer's call text unexpectedly mentions {token!r}"
            )
        assert "you do not stage" in lowered

    commit_call = grind_stages.compose_commit_call(
        label="commit:row1",
        phase_title="Commit",
        row_id="row1",
        touched_files=["a.py"],
        removed_files=["old.py"],
    )
    assert "grind-row settle" in commit_call
    assert "stage exactly" in commit_call.lower()

    ledger_only_call = grind_stages.compose_commit_ledger_only_call(
        label="commit:ledger",
        phase_title="Commit",
        profile="p1",
        unsettled_row_ids=["row1", "row2"],
    )
    assert "stage exactly" in ledger_only_call.lower()


def test_commit_prompt_names_declared_revert_per_removed_path():
    call_text = grind_stages.compose_commit_call(
        label="commit:row1",
        phase_title="Commit",
        row_id="row1",
        touched_files=["a.py"],
        removed_files=["old1.py", "old2.py"],
    )
    assert "--declared-revert" in call_text
    assert "old1.py" in call_text
    assert "old2.py" in call_text


def test_commit_prompt_omits_declared_revert_clause_with_no_removed_paths():
    call_text = grind_stages.compose_commit_call(
        label="commit:row1",
        phase_title="Commit",
        row_id="row1",
        touched_files=["a.py"],
        removed_files=[],
    )
    assert "--declared-revert" not in call_text


def test_op_runner_agent_type_equals_vocab_constant():
    call_text = grind_stages.compose_verify_op_call(
        label="verify-op:b1",
        phase_title="Verify",
        op="lessons.verify_extraction",
        run_dir="state/queue-grind/run1",
        batch_id="b1",
    )
    assert f"agentType: '{OP_RUNNER_AGENT_TYPE}'" in call_text


def test_every_composer_writes_literal_sonnet_model():
    calls = list(_all_non_commit_calls().values())
    calls.append(
        grind_stages.compose_commit_call(
            label="commit:row1",
            phase_title="Commit",
            row_id="row1",
            touched_files=["a.py"],
        )
    )
    calls.append(
        grind_stages.compose_commit_ledger_only_call(
            label="commit:ledger",
            phase_title="Commit",
            profile="p1",
            unsettled_row_ids=["row1"],
        )
    )
    for call_text in calls:
        assert "model: 'sonnet'" in call_text


def test_fix_prompt_precheck_peer_dirty_before_work():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1",
        phase_title="Fix",
        row_id="row1",
        locked_files=["a.py", "b.py"],
    )
    assert "PEER_DIRTY" in call_text
    peer_dirty_index = call_text.index("PEER_DIRTY")
    fix_index = call_text.lower().index("fix the row")
    assert peer_dirty_index < fix_index


def test_commit_reconciles_before_retry():
    call_text = grind_stages.compose_commit_call(
        label="commit:row1",
        phase_title="Commit",
        row_id="row1",
        touched_files=["a.py"],
    )
    assert "git log" in call_text
    assert "git status" in call_text
    assert "retry blind" in call_text
