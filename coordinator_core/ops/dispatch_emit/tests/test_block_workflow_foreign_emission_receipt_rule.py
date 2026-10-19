"""The plan-amended deny branch of block_workflow_foreign_emission: a fire whose receipt plan
body sha differs from the plan's current body is refused; every other shape stays silent."""

from __future__ import annotations

import hashlib
import json

from coordinator_core.frontmatter.primitives import canonical_body_sha
from coordinator_core.hooks.block_workflow_foreign_emission import _handler

PLAN_REL = "docs/plans/p.md"
PLAN_V1 = "---\ntitle: p\n---\n\n# P\n\nold wording\n"
PLAN_V2 = "---\ntitle: p\n---\n\n# P\n\nnew wording\n"


def _fire(tmp_path, plan_text=PLAN_V1, receipt_extra=None, plan_now=PLAN_V1):
    (tmp_path / ".git").mkdir()
    if plan_now is not None:
        plan = tmp_path / PLAN_REL
        plan.parent.mkdir(parents=True)
        plan.write_text(plan_now, encoding="utf-8")
    script = tmp_path / "scratch" / "p.workflow.mjs"
    script.parent.mkdir()
    script.write_bytes(b"// emitted\n")
    receipt = {"sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
    receipt.update(receipt_extra or {})
    script.with_name(script.name + ".emitted.json").write_text(json.dumps(receipt))
    return _handler(
        {"tool_name": "Workflow", "tool_input": {"scriptPath": str(script)}, "cwd": str(tmp_path)}
    )


def _provenance(text):
    return {"plan_body_sha": canonical_body_sha(text), "plan_relpath": PLAN_REL}


def _denied(result):
    return "deny" in json.dumps(result)


def test_amended_plan_is_denied_naming_reemit(tmp_path):
    result = _fire(tmp_path, receipt_extra=_provenance(PLAN_V1), plan_now=PLAN_V2)
    assert _denied(result)
    assert "--plan " + PLAN_REL in json.dumps(result)


def test_unchanged_plan_fires(tmp_path):
    assert not _denied(_fire(tmp_path, receipt_extra=_provenance(PLAN_V1)))


def test_legacy_receipt_without_key_fires(tmp_path):
    assert not _denied(_fire(tmp_path, plan_now=PLAN_V2))


def test_missing_plan_fires(tmp_path):
    assert not _denied(_fire(tmp_path, receipt_extra=_provenance(PLAN_V1), plan_now=None))
