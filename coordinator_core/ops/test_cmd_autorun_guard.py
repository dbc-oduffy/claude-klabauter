from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops import cmd_autorun_guard as mod


def _fake_registry(monkeypatch, initial=None):
    state = {"value": initial}

    def _read():
        return state["value"]

    def _write(value):
        state["value"] = value

    def _delete():
        state["value"] = None

    monkeypatch.setattr(mod, "_read_autorun", _read)
    monkeypatch.setattr(mod, "_write_autorun", _write)
    monkeypatch.setattr(mod, "_delete_autorun", _delete)
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    return state


def _allow_mutation(monkeypatch):
    monkeypatch.setattr(mod, "_refuse_if_disabled", lambda what: None)


def test_detect_not_windows(monkeypatch):
    monkeypatch.setattr(mod, "_is_windows", lambda: False)
    result = mod.detect_cmd_autorun_coverage()
    assert result == {"classification": "not_windows", "current_value": None}


def test_detect_uncovered_when_unset(monkeypatch):
    _fake_registry(monkeypatch, initial=None)
    result = mod.detect_cmd_autorun_coverage()
    assert result == {"classification": "uncovered", "current_value": None}


def test_detect_uncovered_when_empty(monkeypatch):
    _fake_registry(monkeypatch, initial="")
    result = mod.detect_cmd_autorun_coverage()
    assert result["classification"] == "uncovered"


def test_detect_covered(monkeypatch):
    _fake_registry(monkeypatch, initial=mod._DESIRED_MACRO)
    result = mod.detect_cmd_autorun_coverage()
    assert result["classification"] == "covered"


def test_detect_foreign_present(monkeypatch):
    _fake_registry(monkeypatch, initial="doskey ls=dir /w")
    result = mod.detect_cmd_autorun_coverage()
    assert result["classification"] == "foreign_present"
    assert result["current_value"] == "doskey ls=dir /w"


def test_detect_foreign_present_when_macro_text_is_an_unanchored_substring(monkeypatch):
    foreign = f"rem {mod._DESIRED_MACRO} (disabled by IT policy)"
    _fake_registry(monkeypatch, initial=foreign)
    result = mod.detect_cmd_autorun_coverage()
    assert result["classification"] == "foreign_present"
    assert result["current_value"] == foreign


def test_strip_is_a_clean_noop_when_macro_text_is_an_unanchored_substring(monkeypatch):
    foreign = f"rem {mod._DESIRED_MACRO} (disabled by IT policy)"
    state = _fake_registry(monkeypatch, initial=foreign)
    _allow_mutation(monkeypatch)
    result = mod.strip_cmd_autorun_guard()
    assert result["modified"] is False
    assert state["value"] == foreign


def test_write_not_windows_noop(monkeypatch):
    monkeypatch.setattr(mod, "_is_windows", lambda: False)
    result = mod.write_cmd_autorun_guard()
    assert result == {"classification": "not_windows", "modified": False, "would_modify": False}


def test_write_sets_when_unset(monkeypatch):
    state = _fake_registry(monkeypatch, initial=None)
    _allow_mutation(monkeypatch)
    result = mod.write_cmd_autorun_guard()
    assert result["modified"] is True
    assert state["value"] == mod._DESIRED_MACRO


def test_write_appends_to_existing_foreign_content(monkeypatch):
    state = _fake_registry(monkeypatch, initial="doskey ls=dir /w")
    _allow_mutation(monkeypatch)
    result = mod.write_cmd_autorun_guard()
    assert result["modified"] is True
    assert state["value"] == f"doskey ls=dir /w{mod._JOIN}{mod._DESIRED_MACRO}"


def test_write_noop_when_already_covered(monkeypatch):
    state = _fake_registry(monkeypatch, initial=mod._DESIRED_MACRO)
    _allow_mutation(monkeypatch)
    result = mod.write_cmd_autorun_guard()
    assert result == {"classification": "covered", "modified": False, "would_modify": False}
    assert state["value"] == mod._DESIRED_MACRO


def test_write_check_only_never_mutates(monkeypatch):
    state = _fake_registry(monkeypatch, initial=None)
    _allow_mutation(monkeypatch)
    result = mod.write_cmd_autorun_guard(check_only=True)
    assert result["modified"] is False
    assert result["would_modify"] is True
    assert state["value"] is None


def test_write_refused_by_disable_gate_never_mutates(monkeypatch):
    state = _fake_registry(monkeypatch, initial=None)
    monkeypatch.setattr(mod, "_refuse_if_disabled", lambda what: "COORDINATOR_DISABLE_MACHINE_MUTATION=1")
    result = mod.write_cmd_autorun_guard()
    assert result["modified"] is False
    assert result["would_modify"] is True
    assert state["value"] is None


