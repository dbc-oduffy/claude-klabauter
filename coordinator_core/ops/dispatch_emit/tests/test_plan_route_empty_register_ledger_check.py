"""A `route: plan` sizing with no register reaches the composed ledger check holding plan-row ids, not an empty id list."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt, PLAN_ROW_ANCHOR_PREFIX
from coordinator_core.ops.dispatch_emit.ask_gate import _bind_run, _resume_ledger
from coordinator_core.ops.dispatch_emit.ask_stage import _handler as stage_handler
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

RUN = "ask-20261022T000000-abc123"
PLAN_REL = "docs/plans/2026-10-22-route-plan.md"
SIZING_REL = "state/sizings/2026-10-22-route-plan.yaml"
LEDGER_IDS_EXPR = "_gate.resume_ledger?.ledger_item_ids ?? _manifest?.ledger_item_ids ?? null"

PLAN = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: C2
  title: second
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
```
"""


def _sizing() -> dict:
    return {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    }


def _setup(tmp_path, *, with_run: bool = True):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    if with_run:
        rl.start(tmp_path, RUN, all_of_pm_ask=False)


def _register_ids(tmp_path) -> list[str]:
    register = rr.read_register((tmp_path / SIZING_REL).read_text(encoding="utf-8"))
    return [r["id"] for r in register.rows] if register is not None else []


def _stage(tmp_path, **extra) -> dict:
    params = {"run_id": RUN, "plan_path": PLAN_REL, **extra}
    return read_stage_reply(tmp_path, stage_handler(params, repo_root=tmp_path))


def _js_coalesce(*values):
    """JS `a ?? b ?? null`: only null/undefined fall through; an empty list does not."""
    for v in values:
        if v is not None:
            return v
    return None


def refuses_as_empty_register(resume_ledger: dict | None, manifest: dict) -> bool:
    """Mirror of the composed guard for a verify leg that returned no items: refusal fires on empty ids and empty withheld ids."""
    ids = _js_coalesce((resume_ledger or {}).get("ledger_item_ids"), manifest.get("ledger_item_ids"))
    withheld = _js_coalesce((resume_ledger or {}).get("withheld_ids"), manifest.get("withheld_ids"))
    return isinstance(ids, list) and not ids and isinstance(withheld, list) and not withheld


def _composed(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=SIZING_REL, run_id=RUN, session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="x.mjs", **REVIEW_KW,
    )


def test_composed_script_reads_resume_ids_ahead_of_manifest_ids(monkeypatch):
    assert LEDGER_IDS_EXPR in _composed(monkeypatch)


def test_fresh_route_plan_run_is_seeded_and_does_not_refuse(tmp_path):
    _setup(tmp_path)
    assert _bind_run(tmp_path, RUN, SIZING_REL) is None
    assert _register_ids(tmp_path) == []
    manifest = _stage(tmp_path, commit_sizing_path=SIZING_REL)
    assert "error" not in manifest
    rows = rr.read_register((tmp_path / SIZING_REL).read_text(encoding="utf-8")).rows
    assert manifest["ledger_item_ids"]
    assert [r["anchor"] for r in rows] == [f"{PLAN_ROW_ANCHOR_PREFIX}C1", f"{PLAN_ROW_ANCHOR_PREFIX}C2"]
    assert manifest["ledger_item_ids"] == [r["id"] for r in rows]
    assert not refuses_as_empty_register(None, manifest)


@pytest.mark.pending_fix
def test_l1_empty_resume_ledger_ids_do_not_shadow_the_minted_manifest_ids(tmp_path):
    _setup(tmp_path)
    _bind_run(tmp_path, RUN, SIZING_REL)
    sizing_at_gate = yaml.safe_load((tmp_path / SIZING_REL).read_text(encoding="utf-8"))
    halt = LedgerHalt(run_id=RUN, plan_rel=PLAN_REL, sizing_rel=SIZING_REL, blocking=())
    resume = _resume_ledger(sizing_at_gate, halt, tmp_path, run_id=RUN, sizing_rel=SIZING_REL)
    assert resume["ledger_item_ids"] == []
    manifest = _stage(tmp_path, commit_sizing_path=SIZING_REL)
    assert manifest["ledger_item_ids"], "stage minted plan-row ids"
    assert not refuses_as_empty_register(resume, manifest)


def test_l2_stage_reply_carries_ledger_item_ids_when_the_plan_is_authored_in_run(tmp_path):
    _setup(tmp_path)
    _bind_run(tmp_path, RUN, SIZING_REL)
    manifest = _stage(tmp_path, commit_sizing_path=SIZING_REL)
    assert isinstance(manifest.get("ledger_item_ids"), list) and manifest["ledger_item_ids"]


@pytest.mark.pending_fix
def test_l3_stage_mints_the_plan_rows_when_the_gate_bound_no_run_json(tmp_path):
    _setup(tmp_path, with_run=False)
    assert _bind_run(tmp_path, RUN, SIZING_REL) is None
    manifest = _stage(tmp_path, commit_sizing_path=SIZING_REL)
    assert manifest["ledger_item_ids"] == [] and _register_ids(tmp_path) == []
    assert not refuses_as_empty_register(None, manifest)


@pytest.mark.pending_fix
def test_l4_stage_mints_the_plan_rows_when_the_plan_names_no_sizing(tmp_path):
    _setup(tmp_path)
    _bind_run(tmp_path, RUN, SIZING_REL)
    manifest = _stage(tmp_path)
    assert manifest["ledger_item_ids"] and len(_register_ids(tmp_path)) == 2
    assert not refuses_as_empty_register(None, manifest)
