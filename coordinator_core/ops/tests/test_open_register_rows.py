"""open_register_rows names exactly the rows `ship_refusal` refuses on."""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops import requirement_register as rr

_REL = "state/sizings/2026-11/x.yaml"


def _write(root: Path, register_block: str) -> None:
    p = root / _REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("status: draft\ninteraction_mode: autonomous\n" + register_block, encoding="utf-8")


_ROWS = """requirement_register:
  rows:
  - id: R1
    surface: s
    status: open
  - id: R2
    surface: s
    status: partial
  - id: R3
    surface: s
    status: met
    wired: true
  - id: R4
    surface: s
    status: met
  - id: R5
    surface: s
    status: deferred
  - id: R6
    surface: s
    status: waived
    ruling:
      by: pm
"""


def test_names_every_blocking_row_and_only_those(tmp_path: Path) -> None:
    _write(tmp_path, _ROWS)
    owed = rr.open_register_rows(tmp_path, _REL)
    assert [r["id"] for r in owed] == ["R1", "R2", "R4", "R5"]
    assert {r["id"]: r["status"] for r in owed} == {
        "R1": "open",
        "R2": "partial",
        "R4": "met",
        "R5": "deferred",
    }
    assert "is open" in owed[0]["reason"]


def test_agrees_with_ship_refusal(tmp_path: Path) -> None:
    _write(tmp_path, _ROWS)
    assert rr.open_register_rows(tmp_path, _REL)
    assert rr.ship_refusal(rr.read_register((tmp_path / _REL).read_text(encoding="utf-8")))
    settled = """requirement_register:
  rows:
  - id: R1
    surface: s
    status: met
    wired: true
"""
    _write(tmp_path, settled)
    assert rr.open_register_rows(tmp_path, _REL) == []
    assert rr.ship_refusal(rr.read_register((tmp_path / _REL).read_text(encoding="utf-8"))) is None


def test_no_register_is_empty(tmp_path: Path) -> None:
    _write(tmp_path, "")
    assert rr.open_register_rows(tmp_path, _REL) == []