def test_strip_not_windows_noop(monkeypatch):
    monkeypatch.setattr(mod, "_is_windows", lambda: False)
    result = mod.strip_cmd_autorun_guard()
    assert result["modified"] is False


def test_strip_uncovered_noop(monkeypatch):
    state = _fake_registry(monkeypatch, initial=None)
    _allow_mutation(monkeypatch)
    result = mod.strip_cmd_autorun_guard()
    assert result["modified"] is False
    assert state["value"] is None


def test_strip_deletes_value_when_macro_is_entire_content(monkeypatch):
    state = _fake_registry(monkeypatch, initial=mod._DESIRED_MACRO)
    _allow_mutation(monkeypatch)
    result = mod.strip_cmd_autorun_guard()
    assert result["modified"] is True
    assert state["value"] is None


def test_strip_preserves_foreign_content_appended_after(monkeypatch):
    original = f"doskey ls=dir /w{mod._JOIN}{mod._DESIRED_MACRO}"
    state = _fake_registry(monkeypatch, initial=original)
    _allow_mutation(monkeypatch)
    result = mod.strip_cmd_autorun_guard()
    assert result["modified"] is True
    assert state["value"] == "doskey ls=dir /w"


def test_strip_preserves_foreign_content_appended_before_our_macro(monkeypatch):
    original = f"{mod._DESIRED_MACRO}{mod._JOIN}doskey ls=dir /w"
    state = _fake_registry(monkeypatch, initial=original)
    _allow_mutation(monkeypatch)
    result = mod.strip_cmd_autorun_guard()
    assert result["modified"] is True
    assert state["value"] == "doskey ls=dir /w"


def test_strip_check_only_never_mutates(monkeypatch):
    state = _fake_registry(monkeypatch, initial=mod._DESIRED_MACRO)
    _allow_mutation(monkeypatch)
    result = mod.strip_cmd_autorun_guard(check_only=True)
    assert result["modified"] is False
    assert result["would_modify"] is True
    assert state["value"] == mod._DESIRED_MACRO


def test_strip_refused_by_disable_gate_never_mutates(monkeypatch):
    state = _fake_registry(monkeypatch, initial=mod._DESIRED_MACRO)
    monkeypatch.setattr(mod, "_refuse_if_disabled", lambda what: "COORDINATOR_DISABLE_MACHINE_MUTATION=1")
    result = mod.strip_cmd_autorun_guard()
    assert result["modified"] is False
    assert result["would_modify"] is True
    assert state["value"] == mod._DESIRED_MACRO


def test_round_trip_write_then_strip_restores_original(monkeypatch):
    original = "doskey ls=dir /w"
    state = _fake_registry(monkeypatch, initial=original)
    _allow_mutation(monkeypatch)
    mod.write_cmd_autorun_guard()
    assert state["value"] != original
    mod.strip_cmd_autorun_guard()
    assert state["value"] == original


def test_round_trip_write_then_strip_restores_unset(monkeypatch):
    state = _fake_registry(monkeypatch, initial=None)
    _allow_mutation(monkeypatch)
    mod.write_cmd_autorun_guard()
    assert state["value"] is not None
    mod.strip_cmd_autorun_guard()
    assert state["value"] is None


def test_detect_handler(monkeypatch):
    _fake_registry(monkeypatch, initial=None)
    result = asyncio.run(mod._detect_handler({}, repo_root=None))
    assert result["classification"] == "uncovered"


def test_write_handler_check_only(monkeypatch):
    state = _fake_registry(monkeypatch, initial=None)
    _allow_mutation(monkeypatch)
    result = asyncio.run(mod._write_handler({"check_only": True}, repo_root=None))
    assert result["modified"] is False
    assert state["value"] is None


def test_strip_handler_check_only(monkeypatch):
    state = _fake_registry(monkeypatch, initial=mod._DESIRED_MACRO)
    _allow_mutation(monkeypatch)
    result = asyncio.run(mod._strip_handler({"check_only": True}, repo_root=None))
    assert result["modified"] is False
    assert state["value"] == mod._DESIRED_MACRO


def test_main_detect_default_verb(monkeypatch, capsys):
    _fake_registry(monkeypatch, initial=None)
    rc = mod.main([])
    assert rc == 0
    assert "uncovered" in capsys.readouterr().out


def test_main_apply_check_only(monkeypatch, capsys):
    state = _fake_registry(monkeypatch, initial=None)
    _allow_mutation(monkeypatch)
    rc = mod.main(["apply", "--check-only"])
    assert rc == 0
    assert state["value"] is None


def test_main_unrecognized_verb(monkeypatch, capsys):
    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    rc = mod.main(["bogus"])
    assert rc == 2
    assert "unrecognized verb" in capsys.readouterr().err
