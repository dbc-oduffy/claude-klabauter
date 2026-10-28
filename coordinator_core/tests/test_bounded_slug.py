"""bounded_slug: width, word boundary, fallback, and NAME_MAX through each scaffold path."""

import os

import pytest

from coordinator_core.bounded_slug import NAME_MAX, bounded_slug
from coordinator_core.roadmap_planning_assemble import _roadmap_baton_and_seed_directives
from coordinator_core.sizing_assemble import _sizing_object_scaffold_directive

LONG_ASCII = "Very Long Title " * 400
LONG_NON_ASCII = "日本語のとても長いタイトル " * 400


@pytest.mark.parametrize("text", [LONG_ASCII, LONG_NON_ASCII, "x" * 5000, "é" * 5000])
@pytest.mark.parametrize("width", [10, 40, 60])
def test_helper_bounds_utf8_bytes(text, width):
    slug = bounded_slug(text, width)
    assert len(slug.encode("utf-8")) <= width
    assert slug and not slug.startswith("-") and not slug.endswith("-")


def test_word_boundary_cut():
    assert bounded_slug("alpha beta gamma", 12) == "alpha-beta"
    assert bounded_slug("alpha beta gamma", 10) == "alpha-beta"
    assert bounded_slug("alpha beta gamma", 16) == "alpha-beta-gamma"


def test_fallback_and_normalisation():
    assert bounded_slug("", 10) == "untitled"
    assert bounded_slug("!!!", 10) == "untitled"
    assert bounded_slug("  A__B  ", 10) == "a-b"


def _basename(path: str) -> str:
    return os.path.basename(path)


@pytest.mark.parametrize("text", [LONG_ASCII, LONG_NON_ASCII])
def test_sizing_scaffold_basename_within_name_max(text):
    d = _sizing_object_scaffold_directive(intent="i", name=text)
    out = next(a for a in d["args"] if a.startswith("--out=")).split("=", 1)[1]
    assert len(_basename(out).encode("utf-8")) <= NAME_MAX


@pytest.mark.parametrize("text", [LONG_ASCII, LONG_NON_ASCII])
def test_roadmap_seed_basename_within_name_max(text):
    directives = _roadmap_baton_and_seed_directives(None, None, None, text, None, ["g1"])
    seed = next(d for d in directives if d["id"] == "d-scaffold-roadmap-seed")
    out = next(a for a in seed["args"] if str(a).startswith("--out=")).split("=", 1)[1]
    assert len(_basename(out).encode("utf-8")) <= NAME_MAX
