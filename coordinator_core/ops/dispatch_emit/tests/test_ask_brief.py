"""ask_brief loading, refusals and JS rendering; GateVerdict.resume_plan emission."""

import pytest

from coordinator_core.ops.dispatch_emit.ask_brief import (
    EM_BRIEF_BYTE_CAP,
    EmBriefRefused,
    EmBrief,
    brief_decl_js,
    load_em_brief,
    receipt_fields,
)
from coordinator_core.ops.dispatch_emit.ask_contract import GateVerdict


def test_all_empty_is_none(tmp_path):
    assert load_em_brief(tmp_path, brief=None, brief_file=None) is None
    assert load_em_brief(tmp_path, brief="", brief_file="", context=()) is None


def test_brief_and_file_refused_naming_both(tmp_path):
    with pytest.raises(EmBriefRefused) as e:
        load_em_brief(tmp_path, brief="x", brief_file="b.md")
    assert "--brief" in str(e.value) and "--brief-file" in str(e.value)


def test_missing_context_names_path(tmp_path):
    with pytest.raises(EmBriefRefused, match="nope.md"):
        load_em_brief(tmp_path, brief="x", brief_file=None, context=["nope.md"])


def test_context_outside_repo_refused(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "out.md").write_text("x")
    with pytest.raises(EmBriefRefused, match="outside"):
        load_em_brief(root, brief="x", brief_file=None, context=["../out.md"])


def test_over_cap_refused(tmp_path):
    with pytest.raises(EmBriefRefused, match="exceeds"):
        load_em_brief(tmp_path, brief="a" * (EM_BRIEF_BYTE_CAP + 1), brief_file=None)


def test_brief_file_read_and_empty_refused(tmp_path):
    (tmp_path / "b.md").write_text("hello")
    b = load_em_brief(tmp_path, brief=None, brief_file="b.md")
    assert b.text == "hello" and b.source == "b.md"
    (tmp_path / "e.md").write_text("  \n")
    with pytest.raises(EmBriefRefused, match="empty"):
        load_em_brief(tmp_path, brief=None, brief_file="e.md")
    with pytest.raises(EmBriefRefused, match="missing.md"):
        load_em_brief(tmp_path, brief=None, brief_file="missing.md")


def test_decl_round_trips_escapes():
    b = EmBrief(text="it's a \\ path\nline2", source="inline", context=("a/b.md",))
    js = brief_decl_js(b)
    assert js.startswith("const _EM_BRIEF = ")
    assert "\\'" in js and "\\\\" in js and "\\n" in js and "\n" not in js
    assert "Read each context file first: a/b.md" in js


def test_receipt_fields(tmp_path):
    (tmp_path / "c.md").write_text("c")
    b = load_em_brief(tmp_path, brief="x", brief_file=None, context=["c.md"])
    r = receipt_fields(b)
    assert r["source"] == "inline" and r["context"] == ["c.md"] and len(r["sha256"]) == 64


def test_resume_plan_emitted_only_when_set():
    assert "resume_plan" not in GateVerdict(arm="s", halt=None).to_json()
    assert GateVerdict(arm="s", halt=None, resume_plan="docs/plans/p.md").to_json()["resume_plan"] == "docs/plans/p.md"


def test_backslash_context_path_resolves_to_the_same_file(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ctx.md").write_text("c", encoding="utf-8")
    b = load_em_brief(tmp_path, brief="t", brief_file=None, context=["docs\\ctx.md"])
    assert b.context == ("docs/ctx.md",)
