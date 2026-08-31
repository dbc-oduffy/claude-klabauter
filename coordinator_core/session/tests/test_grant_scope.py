"""A Tier-U grant can be narrowed without touching a schema we do not own.

Purpose: the grant was all-or-nothing by construction, so the only way past
a correct denial was a wholesale disarm. This pins the narrowing form and,
just as importantly, pins that it can only ever REMOVE authority.

Negative-spec: this file asserts nothing about the grant RECORD's own
schema — that artifact is DoE-owned, its shape is pinned by
`test_tier_u_grant.py`, and the whole design point of the sidecar is that
none of those assertions change. If a test here starts needing the record's
fields, the sidecar has grown into the field it was built not to be.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.session import core
from coordinator_core.session import grant as grant_mod
from coordinator_core.session import grant_scope as gs


@pytest.fixture
def granted_session(tmp_path, monkeypatch):
    """A live session holding an ordinary, unnarrowed PM grant.

    Liveness is stubbed rather than staged: `check_tier_u_grant`'s liveness
    leg is `test_tier_u_grant.py`'s subject, and re-deriving it here would
    couple these cases to a mechanism they are not about.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sid = "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(core, "resolve_session_id", lambda cwd=None: sid)
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True)
    monkeypatch.setattr(core, "session_dir", lambda s, cwd=None: str(sdir))
    monkeypatch.setattr(core, "ensure_session", lambda s, cwd=None: str(sdir))
    monkeypatch.setattr(grant_mod.liveness, "session_live", lambda s, cwd=None: True)

    assert grant_mod.write_tier_u_grant(
        "pm", "run the suite", session_id=sid, cwd=str(repo)
    )
    return repo, sid, sdir


def test_an_unnarrowed_grant_is_unchanged(granted_session) -> None:
    """The additive property, asserted first because everything else rests
    on it: with no sidecar, behaviour is exactly today's."""
    repo, sid, _ = granted_session
    granted, record, reason = gs.check_tier_u_grant_scoped([], str(repo), session_id=sid)
    assert granted is True
    assert reason == gs.REASON_GRANTED
    assert record is not None

    granted, _, reason = gs.check_tier_u_grant_scoped(
        ["anything/at/all"], str(repo), session_id=sid
    )
    assert granted is True
    assert reason == gs.REASON_GRANTED


def test_a_narrowed_grant_admits_only_its_prefixes(granted_session) -> None:
    repo, sid, _ = granted_session
    assert gs.write_tier_u_grant_scope(
        ["coordinator_core/session"], "only the session package",
        session_id=sid, cwd=str(repo),
    )

    granted, _, reason = gs.check_tier_u_grant_scoped(
        ["coordinator_core/session/tests/test_grant.py"], str(repo), session_id=sid
    )
    assert granted is True and reason == gs.REASON_GRANTED

    granted, _, reason = gs.check_tier_u_grant_scoped(
        ["coordinator_core/bash_guards"], str(repo), session_id=sid
    )
    assert granted is False and reason == gs.REASON_OUT_OF_SCOPE


def test_a_narrowed_grant_refuses_the_whole_suite_run(granted_session) -> None:
    """The entire point of the form. A live grant plus a narrowing must NOT
    authorize an unscoped invocation — one that authorized it would narrow
    nothing."""
    repo, sid, _ = granted_session
    assert gs.write_tier_u_grant_scope(
        ["coordinator_core/session"], "only the session package",
        session_id=sid, cwd=str(repo),
    )
    granted, record, reason = gs.check_tier_u_grant_scoped([], str(repo), session_id=sid)
    assert granted is False
    assert reason == gs.REASON_UNSCOPED_INVOCATION
    assert record is not None, "the grant is still quotable in the denial"


def test_every_positional_must_be_in_scope_not_merely_one(granted_session) -> None:
    """A mixed invocation is out of scope. Admitting it on `any()` would let
    one in-scope operand launder an arbitrary second one — the same
    laundering class `_classify_pytest` closes for selection flags."""
    repo, sid, _ = granted_session
    assert gs.write_tier_u_grant_scope(
        ["coordinator_core/session"], "narrow", session_id=sid, cwd=str(repo)
    )
    granted, _, reason = gs.check_tier_u_grant_scoped(
        ["coordinator_core/session/x.py", "coordinator_core/bash_guards/y.py"],
        str(repo), session_id=sid,
    )
    assert granted is False and reason == gs.REASON_OUT_OF_SCOPE


def test_prefix_match_is_component_wise(granted_session) -> None:
    """A scope of `coordinator` must not admit `coordinator_core` — the bug
    a bare `startswith` would ship."""
    repo, sid, _ = granted_session
    assert gs.write_tier_u_grant_scope(
        ["coordinator"], "narrow", session_id=sid, cwd=str(repo)
    )
    granted, _, _ = gs.check_tier_u_grant_scoped(
        ["coordinator_core/session/x.py"], str(repo), session_id=sid
    )
    assert granted is False

    granted, _, _ = gs.check_tier_u_grant_scoped(
        ["coordinator/tests/x.py"], str(repo), session_id=sid
    )
    assert granted is True


