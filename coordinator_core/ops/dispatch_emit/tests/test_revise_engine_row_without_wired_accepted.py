"""workflow.revise onto a shipped sizing whose register is all met and wired lands its open row."""

from __future__ import annotations

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "ask-20261027T000000-abc123"
REL = "state/sizings/2026-10-27-shipped.yaml"


def _sizing(status):
    return {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": status,
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "requirement_register": {
            "sources": [{"doc": REL, "ref": "ask"}],
            "rows": [{
                "id": "R1", "source_text": "first", "anchor": "ask", "surface": "internal",
                "status": "met", "claimed_by": [], "wired": True,
            }],
        },
    }


def _make(tmp_path, status):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_sizing(status), sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def test_revise_on_shipped_sizing_with_unwired_open_row_is_accepted(tmp_path):
    repo = _make(tmp_path, "shipped")
    reply = rl._handler({"run_id": RUN, "verbatim": "a later requirement"}, repo_root=repo)
    assert reply["ok"] is True
    last = _rows(repo)[-1]
    assert (last["source_text"], last["status"], "wired" in last) == ("a later requirement", "open", False)


def test_shipped_sizing_still_refuses_an_unanchored_open_row(tmp_path):
    from coordinator_core.frontmatter.schema_validate import validate_frontmatter

    repo = _make(tmp_path, "shipped")
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["requirement_register"]["rows"].append(
        {"id": "R2", "source_text": "hand edit", "surface": "internal", "status": "open"}
    )
    assert validate_frontmatter(doc, rl._SIZING_SCHEMA)


def test_shipped_sizing_still_refuses_met_without_wired(tmp_path):
    from coordinator_core.frontmatter.schema_validate import validate_frontmatter

    repo = _make(tmp_path, "shipped")
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["requirement_register"]["rows"].append(
        {"id": "R2", "source_text": "x", "anchor": "ask", "surface": "internal", "status": "met"}
    )
    assert validate_frontmatter(doc, rl._SIZING_SCHEMA)
