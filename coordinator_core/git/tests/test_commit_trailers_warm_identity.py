"""The trailer applier omits Session-Id under a warm dispatch that carried
no identity, instead of degrading to the server's own environment.

The defect this pins (state/bug-backlog/2026-08-29-the-warm-door-s-exe-route-
stamps-the-ser-47373b19c77e.yaml, P0, measured independently by three sessions
in three repos): a CLI served in-process by the resident warm server resolved
identity from the SERVER's `os.environ` rather than the caller's, so commits,
claims and memo stamps were attributed to whichever session happened to start
the server. 71 commits in the 2026-08-29 window carry one such id and 32 carry
another; neither set is correctable after the fact, which is why the fix has to
make a wrong trailer impossible rather than merely rarer.

NEGATIVE SPEC — what these tests exist to stop coming back:
  * A warm-served commit that carried no `_session_id` must emit NO
    `Session-Id:` trailer. It must never emit the ambient one. An absent
    trailer is recoverable; a confidently wrong one is what made the window
    unusable as an attribution key, because no reader can tell it from a
    genuine one.
  * Cold behaviour must not move. `os.environ` in a cold process IS the
    caller's own, so the environment tiers are correct there, and a fix that
    silences cold trailers to close the warm hole has traded one silent
    attribution failure for another.
  * `carried_session_id()` must stay the tier-0-only accessor. If the warm
    branch ever falls back to `resolve_session_id()` on an empty carry, this
    file's warm test is the one that goes red.
"""

import re

import pytest

from coordinator_core.git import commit_trailers
from coordinator_core.session import core as session_core

_SERVER_OWNER = "0dcf3da2-1c32-423f-9376-63ebde4b947c"
_CALLER = "8b40d62c-55ef-4702-83ce-0cd8dc6513e3"


@pytest.fixture
def ambient_is_the_server_owner(monkeypatch):
    """Put a foreign id in every environment tier the resolver reads.

    Mirrors the measured shape rather than inventing one: inside the resident
    server these vars held the id of the session that spawned it, which is how
    a caller's own correct records came back signed by a stranger.
    """
    for var in session_core.SESSION_ENV_PRECEDENCE:
        monkeypatch.setenv(var, _SERVER_OWNER)
    return _SERVER_OWNER


def _session_ids_in(message: str):
    return re.findall(r"^Session-Id: (.+)$", message, flags=re.MULTILINE)


def test_cold_call_still_stamps_the_ambient_identity(tmp_path, ambient_is_the_server_owner):
    """Cold is the case where the environment IS the caller — do not break it."""
    out = commit_trailers.apply_missing_trailers("a cold commit\n", tmp_path)
    assert _session_ids_in(out) == [ambient_is_the_server_owner]


def test_warm_call_with_no_carried_identity_omits_rather_than_guessing(
    tmp_path, ambient_is_the_server_owner
):
    """The whole defect, in one assertion.

    An old door sends no `_session_id`, so the identity ContextVar is unbound
    and the carry is empty — byte-identical to the cold case above, and the
    reason the warm flag has to be its own axis. The ambient id is RIGHT THERE
    and readable; the point is that it must not be read.
    """
    with session_core.warm_served_request():
        out = commit_trailers.apply_missing_trailers("a warm-served commit\n", tmp_path)
    assert _session_ids_in(out) == [], (
        "warm-served commit stamped an identity nothing carried — this is the "
        "2026-08-29 defect, and the stamped value is the server owner's"
    )
    assert ambient_is_the_server_owner not in out


def test_warm_call_stamps_the_carried_identity_not_the_ambient_one(
    tmp_path, ambient_is_the_server_owner
):
    """A door that DOES send the field: the caller's id wins over the server's."""
    with session_core.warm_served_request():
        with session_core.session_identity_override(_CALLER):
            out = commit_trailers.apply_missing_trailers("a warm commit\n", tmp_path)
    assert _session_ids_in(out) == [_CALLER]
    assert ambient_is_the_server_owner not in out


def test_the_warm_flag_unwinds_so_a_later_cold_call_is_unaffected(
    tmp_path, ambient_is_the_server_owner
):
    """Token/reset scoping, asserted rather than assumed.

    A leaked flag would silence every subsequent commit in a long-lived
    process — the same class of silent, self-consistent failure as the defect,
    with the sign flipped.
    """
    with session_core.warm_served_request():
        pass
    assert session_core.in_warm_served_request() is False
    out = commit_trailers.apply_missing_trailers("after the warm block\n", tmp_path)
    assert _session_ids_in(out) == [ambient_is_the_server_owner]
