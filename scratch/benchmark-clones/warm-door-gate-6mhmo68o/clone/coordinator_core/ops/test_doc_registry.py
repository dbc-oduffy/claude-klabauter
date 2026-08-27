"""Tests for coordinator_core.ops.doc_registry.

Covers the three resolution shapes the reader's docstring commits to:
all-absent (pure fleet defaults, and specifically that the default list is
the four generics WITHOUT `coordinator/README.md`), full override (every
key set, every field honoured), and partial override (one key set, the
other three still default).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.doc_registry import (
    DEFAULT_DOC_STALENESS_COMMITS,
    DEFAULT_DOC_STALENESS_DAYS,
    DEFAULT_HUMAN_FACING_DOCS,
    resolve_doc_registry_config,
)


def _write_local_md(tmp_path: Path, frontmatter_body: str) -> Path:
    local_md = tmp_path / "coordinator.local.md"
    local_md.write_text(f"---\n{frontmatter_body}---\n\n# body\n", encoding="utf-8")
    return local_md


def test_all_absent_no_local_md_returns_fleet_defaults(tmp_path):
    cfg = resolve_doc_registry_config(str(tmp_path))

    assert cfg.human_facing_docs == ["README.md", "INSTALL.md", "CONTEXT.md", "CONTRIBUTING.md"]
    assert "coordinator/README.md" not in cfg.human_facing_docs
    assert cfg.doc_staleness_commits == 8000
    assert cfg.doc_staleness_days == 21
    assert cfg.doc_verify_ignore == []


def test_all_absent_local_md_present_but_no_relevant_keys(tmp_path):
    _write_local_md(tmp_path, "project_type: general\n")

    cfg = resolve_doc_registry_config(str(tmp_path))

    assert cfg.human_facing_docs == list(DEFAULT_HUMAN_FACING_DOCS)
    assert cfg.doc_staleness_commits == DEFAULT_DOC_STALENESS_COMMITS
    assert cfg.doc_staleness_days == DEFAULT_DOC_STALENESS_DAYS
    assert cfg.doc_verify_ignore == []


def test_full_override_honoured(tmp_path):
    _write_local_md(
        tmp_path,
        "project_type: general\n"
        "human_facing_docs: [README.md, INSTALL.md, CONTEXT.md, CONTRIBUTING.md, "
        "coordinator/README.md]\n"
        "doc_staleness_commits: 500\n"
        "doc_staleness_days: 7\n"
        "doc_verify_ignore: [coordinator/lib/install-substrate.py]\n",
    )

    cfg = resolve_doc_registry_config(str(tmp_path))

    assert cfg.human_facing_docs == [
        "README.md",
        "INSTALL.md",
        "CONTEXT.md",
        "CONTRIBUTING.md",
        "coordinator/README.md",
    ]
    assert cfg.doc_staleness_commits == 500
    assert cfg.doc_staleness_days == 7
    assert cfg.doc_verify_ignore == ["coordinator/lib/install-substrate.py"]


def test_partial_override_one_key_set_others_default(tmp_path):
    _write_local_md(tmp_path, "project_type: general\ndoc_staleness_days: 45\n")

    cfg = resolve_doc_registry_config(str(tmp_path))

    assert cfg.doc_staleness_days == 45
    assert cfg.human_facing_docs == list(DEFAULT_HUMAN_FACING_DOCS)
    assert cfg.doc_staleness_commits == DEFAULT_DOC_STALENESS_COMMITS
    assert cfg.doc_verify_ignore == []


def test_doe_claude_own_override_shape_dogfood(tmp_path):
    """Mirrors the exact frontmatter DoE-claude's own coordinator.local.md declares (C4)."""
    _write_local_md(
        tmp_path,
        "human_facing_docs: [README.md, INSTALL.md, CONTEXT.md, CONTRIBUTING.md, "
        "coordinator/README.md]\n"
        "doc_staleness_commits: 8000\n"
        "doc_staleness_days: 21\n"
        "doc_verify_ignore: []\n",
    )

    cfg = resolve_doc_registry_config(str(tmp_path))

    assert cfg.human_facing_docs == [
        "README.md",
        "INSTALL.md",
        "CONTEXT.md",
        "CONTRIBUTING.md",
        "coordinator/README.md",
    ]
    assert cfg.doc_staleness_commits == 8000
    assert cfg.doc_staleness_days == 21
    assert cfg.doc_verify_ignore == []
