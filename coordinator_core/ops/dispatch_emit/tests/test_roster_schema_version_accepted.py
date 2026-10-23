"""Pins that compose_script and compose_grind_script accept review roster
schema_version 5 and 6 and refuse any other version."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.emit import (
    ACCEPTED_ROSTER_SCHEMA_VERSIONS,
    NoReviewStageError,
    compose_script,
)

from .conftest import REVIEW_KW
from .test_emit_wake_digest import _row
from .test_grind_compose import _FIXTURE_PROFILE_DIR, _fixture_manifest


def _fragment(version: int) -> dict:
    fragment = copy.deepcopy(REVIEW_KW["review_roster_fragment"])
    fragment["schema_version"] = version
    return fragment


def _plan_compose(version: int) -> str:
    return compose_script(
        [[_row("C1", ["a.py"])]],
        name="wf",
        description="d",
        review_roster_fragment=_fragment(version),
        review_stage_schemas=REVIEW_KW["review_stage_schemas"],
    )


def _grind_compose(version: int) -> str:
    profile = gp.load_profile("fixture", _FIXTURE_PROFILE_DIR)
    knobs = gp.resolve_appetite(profile, "standard")
    return gc.compose_grind_script(
        _fixture_manifest(),
        profile,
        knobs,
        run_dir=Path("state/queue-grind/fixture/run-1"),
        appetite="standard",
        agent_type_host=None,
        queue_dirs=["state/bug-backlog"],
        review_roster_fragment=_fragment(version),
        review_stage_schemas=REVIEW_KW["review_stage_schemas"],
    )


def test_accepted_set_is_v5_and_v6():
    assert ACCEPTED_ROSTER_SCHEMA_VERSIONS == {5, 6}


@pytest.mark.parametrize("version", [5, 6])
@pytest.mark.parametrize("compose", [_plan_compose, _grind_compose])
def test_accepted_version_composes(compose, version):
    assert "Review prep" in compose(version)


@pytest.mark.parametrize("compose", [_plan_compose, _grind_compose])
def test_unknown_version_still_raises(compose):
    with pytest.raises(NoReviewStageError, match="schema_version 99"):
        compose(99)
