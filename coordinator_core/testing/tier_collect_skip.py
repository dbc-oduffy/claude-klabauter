"""Skip importing `complete`-moved test files when the marker expression deselects `complete`.

A deselected file is still imported by every xdist worker; the ledger's `moves` list names
the files that carry a module-level `complete` mark, so ignoring them at collection is the
same selection without the import. Registered from the repo-root conftest.py only: a hook in
a subdirectory conftest never sees moved files outside its own tree.

NEGATIVE SPEC
    - Inactive unless `config.option.markexpr` contains `not complete`; the full tier imports all.
    - Never reads a file's AST or content: one JSON read per process, keyed on repo-relative path.
    - An unlisted `complete` mark is still caught by the ratchet, which scans AST.
    - A missing or unreadable ledger skips nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

LEDGER_RELPATH = "state/test-tiers/fast-tier-ledger.json"
_DESELECT_RE = re.compile(r"\bnot complete\b")

_cache: dict[str, frozenset[str]] = {}


def moved_complete_ids(root: Path) -> frozenset[str]:
    """Repo-relative posix paths of ledger moves whose new marker is `complete`; empty on any read failure."""
    key = str(root)
    if key not in _cache:
        try:
            moves = json.loads((root / LEDGER_RELPATH).read_text(encoding="utf-8")).get("moves", [])
            _cache[key] = frozenset(
                m["id"] for m in moves if isinstance(m, dict) and m.get("new_marker") == "complete" and "id" in m
            )
        except (OSError, ValueError, AttributeError):
            _cache[key] = frozenset()
    return _cache[key]


def deselects_complete(markexpr: str | None) -> bool:
    """True when the marker expression excludes the `complete` tier."""
    return _DESELECT_RE.search(markexpr or "") is not None


class _Plugin:
    def pytest_ignore_collect(self, collection_path, config):
        if not deselects_complete(getattr(config.option, "markexpr", None)):
            return None
        root = Path(str(config.rootpath))
        try:
            rel = Path(str(collection_path)).relative_to(root).as_posix()
        except ValueError:
            return None
        return True if rel in moved_complete_ids(root) else None


def register(config) -> None:
    """Install the collection-skip hook on `config`'s plugin manager."""
    config.pluginmanager.register(_Plugin(), "tier_collect_skip")
