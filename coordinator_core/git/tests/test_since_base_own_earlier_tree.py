"""since_base_overlap: lines a peer commit re-landed byte-identical to lines the run's own commit introduced
earlier are the run's own; a peer line the run never held, or one an own commit merely carried, is still foreign."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git import since_base_overlap
from coordinator_core.git.tests.test_since_base_overlap import PATH, PLAN, _base_lines, _join, _Repo
from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP

HUNK = ["def as_date(x):\n", "    return parse_date(x)\n"]
PEER = ["def peer_only(y):\n", "    return peer_feature(y)\n"]
OWN_MSG = f"checkpoint(wave 0): C1\n\nCheckpoint-Plan: {PLAN}"


def _check(tmp_path: Path, base: str):
    return since_base_overlap.check(tmp_path, [PATH], base, plan=PLAN)


def test_peer_relanding_of_the_runs_earlier_lines_then_rewritten_is_not_foreign(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    repo.commit({PATH: _join(base_lines + HUNK)}, OWN_MSG)
    repo.commit({PATH: _join(base_lines)}, "unrelated revert")
    repo.commit({PATH: _join(base_lines + HUNK)}, "hand-land: byte-identical copy")
    repo.land(_join(base_lines + ["def as_date(x):\n", "    return parse_iso(x)\n"]))

    assert _check(tmp_path, base) == ([], [])


def test_peer_lines_the_run_never_held_are_still_refused(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    repo.commit({PATH: _join(base_lines + HUNK)}, OWN_MSG)
    repo.commit({PATH: _join(base_lines)}, "unrelated revert")
    peer = repo.commit({PATH: _join(base_lines + PEER)}, "peer feature")
    repo.land(_join(base_lines))

    findings, _notes = _check(tmp_path, base)

    assert [(f.reason, f.detail.startswith(f"drops 2 line(s) added by {peer[:10]}")) for f in findings] == [
        (SINCE_BASE_OVERLAP, True)
    ]


def test_peer_line_an_own_commit_merely_carried_is_still_refused(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    peer = repo.commit({PATH: _join(base_lines + PEER)}, "peer feature")
    repo.commit({PATH: _join(base_lines + PEER + HUNK)}, OWN_MSG)
    repo.land(_join(base_lines + HUNK))

    findings, _notes = _check(tmp_path, base)

    assert [(f.reason, f.detail.startswith(f"drops 2 line(s) added by {peer[:10]}")) for f in findings] == [
        (SINCE_BASE_OVERLAP, True)
    ]
