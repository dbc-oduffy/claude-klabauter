"""Regression tests for `sync_mirror`'s `foreign_dir_names` exemption.

The gap this parameter closes: `sync_mirror`'s orphan set treats any non-dot
destination top-level directory with no same-named entry in ITS source as an
orphan. When a mirror-mode row owns a destination ROOT and a second, sibling
row lands into a SUBDIRECTORY of that same root, the subdirectory is absent
from the mirror row's source by construction (nothing produces it there, and
nothing should) -- it looks exactly like a stray directory to this module,
which cannot see the row table. Unexempted, the 2026-07-26 top-level
presence preflight FATAL-aborts the whole publish the moment the sibling
lands its files, and the operator's only escape,
`COORDINATOR_OVERRIDE_ORPHAN_SWEEP=1`, does not skip the sweep -- it permits
it, which `rmtree`s the sibling's published output out of the mirror.

`foreign_dir_names` is deliberately a separate parameter from
`renamed_dir_names`, not a widening of it: `renamed_dir_names` comes from the
engine's rename ledger, and a wrong entry there oscillates loudly (create,
rename, delete, recreate next pass) -- easy to notice. `foreign_dir_names`
comes from the publish row set, and a wrong entry there is SILENT: no row
sweeps that subdirectory and no row refreshes it. That asymmetry is also why
the top-level presence preflight's abort text must be able to name which
exemption class it consulted -- a single merged set could not distinguish
"this name is exempt because it's a rename target" from "this name is exempt
because a sibling row owns it", which matters when deciding whether an
override is even the right fix (it never is for a sibling-owned name --
`foreign_dir_names` is).

Loaded via `coordinator/lib` on `sys.path` rather than a bare
`spec_from_file_location`: `publish_sync.py` does `from .ignore import ...`,
which only resolves when the module is loaded as part of its `percolate`
package -- the same idiom as the sibling
`test_publish_sync_top_level_orphan_sweep.py`.

Negative-spec: no persona names, no codenames, no consumer-home path
literals; all fixture content is synthetic. No `git init` and no subprocess
-- `sync_mirror` only reads and writes files.
"""

from __future__ import annotations

import sys
from pathlib import Path

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402


def _no_ignore():
    return publish_sync.load_ignore(None)


def _seed(tmp_path: Path) -> "tuple[Path, Path]":
    """A source and destination that agree on `kept/` and disagree on two
    destination-only top-level directories: `sibling/` (a second row's
    output) and `stray/` (a genuine orphan nobody owns). Mirrors the shape a
    real mirror-root row plus a sibling subdirectory row produces."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "kept").mkdir(parents=True)
    (dst / "kept").mkdir(parents=True)
    (src / "kept" / "file.py").write_text("kept\n", encoding="utf-8")
    (dst / "kept" / "file.py").write_text("kept\n", encoding="utf-8")
    (dst / "sibling").mkdir(parents=True)
    (dst / "sibling" / "owned.py").write_text("sibling\n", encoding="utf-8")
    return src, dst


class TestForeignDirNamesExemptsASiblingRow:
    def test_sibling_subdir_exempted_preflight_and_sweep_stay_quiet(self, tmp_path, capsys):
        """Both consumers of the single `orphans` list -- the top-level
        presence preflight and the rmtree sweep -- must stay quiet on a
        name in `foreign_dir_names`."""
        src, dst = _seed(tmp_path)

        synced, removed = publish_sync.sync_mirror(
            src, dst, _no_ignore(), False,
            foreign_dir_names=frozenset({"sibling"}),
        )

        assert (dst / "sibling" / "owned.py").is_file()
        assert removed == 0
        captured = capsys.readouterr()
        assert "FATAL" not in captured.err
        assert "sibling" not in captured.err

    def test_a_genuine_orphan_alongside_an_exempted_one_still_aborts(self, tmp_path):
        """Proves the exemption is name-scoped, not a blanket disarm: a
        second, genuinely-stray directory at the same level must still
        FATAL-abort the publish even though `sibling/` is exempted."""
        src, dst = _seed(tmp_path)
        (dst / "stray").mkdir()
        (dst / "stray" / "orphan.py").write_text("stray\n", encoding="utf-8")

        try:
            publish_sync.sync_mirror(
                src, dst, _no_ignore(), False,
                foreign_dir_names=frozenset({"sibling"}),
            )
            raised = False
        except SystemExit as exc:
            raised = True
            assert exc.code == 3

        assert raised, "a non-exempted orphan alongside an exempted one must still abort"
        assert (dst / "sibling" / "owned.py").is_file()
        assert (dst / "stray" / "orphan.py").is_file()

    def test_omitting_the_parameter_aborts_exactly_as_today(self, tmp_path):
        """The version-skew arm: a caller (or a copy of this module) that does
        not know about `foreign_dir_names` must see zero behaviour change --
        the sibling directory is indistinguishable from a stray one and the
        publish aborts exactly as it did before this parameter existed."""
        src, dst = _seed(tmp_path)

        try:
            publish_sync.sync_mirror(src, dst, _no_ignore(), False)
            raised = False
        except SystemExit as exc:
            raised = True
            assert exc.code == 3

        assert raised
        assert (dst / "sibling" / "owned.py").is_file()
