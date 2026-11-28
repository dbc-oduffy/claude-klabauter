"""workflow.revise retract_row: withdraws an unclaimed, open revision row; every refusal writes nothing."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from pathlib import Path

RUN = "ask-20261027T000001-abc124"
REL = "state/sizings/2026-10-27-revise-retract.yaml"
WORDS = "also add a changelog entry"


def _sizing():
    return {
        "schema": "sizing-object", "intent": "do it", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "lint the thing"}],
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def _revise(repo, **params):
    return rl._handler({"run_id": RUN, **params}, repo_root=repo)


def _retract(repo, row_id, reason: str = "duplicate"):
    return _revise(repo, retract_row={"row_id": row_id, "reason": reason})


def test_retract_withdraws_an_unclaimed_revision_row(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    out = _retract(repo, row)
    assert out["ok"], out
    assert out["retracted_row"] == {"run_id": RUN, "row_id": row}
    assert [r["id"] for r in _rows(repo)] == ["R1"]
    view = (repo / rl.RECORD_DIR_ROOT / RUN / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert WORDS not in view


def test_retract_adds_no_row(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    assert _retract(repo, row)["item"] is None


def test_a_retracted_verbatim_can_be_sent_again(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    _retract(repo, row)
    assert _revise(repo, verbatim=WORDS)["item"] == row


def _refused(repo, row_id, match) -> None:
    before = (repo / REL).read_bytes()
    out = _retract(repo, row_id)
    assert "error" in out and match in out["error"], out
    assert (repo / REL).read_bytes() == before


def test_unknown_row_refuses(repo) -> None:
    _refused(repo, "R9", "no register row")


def test_a_pm_verbatim_row_refuses(repo) -> None:
    _refused(repo, "R1", "not a revision or relayed row")


def _add_relayed_row(repo, text: str = "Context files: a.md") -> str:
    def edit(rows):
        rid = rl._next_id(rows)
        rows.append({
            "id": rid, "source_text": text, "anchor": rl.ORIGIN_RELAYED, "surface": rl.ROW_SURFACE,
            "status": rr.STATUS_OPEN, "claimed_by": [],
        })
        return rows

    rl._mutate_register(repo, REL, edit)
    return _rows(repo)[-1]["id"]


def test_retract_withdraws_an_open_unclaimed_relayed_row(repo) -> None:
    row = _add_relayed_row(repo)
    out = _retract(repo, row)
    assert out["ok"], out
    assert [r["id"] for r in _rows(repo)] == ["R1"]


def test_a_claimed_relayed_row_refuses(repo) -> None:
    row = _add_relayed_row(repo)
    _edit_row(repo, row, claimed_by=["docs/plans/p.md"])
    _refused(repo, row, "claimed")


def test_a_raw_ask_row_refuses(repo) -> None:
    _edit_row(repo, "R1", anchor=rl.ORIGIN_ASK)
    _refused(repo, "R1", "not a revision or relayed row")


def test_a_claimed_row_refuses(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]

    def edit(rows):
        for r in rows:
            if r["id"] == row:
                r["claimed_by"] = ["docs/plans/p.md"]
        return rows

    rl._mutate_register(repo, REL, edit)
    _refused(repo, row, "claimed")


def test_a_row_claimed_only_by_the_plans_register_claims_refuses(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    sizing_doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    sizing_doc["plan"] = "docs/plans/p.md"
    (repo / REL).write_text(yaml.safe_dump(sizing_doc, sort_keys=False), encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "p.md").write_text(
        f"---\nregister_claims:\n  sizing: {REL}\n  rows: [{row}]\n---\n\nbody\n", encoding="utf-8"
    )
    _refused(repo, row, "register_claims")


def test_a_row_that_is_no_longer_open_refuses(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    _revise(repo, verbatim="drop it", out_of_scope=[row])
    _refused(repo, row, "deferred")


@pytest.mark.parametrize("bad", [{"row_id": "R2", "reason": ""}, {"reason": "x"}, "R2"])
def test_malformed_retract_refuses(repo, bad) -> None:
    _revise(repo, verbatim=WORDS)
    before = (repo / REL).read_bytes()
    out = _revise(repo, retract_row=bad)
    assert "error" in out
    assert (repo / REL).read_bytes() == before


def test_retract_combined_with_a_verbatim_refuses(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    before = (repo / REL).read_bytes()
    out = _revise(repo, verbatim="more words", retract_row={"row_id": row, "reason": "dup"})
    assert "cannot combine" in out["error"]
    assert (repo / REL).read_bytes() == before


def _garbage(repo, row_id, reason: str = "engine garbage"):
    return _revise(repo, retract_row={"row_id": row_id, "reason": reason, rl.GARBAGE_KEY: True})


def _edit_row(repo, row, **fields) -> None:
    def edit(rows):
        for r in rows:
            if r["id"] == row:
                r.update(fields)
        return rows

    rl._mutate_register(repo, REL, edit)


def test_engine_garbage_drops_a_claimed_row_with_no_pm_verbatim(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    _edit_row(repo, row, claimed_by=["docs/plans/p.md"])
    out = _garbage(repo, row)
    assert out["ok"] and out["retracted_row"] == {"run_id": RUN, "row_id": row}, out
    assert [r["id"] for r in _rows(repo)] == ["R1"]
    view = (repo / rl.RECORD_DIR_ROOT / RUN / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert WORDS not in view


def test_engine_garbage_drops_a_non_open_row(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    _revise(repo, verbatim="drop it", out_of_scope=[row])
    assert _garbage(repo, row)["ok"]
    assert [r["id"] for r in _rows(repo)] == ["R1"]


def test_engine_garbage_drops_the_row_from_the_plans_register_claims(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    sizing_doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    sizing_doc["plan"] = "docs/plans/p.md"
    (repo / REL).write_text(yaml.safe_dump(sizing_doc, sort_keys=False), encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True)
    plan = repo / "docs" / "plans" / "p.md"
    plan.write_text(
        f"---\nregister_claims:\n  sizing: {REL}\n  rows: [R1, {row}]\n  pins: {{R1: x, {row}: y}}\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert _garbage(repo, row)["ok"]
    assert [r["id"] for r in _rows(repo)] == ["R1"]
    claims = yaml.safe_load(plan.read_text(encoding="utf-8").split("---")[1])["register_claims"]
    assert claims["rows"] == ["R1"] and set(claims["pins"]) == {"R1"}
    assert plan.read_text(encoding="utf-8").endswith("---\n\nbody\n")


def test_engine_garbage_never_drops_a_pm_row(repo) -> None:
    before = (repo / REL).read_bytes()
    out = _garbage(repo, "R1")
    assert "error" in out and "PM requirement" in out["error"], out
    assert (repo / REL).read_bytes() == before


def test_engine_garbage_needs_a_reason_and_a_boolean_flag(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    before = (repo / REL).read_bytes()
    assert "reason is empty" in _garbage(repo, row, reason="")["error"]
    bad = _revise(repo, retract_row={"row_id": row, "reason": "x", rl.GARBAGE_KEY: "yes"})
    assert "boolean" in bad["error"]
    assert (repo / REL).read_bytes() == before


def test_without_the_flag_a_claimed_row_still_refuses(repo) -> None:
    row = _revise(repo, verbatim=WORDS)["item"]
    _edit_row(repo, row, claimed_by=["docs/plans/p.md"])
    _refused(repo, row, "claimed")


def test_a_held_run_refuses(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    out = rl._handler({"run_id": "ask-held", "retract_row": {"row_id": "R2", "reason": "x"}}, repo_root=tmp_path)
    assert "emit it first" in out["error"]
