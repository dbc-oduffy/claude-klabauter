"""Every register row shape the engine writes (seed, plan-row, revise) validates against the sizing
schema, under a sized sizing and under a shipped one."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

REL = "state/sizings/2026-10-27-engine-rows.yaml"


def _doc(status):
    return {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": status,
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }


@pytest.mark.parametrize("status", ["sized", "shipped"])
def test_every_engine_written_row_validates(tmp_path, status):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    path = tmp_path / REL
    path.write_text(yaml.safe_dump(_doc(status), sort_keys=False), encoding="utf-8")
    run = "ask-20261027T000000-abc123"
    rl.start(tmp_path, run)
    rl.bind(tmp_path, run, REL, ask_text="a raw ask", plan_rows=[("C1", "first chunk")])
    rl.revise(tmp_path, run, verbatim="an EM revision")
    rl.revise(tmp_path, run, verbatim="a peer revision", sender="peer-1")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = doc["requirement_register"]["rows"]
    assert {r["anchor"].split("@")[0] for r in rows} >= {"pm_verbatims", "ask", "revision"}
    assert validate_frontmatter(doc, rl._SIZING_SCHEMA) == []
