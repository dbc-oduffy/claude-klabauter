"""fd8612 replay through the default entry points: a token only the uncommitted tree holds is
reported "uncommitted, not landed" by plan.readiness_verdict (no base_ref), em_brief_block and
prep_gate._anchors, and all three read it RESOLVES once the edit is committed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import plan_readiness_verdict as op
from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import UNCOMMITTED_NOT_LANDED, premise_ref
from coordinator_core.roadmap import prep_gate

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REL = "docs/plans/2026-11-22-fixture.md"
SIZING = "state/sizings/2026-11-22-weightpins.yaml"
TOKEN = "WEIGHT_PINS"

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""

_ANCHOR = (
    f"  - id: AP1\n    claim: pins.py already pins {TOKEN}\n    check:\n"
    f"      command: grep -c {TOKEN} pins.py\n      expect: '1'\n"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _sizing() -> dict:
    return {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "do x"}],
        "em_analysis": {"em_brief": f"The pin lives as `{TOKEN}` in `pins.py`."},
    }


@pytest.fixture
def world(tmp_path):
    """HEAD commits pins.py without the token; the working tree then adds it, uncommitted."""
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "pins.py").write_text("OTHER = 1\n", encoding="utf-8")
    (root / SIZING).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-11-22\n"
        f"sizing_object: {SIZING}\n"
        "census:\n  - question: q\n    command: grep -c a b\n    result: '1'\n"
        "capabilities: []\nanchor_premises:\n" + _ANCHOR +
        "prime_exit_criterion:\n  statement: the op reports per class\n"
        f"  derived_from: {SIZING}\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pins without the token")
    (root / "pins.py").write_text(f"OTHER = 1\n{TOKEN} = {{}}\n", encoding="utf-8")
    return root


def _readiness(root: Path) -> dict:
    reply = op._handler({"plan": REL}, root / ".git")
    return {v["id"]: v for v in reply["anchor_verdicts"]}["AP1"]


def _brief(root: Path) -> str:
    return ask_stage.em_brief_block(_sizing(), root, premise_ref(root, SIZING))


def _prep_anchors(root: Path) -> dict:
    text = (root / REL).read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---", 2)[1])
    return prep_gate._anchors(fm, text, root)


def test_a_token_only_the_dirty_tree_holds_is_uncommitted_not_landed(world):
    verdict = _readiness(world)
    assert verdict["verdict"] == "UNRESOLVED"
    assert UNCOMMITTED_NOT_LANDED in verdict["observed"]

    block = _brief(world)
    assert f"UNCOMMITTED: {TOKEN} in pins.py" in block
    assert UNCOMMITTED_NOT_LANDED in block

    assert _prep_anchors(world)["status"] != "PASS"


def test_the_same_claims_resolve_once_the_edit_is_committed(world):
    _git(world, "add", "-A")
    _git(world, "commit", "-q", "-m", "land the pin")

    verdict = _readiness(world)
    assert verdict["verdict"] == "RESOLVES"
    assert UNCOMMITTED_NOT_LANDED not in str(verdict["observed"])

    block = _brief(world)
    assert "UNCOMMITTED:" not in block and "CONTRADICTED:" not in block

    assert _prep_anchors(world)["status"] == "PASS"
