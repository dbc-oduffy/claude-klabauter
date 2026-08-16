"""Regression tests for the structured `changed_paths` sink on `sync_mirror`/
`sync_flat_mirror`/`_sync_mirror_top_level_files` (bug-backlog `0d018c65`, P2).

Before this fix, `coordinator/bin/publish.py` derived a publish round's
"changed set" — the signal that decides whether the downstream entrypoint-gate
sweep runs at all — by TEXT-SCRAPING this module's own `NEW:`/`UPDATE:` print
lines out of captured stdout (`_parse_sync_changed_paths`). A stdout-format
change (a wording tweak, a reordering, a label rename) silently degraded that
scrape to an EMPTY set rather than `None` — and empty means "nothing changed,
skip the sweep" downstream, while `None` is the explicit undeterminable
sentinel that correctly falls back to a full sweep. That is failing NARROW: a
formatting change ships an unswept entrypoint with no error anywhere.

This module's `changed_paths` param replaces the scrape with a structured
signal recorded at its SOURCE (the actual `_needs_copy`-driven copy decision),
independent of whatever this module happens to print. The tests below pin the
property that matters: the structured signal cannot be knocked out by a
print-format change, an empty structured set is only ever produced by a
genuine no-changes sync, and the structured signal agrees with the printed
`NEW:`/`UPDATE:` lines on a normal sync.

`None`-when-undeterminable itself is NOT this module's contract — `sync_mirror`/
`sync_flat_mirror` always run to completion or raise, so a call that reaches
this parameter always genuinely knows its changed set; `None` is decided one
layer up, by whichever caller chooses not to pass this param at all (see
`coordinator_core/percolate/engine.py`'s `PhaseResult.changed_files` docstring
and `run_post_rsync`'s `sync_changed_paths` param for that half of the
contract). Nothing in this file asserts `None` comes out of `sync_mirror`
itself — asserting that would be the "returns a set" tautology the dispatching
brief calls worthless; what earns its place here is proving the sink cannot
silently collapse populated -> empty.

Loaded via the `percolate` package (§ sibling `test_publish_sync_needs_copy_
honest.py`'s own note on why a bare `spec_from_file_location` can't resolve
`publish_sync.py`'s relative `.ignore` import).
"""

from __future__ import annotations

import sys
from pathlib import Path

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402
from percolate.ignore import PercolateIgnoreMatcher  # noqa: E402

_NO_IGNORE = PercolateIgnoreMatcher([])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# sync_flat_mirror
# ---------------------------------------------------------------------------
def test_flat_mirror_changed_paths_populated_on_real_copy(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "payload")
    changed: "set[str]" = set()
    publish_sync.sync_flat_mirror(src, dst, _NO_IGNORE, False, changed_paths=changed)
    assert changed == {"a.txt"}
    assert (dst / "a.txt").read_text(encoding="utf-8") == "payload"


def test_flat_mirror_changed_paths_empty_only_on_genuine_no_change(tmp_path, capsys):
    """An empty structured set must mean the sweep genuinely determined
    nothing changed — not "we couldn't tell". Seed dst identically to src
    first, then sync again: zero copy decisions were made, so the sink must
    come back empty, and the printed output must carry zero NEW:/UPDATE:
    lines either — the two channels must agree on "nothing happened"."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "payload")
    _write(dst / "a.txt", "payload")
    changed: "set[str]" = set()
    capsys.readouterr()
    publish_sync.sync_flat_mirror(src, dst, _NO_IGNORE, False, changed_paths=changed)
    out = capsys.readouterr().out
    assert changed == set()
    assert "NEW:" not in out
    assert "UPDATE:" not in out


def test_flat_mirror_changed_paths_excludes_deletions(tmp_path):
    """A REMOVE (dst file absent from src) is not new content — must not
    land in the structured sink, matching the pre-existing NEW:/UPDATE:-only
    printed-line contract. `keep.txt` keeps the source non-empty so the
    empty-source mass-delete guard (unrelated to this fix) does not fire."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "keep.txt", "keep")
    _write(dst / "keep.txt", "keep")
    _write(dst / "stale.txt", "old")
    changed: "set[str]" = set()
    synced, removed = publish_sync.sync_flat_mirror(
        src, dst, _NO_IGNORE, False, changed_paths=changed
    )
    assert removed == 1
    assert changed == set()


