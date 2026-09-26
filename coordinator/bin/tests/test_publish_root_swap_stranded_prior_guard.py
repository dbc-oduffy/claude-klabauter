from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish():
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
    dest = _dest_root(tmp_path)
    (dest / "coordinator_core").mkdir()
    _pub._refuse_stranded_root_swap_prior(dest)


def test_stranded_entry_prior_refuses(tmp_path):
    dest = _dest_root(tmp_path)
    (dest / "coordinator_core.prior").mkdir()
    with pytest.raises(_pub.PublishSwapPartial) as excinfo:
        _pub._refuse_stranded_root_swap_prior(dest)
    assert "coordinator_core.prior" in str(excinfo.value)
    assert excinfo.value.content_swapped is False


def test_dotted_stranded_entry_prior_refuses(tmp_path):
    dest = _dest_root(tmp_path)
    (dest / ".github.prior").mkdir()
    with pytest.raises(_pub.PublishSwapPartial) as excinfo:
        _pub._refuse_stranded_root_swap_prior(dest)
    assert ".github.prior" in str(excinfo.value)
    assert excinfo.value.content_swapped is False


def test_fleet_env_prior_is_not_a_strand(tmp_path):
    """`.fleet-env.prior` is NAMED in
    `surface.STRUCTURAL_NEVER_PUBLISHED_PREFIXES` -- destination-repo build
    plumbing minted by something else entirely. An unfiltered check reads it
    as a strand and refuses every round on any box that has one, which is a
    worse failure than the one being guarded against."""
    dest = _dest_root(tmp_path)
    (dest / ".fleet-env.prior").mkdir()
    _pub._refuse_stranded_root_swap_prior(dest)


def test_guard_runs_before_the_root_branch_swaps_anything(tmp_path, monkeypatch):
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
