"""The halt commit withholds every path a live peer holds, asked of commit_v2's own peer oracle."""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _landed,
    _marker_script,
    _name_sizing,
    _write_record,
    repo,
)
from coordinator_core.warm import entry_seam

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/own.yaml"


@pytest.fixture
def oracle(monkeypatch):
    """Install a fake peer oracle; returns a controller with `held`, `warnings`, `calls`, `params`."""

    class Ctl:
        held: set = set()
        warnings: list = []
        calls: int = 0
        before_commit: int = -1
        params: list = []
        held_out_seen: list = []

    ctl = Ctl()
    ctl.held, ctl.warnings, ctl.params, ctl.held_out_seen = set(), [], [], []

    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        ctl.calls += 1
        hit = [p for p in paths if p in ctl.held]
        holds.extend((p, "peer-sid") for p in hit)
        if held_out is not None:
            if hit:
                held_out.setdefault("peer-sid", []).extend(hit)
            ctl.held_out_seen.append(held_out)
        return list(ctl.warnings)

    real = entry_seam.reentrant_dispatch

    def capture(op, params, **kw):
        if op == "ceremony.commit_v2":
            ctl.params.append(params)
            ctl.before_commit = ctl.calls
        return real(op, params, **kw)

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)
    monkeypatch.setattr(entry_seam, "reentrant_dispatch", capture)
    return ctl


def _message(repo_root) -> str:
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import _git

    return _git(["log", "-1", "--format=%B"], repo_root)


def _two_chunks(repo):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    (repo / "other.py").write_text("o = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py"]), ("C2", ["other.py"])], {"C1": "DONE: C1\n", "C2": "DONE: C2\n"})


def test_peer_held_declared_path_is_withheld_and_not_landed_chunk(repo, oracle):
    _two_chunks(repo)
    oracle.held = {"other.py"}

    out = _call(repo, LEDGER, **PASSING)

    assert out["withheld"] == {"other.py": "peer-held", "done.py": "halt-record-only"}
    assert out["peer_held_by"] == [{"session": "peer-sid", "paths": ["other.py"]}]
    assert "other.py" not in oracle.params[0]["paths"]
    assert "other.py" not in _landed(repo, out, "--name-only")
    assert "Lands-Chunks" not in _message(repo)


def test_peer_held_run_artifact_is_withheld(repo, oracle):
    _write_record(repo, RUN)
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / SIZING).write_text("a: 1\n", encoding="utf-8")
    _name_sizing(repo, SIZING)
    oracle.held = {SIZING}

    out = _call(repo, LEDGER)

    assert out["withheld"] == {SIZING: "peer-held"}
    assert SIZING not in oracle.params[0]["paths"]
    assert SIZING not in _landed(repo, out, "--name-only")


def test_partly_withheld_chunk_lands_none_of_its_paths(repo, oracle):
    _write_record(repo, RUN)
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["a.py", "b.py"])], {"C1": "DONE: C1\n"})
    oracle.held = {"b.py"}

    out = _call(repo, LEDGER)

    # A chunk lands whole or not at all: a.py alone could import b.py's uncommitted half.
    names = _landed(repo, out, "--name-only")
    assert "a.py" not in names and "b.py" not in names
    assert "Lands-Chunks" not in _message(repo)
    assert "landed_chunks" not in out


def test_lookup_raise_withholds_declared_paths_and_lands_run_artifacts(repo, monkeypatch):
    _two_chunks(repo)
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / SIZING).write_text("a: 1\n", encoding="utf-8")
    _name_sizing(repo, SIZING)

    def boom(*_a, **_k):
        raise RuntimeError("index down")

    monkeypatch.setattr(commit_v2.session_claim_index, "lookup", boom)

    out = _call(repo, LEDGER)

    assert out["withheld"]["done.py"] == "peer-claim-indeterminate"
    assert out["withheld"]["other.py"] == "peer-claim-indeterminate"
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names and "other.py" not in names
    assert any(n.endswith("/halted.json") for n in names)
    assert "Lands-Chunks" not in _message(repo)


def test_per_path_indeterminate_withholds_only_that_path(repo, oracle):
    _two_chunks(repo)
    oracle.warnings = ["claim state indeterminate for 'other.py'"]

    out = _call(repo, LEDGER, **PASSING)

    assert out["withheld"] == {"other.py": "peer-claim-indeterminate", "done.py": "halt-record-only"}
    assert "done.py" not in _landed(repo, out, "--name-only")
    assert "Lands-Chunks" not in _message(repo)


def test_no_hold_still_lands_no_product(repo, oracle):
    _two_chunks(repo)

    out = _call(repo, LEDGER, **PASSING)

    assert out["withheld"] == {"done.py": "halt-record-only", "other.py": "halt-record-only"}
    assert "peer_held_by" not in out
    assert not set(oracle.params[0]["paths"]) & {"done.py", "other.py"}
    assert "Lands-Chunks" not in _message(repo)


def test_one_claim_lookup_per_halt_commit(repo, oracle):
    _two_chunks(repo)

    _call(repo, LEDGER)

    assert oracle.before_commit == 1
