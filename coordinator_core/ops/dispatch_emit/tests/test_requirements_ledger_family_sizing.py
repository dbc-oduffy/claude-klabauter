"""A sizing carrying top-level `family:` still takes a revise verbatim; other unknown keys still refuse."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from pathlib import Path

REL = "state/sizings/2026-10-01-family.yaml"
RUN = "ask-20261001T000000-cccccc"


def _repo(tmp_path, monkeypatch, **extra):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    doc = {
        "schema": "sizing-object", "name": "family", "intent": "exercise family",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-family-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make it so"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="the ask")
    # Added after bind so only the revise write validates the family-carrying sizing.
    path = tmp_path / REL
    text = path.read_text(encoding="utf-8")
    path.write_text(text + yaml.safe_dump(extra), encoding="utf-8", newline="\n")
    return tmp_path


def _texts(repo):
    return [r["source_text"] for r in rr.read_register((repo / REL).read_text(encoding="utf-8")).rows]


def test_verbatim_records_on_a_family_sizing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch, family="agy")
    before = _texts(repo)
    rl.revise(repo, RUN, verbatim="do the other thing")
    assert len(_texts(repo)) == len(before) + 1
    assert yaml.safe_load((repo / REL).read_text(encoding="utf-8"))["family"] == "agy"


def test_other_unknown_key_still_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch, bogus_key="x")
    with pytest.raises(rl.LedgerRefused, match="bogus_key"):
        rl.revise(repo, RUN, verbatim="do the other thing")
