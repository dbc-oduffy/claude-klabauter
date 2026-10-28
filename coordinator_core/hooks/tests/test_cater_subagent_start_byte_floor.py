"""
coordinator_core.hooks.tests.test_cater_subagent_start_byte_floor -- a
shrink-only, per-agent-type floor on the bytes of SubagentStart text that
`compose_catering` produces against the real coordinator-content-repo policy.

Per type, not one global cap: a single cap lets one type grow while another
shrinks. Each floor here only ever moves down. A type whose bytes rise past
its floor fails; a type that shrank passes and warns, naming the lower floor
to record. A catered type with no floor fails, so a new type cannot grow
unguarded.

Floors recorded at coordinator-content-repo c6c8bf7a33: provisioned-scaffold-precedence
restored to the 23 types 732baedf40 dropped (6b58fc65ee, e0dedb635e) and its
snippet trimmed from 1539 B to 432 B. The measured text is the delivered shape:
a UUID session id and an agent id, so the sidecar path is the length a live
dispatch carries.

Spec backlink: docs/plans/2026-10-28-a8dde4-2-subagentstart-scaffold-block-cut.md (C2)
Module under test: coordinator_core/hooks/cater_subagent_start.py
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

from coordinator_core.hooks.cater_subagent_start import compose_catering
from coordinator_core.session import machinery_paths
from coordinator_core.testing.content_root import content_root_and_present

CONTENT_ROOT, CONTENT_ROOT_PRESENT = content_root_and_present()

#: Delivery-shaped ids: the sidecar path a live dispatch carries embeds both.
SESSION_ID = "00000000-0000-4000-8000-0000b17ef100"
AGENT_ID = "a0123456789abcdef"

#: UTF-8 bytes of each type's composed SubagentStart text. Lower a value when
#: its type shrinks; never raise one -- a floor that may rise is not a floor.
FLOORS: dict[str, int] = {
    "coordinator:apm": 3095,
    "coordinator:atlas-clarity-reviewer": 1700,
    "coordinator:atlassian-worker": 1062,
    "coordinator:code-reviewer": 3860,
    "coordinator:code-reviewer-weekly": 1700,
    "coordinator:coverage-auditor": 1700,
    "coordinator:distill-writer": 1062,
    "coordinator:doc-link-checker": 1700,
    "coordinator:docs-checker": 2246,
    "coordinator:drive-worker": 1062,
    "coordinator:eng-director": 2764,
    "coordinator:enricher": 2335,
    "coordinator:executor": 2250,
    "coordinator:exit-criterion-falsifier": 1062,
    "coordinator:exit-criterion-judge": 1985,
    "coordinator:external-pattern-checker": 2246,
    "coordinator:falsifier-integrity-reviewer": 1147,
    "coordinator:git-commit-agent": 1062,
    "coordinator:group-em-assistant": 1393,
    "coordinator:notebooklm-research-scout": 2308,
    "coordinator:objective-reacher": 1985,
    "coordinator:op-relay": 1062,
    "coordinator:overengineering-reviewer": 2764,
    "coordinator:plan-author": 1086,
    "coordinator:plan-author-residue": 1147,
    "coordinator:plan-coverage-checker": 2331,
    "coordinator:plan-reviewer": 2484,
    "coordinator:premise-checker": 2331,
    "coordinator:prior-art-checker": 2331,
    "coordinator:queue-grind-op-runner": 1062,
    "coordinator:repo-scout": 2308,
    "coordinator:repo-specialist": 2240,
    "coordinator:research-brief-author": 1062,
    "coordinator:research-scout": 2308,
    "coordinator:research-specialist": 2240,
    "coordinator:research-sweep": 2240,
    "coordinator:research-synthesizer": 2240,
    "coordinator:research-worker": 2240,
    "coordinator:residue-author": 1147,
    "coordinator:roadmap-planner": 1086,
    "coordinator:security-audit-worker": 1700,
    "coordinator:senior-front-end": 2611,
    "coordinator:sizing-scout": 1062,
    "coordinator:staff-data-sci": 2611,
    "coordinator:staff-eng": 2764,
    "coordinator:staff-ux": 2611,
    "coordinator:structured-synthesizer": 2240,
    "coordinator:subtractive-adjudicator": 1062,
    "coordinator:test-runner": 2250,
    "coordinator:vp-product": 2611,
    "coordinator:workflow-maker": 1062,
}


@pytest.mark.skipif(
    not CONTENT_ROOT_PRESENT,
    reason="sibling coordinator-content-repo checkout not resolvable on this machine "
    "(see coordinator_core.testing.content_root.resolve_content_root)",
)
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_agent_type_grows_its_subagent_start_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    policy_file = Path(CONTENT_ROOT) / "coordinator" / "subagent-sandbox-policy.yaml"
    catered = yaml.safe_load(policy_file.read_text(encoding="utf-8"))["contract_blocks"]
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_file))
    # The suite conftest quarantines HOME and stubs the content-root pointer,
    # so unpinned, the blocks and role legs both resolve to nothing and every
    # type measures only its sidecar text -- a floor that can never trip.
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(Path(CONTENT_ROOT) / "coordinator"))
    monkeypatch.setattr(
        "coordinator_core.hooks.cater_subagent_start.read_content_root",
        lambda: str(CONTENT_ROOT),
    )

    # cwd must be the real sibling checkout, so the sidecar writes land in a
    # PEER's tracked tree: remove exactly the share dir this loop mints.
    measured: dict[str, int] = {}
    try:
        for agent_type, block_names in catered.items():
            text = compose_catering(
                {
                    "agent_type": agent_type, "session_id": SESSION_ID, "agent_id": AGENT_ID,
                    "contract_blocks": block_names,
                },
                cwd=CONTENT_ROOT,
            )
            measured[agent_type] = len(text.encode("utf-8"))
    finally:
        shutil.rmtree(machinery_paths.share_dir(CONTENT_ROOT, SESSION_ID), ignore_errors=True)

    unfloored = sorted(t for t in measured if t not in FLOORS)
    grew = {t: (FLOORS[t], b) for t, b in measured.items() if t in FLOORS and b > FLOORS[t]}
    shrank = {t: b for t, b in measured.items() if t in FLOORS and b < FLOORS[t]}

    if shrank:
        warnings.warn(f"SubagentStart bytes shrank; lower these FLOORS: {shrank!r}")
    assert not unfloored, f"catered types with no recorded floor: {unfloored!r}"
    assert not grew, f"SubagentStart bytes grew past the floor (floor, now): {grew!r}"
