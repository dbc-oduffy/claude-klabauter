"""workflow.revise order_rows: validated against the plan spine, recorded to order.jsonl, echoed;
an ordering note with no order_rows replies `unapplied`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_order

RUN = "warp-2026-11-02-x-6fcf46-20261102T200256-bf0de2"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-02-p.md"

_PLAN = """---
title: p
created: 2026-11-02
grouping_approvals:
  do:
    status: pending
  spun_off:
    status: pending
  defer:
    status: pending
  ruled_out:
    status: pending
---

# p

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  writes:
    - pkg/a.py
- id: C4
  title: fourth
  writes:
    - pkg/b.py
```
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "name": "order fixture", "intent": "exercise order_rows", "plan": PLAN_REL,
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-order-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    return tmp_path


def _order_file(repo: Path) -> Path:
    return rl.run_dir(repo, RUN) / run_order.ORDER_FILE


def test_order_rows_alone_is_applied_echoed_and_recorded(repo):
    reply = rl._handler({"run_id": RUN, "order_rows": ["C4"]}, repo_root=repo)
    assert reply["ok"] is True and reply["item"] is None
    assert reply["order_rows"] == ["C4"]
    assert "order_rows" in reply["applied"]
    assert "unapplied" not in reply
    assert run_order.read_for_sizing(repo, RUN, SIZING_REL) == ["C4"]


def test_an_order_of_only_off_spine_ids_is_refused_naming_them_and_writes_nothing(repo):
    reply = rl._handler({"run_id": RUN, "order_rows": ["C98", "C99"]}, repo_root=repo)
    assert "C98" in reply["error"] and "C99" in reply["error"]
    assert not _order_file(repo).exists()


def test_an_off_spine_id_among_valid_ones_lands_in_refused_and_the_rest_is_recorded(repo):
    reply = rl._handler({"run_id": RUN, "order_rows": ["C4", "C99"]}, repo_root=repo)
    assert reply["ok"] is True and reply["order_rows"] == ["C4"]
    assert any(r["input"] == "order_rows" and "C99" in r["reason"] for r in reply["refused"])
    assert run_order.read_for_sizing(repo, RUN, SIZING_REL) == ["C4"]


@pytest.mark.parametrize("bad", ["C4", [4], ["../x"]])
def test_a_malformed_order_is_refused(repo, bad):
    assert "error" in rl._handler({"run_id": RUN, "order_rows": bad}, repo_root=repo)
    assert not _order_file(repo).exists()


def test_an_empty_list_clears_the_recorded_order(repo):
    rl._handler({"run_id": RUN, "order_rows": ["C4"]}, repo_root=repo)
    reply = rl._handler({"run_id": RUN, "order_rows": []}, repo_root=repo)
    assert reply["ok"] is True and reply["order_rows"] == []
    assert run_order.read_for_sizing(repo, RUN, SIZING_REL) == []


def test_an_unemitted_run_refuses_and_creates_nothing(repo):
    with pytest.raises(rl.LedgerRefused):
        rl.revise(repo, "run-never", order_rows=["C4"])
    assert not rl.run_dir(repo, "run-never").exists()


def test_the_empty_precondition_text_lists_order_rows(repo):
    with pytest.raises(rl.LedgerRefused, match="order_rows"):
        rl.revise(repo, RUN)


@pytest.mark.parametrize("field", ["note", "verbatim"])
def test_an_ordering_text_with_no_order_rows_replies_unapplied(repo, field):
    text = "Order C4 ahead of every other remaining row"
    reply = rl._handler({"run_id": RUN, field: text}, repo_root=repo)
    assert reply["unapplied"] == [{"kind": "ordering", "text": text, "use": "order_rows"}]
    assert not _order_file(repo).exists()


def test_the_same_text_with_order_rows_in_the_call_is_not_unapplied(repo):
    reply = rl._handler(
        {"run_id": RUN, "note": "Order C4 ahead of every other row", "order_rows": ["C4"]}, repo_root=repo
    )
    assert reply["ok"] is True and "unapplied" not in reply


@pytest.mark.parametrize("text", ["Order U1x ahead of the rest", "put c4 first", "run C3-F before C3"])
def test_lowercase_and_suffixed_chunk_ids_are_detected_as_unapplied_ordering(repo, text):
    reply = rl._handler({"run_id": RUN, "note": text}, repo_root=repo)
    assert reply["unapplied"] == [{"kind": "ordering", "text": text, "use": "order_rows"}]


@pytest.mark.parametrize("text", ["tighten the docstring", "run the first pass", "C4 needs a test"])
def test_a_note_without_both_an_order_word_and_a_chunk_id_is_not_unapplied(repo, text):
    reply = rl._handler({"run_id": RUN, "note": text}, repo_root=repo)
    assert "unapplied" not in reply
