import pytest


#: The suite-wide autouse isolation for this directory (_no_ambient_plugin_root,
#: _unhoisted_ask_scripts, _no_ambient_box_terms, _write_set_reads_clean) lives in
#: coordinator_core/conftest.py: a Package-level conftest's fixtures detach under interleaved
#: collection. Guard: test_dispatch_emit_fixtures_attach.py.


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "xs_gate_live: opt out of this directory's stub of the XS diff-budget refusal "
        "(terminal_commit tests that pin the unmeasurable-diff refusal live)",
    )


@pytest.fixture(autouse=True)
def _xs_diff_budget_gate_off_outside_its_own_tests(request, monkeypatch):
    """A terminal_commit fixture cites an XS sizing but names no run base, so the XS diff-budget
    gate refuses it as unmeasurable before the behaviour under test. A module marked
    `xs_gate_live` exercises the gate live."""
    if request.node.get_closest_marker("xs_gate_live"):
        return
    from coordinator_core.ops.dispatch_emit import terminal_commit

    monkeypatch.setattr(terminal_commit, "_xs_over_budget_refusal", lambda *a, **k: None)


_V5_ROSTER_FRAGMENT = {
    "schema": "review-roster-fragment",
    "schema_version": 5,
    "execute_review": {
        "stages": [
            {
                "kind": "prep",
                "agents": [
                    {
                        "agentType": "coordinator:review-prep",
                        "model": "sonnet",
                        "effort": "low",
                        "schema": "prep",
                    }
                ],
            },
            {
                "kind": "review-wave",
                "agents": [
                    {
                        "agentType": "coordinator:code-reviewer",
                        "model": "opus",
                        "effort": "low",
                        "per": "whole-diff",
                        "schema": "wave",
                    }
                ],
            },
            {
                "kind": "integration",
                "agents": [
                    {
                        "agentType": "coordinator:integrator",
                        "model": "opus",
                        "effort": "low",
                        "schema": "integration",
                    }
                ],
            },
        ]
    },
}

_V5_STAGE_SCHEMAS = {
    "prep": {"type": "object"},
    "wave": {"type": "object"},
    "integration": {"type": "object"},
}

#: Keyword arguments every compose_script/emit_script call needs: an emitted
#: workflow always carries the review wave, so no caller may omit them.
REVIEW_KW = {
    "review_roster_fragment": _V5_ROSTER_FRAGMENT,
    "review_stage_schemas": _V5_STAGE_SCHEMAS,
}


def execute_section(script: str) -> str:
    """The emitted script up to its review wave, for assertions about the
    execute phase that the review wave's own agents would otherwise satisfy."""
    return script.split("phase('Review prep')", 1)[0]


@pytest.fixture
def verifier_roster(monkeypatch):
    """DoE's live plan-route roster plus a delivery-verifier. The live roster names none
    (delivery-exempt), so a test of the verifier's brief has to add one."""
    import copy

    from coordinator_core.ops.dispatch_emit import emission_receipt
    from coordinator_core.ops.review_mint.tests.test_execute_review import _v5_fragment

    live = emission_receipt._load_review_inputs
    verifier = next(
        a for s in _v5_fragment()["execute_review"]["stages"] if s["kind"] == "review-wave"
        for a in s["agents"] if a.get("agentType") == "coordinator:delivery-verifier"
    )

    def _with_verifier(route):
        fragment, schemas = live(route)
        fragment = copy.deepcopy(fragment)
        wave = next(s for s in fragment["execute_review"]["stages"] if s["kind"] == "review-wave")
        wave["agents"].append(verifier)
        return fragment, schemas

    monkeypatch.setattr(emission_receipt, "_load_review_inputs", _with_verifier)
