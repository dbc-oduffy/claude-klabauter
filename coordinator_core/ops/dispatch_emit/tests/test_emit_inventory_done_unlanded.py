"""dispatch.emit --inventory skips DONE-unlanded chunks: no executor row, a no-op chunk's plan
row flips coded, a chunk with dirty files is routed to landing and left byte-identical."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.emit import _DISPATCH_REPORT_DIR
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_mint_done_unlanded import _dep, _git, _plan_text
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import _chunk
from coordinator_core.testing.git_write import write_bytes_seen_by_git, write_seen_by_git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE = "20261022T000000-eun"
DIR = "state/mise-inventory"
_DIRTY = b"uncommitted bytes\n"


def _fixture(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / DIR).mkdir(parents=True)
    (tmp_path / "src").mkdir()
    rows = [
        _chunk("N1", "src/n1.py"),
        _chunk("L1", "src/l1.py"),
        _chunk("D1", "src/d1.py", depends_on=_dep("L1")),
        _chunk("F1", "src/f1.py"),
    ]
    plan = tmp_path / "docs" / "plans" / "a.md"
    plan.write_text(_plan_text(rows), encoding="utf-8")
    (tmp_path / "src" / "n1.py").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    write_bytes_seen_by_git(tmp_path / "src" / "l1.py", _DIRTY)
    inv = tmp_path / DIR / f"{BASE}.md"
    write_seen_by_git(
        inv,
        f"---\nrun_id: {BASE}\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| A | `docs/plans/a.md` | s a | `docs/plans/a.md` | — | v | S | in_progress |\n",
        encoding="utf-8",
    )
    for mid in ("A.N1", "A.L1"):
        rep = tmp_path / _DISPATCH_REPORT_DIR / f"{BASE}-t1-p1.spine" / f"{mid}.md"
        rep.parent.mkdir(parents=True, exist_ok=True)
        rep.write_text("status: DONE\n", encoding="utf-8")
    return inv


def _emit(tmp_path, monkeypatch, inv, **extra):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)
    return op_mod._dispatch_emit(
        {
            "inventory_path": str(inv),
            "output_path": str(tmp_path / "run.workflow.mjs"),
            "target_root": str(tmp_path),
            **extra,
        },
        repo_root=tmp_path,
    )


def _row_disposition(tmp_path: Path, chunk: str) -> str:
    text = (tmp_path / "docs" / "plans" / "a.md").read_text(encoding="utf-8")
    block = text.split(f"- id: {chunk}\n", 1)[1].split("\n- id:", 1)[0]
    return next(
        (l.split(":", 1)[1].strip() for l in block.splitlines() if l.strip().startswith("disposition:")),
        "open",
    )


def test_emit_skips_done_unlanded_flips_noop_and_leaves_dirty_bytes(tmp_path, monkeypatch):
    inv = _fixture(tmp_path)

    reply = _emit(tmp_path, monkeypatch, inv, max_rows=100)

    spine = next((tmp_path / DIR).glob("*.spine.md")).read_text(encoding="utf-8")
    assert "A.N1" not in spine and "A.L1" not in spine and "A.D1" not in spine
    assert "A.F1" in spine
    entries = {e["id"]: e for e in reply["inventory_accounting"]["done_unlanded"]}
    assert set(entries) == {"A.N1", "A.L1"}
    assert entries["A.N1"]["class"] == "no-op"
    assert entries["A.L1"]["class"] == "land" and entries["A.L1"]["held_behind"] == ["A.D1"]
    assert "N1" in reply["landed_reconciled"]["docs/plans/a.md"]
    assert "L1" not in reply["landed_reconciled"]["docs/plans/a.md"]
    assert _row_disposition(tmp_path, "N1") == "coded"
    assert _row_disposition(tmp_path, "L1") == "open"
    assert (tmp_path / "src" / "l1.py").read_bytes() == _DIRTY


def test_review_only_emit_flips_nothing(tmp_path, monkeypatch):
    inv = _fixture(tmp_path)
    before = (tmp_path / "docs" / "plans" / "a.md").read_bytes()
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()

    reply = _emit(tmp_path, monkeypatch, inv, review_only_rows=["A.F1"], run_base_sha=head)

    assert reply["landed_reconciled"] == {}
    assert (tmp_path / "docs" / "plans" / "a.md").read_bytes() == before
    assert (tmp_path / "src" / "l1.py").read_bytes() == _DIRTY
