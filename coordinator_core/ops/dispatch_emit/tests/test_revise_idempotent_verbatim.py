"""workflow.revise: a byte-identical verbatim on the same run returns the existing row, mints nothing."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "ask-20261027T000000-abc123"
REL = "state/sizings/2026-10-27-revise-idem.yaml"
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


def test_identical_verbatim_returns_the_same_row_and_mints_none(repo):
    first = _revise(repo, verbatim=WORDS)
    before = (repo / REL).read_bytes()
    second = _revise(repo, verbatim=WORDS)
    assert first["ok"] and second["ok"]
    assert first["item"] == second["item"] == "R2"
    assert (repo / REL).read_bytes() == before
    assert len(_rows(repo)) == 2


def test_whitespace_padded_repeat_is_the_same_verbatim(repo):
    first = _revise(repo, verbatim=WORDS)
    assert _revise(repo, verbatim="  " + WORDS + "\n")["item"] == first["item"]
    assert len(_rows(repo)) == 2


def test_a_different_verbatim_still_mints_a_row(repo):
    _revise(repo, verbatim=WORDS)
    out = _revise(repo, verbatim=WORDS + " too")
    assert out["item"] == "R3" and len(_rows(repo)) == 3


def test_a_ruled_out_revision_row_is_not_reused(repo):
    first = _revise(repo, verbatim=WORDS)
    _revise(repo, verbatim="drop it", out_of_scope=[first["item"]])
    again = _revise(repo, verbatim=WORDS)
    assert again["item"] != first["item"]


def test_a_pm_word_row_is_not_matched_by_a_revision(repo):
    out = _revise(repo, verbatim="lint the thing")
    assert out["item"] == "R2"


def test_identical_verbatim_on_a_held_run_queues_once(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(rl, "_parent_lists_run", lambda root, run_id: True)
    a = rl.revise(tmp_path, "ask-held", verbatim=WORDS)
    b = rl.revise(tmp_path, "ask-held", verbatim=WORDS)
    assert a[2] == b[2] == "Q1"
    assert len(rl._inbox(a[1])) == 1
