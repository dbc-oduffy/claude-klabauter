from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.docindex.entry_kinds import (
    MissingEntryFieldError,
    UnknownEntryKindError,
    get_reader,
    read_entry,
)
from coordinator_core.docindex.spec import EntryField


WIKI_FIELDS = (
    EntryField(field="system", label="System"),
    EntryField(field="depends_on", label="Dependencies"),
)

PLUGIN_FIELDS = (
    EntryField(field="name", label="Name"),
    EntryField(field="description", label="Description"),
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_get_reader_unknown_kind_raises_named_error():
    with pytest.raises(UnknownEntryKindError, match="not-a-real-kind"):
        get_reader("not-a-real-kind")


def test_wiki_entry_reads_all_declared_fields_from_frontmatter(tmp_path):
    f = _write(
        tmp_path / "emit-engine.md",
        """---
system: emit-engine
depends_on: []
---
## emit-engine

body text
""",
    )
    entry = read_entry(f, "wiki-entry", WIKI_FIELDS, None)
    assert entry == {"system": "emit-engine", "depends_on": "[]"}
    assert list(entry.keys()) == ["system", "depends_on"]


def test_wiki_entry_identity_field_falls_back_to_heading(tmp_path):
    f = _write(
        tmp_path / "cartography.md",
        """---
depends_on: []
---
## cartography

body text
""",
    )
    entry = read_entry(f, "wiki-entry", WIKI_FIELDS, None)
    assert entry["system"] == "cartography"
    assert entry["depends_on"] == "[]"


def test_wiki_entry_missing_non_identity_field_raises_named_error(tmp_path):
    f = _write(
        tmp_path / "guards.md",
        """---
system: guards
---
## guards
""",
    )
    with pytest.raises(MissingEntryFieldError, match="guards.md.*depends_on"):
        read_entry(f, "wiki-entry", WIKI_FIELDS, None)


def test_wiki_entry_missing_identity_field_and_no_heading_raises(tmp_path):
    f = _write(
        tmp_path / "no-heading.md",
        """---
depends_on: []
---
just prose, no heading
""",
    )
    with pytest.raises(MissingEntryFieldError, match="system"):
        read_entry(f, "wiki-entry", WIKI_FIELDS, None)


def test_wiki_entry_exclusion_matches_string_status_not_yaml_date(tmp_path):
    excluded = _write(
        tmp_path / "install-node.md",
        """---
status: retired
retired: 2026-08-06
---
## install-node
""",
    )
    result = read_entry(
        excluded, "wiki-entry", WIKI_FIELDS, {"status": "retired"}
    )
    assert result is None


def test_wiki_entry_exclude_when_retired_true_does_not_match_yaml_date(tmp_path):
    f = _write(
        tmp_path / "percolate.md",
        """---
system: percolate
depends_on: []
retired: 2026-08-06
---
## percolate
""",
    )
    entry = read_entry(f, "wiki-entry", WIKI_FIELDS, {"retired": True})
    assert entry == {"system": "percolate", "depends_on": "[]"}


def test_wiki_entry_exclusion_evaluated_before_missing_field_check(tmp_path):
    f = _write(
        tmp_path / "worklife-ops.md",
        """---
status: retired
system: worklife-ops
---
## worklife-ops
""",
    )
    result = read_entry(f, "wiki-entry", WIKI_FIELDS, {"status": "retired"})
    assert result is None


def test_plugin_manifest_reads_all_declared_fields(tmp_path):
    f = _write(
        tmp_path / "manifest.md",
        """---
name: coordinator-claude
description: the operating system
---
body not read
""",
    )
    entry = read_entry(f, "plugin-manifest", PLUGIN_FIELDS, None)
    assert entry == {
        "name": "coordinator-claude",
        "description": "the operating system",
    }


def test_plugin_manifest_missing_field_raises_named_error(tmp_path):
    f = _write(
        tmp_path / "manifest2.md",
        """---
name: coordinator-claude
---
""",
    )
    with pytest.raises(MissingEntryFieldError, match="manifest2.md.*description"):
        read_entry(f, "plugin-manifest", PLUGIN_FIELDS, None)


def test_plugin_manifest_has_no_heading_fallback(tmp_path):
    f = _write(
        tmp_path / "manifest3.md",
        """---
description: some description
---
## name-that-looks-like-a-heading
""",
    )
    with pytest.raises(MissingEntryFieldError, match="name"):
        read_entry(f, "plugin-manifest", PLUGIN_FIELDS, None)
