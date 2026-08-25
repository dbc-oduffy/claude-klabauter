"""
Round-trips the composer against a real shipped review-roster fragment.

Spec: dispatch brief C5, ``docs/plans/2026-08-19-review-mints-its-own-gated-
workflow.md``. Oracle for AC2 (stage order), AC3 (arity decides serial-vs-
parallel), AC4 (gate schema), AC7 (no ``model:`` on a reviewer call), AC9
(no commit/pytest stage) in one place, exercised against the actual v3
fragment DoE-claude ships -- not just the synthetic fixtures ``test_op.py``
and ``test_compose.py`` hand-roll.

``FIXTURES_DIR / "review-roster-fragment.json"`` is a ONE-TIME vendored copy
of ``coordinator/contract/review-roster-fragment.json``, resolved through
``coordinator_core.doe_root_pointer.read_doe_root_pointer()`` at copy time
(never a literal cross-repo path). Every test in this file reads that
vendored copy -- no test here touches the sibling clone at run time, and the
suite must pass with no sibling clone present at all (plan Anti-scope "do
not let the suite depend on a sibling clone existing").

Anti-scope: this vendored copy is a TEST FIXTURE only. Nothing outside this
file may treat it as a runtime source of truth for the fragment's shape --
``review_mint.op.load_fragment()`` is still the only runtime reader, and it
still resolves the sibling clone itself (see that module's docstring).

FIXTURE DRIFT: ``test_vendored_fixture_matches_the_shipped_fragment`` is the
one test that DOES resolve the sibling clone (again via
``read_doe_root_pointer()``, never a literal path) -- to catch this vendored
copy silently diverging from what DoE-claude actually ships (e.g. a new gate
agent the vendored copy doesn't know about). It skips cleanly, rather than
failing, when no sibling root resolves -- preserving the no-hard-dependency
rule the rest of this file honours.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.ops.review_mint.compose import compose
from coordinator_core.ops.review_mint.op import _make_gate_policy
from coordinator_core.ops.review_mint.roster import parse_stages

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_FRAGMENT_RELPATH = "coordinator/contract/review-roster-fragment.json"
_VENDORED_FRAGMENT_PATH = FIXTURES_DIR / "review-roster-fragment.json"

_PROMPT = "Review this plan before any task in it executes."
_PHASE_TITLE = "Review"

_TIERS = ["lightweight", "standard", "full"]


def _load_vendored_fragment() -> dict:
    return json.loads(_VENDORED_FRAGMENT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fragment() -> dict:
    return _load_vendored_fragment()


# A pinned nonce, not a generated one: these are shape oracles over the
# emitted text, so a fresh value per run would make the composed script
# differ run to run for no benefit. AC12's freshness BEHAVIOUR is exercised
# in test_compose.py/test_op.py, not here.
_ROUNDTRIP_NONCE = "0123456789abcdef"


def _compose_tier(fragment: dict, tier: str):
    stages = parse_stages(fragment, tier)
    blocking_verdicts = fragment.get("blocking_verdicts") or {}
    gate_policy = _make_gate_policy(blocking_verdicts, _ROUNDTRIP_NONCE)
    body_blocks = compose(stages, _PROMPT, _PHASE_TITLE, gate_policy)
    return stages, body_blocks


@pytest.mark.parametrize("tier", _TIERS)
def test_stage_order_matches_the_fragment(fragment, tier):
    stages, body_blocks = _compose_tier(fragment, tier)
    assert len(body_blocks) == len(stages)

    # Flatten the fragment's own per-stage agent order and the order agent
    # calls actually appear in the composed script -- they must match 1:1,
    # stage by stage (AC2).
    for stage, (_, block) in zip(stages, body_blocks):
        for agent in stage.agents:
            assert f"agentType: '{agent}'" in block, (
                f"tier {tier!r}: stage agents {stage.agents!r} not found "
                f"in its own composed block"
            )
        # Order within the stage's block matches stage.agents order.
        positions = [block.index(f"agentType: '{a}'") for a in stage.agents]
        assert positions == sorted(positions), (
            f"tier {tier!r}: agent call order in stage block disagrees "
            f"with fragment stage order {stage.agents!r}"
        )


@pytest.mark.parametrize("tier", _TIERS)
def test_arity_decides_serial_vs_parallel(fragment, tier):
    stages, body_blocks = _compose_tier(fragment, tier)
    saw_parallel_call_site = False
    for stage, (_, block) in zip(stages, body_blocks):
        if len(stage.agents) == 1:
            assert "await parallel([" not in block
            assert "await " in block
        else:
            assert "await parallel([" in block
            # Each item in the parallel array is arrow-wrapped -- the
            # composer's own "modeled as a callback" shape (compose.py's
            # `as_arrow=True`), never a bare call spliced into the array.
            assert "() => agent(" in block
            saw_parallel_call_site = True
    if any(len(s.agents) >= 2 for s in stages):
        assert saw_parallel_call_site, (
            f"tier {tier!r} fragment declares a multi-agent stage but no "
            "parallel call site was modeled"
        )


@pytest.mark.parametrize("tier", _TIERS)
def test_gate_stage_carries_schema_and_abort_branch(fragment, tier):
    stages, body_blocks = _compose_tier(fragment, tier)
    gate_stages = [(s, b) for s, (_, b) in zip(stages, body_blocks) if s.gate]
    if tier in ("standard", "full"):
        assert gate_stages, f"tier {tier!r} fixture must exercise at least one gate stage"
    for stage, block in gate_stages:
        assert "schema:" in block
        assert "blocking_agent" in block
        assert "if (" in block and ".verdict ===" in block


@pytest.mark.parametrize("tier", _TIERS)
def test_no_reviewer_call_carries_model_key(fragment, tier):
    _, body_blocks = _compose_tier(fragment, tier)
    full_script = "\n\n".join(block for _, block in body_blocks)
    assert "model:" not in full_script


@pytest.mark.parametrize("tier", _TIERS)
def test_no_commit_or_pytest_stage(fragment, tier):
    _, body_blocks = _compose_tier(fragment, tier)
    full_script = "\n\n".join(block for _, block in body_blocks)
    assert "git commit" not in full_script
    assert "pytest" not in full_script


# ---------------------------------------------------------------------------
# Fixture-drift oracle -- catches the vendored copy silently diverging from
# what DoE-claude actually ships. Skips cleanly when no sibling root
# resolves; never a hard failure or a hard dependency on the clone.
# ---------------------------------------------------------------------------


@pytest.mark.real_home
def test_vendored_fixture_matches_the_shipped_fragment():
    # Opts out of conftest's home quarantine deliberately: the quarantine nulls
    # the registry `read_doe_root_pointer()` reads, so under it this oracle
    # resolves a stub root, hits the is_file() rung, and skips on EVERY run --
    # a drift detector that never detects. Read-only parity check, which is
    # exactly what the marker is for.
    doe_root = read_doe_root_pointer()
    if not doe_root:
        pytest.skip("no DoE-claude sibling root resolved -- cannot check fixture drift")

    shipped_path = Path(doe_root) / _FRAGMENT_RELPATH
    if not shipped_path.is_file():
        pytest.skip(f"sibling root resolved but {shipped_path} is absent")

    shipped = json.loads(shipped_path.read_text(encoding="utf-8"))
    vendored = _load_vendored_fragment()
    assert vendored == shipped, (
        "vendored fixture "
        f"{_VENDORED_FRAGMENT_PATH} has drifted from the shipped fragment "
        f"at {shipped_path} -- re-copy it (see this file's module "
        "docstring)"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