def test_a_scope_without_a_grant_authorizes_nothing(tmp_path, monkeypatch) -> None:
    """Narrowing nothing is not granting. This is the arm that stops the
    sidecar from being usable to manufacture authority."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sid = "99999999-8888-7777-6666-555555555555"
    monkeypatch.setattr(core, "resolve_session_id", lambda cwd=None: sid)
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True)
    monkeypatch.setattr(core, "session_dir", lambda s, cwd=None: str(sdir))
    monkeypatch.setattr(core, "ensure_session", lambda s, cwd=None: str(sdir))

    assert gs.write_tier_u_grant_scope(["a/b"], "narrow", session_id=sid, cwd=str(repo))
    granted, record, reason = gs.check_tier_u_grant_scoped(
        ["a/b/c.py"], str(repo), session_id=sid
    )
    assert granted is False
    assert reason == gs.REASON_NO_GRANT
    assert record is None


@pytest.mark.parametrize(
    "payload",
    ['{"paths": []}', '{"paths": "notalist"}', '{"paths": [""]}', "not json at all", "[]"],
    ids=["empty-list", "not-a-list", "blank-entry", "unparseable", "not-an-object"],
)
def test_a_malformed_scope_fails_closed(granted_session, payload: str) -> None:
    """A corrupted narrowing must NOT widen back to unbounded. That failure
    mode is indistinguishable from an attack and from a bug, and it fails in
    the direction that grants authority."""
    repo, sid, sdir = granted_session
    (Path(sdir) / "tier-u-grant-scope.json").write_text(payload, encoding="utf-8")
    granted, _, reason = gs.check_tier_u_grant_scoped(
        ["anything"], str(repo), session_id=sid
    )
    assert granted is False
    assert reason == gs.REASON_SCOPE_UNREADABLE


def test_revoke_restores_the_unbounded_grant_and_is_idempotent(granted_session) -> None:
    repo, sid, _ = granted_session
    assert gs.write_tier_u_grant_scope(["a/b"], "narrow", session_id=sid, cwd=str(repo))
    granted, _, _ = gs.check_tier_u_grant_scoped([], str(repo), session_id=sid)
    assert granted is False

    assert gs.revoke_tier_u_grant_scope(str(repo), session_id=sid) is True
    granted, _, reason = gs.check_tier_u_grant_scoped([], str(repo), session_id=sid)
    assert granted is True and reason == gs.REASON_GRANTED

    assert gs.revoke_tier_u_grant_scope(str(repo), session_id=sid) is True


@pytest.mark.parametrize(
    "bad",
    # abs-path-ok: these are REJECTION INPUTS, not paths this test resolves.
    # The whole assertion is that write_tier_u_grant_scope refuses them, so
    # they must stay literal absolute spellings -- one POSIX, one Windows.
    ["/abs/path", "C:/abs/path", "..", "../escape", "a/../../escape", "", "   "],
    ids=["posix-abs", "windows-abs", "dotdot", "leading-dotdot", "embedded-dotdot",
         "empty", "whitespace"],
)
def test_a_scope_cannot_name_a_target_outside_the_repo(granted_session, bad: str) -> None:
    repo, sid, _ = granted_session
    with pytest.raises(ValueError):
        gs.write_tier_u_grant_scope([bad], "narrow", session_id=sid, cwd=str(repo))


def test_an_empty_scope_list_is_a_revoke_not_a_write(granted_session) -> None:
    """Refused rather than silently written: an empty list reads as 'admits
    nothing', which is a revoke wearing a write's clothing."""
    repo, sid, _ = granted_session
    with pytest.raises(ValueError):
        gs.write_tier_u_grant_scope([], "narrow", session_id=sid, cwd=str(repo))


def test_windows_spelling_of_a_scope_matches_a_posix_positional(granted_session) -> None:
    """Windows is first-class in this repo."""
    repo, sid, _ = granted_session
    assert gs.write_tier_u_grant_scope(
        ["coordinator_core\\session"], "narrow", session_id=sid, cwd=str(repo)
    )
    granted, _, _ = gs.check_tier_u_grant_scoped(
        ["coordinator_core/session/x.py"], str(repo), session_id=sid
    )
    assert granted is True


def test_a_node_id_is_judged_on_its_path_half(granted_session) -> None:
    repo, sid, _ = granted_session
    assert gs.write_tier_u_grant_scope(
        ["coordinator_core/session"], "narrow", session_id=sid, cwd=str(repo)
    )
    granted, _, _ = gs.check_tier_u_grant_scoped(
        ["coordinator_core/session/test_x.py::test_one"], str(repo), session_id=sid
    )
    assert granted is True


def test_the_grant_record_is_untouched_by_narrowing(granted_session) -> None:
    """The boundary property: their schema-validated artifact must be
    byte-identical before and after a narrowing. If this ever fails, the
    sidecar has started writing the field it exists to avoid."""
    repo, sid, sdir = granted_session
    grant_file = Path(sdir) / "tier-u-grant.json"
    before = grant_file.read_bytes()

    assert gs.write_tier_u_grant_scope(["a/b"], "narrow", session_id=sid, cwd=str(repo))
    gs.check_tier_u_grant_scoped(["a/b/c.py"], str(repo), session_id=sid)
    gs.revoke_tier_u_grant_scope(str(repo), session_id=sid)

    assert grant_file.read_bytes() == before
    record = json.loads(before.decode("utf-8"))
    assert record["schema_version"] == 1
    assert set(record) == {
        "schema_version", "session_id", "granted_by", "granted_at", "ceremony", "note",
    }, "the DoE-owned record must gain no key -- their schema is additionalProperties:false"
