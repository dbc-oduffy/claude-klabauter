"""Pure derivation of one dispatchable row per open ledger item."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import _clean, derive_ledger_rows


def _touch(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


def test_clean_strips_symbol_suffix():
    assert _clean("coordinator_core/machine_profile.py::apply_guard_level") == "coordinator_core/machine_profile.py"
    assert _clean("a/b.py::Cls.meth") == "a/b.py"


def test_s_arm_incident_derives_module_beside_serving_write(tmp_path):
    guards = "coordinator_core/bash_guards"
    for rel in (f"{guards}/guard_grep_via_bash.py", f"{guards}/_dc_rewrites.py",
                f"{guards}/tests/test_guard_grep_via_bash.py"):
        _touch(tmp_path, rel)
    serving = [f"{guards}/guard_grep_via_bash.py", f"{guards}/tests/test_guard_grep_via_bash.py"]
    rows, under = derive_ledger_rows(
        {"R1": {"remaining": ["compile gate on _dc_rewrites.check_grep_via_bash_rewrite"]}},
        ["please finish the compile gate"], ["R1"], {"R1": serving}, ["C1", "C2"], tmp_path)
    assert under == []
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "LR1" and row["register_rows"] == ["R1"] and row["disposition"] == "open"
    assert f"{guards}/_dc_rewrites.py" in row["writes"]
    assert serving[1] in row["writes"]
    assert "the ledger check judges R1 met" in row["body"]


def test_xs_arm_incident_derives_named_symbol_path(tmp_path):
    _touch(tmp_path, "coordinator_core/machine_profile.py")
    rows, under = derive_ledger_rows(
        {"R1": {"remaining": ["apply_guard_level must return a Stop-valid warn shape in "
                              "coordinator_core/machine_profile.py::apply_guard_level"]}},
        [], ["R1"], {}, [], tmp_path)
    assert under == [] and rows[0]["writes"] == ["coordinator_core/machine_profile.py"]


def test_id_suffixing_past_existing(tmp_path):
    _touch(tmp_path, "a/x.py")
    rows, _ = derive_ledger_rows({"R1": {"remaining": ["edit a/x.py"]}}, [], ["R1"], {}, ["LR1", "LR1-2"], tmp_path)
    assert rows[0]["id"] == "LR1-3"


def test_rerule_note_derives_nothing(tmp_path):
    _touch(tmp_path, "a/x.py")
    rows, under = derive_ledger_rows(
        {"R1": {"remaining": ["edit a/x.py"]}}, ["met in commit abc1234def"], ["R1"], {}, [], tmp_path)
    assert (rows, under) == ([], [])


def test_underivable_item_and_closed_item(tmp_path):
    rows, under = derive_ledger_rows(
        {"R1": {"remaining": ["do something vague"]}, "R2": {"remaining": []}},
        [], ["R1", "R2"], {}, [], tmp_path)
    assert rows == [] and under == ["R1"]


def test_claude_md_class_path_stays_out_of_writes(tmp_path):
    _touch(tmp_path, "a/x.py")
    _touch(tmp_path, "CLAUDE.md")
    rows, _ = derive_ledger_rows({"R1": {"remaining": ["edit a/x.py and CLAUDE.md"]}}, [], ["R1"], {}, [], tmp_path)
    assert rows[0]["writes"] == ["a/x.py"]


def test_note_only_item_derives_from_serving_writes(tmp_path):
    rows, under = derive_ledger_rows({}, ["finish the gate"], ["R1"], {"R1": ["a/x.py"]}, [], tmp_path)
    assert under == [] and rows[0]["writes"] == ["a/x.py"]
