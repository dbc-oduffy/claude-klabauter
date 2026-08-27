"""The git-revert oracle memo inside ``_build_guard_chain`` is keyed on the
override env it was computed against, not on ``(cmd, session_id)`` alone.

WHY THIS FILE EXISTS. ``_git_revert_cache`` memoises
``_check_destructive_git_revert_full`` so the hard-deny leg and its advisory
sibling share one ``git status``/``git rev-parse`` oracle pass. That oracle
reads ``COORDINATOR_OVERRIDE_GIT_REVERT`` through ``dispatch_checks._override``,
which C14c re-keyed to prefer the per-call ``payload["env"]`` over ambient
``os.environ``. Two calls agreeing on ``cmd`` and ``session_id`` but carrying
different override env therefore have DIFFERENT correct verdicts.

Today the cache is a fresh local per ``_build_guard_chain`` call, so a narrow
``(cmd, session_id)`` key is observationally identical to the wide one and NO
TEST FAILS IF IT IS NARROWED BACK. That is exactly the hazard: the day someone
hoists this memo onto anything that outlives one call -- module scope, a
warm-server-scoped memo -- the narrow key serves one session's override verdict
to another session's identical ``cmd``, which is the boundary C14c exists to
protect, arriving through a cache instead of an environ read. It would fail
only in production, on a warm server, as a silently disarmed guard.

NEGATIVE SPEC. These tests do not assert that any particular command is denied
or allowed, and they never assert on the oracle's verdict content -- that is
``test_check_destructive_git_revert_stash.py``'s job. They assert only that two
override contexts are held APART by the cache key.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import pytest

from coordinator_core.bash_guards import dispatch


OVERRIDE = "COORDINATOR_OVERRIDE_GIT_REVERT"


def _payload(env: Optional[Dict[str, str]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": "git stash drop"},
        "session_id": "s-1",
    }
    if env is not None:
        payload["env"] = env
    return payload


class TestOverrideEnvIdentity:
    def test_two_override_values_are_distinguished(self) -> None:
        on = dispatch._override_env_identity(_payload({OVERRIDE: "1"}))
        off = dispatch._override_env_identity(_payload({OVERRIDE: "0"}))
        assert on != off

    def test_override_present_differs_from_override_absent(self) -> None:
        present = dispatch._override_env_identity(_payload({OVERRIDE: "1"}))
        absent = dispatch._override_env_identity(_payload({}))
        assert present != absent
        assert absent == frozenset()

    def test_identical_override_env_shares_one_identity(self) -> None:
        a = dispatch._override_env_identity(_payload({OVERRIDE: "1", "PATH": "/a"}))
        b = dispatch._override_env_identity(_payload({OVERRIDE: "1", "PATH": "/b"}))
        assert a == b, "non-override env must not fragment the key"

    def test_allow_prefix_is_captured_too(self) -> None:
        identity = dispatch._override_env_identity(
            _payload({"COORDINATOR_ALLOW_SOMETHING": "1"})
        )
        assert identity == frozenset({("COORDINATOR_ALLOW_SOMETHING", "1")})

    def test_absent_payload_env_falls_back_to_ambient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(OVERRIDE, "1")
        assert (OVERRIDE, "1") in dispatch._override_env_identity(_payload(None))
        assert (OVERRIDE, "1") in dispatch._override_env_identity({"no": "env-key"})

    def test_payload_env_wins_over_ambient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(OVERRIDE, "1")
        identity = dispatch._override_env_identity(_payload({}))
        assert identity == frozenset(), "payload env present must shadow os.environ"

    def test_identity_is_hashable_and_usable_as_a_key(self) -> None:
        identity = dispatch._override_env_identity(_payload({OVERRIDE: "1"}))
        assert {("cmd", "sid", identity): 1}


def _git_revert_cache_after_one_call(
    payload: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> Dict[Tuple[Any, ...], Any]:
    """Drive the real registration once and hand back the memo it filled.

    The cache is a closure local, so it is read off the registered entry's own
    closure rather than reconstructed -- a reconstruction would pin this test's
    idea of the key, which is the thing under test.
    """
    sentinel = (None, None)
    monkeypatch.setattr(
        dispatch._dc,
        "_check_destructive_git_revert_full",
        lambda *a, **k: sentinel,
    )
    chain = dispatch._build_guard_chain(
        cmd="git stash drop",
        session_id=str(payload.get("session_id", "s-1")),
        cwd=os.getcwd(),
        payload=payload,
        policy_file=None,
        host_is_windows=None,
    )
    entry = next(e for e in chain if e.name == "destructive-git-revert")
    entry.fn()

    for cell in entry.fn.__closure__ or ():
        contents = cell.cell_contents
        inner = getattr(contents, "__closure__", None) or ()
        for sub in inner:
            value = sub.cell_contents
            if isinstance(value, dict) and value:
                return value
    raise AssertionError("the git-revert memo was not reachable off the closure")


class TestCacheKeyShape:
    def test_the_memo_key_carries_the_override_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = _payload({OVERRIDE: "1"})
        cache = _git_revert_cache_after_one_call(payload, monkeypatch)

        (key,) = list(cache)
        assert len(key) == 3, (
            "the memo key narrowed back to (cmd, session_id); an override "
            "verdict is now shareable between two sessions"
        )
        assert key[2] == dispatch._override_env_identity(payload)

    def test_two_override_contexts_do_not_share_a_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on = _git_revert_cache_after_one_call(_payload({OVERRIDE: "1"}), monkeypatch)
        off = _git_revert_cache_after_one_call(_payload({OVERRIDE: "0"}), monkeypatch)
        assert set(on).isdisjoint(set(off))


def test_no_module_scope_env_read_was_introduced() -> None:
    """``dispatch.py`` reads env only inside function bodies (module docstring
    § inline ``os.environ.get``). The helper this file pins must not have
    hoisted an ``import os`` to module scope to get there.
    """
    assert not hasattr(dispatch, "os"), (
        "os became a module attribute of dispatch.py -- the inline-env-read "
        "invariant in its own module docstring is broken"
    )
    assert os.environ is not None  # the test module's own import, deliberate
