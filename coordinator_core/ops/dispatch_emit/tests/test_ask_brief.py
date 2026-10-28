"""ask_brief loading, refusals and JS rendering; GateVerdict.resume_plan emission."""

import pytest

from coordinator_core.ops.dispatch_emit.ask_brief import (
    EM_BRIEF_BYTE_CAP,
    EmBriefRefused,
    EmBrief,
    brief_decl_js,
    check_em_brief_paths,
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


def test_over_cap_spills_instead_of_refusing(tmp_path):
    full = "a" * (EM_BRIEF_BYTE_CAP + 1)
    b = load_em_brief(tmp_path, brief=full, brief_file=None)
    assert len(b.text.encode("utf-8")) == EM_BRIEF_BYTE_CAP
    assert len(b.advisories) == 1 and "over" in b.advisories[0]
    spill = tmp_path / b.context[0]
    assert spill.read_text(encoding="utf-8") == full
    assert receipt_fields(b)["advisories"] == list(b.advisories)


def test_over_cap_brief_file_is_its_own_spill(tmp_path):
    (tmp_path / "big.md").write_text("b" * (EM_BRIEF_BYTE_CAP + 5))
    b = load_em_brief(tmp_path, brief=None, brief_file="big.md")
    assert b.context[0] == "big.md" and len(b.advisories) == 1


def test_under_cap_has_no_advisory(tmp_path):
    b = load_em_brief(tmp_path, brief="x", brief_file=None)
    assert b.advisories == () and "advisories" not in receipt_fields(b)


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


def _fake_tmp(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    pad = tmp / "claude" / "E--proj" / "1234-uuid" / "scratchpad"
    pad.mkdir(parents=True)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp))
    root = tmp_path / "repo"
    root.mkdir()
    return tmp, pad, root


def test_scratchpad_context_admitted_absolute(tmp_path, monkeypatch):
    _, pad, root = _fake_tmp(tmp_path, monkeypatch)
    f = pad / "cap.md"
    f.write_text("c")
    b = load_em_brief(root, brief="x", brief_file=None, context=[str(f)], session_id="1234-uuid")
    assert b.context == (f.resolve().as_posix(),)
    assert receipt_fields(b)["context"] == [f.resolve().as_posix()]
    check_em_brief_paths(root, brief_file=None, context=[str(f)])


@pytest.mark.parametrize("sid", ["9999-other", ""])
def test_other_or_empty_session_scratchpad_refused(tmp_path, monkeypatch, sid):
    _, pad, root = _fake_tmp(tmp_path, monkeypatch)
    f = pad / "cap.md"
    f.write_text("c")
    with pytest.raises(EmBriefRefused, match="outside the repo root"):
        load_em_brief(root, brief="x", brief_file=None, context=[str(f)], session_id=sid)


def test_non_scratchpad_temp_and_sibling_repo_still_refused(tmp_path, monkeypatch):
    tmp, pad, root = _fake_tmp(tmp_path, monkeypatch)
    other = tmp / "claude" / "E--proj" / "1234-uuid" / "other.md"
    other.write_text("c")
    sibling = tmp_path / "sibling.md"
    sibling.write_text("c")
    for f in (other, sibling):
        with pytest.raises(EmBriefRefused, match="outside the repo root"):
            load_em_brief(root, brief="x", brief_file=None, context=[str(f)])


def test_brief_file_in_scratchpad_still_refused(tmp_path, monkeypatch):
    _, pad, root = _fake_tmp(tmp_path, monkeypatch)
    f = pad / "b.md"
    f.write_text("hello")
    with pytest.raises(EmBriefRefused, match="outside the repo root"):
        load_em_brief(root, brief=None, brief_file=str(f))


def test_backslash_context_path_resolves_to_the_same_file(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ctx.md").write_text("c", encoding="utf-8")
    b = load_em_brief(tmp_path, brief="t", brief_file=None, context=["docs\\ctx.md"])
    assert b.context == ("docs/ctx.md",)
