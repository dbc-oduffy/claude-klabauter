"""A halt commit carries run artifacts only: declared product lands solely through the terminal_commit
landing gates (co-declared peer hunks, blob drift, owning chunk) and scratch export copies never land."""

from __future__ import annotations

import hashlib

import pytest

from coordinator_core.ops.dispatch_emit import halt_product_gates, halted_run, peer_hunk_gate, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401 -- fixtures
    LEDGER,
    PASSING,
    RUN,
    _call,
    _git,
    _landed,
    _marker_script,
    _write_record,
    repo,
)
from coordinator_core.session import core as session_core, touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c1"
SIBLING = "run-sibling"
SCRATCH_TREE = "scratch/merge-9ca2b1/tree/pkg/mod.py"


@pytest.fixture
def session(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(repo))
    return repo


def _product(repo, name="done.py", text="d = 1\n"):
    (repo / name).write_text(text, encoding="utf-8")


def _message(repo) -> str:
    return _git(["log", "-1", "--format=%B"], repo)


def _declare_sibling(repo, path: str) -> None:
    rdir = repo / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(
        '{"rows": [{"id": "C1", "writes": ["%s"]}], "doc_writes": []}' % path, encoding="utf-8"
    )
    run_token.mint(repo, SIBLING, "sibling.mjs", session_id=SELF)


def test_a_product_a_sibling_run_co_declares_is_withheld_and_artifacts_still_land(session):
    repo = session
    _write_record(repo, RUN)
    _product(repo)
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})
    _declare_sibling(repo, "done.py")

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["withheld"]["done.py"] == peer_hunk_gate.PEER_HUNKS_CO_DECLARED
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names
    assert "Lands-Chunks" not in _message(repo)


def test_b_product_whose_bytes_drifted_from_the_recorded_write_is_withheld(session):
    repo = session
    _write_record(repo, RUN)
    _product(repo)
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path="done.py",
        content_hash=hashlib.sha256(b"d = 1\n").hexdigest(), kind="w",
    )
    _product(repo, text="d = 2  # a peer rewrote this after the run's recorded write\n")

    out = _call(repo, LEDGER, session_id=SELF, **PASSING)

    assert out["committed"] is True, out
    names = _landed(repo, out, "--name-only")
    assert "done.py" not in names
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names
    assert "done.py" in out["withheld"]
    assert "Lands-Chunks" not in _message(repo)


def test_c_declared_product_no_chunk_owns_is_withheld(repo):
    rec = _write_record(repo, RUN)
    _product(repo, "orphan.py")
    halted_run.write_halt_marker(rec, halted="ledger-check", sizing_rel=None)
    halted_run.record_withheld(rec, {"orphan.py": "peer-held"})
    params = {"run_id": RUN, HALTED_RECORD_PARAM: LEDGER, **PASSING}

    out = terminal_commit._handler(params, repo_root=repo / ".git")

    assert out["committed"] is True, out
    assert "orphan.py" not in _landed(repo, out, "--name-only")
    assert out["withheld"]["orphan.py"] == "halt-unowned-product"
    assert "Lands-Chunks" not in _message(repo)


def test_d_scratch_export_copy_declared_by_a_chunk_is_never_committed(repo):
    _write_record(repo, RUN)
    (repo / SCRATCH_TREE).parent.mkdir(parents=True)
    (repo / SCRATCH_TREE).write_text("m = 1\n", encoding="utf-8")
    _product(repo)
    _marker_script(repo, [("C1", ["done.py", SCRATCH_TREE])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    names = _landed(repo, out, "--name-only")
    assert not [n for n in names if n == "scratch" or n.startswith("scratch/")], names
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in names


def test_d_scratch_sizing_named_by_the_run_record_is_never_committed(repo):
    _write_record(repo, RUN)
    (repo / "scratch").mkdir()
    (repo / "scratch" / "s.yaml").write_text("a: 1\n", encoding="utf-8")
    (repo / RECORD_DIR_ROOT / RUN / "run.json").write_text('{"sizing_rel": "scratch/s.yaml"}\n', encoding="utf-8")

    out = _call(repo, LEDGER)

    assert out["committed"] is True, out
    names = _landed(repo, out, "--name-only")
    assert not [n for n in names if n == "scratch" or n.startswith("scratch/")], names


def test_e_product_clearing_every_gate_is_still_halt_record_only(session):
    repo = session
    _write_record(repo, RUN)
    _product(repo)
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert "done.py" not in _landed(repo, out, "--name-only")
    assert "Lands-Chunks" not in _message(repo)
    assert out["withheld"] == {"done.py": "halt-record-only"}


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("scratch", True),
        ("scratch/merge-9ca2b1/tree/a.py", True),
        ("./scratch/a.py", True),
        ("scratch\\merge-9ca2b1\\tree\\a.py", True),
        ("scratchy/a.py", False),
        ("docs/scratch/a.py", False),
        ("coordinator_core/a.py", False),
    ],
)
def test_is_scratch_normalises_separators_and_matches_whole_segments(rel, expected):
    assert halt_product_gates.is_scratch(rel) is expected
