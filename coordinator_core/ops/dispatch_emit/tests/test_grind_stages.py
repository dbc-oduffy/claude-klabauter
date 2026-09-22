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


# ---------------------------------------------------------------------------
# Runtime interpolation (`*_js` params) -- a static file/row-id list is a
# break-class defect for fix/commit/undo/ledger-only-commit, since the real
# touched/declared/unsettled set is only known at RUN time (EM follow-up on
# C7's redo).
# ---------------------------------------------------------------------------


def test_fix_prompt_interpolates_locked_files_js_expression():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1",
        phase_title="Fix",
        row_id="row1",
        locked_files_js="row.declaredFiles",
    )
    assert "(row.declaredFiles).join(', ')" in call_text
    assert "'a.py'" not in call_text  # no static list baked in


def test_fix_static_locked_files_still_works():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"],
    )
    assert "a.py" in call_text
    assert ".join(" not in call_text


def test_commit_prompt_interpolates_touched_and_removed_js_expressions():
    call_text = grind_stages.compose_commit_call(
        label="commit:row1",
        phase_title="Commit",
        row_id="row1",
        touched_files_js="fixResult.touched_files",
        removed_files_js="closedPaths",
    )
    assert "(fixResult.touched_files).join(', ')" in call_text
    assert "(closedPaths).join(', ')" in call_text
    assert "--declared-revert" in call_text


def test_undo_prompt_interpolates_touched_and_created_js_expressions():
    call_text = grind_stages.compose_undo_call(
        label="undo:row1",
        phase_title="Undo",
        touched_files_js="fixResult.touched_files",
        created_files_js="fixResult.extra_files",
    )
    assert "(fixResult.touched_files).join(', ')" in call_text
    assert "(fixResult.extra_files).join(', ')" in call_text


def test_ledger_only_commit_interpolates_unsettled_rows_and_run_id():
    call_text = grind_stages.compose_commit_ledger_only_call(
        label="commit:ledger",
        phase_title="Commit",
        profile="p1",
        unsettled_row_ids_js="unsettled",
        is_drain=True,
        run_id_js="RUN_ID",
    )
    assert "(unsettled).join(', ')" in call_text
    assert "(RUN_ID)" in call_text
    assert "runs/" in call_text and ".json in this same commit." in call_text


def test_fix_schema_carries_touched_files():
    call_text = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"],
    )
    assert '"touched_files"' in call_text


_REPO = __import__("pathlib").Path(__file__).resolve().parents[4]
_GOLDEN = _REPO / "coordinator_core/ops/dispatch_emit/tests/fixtures/grind-fixture.golden.mjs"
_NEW_MODULES = [
    "coordinator_core/contract/grind_vocab.py",
    "coordinator_core/ops/dispatch_emit/queue_select.py",
    "coordinator_core/ops/dispatch_emit/grind_profile.py",
    "coordinator_core/ops/dispatch_emit/grind_stages.py",
    "coordinator_core/ops/dispatch_emit/grind_compose.py",
    "coordinator_core/ops/dispatch_emit/queue_emit.py",
    "coordinator_core/backlog_grind_assemble/grind_rows.py",
    "coordinator_core/ops/grind_ops.py",
]


def test_emitted_prompts_carry_no_posix_only_construct():
    """multi-os-first-class: agents run on Bash or PowerShell, so no prompt
    may lean on a POSIX-only construct."""
    import re
    text = _GOLDEN.read_text(encoding="utf-8")
    for construct in ("/dev/null", "| grep", "sleep "):
        assert construct not in text, construct
    assert not re.search(r"(?<![\w-])[A-Z][A-Z0-9_]*=\S+ [a-z]", text), "VAR=value cmd"


def test_no_machine_local_path_in_new_modules_or_golden():
    """no-single-machine-assumptions: the queue path is cloud-launchable, so
    neither the engine modules nor the emitted script name a host path."""
    import re
    banned = re.compile(r"/Users/|/home/|\b[A-Za-z]:\\|~/\.claude/projects|/private/tmp|gettempdir|machine_local")
    for rel in _NEW_MODULES + [str(_GOLDEN.relative_to(_REPO))]:
        text = (_REPO / rel).read_text(encoding="utf-8")
        hit = banned.search(text)
        assert hit is None, f"{rel}: {hit.group(0) if hit else ''}"


