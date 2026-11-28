"""A revise `verbatim` naming a repo file outside a staged run's footprint mints a W row or is refused, never stored silently."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

RUN = "ask-20261126T000000-abc123"
REL = "state/sizings/2026-11-26-outside-verbatim.yaml"
PLAN_REL = "docs/plans/2026-11-26-outside-verbatim.md"
INSIDE = "pkg/inside.py"
OUTSIDE = "pkg/outside.py"

PLAN = f"""---
deliverable_id: dlv-outsideverbatim-abc123
---
# Outside verbatim

## Tasks

```yaml plan-tasks
- id: C7
  title: probe
  change_kind: code-edit
  surface: {INSIDE}
  writes:
    - {INSIDE}
```
"""


def _stage(repo: Path) -> None:
    manifest = {"doc_writes": [], "rows": [{"id": "C7", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(repo, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _files(repo: Path) -> None:
    for rel in (INSIDE, OUTSIDE):
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")


@pytest.fixture
def bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A halted, staged run bound to a sizing whose readable plan holds the one-file row C7."""
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda *a, **k: None)
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "do x"}], "plan": PLAN_REL,
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    write_run_scripts(tmp_path, RUN)
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    record_dir = tmp_path / RECORD_DIR_ROOT / RUN
    record_dir.mkdir(parents=True, exist_ok=True)
    halted_run.write_halt_marker(
        record_dir, halted="dependents_unstarted", sizing_rel=REL, plan_rel=PLAN_REL, reason="C8 held on C7",
    )
    _files(tmp_path)
    _stage(tmp_path)
    return tmp_path


@pytest.fixture
def unbound(tmp_path: Path) -> Path:
    """A staged run with no sizing, hence no plan a W row could land in."""
    (tmp_path / ".git").mkdir()
    _files(tmp_path)
    rl.start(tmp_path, RUN)
    _stage(tmp_path)
    return tmp_path


def _register_texts(repo: Path) -> list[str]:
    from coordinator_core.ops import requirement_register as rr

    return [r["source_text"] for r in rr.read_register((repo / REL).read_text(encoding="utf-8")).rows]


def test_verbatim_naming_an_outside_file_mints_a_w_row_and_stores_the_requirement(bound: Path) -> None:
    text = f"the fix lies in {OUTSIDE}; fix {OUTSIDE} so C8 can proceed"
    outcome: dict = {}
    rl.revise(bound, RUN, verbatim=text, outcome=outcome)
    plan = (bound / PLAN_REL).read_text(encoding="utf-8")
    assert "id: W1" in plan and OUTSIDE in plan
    assert outcome["footprint_widened"] == [{"row": "W1", "paths": [OUTSIDE]}]
    assert text in _register_texts(bound)
    assert "verbatim_refused" not in outcome


def test_verbatim_naming_a_covered_file_widens_nothing(bound: Path) -> None:
    outcome: dict = {}
    rl.revise(bound, RUN, verbatim=f"also fix {INSIDE}", outcome=outcome)
    assert "id: W1" not in (bound / PLAN_REL).read_text(encoding="utf-8")
    assert "footprint_widened" not in outcome and f"also fix {INSIDE}" in _register_texts(bound)


def test_verbatim_citing_an_outside_file_without_an_edit_verb_is_stored_unchecked(bound: Path) -> None:
    rl.revise(bound, RUN, verbatim=f"the premise was read from {OUTSIDE}")
    assert "id: W1" not in (bound / PLAN_REL).read_text(encoding="utf-8")
    assert f"the premise was read from {OUTSIDE}" in _register_texts(bound)


def test_unwidenable_verbatim_is_refused_in_outcome_and_stored_nowhere(unbound: Path) -> None:
    outcome: dict = {}
    _, _, added = rl.revise(unbound, RUN, verbatim=f"fix {OUTSIDE}", outcome=outcome)
    refused = outcome["verbatim_refused"]
    assert refused["paths"] == [OUTSIDE] and OUTSIDE in refused["reason"] and "frozen" in refused["reason"]
    assert added is None and rl._inbox(rl.run_dir(unbound, RUN)) == []


def test_unwidenable_verbatim_without_an_outcome_raises_loudly(unbound: Path) -> None:
    with pytest.raises(rl.LedgerRefused) as exc:
        rl.revise(unbound, RUN, verbatim=f"fix {OUTSIDE}")
    assert OUTSIDE in str(exc.value)
    assert rl._inbox(rl.run_dir(unbound, RUN)) == []


def test_handler_reply_carries_a_refused_verbatim_entry_and_no_refire(unbound: Path) -> None:
    reply = rl._handler({"run_id": RUN, "verbatim": f"fix {OUTSIDE}"}, repo_root=unbound)
    entries = [r for r in reply["refused"] if r.get("input") == "verbatim"]
    assert len(entries) == 1 and entries[0]["paths"] == [OUTSIDE] and OUTSIDE in entries[0]["reason"]
    assert "next" not in reply and "row" not in "".join(reply.get("applied", ()))


def test_unwidenable_verbatim_with_out_of_scope_is_a_ruling_quote_and_is_not_checked(unbound: Path) -> None:
    outcome: dict = {}
    with pytest.raises(rl.LedgerRefused) as exc:
        rl.revise(unbound, RUN, verbatim=f"fix {OUTSIDE}", out_of_scope=["R1"], outcome=outcome)
    assert "verbatim_refused" not in outcome and OUTSIDE not in str(exc.value)
