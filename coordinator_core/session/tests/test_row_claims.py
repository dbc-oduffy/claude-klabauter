"""Queue-row claim class: key helper, blocker predicate, claim/release/clear arms."""

from __future__ import annotations

import pytest

from coordinator_core.session import claims, core, liveness
from coordinator_core.tests.git_seed import seeded_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BUG_ROW = "state/bug-backlog/2026-10-14-login-next-cap.yaml"
DEBT_ROW = "state/debt-backlog/2026-10-14-login-next-cap.yaml"
BUG_KEY = "bug-backlog--2026-10-14-login-next-cap"


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = seeded_repo(tmp_path, readme="x")
    live: set = set()
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in live)
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)

    class Env:
        root = str(repo)
        live_set = live

        @staticmethod
        def as_session(sid):
            monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
            live.add(sid)

        @staticmethod
        def claim(row):
            return claims.claim_artifact("row", row, baton_repo_root=str(repo), cwd=str(repo))

        @staticmethod
        def release(row):
            return claims.release_artifact("row", row, baton_repo_root=str(repo), cwd=str(repo))

        @staticmethod
        def clear(row):
            return claims.clear_claim_if_dead("row", row, baton_repo_root=str(repo), cwd=str(repo))

        sessions_dir = repo / ".git" / "coordinator-sessions"

        @staticmethod
        def claim_dir(key=BUG_KEY):
            return repo / ".git" / "coordinator-sessions" / "row-claims" / key

    return Env


@pytest.mark.parametrize("row", [BUG_ROW, DEBT_ROW])
def test_second_session_claim_on_live_row_is_refused_and_names_holder(env, row, capsys):
    env.as_session("sid-a")
    assert env.claim(row) is True
    env.as_session("sid-b")
    capsys.readouterr()
    assert env.claim(row) is False
    err = capsys.readouterr().err
    assert "sid-a" in err and "release-artifact row" in err


def test_dead_holder_claim_is_taken_over(env):
    env.as_session("sid-a")
    assert env.claim(BUG_ROW)
    env.live_set.discard("sid-a")
    env.as_session("sid-b")
    assert env.claim(BUG_ROW) is True
    assert (env.claim_dir() / "session_id").read_text().strip() == "sid-b"


def test_well_formed_claim_with_dead_pid_is_taken_over(env):
    cdir = env.claim_dir()
    cdir.mkdir(parents=True)
    (cdir / "session_id").write_text("sid-ghost\n")
    (cdir / "pid").write_text("999999\n")
    (cdir / "claimed_at").write_text(f"{core.now_iso()}\n")
    (cdir / "stage").write_text("brief\n")
    env.as_session("sid-b")
    assert env.claim(BUG_ROW) is True
    assert (cdir / "session_id").read_text().strip() == "sid-b"


def test_expired_lease_is_taken_over_while_holder_reads_live(env):
    env.as_session("sid-a")
    assert env.claim(BUG_ROW)
    (env.claim_dir() / "claimed_at").write_text("2000-01-01T00:00:00Z\n")
    env.as_session("sid-b")
    assert "sid-a" in env.live_set
    assert env.claim(BUG_ROW) is True
    assert (env.claim_dir() / "session_id").read_text().strip() == "sid-b"


def test_same_session_reclaim_succeeds_and_refreshes_claimed_at(env):
    env.as_session("sid-a")
    assert env.claim(BUG_ROW)
    stamp = env.claim_dir() / "claimed_at"
    stamp.write_text("2000-01-01T00:00:00Z\n")
    assert env.claim(BUG_ROW) is True
    assert stamp.read_text().strip() != "2000-01-01T00:00:00Z"
    assert claims.claim_stage(env.claim_dir()) == claims.CLAIM_STAGE_BRIEF


def test_row_claim_is_always_brief_stage(env):
    env.as_session("sid-a")
    assert claims.claim_artifact(
        "row", BUG_ROW, baton_repo_root=env.root, cwd=env.root, stage=claims.CLAIM_STAGE_APPLY
    )
    assert claims.claim_stage(env.claim_dir()) == claims.CLAIM_STAGE_BRIEF


def test_keys_differ_across_queue_dirs_and_agree_across_separators():
    assert claims.row_claim_key(BUG_ROW) != claims.row_claim_key(DEBT_ROW)
    assert claims.row_claim_key(BUG_ROW) == BUG_KEY
    assert claims.row_claim_key(BUG_ROW.replace("/", "\\")) == BUG_KEY
    assert claims.row_claim_key("repo\\" + BUG_ROW.replace("/", "\\")) == BUG_KEY


@pytest.mark.parametrize("bad", ["r.yaml", "state/bug-backlog/r.md", "plain", "./r.yaml"])
def test_key_rejects_non_row_shapes(bad):
    with pytest.raises(ValueError):
        claims.row_claim_key(bad)


def test_release_by_non_holder_is_noop_and_holder_release_removes(env):
    env.as_session("sid-a")
    assert env.claim(BUG_ROW)
    env.as_session("sid-b")
    assert env.release(BUG_ROW) is True
    assert env.claim_dir().is_dir()
    env.as_session("sid-a")
    assert env.release(BUG_ROW) is True
    assert not env.claim_dir().exists()


def test_clear_refuses_live_holder_and_clears_dead_one(env):
    env.as_session("sid-a")
    assert env.claim(BUG_ROW)
    env.as_session("sid-b")
    assert env.clear(BUG_ROW) is False
    assert env.claim_dir().is_dir()
    env.live_set.discard("sid-a")
    assert env.clear(BUG_ROW) is True
    assert not env.claim_dir().exists()


class TestRowClaimBlocker:
    def test_none_without_claim(self, env):
        assert claims.row_claim_blocker(env.sessions_dir, BUG_KEY, "sid-b") is None

    def test_none_for_my_own_claim(self, env):
        env.as_session("sid-a")
        env.claim(BUG_ROW)
        assert claims.row_claim_blocker(env.sessions_dir, BUG_KEY, "sid-a") is None

    def test_holder_for_live_peer_claim(self, env):
        env.as_session("sid-a")
        env.claim(BUG_ROW)
        assert claims.row_claim_blocker(env.sessions_dir, BUG_KEY, "sid-b") == "sid-a"
        assert claims.row_claim_blocker(env.sessions_dir, BUG_KEY, None) == "sid-a"

    def test_none_for_dead_holder(self, env):
        env.as_session("sid-a")
        env.claim(BUG_ROW)
        env.live_set.discard("sid-a")
        assert claims.row_claim_blocker(env.sessions_dir, BUG_KEY, "sid-b") is None

    def test_none_for_expired_lease(self, env):
        env.as_session("sid-a")
        env.claim(BUG_ROW)
        (env.claim_dir() / "claimed_at").write_text("2000-01-01T00:00:00Z\n")
        assert claims.row_claim_blocker(env.sessions_dir, BUG_KEY, "sid-b") is None

    def test_none_when_my_other_env_tier_holds_it(self, env, monkeypatch):
        env.as_session("sid-a")
        env.claim(BUG_ROW)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-a-alt")
        assert claims.row_claim_blocker(env.sessions_dir, BUG_KEY, "sid-a-alt") is None


def test_row_claims_dir_is_registered_in_every_enumeration():
    from coordinator_core.ops.fleet._common import _CLAIM_SUBDIRS

    assert _CLAIM_SUBDIRS[2] == "plan-claims"
    assert "row-claims" in _CLAIM_SUBDIRS
    assert "row-claims" in liveness._NON_SESSION_DIR_NAMES
