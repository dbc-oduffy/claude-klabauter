"""since_base_overlap: a dropped since-base line the run itself recorded at an earlier pass is not foreign;
a genuinely foreign dropped hunk is still refused and the finding names the commit and the hunk origin."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.git import since_base_overlap
from coordinator_core.git.git_objects import write_object
from coordinator_core.git.tests.test_since_base_overlap import PATH, _base_lines, _join, _Repo
from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP

RUN_ID = "ask-20261109T000000-aaaaaa"
OWN_HUNK = ["def put_blob(x):\n", "    return store_put(x)\n", "def get_blob(x):\n", "    return store_get(x)\n"]
FOREIGN_HUNK = ["def peer_only(y):\n", "    return peer_feature(y)\n"]


def _record(tmp_path: Path, repo: _Repo, name: str, text: str, shape: str) -> None:
    blob = write_object(repo.gitdir, b"blob", text.encode())
    directory = tmp_path / "state" / "warp" / RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    body = {
        "kept": {"chunks": {"C1": {"paths": {PATH: blob}}}},
        "flat": {PATH: blob},
        "blobs": {"base_sha": "x", "blobs": {PATH: blob}},
    }[shape]
    (directory / name).write_text(json.dumps(body), encoding="utf-8")


def _swept(tmp_path: Path, peer_adds: list, recorded: list, name: str = "kept.json", shape: str = "kept"):
    """Base B; the run recorded ``recorded`` on top of it; a peer commit lands ``peer_adds``; the run
    then removes them all."""
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    if recorded:
        _record(tmp_path, repo, name, _join(base_lines + recorded), shape)
    peer = repo.commit({PATH: _join(base_lines + peer_adds)}, "hand-land: sweep")
    repo.land(_join(base_lines))
    return base, peer


def _check(tmp_path: Path, base: str):
    return since_base_overlap.check(tmp_path, [PATH], base, run_id=RUN_ID)


def test_hunk_swept_by_a_peer_commit_then_removed_by_the_run_lands(tmp_path: Path) -> None:
    base, _peer = _swept(tmp_path, OWN_HUNK, OWN_HUNK)

    assert _check(tmp_path, base) == ([], [])


def test_each_run_record_shape_discounts_its_own_lines(tmp_path: Path) -> None:
    for i, (name, shape) in enumerate([("withheld-prints.json", "flat"), ("measured-rev.json", "blobs")]):
        sub = tmp_path / f"r{i}"
        sub.mkdir()
        base, _peer = _swept(sub, OWN_HUNK, OWN_HUNK, name, shape)

        assert since_base_overlap.check(sub, [PATH], base, run_id=RUN_ID) == ([], [])


def test_genuinely_foreign_hunk_is_refused_naming_commit_and_origin(tmp_path: Path) -> None:
    base, peer = _swept(tmp_path, FOREIGN_HUNK, OWN_HUNK)

    findings, notes = _check(tmp_path, base)

    assert notes == []
    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]
    start = len(_base_lines()) + 1
    assert peer[:10] in findings[0].detail
    assert f"lines {start}-{start + 1} of {peer[:10]}" in findings[0].detail


def test_a_swept_commit_mixing_own_and_foreign_lines_counts_only_the_foreign(tmp_path: Path) -> None:
    base, peer = _swept(tmp_path, OWN_HUNK + FOREIGN_HUNK, OWN_HUNK)

    findings, _notes = _check(tmp_path, base)

    assert [f.detail.startswith(f"drops 2 line(s) added by {peer[:10]}") for f in findings] == [True]


def test_without_a_run_record_the_swept_hunk_is_still_foreign(tmp_path: Path) -> None:
    base, _peer = _swept(tmp_path, OWN_HUNK, [])

    assert [f.reason for f in _check(tmp_path, base)[0]] == [SINCE_BASE_OVERLAP]


def test_another_runs_record_does_not_discount(tmp_path: Path) -> None:
    base, _peer = _swept(tmp_path, OWN_HUNK, OWN_HUNK)

    findings, _notes = since_base_overlap.check(tmp_path, [PATH], base, run_id="ask-20261109T000000-bbbbbb")

    assert [f.reason for f in findings] == [SINCE_BASE_OVERLAP]
