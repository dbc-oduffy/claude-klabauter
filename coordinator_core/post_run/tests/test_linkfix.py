"""Tests for coordinator_core.post_run.linkfix: pin both sides of each link decision."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.post_run import linkfix


def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "docs" / "wiki", tmp_path / "docs" / "decisions"


def test_broken_link_rewritten_to_moved_target(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    page = _w(wiki / "fam" / "a.md", "see [b](./b.md#anchor)\n")
    _w(wiki / "other" / "b.md", "# b\n")
    res = linkfix.fix_to_fixpoint([page], [wiki], dec)
    assert page.read_text() == "see [b](../other/b.md#anchor)\n"
    assert res.changed == (page.resolve(),)
    assert res.residual == ()


def test_existing_target_untouched_even_when_basename_ambiguous(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    page = _w(wiki / "fam" / "a.md", "[i](./_index.md)\n")
    _w(wiki / "fam" / "_index.md", "# i\n")
    _w(wiki / "other" / "_index.md", "# j\n")
    before = page.read_bytes()
    res = linkfix.fix_to_fixpoint([page], [wiki], dec)
    assert page.read_bytes() == before
    assert res.changed == () and res.residual == ()


def test_two_candidate_basename_is_residual_not_picked(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    page = _w(wiki / "fam" / "a.md", "[i](./gone/_index.md)\n")
    _w(wiki / "x" / "_index.md", "# x\n")
    _w(wiki / "y" / "_index.md", "# y\n")
    before = page.read_bytes()
    res = linkfix.fix_to_fixpoint([page], [wiki], dec)
    assert page.read_bytes() == before
    assert [r.reason for r in res.residual] == ["ambiguous"]


def test_unresolvable_link_reported_never_deleted(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    page = _w(wiki / "a.md", "[n](./nowhere.md)\n")
    res = linkfix.fix_to_fixpoint([page], [wiki], dec)
    assert page.read_text() == "[n](./nowhere.md)\n"
    assert [(r.target, r.reason) for r in res.residual] == [("./nowhere.md", "unresolvable")]


def test_decisions_dr_rename_resolves_by_number(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    page = _w(wiki / "fam" / "a.md", "[d](../../decisions/DR-344-old-slug.md)\n")
    _w(dec / "DR-344-new-slug.md", "# d\n")
    res = linkfix.fix_to_fixpoint([page], [wiki], dec)
    assert page.read_text() == "[d](../../decisions/DR-344-new-slug.md)\n"
    assert res.residual == ()


def test_decisions_link_with_wrong_depth_resolves_by_basename(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    page = _w(wiki / "fam" / "a.md", "[d](../decisions/DR-7-x.md)\n")
    _w(dec / "DR-7-x.md", "# d\n")
    linkfix.fix_to_fixpoint([page], [wiki], dec)
    assert page.read_text() == "[d](../../decisions/DR-7-x.md)\n"


def test_two_links_fixed_across_pages_reach_fixpoint_with_residual_zero(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    a = _w(wiki / "fam" / "a.md", "[b](./b.md) and [d](./DR-9-old.md)\n")
    b = _w(wiki / "other" / "b.md", "[a](./a.md)\n")
    _w(dec / "DR-9-new.md", "# d\n")
    res = linkfix.fix_to_fixpoint([a, b], [wiki], dec)
    assert res.residual == ()
    assert a.read_text() == "[b](../other/b.md) and [d](../../decisions/DR-9-new.md)\n"
    assert b.read_text() == "[a](../fam/a.md)\n"
    again = linkfix.fix_to_fixpoint([a, b], [wiki], dec)
    assert again.changed == () and again.residual == () and again.passes == 1


def test_fenced_links_urls_and_crlf_left_alone(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    text = "```\n[x](./gone.md)\n```\n[u](https://e.com/gone.md)\r\n[k](./k.md)\r\n"
    page = _w(wiki / "fam" / "a.md", text)
    _w(wiki / "other" / "k.md", "# k\n")
    res = linkfix.fix_to_fixpoint([page], [wiki], dec)
    assert page.read_bytes().decode() == (
        "```\n[x](./gone.md)\n```\n[u](https://e.com/gone.md)\r\n[k](../other/k.md)\r\n"
    )
    assert res.residual == ()


def test_only_touched_pages_under_wiki_roots_are_rewritten(tmp_path: Path) -> None:
    wiki, dec = _roots(tmp_path)
    untouched = _w(wiki / "fam" / "u.md", "[b](./b.md)\n")
    outside = _w(tmp_path / "docs" / "plans" / "p.md", "[b](./b.md)\n")
    touched = _w(wiki / "fam" / "t.md", "# t\n")
    _w(wiki / "other" / "b.md", "# b\n")
    res = linkfix.fix_to_fixpoint([touched, outside, tmp_path / "missing.md"], [wiki], dec)
    assert untouched.read_text() == "[b](./b.md)\n"
    assert outside.read_text() == "[b](./b.md)\n"
    assert res.changed == ()