def test_flat_mirror_changed_paths_survives_a_hostile_print_format(tmp_path, monkeypatch):
    """The regression this fix exists for: prove the structured sink does
    NOT depend on parsing this module's own printed text. Monkeypatch
    `print` inside publish_sync to emit an unrecognisable format (what a
    stdout-format change looks like from the old scrape's point of view) and
    confirm the structured `changed_paths` set is populated correctly
    anyway. The old text-scrape (`publish.py::_parse_sync_changed_paths`)
    would silently yield an EMPTY set here — this sink must not."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "payload")

    def _garbled_print(*args, **kwargs):
        # Deliberately drop every argument on the floor — worse than a mere
        # wording tweak, this is a total loss of the printed channel.
        pass

    monkeypatch.setattr(publish_sync, "print", _garbled_print, raising=False)
    changed: "set[str]" = set()
    publish_sync.sync_flat_mirror(src, dst, _NO_IGNORE, False, changed_paths=changed)
    assert changed == {"a.txt"}


# ---------------------------------------------------------------------------
# sync_mirror — top-level files + per-plugin files
# ---------------------------------------------------------------------------
def test_mirror_changed_paths_top_level_file(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "README.md", "hello")
    changed: "set[str]" = set()
    publish_sync.sync_mirror(src, dst, _NO_IGNORE, False, changed_paths=changed)
    assert changed == {"README.md"}


def test_mirror_changed_paths_per_plugin_file_is_prefixed(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "pluginA" / "file.txt", "payload")
    changed: "set[str]" = set()
    publish_sync.sync_mirror(src, dst, _NO_IGNORE, False, changed_paths=changed)
    assert changed == {"pluginA/file.txt"}


def test_mirror_changed_paths_agrees_with_printed_output_on_normal_sync(tmp_path, capsys):
    """The structured signal and the printed NEW:/UPDATE: lines must agree
    on a normal sync: every rel_path landing in `changed_paths` has a
    corresponding printed line, and vice versa."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "top.txt", "top")
    _write(src / "pluginA" / "nested.txt", "nested")
    changed: "set[str]" = set()
    capsys.readouterr()
    publish_sync.sync_mirror(src, dst, _NO_IGNORE, False, changed_paths=changed)
    out = capsys.readouterr().out
    assert changed == {"top.txt", "pluginA/nested.txt"}
    assert "top.txt" in out
    assert "nested.txt" in out
    # Every printed NEW:/UPDATE: rel_path (bare or plugin-qualified) is
    # represented in the structured set — no line the sink dropped.
    plugin_marker = None
    for line in out.splitlines():
        marker = line.strip()
        if marker.startswith("--- ") and marker.endswith(" ---"):
            plugin_marker = marker[4:-4]
            continue
        stripped = line.strip()
        if stripped.startswith("NEW:") or stripped.startswith("UPDATE:"):
            rel = stripped.split(None, 1)[1].strip()
            qualified = f"{plugin_marker}/{rel}" if plugin_marker else rel
            assert qualified in changed


def test_mirror_changed_paths_excludes_orphan_removal(tmp_path, monkeypatch):
    """The orphan-plugin-dir sweep (REMOVE DIR) is a deletion, not new
    content — must not land in the structured sink. `keptPlugin` keeps a
    real top-level dir on both sides so this isn't the empty-source case;
    the override env var accepts the deliberate top-level-presence removal
    of `stalePlugin` (§ `sync_mirror`'s own preflight) so the sweep runs at
    all rather than aborting before reaching the removal this test targets."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "keptPlugin" / "kept.txt", "kept")
    _write(dst / "keptPlugin" / "kept.txt", "kept")
    _write(dst / "stalePlugin" / "old.txt", "old")
    monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "1")
    changed: "set[str]" = set()
    synced, removed = publish_sync.sync_mirror(
        src, dst, _NO_IGNORE, False, changed_paths=changed
    )
    assert removed == 1
    assert changed == set()


def test_mirror_changed_paths_default_none_is_a_no_op(tmp_path):
    """Every pre-existing call site (no `changed_paths` arg) must see
    identical behaviour to before this param existed — no crash, no implicit
    tracking object created."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src / "a.txt", "payload")
    synced, removed = publish_sync.sync_mirror(src, dst, _NO_IGNORE, False)
    assert synced == 1
    assert removed == 0
