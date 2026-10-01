"""Tests for dispatch_emit.completion_receipt: baton resolution and receipt composition."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.completion_receipts.model import validate
from coordinator_core.completion_receipts.store import ReceiptWriteError
from coordinator_core.ops.dispatch_emit import completion_receipt as cr
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest

NOW = "2026-10-01T18:00:00Z"
SID = "d7b9dc1a-1455-43e8-922f-87e734b5634e"
DLV_A = "dlv-alpha-aaaaaa"
DLV_B = "dlv-bravo-bbbbbb"

_GOOD_RECORD = {
    "prep": {"slice_files": ["a.py"], "product_files": ["a.py"], "foreign_claims": []},
    "delivery": {"verdict": "PASS"},
    "tests": {"status": "pass"},
    "criterion": {"status": "met", "observation": "the probe is green"},
    "unresolved": [],
    "confinement_violations": 0,
}


def _plan(deliverable: str, rows: list[tuple[str, str]], extra: str = "") -> str:
    body = "".join(
        f"- id: {rid}\n  title: t\n  disposition: {disp}\n  body: |\n    b\n" for rid, disp in rows
    )
    return (
        f"---\ntitle: p\ndeliverable_id: {deliverable}\n{extra}---\n\n"
        f"## Tasks\n\n```yaml plan-tasks\n{body}```\n"
    )


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _baton(root, name, deliverable, handoff_id):
    _write(
        root,
        f"state/handoffs/{name}",
        f"---\nhandoff_id: {handoff_id}\ndeliverable_id: {deliverable}\n---\n\nbody\n",
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(cr, "_actual_loe", lambda root, sid: "S")
    claimed: list = []
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked",
        lambda sid, cwd=None: ([("handoff", n) for n in claimed], []),
    )
    return claimed


def _build(root, request, *, done, incomplete=(), record=_GOOD_RECORD):
    return cr.build_run_receipts(
        root,
        request,
        done_ids=list(done),
        incomplete_ids=list(incomplete),
        record=record,
        script_path="docs/specs/run1.workflow.mjs",
        session_id=SID,
        base_sha="a" * 40,
        branch="work/x",
        now=NOW,
    )


def _plan_request(plan_rel="docs/plans/p.md", deliverable=DLV_A):
    return CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),),
        deliverable_id=deliverable,
        plan_path=plan_rel,
    )


def _plan_repo(root, rows=(("C1", "open"),), sizing=True):
    extra = 'sizing_object: "state/sizings/s.yaml"\n' if sizing else ""
    _write(root, "docs/plans/p.md", _plan(DLV_A, list(rows), extra))
    if sizing:
        _write(
            root,
            "state/sizings/s.yaml",
            'estimate:\n  tshirt: M\nbaton: "state/handoffs/sized.md"\n',
        )
    _write(root, "docs/specs/run1.workflow.mjs.emitted.json", '{"emitted_at": "2026-10-01T17:00:00"}')


def test_plan_mode_with_claimed_baton(tmp_path, _isolate):
    _plan_repo(tmp_path)
    _baton(tmp_path, "claimed.md", DLV_A, "hnd-claimed-111111")
    _baton(tmp_path, "other.md", DLV_B, "hnd-other-222222")
    _isolate.extend(["claimed.md", "other.md"])
    built = _build(tmp_path, _plan_request(), done=["C1"])
    assert len(built) == 1
    fm, prose = built[0]
    assert fm["baton_id"] == "hnd-claimed-111111"
    assert fm["deliverable_id"] == DLV_A
    assert fm["run"] == {"kind": "execute-plan", "id": "run1.workflow"}
    assert fm["verdict"] == "agent-delivered"
    assert fm["judge"]["observation_summary"] == "the probe is green"
    assert fm["commit_range"] == {"base": "a" * 40, "head": None}
    assert fm["started_at"] == "2026-10-01T17:00:00Z"
    assert fm["loe"] == {"estimated": "M", "actual": "S"}
    assert "## Concluded\n\nC1" in prose and "## Remains\n\nnone" in prose


def test_plan_mode_no_claim_falls_back_to_sizing_baton(tmp_path):
    _plan_repo(tmp_path)
    _baton(tmp_path, "sized.md", DLV_A, "hnd-sized-333333")
    built = _build(tmp_path, _plan_request(), done=["C1"])
    assert [fm["baton_id"] for fm, _ in built] == ["hnd-sized-333333"]


def test_plan_mode_with_neither_writes_null_baton(tmp_path):
    _plan_repo(tmp_path)
    built = _build(tmp_path, _plan_request(), done=["C1"])
    assert len(built) == 1
    fm, _ = built[0]
    assert fm["baton_id"] is None and fm["deliverable_id"] == DLV_A


def test_plan_mode_with_no_deliverable_builds_nothing(tmp_path):
    _write(tmp_path, "docs/plans/p.md", "---\ntitle: p\n---\n\n```yaml plan-tasks\n- id: C1\n```\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), plan_path="docs/plans/p.md"
    )
    assert _build(tmp_path, request, done=["C1"]) == []


def test_not_met_criterion_gives_null_verdict(tmp_path):
    _plan_repo(tmp_path)
    record = dict(_GOOD_RECORD, criterion={"status": "not_met", "observation": "still red"})
    ((fm, prose),) = _build(tmp_path, _plan_request(), done=["C1"], record=record)
    assert fm["verdict"] is None and fm["judge"] is None
    assert "exit criterion is not_met" in prose


def test_incomplete_row_gives_null_verdict(tmp_path):
    _plan_repo(tmp_path, rows=(("C1", "open"), ("C2", "open")))
    ((fm, prose),) = _build(tmp_path, _plan_request(), done=["C1"], incomplete=["C2"])
    assert fm["verdict"] is None
    assert "## Remains\n\nC2" in prose


def test_no_record_gives_null_verdict(tmp_path):
    _plan_repo(tmp_path)
    ((fm, prose),) = _build(tmp_path, _plan_request(), done=["C1"], record=None)
    assert fm["verdict"] is None
    assert "no review record" in prose


def _inventory_repo(root):
    _write(root, "docs/plans/a.md", _plan(DLV_A, [("R1", "open")]))
    _write(root, "docs/plans/b.md", _plan(DLV_B, [("R1", "open"), ("R2", "open")]))
    _write(
        root,
        "state/mise-inventory/run1.md",
        "---\nrun_id: run1\nsource_baton: ../handoffs/src.md\n---\n\n## Chunk table\n\n"
        "| ID | Spec path | Summary | Footprint | Deps | Verification | Complexity | Disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| A1 | `docs/plans/a.md` | s | `a.py` | — | v | S | pending |\n"
        "| B1 | `docs/plans/b.md` | s | `b.py` | — | v | S | pending |\n",
    )
    _write(
        root,
        "state/mise-inventory/run1.spine.md",
        "---\nrun_id: run1\nderived_from: mise inventory record\n---\n\n## Tasks\n",
    )
    _baton(root, "baton-a.md", DLV_A, "hnd-a-aaaaaa")
    _baton(root, "baton-b.md", DLV_B, "hnd-b-bbbbbb")
    _baton(root, "src.md", "dlv-source-cccccc", "hnd-src-cccccc")


def test_inventory_mode_two_batons_one_landed_one_incomplete(tmp_path, _isolate):
    _inventory_repo(tmp_path)
    _isolate.extend(["baton-a.md", "baton-b.md"])
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="A1.R1", title="t", paths=("a.py",)),
            ChunkCommit(id="B1.R1", title="t", paths=("b.py",)),
            ChunkCommit(id="B1.R2", title="t", paths=("c.py",)),
        ),
        plan_path="state/mise-inventory/run1.spine.md",
    )
    built = _build(tmp_path, request, done=["A1.R1", "B1.R1"], incomplete=["B1.R2"])
    by_baton = {fm["baton_id"]: fm for fm, _ in built}
    assert set(by_baton) == {"hnd-a-aaaaaa", "hnd-b-bbbbbb", "hnd-src-cccccc"}
    assert by_baton["hnd-a-aaaaaa"]["verdict"] == "agent-delivered"
    assert by_baton["hnd-a-aaaaaa"]["plan_path"] == "docs/plans/a.md"
    assert by_baton["hnd-b-bbbbbb"]["verdict"] is None
    assert by_baton["hnd-src-cccccc"]["verdict"] is None
    assert by_baton["hnd-src-cccccc"]["plan_path"] is None
    assert all(fm["run"]["kind"] == "mise-en-place" for fm in by_baton.values())


def test_every_built_receipt_validates(tmp_path, _isolate):
    _inventory_repo(tmp_path)
    _isolate.extend(["baton-a.md", "baton-b.md"])
    request = CommitRequest(
        chunks=(ChunkCommit(id="A1.R1", title="t", paths=("a.py",)),),
        plan_path="state/mise-inventory/run1.spine.md",
    )
    built = _build(tmp_path, request, done=["A1.R1"])
    built += _build(tmp_path, _plan_request("docs/plans/a.md"), done=["R1"])
    assert built
    for fm, _ in built:
        assert validate(fm) == []


def test_build_spawns_no_subprocess(tmp_path, _isolate, monkeypatch):
    _plan_repo(tmp_path)
    _baton(tmp_path, "claimed.md", DLV_A, "hnd-claimed-111111")
    _isolate.append("claimed.md")
    spawned: list = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, *a, **k):
            spawned.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    assert _build(tmp_path, _plan_request(), done=["C1"])
    assert spawned == []


def test_write_run_receipts_writes_then_cleans_up_on_failure(tmp_path):
    _plan_repo(tmp_path)
    good_a = _build(tmp_path, _plan_request(), done=["C1"])[0]
    good_b = _build(tmp_path, _plan_request(), done=["C1"])[0]
    paths = cr.write_run_receipts(tmp_path, [good_a, good_b])
    assert len(paths) == 2 and all((tmp_path / p).is_file() for p in paths)
    assert b"\r\n" not in (tmp_path / paths[0]).read_bytes()

    first = _build(tmp_path, _plan_request(), done=["C1"])[0]
    bad_fm = dict(good_a[0], receipt_id="not-a-receipt-id")
    with pytest.raises(ReceiptWriteError):
        cr.write_run_receipts(tmp_path, [first, (bad_fm, "x")])
    assert not (tmp_path / first[0]["prose_ref"]).exists()
    assert all((tmp_path / p).is_file() for p in paths)
