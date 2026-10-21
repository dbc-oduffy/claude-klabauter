"""requirements_ledger._sizing_path resolves a sizing the review stamp moved to archive/sizings."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261013T000000-abc123"
NAME = "2026-10-13-ledger.yaml"
REL = f"state/sizings/{NAME}"


def _sizing():
    return {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }


def _write(path, doc=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc or _sizing(), sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    return tmp_path


def test_live_sizing_resolves(repo):
    live = _write(repo / REL)
    assert rl._sizing_path(repo, REL) == live.resolve()


def test_archived_only_sizing_resolves_and_read_ledger_returns_rows(repo):
    rl.start(repo, RUN)
    _write(repo / REL)
    rl.bind(repo, RUN, REL, ask_text="a raw ask")
    archived = repo / "archive" / "sizings" / "2026-10" / NAME
    archived.parent.mkdir(parents=True)
    (repo / REL).replace(archived)
    assert rl._sizing_path(repo, REL) == archived.resolve()
    ledger = rl.read_ledger(repo, repo / RECORD_DIR_ROOT / RUN)
    assert [i.id for i in ledger.items] == ["R1", "R2"]


def test_two_archive_matches_refuse(repo):
    _write(repo / "archive" / "sizings" / "2026-09" / NAME)
    _write(repo / "archive" / "sizings" / "2026-10" / NAME)
    with pytest.raises(rl.LedgerRefused):
        rl._sizing_path(repo, REL)


def test_path_escaping_both_homes_refuses(repo):
    _write(repo / "elsewhere" / NAME)
    with pytest.raises(rl.LedgerRefused):
        rl._sizing_path(repo, "elsewhere/" + NAME)
    with pytest.raises(rl.LedgerRefused):
        rl._sizing_path(repo, "../" + NAME)


def test_ledger_gate_over_an_archived_sizing_is_not_unreadable(repo):
    rl.start(repo, RUN)
    _write(repo / REL)
    rl.bind(repo, RUN, REL, ask_text="a raw ask")
    archived = repo / "archive" / "sizings" / "2026-10" / NAME
    archived.parent.mkdir(parents=True)
    (repo / REL).replace(archived)
    refusal, ledger, _open = land_run._ledger_gate(
        repo, repo / RECORD_DIR_ROOT / RUN, None, None, (), {}, None, {}, None, None
    )
    assert (refusal or {}).get("refused") != "ledger-unreadable"
    assert ledger is not None


def test_mutate_register_on_archived_sizing_writes_the_archive_only(repo):
    _write(repo / REL)
    archived = repo / "archive" / "sizings" / "2026-10" / NAME
    archived.parent.mkdir(parents=True)
    (repo / REL).replace(archived)
    rl._append_rows(repo, REL, [("late revision", "revision@2026-10-19T00:00:00Z")])
    rows = rr.read_register(archived.read_text(encoding="utf-8")).rows
    assert [r["source_text"] for r in rows] == ["late revision"]
    assert not any((repo / "state" / "sizings").iterdir())
