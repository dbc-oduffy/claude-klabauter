"""A stalled index-directory write never holds a claim lookup: the build runs off the caller's path."""

import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.session import claim_index, touch_record


def _claim(base: str, sid: str, path: str) -> None:
    touch_record.append_event(
        os.path.join(base, sid, "touch-record.jsonl"),
        session_id=sid,
        agent_id=None,
        verb="T",
        path=path,
        timestamp=1_780_000_000.0,
        kind=touch_record.KIND_WRITE,
        name=None,
    )


def _stamp(base: str) -> str:
    return os.path.join(touch_record.claim_index_dir(base), touch_record.CLAIM_INDEX_COMPLETE)


def _wait_builds_drained(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while claim_index._BUILDS and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.fixture
def blocked_index_writes(monkeypatch: pytest.MonkeyPatch):
    """Block makedirs under the index directory until released, as a stalled filesystem would."""
    release = threading.Event()
    real = os.makedirs

    def _makedirs(name, *args, **kwargs):
        if "-claim-index" in str(name):
            release.wait(10)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", _makedirs)
    monkeypatch.setattr(claim_index, "INDEX_BUILD_WAIT_SECS", 0.05)
    yield release
    release.set()
    _wait_builds_drained()


@pytest.mark.deliberate_wall_clock(reason="bounds the lookup's wait on an index write blocked for seconds")
def test_lookup_answers_from_its_walk_while_the_index_build_is_blocked(
    tmp_path: Path, blocked_index_writes
) -> None:
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")

    start = time.process_time()
    result = claim_index.lookup(["a.py"], sessions_dir=base)
    cpu = time.process_time() - start

    assert result == {"a.py": ["peer"]} and result.complete
    assert cpu < 0.5
    assert not os.path.exists(_stamp(base))


def test_a_lookup_during_an_in_flight_build_starts_no_second_build(
    tmp_path: Path, blocked_index_writes, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")
    begins = []
    real_begin = claim_index._begin_index
    monkeypatch.setattr(
        claim_index, "_begin_index", lambda *a, **k: begins.append(1) or real_begin(*a, **k)
    )

    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}
    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}

    assert len(begins) == 1


def test_the_next_unblocked_lookup_builds_and_then_serves_the_index(
    tmp_path: Path, blocked_index_writes
) -> None:
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")
    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}
    assert not os.path.exists(_stamp(base))

    blocked_index_writes.set()
    _wait_builds_drained()
    assert os.path.isfile(_stamp(base))

    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}


def test_an_unblocked_lookup_builds_before_it_returns(tmp_path: Path) -> None:
    base = str(tmp_path / "sessions")
    _claim(base, "peer", "a.py")

    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}
    _wait_builds_drained()

    assert os.path.isfile(_stamp(base))
