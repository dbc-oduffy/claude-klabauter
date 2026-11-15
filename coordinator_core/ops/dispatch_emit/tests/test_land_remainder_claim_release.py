"""``dispatch.land_verified_remainder`` releases the resolved session's claims on the paths it landed."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_remainder as lane
from coordinator_core.ops.dispatch_emit.tests.test_land_remainder_commit import RUN, _write, repo  # noqa: F401
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]


@pytest.fixture
def released(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(session_core, "resolve_session_id", lambda cwd=None, *a, **k: "sid-1")
    monkeypatch.setattr(
        session_scope,
        "release_committed_claims_or_retain",
        lambda root, paths, sid, label: seen.append((sorted(paths), sid, label)),
    )
    return seen


def test_landed_paths_are_released_under_the_resolved_session(repo, released) -> None:  # noqa: F811
    _write(repo, "a/one.py", "b/two.py")
    reply = lane.land(repo, {"run_id": RUN, "paths": ["a/one.py"]})
    assert reply["landed"] is True
    assert released == [(["a/one.py"], "sid-1", "land_remainder")]


def test_a_refused_landing_releases_nothing(repo, released) -> None:  # noqa: F811
    _write(repo, "a/one.py", "z/stray.py")
    reply = lane.land(repo, {"run_id": RUN, "paths": ["a/one.py", "z/stray.py"]})
    assert reply["landed"] is False
    assert released == []
