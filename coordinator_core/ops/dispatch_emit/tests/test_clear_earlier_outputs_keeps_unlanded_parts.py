"""`_clear_earlier_outputs` keeps an unlanded split part's script and spine, and still removes
landed or receipt-less ones."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import cli as cli_module

_BASE = "20261021T182452-4b88be1d"


def _make(d: Path, name: str, receipt: dict | None) -> Path:
    path = d / name
    path.write_text("// script\n", encoding="utf-8")
    if receipt is not None:
        (d / (name + ".emitted.json")).write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_unlanded_part_survives_sibling_tranche_emit(tmp_path, capsys):
    d = tmp_path
    (d / f"{_BASE}-t2.md").write_text("rec", encoding="utf-8")
    t3 = d / f"{_BASE}-t3.md"
    t3.write_text("rec", encoding="utf-8")
    p1 = _make(d, f"{_BASE}-t2-p1.workflow.mjs", {"sha256": "a", "landed": {"sha": "abc", "at": "t"}})
    p2 = _make(d, f"{_BASE}-t2-p2.workflow.mjs", {"sha256": "b"})
    bare = _make(d, f"{_BASE}-t2-p3.workflow.mjs", None)
    spines = {n: d / f"{_BASE}-t2-{n}.spine.md" for n in ("p1", "p2", "p3")}
    for s in spines.values():
        s.write_text("spine", encoding="utf-8")
    live = d / f"{_BASE}-t3.workflow.mjs"
    live.write_text("// live", encoding="utf-8")

    cli_module._clear_earlier_outputs(str(t3), [live])

    assert p2.is_file() and spines["p2"].is_file()
    assert p1.is_file() and spines["p1"].is_file()
    assert not bare.exists() and not spines["p3"].exists()
    assert live.is_file()
    err = capsys.readouterr().err
    assert f"kept unlanded part {p2}" in err
    assert f"kept spent part {p1}" in err
    assert f"kept unlanded part {p1}" not in err


def test_unlanded_spine_without_script_survives_and_landed_one_goes(tmp_path):
    t3 = tmp_path / f"{_BASE}-t3.md"
    t3.write_text("rec", encoding="utf-8")
    unlanded = tmp_path / f"{_BASE}-t2.spine.md"
    unlanded.write_text("spine", encoding="utf-8")
    landed = tmp_path / f"{_BASE}-t1.spine.md"
    landed.write_text("spine", encoding="utf-8")
    (tmp_path / f"{_BASE}-t1.workflow.mjs.emitted.json").write_text(
        json.dumps({"sha256": "a", "landed": {"sha": "abc", "at": "t"}}), encoding="utf-8"
    )

    cli_module._clear_earlier_outputs(str(t3), [])

    assert unlanded.is_file()
    assert not landed.exists()


def test_resume_sibling_spends_a_receiptless_part(tmp_path, capsys):
    t2 = tmp_path / f"{_BASE}-t2.md"
    t2.write_text("rec", encoding="utf-8")
    part = _make(tmp_path, f"{_BASE}-t1-p1.workflow.mjs", None)
    spine = tmp_path / f"{_BASE}-t1-p1.spine.md"
    spine.write_text("spine", encoding="utf-8")
    resume = _make(tmp_path, f"{_BASE}-t1-p1-resume.workflow.mjs", None)

    cli_module._clear_earlier_outputs(str(t2), [])

    assert part.is_file() and spine.is_file() and resume.is_file()
    assert f"kept spent part {part}" in capsys.readouterr().err


def test_unlanded_tranche_script_is_kept_too(tmp_path):
    t3 = tmp_path / f"{_BASE}-t3.md"
    t3.write_text("rec", encoding="utf-8")
    t2 = _make(tmp_path, f"{_BASE}-t2.workflow.mjs", {"sha256": "x"})
    cli_module._clear_earlier_outputs(str(t3), [])
    assert t2.is_file()