def test_every_grind_row_close_in_golden_carries_every_required_flag():
    """A close invocation missing a flag is one the agent cannot run; the fixer's
    once said only "run `grind-row close`"."""
    import re
    text = _GOLDEN.read_text(encoding="utf-8")
    closes = re.findall(r"grind-row close --[^`]*", text)
    assert len(closes) >= 2
    required = ("--profile-dir", "--profile ", "--row ", "--digest", "--verdict", "--evidence-file",
                "--closed-by", "--run-stamp", "--repo-root")
    for invocation in closes:
        for flag in required:
            assert flag in invocation, (flag, invocation[:120])
    assert "PROFILE_DIR = args.profile_dir" in text


def test_fix_and_triage_schemas_gate_tradeoff_on_a_boolean():
    """Live-run defect 6: a non-empty `tradeoff` string ("None", "No
    tradeoff") routed to needs-judgment. Both schemas now require a
    `has_tradeoff` boolean and the prompt gates `tradeoff` on it."""
    fix_call = grind_stages.compose_fix_call(
        label="fix:row1", phase_title="Fix", row_id="row1", locked_files=["a.py"],
    )
    assert "has_tradeoff" in fix_call
    assert "has_tradeoff" in fix_call.lower()

    triage_call = grind_stages.compose_triage_call(
        label="triage:b1", phase_title="Triage", run_dir="state/queue-grind/run1",
        batch_id="b1", triage_depth="standard", verdicts=["confirmed-bug", "not-reproduced"],
    )
    assert "has_tradeoff" in triage_call
    assert '"enum": ["confirmed-bug", "not-reproduced"]' in triage_call


def test_commit_prompts_carry_an_explicit_subject_and_body():
    """Live-run defect 1: `coordinator:git-commit-agent` refuses a brief with
    no subject. Every commit prompt now composes one at runtime."""
    commit_call = grind_stages.compose_commit_call(
        label="commit:row1", phase_title="Commit", profile="p1", row_id="row1",
        touched_files=["a.py"],
    )
    assert "Use commit subject `grind(p1): " in commit_call
    assert "commit body naming this row" in commit_call

    batch_call = grind_stages.compose_commit_ledger_only_call(
        label="commit:ledger", phase_title="Commit", profile="p1",
        unsettled_row_ids=["row1", "row2"], run_id="run-1",
    )
    assert "Use commit subject `grind(p1): ledger for " in batch_call
    assert "commit body naming these rows" in batch_call
    assert "skip it, report which one" in batch_call

    drain_call = grind_stages.compose_commit_ledger_only_call(
        label="commit:drain", phase_title="Commit", profile="p1",
        unsettled_row_ids=["row1"], run_id="run-1", is_drain=True,
    )
    assert "Use commit subject `grind(p1): drain run " in drain_call
    assert "run-1" in drain_call


def test_grind_row_verb_always_run_through_backlog_grind_assemble():
    """Every composer's PROMPT text names the installed entrypoint
    (`backlog-grind-assemble grind-row <verb>`) -- a bare `grind-row` is not
    on PATH and exits 127 (live-run defect 8)."""
    import re

    calls = dict(_all_non_commit_calls())
    calls["commit"] = grind_stages.compose_commit_call(
        label="commit:row1", phase_title="Commit", profile="p1", row_id="row1",
        touched_files=["a.py"],
    )
    calls["commit-ledger"] = grind_stages.compose_commit_ledger_only_call(
        label="commit:ledger", phase_title="Commit", profile="p1",
        unsettled_row_ids=["row1"], run_id="run-1",
    )
    for kind, call_text in calls.items():
        for m in re.finditer(r"grind-row", call_text):
            prefix = call_text[max(0, m.start() - len("backlog-grind-assemble ")):m.start()]
            assert prefix.endswith("backlog-grind-assemble "), (
                f"{kind}: bare `grind-row` not preceded by `backlog-grind-assemble `: "
                f"...{call_text[max(0, m.start()-40):m.start()+20]!r}..."
            )
