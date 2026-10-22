"""The since-halt leg records rows whose met_by names a commit at or before the halt, and the ledger gate closes already_landed on them."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    shas = {}
    for name, body in (("A", "x = 1\n"), ("A2", "x = 2\n"), ("B", "x = 3\n"), ("C", "y = 1\n")):
        target = "other.py" if name == "C" else "prod.py"
        (tmp_path / target).write_text(body, encoding="utf-8")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-q", "-m", f"commit {name}")
        shas[name] = _git(tmp_path, "rev-parse", "HEAD")
        if name == "B":
            shas["halt"] = shas["B"]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, shas


def _resume(halt, blocking=("R1",)):
    return {"run_id": "old", "halt_sha": halt, "blocking": list(blocking), "unlanded": ["C2"]}


def _verdict(**met_by):
    return {"items": [{"id": k, "status": "met", "wired": True, "met_by": [v]} for k, v in met_by.items()]}


def test_pre_halt_ancestor_is_cited_and_stands_as_the_landing(repo):
    root, manifest, sh = repo
    detail: dict = {}
    got = tc._unchanged_since_halt_failures(
        root, _resume(sh["halt"]), _verdict(R1=sh["A"][:10]), manifest, detail=detail
    )
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert detail[tc.CITED_DETAIL_KEY] == {"R1": sh["A"]}
    assert detail["landed_sha"] == sh["A"]
    assert detail["landed_paths"] == ["prod.py"]


def test_rows_citing_different_commits_land_at_the_base(repo):
    root, manifest, sh = repo
    detail: dict = {}
    tc._unchanged_since_halt_failures(
        root, _resume(sh["halt"], ("R1", "R2")), _verdict(R1=sh["A"][:10], R2=sh["A2"][:10]),
        manifest, detail=detail,
    )
    assert set(detail[tc.CITED_DETAIL_KEY]) == {"R1", "R2"}
    assert detail["landed_sha"] == sh["halt"]


def test_post_commit_row_citing_an_ancestor_is_refused_and_never_cited(repo):
    root, manifest, sh = repo
    detail: dict = {}
    verdict = {"items": [{"id": "R1", "status": "post_commit", "met_by": [sh["A"][:10]]}]}
    got = tc._unchanged_since_halt_failures(root, _resume(sh["halt"]), verdict, manifest, detail=detail)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert tc.CITED_DETAIL_KEY not in detail
    assert "landed_sha" not in detail


def test_post_halt_citation_is_not_cited(repo):
    root, manifest, sh = repo
    detail: dict = {}
    got = tc._unchanged_since_halt_failures(
        root, _resume(sh["halt"]), _verdict(R1=sh["C"][:10]), manifest, detail=detail
    )
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert tc.CITED_DETAIL_KEY not in detail
    assert "landed_sha" not in detail


def test_partial_coverage_falls_back_to_landed_before_halt(repo):
    root, manifest, sh = repo
    detail: dict = {}
    tc._unchanged_since_halt_failures(
        root, _resume(sh["halt"], ("R1", "R2")), _verdict(R1=sh["A"][:10], R2=sh["C"][:10]),
        manifest, detail=detail,
    )
    assert tc.CITED_DETAIL_KEY not in detail


def _gate(tmp_path, monkeypatch, since_halt, failures, verdict=None):
    class _Ledger:
        items = [1]
        all_of_pm_ask = False
        sizing_rel = None

    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    monkeypatch.setattr(rl, "read_ledger", lambda *a, **k: _Ledger())
    monkeypatch.setattr(
        rl, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []}
    )
    monkeypatch.setattr(rl, "read_notes", lambda *a, **k: [])
    monkeypatch.setattr(tc.pre_halt_credit, "with_notes", lambda r, n: r)

    def fake(root, resume, verdict, manifest, plan_rel, detail):
        detail.update(since_halt)
        return failures

    monkeypatch.setattr(tc, "_unchanged_since_halt_failures", fake)
    return tc._ledger_gate(tmp_path, tmp_path, verdict or {"items": []}, resume_ledger={"blocking": []})


def _stale(*ids):
    return [{"id": i, "reason": UNCHANGED_SINCE_HALT_REASON} for i in ids]


def test_gate_closes_already_landed_when_every_refused_row_is_cited(tmp_path, monkeypatch):
    refusal, _, _ = _gate(
        tmp_path, monkeypatch,
        {"landed_sha": "a" * 40, "landed_paths": ["p"], tc.CITED_DETAIL_KEY: {"R1": "a" * 40}},
        _stale("R1"),
    )
    assert refusal["already_landed"] is True and refusal["sha"] == "a" * 40


def test_gate_refuses_a_refused_row_the_ancestry_did_not_vouch_for(tmp_path, monkeypatch):
    refusal, _, _ = _gate(
        tmp_path, monkeypatch,
        {"landed_sha": "a" * 40, "landed_paths": ["p"], tc.CITED_DETAIL_KEY: {"R1": "a" * 40}},
        _stale("R1", "R2"),
        {"items": [{"id": "R2", "status": "met", "met_by": ["b" * 10]}]},
    )
    assert refusal.get("refused") == "ledger-unmet"
