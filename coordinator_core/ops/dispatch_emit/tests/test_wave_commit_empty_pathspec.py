"""The emitted `_commitWave` dispatches no committer leg when the wave's path union is empty,
and passes a non-empty union verbatim, in both the plain and the seam variants."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import SeamInputs, compose_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_emit_dag import _write_row
from coordinator_core.ops.dispatch_emit.tests.test_seam_wave_boundary import _two_plan_waves

_GUARD = "if (!done.length || !paths.length)"


def _commit_wave(script: str) -> str:
    return script.split("async function _commitWave", 1)[1].split("\n  }\n", 1)[0]


def _plain() -> str:
    return compose_script(
        [[_write_row("C1"), _write_row("C2")]],
        name="wf",
        description="d",
        session_id="9d860631-c114-462a-a6ee-f685cc671ced",
        expected_branch="work/run",
        **REVIEW_KW,
    )


def _seam() -> str:
    return compose_script(
        _two_plan_waves(),
        name="n",
        description="d",
        plan_path="state/mise-inventory/x.spine.md",
        expected_branch="work/x",
        run_base_sha="a" * 40,
        seam=SeamInputs({}, False),
        **REVIEW_KW,
    )


def _assert_guard_precedes_leg(body: str) -> None:
    assert "const paths = [...new Set([..._carried, ...done].flatMap((i) => _landed[i].paths))];" in body
    assert body.index("const paths =") < body.index("!paths.length") < body.index("await agent(")


def test_plain_wave_returns_before_the_leg_on_an_empty_union():
    body = _commit_wave(_plain())
    _assert_guard_precedes_leg(body)
    assert "    if (!paths.length) return;" in body


def test_seam_wave_treats_an_empty_union_like_a_no_landed_wave():
    body = _commit_wave(_seam())
    _assert_guard_precedes_leg(body)
    guard_block = body.split(_GUARD, 1)[1].split("return;", 1)[0]
    assert "_seamHeldPlans[n]" in guard_block
    assert "no row landed to commit" in guard_block


def test_non_empty_union_still_reaches_the_committer_verbatim():
    for body in (_commit_wave(_plain()), _commit_wave(_seam())):
        assert "+ paths.join(', ') +" in body
        assert "label: 'commit:wave-' + n" in body
        assert body.index("!paths.length") < body.index("+ paths.join(', ') +")
