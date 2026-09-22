"""Tests for the PINNED-API.md contract doc itself.

`sites_in_source` resolves at most one hop of same-module indirection; a
spawn reached through a helper imported from another module is invisible
to it (state/bug-backlog/2026-08-27-spawn-policy-sites-in-source-misses-
cross-module-helper-hops.yaml). The doc instructs guard authors to build on
`sites_in_source` alone ("This is a contract, not a suggestion") without
naming that limitation or the sibling cross-file resolver that covers it,
so a guard author following the doc as written silently under-reports.
"""

from __future__ import annotations

import pathlib

PINNED_API_DOC = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tasks"
    / "shell-spawn-regrowth-gate"
    / "PINNED-API.md"
)


def _doc_text() -> str:
    return PINNED_API_DOC.read_text(encoding="utf-8")


def test_doc_names_same_file_only_limitation():
    text = _doc_text()
    assert "SAME-FILE ONLY" in text or "same-file only" in text.lower()


def test_doc_points_at_wrapper_resolution_for_cross_module_hops():
    text = _doc_text()
    assert "wrapper_resolution" in text
    assert "WrapperResolver" in text
