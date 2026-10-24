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


_LR1_NOTES = [
    "C1 BLOCKED on footprint: widen C1 writes to coordinator_core/plugin_health/forwarder_drift.py "
    "and its own test file. Classify installed names as retire-pending, named, not orphaned. "
    "Pin with tests.",
    "EM ruling: R1's guard-test failures are out of scope here. Publish to klabauter and "
    "scripts/setup.py --i-am-agent are EM post-commit acts. Land the forwarder_drift.py change "
    "and its tests; R1 is met on that commit.",
]
_BIN = "coordinator/bin"


def _fake(backed: dict):
    return lambda paths: {p: backed.get(p) for p in paths}


def _lr1_backing():
    return {
        f"{_BIN}/blocked.py": f"{_BIN}/blocked.py",
        f"{_BIN}/publish.py": f"{_BIN}/publish.py",
        f"{_BIN}/Publish.py": f"{_BIN}/publish.py",
    }


def test_spelling_lr1_incident_declares_no_case_variant_modules(tmp_path):
    serving = [f"{_BIN}/check-forwarder-drift.py"]
    rows, _ = derive_ledger_rows(
        {"R1": {"remaining": ["BLOCKED footprint", "Publish then run"]}}, _LR1_NOTES, ["R1"],
        {"R1": serving}, [], tmp_path, spelling=_fake(_lr1_backing()))
    writes = rows[0]["writes"]
    assert f"{_BIN}/BLOCKED.py" not in writes and f"{_BIN}/Publish.py" not in writes
    assert writes == serving


def test_spelling_module_admitted_only_verbatim(tmp_path):
    rows, _ = derive_ledger_rows(
        {"R1": {"remaining": ["finish blocked and publish"]}}, [], ["R1"],
        {"R1": [f"{_BIN}/x.py"]}, [], tmp_path, spelling=_fake(_lr1_backing()))
    assert rows[0]["writes"] == [f"{_BIN}/blocked.py", f"{_BIN}/publish.py", f"{_BIN}/x.py"]


def test_spelling_named_case_variant_becomes_tracked_spelling(tmp_path):
    rows, _ = derive_ledger_rows(
        {"R1": {"remaining": [f"edit {_BIN}/Publish.py"]}}, [], ["R1"], {}, [], tmp_path,
        spelling=_fake(_lr1_backing()))
    assert rows[0]["writes"] == [f"{_BIN}/publish.py"]


def test_spelling_unbacked_named_dropped_and_named_in_body(tmp_path):
    rows, _ = derive_ledger_rows(
        {"R1": {"remaining": [f"edit {_BIN}/Publish.py and {_BIN}/tests/test_Publish.py"]}}, [], ["R1"],
        {}, [], tmp_path, spelling=_fake(_lr1_backing()))
    assert rows[0]["writes"] == [f"{_BIN}/publish.py"]
    assert f"Not declared (no tracked or present path): {_BIN}/tests/test_Publish.py" in rows[0]["body"]


def test_spelling_unbacked_serving_write_kept_and_variant_canonicalized(tmp_path):
    serving = [f"{_BIN}/new_thing.py", f"{_BIN}/Publish.py"]
    rows, _ = derive_ledger_rows(
        {"R1": {"remaining": [f"edit {_BIN}/new_thing.py"]}}, [], ["R1"], {"R1": serving}, [], tmp_path,
        spelling=_fake(_lr1_backing()))
    assert rows[0]["writes"] == [f"{_BIN}/new_thing.py", f"{_BIN}/publish.py"]
    assert "Not declared" not in rows[0]["body"]


def test_spelling_none_ignores_resolver_free_behaviour(tmp_path):
    _touch(tmp_path, "a/x.py")
    rows, _ = derive_ledger_rows({"R1": {"remaining": ["edit a/x.py"]}}, [], ["R1"], {}, [], tmp_path)
    assert rows[0]["writes"] == ["a/x.py"] and "Not declared" not in rows[0]["body"]


def test_remaining_line_is_framed_as_gap_not_title_spec(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import GAP_TITLE_PREFIX, HALT_GAP_RULE

    _touch(tmp_path, "a/x.py")
    line = "verify-stage prompts do not carry the corpus-probe clause in a/x.py"
    rows, _ = derive_ledger_rows({"R2": {"remaining": [line]}}, [], ["R2"], {}, [], tmp_path)
    row = rows[0]
    assert row["title"] != line and row["title"].startswith(GAP_TITLE_PREFIX)
    assert row["title"] == "Close gap: " + line
    assert HALT_GAP_RULE in row["body"]
    assert "Gaps to close (each line is still true; make it false):" in row["body"]
    assert f"- {line}" in row["body"]
    assert "no gap line above holds at HEAD and the ledger check judges R2 met" in row["body"]


def test_note_only_item_title_is_not_gap_prefixed(tmp_path):
    rows, _ = derive_ledger_rows({}, ["finish the gate"], ["R1"], {"R1": ["a/x.py"]}, [], tmp_path)
    assert rows[0]["title"] == "Serve revise note for R1"


def test_note_only_item_derives_from_serving_writes(tmp_path):
    rows, under = derive_ledger_rows({}, ["finish the gate"], ["R1"], {"R1": ["a/x.py"]}, [], tmp_path)
    assert under == [] and rows[0]["writes"] == ["a/x.py"]
