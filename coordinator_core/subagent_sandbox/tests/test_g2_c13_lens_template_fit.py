"""
coordinator_core.subagent_sandbox.tests.test_g2_c13_lens_template_fit -- G2
chunk C13 regression net (docs/plans/2026-07-24-g2-plan-pipeline-sidecar-
contract.md, C13, coordinator-claude): does each of the five surviving plan-pipeline
lenses' `report_type_map:`-assigned template (`assessment` / `review-findings` /
`run-report`) actually accommodate the lens's real provisioned output shape?

Empirical finding (live dogfood dispatches, 2026-07-25, and the pre-existing
`state/plan-sidecars/*.{prior-art-check,plan-coverage-check,external-pattern,
docs-check}.md` corpus): all six lenses -- prior-art-checker, external-
pattern-checker, docs-checker, code-architect (`assessment`), plan-coverage-
checker (`review-findings`), enricher (`run-report`) -- replace the
template's placeholder body heading (`## Questions` / `## Findings` /
`## Run notes`) wholesale with their own bucket/verdict shape (Conflicts /
Compatible-but-relevant / Silent; Missed / Ambiguous / Weak-OOS / Hedges /
Unratified-Deferrals / Substrate-drift; a full architecture blueprint; etc.)
-- never the assigned placeholder heading itself. The only part of the
scaffold every lens's real output actually preserves is the shared
frontmatter contract (`status`/`agent_type`/`spawned_at`/`lead_session_id`/
`divergence`/`commits`/`dispatch_feed`) and the universal `## Exit
interview` section -- both of which are template-type-INDEPENDENT (see
`provision_report._frontmatter` / `_exit_interview_section`, shared by every
entry in `_TEMPLATE_REGISTRY`).

Conclusion (no template variant needed): a plan-derivable sidecar's body is
freeform markdown with no schema enforcing its placeholder heading -- the
"assessment"/"review-findings"/"run-report" label only picks a suggested
starting heading, never a constraint a lens's real content must conform to.
This test locks the two invariants that make that safe: (1) `report_type_map`
still resolves these six subagent_types to the template types this finding
assumes -- a drift here would invalidate the finding above and needs a
re-check, not a silent pass; (2) `_build_doc_text` for each resolved type
still emits the shared frontmatter + exit-interview wrapper untouched by the
type-specific body, so a lens overwriting the body placeholder can never lose
the wrapper machine-consumers (doc-handoff, exit-interview harvesting) rely
on.

Spec backlink: docs/plans/2026-07-24-g2-plan-pipeline-sidecar-contract.md, C13
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.subagent_sandbox.provision_report import _build_doc_text
from coordinator_core.testing.doe_root import doe_root_and_present

_doe_root, _doe_present = doe_root_and_present()

#: The surviving G2 plan-pipeline lenses this chunk covers, and the template
#: type this test asserts `report_type_map:` (coordinator/subagent-sandbox-
#: policy.yaml, coordinator-claude) still resolves each of them to. A change here
#: (in either direction) means the C13 finding above needs re-verification
#: against fresh live output, not a silent test-literal update.
#:
#: `coordinator:code-architect` was the sixth entry and is deliberately absent:
#: the agent was RETIRED by PM ruling in coordinator-claude `ab2889499` (2026-07-30,
#: "delete the agent nobody ever dispatched"), which unwired its `report_type_map`
#: row along with the agent body, its registry rows, and its provisioning entry.
#: This is the re-verification this comment demands, not a literal bump: the
#: lens's current real output is that it has none, because it no longer exists.
#: Ruled out as a rename -- the retirement names no successor, and
#: `coordinator:architecture-audit`/`architecture-survey` are SKILLS, so they
#: never appear in a map keyed on subagent_type. C13 itself deferred this
#: decision rather than making it ("Do NOT retire code-architect inside G2 --
#: flag its orphan status for a separate decision"); `ab2889499` is that
#: separate decision landing, five days after this net was written.
_G2_LENS_EXPECTED_TEMPLATE_TYPES = {
    "coordinator:prior-art-checker": "assessment",
    "coordinator:external-pattern-checker": "assessment",
    "coordinator:docs-checker": "assessment",
    "coordinator:plan-coverage-checker": "review-findings",
    "coordinator:enricher": "run-report",
}

#: The placeholder body heading each template type provisions -- this is what
#: every one of the six lenses' REAL dispatched output replaces wholesale,
#: per the module docstring's live-evidence citation.
_TEMPLATE_PLACEHOLDER_HEADING = {
    "assessment": "## Questions",
    "review-findings": "## Findings",
    "run-report": "## Run notes",
}


def _policy_path() -> Path:
    return Path(_doe_root) / "coordinator" / "subagent-sandbox-policy.yaml"


@pytest.fixture(scope="module")
def report_type_map() -> dict:
    if not _doe_present or not _policy_path().exists():
        pytest.skip(
            "sibling coordinator-claude checkout with coordinator/subagent-sandbox-policy.yaml not found"
        )
    data = yaml.safe_load(_policy_path().read_text(encoding="utf-8"))
    return data["report_type_map"]


@pytest.mark.parametrize(
    ("subagent_type", "expected_template"),
    sorted(_G2_LENS_EXPECTED_TEMPLATE_TYPES.items()),
)
def test_g2_lens_still_resolves_to_the_template_this_finding_assumes(
    report_type_map: dict, subagent_type: str, expected_template: str
) -> None:
    assert report_type_map.get(subagent_type) == expected_template, (
        f"{subagent_type}'s report_type_map entry drifted from {expected_template!r} "
        "-- the C13 template-fit finding was verified against the old mapping and "
        "needs re-checking against this lens's current real output, not a silent "
        "test-literal bump."
    )


@pytest.mark.parametrize(
    ("subagent_type", "expected_template"),
    sorted(_G2_LENS_EXPECTED_TEMPLATE_TYPES.items()),
)
def test_g2_lens_template_wrapper_is_body_independent(
    subagent_type: str, expected_template: str
) -> None:
    """The frontmatter + exit-interview wrapper `_build_doc_text` emits for
    each of the five surviving lenses' assigned template is identical regardless of the
    type-specific body placeholder -- proving the wrapper a lens's bespoke
    output must coexist with never depends on, or constrains, that body."""
    text = _build_doc_text(
        subagent_type,
        "2026-01-01T00:00:00+00:00",
        expected_template,
        lead_session_id="sess-g2-c13-fit",
    )

    # Shared frontmatter contract -- template-type independent.
    assert "status: open" in text
    assert f"agent_type: {subagent_type}" in text
    assert "lead_session_id: sess-g2-c13-fit" in text
    assert "divergence:\n  diverged: false" in text
    assert "commits: []" in text
    assert "dispatch_feed:  # forward-declared, INERT until pcli-04 emitter" in text
    assert "  gate_kind: none" in text

    # Universal exit interview -- every template inherits it verbatim.
    assert "## Exit interview" in text
    assert "What did you have to work out that the brief could have told you?" in text

    # Sanity: the scaffold we're asserting the wrapper around really does
    # carry the placeholder heading this lens's real output replaces --
    # otherwise this test would be checking a wrapper around nothing.
    assert _TEMPLATE_PLACEHOLDER_HEADING[expected_template] in text
