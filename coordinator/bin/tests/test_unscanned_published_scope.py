"""test_unscanned_published_scope — regression tests for the unscanned-
published-guard FALSE-POSITIVE fixed here: `dispatch_end_of_run_
unscanned_published_check`'s `published` set used to be "every file that
exists under the repo root right now," compared against `scanned` = "what
THIS invocation's sweep visited." Any destination file this invocation did
not publish -- a prior publish, a `--target`-excluded sibling row, a row
skipped by a failed pre-sync gate -- has no entry in the real `scanned` set
and was reported as "published but never visited," even though this
invocation never touched it.

The fix (§ `publish.py`'s `dispatch_end_of_run_unscanned_published_check`
docstring, `published_dest_dirs_by_repo_root` param) scopes `published` down
to files under a `dest_dir` THIS RUN actually swapped (§ `process_target`'s
`published_dest_dirs_sink`, populated only after `_swap_publish_staging_
into_dest` has succeeded for that row) -- never the whole repo root.

Two directions this file pins, per dispatch brief:
  1. An injected-but-unswept file that WAS published this run (lives under a
     swapped dest_dir) still fails the check -- the original `2cb8f4103` hole
     must stay closed (`test_published_but_unswept_file_still_fails`).
  2. A destination file this invocation never published (exists under the
     repo root, but NOT under any dest_dir this run swapped) does NOT fail
     the check (`test_unpublished_destination_file_does_not_fail`).

A third direction, `test_swept_then_renamed_file_does_not_fail`, guards
against reopening the SEPARATE `basename-rename` false-positive already
fixed in `coordinator_core/percolate/engine.py` (commit e3fe55ea0,
`run_post_rsync`'s `RenameManifest.resolve` reconciliation, § its own
`TestVisitedFilesRecording` coverage in `test_engine.py`) -- this file's
`scanned`/`published` sets are supplied directly by the caller, so it proves
the SCOPING fix here composes correctly with an already-reconciled (post-
rename) visited id, not that the reconciliation itself is correct (that is
`test_engine.py`'s job).

Run: python -m pytest coordinator/bin/tests/test_unscanned_published_scope.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_unscanned_published_scope_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _target(tmp_path: Path, name="t", dest_subdir: str = "dst") -> "publish.ResolvedTarget":
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    dst = tmp_path / dest_subdir
    dst.mkdir(parents=True, exist_ok=True)
    return publish.ResolvedTarget(name=name, mode="mirror", source_dir=src, dest_dir=dst)


class TestPublishedScopeFalsePositiveFix:
    def test_published_but_unswept_file_still_fails(self, tmp_path, capsys):
        """Direction 1 (§ module docstring) -- the 2cb8f4103 hole stays
        closed: a file injected into a dest_dir THIS RUN swapped, but never
        recorded in the real `scanned` set, is still a hard failure even
        though `published` is now scoped to swapped dest_dirs rather than
        the whole repo root."""
        target = _target(tmp_path)
        (target.dest_dir / "injected-after-sweep.py").write_text("print('x')\n", encoding="utf-8")
        section = {"file_surface": {}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)],
            target_filtered=False,
            visited_files_by_repo_root={target.dest_dir: set()},
            published_dest_dirs_by_repo_root={target.dest_dir: {target.dest_dir}},
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "injected-after-sweep.py" in captured.err
        assert "unscanned-published check FAILED" in captured.err

    def test_unpublished_destination_file_does_not_fail(self, tmp_path, capsys):
        """Direction 2 (§ module docstring) -- the bug this dispatch fixes: a
        file that exists under the repo root but under NO dest_dir this run
        swapped (a prior publish, a --target-excluded sibling, a row a
        failed pre-sync gate skipped) must not be flagged, even though it
        has no entry in the real `scanned` set either."""
        target = _target(tmp_path)
        (target.dest_dir / "never-touched-this-run.py").write_text(
            "print('stale')\n", encoding="utf-8"
        )
        section = {"file_surface": {}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)],
            target_filtered=False,
            visited_files_by_repo_root={target.dest_dir: set()},
            # This run swapped NOTHING for this repo root (empty set) --
            # simulates every row sharing target.dest_dir's repo root being
            # skipped/gated/filtered out of this invocation.
            published_dest_dirs_by_repo_root={target.dest_dir: set()},
        )
        assert ok is True
        captured = capsys.readouterr()
        assert "never-touched-this-run.py" not in captured.err

    def test_target_filtered_run_only_scopes_to_the_filtered_row(self, tmp_path, capsys):
        """The exact `--target` shape from the dispatch brief's reproduction:
        a repo root shared by two rows, only one of which this invocation
        processed (the other's dest_dir never appears in
        `published_dest_dirs_by_repo_root`). The unfiltered row's
        pre-existing file must not fail the filtered run's check."""
        repo_root = tmp_path / "dest-repo"
        repo_root.mkdir()
        filtered_dest = repo_root / "sub-a"
        filtered_dest.mkdir()
        other_dest = repo_root / "sub-b"
        other_dest.mkdir()
        (other_dest / "from-a-different-row.py").write_text("print('y')\n", encoding="utf-8")

        filtered_target = publish.ResolvedTarget(
            name="row-a", mode="mirror", source_dir=tmp_path / "src", dest_dir=filtered_dest
        )
        section = {"file_surface": {}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(filtered_target, section)],
            target_filtered=True,
            visited_files_by_repo_root={repo_root: set()},
            published_dest_dirs_by_repo_root={repo_root: {filtered_dest}},
        )
        assert ok is True
        captured = capsys.readouterr()
        assert "from-a-different-row.py" not in captured.err

    def test_swept_then_renamed_file_does_not_fail(self, tmp_path):
        """Direction 3 (§ module docstring) -- composes with the SEPARATE
        rename-reconciliation fix (engine.py commit e3fe55ea0): a visited id
        supplied here is already POST-rename (as `run_post_rsync` now
        guarantees), and the file on disk at that post-rename path, inside a
        swapped dest_dir, must not be flagged."""
        target = _target(tmp_path)
        (target.dest_dir / "new-name.py").write_text("print('renamed')\n", encoding="utf-8")
        section = {"file_surface": {}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)],
            target_filtered=False,
            visited_files_by_repo_root={target.dest_dir: {target.dest_dir / "new-name.py"}},
            published_dest_dirs_by_repo_root={target.dest_dir: {target.dest_dir}},
        )
        assert ok is True
