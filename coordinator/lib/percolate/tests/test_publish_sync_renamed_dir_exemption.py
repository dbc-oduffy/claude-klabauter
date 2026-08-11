"""Regression test for `sync_mirror`'s `renamed_dir_names` orphan-sweep exemption.

Covers the forward-compatible hook added to `coordinator/lib/percolate/publish_sync.py`
for the engine-side directory-rename primitive (§ state/audits/2026-08-05-first-full-
payload-identity-findings.md Group E, and
coordinator_core/percolate/rewrite_basename.py's `rename_directories`). Not yet wired
to any real publish target -- see this module's own `sync_mirror` docstring for why:
closing the hazard end to end also needs a `coordinator/bin/publish.py` call-site change
(reading the engine's rename ledger and passing it into this parameter) and an
`coordinator_core/percolate/engine.py` sequencing change (reap-before-rename), both out
of scope for this dispatch.

Loaded by file path (`importlib.util.spec_from_file_location`), matching the convention
`coordinator/tests/test_percolate_driver_sync.py` already uses for this same module --
`coordinator/` and `coordinator/lib/` carry no `__init__.py`, so a dotted import is not
available.

Negative-spec: no persona names, no codenames, no consumer-home path literals. All
fixture content is synthetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `publish_sync.py` does `from .ignore import ...` -- a package-relative import that
# only resolves when the module is loaded AS PART OF its `percolate` package, not via a
# bare `spec_from_file_location` (which `coordinator/tests/test_percolate_driver_sync.py`
# avoids by going through `publish._import_publish_sync` instead). `coordinator/` and
# `coordinator/lib/` carry no `__init__.py` (no dotted import available from repo root),
# but `coordinator/lib/percolate/` DOES have one, so putting `coordinator/lib` on
# `sys.path` and importing `percolate.publish_sync` as an ordinary package member
# resolves the relative import correctly.
_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402


def _make_ignore():
    return publish_sync.load_ignore(None)


class TestRenamedDirNamesExemption:
    def test_default_none_preserves_existing_orphan_removal(self, tmp_path, monkeypatch):
        """Omitting the parameter entirely must behave exactly as before its
        introduction -- an unrelated leftover directory is still removed as an
        orphan. `COORDINATOR_OVERRIDE_ORPHAN_SWEEP=1` is required here for a reason
        independent of this parameter: the pre-existing 2026-07-26 top-level-presence
        preflight FATAL-aborts on ANY orphan by default -- this is exactly the
        production hazard the `sync_mirror` docstring's Group-E note names (a real
        directory-rename row would trip this same preflight without BOTH this
        exemption AND that env override, or a further guard change, wired end to
        end)."""
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "1")
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (dst / "stray").mkdir()
        (dst / "stray" / "leftover.txt").write_text("x", encoding="utf-8")

        publish_sync.sync_mirror(src, dst, _make_ignore(), dry_run=False)

        assert not (dst / "stray").exists()

    def test_renamed_dir_name_is_exempt_from_orphan_removal(self, tmp_path):
        """A destination directory the caller names via `renamed_dir_names` -- e.g.
        one the engine's own directory-rename primitive produced last pass -- must
        survive the orphan sweep even though no source directory shares its name."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (dst / "renamed-target").mkdir()
        (dst / "renamed-target" / "kept.txt").write_text("payload", encoding="utf-8")

        publish_sync.sync_mirror(
            src, dst, _make_ignore(), dry_run=False,
            renamed_dir_names=frozenset({"renamed-target"}),
        )

        assert (dst / "renamed-target").is_dir()
        assert (dst / "renamed-target" / "kept.txt").read_text(encoding="utf-8") == "payload"

    def test_exemption_does_not_shield_an_unrelated_directory(self, tmp_path, monkeypatch):
        """The exemption is name-scoped -- a DIFFERENT stray directory not named in
        `renamed_dir_names` is still reaped as an ordinary orphan. Same preflight
        override reason as the first test above."""
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "1")
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (dst / "renamed-target").mkdir()
        (dst / "genuinely-stray").mkdir()

        publish_sync.sync_mirror(
            src, dst, _make_ignore(), dry_run=False,
            renamed_dir_names=frozenset({"renamed-target"}),
        )

        assert (dst / "renamed-target").is_dir()
        assert not (dst / "genuinely-stray").exists()

    def test_dry_run_never_deletes_an_unexempted_orphan_either(self, tmp_path):
        """Sanity check that the exemption plumbing did not disturb the pre-existing
        dry-run-never-deletes contract."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (dst / "stray").mkdir()

        publish_sync.sync_mirror(
            src, dst, _make_ignore(), dry_run=True,
            renamed_dir_names=frozenset(),
        )

        assert (dst / "stray").is_dir()
