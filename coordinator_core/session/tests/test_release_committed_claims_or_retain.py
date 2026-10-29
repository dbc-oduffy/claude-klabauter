"""`scope.release_committed_claims_or_retain` carries the two post-commit
release rules every commit route inherits: skip on a falsy sid, and never
let a release failure escape."""

from __future__ import annotations

import logging

import pytest

from coordinator_core.session import scope


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        scope,
        "release_committed_claims",
        lambda sid, paths, cwd=None: seen.append((sid, paths, cwd)),
    )
    return seen


def test_releases_under_the_given_sid(calls, tmp_path):
    scope.release_committed_claims_or_retain(tmp_path, iter(["a.md", "b.md"]), "s1", "x")
    assert calls == [("s1", ["a.md", "b.md"], str(tmp_path))]


@pytest.mark.parametrize("sid", [None, ""])
def test_falsy_sid_skips_without_releasing(calls, tmp_path, sid):
    scope.release_committed_claims_or_retain(tmp_path, ["a.md"], sid, "x")
    assert calls == []


def test_empty_paths_is_a_noop(calls, tmp_path):
    scope.release_committed_claims_or_retain(tmp_path, [], "s1", "x")
    assert calls == []


def test_release_failure_is_retained_and_logged(monkeypatch, tmp_path, caplog):
    def boom(sid, paths, cwd=None):
        raise RuntimeError("git gone")

    monkeypatch.setattr(scope, "release_committed_claims", boom)
    with caplog.at_level(logging.DEBUG, logger=scope.__name__):
        scope.release_committed_claims_or_retain(tmp_path, ["a.md"], "s1", "my_route")
    assert "my_route: release_committed_claims failed post-commit" in caplog.text


def _agent(base, agent_id, owner, path):
    from pathlib import Path

    from coordinator_core.session import touch_record

    adir = Path(base) / ".agents" / agent_id
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "em-session-id.txt").write_text(owner + "\n", encoding="utf-8")
    touch_record.append_event(
        adir / scope._TOUCH_RECORD_FILENAME,
        session_id=owner,
        agent_id=agent_id,
        verb=touch_record.VERB_TOUCH,
        path=path,
    )


@pytest.fixture
def agent_world(tmp_path, monkeypatch):
    from coordinator_core.session import claim_index, core

    base = str(tmp_path / "coordinator-sessions")
    monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: base)
    monkeypatch.setattr(core, "session_dir", lambda sid, cwd=None: None)
    _agent(base, "a-hit", "em", "src/x.py")
    _agent(base, "a-other-path", "em", "src/unrelated.py")
    _agent(base, "a-peer", "peer", "src/x.py")
    claim_index.lookup(["src/x.py"], sessions_dir=base, cwd=str(tmp_path))
    opened: list[str] = []
    real = scope._release_from_touch_record

    def spy(sink, *a, **k):
        opened.append(str(sink).replace("\\", "/"))
        return real(sink, *a, **k)

    monkeypatch.setattr(scope, "_release_from_touch_record", spy)
    return tmp_path, base, opened


def test_named_paths_release_opens_only_indexed_sinks(agent_world):
    from coordinator_core.session import claim_index

    tmp_path, base, opened = agent_world
    scope.release_committed_claims("em", ["src/x.py"], cwd=str(tmp_path))
    agent_sinks = [s for s in opened if "/.agents/" in s]
    assert len(agent_sinks) == 1 and "/a-hit/" in agent_sinks[0]
    assert claim_index.lookup(["src/x.py"], sessions_dir=base, cwd=str(tmp_path)) == {
        "src/x.py": ["peer"]
    }


def test_full_release_still_scans_every_owned_agent_dir(agent_world):
    tmp_path, _base, opened = agent_world
    scope.release_all_committed_claims("em", cwd=str(tmp_path))
    agent_sinks = [s for s in opened if "/.agents/" in s]
    assert {s.split("/.agents/")[1].split("/")[0] for s in agent_sinks} == {"a-hit", "a-other-path"}


def test_absent_index_falls_back_to_scan(agent_world):
    import shutil

    from coordinator_core.session import touch_record

    tmp_path, base, opened = agent_world
    shutil.rmtree(touch_record.claim_index_dir(base))
    scope.release_committed_claims("em", ["src/x.py"], cwd=str(tmp_path))
    assert any("/a-hit/" in s for s in opened)


def test_path_generator_failure_is_retained(calls, tmp_path):
    def bad_paths():
        yield "a.md"
        raise ValueError("outside root")

    scope.release_committed_claims_or_retain(tmp_path, bad_paths(), "s1", "x")
    assert calls == []
