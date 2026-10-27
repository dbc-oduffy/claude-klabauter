"""The `ceremony.commit_v2` calls of one terminal commit's landing leg, at a session that holds
CLAIMED_IGNORED gitignored written files: each call is handed only landable paths, and the leg sums
under the 500ms brightline (DR-344) with a bounded spawn count.

The leg is the product commit, the own-left-behind follow-on and the coded stamp. Process time is
the sum of one bracket per commit_v2 dispatch plus the follow-on's ignore answer (own CPU plus
descendants); per-call and whole-handler figures are diagnostics and nothing asserts on them. Wall clock is never read. Assertions (1)-(3) are pending_fix on bug row 29d78c496834.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core.benchmarks import commit_v2_landing_leg_shape as shape
from coordinator_core.benchmarks.commit_v2_landing_leg_shape import CLAIMED_IGNORED, LANDABLE_PATHS
from coordinator_core.git.machine_local_paths import machine_local_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

LEG_BAR_MS = 500.0
# One batched `git check-ignore --stdin` answering the ignore question for the follow-on's
# candidate set; the product commit, the follow-on landing and the coded stamp are native (zero spawns).
LEG_SPAWN_BOUND = 1

_PENDING = pytest.mark.pending_fix(reason="bug row 29d78c496834: commit_v2 landing leg classifies the whole claim set")


@pytest.fixture(scope="module")
def leg(tmp_path_factory):
    patch = pytest.MonkeyPatch()
    patch.setenv("COORDINATOR_SESSION_ID", shape.SELF)
    patch.delenv("CLAUDE_SESSION_ID", raising=False)
    patch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    try:
        repo = shape.build_fixture(tmp_path_factory.mktemp("leg"))
        yield repo, shape.run_leg(repo)
    finally:
        patch.undo()


def test_the_fixture_claims_the_ignored_set_and_the_leg_lands_the_product(leg):
    repo, report = leg
    assert report.reply.get("committed") is True, report.reply
    assert len(report.calls) == 3, [c.paths[:3] for c in report.calls]
    claimed = [
        f for d in (shape.SHARE_DIR, shape.OTHER_IGNORED_DIR) for f in (repo / d).iterdir() if f.is_file()
    ]
    assert len(claimed) == CLAIMED_IGNORED


def test_the_ignored_claims_split_between_prefix_dropped_and_ignore_answer_dropped(leg):
    repo, _ = leg
    rels = [
        f.relative_to(repo).as_posix()
        for d in (shape.SHARE_DIR, shape.OTHER_IGNORED_DIR) for f in (repo / d).iterdir() if f.is_file()
    ]
    by_prefix = machine_local_paths(rels)
    assert 0 < len(by_prefix) < len(rels)
    assert set(shape.ignored_among(repo, [p for p in rels if p not in set(by_prefix)])) == set(rels) - set(by_prefix)


def test_the_driver_reads_no_os_times():
    source = Path(shape.__file__).read_text(encoding="utf-8")
    assert not [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute) and n.attr == "times"
                and isinstance(n.value, ast.Name) and n.value.id == "os"]


@_PENDING
def test_no_call_is_handed_a_gitignored_or_machine_local_path_and_the_follow_on_lands_the_landable(leg):
    repo, report = leg
    handed = report.handed_paths
    assert machine_local_paths(handed) == [], machine_local_paths(handed)[:3]
    ignored = shape.ignored_among(repo, handed)
    assert ignored == [], f"{len(ignored)} gitignored paths handed, first {ignored[:3]}"
    follow_on = report.reply.get("own_left_behind_commit") or {}
    assert sorted(follow_on.get("paths", [])) == sorted(LANDABLE_PATHS), follow_on


@_PENDING
def test_the_leg_sums_under_the_brightline(leg):
    _, report = leg
    detail = [(len(c.paths), c.process_time_ms, c.spawns) for c in report.calls]
    assert report.process_time_ms < LEG_BAR_MS, f"{report.process_time_ms}ms over {detail}"


@_PENDING
def test_the_leg_spawns_stay_within_the_bound(leg):
    _, report = leg
    detail = [(len(c.paths), c.process_time_ms, c.spawns) for c in report.calls]
    assert report.spawns <= LEG_SPAWN_BOUND, f"{report.spawns} spawns over {detail}"
