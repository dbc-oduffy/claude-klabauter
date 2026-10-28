"""workflow.revise on a shipped sizing: the revision row reopens the sizing instead of being refused by the shipped rule."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "ask-20261028T000001-abc125"
REL = "state/sizings/2026-10-28-revise-shipped.yaml"
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


def _ship(repo, rows_edit):
    rl._mutate_register(repo, REL, rows_edit)
    path = repo / REL
    path.write_text(path.read_text(encoding="utf-8").replace("status: sized", "status: shipped", 1), encoding="utf-8")


def _met(rows):
    return [{**r, "status": "met", "wired": True} for r in rows]


def _doc(repo):
    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


def test_revise_on_a_shipped_sizing_is_accepted_and_reopens_it(repo):
    _ship(repo, _met)
    out = rl._handler({"run_id": RUN, "verbatim": WORDS}, repo_root=repo)
    assert out.get("ok"), out
    doc = _doc(repo)
    assert doc["status"] == "routed"
    assert [r["status"] for r in doc["requirement_register"]["rows"]][-1] == "open"
    assert out["item"] == doc["requirement_register"]["rows"][-1]["id"]


def test_a_shipped_sizing_whose_rows_all_hold_stays_shipped(repo):
    _ship(repo, _met)
    rl._mutate_register(repo, REL, lambda rows: [dict(r) for r in rows] + [])
    assert _doc(repo)["status"] == "shipped"


def test_reopening_does_not_excuse_an_otherwise_invalid_row(repo):
    _ship(repo, _met)
    before = (repo / REL).read_bytes()
    with pytest.raises(rl.LedgerRefused):
        rl._mutate_register(repo, REL, lambda rows: [{**rows[0], "surface": "bogus", "status": "open"}, *rows[1:]])
    assert (repo / REL).read_bytes() == before


def test_every_row_the_engine_writes_validates_on_a_shipped_sizing(repo):
    _ship(repo, _met)
    rl.revise(repo, RUN, verbatim=WORDS)
    rows = rr.read_register((repo / REL).read_text(encoding="utf-8")).rows
    assert rows and all(r[rr.ROW_STATUS] in (rr.STATUS_MET, rr.STATUS_OPEN) for r in rows)
    assert rl._validated((repo / REL).read_text(encoding="utf-8"))
