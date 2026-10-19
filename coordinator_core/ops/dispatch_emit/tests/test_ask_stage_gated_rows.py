"""ask_stage stages ungated rows and lists gated rows in the manifest instead of refusing the run."""

from __future__ import annotations

import time

from coordinator_core.ops.dispatch_emit.ask_stage import _handler

PLAN_REL = "docs/plans/2026-10-03-gated.md"


def _plan(gate_block: str, extra_dep: bool = False) -> str:
    dep = "  depends_on:\n    - chunk: G1\n" if extra_dep else ""
    default_dep = "  depends_on:\n    - chunk: U1\n"
    return f"""---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: U1
  title: ungated
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: G1
  title: gated
  change_kind: code-edit
  surface: pkg/g.py
  writes:
    - pkg/g.py
{gate_block}
- id: T1
  title: dependent
  change_kind: code-edit
  surface: pkg/t.py
  writes:
    - pkg/t.py
{dep or default_dep}```
"""


def _stage(tmp_path, plan: str):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)


def _gate(requires: str, blocks: str | None = None) -> str:
    b = f"      blocks: {blocks}\n" if blocks else ""
    return f"  external_gate:\n    - owner_repo: other\n      requires: {requires}\n{b}".rstrip("\n")


def test_landed_work_gate_withholds_row_into_manifest(tmp_path):
    start = time.process_time()
    reply = _stage(tmp_path, _plan(_gate("landed-work")))
    assert (time.process_time() - start) * 1000 < 500
    assert "error" not in reply
    assert [r["id"] for r in reply["rows"]] == ["U1", "T1"]
    assert [(g["id"], g["reason"]) for g in reply["gated"]] == [("G1", "external_gate")]


def test_commit_in_owner_repo_gate_behaves_identically(tmp_path):
    reply = _stage(tmp_path, _plan(_gate("commit-in-owner-repo")))
    assert [r["id"] for r in reply["rows"]] == ["U1", "T1"]
    assert [g["id"] for g in reply["gated"]] == ["G1"]


def test_ac_closure_gate_is_staged_as_an_ordinary_row(tmp_path):
    reply = _stage(tmp_path, _plan(_gate("landed-work", "ac-closure")))
    assert "G1" in [r["id"] for r in reply["rows"]]
    assert reply["gated"] == []


def _owned_gate(owner: str) -> str:
    return f"  external_gate:\n    - owner_repo: {owner}\n      requires: landed-work"


def _patch_registry(monkeypatch, table, aliases=None):
    import coordinator_core.machine_resolver as mr
    import coordinator_core.ops.fleet._memo_resolver as memo

    monkeypatch.setattr(mr, "registry_get", lambda key: table.get(key))
    monkeypatch.setattr(memo, "read_receiver_aliases", lambda: aliases or {})


def test_gated_row_carries_the_registered_owner_root(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {"repos.claude_klabauter": "OWNER/ROOT"})
    reply = _stage(tmp_path, _plan(_owned_gate("claude-klabauter")))
    assert [(g["id"], g["owner_root"]) for g in reply["gated"]] == [("G1", "OWNER/ROOT")]


def test_gated_row_without_a_registry_entry_has_empty_owner_root(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {})
    reply = _stage(tmp_path, _plan(_owned_gate("claude-klabauter")))
    assert reply["gated"][0]["owner_root"] == ""


def test_aliased_owner_resolves_through_the_alias_key(tmp_path, monkeypatch):
    _patch_registry(
        monkeypatch, {"repos.example_game_workbench_repo": "HOLO/ROOT"}, {"example-game-repo": "example_game_workbench_repo"}
    )
    reply = _stage(tmp_path, _plan(_owned_gate("example-game-repo")))
    assert reply["gated"][0]["owner_root"] == "HOLO/ROOT"


def test_owner_root_resolution_spawns_no_process(tmp_path, monkeypatch):
    import subprocess

    _patch_registry(monkeypatch, {"repos.claude_klabauter": "OWNER/ROOT"})

    def _boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    reply = _stage(tmp_path, _plan(_owned_gate("claude-klabauter")))
    assert reply["gated"][0]["owner_root"] == "OWNER/ROOT"


def test_owner_root_round_trips_through_json():
    from coordinator_core.ops.dispatch_emit.ask_contract import GatedRow

    row = GatedRow(id="G1", reason="external_gate", gate="g", owner_repo="x", owner_root="R")
    assert GatedRow.from_json(row.to_json()) == row
    assert GatedRow.from_json({"id": "G1", "reason": "r", "gate": "g"}).owner_root == ""


def _em_plan() -> str:
    return """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: U1
  title: ungated
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: C3
  title: merge
  change_kind: code-edit
  surface: pkg/m.py
  performer: em
  writes:
    - pkg/m.py
- id: C4
  title: memo
  change_kind: doc-edit
  kind: memo-send
  memo_to: coordinator-content-repo
  surface: pkg/memo.md
  receipt: .coordinator-local/memo-outbox/sent/the-topic.md
  writes:
    - pkg/memo.md
- id: C5
  title: after merge
  change_kind: code-edit
  surface: pkg/d.py
  writes:
    - pkg/d.py
  depends_on:
    - chunk: C3
      gate_kind: output-consumption-runtime
      note: consumes the merge
```
"""


def test_em_owned_rows_land_in_owed_not_gated(tmp_path):
    reply = _stage(tmp_path, _em_plan())
    assert "error" not in reply
    assert [(o["id"], o["reason"]) for o in reply["owed"]] == [
        ("C3", "em-performed"),
        ("C4", "memo-send"),
        ("C5", "withheld_by_owed_dependency"),
    ]
    assert reply["gated"] == []


def test_plan_without_em_owned_rows_writes_empty_owed(tmp_path):
    reply = _stage(tmp_path, _plan(_gate("landed-work")))
    assert reply["owed"] == []


def test_zero_waves_error_names_owed_rows(tmp_path):
    full = _em_plan()
    only_em = full[: full.index("- id: U1")] + full[full.index("- id: C3") : full.index("- id: C5")] + "```\n"
    reply = _stage(tmp_path, only_em)
    assert "owed by the EM: C3, C4" in reply["error"]


def test_dependent_of_gated_row_is_gated_transitively(tmp_path):
    reply = _stage(tmp_path, _plan(_gate("landed-work"), extra_dep=True))
    assert [r["id"] for r in reply["rows"]] == ["U1"]
    assert [(g["id"], g["reason"]) for g in reply["gated"]] == [
        ("G1", "external_gate"),
        ("T1", "transitive_gate_closure"),
    ]
