"""coordinator_core/hooks/tests/test_arrival_w4_c12.py — the W4-C12 arrival
gate for the UserPromptExpansion and observation hook family.

Subject: the eight `hooks.<name>` ops this row's own body writes —
`pickup_autofire`, `mise_autofire`, `handoff_segment_inject`,
`group_em_autofire`, `nudge_initiative_goals_ladder`,
`offer_exploration_tier_dispatch`, `observe_config_change`,
`observe_post_compact` — ported from DoE-claude's
`coordinator/hooks/scripts/*.py` siblings per
docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.

Not exhaustive re-coverage of every DoE test assertion (several source
modules carry hundreds of lines of decision-object rendering/budget-ladder
logic already exercised at the DoE layer) — this file exercises each op's
`register_op` registration, fast cold import, its fail-open no-op/silent-
pass path against a non-matching payload, and one or two of the pure
predicate/render helpers each module carries.
"""

from __future__ import annotations

import importlib
import sys
import time

import pytest

from coordinator_core import ipc
from coordinator_core.hooks import group_em_autofire as gea
from coordinator_core.hooks import handoff_segment_inject as hsi
from coordinator_core.hooks import mise_autofire as ma
from coordinator_core.hooks import nudge_initiative_goals_ladder as nigl
from coordinator_core.hooks import observe_config_change as occ
from coordinator_core.hooks import observe_post_compact as opc
from coordinator_core.hooks import offer_exploration_tier_dispatch as oetd
from coordinator_core.hooks import pickup_autofire as pa


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_name",
    [
        "hooks.pickup_autofire",
        "hooks.mise_autofire",
        "hooks.handoff_segment_inject",
        "hooks.group_em_autofire",
        "hooks.nudge_initiative_goals_ladder",
        "hooks.offer_exploration_tier_dispatch",
        "hooks.observe_config_change",
        "hooks.observe_post_compact",
    ],
)
def test_op_is_registered(op_name):
    assert op_name in ipc._REGISTRY


@pytest.mark.parametrize(
    "module_name",
    [
        "coordinator_core.hooks.pickup_autofire",
        "coordinator_core.hooks.mise_autofire",
        "coordinator_core.hooks.handoff_segment_inject",
        "coordinator_core.hooks.group_em_autofire",
        "coordinator_core.hooks.nudge_initiative_goals_ladder",
        "coordinator_core.hooks.offer_exploration_tier_dispatch",
        "coordinator_core.hooks.observe_config_change",
        "coordinator_core.hooks.observe_post_compact",
    ],
)
def test_import_is_fast(module_name):
    """Cold-import budget: well under the plan's 500ms brightline."""
    for name in list(sys.modules):
        if name == module_name or name.startswith(module_name + "."):
            del sys.modules[name]

    t0 = time.perf_counter()
    importlib.import_module(module_name)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 500, f"{module_name} cold import took {elapsed_ms}ms"


# ---------------------------------------------------------------------------
# pickup_autofire
# ---------------------------------------------------------------------------


def test_pickup_autofire_non_matching_command_is_silent_pass():
    resp = pa._handler({"command_name": "not-pickup", "command_args": "x"})
    assert resp == {}


def test_pickup_autofire_should_apply_reads_absent_verdict_as_hold():
    assert pa.should_apply({}) is False
    assert pa.coast_verdict({}) is None


def test_pickup_autofire_should_apply_true_on_clear_and_empty_judgment_points():
    decision = {"gates": {"coast": {"verdict": "clear"}}, "judgment_points": []}
    assert pa.should_apply(decision) is True


def test_pickup_autofire_extract_baton_paths_filters_non_claim_tokens():
    result = pa.extract_baton_paths("state/handoffs/x.md --hibernate plan.md")
    assert result == "state/handoffs/x.md"


def test_pickup_autofire_split_prose_tail():
    path, prose = pa.split_prose_tail("state/handoffs/x.md -- please review")
    assert path == "state/handoffs/x.md"
    assert prose == "please review"


def test_pickup_autofire_never_raises_on_malformed_payload():
    resp = pa._handler({"tool_input": "not-a-dict"})
    assert resp == {}


# ---------------------------------------------------------------------------
# mise_autofire
# ---------------------------------------------------------------------------


def test_mise_autofire_non_matching_command_is_silent_pass():
    resp = ma._handler({"command_name": "not-mise", "command_args": ""})
    assert resp == {}


def test_mise_autofire_decode_mint_payload_rejects_malformed_shape():
    assert ma.decode_mint_payload("not json") is None
    assert ma.decode_mint_payload(
        '{"run_id": "r1", "inventory_path": "state/mise-inventory/r1.md"}'
    ) == {"run_id": "r1", "inventory_path": "state/mise-inventory/r1.md"}


def test_mise_autofire_never_raises_on_malformed_payload():
    resp = ma._handler({"tool_input": None})
    assert resp == {}


# ---------------------------------------------------------------------------
# handoff_segment_inject
# ---------------------------------------------------------------------------


def test_handoff_segment_inject_non_matching_command_is_silent_pass():
    resp = hsi._handler({"command_name": "not-handoff", "command_args": ""})
    assert resp == {}


