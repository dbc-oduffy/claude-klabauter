"""Tests for coordinator_core.docindex.entry_kinds :: plugin-manifest reader — AC11.

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C6

Proves the claim: the "plugin-manifest" reader recovers EVERY `.md` file in an
`agents/`- or `commands/`-shaped directory it is pointed at (AC11), against a
committed set of representative fixtures written under `tmp_path` — never a
live mirror read (Review: coordinator-staff-eng — F-4: no `coordinator-claude`
checkout is reachable from this box, so the original read-only dry-run scope
was removed from this chunk; see the plan's C6 row).

Negative-spec:
  - No mirror/sibling-checkout path is read at runtime — every fixture is
    written to `tmp_path` by this test itself.
  - No subprocess is spawned.
  - Unmarked: stays in the fast tier.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.docindex.entry_kinds import read_entry
from coordinator_core.docindex.spec import EntryField

PLUGIN_FIELDS = (
    EntryField(field="name", label="Name"),
    EntryField(field="description", label="Description"),
)

# A handful of representative agent/command frontmatter blocks, keyed by the
# filename they're written under. Includes one multi-line description and one
# with a colon in it (per the plan body's explicit fixture requirements).
_FIXTURES: "dict[str, str]" = {
    "code-reviewer.md": """---
name: code-reviewer
description: Reviews a diff for correctness and style issues.
---
Agent body, never read by the reader.
""",
    "test-runner.md": """---
name: test-runner
description: >-
  Runs the scoped test suite for a chunk and reports pass/fail: never the
  full or fast tier unless explicitly asked.
---
Agent body.
""",
    "git-commit-agent.md": """---
name: git-commit-agent
description: "Commits a wave's changes once: scoped, never git add -A."
---
Agent body.
""",
    "plan-runner.md": """---
name: plan-runner
description: Drives one plan chunk end to end.
---
Command body.
""",
}


def _write_fixture_dir(tmp_path: Path) -> Path:
    manifest_dir = tmp_path / "agents"
    manifest_dir.mkdir()
    for filename, text in _FIXTURES.items():
        (manifest_dir / filename).write_text(text, encoding="utf-8")
    return manifest_dir


def test_plugin_manifest_reader_recovers_every_md_file_in_directory(tmp_path):
    manifest_dir = _write_fixture_dir(tmp_path)

    md_files = sorted(manifest_dir.glob("*.md"))
    assert len(md_files) == len(_FIXTURES), (
        "fixture setup drifted from _FIXTURES — every .md file written must "
        "be present on disk before the reader is exercised"
    )

    recovered = {
        f.name: read_entry(f, "plugin-manifest", PLUGIN_FIELDS, None)
        for f in md_files
    }

    # AC11: every .md file in the directory is recovered — none silently
    # dropped, none invented.
    assert set(recovered) == set(_FIXTURES)
    assert len(recovered) == len(_FIXTURES)

    assert recovered["code-reviewer.md"] == {
        "name": "code-reviewer",
        "description": "Reviews a diff for correctness and style issues.",
    }

    # Multi-line (YAML folded-scalar) description: recovered as a single
    # collapsed string, not dropped or truncated.
    assert recovered["test-runner.md"] == {
        "name": "test-runner",
        "description": (
            "Runs the scoped test suite for a chunk and reports pass/fail: "
            "never the full or fast tier unless explicitly asked."
        ),
    }

    # Description containing a colon: quoted scalar parses intact, colon
    # preserved verbatim rather than truncating the value at it.
    assert recovered["git-commit-agent.md"] == {
        "name": "git-commit-agent",
        "description": "Commits a wave's changes once: scoped, never git add -A.",
    }

    assert recovered["plan-runner.md"] == {
        "name": "plan-runner",
        "description": "Drives one plan chunk end to end.",
    }


def test_plugin_manifest_reader_entry_field_order_matches_declared_order(tmp_path):
    manifest_dir = _write_fixture_dir(tmp_path)
    f = manifest_dir / "code-reviewer.md"

    entry = read_entry(f, "plugin-manifest", PLUGIN_FIELDS, None)

    assert list(entry.keys()) == ["name", "description"]
