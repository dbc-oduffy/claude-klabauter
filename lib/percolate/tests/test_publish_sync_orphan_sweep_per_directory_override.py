"""Regression tests for the per-directory form of `COORDINATOR_OVERRIDE_ORPHAN_SWEEP`.

Before this, the env var was all-or-nothing for a whole round: an operator
who reasoned about removing ONE destination top-level directory had to arm
`=1` against every other top-level removal in the same round, including
ones they never looked at. `_orphan_sweep_override`/`_orphan_sweep_overridden`
(`coordinator/lib/percolate/publish_sync.py`) add a scoped comma-separated
form -- `COORDINATOR_OVERRIDE_ORPHAN_SWEEP=<dirname>[,<dirname>...]` --
that exempts exactly those top-level names from BOTH the top-level presence
preflight and the >50% mass-deletion guard, while every other orphan still
goes through them exactly as before. `=1` keeps working as the blanket form
(already exercised by `test_publish_sync_renamed_dir_exemption.py` and
`test_publish_sync_changed_paths_sink.py`).

Loaded via `coordinator/lib` on `sys.path` rather than a bare
`spec_from_file_location`: `publish_sync.py` does `from .ignore import ...`,
which only resolves as part of its `percolate` package -- same idiom as the
sibling orphan-sweep test files.

Negative-spec: no persona names, no codenames, no consumer-home path
literals; all fixture content is synthetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402


def _no_ignore():
    return publish_sync.load_ignore(None)


def _seed_with_two_orphan_dirs(tmp_path: Path) -> "tuple[Path, Path]":
    """A shared plugin dir (`kept`) plus two destination-only top-level
    directories (`orphan_a`, `orphan_b`) absent from source -- the shape
    the top-level presence preflight fires on for ANY orphan."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "kept").mkdir(parents=True)
    (dst / "kept").mkdir(parents=True)
    (src / "kept" / "file.py").write_text("kept\n", encoding="utf-8")
    (dst / "kept" / "file.py").write_text("kept\n", encoding="utf-8")
    (dst / "orphan_a").mkdir()
    (dst / "orphan_a" / "file.py").write_text("a\n", encoding="utf-8")
    (dst / "orphan_b").mkdir()
    (dst / "orphan_b" / "file.py").write_text("b\n", encoding="utf-8")
    return src, dst


class TestOrphanSweepOverrideParsing:
    def test_unset_is_no_override(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", raising=False)
        assert publish_sync._orphan_sweep_override() is False

    def test_literal_one_is_blanket_override(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "1")
        assert publish_sync._orphan_sweep_override() is True

    def test_comma_separated_list_is_a_scoped_frozenset(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "orphan_a, orphan_b")
        assert publish_sync._orphan_sweep_override() == frozenset({"orphan_a", "orphan_b"})

    def test_single_name_is_a_scoped_frozenset(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "orphan_a")
        assert publish_sync._orphan_sweep_override() == frozenset({"orphan_a"})

    def test_overridden_predicate_true_only_for_named_directory(self):
        override = frozenset({"orphan_a"})
        assert publish_sync._orphan_sweep_overridden("orphan_a", override) is True
        assert publish_sync._orphan_sweep_overridden("orphan_b", override) is False

    def test_overridden_predicate_honors_blanket_and_no_override(self):
        assert publish_sync._orphan_sweep_overridden("anything", True) is True
        assert publish_sync._orphan_sweep_overridden("anything", False) is False


class TestScopedOverrideExemptsOnlyNamedDirectories:
    def test_unset_still_aborts_on_any_orphan(self, tmp_path, monkeypatch):
        """Behaviour preservation: no override, both orphans present, real
        run -- still a FATAL SystemExit(3), unchanged from before this
        chunk."""
        monkeypatch.delenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", raising=False)
        src, dst = _seed_with_two_orphan_dirs(tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            publish_sync.sync_mirror(src, dst, _no_ignore(), False)
        assert excinfo.value.code == 3
        assert (dst / "orphan_a").is_dir()
        assert (dst / "orphan_b").is_dir()

    def test_scoping_one_name_still_aborts_for_the_other(self, tmp_path, monkeypatch):
        """The half that matters: naming ONLY `orphan_a` must not silently
        arm removal of `orphan_b` too -- the exact all-or-nothing gap this
        chunk closes."""
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "orphan_a")
        src, dst = _seed_with_two_orphan_dirs(tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            publish_sync.sync_mirror(src, dst, _no_ignore(), False)
        assert excinfo.value.code == 3
        assert (dst / "orphan_a").is_dir()
        assert (dst / "orphan_b").is_dir()

    def test_scoping_both_names_removes_only_those(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "orphan_a,orphan_b")
        src, dst = _seed_with_two_orphan_dirs(tmp_path)

        _synced, removed = publish_sync.sync_mirror(src, dst, _no_ignore(), False)

        assert not (dst / "orphan_a").exists()
        assert not (dst / "orphan_b").exists()
        assert (dst / "kept").is_dir()
        assert removed == 2

    def test_blanket_one_still_removes_both_unchanged(self, tmp_path, monkeypatch):
        """Behaviour preservation for the pre-existing all-or-nothing form."""
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "1")
        src, dst = _seed_with_two_orphan_dirs(tmp_path)

        _synced, removed = publish_sync.sync_mirror(src, dst, _no_ignore(), False)

        assert not (dst / "orphan_a").exists()
        assert not (dst / "orphan_b").exists()
        assert removed == 2

    def test_dry_run_never_aborts_even_when_unscoped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", raising=False)
        src, dst = _seed_with_two_orphan_dirs(tmp_path)

        _synced, removed = publish_sync.sync_mirror(src, dst, _no_ignore(), True)

        assert (dst / "orphan_a").is_dir()
        assert (dst / "orphan_b").is_dir()
        assert removed == 2
