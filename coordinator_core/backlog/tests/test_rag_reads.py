"""Tests for the read-contract adapter: present forms map, missing or broken forms fall back with a gap."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from coordinator_core.backlog import rag_reads
from coordinator_core.backlog.rag_reads import changelog_resolve, doc_outline
from coordinator_core.backlog.record_schema import FlaggedChange

CHANGE = FlaggedChange("Landed", "2026-05-27", ("abc1234",), ("docs/a.md",))


def test_missing_form_falls_back_with_gap_naming_form():
    res = changelog_resolve(CHANGE, loader=lambda: SimpleNamespace())
    assert res.fallback is True
    assert res.gaps == ({"form": "changelog_resolve", "reason": "form not served by the read contract"},)
    assert res.change == CHANGE and res.outlines is None


def test_import_error_is_the_form_missing_case():
    def boom():
        raise ImportError("no module")

    res = doc_outline(["docs/a.md"], loader=boom)
    assert res.fallback is True
    assert res.gaps[0]["form"] == "doc_outline" and "not importable" in res.gaps[0]["reason"]


def test_default_loader_without_example_retrieval_repo_module_falls_back(monkeypatch):
    def refuse(name):
        raise ImportError(name)

    monkeypatch.setattr(rag_reads.importlib, "import_module", refuse)
    res = changelog_resolve(CHANGE)
    assert res.fallback and res.gaps[0]["form"] == "changelog_resolve"


def test_raising_form_is_a_gap_not_a_crash():
    def bad(**_):
        raise RuntimeError("store locked")

    res = changelog_resolve(CHANGE, loader=lambda: SimpleNamespace(changelog_resolve=bad))
    assert res.fallback and "RuntimeError" in res.gaps[0]["reason"]


def test_present_changelog_resolve_maps_paths_and_outlines():
    seen = {}

    def form(entry):
        seen.update(entry)
        return [
            {"path": "docs/a.md", "outline": ["# A", "## Plan"]},
            {"path": "docs/b.md", "outline": "# B\n\n## Outcome\n"},
            {"path": "docs/b.md", "outline": ["# B2"]},
            {"nopath": 1},
        ]

    res = changelog_resolve(CHANGE, loader=lambda: SimpleNamespace(changelog_resolve=form))
    assert res.fallback is False and res.gaps == ()
    assert seen == {"title": "Landed", "day": "2026-05-27", "commits": ["abc1234"]}
    assert res.change.cited_paths == ("docs/a.md", "docs/b.md")
    assert res.outlines == {"docs/a.md": ("# A", "## Plan"), "docs/b.md": ("# B2",)}
    assert CHANGE.cited_paths == ("docs/a.md",)


def test_present_doc_outline_returns_outlines():
    form = lambda paths: [{"path": p, "outline": [f"# {p}"]} for p in paths]  # noqa: E731
    res = doc_outline(["x.md", "y.md"], loader=lambda: SimpleNamespace(doc_outline=form))
    assert res.fallback is False
    assert res.outlines == {"x.md": ("# x.md",), "y.md": ("# y.md",)}


def test_no_subprocess_on_any_path(monkeypatch):
    def forbid(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", forbid)
    changelog_resolve(CHANGE, loader=lambda: SimpleNamespace())
    doc_outline(["x.md"], loader=lambda: SimpleNamespace(doc_outline=lambda paths: []))
