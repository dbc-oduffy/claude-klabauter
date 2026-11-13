"""Tests for coordinator_core.post_run.nav_index: scoped rebuild, determinism, byte-stability."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.post_run import nav_index


def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_new_family_dir_gains_index_listing_exactly_its_pages(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _w(wiki / "top.md", "# Top\n\nTop page. More.\n")
    b = _w(wiki / "fam" / "b.md", "# B page\n\nBee summary here. Extra.\n")
    _w(wiki / "fam" / "a.md", "---\nx: 1\n---\n# A page\n\nAye.\n")
    res = nav_index.rebuild([b], [wiki])
    fam = (wiki / "fam" / "_index.md").read_text()
    assert fam == (
        "[wiki](../_index.md) > fam\n\n# fam\n\n"
        "- [A page](a.md) -- Aye.\n- [B page](b.md) -- Bee summary here.\n"
    )
    root = (wiki / "_index.md").read_text()
    assert root == "# wiki\n\n- [fam/](fam/_index.md)\n\n- [Top](top.md) -- Top page.\n"
    assert sorted(p.parent.name for p in res.written) == ["fam", "wiki"]


def test_unchanged_index_is_byte_identical_and_not_rewritten(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    a = _w(wiki / "fam" / "a.md", "# A\n\nAye.\n")
    nav_index.rebuild([a], [wiki])
    idx = wiki / "fam" / "_index.md"
    before = idx.read_bytes()
    mtime = idx.stat().st_mtime_ns
    res = nav_index.rebuild([a], [wiki])
    assert idx.read_bytes() == before and idx.stat().st_mtime_ns == mtime
    assert res.written == () and idx in res.unchanged


def test_stale_index_refreshed_when_page_added(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    a = _w(wiki / "fam" / "a.md", "# A\n\nAye.\n")
    nav_index.rebuild([a], [wiki])
    c = _w(wiki / "fam" / "c.md", "# C\n\nSea.\n")
    nav_index.rebuild([c], [wiki])
    text = (wiki / "fam" / "_index.md").read_text()
    assert "[A](a.md)" in text and "[C](c.md)" in text


def test_only_dirs_with_a_touched_page_are_rebuilt(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    a = _w(wiki / "one" / "a.md", "# A\n")
    _w(wiki / "two" / "b.md", "# B\n")
    nav_index.rebuild([a], [wiki])
    assert (wiki / "one" / "_index.md").is_file()
    assert not (wiki / "two" / "_index.md").exists()


def test_family_parent_page_lists_in_parent_index_and_heads_its_own(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    parent = _w(wiki / "fam.md", "# Family title\n\nWhat the family covers.\n")
    _w(wiki / "fam" / "kid.md", "# Kid\n")
    nav_index.rebuild([parent], [wiki])
    assert "- [Family title/](fam/_index.md) -- What the family covers." in (wiki / "_index.md").read_text()
    kid_idx = (wiki / "fam" / "_index.md").read_text()
    assert "# Family title" in kid_idx and "Parent page: [Family title](../fam.md)" in kid_idx
    assert "fam.md)" not in (wiki / "_index.md").read_text().split("\n\n")[-1]


def test_dotfiles_index_and_crlf_handled(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    a = _w(wiki / "fam" / "a.md", "# A\r\n\r\nAye.\r\n")
    _w(wiki / "fam" / ".hidden.md", "# H\n")
    _w(wiki / "fam" / "_index.md", "hand written\n")
    nav_index.rebuild([a], [wiki])
    data = (wiki / "fam" / "_index.md").read_bytes()
    assert b"\r" not in data and b"hidden" not in data and b"hand written" not in data


def test_touched_outside_roots_and_index_files_ignored(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    _w(wiki / "fam" / "a.md", "# A\n")
    outside = _w(tmp_path / "plans" / "p.md", "# P\n")
    res = nav_index.rebuild([outside, wiki / "fam" / "_index.md"], [wiki])
    assert res.written == () and not (wiki / "fam" / "_index.md").exists()
