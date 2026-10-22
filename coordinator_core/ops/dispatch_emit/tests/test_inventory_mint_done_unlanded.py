"""mint_rows mints no executor row for a DONE-unlanded chunk, discharges a no-op one and
holds a land one's dependents."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.emit import _DISPATCH_REPORT_DIR
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_rows_landed import _cp
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import _chunk

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE = "20261022T000000-dun"
DIR = "state/mise-inventory"
SPINE = f"{DIR}/{BASE}-t1-p1.spine.md"
_ENV = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *_ENV, "-C", str(root), *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _plan_text(rows: list) -> str:
    return "# plan\n\n## Tasks\n\n```yaml plan-tasks\n" + yaml.safe_dump(rows, sort_keys=False) + "```\n"


def _dep(chunk: str) -> list:
    return [{"chunk": chunk, "gate_kind": "output-consumption-runtime"}]


def _report(root: Path, mid: str, text: str = "status: DONE\n") -> None:
    p = root / _DISPATCH_REPORT_DIR / f"{BASE}-t1-p1.spine" / f"{mid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _fixture(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / DIR).mkdir(parents=True)
    (tmp_path / "src").mkdir()
    a_rows = [
        _chunk("C1", "src/c1.py"),
        _chunk("C2", "src/c2.py", depends_on=_dep("C1")),
        _chunk("C3", "src/c3.py"),
        _chunk("C4", "src/c4.py", depends_on=_dep("C3")),
    ]
    (tmp_path / "docs" / "plans" / "a.md").write_text(_plan_text(a_rows), encoding="utf-8")
    (tmp_path / "docs" / "plans" / "c.md").write_text(_plan_text([_chunk("K1", "src/k1.py")]), encoding="utf-8")
    (tmp_path / "src" / "c1.py").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "src/c1.py")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    (tmp_path / "src" / "c3.py").write_text("dirty\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("dirty\n", encoding="utf-8")
    head = (
        f"---\nrun_id: {BASE}\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    body = (
        "| A | `docs/plans/a.md` | s a | `docs/plans/a.md` | — | v | S | in_progress |\n"
        "| B | — | s b | `src/b.py` | — | v | S | in_progress |\n"
        "| C | `docs/plans/c.md` | s c | `docs/plans/c.md` | — | v | S | in_progress |\n"
    )
    inv = tmp_path / DIR / f"{BASE}.md"
    inv.write_text(head + body, encoding="utf-8")
    return inv


def _mint(inv: Path, **outs):
    return im.mint_rows(im.parse_chunk_table(inv.read_text(encoding="utf-8")), inventory_path=inv, **outs)


def test_done_unlanded_chunks_are_classified_and_not_minted(tmp_path):
    inv = _fixture(tmp_path)
    for mid in ("A.C1", "A.C3", "B"):
        _report(tmp_path, mid)
    out: dict = {}
    acct: dict = {}
    rows = _mint(inv, done_unlanded_out=out, accounting_out=acct)
    by_id = {r["id"]: r for r in rows}
    assert sorted(by_id) == ["A.C2", "C.K1"]
    assert "depends_on" not in by_id["A.C2"]
    assert sorted(out) == ["A.C1", "A.C3", "B"]
    assert out["A.C1"]["class"] == "no-op" and out["A.C1"]["held_behind"] == []
    assert out["A.C3"]["class"] == "land" and out["A.C3"]["held_behind"] == ["A.C4"]
    assert out["A.C3"]["dirty"] == ["src/c3.py"]
    assert (out["A.C3"]["plan"], out["A.C3"]["chunk"]) == ("docs/plans/a.md", "C3")
    assert (out["A.C1"]["plan"], out["A.C1"]["chunk"]) == ("docs/plans/a.md", "C1")
    assert out["B"]["class"] == "land" and (out["B"]["plan"], out["B"]["chunk"]) == ("", "")
    assert acct["B"] == "done, unlanded: land B first"
    assert "A" not in acct and "C" not in acct


def test_item_with_every_chunk_noop_is_discharged_and_dependents_mint(tmp_path):
    inv = _fixture(tmp_path)
    (tmp_path / "docs" / "plans" / "c.md").write_text(
        _plan_text([_chunk("K1", "src/c1.py")]), encoding="utf-8"
    )
    inv.write_text(
        inv.read_text(encoding="utf-8") + "| D | — | s d | `src/d.py` | C | v | S | in_progress |\n",
        encoding="utf-8",
    )
    _report(tmp_path, "C.K1")
    out: dict = {}
    acct: dict = {}
    landed: dict = {}
    rows = _mint(inv, done_unlanded_out=out, accounting_out=acct, landed_out=landed)
    assert out["C.K1"]["class"] == "no-op"
    assert acct["C"] == "every open chunk done, unlanded: no-op C.K1"
    assert landed["C"] == acct["C"]
    ids = {r["id"] for r in rows}
    assert "D" in ids and "C.K1" not in ids
    assert "depends_on" not in next(r for r in rows if r["id"] == "D")


def test_land_item_withholds_its_dependents(tmp_path):
    inv = _fixture(tmp_path)
    inv.write_text(
        inv.read_text(encoding="utf-8") + "| D | — | s d | `src/d.py` | B | v | S | in_progress |\n",
        encoding="utf-8",
    )
    _report(tmp_path, "B")
    withheld: dict = {}
    rows = _mint(inv, withheld_out=withheld, done_unlanded_out={})
    assert "D" not in {r["id"] for r in rows}
    assert withheld["B"] == "done, unlanded: land B first"
    assert withheld["D"] == "depends on withheld item B"


def test_checkpoint_landed_chunk_is_not_classified(tmp_path):
    inv = _fixture(tmp_path)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "all")
    _cp(tmp_path, ["A.C1"], plan=SPINE)
    _report(tmp_path, "A.C1")
    out: dict = {}
    rows = _mint(inv, done_unlanded_out=out)
    assert out == {}
    assert "A.C1" not in {r["id"] for r in rows}


def test_standalone_mint_rows_is_unchanged(tmp_path):
    inv = _fixture(tmp_path)
    rows = im.mint_rows(im.parse_chunk_table(inv.read_text(encoding="utf-8")))
    assert [r["id"] for r in rows] == ["A", "B", "C"]


def test_no_report_spawns_no_git_status(tmp_path, monkeypatch):
    inv = _fixture(tmp_path)
    from coordinator_core.ops.dispatch_emit import dirty_write_set as dws

    def _boom(*a, **k):
        raise AssertionError("git status spawned without a DONE candidate")

    monkeypatch.setattr(dws, "_dirty_in_write_set", _boom)
    out: dict = {}
    _mint(inv, done_unlanded_out=out)
    assert out == {}
