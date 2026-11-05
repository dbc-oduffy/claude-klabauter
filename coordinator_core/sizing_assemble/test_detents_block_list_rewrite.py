"""`sizing-assemble --write` over a sizing whose `detents` is a block list writes parseable YAML."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.session import record_homes

_NAME = "2026-11-04-block-detents.yaml"

_DRAFT = """schema: sizing-object
intent: "Ship the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents:
  - premise_unproven
  - exit_criterion_pending
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""

_ARGS = ["--tshirt", "M", "--intent", "Ship the thing", "--premise-provenance", "read",
         "--premise-evidence", "read it", "--no-plan-hand-off", "--no-verbatims"]


def _target(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".git").mkdir()
    path = Path(record_homes.record_path(str(tmp_path), "sizings", _NAME))
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize("indent", ["  ", ""])
def test_block_list_detents_rewritten_as_parseable_yaml(tmp_path, indent):
    body = _DRAFT.replace("  - ", f"{indent}- ")
    path = _target(tmp_path, body)
    assert sizing_assemble.main([*_ARGS, "--write", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert isinstance(doc["detents"], list)
    assert "exit_criterion_pending" not in doc["detents"]
    assert doc["status"] == "routed"


def test_inline_detents_still_rewritten(tmp_path):
    path = _target(tmp_path, _DRAFT.replace("detents:\n  - premise_unproven\n  - exit_criterion_pending\n", "detents: []\n"))
    assert sizing_assemble.main([*_ARGS, "--write", str(path)]) == 0
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8"))["detents"], list)
