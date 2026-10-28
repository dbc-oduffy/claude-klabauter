"""workflow.revise retract_row: withdraws an unclaimed, open revision row; every refusal writes nothing."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

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
def repo(tmp_path):
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


def _retract(repo, row_id, reason="duplicate"):
    return _revise(repo, retract_row={"row_id": row_id, "reason": reason})


def test_retract_withdraws_an_unclaimed_revision_row(repo):
    row = _revise(repo, verbatim=WORDS)["item"]
    out = _retract(repo, row)
    assert out["ok"], out
    assert out["retracted_row"] == {"run_id": RUN, "row_id": row}
    assert [r["id"] for r in _rows(repo)] == ["R1"]
    view = (repo / rl.RECORD_DIR_ROOT / RUN / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert WORDS not in view


def test_retract_adds_no_row(repo):
    row = _revise(repo, verbatim=WORDS)["item"]
    assert _retract(repo, row)["item"] is None


def test_a_retracted_verbatim_can_be_sent_again(repo):
    row = _revise(repo, verbatim=WORDS)["item"]
    _retract(repo, row)
    assert _revise(repo, verbatim=WORDS)["item"] == row


def _refused(repo, row_id, match):
    before = (repo / REL).read_bytes()
    out = _retract(repo, row_id)
    assert "error" in out and match in out["error"], out
    assert (repo / REL).read_bytes() == before


def test_unknown_row_refuses(repo):
    _refused(repo, "R9", "no register row")


def test_a_non_revision_row_refuses(repo):
    _refused(repo, "R1", "not a revision row")


def test_a_claimed_row_refuses(repo):
    row = _revise(repo, verbatim=WORDS)["item"]

    def edit(rows):
        for r in rows:
            if r["id"] == row:
                r["claimed_by"] = ["docs/plans/p.md"]
        return rows

    rl._mutate_register(repo, REL, edit)
    _refused(repo, row, "claimed")


def test_a_row_claimed_only_by_the_plans_register_claims_refuses(repo):
    row = _revise(repo, verbatim=WORDS)["item"]
    sizing_doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    sizing_doc["plan"] = "docs/plans/p.md"
    (repo / REL).write_text(yaml.safe_dump(sizing_doc, sort_keys=False), encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "p.md").write_text(
        f"---\nregister_claims:\n  sizing: {REL}\n  rows: [{row}]\n---\n\nbody\n", encoding="utf-8"
    )
    _refused(repo, row, "register_claims")


def test_a_row_that_is_no_longer_open_refuses(repo):
    row = _revise(repo, verbatim=WORDS)["item"]
    _revise(repo, verbatim="drop it", out_of_scope=[row])
    _refused(repo, row, "deferred")


@pytest.mark.parametrize("bad", [{"row_id": "R2", "reason": ""}, {"reason": "x"}, "R2"])
def test_malformed_retract_refuses(repo, bad):
    _revise(repo, verbatim=WORDS)
    before = (repo / REL).read_bytes()
    out = _revise(repo, retract_row=bad)
    assert "error" in out
    assert (repo / REL).read_bytes() == before


def test_retract_combined_with_a_verbatim_refuses(repo):
    row = _revise(repo, verbatim=WORDS)["item"]
    before = (repo / REL).read_bytes()
    out = _revise(repo, verbatim="more words", retract_row={"row_id": row, "reason": "dup"})
    assert "cannot combine" in out["error"]
    assert (repo / REL).read_bytes() == before


def test_a_held_run_refuses(tmp_path):
    (tmp_path / ".git").mkdir()
    out = rl._handler({"run_id": "ask-held", "retract_row": {"row_id": "R2", "reason": "x"}}, repo_root=tmp_path)
    assert "emit it first" in out["error"]
