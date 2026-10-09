"""The run base is HEAD when the script fires, not when it was emitted: parts of an
over-cap inventory are emitted together and fired in sequence."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import _run_base_blocks, compose_script

from .conftest import REVIEW_KW
from .test_emit import _wave_row

# _run_block executes the emitted block under a real node process; needs a real process.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

EMIT = "a" * 40
FIRE = "b" * 40


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


def _run_block(agent_js: str) -> str:
    """Execute the base-resolution block against a stubbed ``agent``; return ``_runBase``."""
    body = "\n".join(_run_base_blocks(EMIT, None))
    src = (
        f"const log = () => {{}}; const agent = {agent_js};\n"
        f"(async () => {{\n{body}\nconsole.log(_runBase);\n}})();"
    )
    return subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout.strip()


def test_fire_time_part_script_carries_no_emit_time_base_in_its_checkpoint_or_seam_ranges():
    script = _compose(run_base_sha=EMIT)

    assert "Checkpoint-Base: ' + _runBase;" in script
    assert f"Checkpoint-Base: {EMIT}" not in script
    assert f"'{EMIT}..'" not in script


def test_base_is_resolved_before_any_other_agent_call():
    script = _compose(run_base_sha=EMIT)

    # The run-base call is the first `await agent(` in the script.
    assert script.count("await agent(", 0, script.index("label: 'run-base'")) == 1
    assert "Run base sha (observed at fire)" in script


def test_resume_replays_the_same_call():
    # The runtime caches the longest unchanged prefix of agent() calls: an identical
    # prompt at the identical position is what makes a resumed run keep its first base.
    assert _compose(run_base_sha=EMIT).count("label: 'run-base'") == 1
    assert _run_base_blocks(EMIT, "/r") == _run_base_blocks(EMIT, "/r")


def test_explicit_review_only_base_wins_and_is_never_re_read():
    script = compose_script(
        [[_wave_row("C1", ["a.py"])]],
        name="wf",
        description="d",
        run_base_sha=EMIT,
        review_only=True,
        **REVIEW_KW,
    )

    assert "run-base" not in script
    assert "_runBase" not in script
    assert f"base {EMIT}" in script


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_base_is_the_head_read_at_fire_time():
    assert _run_block(f"async () => ({{ sha: '{FIRE}\\n' }})") == FIRE


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
@pytest.mark.parametrize("stub", ["async () => null", "async () => ({ sha: 'nope' })", "async () => { throw new Error('x'); }"])
def test_unreadable_head_falls_back_to_the_emit_time_sha(stub):
    assert _run_block(stub) == EMIT


def test_review_prompts_and_digest_read_the_fire_time_base_but_the_slice_id_keeps_emit_identity():
    script = _compose(run_base_sha=EMIT)

    assert "run_base_sha: ' + _runBase" in script
    assert "run_base_sha: _runBase," in script
    assert f"run_base_sha: {EMIT}" not in script
    # Identity, not content: the frozen-diff slice id stays on the emit-time sha.
    assert f"{EMIT[:12]}-prep" in script
    assert f"git diff --name-only {EMIT}" not in script
