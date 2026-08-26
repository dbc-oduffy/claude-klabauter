"""Regression tests for `sync_mirror`'s `sweep_top_level_orphans` opt-in.

The gap (2026-08-26): `_sync_mirror_top_level_files` carried a blanket negative-spec
-- "deliberately does NOT delete destination top-level files that are absent from the
source, and must not grow one" -- reasoning that a mirror repo's own `README.md`,
`LICENSE`, and dotfiles live at a destination root the publisher does not own. That is
right for a row landing at a repo ROOT. It was wrong as a rule for every row: a row
projecting into a destination SUBDIRECTORY owns every file directly under it, and for
`claude-klabauter-coordinator-bin` every published CLI IS a top-level file. Retirement
was therefore unrepresentable for that whole row -- `coordinator/bin/detect-staged-
rollback.py`/`.cmd` were deleted at source AND correctly dropped from the row's
allowlist, and went on shipping in the mirror regardless, because nothing could ever
take a name back off it.

The fix splits the decision from the mechanism: `_sweep_mirror_top_level_orphans` does
the deleting, `sync_mirror`'s `sweep_top_level_orphans` gates it, and the CALLER -- the
only layer that can see where a row lands -- decides. These tests pin both halves:
the sweep's behaviour here, and `publish._dest_is_owned_subdir`'s root-vs-subdir verdict
(including its fail-closed answer when no repo root can be found at all).

Loaded via `coordinator/lib` on `sys.path` rather than a bare
`spec_from_file_location`: `publish_sync.py` does `from .ignore import ...`, which only
resolves when the module is loaded as part of its `percolate` package -- the same
reason, and the same idiom, as the sibling
`test_publish_sync_renamed_dir_exemption.py`.

Negative-spec: no persona names, no codenames, no consumer-home path literals; all
fixture content is synthetic. No `git init` and no subprocess -- `sync_mirror` only
reads and writes files, and `_dest_is_owned_subdir` keys on a `.git` ENTRY existing,
which an empty directory satisfies.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PUBLISH_PY_PATH = _REPO_ROOT / "coordinator" / "bin" / "publish.py"


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_top_level_orphan_sweep_under_test", _PUBLISH_PY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _no_ignore():
    return publish_sync.load_ignore(None)


def _seed(tmp_path: Path) -> "tuple[Path, Path]":
    """A source and destination whose top level agree on `kept.py` and disagree on
    `retired.py` -- the destination still carries a CLI the source dropped. One
    subdirectory on each side keeps this a realistic mirror-mode row rather than a
    degenerate top-level-only one, and keeps the empty-source mass-delete preflight
    from firing on a source that would otherwise have zero files."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (dst / "sub").mkdir(parents=True)
    (src / "kept.py").write_text("kept\n", encoding="utf-8")
    (dst / "kept.py").write_text("kept\n", encoding="utf-8")
    (dst / "retired.py").write_text("retired\n", encoding="utf-8")
    (src / "sub" / "inner.py").write_text("inner\n", encoding="utf-8")
    (dst / "sub" / "inner.py").write_text("inner\n", encoding="utf-8")
    return src, dst


