"""Guard: no emitted grind hands the PM a question an adjudicator has not ruled on."""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit import pm_adjudication as pa
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry
from coordinator_core.session import record_homes

from .conftest import REVIEW_KW
from .test_grind_compose import grind_only

_PROFILES = Path(__file__).parent / "fixtures" / "queue-profiles"


def _script() -> str:
    profile = gp.load_profile("fixture", _PROFILES)
    gp.validate_graph(profile)
    knobs = gp.resolve_appetite(profile, "standard")
    entries = (ManifestEntry(row_id="r0", path="state/x/r0.yaml", digest="0" * 64, batch_key="P0"),)
    manifest = Manifest(entries=entries, batch_sizes={"P0": 4}, source=None, digest="deadbeef")
    return gc.compose_grind_script(
        manifest, profile, knobs, run_dir=Path(record_homes.home_dir("", "queue-grind")) / "fixture" / "run-1",
        appetite="standard", agent_type_host=None, **REVIEW_KW,
    )


def test_pm_bound_types_are_adjudicated_before_the_result_returns():
    s = _script()
    assert "coordinator:apm" not in grind_only(s)
    assert s.index("await _adjudicatePmBound();") < s.index("const HANDBACK = {")
    for t in pa.PM_BOUND_HANDBACK_TYPES:
        assert f"'{t}'" in s
    assert {"needs-judgment", "unclear-direction"} <= set(pa.PM_BOUND_HANDBACK_TYPES)


def test_missing_verdict_fails_closed_with_an_adjudication_record():
    s = _script()
    assert "const pmOnly = !known || stated || gated;" in s
    assert "verdict: known ? v.verdict : 'unavailable'" in s
    assert "rulings.find((r) => r && r.row === h.row) || null" in s


def test_pm_only_false_cannot_clear_an_irreversible_gate():
    s = _script()
    gate = re.compile(pa.IRREVERSIBLE_GATE_SOURCE, re.IGNORECASE)
    for text in ("merge to main", "push to main", "publish the mirror",
                 "cross-repo commit assent", "force-push the branch"):
        assert gate.search(text), text
    assert not gate.search("should the row be parked")
    assert f"const IRREVERSIBLE_GATE = /{pa.IRREVERSIBLE_GATE_SOURCE}/i;" in s
    assert "const gated = known && IRREVERSIBLE_GATE.test(v.externalAction || '');" in s
    assert "h.reason, v && v.ruling" not in s


def test_gate_reads_the_declared_external_action_not_the_hand_back_text():
    s = _script()
    settle = s[s.index("function _settleAdjudication"):s.index("async function _adjudicateEntries")]
    assert "h.reason" not in settle and "v.ruling" not in settle.split("h.adjudication")[0]
    assert "actionable: known && v.verdict === 'ruled' && !!v.actionable && !pmOnly," in settle
    assert "verdict: known ? v.verdict : 'unavailable'" in settle
    item = pa._ADJUDICATION_SCHEMA["properties"]["rulings"]["items"]
    assert item["required"] == ["row", "verdict", "ruling", "externalAction", "actionable"]
    assert item["properties"]["externalAction"] == {"type": "string"}
    assert item["properties"]["actionable"] == {"type": "boolean"}
    assert item["properties"]["verdict"]["enum"] == ["ruled", "spin-out", "pm-only"]


def test_adjudicated_entry_is_not_re_asked():
    s = _script()
    assert "_adjudicateHandback" not in s
    assert "PM_BOUND_TYPES.includes(h.type) && !h.adjudication" in s
    assert "if (pending.length === 0) return;" in s
    assert "Promise.all" not in s[s.index("async function _adjudicatePmBound"):][:400]


def test_adjudicator_call_is_a_counted_sonnet_call_site():
    s = grind_only(_script())
    assert len(re.findall(r"\bagent\(", s)) == len(re.findall(r"_recordCall\('", s))
    assert "_recordCall('adjudicate');" in s


def test_adjudicate_is_one_sonnet_medium_call_for_any_number_of_hand_backs():
    block = pa.compose_adjudicate_block(agent_type_host=None)
    assert len(re.findall(r"\bagent\(", block)) == 1
    assert "agentType: 'general-purpose'" in block
    assert "model: 'sonnet'" in block and "effort: 'medium'" in block
    assert "coordinator:apm" not in block and "opus" not in block
    assert "pending.map(" in block


def test_ask_accept_keeps_the_apm_type():
    assert pa.APM_RULING_AGENT_TYPE == "coordinator:apm"
    assert pa.ADJUDICATOR_AGENT_TYPE != pa.APM_RULING_AGENT_TYPE
