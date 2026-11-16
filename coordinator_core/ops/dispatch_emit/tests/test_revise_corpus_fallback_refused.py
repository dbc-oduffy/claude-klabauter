"""workflow.revise refuses a note or verbatim that falls back from the corpus to engine-source reads, before any write."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core import ipc
from coordinator_core.ops import corpus_fallback
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

REL = "state/sizings/2026-10-01-fallback.yaml"
RUN = "ask-20261001T000000-cccccc"
FALLBACK_NOTE = "if example-retrieval-repo is unreachable, fall back to engine-source reads"
BENIGN_NOTE = "example-retrieval-repo was ECONNREFUSED; stop and memo example-retrieval-repo-em"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    doc = {
        "schema": "sizing-object", "name": "fallback", "intent": "exercise the fallback refusal",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fallback-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make it so"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="the ask")
    return tmp_path


def _revise(repo, **params):
    return ipc._REGISTRY["workflow.revise"]({"run_id": RUN, **params}, repo)


def _notes_bytes(repo) -> bytes | None:
    path = rl.run_dir(repo, RUN) / rl.NOTES_FILE
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize("field", ["note", "verbatim"])
def test_fallback_text_is_refused_naming_the_rule_and_writes_nothing(repo, field):
    before = _notes_bytes(repo)
    register_before = (repo / REL).read_bytes()
    out = _revise(repo, **{field: FALLBACK_NOTE})
    assert corpus_fallback.RULE in out["error"]
    assert _notes_bytes(repo) == before
    assert (repo / REL).read_bytes() == register_before


def test_notes_alias_is_refused_too(repo):
    before = _notes_bytes(repo)
    out = _revise(repo, notes=[FALLBACK_NOTE])
    assert corpus_fallback.RULE in out["error"]
    assert _notes_bytes(repo) == before


def test_benign_outage_note_still_applies(repo):
    out = _revise(repo, note=BENIGN_NOTE)
    assert "error" not in out
    assert rl.read_notes(rl.run_dir(repo, RUN)) == [BENIGN_NOTE]
