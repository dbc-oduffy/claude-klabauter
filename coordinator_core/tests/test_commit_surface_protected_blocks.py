"""Fast-tier guard: every commit-surface protected block survives in its source file.

Purpose: `coordinator_core/tests/fixtures/commit_surface_protected_blocks.json` is a checked-in
extraction (at a fixed HEAD) of every docstring-first-paragraph, prohibition, and gravestone
block from the commit-surface roster files named in `_commit_surface_roster.py`
(`COMMIT_SURFACE_FILES`). This test is the fast-tier tripwire that fires the moment any future
edit deletes or reworks one of those blocks without a deliberate, reviewed fixture update: for
every fixture entry, assert its normalized text still appears, in normalized form, somewhere in
its named source file. A future narration-shrink pass that silently deletes a negative-spec
paragraph fails here; a deliberate edit updates the fixture in the same commit.

Negative-spec: this test does NOT re-derive or re-classify anything -- it does not import
`_commit_surface_roster`'s `classify()`/`marker_lines()`/`deletable_partition()` machinery and
does not repeat that module's own policy. Its only question is "is this exact protected text
still present in this file", nothing else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "commit_surface_protected_blocks.json"

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip the ends."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _load_fixture() -> list[dict]:
    with _FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


_FIXTURE_ENTRIES = _load_fixture()


def _entry_id(entry: dict) -> str:
    return entry.get("id", "<unknown-id>")


@pytest.mark.parametrize("entry", _FIXTURE_ENTRIES, ids=_entry_id)
def test_protected_block_still_present(entry: dict) -> None:
    file_rel = entry["file"]
    source_path = _REPO_ROOT / file_rel
    assert source_path.is_file(), (
        f"fixture entry {entry['id']!r} names {file_rel!r}, which no longer exists at "
        f"{source_path} -- a commit-surface roster file must not be deleted without updating "
        f"the fixture"
    )

    source_text = source_path.read_text(encoding="utf-8")
    normalized_source = _normalize(source_text)
    normalized_expected = _normalize(entry["text"])

    assert normalized_expected in normalized_source, (
        f"protected block {entry['id']!r} (class={entry.get('class')!r}, "
        f"symbol={entry.get('symbol')!r}, lines={entry.get('start_line')}-"
        f"{entry.get('end_line')}) is no longer present, in normalized form, in {file_rel!r}. "
        f"If this text was deliberately reworked or removed, update "
        f"coordinator_core/tests/fixtures/commit_surface_protected_blocks.json to match -- "
        f"never silently delete a negative-spec/prohibition block."
    )


def test_fixture_is_nonempty() -> None:
    assert len(_FIXTURE_ENTRIES) > 0, "fixture must carry at least one protected block"
