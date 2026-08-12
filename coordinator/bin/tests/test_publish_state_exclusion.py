"""test_publish_state_exclusion.py — PM ruling 2026-08-12 ("`state`
shouldn't be in the mirror at all"): asserts, from CONFIG and MECHANISM
rather than from a hardcoded incident filename, that no
`publish-mirror:claude_klabauter` row in `setup/publish-targets.portable`
can carry claude-klabauter's own `state/` working substrate into the mirror.

Two distinct incidents motivate this file, per the row audit below:

  1. Every row with an allowlist (field 7) is a FAIL-CLOSED mirror: an
     allowlist entry absent from the real allowlist CSV is never copied —
     `build_allowlisted_source` (coordinator/lib/percolate/allowlist.py)
     raises `AllowlistError` on an entry NOT present on disk, and never
     widens what the allowlist admits. `state` is not, and must never
     become, a member of any of these rows' allowlist CSVs. This is
     asserted directly off the real `setup/publish-targets.portable` file
     (never a second hand-copied list), so a future row edit that adds
     `state` fails this test immediately.

  2. `check_working_data_paths` (same module) is the SECONDARY,
     defense-in-depth assertion for those allowlisted rows: even if `state`
     somehow entered a restricted tree (e.g. via a `source_map` entry from
     a different contributing root), this gate hard-fails the publish on
     any `state/`-prefixed path. Exercised here directly against the real
     function, with a synthetic path, to prove the mechanism itself (not
     this test's paraphrase of it) rejects `state/`.

  One row (`claude-klabauter-publish-repo-toplevel`) carries NO allowlist —
  it is a flat-mirror sourced from `dist/klabauter-toplevel/`, a curated
  staging directory that structurally does not contain `state/` on disk.
  `check_working_data_paths` is NOT wired for allowlist-less rows (`if
  target.allowlist:` in `coordinator/bin/publish.py::run_pre_sync_gates`),
  so this row's own protection is a Phase-1 `.percolate-ignore` entry
  (`dist/klabauter-toplevel/.percolate-ignore`'s `state/` line) rather than
  the secondary-assertion gate — asserted here against the real ignore
  matcher, not paraphrased.

Neither of these two mechanisms depends on the SPECIFIC filename
(`session-hierarchy.machine-b-local.json`) that tripped the identity guard
on 2026-08-12 — both are asserted against an arbitrary synthetic path under
`state/`, so a future incident under a different filename is caught the
same way.

The runtime writer that actually wrote `state/` into the live mirror
(`coordinator_core/state_root.py`) is a SEPARATE, peer-owned fix — out of
this file's scope; see docs/plans/2026-08-12-*-klabauter-mirror-allowlist-
omits-modules* / that plan's sibling chunk.

Run: python -m pytest coordinator/bin/tests/test_publish_state_exclusion.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_COORDINATOR_DIR = os.path.dirname(_BIN_DIR)
_CLAUDE_KLABAUTER_ROOT = Path(_COORDINATOR_DIR).parent

_COORDINATOR_LIB_DIR = os.path.join(_COORDINATOR_DIR, "lib")
if _COORDINATOR_LIB_DIR not in sys.path:
    sys.path.insert(0, _COORDINATOR_LIB_DIR)

from percolate import allowlist as allowlist_mod  # noqa: E402
from percolate import ignore as ignore_mod  # noqa: E402

_DEST_SIGIL = "publish-mirror:claude_klabauter"
_PORTABLE_PATH = _CLAUDE_KLABAUTER_ROOT / "setup" / "publish-targets.portable"


def _load_klabauter_rows() -> list[list[str]]:
    """Parse the real `setup/publish-targets.portable` and return every
    row (as its pipe-split fields) whose dest sigil (field index 2) is
    `publish-mirror:claude_klabauter`. Comments/blank lines skipped exactly
    as `targets.load_targets` skips them."""
    text = _PORTABLE_PATH.read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) >= 3 and fields[2] == _DEST_SIGIL:
            rows.append(fields)
    return rows


# ---------------------------------------------------------------------------
# Mechanism 1 — fail-closed allowlist: `state` must never be an admitted
# entry on any allowlisted claude_klabauter row.
# ---------------------------------------------------------------------------


def test_eight_klabauter_rows_present():
    """Sanity floor for the rest of this file: if the row count drifts, the
    per-row audit below (this file + the dispatch report) is stale and must
    be re-walked, not silently trusted."""
    rows = _load_klabauter_rows()
    assert len(rows) == 8, (
        f"expected 8 publish-mirror:claude_klabauter rows, found {len(rows)}: "
        f"{[r[0] for r in rows]} — re-audit this file's row-by-row rationale"
    )


def test_no_klabauter_allowlist_admits_state():
    """For every row that declares an allowlist (field index 6), `state`
    must not appear as a plain (inclusion) entry — the fail-closed allowlist
    mechanism means an entry absent here is an entry `build_allowlisted_source`
    will never copy, so this assertion is equivalent to "no row's copy phase
    can admit `state/`"."""
    rows = _load_klabauter_rows()
    checked_allowlisted = 0
    for fields in rows:
        name = fields[0]
        allowlist_csv = fields[6] if len(fields) > 6 else ""
        if not allowlist_csv:
            continue
        checked_allowlisted += 1
        entries = [e.strip() for e in allowlist_csv.split(",") if e.strip()]
        # Plain inclusion entries only — a `!`-exclusion can never GRANT
        # admission (module docstring, allowlist.py § Exclusion entries), so
        # a `!state` entry (there is none) would not need checking here, but
        # excluding it from the token set keeps this assertion honest about
        # what "admits" actually means.
        inclusion_entries = {e for e in entries if not e.startswith("!")}
        assert "state" not in inclusion_entries, (
            f"row {name!r} allowlist admits a top-level 'state' entry — "
            f"this would carry claude-klabauter's working substrate into the mirror"
        )

    assert checked_allowlisted == 7, (
        f"expected 7 of the 8 claude_klabauter rows to declare an allowlist "
        f"(the 8th, claude-klabauter-publish-repo-toplevel, deliberately has "
        f"none), found {checked_allowlisted} — row shape has drifted, re-audit"
    )


