"""requirements_ledger.judge: a design row judged met with an empty met_by is judged not_met."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_requirements_ledger_design_rows import (  # noqa: F401
    _verdict,
    run,
)


@pytest.mark.parametrize("met_by", [None, [], "", [""], ["  "], [3]])
def test_met_with_empty_met_by_is_not_met(run, met_by):
    root, ledger, design, _ = run
    extra = {} if met_by is None else {"met_by": met_by}
    out = rl.judge(ledger, _verdict(ledger, design, status="met", **extra), repo_root=root)
    assert not out["pass"]
    reasons = [f["reason"] for f in out["failures"] if f["id"] == design.id]
    assert any(r.startswith("not met") for r in reasons), reasons
    assert all(o["id"] != design.id for o in out["open"])


def test_met_with_met_by_naming_no_evidence_file_is_also_not_met(run):
    root, ledger, design, _ = run
    out = rl.judge(ledger, _verdict(ledger, design, status="met", met_by=["no/such/file.md"]), repo_root=root)
    assert not out["pass"]
    assert any(f["id"] == design.id and f["reason"].startswith("not met") for f in out["failures"])


def test_empty_met_by_does_not_downgrade_a_row_with_default_evidence(run):
    root, ledger, design, _ = run
    review = root / "review.md"
    review.write_text(
        "---\nagent_type: coordinator:senior-front-end\nverdict: APPROVED\n---\n"
        "Checked https://claude.ai/design/p/abc123\n",
        encoding="utf-8",
    )
    wave = [{"agent_type": "coordinator:senior-front-end", "sidecar_path": "review.md", "verdict": "APPROVED"}]
    out = rl.judge(ledger, _verdict(ledger, design, status="met"), repo_root=root, wave_reviews=wave)
    assert out["pass"], out
