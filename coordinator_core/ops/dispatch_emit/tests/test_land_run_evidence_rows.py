"""land_run credits an evidence-append row only when the row is DONE and its footprint is the changed sidecar."""

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit
from coordinator_core.ops.dispatch_emit.land_run import _grade, _incomplete_reasons, _own_evidence_rows

EV = "docs/plans/p.evidence.yaml"


def _c(cid, *paths):
    return ChunkCommit(id=cid, title="t", paths=tuple(paths), report="r.md")


def test_only_done_rows_inside_the_evidence_footprint_are_credited():
    done = [_c("C1", EV), _c("C2", EV, "a.py"), _c("C3")]
    assert _own_evidence_rows(done, EV) == {"C1"}


def test_an_incomplete_row_is_absent_from_done_so_never_credited():
    request_chunks = [_c("C1", EV), _c("C2", EV)]
    done = [c for c in request_chunks if c.id != "C2"]
    assert _own_evidence_rows(done, EV) == {"C1"}


def _grade_one(tmp_path, chunk, row, items, held=frozenset()):
    request = SimpleNamespace(chunks=(chunk,))
    manifest = SimpleNamespace(rows=(row,))
    return _grade(
        tmp_path, request, {chunk.id}, {}, object(), {"items": items}, set(held), None, None, None, None, {},
        manifest,
    )


def _row(cid="C5", writes=(), regs=("R1",)):
    return ManifestRow(
        id=cid, agent_type="a", model="m", brief_path="b.md", writes=tuple(writes), wave=0, register_rows=tuple(regs),
    )


def test_writes_empty_verification_row_with_ledger_met_evidence_is_complete(tmp_path):
    chunk = ChunkCommit(id="C5", title="t")
    out = _grade_one(tmp_path, chunk, _row(), [{"id": "R1", "status": "met", "met_by": ["capture.txt"]}])
    assert set(out) == {"C5"} and out["C5"].startswith("ledger-met")
    assert _incomplete_reasons({"C5"} - set(out), set(), {}, {}, {}, out, []) == {}


def test_writes_empty_row_stays_open_without_met_evidence_or_with_unmet_register_row(tmp_path):
    chunk = ChunkCommit(id="C5", title="t")
    assert _grade_one(tmp_path, chunk, _row(), [{"id": "R1", "status": "met"}]) == {}
    assert _grade_one(tmp_path, chunk, _row(), [{"id": "R1", "status": "open", "met_by": ["x"]}]) == {}
    assert _grade_one(tmp_path, chunk, _row(regs=("R1", "R2")), [{"id": "R1", "status": "met", "met_by": ["x"]}]) == {}
    assert _grade_one(tmp_path, chunk, _row(), [{"id": "R1", "status": "met", "met_by": ["x"]}], held={"C5"}) == {}


def test_row_that_declares_writes_is_never_credited_by_the_ledger_alone(tmp_path):
    chunk = ChunkCommit(id="C5", title="t", paths=("a.py",))
    out = _grade_one(tmp_path, chunk, _row(writes=("a.py",)), [{"id": "R1", "status": "met", "met_by": ["x"]}])
    assert out == {}
