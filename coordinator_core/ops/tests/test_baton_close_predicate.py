"""Branch coverage for the pure baton_close_predicate.decide()."""

from __future__ import annotations

import pytest

from coordinator_core.ops import baton_close_predicate as p

B = "state/handoffs/x.md"


def ev(kind: str) -> dict:
    reads, strength, proposes = p.KIND_SHAPES[kind]
    return {"kind": kind, "reads": reads, "strength": strength,
            "proposes": proposes, "source": "state/sizings/s.yaml", "detail": "d"}


def run(kinds, refusal=None):
    return p.decide(B, {}, [ev(k) for k in kinds], refusal)


def test_schema_version():
    assert p.SCHEMA_VERSION == 1


def test_no_evidence():
    assert run([]) == {"verdict": "no_evidence", "proposes": None, "reason": None}


def test_live_only_is_residue():
    for kinds in ([p.RUN_HALTED], [p.SUCCESSOR_LIVE], [p.SIZING_MISSING],
                  [p.RUN_HALTED, p.SUCCESSOR_LIVE]):
        r = run(kinds)
        assert r["verdict"] == "residue" and r["proposes"] is None
        assert r["reason"].startswith("still live: ")
    assert run([p.RUN_HALTED, p.SUCCESSOR_LIVE])["reason"] == \
        "still live: RUN_HALTED, SUCCESSOR_LIVE"


def test_live_alongside_terminal_conflicts():
    r = run([p.SIZING_TERMINAL, p.RUN_HALTED])
    assert r["verdict"] == "residue"
    assert r["reason"] == "conflicting evidence: RUN_HALTED, SIZING_TERMINAL"


def test_strong_disagreement():
    r = run([p.SIZING_TERMINAL, p.SUCCESSOR_CLAIMED])
    assert r["verdict"] == "residue"
    assert r["reason"].startswith("conflicting evidence: ")


def test_strong_with_none_proposes_is_residue():
    e = ev(p.SIZING_TERMINAL)
    e["proposes"] = None
    assert p.decide(B, {}, [e], None)["verdict"] == "residue"


def test_weak_only():
    r = run([p.PLAN_SPINE_DONE, p.MERGED_BRANCH])
    assert r["verdict"] == "residue"
    assert r["reason"] == "weak evidence only: MERGED_BRANCH, PLAN_SPINE_DONE"


def test_abandoned_is_human_call():
    for k in (p.SIZING_DECLINED, p.SUCCESSOR_UNCLAIMED):
        r = run([k])
        assert r["verdict"] == "residue"
    strong_abandon = ev(p.SIZING_TERMINAL)
    strong_abandon["proposes"] = "abandoned"
    r = p.decide(B, {}, [strong_abandon], None)
    assert r == {"verdict": "residue", "proposes": None,
                 "reason": "abandoned is a human call (DR-242)"}


def test_predicate_refusal():
    r = run([p.SIZING_TERMINAL], refusal="leg a failed")
    assert r == {"verdict": "residue", "proposes": None,
                 "reason": "predicate refused: leg a failed"}


def test_divergence_agreeing_strong_with_weak_support():
    r = run([p.SIZING_TERMINAL, p.RUN_COMMITTED, p.MERGED_BRANCH])
    assert r == {"verdict": "divergence", "proposes": "shipped", "reason": None}


def test_divergence_continued():
    assert run([p.SUCCESSOR_CLAIMED])["proposes"] == "continued"


@pytest.mark.parametrize("kind", sorted(p.KIND_SHAPES))
def test_every_kind_constant(kind):
    assert getattr(p, kind) == kind
    reads, strength, proposes = p.KIND_SHAPES[kind]
    r = run([kind])
    if reads == "live":
        assert r["reason"].startswith("still live")
    elif strength == "weak":
        assert r["verdict"] == "residue" and r["proposes"] is None
    else:
        assert r["verdict"] == "divergence" and r["proposes"] == proposes


def test_kind_table_is_complete():
    consts = {v for k, v in vars(p).items()
              if k.isupper() and isinstance(v, str) and v == k}
    assert consts == set(p.KIND_SHAPES) and len(consts) == 11
