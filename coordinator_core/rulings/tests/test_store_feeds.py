"""store.load reads PM-accepted decision records and APM memos relaying a PM turn."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _by_id(root: Path) -> dict[str, Ruling]:
    return {r.id: r for r in store.load(root)}


def test_dr_with_pm_verbatim_heading(tmp_path):
    _w(tmp_path, "docs/decisions/DR-1-verbatim.md",
       "---\ntitle: Gate thresholds\nstatus: accepted\nenforced_by: coordinator_core/x/test_y.py\n"
       "created: 2026-11-01\n---\n\n# DR-1\n\n## PM verbatim\n\nOne millimetre, not five.\n\n## Other\n\nx\n")
    r = _by_id(tmp_path)["DR-1-verbatim"]
    assert r.text == "One millimetre, not five."
    assert r.pm_turn == "docs/decisions/DR-1-verbatim.md"
    assert r.enforcement == "coordinator_core/x/test_y.py"
    assert r.status == "accepted" and r.recorded == "2026-11-01"
    assert "gate" in r.scope


def test_dr_with_authority_pm_uses_summary(tmp_path):
    _w(tmp_path, "coordinator/docs/decisions/DR-2-auth.md",
       "---\ntitle: Auth\nstatus: accepted\nauthority: PM ruling 2026-11-02\nsummary: No licence gate.\n---\nbody\n")
    r = _by_id(tmp_path)["DR-2-auth"]
    assert r.text == "No licence gate."
    assert r.enforcement is None


def test_dr_with_pm_decider_prose_only_owed_reads_as_unenforced(tmp_path):
    _w(tmp_path, "docs/decisions/DR-3-decider.md",
       "---\ntitle: Brightline budget\nstatus: accepted\ndeciders:\n  - \"dbc (PM)\"\n  - em\n"
       "enforcement: prose-only-owed\n---\nbody text\n")
    r = _by_id(tmp_path)["DR-3-decider"]
    assert r.text == "Brightline budget"
    assert r.enforcement is None


def test_non_pm_and_non_accepted_drs_are_excluded(tmp_path):
    _w(tmp_path, "docs/decisions/DR-4-em.md",
       "---\ntitle: EM call\nstatus: accepted\ndeciders:\n  - em\n---\nbody\n")
    _w(tmp_path, "docs/decisions/DR-5-proposed.md",
       "---\ntitle: P\nstatus: proposed\ndeciders:\n  - \"x (PM)\"\n---\nbody\n")
    assert store.load(tmp_path) == []


def test_apm_memo_relaying_pm_turn_is_read_and_unmarked_is_skipped(tmp_path):
    _w(tmp_path, "state/apm-rulings/2026-11-03-relay.md",
       "---\ntitle: Relay\nrelays_pm_turn: sizing.yaml R2\nscope: [licensing]\n---\n\n# APM ruling\n\nPrivate repo.\n")
    _w(tmp_path, "state/apm-rulings/2026-11-03-plain.md", "---\ntitle: Plain\n---\n\n# APM\n\nopinion\n")
    got = _by_id(tmp_path)
    assert set(got) == {"2026-11-03-relay"}
    r = got["2026-11-03-relay"]
    assert (r.pm_turn, r.scope, r.text) == ("sizing.yaml R2", ("licensing",), "Private repo.")


def test_register_id_wins_over_feed_and_bad_feed_is_a_problem(tmp_path):
    store.append(tmp_path, Ruling(id="DR-6-dup", pm_turn="t", scope=("a",), text="register copy"))
    _w(tmp_path, "docs/decisions/DR-6-dup.md",
       "---\ntitle: D\nstatus: accepted\nauthority: PM\n---\nbody\n")
    _w(tmp_path, "docs/decisions/DR-7-bad.md",
       "---\ntitle: D\nstatus: accepted\nauthority: PM\ntargets: [{quantity: q}]\n---\nbody\n")
    rulings, problems = store.scan(tmp_path)
    assert [(r.id, r.text) for r in rulings] == [("DR-6-dup", "register copy")]
    assert [p["id"] for p in problems] == ["DR-7-bad"]
