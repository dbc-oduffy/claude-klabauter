"""The grind script's meta carries `fanout`: the expected-case agent and row-output token figure."""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops import _workflow_contract as wc
from coordinator_core.ops.dispatch_emit.grind_admission import ROW_TOTAL_TOKENS
from coordinator_core.ops.dispatch_emit import fanout_cost
from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

from .conftest import REVIEW_KW

_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


_FIXED_LEGS = 8


def _compose(
    n_entries: int, batch_key: str = "P0", row_dir: str = "coordinator_core/rows", profile_dir: Path = _PROFILE_DIR,
    **knob_overrides,
) -> str:
    """`row_dir` defaults to a product path, so a row's close is an agent call (not record-only)."""
    profile = gp.load_profile("fixture", profile_dir)
    gp.validate_graph(profile)
    knobs = {**gp.resolve_appetite(profile, "standard"), **knob_overrides}
    entries = tuple(
        ManifestEntry(row_id=f"row{i}", path=f"{row_dir}/row{i}.yaml", digest=f"{i:064x}", batch_key=batch_key)
        for i in range(n_entries)
    )
    manifest = Manifest(entries=entries, batch_sizes={batch_key: 4}, source=None, digest="deadbeef")
    return gc.compose_grind_script(
        manifest,
        profile,
        knobs,
        run_dir=Path("state/queue-grind/fixture/run-1"),
        appetite="standard",
        agent_type_host=None,
        queue_dirs=["state/bug-backlog"],
        **REVIEW_KW,
    )


def test_six_entries_compose_fewer_batches_than_entries_and_meta_reports_it():
    script = _compose(6)
    fanout = fanout_cost.read_fanout(script)
    assert fanout is not None
    assert fanout["units"] == 6
    assert fanout["basis"] == "expected"
    assert fanout["groups"] == [["row0", "row1", "row2", "row3"], ["row4", "row5"]]
    assert len(fanout["groups"]) < fanout["units"]
    assert fanout["expectedAgents"] > len(fanout["groups"]) + _FIXED_LEGS
    assert fanout["startupTokensPerAgent"] == fanout_cost.STARTUP_TOKENS_PER_AGENT == 54_000
    assert fanout["startupTokensTotal"] == fanout["expectedAgents"] * fanout["startupTokensPerAgent"]
    assert fanout["rowTotalTokensTotal"] == ROW_TOTAL_TOKENS * 6


def test_agents_cover_per_batch_per_row_and_once_per_run_legs():
    # Fixture, P0 batch key: triage + refute-close per batch, one fix per row, verify is op-mode
    # (no agent), plus the eight once-per-run sites: terminal commit, run close, review prep, review
    # wave, review integration, review re-verify, review-fix commit, adjudicate. Add a leg, bump this.
    fanout = fanout_cost.read_fanout(_compose(7))
    assert fanout["expectedAgents"] == 2 * 2 + 7 + _FIXED_LEGS


def test_agent_mode_verify_adds_one_leg_per_row():
    op_mode = fanout_cost.read_fanout(_compose(7, "P0"))
    agent_mode = fanout_cost.read_fanout(_compose(7, "P1"))
    assert agent_mode["expectedAgents"] - op_mode["expectedAgents"] == 7


def test_per_row_legs_scale_with_entry_count():
    small, large = fanout_cost.read_fanout(_compose(2)), fanout_cost.read_fanout(_compose(9))
    assert large["expectedAgents"] - small["expectedAgents"] == (9 - 2) + 2 * (3 - 1)
    assert large["rowTotalTokensTotal"] > small["rowTotalTokensTotal"]


def test_a_named_budget_renders_the_ceiling_const_and_none_renders_null():
    script = _compose(6, budget_tokens=2_000_000)
    assert "const BUDGET_TOKENS = 2000000;" in script
    assert "const BUDGET_TOKENS = null;" in _compose(6)


def test_record_only_rows_count_only_the_agent_stages(tmp_path):
    """Scripted close, commit, sweep, review and adjudicate contribute 0: one batch is one triage
    plus the routing verify per row."""
    fixture = (_PROFILE_DIR / "fixture.yaml").read_text(encoding="utf-8")
    pre_fix = fixture.replace(
        "  triage:\n    kind: triage\n    edges:\n      confirmed-bug: fix\n",
        "  triage:\n    kind: triage\n    edges:\n      confirmed-bug: route\n",
    ).replace(
        "  refute_close:\n",
        "  route:\n    kind: verify\n    verify:\n      default: agent\n    edges:\n      pass: refute_close\n    on_fail: verify-failed\n"
        "  refute_close:\n",
    )
    assert pre_fix != fixture
    (tmp_path / "fixture.yaml").write_text(pre_fix, encoding="utf-8")

    record = fanout_cost.read_fanout(_compose(1, row_dir="state/bug-backlog", profile_dir=tmp_path))
    assert record["expectedAgents"] == 2  # triage + routing verify
    assert record["startupTokensTotal"] == 2 * fanout_cost.STARTUP_TOKENS_PER_AGENT

    product = fanout_cost.read_fanout(_compose(1, row_dir="coordinator_core/rows", profile_dir=tmp_path))
    assert product["expectedAgents"] > record["expectedAgents"] + _FIXED_LEGS - 1


def test_record_only_rows_leave_the_fix_path_uncounted_and_product_rows_count_it():
    record = fanout_cost.read_fanout(_compose(1, row_dir="state/bug-backlog"))
    product = fanout_cost.read_fanout(_compose(1))
    assert record["expectedAgents"] == 1  # triage only: close is scripted, the fix path is off the counted path
    assert product["expectedAgents"] == 2 + 1 + _FIXED_LEGS


def test_meta_stays_a_pure_literal_with_phases_declared():
    script = _compose(6)
    meta = re.search(r"export const meta = \{.*?\n\};", script, re.S)
    assert meta is not None
    assert [f.code for f in wc.check_meta_pure_literal(meta.group(0))] == []
    assert [f.code for f in wc.check_phase_mismatch(script, meta.group(0))] == []