def test_handoff_segment_inject_normalize_segment_fields_rejects_bad_case():
    assert (
        hsi._normalize_segment_fields("seg-1", "not-a-case", "protected", 1, "body", "x.md")
        is None
    )
    good = hsi._normalize_segment_fields("seg-1", "shared", "protected", 1, "body text", "x.md")
    assert good == {
        "segment_id": "seg-1",
        "case": "shared",
        "class": "protected",
        "order": 1,
        "body": "body text",
        "source": "x.md",
    }


def test_handoff_segment_inject_select_segments_filters_and_sorts():
    segments = [
        {"segment_id": "b", "case": "shared", "order": 2},
        {"segment_id": "a", "case": "shared", "order": 1},
        {"segment_id": "c", "case": "dirty-tree", "order": 0},
    ]
    selected = hsi.select_segments(segments, {"shared"})
    assert [s["segment_id"] for s in selected] == ["a", "b"]


def test_handoff_segment_inject_never_raises_on_malformed_payload():
    resp = hsi._handler({"tool_input": 12345})
    assert resp == {}


# ---------------------------------------------------------------------------
# group_em_autofire
# ---------------------------------------------------------------------------


def test_group_em_autofire_non_matching_command_is_silent_pass():
    resp = gea._handler({"command_name": "not-group-em", "session_id": "sid"})
    assert resp == {}


def test_group_em_autofire_no_session_id_is_silent_pass():
    resp = gea._handler({"command_name": "group-em"})
    assert resp == {}


def test_group_em_autofire_render_refused_nomination():
    text = gea.render_additional_context({"nomination": {"claimed": False, "message": "busy"}})
    assert "REFUSED" in text
    assert "busy" in text


def test_group_em_autofire_never_raises_on_malformed_payload():
    resp = gea._handler({"command_name": None})
    assert resp == {}


# ---------------------------------------------------------------------------
# nudge_initiative_goals_ladder
# ---------------------------------------------------------------------------


def test_nudge_initiative_goals_ladder_non_write_edit_tool_is_silent_pass():
    import asyncio

    resp = asyncio.run(
        nigl._handler({"tool_name": "Read", "tool_input": {"file_path": "state/initiatives/x.yaml"}})
    )
    assert resp == {}


def test_nudge_initiative_goals_ladder_non_initiative_path_is_silent_pass():
    import asyncio

    resp = asyncio.run(
        nigl._handler(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "state/other/x.yaml", "content": "a: 1"},
            }
        )
    )
    assert resp == {}


def test_nudge_initiative_goals_ladder_escape_hatch(monkeypatch):
    import asyncio

    monkeypatch.setenv("COORDINATOR_INITIATIVE_GOALS_NUDGE_OFF", "1")
    resp = asyncio.run(
        nigl._handler(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "state/initiatives/x.yaml", "content": "label: x"},
            }
        )
    )
    assert resp == {}


def test_nudge_initiative_goals_ladder_never_raises_on_malformed_payload():
    import asyncio

    resp = asyncio.run(nigl._handler({"tool_input": "not-a-dict"}))
    assert resp == {}


# ---------------------------------------------------------------------------
# offer_exploration_tier_dispatch
# ---------------------------------------------------------------------------


def test_offer_exploration_tier_dispatch_non_agent_tool_is_silent_pass():
    resp = oetd._handler({"tool_name": "Bash", "tool_input": {}})
    assert resp == {}


def test_offer_exploration_tier_dispatch_is_doctrine_carrying():
    assert oetd._is_doctrine_carrying("code-reviewer") is True
    assert oetd._is_doctrine_carrying("Explore") is False
    assert oetd._is_doctrine_carrying("plan") is False
    assert oetd._is_doctrine_carrying(None) is False


def test_offer_exploration_tier_dispatch_is_read_only_shaped():
    assert oetd._is_read_only_shaped("find every caller of foo()") is True
    assert oetd._is_read_only_shaped("find and fix every caller of foo()") is False
    assert oetd._is_read_only_shaped("") is False


def test_offer_exploration_tier_dispatch_never_raises_on_malformed_payload():
    resp = oetd._handler({"tool_input": None})
    assert resp == {}


# ---------------------------------------------------------------------------
# observe_config_change
# ---------------------------------------------------------------------------


def test_observe_config_change_never_raises_on_malformed_payload():
    resp = occ._handler({"cwd": 12345})
    assert resp == {}


def test_observe_config_change_writes_a_record(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    resp = occ._handler({"cwd": str(repo), "session_id": "sid-1", "file_path": ".claude/settings.local.json"})
    assert resp == {}
    log = repo / ".git" / "coordinator-sessions" / "hook-observations" / "ConfigChange.jsonl"
    assert log.is_file()
    assert "sid-1" in log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# observe_post_compact
# ---------------------------------------------------------------------------


def test_observe_post_compact_never_raises_on_malformed_payload():
    resp = opc._handler({"cwd": 12345})
    assert resp == {}


def test_observe_post_compact_writes_the_whole_payload_verbatim(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    resp = opc._handler({"cwd": str(repo), "compact_summary": "s", "trigger": "manual"})
    assert resp == {}
    log = repo / ".git" / "coordinator-sessions" / "hook-observations" / "PostCompact.jsonl"
    assert log.is_file()
    text = log.read_text(encoding="utf-8")
    assert "compact_summary" in text
    assert "manual" in text
