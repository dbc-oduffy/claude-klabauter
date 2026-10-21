"""A DoE-cwd ledger whose engine-owned row's symbol exists only in the engine root is judged there."""

from __future__ import annotations

import copy

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, engine_owned_rows
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.engine_owned_rows import engine_owner_rows, routed_roots
from coordinator_core.ops.dispatch_emit.requirements_ledger import Item, Ledger, judge
from coordinator_core.ops.dispatch_emit.tests.conftest import _V5_ROSTER_FRAGMENT, _V5_STAGE_SCHEMAS

ENGINE_CLASS = "resolved-engine"
PLAN_REL = "docs/plans/2026-10-14-doe-engine-row.md"
ENGINE_FILE = "coordinator_core/ops/sizing_intake.py"
PLAN = f"""---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: T1
  title: cwd-owned
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: T8
  title: engine-delivered
  change_kind: code-edit
  surface: {ENGINE_FILE}
  writes:
    - {ENGINE_FILE}
```
"""
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A DoE-like cwd (no coordinator_core/) and an engine root holding the only `intake_mint`."""
    cwd = tmp_path / "doe"
    (cwd / ".git").mkdir(parents=True)
    (cwd / "docs" / "plans").mkdir(parents=True)
    (cwd / "pkg").mkdir()
    (cwd / "pkg" / "a.py").write_text("def cwd_only(): ...\n", encoding="utf-8", newline="\n")
    (cwd / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    engine = tmp_path / "engine"
    (engine / "coordinator_core" / "ops").mkdir(parents=True)
    (engine / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8", newline="\n")
    (engine / ENGINE_FILE).write_text("def intake_mint(): ...\n", encoding="utf-8", newline="\n")
    root = engine.as_posix()
    monkeypatch.setattr(engine_owned_rows, "resolve_engine_root", lambda: (root, ENGINE_CLASS))
    return cwd, root


REGISTER = [
    {"id": "T8", "source_text": f"intake_mint in {ENGINE_FILE} :: intake_mint mints the sizing", "met_by": None},
    {"id": "T1", "source_text": "pkg/a.py :: cwd_only stays", "met_by": None},
]


def test_engine_owner_rows_routes_the_engine_row_with_root_and_class(world):
    cwd, root = world
    rows = engine_owner_rows([], REGISTER, "", cwd)
    assert [r["id"] for r in rows] == ["T8"]
    assert rows[0]["owner_root"] == root
    assert rows[0]["root_class"] == ENGINE_CLASS
    assert rows[0]["engine"] is True
    assert rows[0]["owner_repo"] == "claude-klabauter"


def test_stage_manifest_carries_engine_rows_for_the_engine_row_only(world):
    cwd, root = world
    reply = read_stage_reply(cwd, _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=cwd))
    assert "error" not in reply
    assert [r["id"] for r in reply["engine_rows"]] == ["T8"]
    assert routed_roots(reply) == {"T8": root}


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    roster = copy.deepcopy(_V5_ROSTER_FRAGMENT)
    roster["execute_review"]["stages"][1]["agents"].append(
        {"agentType": "coordinator:delivery-verifier", "model": "opus", "effort": "low", "per": "whole-diff", "schema": "wave"}
    )
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        review_roster_fragment=roster,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )


def test_verify_prompt_carries_the_engine_clause_and_root_read(monkeypatch):
    script = _script(monkeypatch)
    assert "label: 'ledger-check'" not in script
    verify_end = script.index("label: 'verify'")
    verify = script[script.rindex("agent(", 0, verify_end) : verify_end]
    assert "engine-plane row" in verify
    assert "root_read" in verify
    assert "root_read" in str(ask_compose._LEDGER_CHECK_SCHEMA)


def test_the_folded_judge_carries_the_engine_clause_and_root_read(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    roster = copy.deepcopy(_V5_ROSTER_FRAGMENT)
    roster["execute_review"]["stages"][1]["agents"].append(
        {"agentType": "coordinator:delivery-verifier", "model": "opus", "effort": "low", "per": "whole-diff", "schema": "wave"}
    )
    roster["execute_review"]["stages"].append(
        {"kind": "judge", "agents": [{"agentType": "coordinator:criterion-judge", "model": "opus", "effort": "low", "schema": "judge"}]}
    )
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
        review_roster_fragment=roster, review_stage_schemas={**_V5_STAGE_SCHEMAS, "judge": {"type": "object"}},
    )
    assert "label: 'ledger-check'" not in script
    verify_end = script.index("label: 'verify'")
    verify = script[script.rindex("agent(", 0, verify_end) : verify_end]
    assert "engine-plane row" in verify and "root_read" in verify
    assert '"root_read"' in script[verify_end:].split("\n", 1)[0]


def _ledger() -> Ledger:
    return Ledger(
        items=(
            Item("T8", "engine row", "ask", "2026-10-14T00:00:00Z"),
            Item("T1", "cwd row", "ask", "2026-10-14T00:00:00Z"),
        ),
        all_of_pm_ask=False,
    )


def test_verdict_naming_the_engine_root_passes(world):
    _, root = world
    verdict = {"items": [
        {"id": "T8", "status": "met", "root_read": root},
        {"id": "T1", "status": "met"},
    ]}
    assert judge(_ledger(), verdict, routed={"T8": root}) == {"pass": True, "failures": [], "open": []}


def test_verdict_naming_the_cwd_root_for_an_engine_row_fails_as_misrouted(world):
    cwd, root = world
    verdict = {"items": [
        {"id": "T8", "status": "met", "root_read": cwd.as_posix()},
        {"id": "T1", "status": "met"},
    ]}
    result = judge(_ledger(), verdict, routed={"T8": root})
    assert result["pass"] is False
    assert [f["id"] for f in result["failures"]] == ["T8"]
    assert "owed the engine root" in result["failures"][0]["reason"]


def test_a_ledger_with_no_engine_rows_judges_identically_with_and_without_routed():
    verdict = {"items": [{"id": "T8", "status": "not_met"}, {"id": "T1", "status": "met"}]}
    assert judge(_ledger(), verdict, routed={}) == judge(_ledger(), verdict)
    assert judge(_ledger(), verdict, routed=None) == judge(_ledger(), verdict)


def test_routed_roots_reads_a_manifest_and_tolerates_absence():
    assert routed_roots({}) == {}
    assert routed_roots({"engine_rows": [{"id": "T8", "owner_root": "R"}]}) == {"T8": "R"}


def test_resolve_engine_root_degrades_to_unresolved(monkeypatch):
    def _miss():
        raise RuntimeError("no engine")

    monkeypatch.setattr(engine_owned_rows.engine_root, "coordinator_engine_root_with_class", _miss)
    assert engine_owned_rows.resolve_engine_root() == ("", "unresolved")
