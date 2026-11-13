"""Tests for coordinator_core.updatedocs.retired_terms: synthetic repos under tmp_path only."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.updatedocs.retired_terms import (
    MANIFEST_RELPATH,
    RetiredTermsManifestError,
    read_manifest,
    scan_retired_terms,
    scan_retired_terms_report,
)

MANIFEST = """\
terms:
  - term: chroma
    removed: 2026-08-18
    note: replaced by the sqlite vector store
"""

HOST_VS_ADDONS = """\
# Host vs addons

The host owns the daemon.

Embeddings persist in Chroma collections per project.
Unrelated line.
"""

ADDON_PROTOCOL = """\
# Addon protocol

Addons never touch the store directly.
The Chroma-era client handshake is described below.
A second mention: chroma again, and CHROMA in caps.
Nothing about chromadb-style names matches a word-bounded term.
"""


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    _write(tmp_path, MANIFEST_RELPATH, MANIFEST)
    _write(tmp_path, "docs/wiki/host-vs-addons.md", HOST_VS_ADDONS)
    _write(tmp_path, "docs/wiki/addon-protocol.md", ADDON_PROTOCOL)
    return tmp_path


def test_hits_name_file_line_and_retirement_record(tmp_path):
    hits = scan_retired_terms(_repo(tmp_path))
    got = [(h["file"], h["line"]) for h in hits]
    assert got == [
        ("docs/wiki/addon-protocol.md", 4),
        ("docs/wiki/addon-protocol.md", 5),
        ("docs/wiki/host-vs-addons.md", 5),
    ]
    first = hits[0]
    assert first["term"] == "chroma"
    assert first["removed"] == "2026-08-18"
    assert first["note"] == "replaced by the sqlite vector store"
    assert "Chroma-era" in first["text"]


def test_word_bounded_and_clean_lines_do_not_hit(tmp_path):
    hits = scan_retired_terms(_repo(tmp_path))
    assert all(h["line"] != 6 for h in hits)
    assert not any("Unrelated" in h["text"] for h in hits)


def test_no_manifest_yields_empty_without_error(tmp_path):
    _write(tmp_path, "docs/wiki/a.md", "chroma is mentioned here\n")
    assert read_manifest(tmp_path) == []
    assert scan_retired_terms(tmp_path) == []


def test_manifest_file_itself_is_not_scanned(tmp_path):
    _write(tmp_path, MANIFEST_RELPATH, MANIFEST)
    assert scan_retired_terms(tmp_path) == []


def test_allow_in_exempts_a_path_prefix_but_not_others(tmp_path):
    _write(
        tmp_path,
        MANIFEST_RELPATH,
        "terms:\n  - term: chroma\n    removed: 2026-08-18\n    allow_in: [docs/decisions/]\n",
    )
    _write(tmp_path, "docs/decisions/dr-1.md", "We dropped chroma.\n")
    _write(tmp_path, "docs/wiki/page.md", "Uses chroma.\n")
    assert [h["file"] for h in scan_retired_terms(tmp_path)] == ["docs/wiki/page.md"]


def test_archive_dirs_and_non_markdown_are_skipped(tmp_path):
    _write(tmp_path, MANIFEST_RELPATH, MANIFEST)
    _write(tmp_path, "docs/archive/old.md", "chroma\n")
    _write(tmp_path, "docs/wiki/notes.txt", "chroma\n")
    assert scan_retired_terms(tmp_path) == []


def test_top_level_markdown_is_scanned(tmp_path):
    _write(tmp_path, MANIFEST_RELPATH, MANIFEST)
    _write(tmp_path, "README.md", "Built on Chroma.\n")
    assert [(h["file"], h["line"]) for h in scan_retired_terms(tmp_path)] == [("README.md", 1)]


def test_one_hit_per_term_on_a_shared_line(tmp_path):
    _write(
        tmp_path,
        MANIFEST_RELPATH,
        "terms:\n  - {term: chroma, removed: 2026-08-18}\n  - {term: faiss, removed: 2026-09-01}\n",
    )
    _write(tmp_path, "docs/a.md", "chroma and faiss together\n")
    assert [h["term"] for h in scan_retired_terms(tmp_path)] == ["chroma", "faiss"]


@pytest.mark.parametrize(
    "body",
    [
        "terms: not-a-list\n",
        "terms:\n  - just-a-string\n",
        "terms:\n  - {term: chroma}\n",
        "terms: [unclosed\n",
    ],
)
def test_malformed_manifest_raises_typed_error(tmp_path, body):
    _write(tmp_path, MANIFEST_RELPATH, body)
    with pytest.raises(RetiredTermsManifestError):
        scan_retired_terms(tmp_path)


def test_empty_manifest_yields_no_terms(tmp_path):
    _write(tmp_path, MANIFEST_RELPATH, "")
    assert read_manifest(tmp_path) == []


def test_report_is_complete_within_budget_and_incomplete_past_it(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    hits, complete = scan_retired_terms_report(repo)
    assert complete is True and hits == scan_retired_terms(repo) and hits

    monkeypatch.setattr("coordinator_core.updatedocs.retired_terms._SCAN_BUDGET_SECS", -1.0)
    hits, complete = scan_retired_terms_report(repo)
    assert complete is False and hits == []
