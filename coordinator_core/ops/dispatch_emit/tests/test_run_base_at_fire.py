"""The emit route's run base is the emit-time sha literal and composes no agent."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import compose_script

from .conftest import REVIEW_KW
from .test_emit import _wave_row

EMIT = "a" * 40


def _compose(**kw):
    return compose_script(
        [[_wave_row("C1", ["a.py"])]],
        name="wf",
        description="d",
        plan_path="docs/plans/p.md",
        expected_branch="work/x",
        **REVIEW_KW,
        **kw,
    )


def test_emit_route_script_composes_no_run_base_agent():
    script = _compose(run_base_sha=EMIT)

    assert "label: 'run-base'" not in script
    assert "Run base. Run exactly" not in script
    assert "Run base sha (observed at fire)" not in script


def test_run_base_is_the_emit_sha_literal_and_the_missing_sha_throw_stays():
    script = _compose(run_base_sha=EMIT)

    assert f"let _runBase = '{EMIT}';" in script
    assert "if (!_runBase) throw new Error('run base sha unreadable and none known at emit" in script
    assert "Run base sha (emit-time): ' + _runBase" in script


def test_checkpoint_trailer_still_reads_the_script_scope_base():
    script = _compose(run_base_sha=EMIT)

    assert "Checkpoint-Base: ' + _runBase" in script


def test_explicit_review_only_base_wins_and_is_never_re_read():
    script = compose_script(
        [[_wave_row("C1", ["a.py"])]],
        name="wf",
        description="d",
        run_base_sha=EMIT,
        review_only=True,
        review_terminal_sha="b" * 40,
        **REVIEW_KW,
    )

    assert "run-base" not in script
    assert "_runBase" not in script
    assert f"base {EMIT}" in script


def test_review_prompts_and_digest_read_the_script_scope_base_but_the_slice_id_keeps_emit_identity():
    script = _compose(run_base_sha=EMIT)

    assert "run_base_sha: ' + _runBase" in script
    assert "run_base_sha: _runBase," in script
    assert f"run_base_sha: {EMIT}" not in script
    # Identity, not content: the frozen-diff slice id stays on the emit-time sha.
    assert f"{EMIT[:12]}-prep" in script
    assert f"git diff --name-only {EMIT}" not in script
