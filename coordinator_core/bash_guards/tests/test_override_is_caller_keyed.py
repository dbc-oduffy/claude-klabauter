"""Tests for `coordinator_core.bash_guards.dispatch_checks._override`'s
re-keying (C14c, `docs/plans/2026-08-22-a-bash-call-stops-costing-a-second-
and-a-half.md`) off ambient `os.environ` onto a per-call caller-context
`payload["env"]` mapping.

Prerequisite for the warm-dispatch server routing C14b adds: once guard
evaluation runs inside a long-lived server process, `os.environ` is that
SERVER's environ, frozen at server start and shared by every session on the
box -- a per-session `COORDINATOR_OVERRIDE_*`/`COORDINATOR_ALLOW_*` set in
one operator's shell would go silently dead, and whatever the server itself
started under would apply, invisibly, to every other session. `_override`
must prefer a caller-supplied `payload["env"]` over `os.environ` so a future
caller (the warm-server wiring) can populate it per-call; every EXISTING
call site (which never passes `payload`, or passes one with no `env` key)
must keep reading `os.environ` exactly as before -- this suite pins both
halves of that contract, plus each of the 22 in-file call sites actually
threading `payload`/`hook_payload` through to `_override` rather than
reading `os.environ` directly.

Spec backlink: state/dispatch-briefs/2026-08-22-a-bash-call-stops-costing-a-
second-and-a-half/C14c.md
"""

from __future__ import annotations

import os

from coordinator_core.bash_guards import dispatch_checks as dc


_ENV_VAR = "COORDINATOR_ALLOW_RM"


def _clear_env(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)


def test_override_reads_payload_env_over_os_environ(monkeypatch):
    """A `payload["env"]` mapping wins over `os.environ` -- the core
    caller-keying behavior this chunk adds. `os.environ` says "0" (or is
    unset); `payload["env"]` says "1" -- the payload wins.
    """
    monkeypatch.setenv(_ENV_VAR, "0")
    payload = {"env": {_ENV_VAR: "1"}}
    assert dc._override(_ENV_VAR, payload=payload) is True


def test_override_payload_env_can_withhold_an_os_environ_grant(monkeypatch):
    """The inverse: `os.environ` says "1" (a stale/frozen server-start
    value) but the CALLER's own resolved context says "0" -- the payload
    still wins, proving this is not just an OR-widening of the old check.
    """
    monkeypatch.setenv(_ENV_VAR, "1")
    payload = {"env": {_ENV_VAR: "0"}}
    assert dc._override(_ENV_VAR, payload=payload) is False


def test_override_falls_back_to_os_environ_when_payload_is_none(monkeypatch):
    """No `payload` at all (every pre-C14c call site, and every direct
    function call in this file's own test suite) -- behavior is
    byte-identical to the pre-existing `os.environ`-only read.
    """
    monkeypatch.setenv(_ENV_VAR, "1")
    assert dc._override(_ENV_VAR) is True
    monkeypatch.setenv(_ENV_VAR, "0")
    assert dc._override(_ENV_VAR) is False


def test_override_falls_back_to_os_environ_when_payload_has_no_env_key(monkeypatch):
    """A `payload` dict that carries no `env` mapping (the shape of every
    real PreToolUse hook payload today, pre-C14b) falls back to
    `os.environ` unchanged -- this chunk does not require the wire shape to
    change to stay behavior-preserving.
    """
    monkeypatch.setenv(_ENV_VAR, "1")
    payload = {"session_id": "sess1", "cwd": "/repo", "agent_id": "aXYZ"}
    assert dc._override(_ENV_VAR, payload=payload) is True


def test_override_falls_back_to_os_environ_when_payload_env_not_a_dict(monkeypatch):
    """A malformed/unexpected `env` value (not a mapping) is treated the
    same as absent -- fail toward the pre-existing behavior, never toward a
    crash or a silent always-False read.
    """
    monkeypatch.setenv(_ENV_VAR, "1")
    payload = {"env": "not-a-dict"}
    assert dc._override(_ENV_VAR, payload=payload) is True


def test_override_missing_key_in_payload_env_defaults_false(monkeypatch):
    """`payload["env"]` present but missing the specific override key reads
    as unset ("0"/False) -- mirrors `os.environ.get(name, "0")`'s own
    default, applied to the payload leg instead.
    """
    monkeypatch.delenv(_ENV_VAR, raising=False)
    payload = {"env": {}}
    assert dc._override(_ENV_VAR, payload=payload) is False


