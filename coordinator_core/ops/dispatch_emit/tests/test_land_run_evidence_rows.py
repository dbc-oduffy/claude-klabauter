"""land_run credits an evidence-append row only when the row is DONE and its footprint is the changed sidecar."""

from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit
from coordinator_core.ops.dispatch_emit.land_run import _own_evidence_rows

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
