"""Guard: no emitted grind hands the PM a question an adjudicator has not ruled on."""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit import pm_adjudication as pa
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry
from coordinator_core.session import record_homes

_PROFILES = Path(__file__).parent / "fixtures" / "queue-profiles"


def _script() -> str:
    profile = gp.load_profile("fixture", _PROFILES)
    gp.validate_graph(profile)
    knobs = gp.resolve_appetite(profile, "standard")
    entries = (ManifestEntry(row_id="r0", path="state/x/r0.yaml", digest="0" * 64, batch_key="P0"),)
    manifest = Manifest(entries=entries, batch_sizes={"P0": 4}, source=None, digest="deadbeef")
    return gc.compose_grind_script(
        manifest, profile, knobs, run_dir=Path(record_homes.home_dir("", "queue-grind")) / "fixture" / "run-1",
        appetite="standard", agent_type_host=None,
    )


def test_pm_bound_types_are_adjudicated_before_the_result_returns():
    s = _script()
    assert "'coordinator:apm'" in s or '"coordinator:apm"' in s
    assert s.index("await _adjudicatePmBound();") < s.index("const HANDBACK = {")
    for t in pa.PM_BOUND_HANDBACK_TYPES:
        assert f"'{t}'" in s
    assert {"needs-judgment", "unclear-direction"} <= set(pa.PM_BOUND_HANDBACK_TYPES)


def test_missing_verdict_fails_closed_with_an_adjudication_record():
    s = _script()
    assert "const pmOnly = !v || v.pmOnly === true || gated;" in s
    assert "verdict: v ? v.verdict : 'unavailable'" in s


def test_pm_only_false_cannot_clear_an_irreversible_gate():
    s = _script()
    gate = re.compile(pa.IRREVERSIBLE_GATE_SOURCE, re.IGNORECASE)
    for text in ("merge to main", "push to main", "publish the mirror",
                 "cross-repo commit assent", "force-push the branch"):
        assert gate.search(text), text
    assert not gate.search("should the row be parked")
    assert f"const IRREVERSIBLE_GATE = /{pa.IRREVERSIBLE_GATE_SOURCE}/i;" in s
    assert "IRREVERSIBLE_GATE.test([h.reason, v && v.ruling].join(' '))" in s


def test_adjudicator_call_is_a_counted_sonnet_call_site():
    s = _script()
    assert len(re.findall(r"\bagent\(", s)) == len(re.findall(r"_recordCall\('", s))
    assert "_recordCall('adjudicate');" in s
