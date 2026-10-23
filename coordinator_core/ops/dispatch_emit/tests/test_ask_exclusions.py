"""Pins ask_exclusions: which paths an ask rules out, and how a path is matched against them."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_exclusions import drop_excluded, excluded_paths, is_excluded


def _sz(text, status="open"):
    return {"requirement_register": {"rows": [{"id": "R1", "status": status, "source_text": text}]}}


def test_incident_phrasing_excludes_bare_dir_and_covers_its_files():
    ex = excluded_paths(_sz("Edit CLAUDE.md but I asked not to touch global-doctrine."))
    assert ex == ("global-doctrine",)
    assert is_excluded("global-doctrine/CLAUDE.md", ex)
    assert not is_excluded("CLAUDE.md", ex)


def test_negated_edit_excludes_only_that_path():
    ex = excluded_paths(_sz("Please do not edit docs/x.md and fix docs/y.md"))
    assert ex == ("docs/x.md",)
    assert is_excluded("docs/x.md", ex)
    assert not is_excluded("docs/y.md", ex)


def test_affirmative_edit_excludes_nothing():
    assert excluded_paths(_sz("edit docs/x.md")) == ()


def test_backslash_input_normalises():
    ex = excluded_paths(_sz(r"never touch docs\x.md"))
    assert ex == ("docs/x.md",)
    assert is_excluded("docs\\x.md", ex)
    assert drop_excluded(["docs\\x.md", "a/b.py"], ex) == ["a/b.py"]


def test_deferred_row_exclusion_still_binds():
    assert excluded_paths(_sz("don't modify pkg/mod.py", status="deferred")) == ("pkg/mod.py",)


def test_no_register_yields_empty():
    assert excluded_paths({}) == ()
    assert excluded_paths({"requirement_register": None}) == ()


def test_intent_and_relayed_text_are_read():
    sz = {"intent": "leave docs/a.md alone", "em_analysis": {"relayed_text": "excluding pkg/m.py"}}
    assert excluded_paths(sz) == ("docs/a.md", "pkg/m.py")


def test_list_object_and_prose_stop():
    ex = excluded_paths(_sz("do not touch a/b.py, c/d.py because they are frozen"))
    assert ex == ("a/b.py", "c/d.py")


def test_drop_excluded_preserves_order_and_empty_passthrough():
    assert drop_excluded(["z.py", "a/b.py", "a/c.py"], ["a"]) == ["z.py"]
    assert drop_excluded(["z.py", "a.py"], ()) == ["z.py", "a.py"]
    assert not is_excluded("ab/c.py", ["a"])
