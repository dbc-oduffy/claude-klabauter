"""review.prep in run_id mode mints slice ids from the canonical prefix; a typed slice_prefix, even one missing the run token, is ignored."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.review_slice_id import prep_slice_id_for
from coordinator_core.ops.tests.test_review_prep import _base, _write, repo  # noqa: F401
from coordinator_core.ops.tests.test_review_prep_run_id import _params, _stage

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261026T133215-c4413e"
TOKEN = "c4413e"


def _ids(reply: dict) -> list:
    assert reply["error"] is None, reply["error"]
    return [s["id"] for s in reply["slices"]]


def _staged(repo: Path) -> list:
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    _stage(repo, rels, run_id=RUN)
    return rels


def test_a_token_less_typed_prefix_yields_the_canonical_ids(repo):
    _staged(repo)
    canonical = prep_slice_id_for("x/manifest.json", _base(repo), RUN)
    token_less = canonical.replace(f"{TOKEN}-", "")
    assert token_less != canonical

    full = _ids(rp._handler(_params(repo, canonical, run_id=RUN), repo))
    dropped = _ids(rp._handler(_params(repo, token_less, run_id=RUN), repo))

    assert len(full) > 1
    assert dropped == full
    assert all(f"-{TOKEN}-" in i for i in dropped)


def test_a_later_group_is_the_matching_contiguous_slice_of_the_whole_run_ids(repo):
    _staged(repo)
    canonical = prep_slice_id_for("x/manifest.json", _base(repo), RUN)
    token_less = canonical.replace(f"{TOKEN}-", "")

    whole = _ids(rp._handler(_params(repo, canonical, run_id=RUN), repo))
    grouped = _ids(rp._handler(_params(repo, token_less, run_id=RUN, group_index=1, group_count=2), repo))

    expected = [s["id"] for s in rp.group_slices([{"id": i} for i in whole], 2)[1]]
    assert grouped == expected
    assert grouped and set(grouped) < set(whole)


def test_an_omitted_slice_prefix_succeeds_with_the_same_ids(repo):
    _staged(repo)
    canonical = prep_slice_id_for("x/manifest.json", _base(repo), RUN)
    full = _ids(rp._handler(_params(repo, canonical, run_id=RUN), repo))

    params = _params(repo, canonical, run_id=RUN)
    del params["slice_prefix"]
    assert _ids(rp._handler(params, repo)) == full


def test_paths_mode_still_requires_slice_prefix(repo):
    rels = _staged(repo)
    params = _params(repo, "prep-x", paths=rels)
    del params["slice_prefix"]
    reply = rp._handler(params, repo)
    assert reply["error"] and reply["slices"] == []
