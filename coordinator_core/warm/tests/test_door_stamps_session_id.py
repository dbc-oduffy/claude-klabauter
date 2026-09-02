"""The native door tells the server which session its caller is.

Cross-repo report: `cross-repo/inbox/2026-08-29-doe-claude-em-session-identity-
resolves-three-ways-one-lands-on-your-session.md` and its addendum. A DoE session
dispatching through `coordinator-invoke.exe` had thirteen `handoff.correct_body`
writes and one `memo.send` sent-ledger receipt stamped with a LIVE claude-klabauter
session id -- and `handoff.correct_body` passed its possession gate with
`basis=author` on that stranger. Misattribution in a marker is a paper-trail
defect; a possession gate that passes because the resolver handed it a stranger's
id is an authorization one, and it is silent in both directions.

WHY THE DOOR IS WHERE THIS IS PINNED, and why the reporter's own probe could not
have caught it. `warm/client.py :: _try_warm_dispatch_inner` has stamped
`_session_id` since the caller-identity seam was built, and `warm/server.py ::
_serve_line` already pops it and binds it through `entry_seam.per_request_state`.
Only the NATIVE door was silent, so the same op resolved correctly cold (its own
process, its own env) and wrongly warm (the server's env, i.e. whoever spawned
it). That asymmetry is exactly the shape that certifies green against the route
that works, which is why the pinning here runs the SHIPPED BINARY against a real
`election.elect()` pipe rather than asserting over a Python stand-in: the frame
`door.c` builds is C string assembly no Python test can substitute for. Same
reason, same fixtures, and the same file layout as its immediate precedent one
field over, `test_door_stamps_settings_home.py`.

WHAT IS NOT ASSERTED HERE, deliberately: that a served op resolves the stamped
id. `test_server_loop.py`'s `test_run_dispatch_itself_binds_the_given_session_id`
and `test_entry_seam.py` already own the server half, and duplicating it here
would pin the same behaviour twice while proving nothing about the binary.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session.core import SESSION_ENV_PRECEDENCE
from coordinator_core.warm.tests.test_door_read_deadline import (
    _ReplyingServer,
    _door_under_default_name,
    _make_stub_engine_root,
    _pipe_name_for,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name != "nt", reason="door.exe is a Windows binary"),
]

#: C1b (docs/plans/2026-08-30-every-op-runs-in-the-callers-environment.md)
#: retired the bare envelope-level `_session_id` string in favour of ONE
#: `_caller` object whose fields are `warm.caller_context.CallerContext`
#: serialised -- with NO deprecated alias, `_serve_line` reading `_caller`
#: only (door.c's own C1b note). This module kept asserting the retired key
#: and went red the moment the rename landed; two of its assertions
#: (`_session_id not in ...`) went FALSE-GREEN instead, which is the worse
#: half. Asserted here through the object, one accessor, so a future rename
#: breaks in one place.
#: C2 folded the legacy `_caller.session_id` field into the envelope-level
#: `_env` object, keyed by whichever `SESSION_ENV_PRECEDENCE` name resolved
#: (see `test_door_stamps_declared_env_set.py`'s negative-spec block). The
#: subject this file pins -- the door stamps the session id its caller is,
#: first-non-empty-wins over `SESSION_ENV_PRECEDENCE` -- is unchanged; only
#: the field it looks in moves.
_CALLER_FIELD = "_caller"
_SESSION_ID_KEY = "session_id"
_ENV_FIELD = "_env"


def _stamped_session_id(request: dict):
    """The caller session id as it reaches `_serve_line`, or None if the door
    stamped no identity at all -- absence, never an empty string
    (`test_no_resolvable_identity_stamps_nothing_and_still_serves`).

    First-non-empty-wins over `SESSION_ENV_PRECEDENCE`, read out of `_env`
    now that the door stamps every resolved declared name by its own key
    rather than a single pre-resolved `_caller.session_id` slot."""
    env_obj = request.get(_ENV_FIELD)
    if not isinstance(env_obj, dict):
        return None
    for var in SESSION_ENV_PRECEDENCE:
        value = env_obj.get(var)
        if value:
            return value
    return None

#: Shaped like a real session id because `session.core.session_identity_override`
#: gates on UUID shape and binds nothing for a value that fails it -- a test
#: fixture that could not survive that gate would pass here and prove nothing
#: about the path the defect ran down.
_CALLER_SID = "8b40d62c-55ef-4702-83ce-0cd8dc6513e3"
_OTHER_SID = "c131691f-c58d-46f9-80c6-4b6d5e670485"

_OK_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":{"stdout":"pong\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")


def _exchange(root: Path, session_env: "dict[str, str] | None"):
    """Run the door once against a one-shot replying pipe; return (request, proc).

    `session_env` is applied to the DOOR's environment only, and every name in
    `SESSION_ENV_PRECEDENCE` not named there is POPPED rather than left inherited
    -- this process has a real `CLAUDE_CODE_SESSION_ID` of its own, and a test
    that let it leak into the child would assert against the runner's identity
    instead of the fixture's.
    """
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    for var in SESSION_ENV_PRECEDENCE:
        env.pop(var, None)
    env.update(session_env or {})

    server = _ReplyingServer(_pipe_name_for(root), _OK_REPLY)
    try:
        proc = subprocess.run(
            [str(_door_under_default_name(root)), "ping"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            cwd=str(root),
            **no_console_creationflags(),
        )
    finally:
        server.close()

    request = server.request.decode("utf-8").strip()
    return (json.loads(request) if request else {}), proc


def test_the_door_stamps_the_session_its_caller_is(tmp_path: Path) -> None:
    """The defect itself. Without this field the server has no way to know who
    called, falls back to its own environment, and attributes the caller's write
    to whichever session spawned the engine."""
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, {"CLAUDE_CODE_SESSION_ID": _CALLER_SID})

    assert _stamped_session_id(request) == _CALLER_SID


def test_the_stamp_is_the_callers_id_and_never_the_servers(tmp_path: Path) -> None:
    """The reported symptom stated as a property: the id on the wire is the one
    in the DOOR's environment, and a different id reachable elsewhere on the box
    is not it. This is the leg that fails against a silent door -- which stamps
    nothing, so neither id appears at all."""
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, {"CLAUDE_CODE_SESSION_ID": _CALLER_SID})

    assert _stamped_session_id(request) != _OTHER_SID
    assert _stamped_session_id(request) == _CALLER_SID


def test_precedence_matches_the_resolver_the_door_stands_in_for(tmp_path: Path) -> None:
    """`session.core.SESSION_ENV_PRECEDENCE`, in order, first non-empty wins.
    A door reading only one of the three would disagree with the resolver it
    substitutes for -- the precise defect that constant's own comment records
    (slice D, F1: a guard reading only COORDINATOR_SESSION_ID told a real
    session that had only CLAUDE_CODE_SESSION_ID set "Not your claim")."""
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(
        root,
        {
            "COORDINATOR_SESSION_ID": _CALLER_SID,
            "CLAUDE_SESSION_ID": _OTHER_SID,
            "CLAUDE_CODE_SESSION_ID": _OTHER_SID,
        },
    )

    assert _stamped_session_id(request) == _CALLER_SID


def test_a_lower_rung_is_read_when_the_higher_ones_are_unset(tmp_path: Path) -> None:
    """The rung the reported defect actually ran down: the harness injects
    `CLAUDE_CODE_SESSION_ID` and neither override above it is set, which is the
    ordinary condition of every session on the box."""
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, {"CLAUDE_CODE_SESSION_ID": _CALLER_SID})

    assert _stamped_session_id(request) == _CALLER_SID


def test_an_empty_value_falls_through_to_the_next_rung(tmp_path: Path) -> None:
    """Empty is not an identity. `resolve_session_id` skips an empty variable and
    keeps walking; a door that treated "set but empty" as an answer would stamp
    nothing while a real id sat on the rung below it."""
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(
        root,
        {"COORDINATOR_SESSION_ID": "", "CLAUDE_CODE_SESSION_ID": _CALLER_SID},
    )

    assert _stamped_session_id(request) == _CALLER_SID


def test_the_stamp_is_envelope_level_not_an_op_param(tmp_path: Path) -> None:
    """Sibling of `_engine_token`, never inside `params` -- transport metadata the
    server pops before it dispatches, not an argument any op reads. `entrypoint`
    shipped in the wrong half of this same envelope once already (door.c's own
    note, 2026-08-27), and `_settings_home` carries the identical assertion."""
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, {"CLAUDE_CODE_SESSION_ID": _CALLER_SID})

    assert _ENV_FIELD not in request["params"]


def test_no_resolvable_identity_stamps_nothing_and_still_serves(tmp_path: Path) -> None:
    """The field must be ABSENT, not present-and-empty. `_serve_line` reads
    absence as "this caller could not identify itself" and binds nothing, which
    is a no-op rather than a fabricated identity -- and an empty string would
    reach `session_identity_override`'s shape gate as a value, not as silence.
    Backward compatibility is an AC here for the same reason it is for
    `_settings_home`: the no-identity path must serve byte-identically."""
    root = _make_stub_engine_root(tmp_path)

    request, proc = _exchange(root, None)

    assert _stamped_session_id(request) is None
    assert proc.returncode == 0
    assert proc.stdout == "pong\n"
