
from __future__ import annotations

import tempfile

import pytest

from coordinator_core.session import guard_unlock_sentinel as gus
from coordinator_core.write_guards import engine


SENTINEL_BASENAME = ".coordinator-override-worktree-guard"
GUARD_NAME = "block_worktree_sentinel_write"
ADVISORY_SENTINEL_BASENAME = ".coordinator-dev-repo"
ADVISORY_GUARD_NAME = "block_dev_repo_sentinel_write"


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    yield


def _payload(session_id, tool_name="Write", file_path=None):
    p = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path or "/repo/%s" % SENTINEL_BASENAME},
    }
    if session_id is not None:
        p["session_id"] = session_id
    return p


def _is_deny(out):
    if out is None:
        return False
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestAbsentSentinelDenies:

    def test_no_sentinel_denies(self):
        out = engine.evaluate(_payload("sess-1"))
        assert _is_deny(out)


class TestPresentSentinelGrantsOnce:
    def test_sentinel_allows(self):
        gus.sentinel_path("sess-1", GUARD_NAME).write_text("", encoding="utf-8")
        out = engine.evaluate(_payload("sess-1"))
        assert not _is_deny(out)

    def test_one_shot_re_denies_on_immediate_retry(self):
        gus.sentinel_path("sess-1", GUARD_NAME).write_text("", encoding="utf-8")
        first = engine.evaluate(_payload("sess-1"))
        assert not _is_deny(first)
        second = engine.evaluate(_payload("sess-1"))
        assert _is_deny(second)

    def test_sentinel_file_is_consumed_from_disk(self):
        p = gus.sentinel_path("sess-1", GUARD_NAME)
        p.write_text("", encoding="utf-8")
        engine.evaluate(_payload("sess-1"))
        assert not p.exists()


class TestPerGuardIsolation:
    def test_sentinel_for_a_different_guard_does_not_clear_this_one(self):
        gus.sentinel_path("sess-1", "block_priority_ledger_edit").write_text("", encoding="utf-8")
        out = engine.evaluate(_payload("sess-1"))
        assert _is_deny(out)


class TestPerSessionIsolation:
    def test_peer_session_sentinel_does_not_clear_ours(self):
        gus.sentinel_path("peer-session", GUARD_NAME).write_text("", encoding="utf-8")
        out = engine.evaluate(_payload("sess-1"))
        assert _is_deny(out)


class TestUnresolvableSessionIdFailsClosed:
    def test_missing_session_id_denies_even_with_a_sentinel_on_disk(self):
        gus.sentinel_path("", GUARD_NAME).write_text("", encoding="utf-8")
        out = engine.evaluate(_payload(None))
        assert _is_deny(out)

    def test_empty_string_session_id_denies(self):
        gus.sentinel_path("", GUARD_NAME).write_text("", encoding="utf-8")
        out = engine.evaluate(_payload(""))
        assert _is_deny(out)


class TestAdvisoryGuardNeverConsultsUnlock:

    def _advisory_payload(self, session_id):
        return _payload(
            session_id,
            file_path="/repo/%s" % ADVISORY_SENTINEL_BASENAME,
        )

    def test_fires_as_advisory_regardless_of_sentinel(self):
        out = engine.evaluate(self._advisory_payload("sess-1"))
        assert not _is_deny(out)
        assert out is not None
        assert (
            out.get("hookSpecificOutput", {}).get("permissionDecision")
            is None
        )

    def test_unlock_sentinel_for_the_advisory_guard_is_left_untouched(self):
        p = gus.sentinel_path("sess-1", ADVISORY_GUARD_NAME)
        p.write_text("", encoding="utf-8")
        engine.evaluate(self._advisory_payload("sess-1"))
        assert p.exists()