class TestSweepIsOptIn:
    def test_default_leaves_a_top_level_orphan_alone(self, tmp_path):
        """Behaviour preservation for every pre-existing caller: with the flag
        omitted entirely, `sync_mirror` must not delete a destination top-level
        file. A root-landing row depends on exactly this -- its destination root
        holds repo-owned `README.md`/`LICENSE` that no row published."""
        src, dst = _seed(tmp_path)

        _synced, removed = publish_sync.sync_mirror(src, dst, _no_ignore(), False)

        assert (dst / "retired.py").is_file()
        assert removed == 0

    def test_opt_in_deletes_the_top_level_orphan(self, tmp_path):
        """The retirement that was unrepresentable before this flag existed."""
        src, dst = _seed(tmp_path)

        _synced, removed = publish_sync.sync_mirror(
            src, dst, _no_ignore(), False, sweep_top_level_orphans=True
        )

        assert not (dst / "retired.py").exists()
        assert removed == 1

    def test_opt_in_keeps_a_file_the_source_still_has(self, tmp_path):
        """The sweep is scoped by source membership, not by "top-level file"."""
        src, dst = _seed(tmp_path)

        publish_sync.sync_mirror(
            src, dst, _no_ignore(), False, sweep_top_level_orphans=True
        )

        assert (dst / "kept.py").is_file()
        assert (dst / "sub" / "inner.py").is_file()

    def test_opt_in_never_touches_a_destination_dotfile(self, tmp_path):
        """A dotfile at a destination root is publish machinery or repo-owned,
        never a row's payload -- the copy leg already skips dotfiles on the
        source side, and the sweep must skip them on the destination side or it
        deletes the very `.percolate-ignore`/`.gitignore` that governs it."""
        src, dst = _seed(tmp_path)
        (dst / ".gitignore").write_text("*.pyc\n", encoding="utf-8")

        publish_sync.sync_mirror(
            src, dst, _no_ignore(), False, sweep_top_level_orphans=True
        )

        assert (dst / ".gitignore").is_file()

    def test_an_engine_renamed_file_is_exempt(self, tmp_path):
        """The exemption without which this sweep is destructive rather than
        merely aggressive. The engine's content-transform pass renames published
        files at the DESTINATION only -- the source keeps its own name forever --
        so a renamed file is absent from the source by construction and looks
        exactly like a dropped one. Measured on the first real preview of this
        sweep: 10 of 12 proposed deletions were renamed published files, which
        the next transform pass would have re-created under the same names.
        That is the file-granular form of the oscillation `renamed_dir_names`
        already prevents for directories."""
        src, dst = _seed(tmp_path)
        (dst / "renamed-by-the-engine.py").write_text("published\n", encoding="utf-8")

        _synced, removed = publish_sync.sync_mirror(
            src,
            dst,
            _no_ignore(),
            False,
            sweep_top_level_orphans=True,
            renamed_file_names=frozenset({"renamed-by-the-engine.py"}),
        )

        assert (dst / "renamed-by-the-engine.py").is_file()
        assert not (dst / "retired.py").exists()
        assert removed == 1

    def test_dry_run_reports_without_deleting(self, tmp_path):
        src, dst = _seed(tmp_path)

        _synced, removed = publish_sync.sync_mirror(
            src, dst, _no_ignore(), True, sweep_top_level_orphans=True
        )

        assert (dst / "retired.py").is_file()
        assert removed == 1


class TestDestIsOwnedSubdirDecidesTheFlag:
    """`publish._dest_is_owned_subdir` is the only thing standing between this
    sweep and a destination repo's own root files, so its three answers are
    pinned individually rather than through a publish round."""

    def test_a_repo_root_is_not_an_owned_subdir(self, tmp_path):
        publish = _load_publish_module()
        (tmp_path / ".git").mkdir()

        assert publish._dest_is_owned_subdir(tmp_path) is False

    def test_a_subdirectory_beneath_a_repo_root_is_owned(self, tmp_path):
        publish = _load_publish_module()
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "coordinator" / "bin"
        subdir.mkdir(parents=True)

        assert publish._dest_is_owned_subdir(subdir) is True

    def test_no_repo_root_anywhere_fails_closed(self, tmp_path):
        """"Could not determine" must never authorize a delete. A bare
        `_dest_repo_root(d) != d` reads `None` as not-the-root and sweeps --
        this is the arm that pins it does not."""
        publish = _load_publish_module()
        orphan_dir = tmp_path / "nowhere" / "under" / "no" / "repo"
        orphan_dir.mkdir(parents=True)

        assert publish._dest_is_owned_subdir(orphan_dir) is False
