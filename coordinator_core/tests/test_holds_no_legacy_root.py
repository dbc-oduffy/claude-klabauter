"""Tripwire: this repo's tracked source holds no retired content-root name outside the ratified allowlist.

Rule, in full:

1. Over `git ls-files`, minus `archive/ state/ docs/ tasks/ .coordinator-local/ .structural-index/`,
   a line matching the legacy-root pattern (see `_legacy_root_scan`) must carry the
   `private-name-ok` marker. `_legacy_root_scan.unmarked_hits()` must return [].
2. The marker is the only way to allowlist a line. A marked line is legal only if it is listed in
   `docs/research/2026-09-30-content-root-allowlist.md` (path, stripped line text), and every listed
   entry must still exist as a marked line. The set of marked lines equals the list, as multisets.

Adding a marked line therefore needs a PM-ratified allowlist row in the same change; removing the
last use of a legacy name needs its row deleted. The allowlist's `status:` line reads `unratified`
until the PM's words are recorded in it; the prime exit criterion stays open while it does.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from coordinator_core.tests import _legacy_root_scan as scan

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALLOWLIST = _REPO_ROOT / "docs" / "research" / "2026-09-30-content-root-allowlist.md"
_BLOCK = re.compile(r"```allowlist\n(.*?)```", re.DOTALL)
_SEP = " :: "


def marked_hits(paths=None, *, root: Path | None = None) -> list[tuple[str, str]]:
    """(repo-relative path, stripped line) for each in-scope line that names a legacy root and is marked."""
    base = Path(root) if root else _REPO_ROOT
    rels = scan._tracked(base) if paths is None else [str(p) for p in paths]
    hits: list[tuple[str, str]] = []
    for rel in rels:
        posix = Path(rel).as_posix()
        if posix.startswith(scan._EXCLUDED_PREFIXES):
            continue
        try:
            text = (base / posix).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if scan._MARKER in line and scan._PATTERN.search(scan._DATED_RECORD_ID.sub("", line)):
                hits.append((posix, line.strip()))
    return hits


def _allowlist_entries() -> list[tuple[str, str]]:
    block = _BLOCK.search(_ALLOWLIST.read_text(encoding="utf-8"))
    assert block, f"{_ALLOWLIST} has no ```allowlist block"
    entries = []
    for raw in block.group(1).splitlines():
        if not raw.strip():
            continue
        path, line, _reason = raw.split(_SEP, 2)
        entries.append((path.strip(), line.strip()))
    return entries


def test_no_unmarked_legacy_root_in_tracked_source():
    hits = scan.unmarked_hits()
    assert hits == [], "unmarked legacy-root lines:\n" + "\n".join(
        f"{p}:{n}: {t}" for p, n, t in hits[:40]
    )


def test_marked_lines_equal_the_ratified_allowlist():
    marked, listed = Counter(marked_hits()), Counter(_allowlist_entries())
    unlisted = sorted((marked - listed).elements())
    stale = sorted((listed - marked).elements())
    assert not unlisted and not stale, (
        f"marked but not in the allowlist: {unlisted[:20]}\nin the allowlist but not marked: {stale[:20]}"
    )


def test_allowlist_declares_a_status():
    assert re.search(r"^status: (unratified|ratified)\s*$", _ALLOWLIST.read_text(encoding="utf-8"), re.M)


def test_tripwire_fails_on_a_tree_with_one_unmarked_line(tmp_path):
    legacy = ".d" "oe-root"
    (tmp_path / "ok.py").write_text(f"P = '{legacy}'  # {scan._MARKER}: fixture\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text(f"P = '{legacy}'\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "prose.md").write_text(f"{legacy}\n", encoding="utf-8")
    rels = ["ok.py", "bad.py", "docs/prose.md"]
    assert scan.unmarked_hits(rels, root=tmp_path) == [("bad.py", 1, f"P = '{legacy}'")]
    assert [p for p, _ in marked_hits(rels, root=tmp_path)] == ["ok.py"]
