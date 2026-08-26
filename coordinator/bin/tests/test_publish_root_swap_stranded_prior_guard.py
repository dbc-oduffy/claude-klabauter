"""test_publish_root_swap_stranded_prior_guard -- pins
`_refuse_stranded_root_swap_prior`, the root-dest branch's stranded-prior
refusal.

The whole-tree branch has carried a stranded-prior refusal for a long time;
the root-dest branch could neither REACH it (`_swap_publish_staging_into_dest`
returns right after delegating) nor MATCH it (that refusal globs
`dest_dir.parent` for `.{name}.publish-staging-*.prior` and predicates on a
`.git` inside the candidate, while the root branch mints `<entry>.prior`
inside `dest_dir` and never moves `.git`). Both publish mirrors are repo roots,
so both take the unguarded branch.

What a strand costs, and why this is a refusal rather than a warning:
`_swap_publish_staging_entry`'s restore sits in an `except OSError` and cannot
cover a SIGKILL between its two renames. A death in that window leaves a whole
top-level subtree absent from the worktree, its only copy in an untracked
`<entry>.prior`, and every file still tracked at dest HEAD -- percolate-round's
removal side reads exactly that as retired payload, and
`_refuse_removals_present_on_disk` cannot catch it because the paths really are
absent.

Negative-spec: this file does not test the swap itself (§
`test_publish_refusal_record.py`) and does not run a publish row -- every
fixture is a throwaway `tmp_path` directory.

Run: python3 -m pytest coordinator/bin/tests/test_publish_root_swap_stranded_prior_guard.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish():
    # Registered in `sys.modules` before `exec_module`: publish.py declares
    # dataclasses, and `dataclasses._is_type` resolves the defining module by
    # name off `sys.modules` -- an unregistered module makes collection fail
    # with an unrelated-looking `AttributeError` on `NoneType`.
    spec = importlib.util.spec_from_file_location("publish_root_swap_guard", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_pub = _load_publish()


def _dest_root(tmp_path: Path) -> Path:
    dest = tmp_path / "mirror"
    (dest / ".git").mkdir(parents=True)
    return dest


def test_clean_root_dest_is_not_refused(tmp_path):
    """The ordinary case: no `.prior` in the destination root, no refusal.
    A successful swap `rmtree`s its own aside copy, so this is what every
    round between incidents looks like."""
    dest = _dest_root(tmp_path)
    (dest / "coordinator_core").mkdir()
    _pub._refuse_stranded_root_swap_prior(dest)  # must not raise


def test_stranded_entry_prior_refuses(tmp_path):
    """A surviving `<entry>.prior` is the signature of a swap that did not
    finish -- the subtree it holds is absent from the worktree and still
    tracked at HEAD, which is precisely what the removal side would read as
    retired payload."""
    dest = _dest_root(tmp_path)
    (dest / "coordinator_core.prior").mkdir()
    with pytest.raises(_pub.PublishSwapPartial) as excinfo:
        _pub._refuse_stranded_root_swap_prior(dest)
    assert "coordinator_core.prior" in str(excinfo.value)
    assert excinfo.value.content_swapped is False


def test_fleet_env_prior_is_not_a_strand(tmp_path):
    """`.fleet-env.prior` is NAMED in
    `surface.STRUCTURAL_NEVER_PUBLISHED_PREFIXES` -- destination-repo build
    plumbing minted by something else entirely. An unfiltered check reads it
    as a strand and refuses every round on any box that has one, which is a
    worse failure than the one being guarded against."""
    dest = _dest_root(tmp_path)
    (dest / ".fleet-env.prior").mkdir()
    _pub._refuse_stranded_root_swap_prior(dest)  # must not raise


def test_guard_runs_before_the_root_branch_swaps_anything(tmp_path, monkeypatch):
    """Ordering is the whole point: the refusal must fire BEFORE
    `_swap_publish_staging_into_dest_root` touches the destination, or the
    round overwrites the strand it was meant to preserve."""
    dest = _dest_root(tmp_path)
    (dest / "coordinator_core.prior").mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()

    swapped: list = []
    monkeypatch.setattr(
        _pub,
        "_swap_publish_staging_into_dest_root",
        lambda d, s: swapped.append((d, s)),
    )
    with pytest.raises(_pub.PublishSwapPartial):
        _pub._swap_publish_staging_into_dest(dest, staging)
    assert swapped == []