def test_all_override_call_sites_thread_payload_or_hook_payload(monkeypatch):
    """Every `_override(...)` call site inside `dispatch_checks.py` (other
    than the function's own definition/docstring) passes a `payload=`
    keyword argument -- pinning that this chunk re-keyed ALL 22 sites named
    in the dispatching stub, not a partial subset. A future call site added
    without threading caller context through would fail this test, not
    silently read `os.environ` again.
    """
    import inspect
    import re

    src = inspect.getsource(dc)
    # Exclude the function's own `def _override(...)` definition line --
    # match only a call (preceded by something other than `def `), not the
    # definition itself.
    call_sites = [
        m.start()
        for m in re.finditer(r"(?<!def )_override\(", src)
    ]
    assert call_sites, "expected at least one _override(...) call site"

    for pos in call_sites:
        # Scan forward from the call site to its matching close paren,
        # tolerating the multi-line call shapes present in this file
        # (e.g. the amend-flag check split across three lines).
        depth = 0
        i = pos + len("_override(") - 1
        end = None
        for j in range(i, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        assert end is not None, "unbalanced parens scanning from %r" % pos
        call_text = src[pos:end + 1]
        assert "payload=" in call_text, (
            "found an _override(...) call site with no payload= kwarg: %r"
            % call_text
        )


def test_check_destructive_rm_accepts_payload_kwarg(monkeypatch):
    """`check_destructive_rm` gained a `payload` parameter as part of this
    re-key (it previously had none, since its own `_override` call
    unconditionally read `os.environ`) -- confirm the new parameter is
    honored end to end, not just accepted and ignored. Uses the recursive
    `rm` + subshell-resolved-target shape (unverifiable-target deny), the
    one branch gated on `rm_override` alone with no further target
    resolution needed to trigger.
    """
    monkeypatch.delenv(_ENV_VAR, raising=False)
    cmd = "rm -rf $(cat targets.txt)"
    denied = dc.check_destructive_rm(cmd)
    assert denied is not None, "expected a deny with no override present"

    allowed = dc.check_destructive_rm(cmd, payload={"env": {_ENV_VAR: "1"}})
    assert allowed is None, "payload-carried override should have allowed"


def test_check_runaway_find_accepts_payload_kwarg(monkeypatch):
    """Same contract as `check_destructive_rm` above, for
    `check_runaway_find`'s new `payload` parameter."""
    monkeypatch.delenv("COORDINATOR_ALLOW_FIND_ROOT", raising=False)
    denied = dc.check_runaway_find("find / -name '*.py'")
    assert denied is not None, "expected a deny with no override present"

    allowed = dc.check_runaway_find(
        "find / -name '*.py'",
        payload={"env": {"COORDINATOR_ALLOW_FIND_ROOT": "1"}},
    )
    assert allowed is None, "payload-carried override should have allowed"


# ---------------------------------------------------------------------------
# The shell-c unwrap recursion -- the path the two tests above do NOT reach.
#
# `rm -rf $(cat targets.txt)` denies on the subshell-target branch and
# `find / -name '*.py'` denies on the root-anchor branch; neither ever enters
# `_shell_c_unwrap_payloads`. The recursion was where the payload was actually
# being dropped (loop variable named `payload` shadowing the parameter, so the
# recursive call re-scanned with caller context gone and `_override` fell back
# to ambient env). These two pin the fix at ad0b39cac against that exact shape.
# ---------------------------------------------------------------------------


def test_destructive_rm_forwards_payload_through_shell_c_unwrap(monkeypatch):
    """A `sh -c '<rm ...>'` wrapper must carry the caller's payload into the
    unwrapped rescan. Ambient env is deliberately left CLEAN: if the recursion
    drops the payload, `_override` falls back to `os.environ`, finds nothing,
    and the wrapped command denies -- so a regression fails here rather than
    passing quietly.
    """
    monkeypatch.delenv(_ENV_VAR, raising=False)
    wrapped = "sh -c 'rm -rf $(cat targets.txt)'"

    denied = dc.check_destructive_rm(wrapped)
    assert denied is not None, "wrapped rm should deny with no override present"

    allowed = dc.check_destructive_rm(wrapped, payload={"env": {_ENV_VAR: "1"}})
    assert allowed is None, (
        "payload-carried override must survive the shell-c unwrap recursion -- "
        "a None payload here means the recursive call dropped caller context"
    )


def test_runaway_find_forwards_payload_through_shell_c_unwrap(monkeypatch):
    """Same contract as above for `check_runaway_find`'s unwrap recursion."""
    monkeypatch.delenv("COORDINATOR_ALLOW_FIND_ROOT", raising=False)
    wrapped = "sh -c \"find / -name '*.py'\""

    denied = dc.check_runaway_find(wrapped)
    assert denied is not None, "wrapped find should deny with no override present"

    allowed = dc.check_runaway_find(
        wrapped,
        payload={"env": {"COORDINATOR_ALLOW_FIND_ROOT": "1"}},
    )
    assert allowed is None, (
        "payload-carried override must survive the shell-c unwrap recursion -- "
        "a None payload here means the recursive call dropped caller context"
    )
