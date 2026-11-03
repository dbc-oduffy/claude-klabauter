"""Tests for _research_layout: the mirrored DoE research layout contract and its parity pin."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path, PurePosixPath

import pytest

from coordinator_core.ops import _research_layout as L


def test_run_dir_builds_topic_slash_run():
    assert L.run_dir("misc", "2026-11-03", "x") == PurePosixPath("misc/2026-11-03-x")
    assert str(L.run_dir("misc", "2026-11-03", "x")) == "misc/2026-11-03-x"


@pytest.mark.parametrize(
    "topic,date,slug",
    [("trail", "2026-11-03", "x"), ("2026-11-03-x", "2026-11-03", "y"), ("Bad", "2026-11-03", "x"),
     ("misc", "2026-11-03", "Bad Slug"), ("misc", "nov-3", "x"), ("", "2026-11-03", "x")],
)
def test_run_dir_rejects_bad_names(topic, date, slug):
    with pytest.raises(ValueError):
        L.run_dir(topic, date, slug)


@pytest.mark.parametrize(
    "rel,kind",
    [
        ("README.md", L.INDEX_KIND),
        (".fleet-readable", L.MARKER),
        ("misc", L.TOPIC_DIR),
        ("misc/2026-11-03-x", L.RUN_DIR),
        ("misc/2026-11-03-x/synthesis.md", L.RUN_CHILD),
        ("misc/2026-11-03-x/trail.yaml", L.RUN_CHILD),
        ("misc/2026-11-03-x/verdict.md", L.RUN_CHILD),
        ("misc/2026-11-03-x/outputs/a/b.md", L.RUN_CHILD),
        ("misc/2026-11-03-x/workdir/n.txt", L.RUN_CHILD),
        ("misc\\2026-11-03-x\\outputs\\a.md", L.RUN_CHILD),
        ("2026-11-03-x.md", L.NONCONFORMING),
        ("trail/abc.yaml", L.NONCONFORMING),
        ("misc/2026-11-03-x/stray.md", L.NONCONFORMING),
        ("misc/notes.md", L.NONCONFORMING),
        ("misc/../x", L.NONCONFORMING),
        ("", L.NONCONFORMING),
    ],
)
def test_classify(rel, kind):
    assert L.classify(rel) == kind


def test_fold_topic():
    assert L.fold_topic("foo-bar-audit") == ("foo-bar", "audit")
    assert L.fold_topic("foo") == ("foo", "")
    assert L.fold_topic("-audit") == ("-audit", "")


def test_topic_for_both_sides():
    assert L.topic_for("widget-survey") == "widget"
    assert L.topic_for("widget-survey", topic="other") == "other"
    assert L.topic_for("2026-11-03-widget") == L.DEFAULT_TOPIC
    assert L.topic_for("trail") == L.DEFAULT_TOPIC
    assert L.topic_for("Weird Slug") == L.DEFAULT_TOPIC


@pytest.mark.parametrize(
    "rel,child",
    [
        ("synthesis.md", "synthesis.md"),
        ("digest.md", "synthesis.md"),
        ("verdict.md", "verdict.md"),
        ("seg-1/part.md", "outputs/seg-1/part.md"),
        ("notes.md", "outputs/notes.md"),
        ("sub/synthesis.md", "outputs/sub/synthesis.md"),
        ("seg\\a.md", "outputs/seg/a.md"),
    ],
)
def test_child_for(rel, child):
    assert L.child_for(rel) == child


def test_child_for_lands_as_run_child():
    base = L.run_dir("misc", "2026-11-03", "x")
    for rel in ("synthesis.md", "digest.md", "verdict.md", "a/b.md"):
        assert L.classify(f"{base}/{L.child_for(rel)}") == L.RUN_CHILD


def _doe_constants() -> dict:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        pytest.skip("CLAUDE_PLUGIN_ROOT unset")
    path = Path(root) / "lib" / "research_layout.py"
    if not path.is_file():
        pytest.skip("DoE research_layout.py absent")
    wanted = ("RESERVED_TOPICS", "TOPIC_RE", "RUN_RE", "FIXED_CHILDREN")
    ns: dict = {"re": re}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                exec(compile(ast.Module([node], []), str(path), "exec"), ns)
    return ns


def test_parity_with_doe_contract():
    doe = _doe_constants()
    assert tuple(doe["FIXED_CHILDREN"]) == L.FIXED_CHILDREN
    assert tuple(doe["RESERVED_TOPICS"]) == L.RESERVED_TOPICS
    assert doe["TOPIC_RE"].pattern == L.TOPIC_RE.pattern
    assert doe["RUN_RE"].pattern == L.RUN_RE.pattern