# ---------------------------------------------------------------------------
# Mechanism 2 — `check_working_data_paths` secondary assertion: proves the
# REAL production gate rejects an arbitrary `state/`-prefixed path, not a
# reimplementation of its deny-prefix logic.
# ---------------------------------------------------------------------------


def test_check_working_data_paths_rejects_arbitrary_state_path(tmp_path):
    """Synthetic filename, deliberately NOT the incident's own
    `session-hierarchy.machine-b-local.json` — this asserts the MECHANISM
    (a `state/`-prefixed path, any name) is what the gate rejects, not that
    one specific basename is blocklisted."""
    import io

    src = tmp_path / "restricted"
    (src / "state" / "priority-intent-inbox").mkdir(parents=True)
    (src / "state" / "priority-intent-inbox" / "arbitrary-entry.json").write_text(
        "{}", encoding="utf-8"
    )
    (src / "coordinator_core").mkdir()
    (src / "coordinator_core" / "keep.py").write_text("# fine\n", encoding="utf-8")

    err_buf = io.StringIO()
    ok = allowlist_mod.check_working_data_paths(src, stderr=err_buf)
    assert ok is False, "check_working_data_paths must hard-fail on any state/ path"
    assert "state/priority-intent-inbox/arbitrary-entry.json" in err_buf.getvalue()


def test_check_working_data_paths_clean_tree_unaffected(tmp_path, capsys):
    """Regression floor: a tree with no working-data paths still passes —
    this row's fix must not make the secondary assertion over-eager."""
    src = tmp_path / "restricted"
    (src / "coordinator_core").mkdir(parents=True)
    (src / "coordinator_core" / "keep.py").write_text("# fine\n", encoding="utf-8")

    ok = allowlist_mod.check_working_data_paths(src)
    assert ok is True


# ---------------------------------------------------------------------------
# Mechanism 1, fixture form — proves build_allowlisted_source's fail-closed
# admission actually excludes an unlisted top-level `state/` directory, using
# an arbitrary allowlist and an arbitrary filename inside `state/` (never the
# incident's own basename) — this is the config/mechanism assertion, not a
# hardcoded-filename one.
# ---------------------------------------------------------------------------


def test_build_allowlisted_source_excludes_unlisted_state_directory(tmp_path):
    real_src = tmp_path / "source"
    (real_src / "coordinator_core").mkdir(parents=True)
    (real_src / "coordinator_core" / "keep.py").write_text("# fine\n", encoding="utf-8")
    (real_src / "state" / "session-ledger").mkdir(parents=True)
    (real_src / "state" / "session-ledger" / "some-other-name.json").write_text(
        "{}", encoding="utf-8"
    )

    restricted = allowlist_mod.build_allowlisted_source(real_src, "coordinator_core")
    try:
        admitted = [p.relative_to(restricted).as_posix() for p in restricted.rglob("*") if p.is_file()]
        assert not any(p == "state" or p.startswith("state/") for p in admitted), (
            f"an allowlist that never named 'state' admitted a state/ path: {admitted}"
        )
        assert "coordinator_core/keep.py" in admitted, (
            "regression: build_allowlisted_source must still copy the "
            "legitimately-allowlisted entry unchanged"
        )
    finally:
        import shutil

        shutil.rmtree(restricted, ignore_errors=True)


# ---------------------------------------------------------------------------
# The 8th row — no allowlist, flat-mirror from dist/klabauter-toplevel/.
# Protection is the row's own `.percolate-ignore`, checked against the real
# matcher, not paraphrased.
# ---------------------------------------------------------------------------


def test_toplevel_row_source_dir_has_no_state_entry_on_disk():
    source_dir = _CLAUDE_KLABAUTER_ROOT / "dist" / "klabauter-toplevel"
    assert source_dir.is_dir(), f"expected {source_dir} to exist"
    assert not (source_dir / "state").exists(), (
        "dist/klabauter-toplevel/state must never exist — this row has no "
        "allowlist and no secondary assertion gate, so a staged state/ "
        "here would publish"
    )


def test_toplevel_row_percolate_ignore_blocks_a_future_state_entry():
    ignore_file = _CLAUDE_KLABAUTER_ROOT / "dist" / "klabauter-toplevel" / ".percolate-ignore"
    patterns = ignore_mod.load_percolate_ignore(ignore_file)
    # Arbitrary filename, any depth under state/ — proves the pattern, not
    # one basename.
    assert ignore_mod.is_ignored("state/some-arbitrary-file.json", patterns) is True
    assert ignore_mod.is_ignored("state/nested/deeper/file.txt", patterns) is True
    # Regression floor: files this row legitimately ships must still be kept.
    assert ignore_mod.is_ignored("README.md", patterns) is False
    assert ignore_mod.is_ignored("LICENSE", patterns) is False
