"""workflow.revise: a revision naming existing plan rows binds to them (no unclaimed row), and run_live /
briefed tell the truth on a live, a resumed-and-firing and a halted run."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import halted_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from pathlib import Path

RUN = "ask-20261121T000000-abc123"
SIBLING = "warp-2026-11-21-x-20261121T010000-def456"
REL = "state/sizings/2026-11-21-binds.yaml"
PLAN = "docs/plans/2026-11-21-binds.md"

_SPINE = """- id: C1
  title: first
  change_kind: script-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
  register_rows: [R1]
  disposition: open
- id: C2
  title: second
  change_kind: script-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  disposition: open
- id: C3
  title: dropped
  change_kind: script-edit
  surface: pkg/c.py
  writes:
    - pkg/c.py
  disposition: wont_do
"""


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}], "plan": PLAN,
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    fm = {"title": "p", "register_claims": {"sizing": REL, "rows": ["R1"], "pins": {"R1": rr.row_pin("build the op")}}}
    (tmp_path / PLAN).write_text(
        "---\n" + yaml.safe_dump(fm) + "---\n# p\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8", newline="\n",
    )
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _revise(root: Path, verbatim: str) -> dict:
    return rl._handler({"run_id": RUN, "verbatim": verbatim}, repo_root=root)


def _spine_rows(root: Path) -> dict:
    rows = rl._plan_rows(root, REL)
    return {r["id"]: r.get("register_rows") for r in rows}


def _claimed(root: Path) -> list:
    return rr.plan_claims((root / PLAN).read_text(encoding="utf-8").split("---")[1])[1]


def test_a_revision_naming_existing_plan_rows_binds_to_them_and_mints_no_unclaimed_row(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = _revise(root, "C2 must also cover the Windows path")
    assert out["ok"] is True and out["item"] == "R2" and out["claimed"] == ["R2"]
    assert "unclaimed_rows" not in out and "warning" not in out
    assert _spine_rows(root) == {"C1": ["R1"], "C2": ["R2"], "C3": None}
    assert set(_claimed(root)) == {"R1", "R2"}
    assert "claimed" in out["applied"]


def test_a_claim_that_fails_after_the_stamp_names_the_stranded_row(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)

    def refuse(*_a, **_k):
        raise rl.LedgerRefused("plan locked")

    monkeypatch.setattr(rl, "_claim_ruling", refuse)
    out = _revise(root, "C2 must also cover the Windows path")
    assert out["ok"] is True and "claimed" not in out
    assert "R2" in out["warning"] and "plan locked" in out["warning"]


def test_a_revision_names_several_rows_and_binds_each(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = _revise(root, "C1 and C2 both need the Windows path")
    assert out["claimed"] == ["R2"] and "unclaimed_rows" not in out
    assert _spine_rows(root) == {"C1": ["R1", "R2"], "C2": ["R2"], "C3": None}


def test_a_revision_naming_no_plan_row_still_mints_an_unclaimed_row(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = _revise(root, "also cover the Windows path (see C20 and AC2)")
    assert out["item"] == "R2" and "claimed" not in out
    assert out["unclaimed_rows"] == ["R2"]
    assert _spine_rows(root) == {"C1": ["R1"], "C2": None, "C3": None}
    assert set(_claimed(root)) == {"R1"}


def test_a_dropped_plan_row_is_not_a_binding_target(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = _revise(root, "C3 must also cover the Windows path")
    assert out["unclaimed_rows"] == ["R2"] and "claimed" not in out
    assert _spine_rows(root)["C3"] is None


def _live_token(monkeypatch, *, own: bool = False, sibling: bool = False) -> None:
    monkeypatch.setattr(run_token, "is_live", lambda _r, run_id: own and run_id == RUN)
    records = [{"run_id": SIBLING, "sizing_rel": REL}] if sibling else []
    monkeypatch.setattr(run_token, "live_records", lambda _r: iter(records))


def _briefed_manifest(root: Path) -> None:
    manifest = {"rows": [{"id": "C1", "brief_path": "briefs/C1.md"}]}
    (record_dir(root, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_a_live_run_reports_run_live_true(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    _live_token(monkeypatch, own=True)
    out = _revise(root, "also cover the Windows path")
    assert out["run_live"] is True and "run live" in out["note"]


def test_a_halted_run_with_a_live_sibling_is_not_live_and_not_briefed(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    _briefed_manifest(root)
    halted_run.write_halt_marker(record_dir(root, RUN), halted="stalled", sizing_rel=REL)
    _live_token(monkeypatch, own=True, sibling=True)
    out = _revise(root, "also cover the Windows path")
    assert out["run_live"] is False
    assert "briefed" not in out and "run live" not in out["note"]


def test_a_resumed_run_with_a_live_fire_is_live_and_one_without_is_not(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    dir_ = record_dir(root, RUN)
    halted_run.write_halt_marker(dir_, halted="stalled", sizing_rel=REL)
    halted_run.mark_resumed(dir_)
    _live_token(monkeypatch)
    monkeypatch.setattr(halted_run, "fire_live", lambda _r, _id: True)
    live = _revise(root, "also cover the Windows path")
    assert live["run_live"] is True and "run live" in live["note"]
    monkeypatch.setattr(halted_run, "fire_live", lambda _r, _id: False)
    dead = _revise(root, "also cover macOS")
    assert dead["run_live"] is False and "briefed" not in dead
