"""Two mutating sites fail CLOSED under a warm dispatch that carried no
identity (AC7 and AC8, docs/plans/2026-08-30-the-c-door-sends-the-callers-
session-identity.md).

THE ASYMMETRY THESE TESTS ENCODE. `resolve_session_id`'s degrade-to-env on an
absent tier 0 is the documented fail-safe FOR A READ -- "degrade to the
server's pre-existing behaviour, not a broken one". It is exactly wrong for a
WRITE. Committing, claiming or registering under whichever session happened to
start the engine is not a degrade, it is a silent misattribution of somebody
else's work, and it is self-consistent, so nothing downstream can spot it.

The two sites here are the two the plan named, and they fail in different
ways, which is why one test would not do:

  AC7 -- `session.liveness.claim_held_by_me` is the handoff-claim MUTEX. Its
  `True` means "you already hold this, proceed". Inside one warm server every
  served session resolves the SAME ambient id, so three sessions dialling the
  same claim would each be told they hold it and the mutex would admit all
  three while looking correct to every one of them. A mutex that grants on a
  stranger's id is not degraded, it is absent.

  AC8 -- `ipc._record_self_reported_touches` MINTS. The id it picks names the
  session dir a touch record is written into, so a wrong id does not mislabel
  a read; it creates an entry under a session that did not do the work and
  leaves the session that did with none.

WHY THE FLAG AND NOT JUST AN EMPTY CARRY. A warm request whose door sent no
`_session_id` leaves the identity ContextVar unset, which is BYTE-IDENTICAL to
a cold invocation -- and the two need opposite answers, because cold
`os.environ` really is the caller's own. `in_warm_served_request()` is the only
thing that separates them, which is why every test below pins the cold arm
alongside the warm one: a fix that closes the warm hole by silencing cold has
traded one silent attribution failure for another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.session import core as session_core
from coordinator_core.session import liveness as session_liveness

_SERVER_OWNER = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
_CALLER = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


@pytest.fixture
def ambient_is_the_server_owner(monkeypatch):
    for var in session_core.SESSION_ENV_PRECEDENCE:
        monkeypatch.setenv(var, _SERVER_OWNER)
    return _SERVER_OWNER


def _claim_dir(tmp_path: Path, holder: str) -> str:
    d = tmp_path / "claims" / "some-artifact"
    d.mkdir(parents=True, exist_ok=True)
    (d / "session_id").write_text(holder, encoding="utf-8")
    return str(d)


def test_cold_call_still_recognises_its_own_claim(tmp_path, ambient_is_the_server_owner):
    claim = _claim_dir(tmp_path, _SERVER_OWNER)
    assert session_liveness.claim_held_by_me(claim) is True


def test_warm_call_with_no_carried_identity_does_not_assert_held_by_self(
    tmp_path, ambient_is_the_server_owner
):
    claim = _claim_dir(tmp_path, _SERVER_OWNER)
    with session_core.warm_served_request():
        held = session_liveness.claim_held_by_me(claim)
    assert held is False, (
        "the claim mutex granted on the ambient identity inside a warm "
        "dispatch that carried none -- every session this server serves would "
        "be told it holds this claim"
    )


def test_three_warm_sessions_do_not_all_hold_the_same_claim(
    tmp_path, ambient_is_the_server_owner
):
    claim = _claim_dir(tmp_path, _SERVER_OWNER)
    verdicts = []
    with session_core.warm_served_request():
        with session_core.session_identity_override(_CALLER):
            verdicts.append(session_liveness.claim_held_by_me(claim))
        verdicts.append(session_liveness.claim_held_by_me(claim))
        with session_core.session_identity_override(_SERVER_OWNER):
            verdicts.append(session_liveness.claim_held_by_me(claim))
    assert verdicts == [False, False, True], (
        "at most the session that actually carried the holder's id may hold "
        f"this claim; got {verdicts} (pre-fix this was [False, True, True] -- "
        "the middle session carried nothing and inherited the answer)"
    )


def test_warm_call_recognises_the_claim_it_actually_carried(
    tmp_path, ambient_is_the_server_owner
):
    claim = _claim_dir(tmp_path, _CALLER)
    with session_core.warm_served_request():
        with session_core.session_identity_override(_CALLER):
            assert session_liveness.claim_held_by_me(claim) is True


def test_an_explicit_my_sid_is_still_honoured_under_warm(
    tmp_path, ambient_is_the_server_owner
):
    claim = _claim_dir(tmp_path, _CALLER)
    with session_core.warm_served_request():
        assert session_liveness.claim_held_by_me(claim, my_sid=_CALLER) is True


def test_the_warm_flag_unwinds_so_a_later_cold_claim_check_is_unaffected(
    tmp_path, ambient_is_the_server_owner
):
    claim = _claim_dir(tmp_path, _SERVER_OWNER)
    with session_core.warm_served_request():
        pass
    assert session_core.in_warm_served_request() is False
    assert session_liveness.claim_held_by_me(claim) is True


def _pickup_claim_dir(repo: Path, class_: str, basename: str, holder: str) -> Path:
    d = repo / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    d.mkdir(parents=True, exist_ok=True)
    (d / "session_id").write_text(holder, encoding="utf-8")
    (d / "stage").write_text("apply" + chr(10), encoding="utf-8")
    return d


def test_compute_claim_grant_honours_the_identity_its_caller_scoped(
    tmp_path, ambient_is_the_server_owner
):
    import coordinator_core.pickup_brief as pa
    from coordinator_core.contract import apply_base

    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    _pickup_claim_dir(repo, "handoff", "h1.md", _CALLER)

    with session_core.warm_served_request():
        with apply_base.session_identity(_CALLER):
            grant = pa.compute_claim_grant(
                repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
            )

    assert grant["held_by_self"] is True, (
        "a session that resolved its own identity and scoped it was refused "
        "its own claim -- `pickup-assemble apply --session-id <mine>` "
        "self-denies on exactly this path"
    )
    assert grant["verdict"] == "granted"


def test_claim_already_self_held_honours_the_identity_its_caller_scoped(
    tmp_path, ambient_is_the_server_owner
):
    import coordinator_core.pickup_brief as pa
    from coordinator_core.contract import apply_base

    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _pickup_claim_dir(repo, "memo", "m1.md", _CALLER)

    with session_core.warm_served_request():
        with apply_base.session_identity(_CALLER):
            held = pa._claim_already_self_held(repo, "memo", "m1.md")

    assert held is True


def test_pickup_callers_still_fail_closed_with_nothing_scoped(
    tmp_path, ambient_is_the_server_owner
):
    import coordinator_core.pickup_brief as pa

    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    _pickup_claim_dir(repo, "handoff", "h1.md", _SERVER_OWNER)
    _pickup_claim_dir(repo, "memo", "m1.md", _SERVER_OWNER)

    with session_core.warm_served_request():
        grant = pa.compute_claim_grant(
            repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
        )
        held = pa._claim_already_self_held(repo, "memo", "m1.md")

    assert grant["held_by_self"] is False
    assert held is False


def test_cold_pickup_callers_still_recognise_their_own_claim(
    tmp_path, ambient_is_the_server_owner
):
    import coordinator_core.pickup_brief as pa

    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    _pickup_claim_dir(repo, "handoff", "h1.md", _SERVER_OWNER)
    _pickup_claim_dir(repo, "memo", "m1.md", _SERVER_OWNER)

    grant = pa.compute_claim_grant(
        repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
    )
    assert grant["held_by_self"] is True
    assert pa._claim_already_self_held(repo, "memo", "m1.md") is True


def _record_touches(result: dict, cwd: str):
    from coordinator_core import ipc

    return ipc._record_self_reported_touches(result, cwd)


def _declaring_result(path: Path) -> dict:
    from coordinator_core import ipc

    return {"ok": True, ipc._SCOPE_TOUCH_PATHS_KEY: [str(path)]}


def _session_dirs_under(repo: Path) -> set:
    base = repo / ".git" / "coordinator-sessions"
    if not base.is_dir():
        return set()
    return {p.name for p in base.iterdir() if p.is_dir()}


@pytest.fixture
def repo_with_a_touchable_file(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    touched = repo / "some_file.py"
    touched.write_text("x = 1\n", encoding="utf-8")
    return repo, touched


def test_warm_dispatch_with_no_carried_identity_mints_no_session_entry(
    repo_with_a_touchable_file, ambient_is_the_server_owner
):
    repo, touched = repo_with_a_touchable_file
    before = _session_dirs_under(repo)
    with session_core.warm_served_request():
        out = _record_touches(_declaring_result(touched), str(repo))
    assert isinstance(out, dict) and out.get("ok") is True, (
        "declining to mint must not fail the op"
    )
    assert _session_dirs_under(repo) - before == set(), (
        "a warm-served call that carried no identity minted a session-registry "
        f"entry from the ambient environment ({ambient_is_the_server_owner})"
    )


def _delivery_message():
    from coordinator_core.ops.tracker import push_suggestion

    return push_suggestion._delivery_commit_message("some/event.json")


def test_cold_delivery_commit_still_carries_the_ambient_session_id(
    ambient_is_the_server_owner,
):
    assert f"Session-Id: {ambient_is_the_server_owner}" in _delivery_message()


def test_warm_delivery_commit_with_no_carried_identity_omits_session_id(
    ambient_is_the_server_owner,
):
    with session_core.warm_served_request():
        msg = _delivery_message()
    assert "Session-Id:" not in msg, (
        "a warm-served cross-repo delivery stamped an identity nothing carried "
        f"-- the value is the server owner's ({ambient_is_the_server_owner}), "
        "landing in a repo that cannot tell it from a genuine one"
    )
    assert ambient_is_the_server_owner not in msg


def test_warm_delivery_commit_carries_the_identity_it_was_given(
    ambient_is_the_server_owner,
):
    with session_core.warm_served_request():
        with session_core.session_identity_override(_CALLER):
            msg = _delivery_message()
    assert f"Session-Id: {_CALLER}" in msg
    assert ambient_is_the_server_owner not in msg


def test_warm_dispatch_records_under_the_carried_identity(
    repo_with_a_touchable_file, ambient_is_the_server_owner
):
    repo, touched = repo_with_a_touchable_file
    with session_core.warm_served_request():
        with session_core.session_identity_override(_CALLER):
            _record_touches(_declaring_result(touched), str(repo))
    minted = _session_dirs_under(repo)
    assert _SERVER_OWNER not in minted, (
        "recorded under the server owner despite the caller carrying its own id"
    )
