"""The grind script's meta carries `fanout`: triage batches plus fixed legs, as a lower bound."""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops import _workflow_contract as wc
from coordinator_core.ops.dispatch_emit import fanout_cost
from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

from .conftest import REVIEW_KW

_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


def _compose(n_entries: int) -> str:
    profile = gp.load_profile("fixture", _PROFILE_DIR)
    gp.validate_graph(profile)
    knobs = gp.resolve_appetite(profile, "standard")
    entries = tuple(
        ManifestEntry(row_id=f"row{i}", path=f"state/bug-backlog/row{i}.yaml", digest=f"{i:064x}", batch_key="P0")
        for i in range(n_entries)
    )
    manifest = Manifest(entries=entries, batch_sizes={"P0": 4}, source=None, digest="deadbeef")
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
    assert fanout["basis"] == "lower-bound"
    assert fanout["groups"] == [["row0", "row1", "row2", "row3"], ["row4", "row5"]]
    assert len(fanout["groups"]) < fanout["units"]
    fixed = fanout["expectedAgents"] - len(fanout["groups"])
    assert fixed >= 1
    assert fanout["startupTokensPerAgent"] == fanout_cost.STARTUP_TOKENS_PER_AGENT
    assert fanout["startupTokensTotal"] == fanout["expectedAgents"] * fanout["startupTokensPerAgent"]


def test_fixed_legs_equal_the_agent_sites_of_the_once_per_run_legs():
    fanout = fanout_cost.read_fanout(_compose(7))
    # Eight fixed sites: terminal commit, run close, review prep, review wave, review integration,
    # review re-verify, review-fix commit, adjudicate. Add a leg, bump this.
    assert fanout["expectedAgents"] == 10
    assert fanout["expectedAgents"] - len(fanout["groups"]) == 8


def test_fixed_legs_do_not_vary_with_entry_count():
    small, large = fanout_cost.read_fanout(_compose(2)), fanout_cost.read_fanout(_compose(9))
    assert small["expectedAgents"] - len(small["groups"]) == large["expectedAgents"] - len(large["groups"])


def test_meta_stays_a_pure_literal_with_phases_declared():
    script = _compose(6)
    meta = re.search(r"export const meta = \{.*?\n\};", script, re.S)
    assert meta is not None
    assert [f.code for f in wc.check_meta_pure_literal(meta.group(0))] == []
    assert [f.code for f in wc.check_phase_mismatch(script, meta.group(0))] == []
