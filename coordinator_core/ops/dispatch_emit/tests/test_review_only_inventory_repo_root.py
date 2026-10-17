"""``--review-only`` honours ``--repo-root``: a review-only emission expands no plan spec, so its
inventory record may live anywhere under the repo's ``state/`` (a ``state/warp/<run>/`` record
included); every other emission still needs ``state/mise-inventory/``."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.op import InventoryOutsideRepoError, _refuse_inventory_outside_repo


def _record(tmp_path, *parts):
    f = tmp_path.joinpath(*parts)
    f.parent.mkdir(parents=True)
    f.write_text("x\n", encoding="utf-8")
    return str(f)


def test_a_warp_record_is_accepted_for_review_only_with_a_repo_root(tmp_path):
    rec = _record(tmp_path, "state", "warp", "run-1", "manifest.json")
    _refuse_inventory_outside_repo(rec, tmp_path, review_only=True)


def test_the_same_record_still_refuses_for_a_non_review_emission(tmp_path):
    rec = _record(tmp_path, "state", "warp", "run-1", "manifest.json")
    with pytest.raises(InventoryOutsideRepoError):
        _refuse_inventory_outside_repo(rec, tmp_path)


def test_review_only_outside_the_repos_state_still_refuses(tmp_path):
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    rec = _record(elsewhere, "stuff", "inv.md")
    with pytest.raises(InventoryOutsideRepoError):
        _refuse_inventory_outside_repo(rec, tmp_path / "repo", review_only=True)


@pytest.mark.parametrize(
    "parts",
    [
        ("state", "warp", "run-1", "notes.json"),
        ("state", "warp", "manifest.json"),
        ("state", "warp", "run-1", "deeper", "manifest.json"),
        ("state", "other", "run-1", "manifest.json"),
    ],
)
def test_review_only_accepts_only_a_warp_runs_manifest_under_state(tmp_path, parts):
    rec = _record(tmp_path, *parts)
    with pytest.raises(InventoryOutsideRepoError):
        _refuse_inventory_outside_repo(rec, tmp_path, review_only=True)
