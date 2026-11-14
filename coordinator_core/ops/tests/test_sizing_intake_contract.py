"""Pins the intake-sizing contract (sizing_intake_contract).

Run: python -m pytest coordinator_core/ops/tests/test_sizing_intake_contract.py -q
"""

from __future__ import annotations

import copy

import pytest

from coordinator_core.frontmatter import schema_validate
from coordinator_core.ops import sizing_intake_contract as c

_SCHEMA = (schema_validate._SCHEMAS_DIR / "sizing-object.schema.json")
_TURN = {"turn": 4, "ts": "2026-10-14T10:00:00Z", "text": "ship it"}


def _rec(**kw):
    return c.intake_record(name="x", turn=_TURN, today="2026-10-14", **kw)


def test_intake_record_validates():
    assert schema_validate.validate_frontmatter(_rec(), _SCHEMA) == []


def test_intake_record_seeds_exactly_one_verbatim():
    rec = _rec()
    assert rec["pm_verbatims"] == [_TURN]
    assert rec["intent"] == "ship it"
    assert rec["intent_source"] == "pm-verbatim"
    assert rec["status"] == "draft"


def test_intake_record_accepts_log_prompt_key():
    rec = c.intake_record(name="x", turn={"turn": 0, "prompt": "hi"}, today="d")
    assert rec["pm_verbatims"] == [{"turn": 0, "text": "hi"}]


def test_intake_record_refuses_blank_turn():
    with pytest.raises(ValueError):
        c.intake_record(name="x", turn={"text": "  "}, today="d")


def test_intake_record_clamps_name_to_schema_max():
    rec = c.intake_record(name="n" * 100, turn=_TURN, today="d")
    assert len(rec["name"]) == 60
    assert schema_validate.validate_frontmatter(rec, _SCHEMA) == []


@pytest.mark.parametrize("value", [None, "", "   ", 5])
def test_read_em_brief_absent_or_blank(value):
    doc = _rec()
    if value is not None:
        doc["em_analysis"][c.EM_BRIEF_KEY] = value
    assert c.read_em_brief(doc) is None
    assert c.read_em_brief({}) is None


def test_read_em_brief_set_is_stripped():
    doc = _rec()
    doc["em_analysis"][c.EM_BRIEF_KEY] = "  do X first \n"
    assert c.read_em_brief(doc) == "do X first"
    assert schema_validate.validate_frontmatter(doc, _SCHEMA) == []


def test_is_intake_true_only_for_the_mints_shape():
    doc = _rec()
    assert c.is_intake(doc)
    for mutate in (
        lambda d: d.pop("em_analysis"),
        lambda d: d["em_analysis"].update(intake="other"),
        lambda d: d.update(status="routed"),
    ):
        other = copy.deepcopy(doc)
        mutate(other)
        assert not c.is_intake(other)
    assert not c.is_intake({"route": "plan", "status": "draft"})


def test_read_em_context_paths_splits_strips_and_drops_blanks():
    doc = {"em_analysis": {"em_context_paths": " a.md \n\nb/c.md"}}
    assert c.read_em_context_paths(doc) == ("a.md", "b/c.md")


@pytest.mark.parametrize("doc", [
    {}, {"em_analysis": {}}, {"em_analysis": {"em_context_paths": ["a.md"]}},
    {"em_analysis": {"em_context_paths": None}}, "not a mapping",
])
def test_read_em_context_paths_absent_or_non_string_is_empty(doc):
    assert c.read_em_context_paths(doc) == ()


def test_em_context_paths_value_joins_stripped_nonblank_lines():
    assert c.em_context_paths_value([" a.md ", "", "b/c.md"]) == "a.md\nb/c.md"
    assert c.em_context_paths_value([]) is None
    assert c.em_context_paths_value(["  ", ""]) is None


@pytest.fixture
def roots(tmp_path, monkeypatch):
    from coordinator_core import _fleet_names

    repo, home, sib = tmp_path / "me", tmp_path / "home", tmp_path / "sib" / "other"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(_fleet_names, "_registry_paths", lambda: [str(sib)])
    return repo, home, sib


def test_context_paths_normalize_to_portable_tokens(roots):
    repo, home, sib = roots
    value = c.em_context_paths_value(
        [str(repo / "docs" / "a.md"), str(home / "bin" / "x.py"), str(sib / "n" / "b.md")], repo_root=repo
    )
    assert value == "docs/a.md\n~/.coordinator-claude-settings/bin/x.py\n<repo:other>/n/b.md"


def test_session_scratchpad_path_is_written_as_the_scratchpad_token(roots, tmp_path):
    repo, _, _ = roots
    pad = tmp_path / "tmp" / "claude" / "E--proj" / "1234-uuid" / "scratchpad" / "sub" / "cap.md"
    assert c.em_context_paths_value([str(pad)], repo_root=repo) == "<scratchpad>/sub/cap.md"


def test_context_path_under_no_root_is_kept_as_is(roots, tmp_path):
    repo, _, _ = roots
    stray = str(tmp_path / "elsewhere" / "z.md")
    assert c.em_context_paths_value([stray], repo_root=repo) == stray


def test_context_path_normalization_needs_a_root_boundary(roots, tmp_path):
    repo, _, _ = roots
    lookalike = str(tmp_path / "me-not" / "a.md")
    assert c.em_context_paths_value([lookalike], repo_root=repo) == lookalike


def test_context_path_without_repo_root_still_tokenizes_settings_home(roots):
    _, home, _ = roots
    assert c.em_context_paths_value([str(home / "k.md")]) == "~/.coordinator-claude-settings/k.md"


def test_context_paths_round_trip_survives_normalization(roots):
    repo, _, _ = roots
    value = c.em_context_paths_value([str(repo / "docs" / "a.md")], repo_root=repo)
    assert c.read_em_context_paths({"em_analysis": {c.EM_CONTEXT_PATHS_KEY: value}}) == ("docs/a.md",)


def test_em_context_paths_round_trip():
    value = c.em_context_paths_value([" a.md ", "b/c.md"])
    assert c.read_em_context_paths({"em_analysis": {c.EM_CONTEXT_PATHS_KEY: value}}) == ("a.md", "b/c.md")


def test_with_em_context_starts_with_marker_and_keeps_brief_first():
    assert c.with_em_context(None, " x ") == f"{c.EM_CONTEXT_MARKER}\nx"
    assert c.with_em_context("b", "x") == f"b\n\n{c.EM_CONTEXT_MARKER}\nx"
    assert c.EM_CONTEXT_MARKER == "EM context (not the PM's words):"


def test_is_path_shaped():
    assert c.is_path_shaped("docs/a.md")
    assert c.is_path_shaped("a\\b")
    assert not c.is_path_shaped("a bug in x")
    assert not c.is_path_shaped("  ")
    assert not c.is_path_shaped("word")
    assert not c.is_path_shaped("process_util.py:266")
    assert not c.is_path_shaped("https://example.com/a.md")
    assert c.is_path_shaped("C:\\repo\\a.md")
