
from __future__ import annotations

import pytest

from coordinator_core import conftest as cc_conftest
from coordinator_core.session_hub_path import hub_for_common


def test_a_fixture_named_dir_created_during_a_test_fails_loudly(tmp_path, monkeypatch):
    hub = hub_for_common(tmp_path)
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    (hub / "sess-abc").mkdir()
    with pytest.raises(AssertionError, match="sess-abc"):
        try:
            next(gen)
        except StopIteration:
            pass


def test_a_uuid_shaped_dir_is_never_flagged(tmp_path, monkeypatch):
    hub = hub_for_common(tmp_path)
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    (hub / "3f2a1c9e-0000-4000-8000-0000000000aa").mkdir()
    with pytest.raises(StopIteration):
        next(gen)


def test_a_registered_harness_session_is_never_flagged(tmp_path, monkeypatch):
    from coordinator_core.session import harness_registry

    hub = hub_for_common(tmp_path)
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setattr(harness_registry, "snapshot", lambda: {"not-a-uuid-peer": object()})
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    (hub / "not-a-uuid-peer").mkdir()
    with pytest.raises(StopIteration):
        next(gen)


def test_an_absent_hub_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(tmp_path / "nope"))
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)


def _mint(hub, name, stable_pid):
    import json

    d = hub / name
    d.mkdir()
    if stable_pid is not None:
        (d / "meta.json").write_text(json.dumps({"stable_pid": stable_pid}), encoding="utf-8")
    return d


def test_a_peer_sessions_dir_is_not_our_leak(tmp_path, monkeypatch):
    hub = hub_for_common(tmp_path)
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setenv("CLAUDE_PID", "20204")
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    _mint(hub, "c7-cold-fwd-probe", "19100")
    with pytest.raises(StopIteration):
        next(gen)


def test_our_own_sessions_dir_is_still_our_leak(tmp_path, monkeypatch):
    hub = hub_for_common(tmp_path)
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setenv("CLAUDE_PID", "20204")
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    _mint(hub, "sess-abc", "20204")
    with pytest.raises(AssertionError, match="sess-abc"):
        try:
            next(gen)
        except StopIteration:
            pass


def test_a_dir_with_no_meta_json_is_still_flagged(tmp_path, monkeypatch):
    hub = hub_for_common(tmp_path)
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setenv("CLAUDE_PID", "20204")
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    _mint(hub, "altlive-probe", None)
    with pytest.raises(AssertionError, match="altlive-probe"):
        try:
            next(gen)
        except StopIteration:
            pass


@pytest.mark.parametrize("meta_body", ["{ not json", "{}", '{"stable_pid": ""}'])
def test_an_unusable_stamp_fails_closed(tmp_path, monkeypatch, meta_body):
    hub = hub_for_common(tmp_path)
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setenv("CLAUDE_PID", "20204")
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    d = hub / "sess-1"
    d.mkdir()
    (d / "meta.json").write_text(meta_body, encoding="utf-8")
    with pytest.raises(AssertionError, match="sess-1"):
        try:
            next(gen)
        except StopIteration:
            pass


class _FakeRequest:
    def __init__(self, nodeid):
        self.node = type("N", (), {"nodeid": nodeid})()


def test_append_guard_names_the_test_that_wrote_the_entry(tmp_path, monkeypatch):
    hub = hub_for_common(tmp_path)
    (hub / "test-c8-litter").mkdir(parents=True)
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setattr(cc_conftest, "_hub_entry_writers", {})
    monkeypatch.setattr(cc_conftest, "_hub_written", set())

    with pytest.raises(pytest.fail.Exception) as excinfo:
        with cc_conftest._watch_preexisting_hub_litter(str(hub)):
            writer = cc_conftest._no_writes_into_live_hub_entries.__wrapped__(
                _FakeRequest("tests/test_writer.py::test_the_writer")
            )
            next(writer)
            (hub / "test-c8-litter" / "em-turns.json").write_text("{}", encoding="utf-8")
            with pytest.raises(AssertionError, match="test-c8-litter"):
                next(writer)
            # A later, innocent test is tearing down when the session guard fires.
            cc_conftest._record_hub_entry_writers([], "tests/test_later.py::test_innocent")

    message = str(excinfo.value)
    assert "test-c8-litter <- tests/test_writer.py::test_the_writer" in message
    assert "test_innocent" not in message


def test_append_guard_says_so_when_no_writer_was_captured(tmp_path, monkeypatch):
    hub = hub_for_common(tmp_path)
    (hub / "test-c8-litter").mkdir(parents=True)
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.setattr(cc_conftest, "_hub_entry_writers", {})

    with pytest.raises(pytest.fail.Exception, match="writer not captured"):
        with cc_conftest._watch_preexisting_hub_litter(str(hub)):
            (hub / "test-c8-litter" / "x.json").write_text("{}", encoding="utf-8")


def test_no_claude_pid_in_our_env_fails_closed(tmp_path, monkeypatch):
    """Without our own `CLAUDE_PID` there is nothing to compare against, so the
    guard must behave exactly as it did before this exemption existed rather
    than exempting everything."""
    hub = hub_for_common(tmp_path)
    hub.mkdir()
    monkeypatch.setattr(cc_conftest, "_LIVE_HUB", str(hub))
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    gen = cc_conftest._no_new_live_session_hub_entries.__wrapped__()
    next(gen)
    _mint(hub, "c7-cold-fwd-probe", "19100")
    with pytest.raises(AssertionError, match="c7-cold-fwd-probe"):
        try:
            next(gen)
        except StopIteration:
            pass
