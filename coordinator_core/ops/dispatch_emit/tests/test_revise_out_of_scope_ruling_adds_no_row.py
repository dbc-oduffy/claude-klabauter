"""workflow.revise `out_of_scope`: the verbatim is the ruling's quote, adds no register row, and a repeat is a no-op."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token

RUN = "ask-20261016T000000-def456"
REL = "state/sizings/2026-10-16-oos-norow.yaml"
WORDS = "R1 is out of scope, ship the rest"


def _sizing():
    return {
        "schema": "sizing-object", "intent": "do it", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "lint the thing"}, {"turn": 2, "text": "close the thing"}],
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


def test_ruling_quotes_verbatim_and_adds_no_row(repo):
    out = _revise(repo, verbatim=WORDS, out_of_scope=["R1"])
    assert out["ok"] and out["item"] is None and out["withheld"] == ["R1"]
    rows = _rows(repo)
    assert [r["id"] for r in rows] == ["R1", "R2"]
    assert rows[0]["status"] == "deferred"
    ruling = rows[0]["ruling"]
    assert ruling["source"] == "pm" and ruling["quote"] == WORDS
    assert ruling["ref"] == f"workflow.revise:{RUN}:out_of_scope"


def test_identical_second_call_leaves_the_sizing_bytes_unchanged(repo):
    _revise(repo, verbatim=WORDS, out_of_scope=["R1"])
    first = (repo / REL).read_bytes()
    out = _revise(repo, verbatim=WORDS, out_of_scope=["R1"])
    assert out["ok"] and out["item"] is None and out["withheld"] == ["R1"]
    assert (repo / REL).read_bytes() == first
    assert len(_rows(repo)) == 2


def test_refire_reply_is_refire_not_revise_plan(repo, monkeypatch):
    monkeypatch.setattr(run_token, "is_live", lambda root, run_id: False)
    out = _revise(repo, verbatim=WORDS, out_of_scope=["R1"])
    assert out["next"]["kind"] == "refire"


def test_verbatim_without_out_of_scope_still_appends_a_row(repo):
    out = _revise(repo, verbatim="also add a changelog entry")
    assert out["item"] == "R3"
    rows = _rows(repo)
    assert len(rows) == 3 and rows[-1]["anchor"].startswith("revision@")


def test_old_shape_ref_reads_withheld_by_revision(repo):
    def edit(rows):
        rows[0] = {
            **rows[0], "status": "deferred",
            "ruling": {"source": "pm", "quote": WORDS, "ref": f"workflow.revise:{RUN}:R3", "on": "2026-10-16"},
        }
        return rows

    rl._mutate_register(repo, REL, edit)
    ledger = rl.read_ledger(repo, repo / rl.RECORD_DIR_ROOT / RUN)
    assert ledger.withheld[0]["reason"] == "withheld by revision R3"
