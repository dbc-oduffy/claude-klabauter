"""Both-host verdict-parity matrix + the confinement overlap test (H6,
docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md).

AC-3 requires each of the 15 advisory-emitting guards OBSERVED FIRING
THROUGH THE REAL DISPATCHER (``dispatch.evaluate_payload_json``) under
both host verdicts. Because several of these guards are, by design,
SHADOWED by an earlier-registered guard on the same command shape in the
full chain (`plumbing-and-loops` behind `head-tail-plumbing-rewrite`;
`grep-via-bash-guard`/`grep-via-bash-rewrite` behind `inprocess-search`),
this file uses the SAME isolation technique
`test_dispatch_blanket_disarm_wiring.py` already established: monkeypatch
`dispatch._build_guard_chain` to return a single, REAL `GuardEntry`
(extracted from a REAL `_build_guard_chain(...)` call, never hand-built)
so `evaluate_payload_json`'s REAL loop -- including the H4 suppression
consult, the leg-scoped stripping, and the stderr attribution line -- runs
against exactly that one guard's real `fn` closure and real classification,
without an earlier guard shadowing it. This is not a synthetic guard; it is
the real registered entry, isolated.

One guard (`validate-commit`) has no cheap, deterministic single-call
trigger -- `check_validate_commit` reads real git-repo staged state. It is
named explicitly below (never silently dropped) and exercised via the REAL
suppression predicate against a representative envelope shaped exactly like
its own `_advisory(...)`/`allow_advisory(...)` return, rather than through a
live trigger command.

Spec backlink: DoE-claude:pln-os-aware-guard-advisory-defaul-060dbe
(DoE-claude) row H6.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile

import pytest

from coordinator_core.bash_guards import _platform_verdict, dispatch
from coordinator_core.bash_guards._advisory_value import AdvisoryValue, suppress_advisory
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _payload(cmd, session_id="h6-probe", cwd="/tmp"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
    }


def _entry_for(name, cmd, session_id, host_is_windows, cwd="/tmp"):
    """Build the REAL chain with `host_is_windows` baked in identically to
    how `evaluate_payload_json` itself builds it (line: `guard_chain =
    _build_guard_chain(cmd, session_id, cwd, payload, policy_file,
    host_is_windows, resolved)`), then pull out the one entry by name.
    Required for `multiprobe-banner` and `plumbing-and-loops` (the two
    remaining PLATFORM_CONDITIONED_DENY guards -- `grep-via-bash-guard`
    moved to ADVISORY_REWRITE, H11, 2026-07-30), whose `fn`
    closures capture `host_is_windows` from `_build_guard_chain`'s own
    call -- not re-read at `fn()` call time -- so the chain must be built
    with the SAME `host_is_windows` this test will later pass to
    `evaluate_payload_json`."""
    payload = _payload(cmd, session_id=session_id, cwd=cwd)
    chain = dispatch._build_guard_chain(
        cmd, session_id, cwd, payload, None, host_is_windows, None
    )
    for entry in chain:
        if entry.name == name:
            return entry, payload
    raise AssertionError("guard %r not found in the real registered chain" % name)


def _run_isolated(name, cmd, host_is_windows, session_id, monkeypatch, cwd="/tmp"):
    entry, payload = _entry_for(name, cmd, session_id, host_is_windows, cwd=cwd)
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
    return dispatch.evaluate_payload_json(json.dumps(payload), host_is_windows=host_is_windows)


def _cwd_for(name, tmp_path, monkeypatch):
    return "/tmp"


_TRIGGERS = {
    "find-exec-rewrite": "find . -exec rm {} \\;",
    "head-tail-plumbing-rewrite": "find . -type f | head -n 5",
    "plumbing-and-loops": "find . -type f | head -n 5",
    # A DEDICATED dir, never a bare `/tmp`: this box keeps live sockets and
    "inprocess-search": "grep -rn TODO /tmp/h6-search",
    "sed-range-read-advise": "sed -n '5,10p' /tmp/h6-somefile.txt",
    "cat-heredoc-write-advise": "cat > /tmp/h6-probe.txt <<'EOF'\nhello\nEOF",
    "block-illegal-filename": "echo hi > file:name.txt",
    "multiprobe-banner": 'echo "=== SESSION FACTS ==="; pwd; whoami; date',
    "multiprobe-banner-rewrite": 'echo "=== SESSION FACTS ==="; pwd; whoami; date',
    "grep-via-bash-rewrite": "grep -E '^status:|^deployment_state:|^closed_reason:' /tmp/h6-somefile.txt",
    # shadowed by the CHAINED-only partial-pipe path either. Reclassified
    # WINDOWS_COST_ONLY -> HOST_INDEPENDENT in the same H11 dispatch: its
    # comment on this guard's entry for why WINDOWS_COST_ONLY would have
    "grep-via-bash-guard": "grep -Pn TODO src/",
    "offer-git-c": "cd /tmp && git status",
    "validate-commit": None,
    "git-commit-safe-commit-advise": "git commit -m 'h6 probe'",
    "offer-invoke-params-stdin": (
        "PYTHONPATH=/r python3 -m coordinator_core.invoke ceremony.scoped_git_commit '%s' --repo /r --bare"
        % json.dumps(
            {
                "worktree_root": "/tmp",
                "paths": ["a.md"],
                "message": "reconcile: C1's h6 probe\n\nSecond line.\n",
            }
        )
    ),
}

_EXPECTED_VALUE = {
    "find-exec-rewrite": AdvisoryValue.WINDOWS_COST_ONLY,
    "head-tail-plumbing-rewrite": AdvisoryValue.WINDOWS_COST_ONLY,
    "plumbing-and-loops": AdvisoryValue.WINDOWS_COST_ONLY,
    "inprocess-search": AdvisoryValue.HOST_INDEPENDENT,
    "sed-range-read-advise": AdvisoryValue.HOST_INDEPENDENT,
    "cat-heredoc-write-advise": AdvisoryValue.HOST_INDEPENDENT,
    "block-illegal-filename": AdvisoryValue.HOST_INDEPENDENT,
    "multiprobe-banner": AdvisoryValue.HOST_INDEPENDENT,
    "multiprobe-banner-rewrite": AdvisoryValue.HOST_INDEPENDENT,
    "grep-via-bash-guard": AdvisoryValue.HOST_INDEPENDENT,
    "grep-via-bash-rewrite": AdvisoryValue.HOST_INDEPENDENT,
    "offer-git-c": AdvisoryValue.NOT_COST_ARGUED,
    "validate-commit": AdvisoryValue.NOT_COST_ARGUED,
    "git-commit-safe-commit-advise": AdvisoryValue.NOT_COST_ARGUED,
    "offer-invoke-params-stdin": AdvisoryValue.NOT_COST_ARGUED,
}


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    """A couple of guards live in `tempfile.gettempdir()`-scoped files keyed
    by session id -- point that at a fresh per-test dir so cells never
    interact with a prior run's leftover state (same discipline
    `test_guard_band_membership.py`'s `_SIX_COMBINATIONS` parametrization
    already uses)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    (tmp_path / "h6-somefile.txt").write_text("one\ntwo\nTODO: probe\nfour\nfive\n", encoding="utf-8")
    # negative-spec: this leg is BEST-EFFORT and must stay so. `/tmp` does not
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname("/tmp/h6-somefile.txt"), exist_ok=True)
        with open("/tmp/h6-somefile.txt", "w", encoding="utf-8") as fh:
            fh.write("one\ntwo\nTODO: probe\nfour\nfive\n")
        os.makedirs("/tmp/h6-search", exist_ok=True)
        with open("/tmp/h6-search/probe.txt", "w", encoding="utf-8") as fh:
            fh.write("one\ntwo\nTODO: probe\nfour\nfive\n")
    yield


_MATRIX_NAMES = [name for name, cmd in _TRIGGERS.items() if cmd is not None]


@pytest.mark.parametrize("name", _MATRIX_NAMES)
def test_windows_true_always_shows(name, monkeypatch, capsys, tmp_path):
    cmd = _TRIGGERS[name]
    cwd = _cwd_for(name, tmp_path, monkeypatch)
    out = _run_isolated(name, cmd, True, "h6-%s-true" % name, monkeypatch, cwd=cwd)
    assert out is not None, "%s: expected a real envelope on Windows, got silent allow" % name
    stderr = capsys.readouterr().err
    assert "%s guard suppressed" % name not in stderr, (
        "%s: unexpectedly suppressed on Windows -- suppression must never trigger when "
        "host_is_windows=True" % name
    )


@pytest.mark.parametrize("name", _MATRIX_NAMES)
def test_non_windows_host_default(name, monkeypatch, capsys, tmp_path):
    """On a non-Windows host: a windows_cost_only guard is suppressed (fully,
    or leg-scoped down to just its rewrite); host_independent and
    not_cost_argued guards are UNCHANGED -- they still show."""
    cmd = _TRIGGERS[name]
    value = _EXPECTED_VALUE[name]
    cwd = _cwd_for(name, tmp_path, monkeypatch)
    out = _run_isolated(name, cmd, False, "h6-%s-false" % name, monkeypatch, cwd=cwd)
    stderr = capsys.readouterr().err
    if value is AdvisoryValue.WINDOWS_COST_ONLY:
        assert ("%s guard suppressed on non-Windows host (advisory_value=windows_cost_only)" % name) in stderr, (
            "%s: expected the H4 suppression stderr line naming this guard -- its absence is "
            "indistinguishable from the guard crashing or never firing (the anti-fail-open hazard "
            "this row exists to catch)" % name
        )
    else:
        assert ("%s guard suppressed" % name) not in stderr, (
            "%s (advisory_value=%s) must NEVER be suppressed on any host" % (name, value.value)
        )
        assert out is not None, (
            "%s (advisory_value=%s): expected the guard to still fire on a non-Windows host"
            % (name, value.value)
        )


def test_validate_commit_predicate_never_suppresses_not_cost_argued():
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "Advisory: this 'git commit' names no scope...",
        }
    }
    assert (
        suppress_advisory(
            envelope,
            advisory_value=AdvisoryValue.NOT_COST_ARGUED,
            band=GuardBand.ADVISORY_REWRITE,
            host_is_windows=False,
        )
        is False
    )


class _FakeOs:

    def __init__(self, name):
        self.name = name


class _FakeSys:

    def __init__(self, platform):
        self.platform = platform


_PAIRED_SYS_PLATFORM = {"nt": "win32", "posix": "linux"}


@pytest.mark.parametrize("os_name,expect_suppressed", [("nt", False), ("posix", True)])
def test_none_path_resolves_to_real_host_not_falsy(os_name, expect_suppressed, monkeypatch, capsys):
    # (`_declared_host_is_windows`, `_REGISTRY_KEY = "coordinator.host_is_windows"`),
    monkeypatch.setattr(_platform_verdict, "_declared_host_is_windows", lambda: None)
    monkeypatch.setattr(_platform_verdict, "os", _FakeOs(os_name))
    monkeypatch.setattr(_platform_verdict, "sys", _FakeSys(_PAIRED_SYS_PLATFORM[os_name]))
    cmd = _TRIGGERS["find-exec-rewrite"]
    session_id = "h6-none-path-%s" % os_name
    payload = _payload(cmd, session_id=session_id)
    chain = dispatch._build_guard_chain(cmd, session_id, "/tmp", payload, None, None, None)
    entry = next(e for e in chain if e.name == "find-exec-rewrite")
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
    out = dispatch.evaluate_payload_json(json.dumps(payload))
    stderr = capsys.readouterr().err
    assert out is not None, "expected the rewrite leg to survive regardless of suppression"
    if expect_suppressed:
        assert "find-exec-rewrite guard suppressed" in stderr
        assert "additionalContext" not in out["hookSpecificOutput"]
    else:
        assert "find-exec-rewrite guard suppressed" not in stderr
        assert "additionalContext" in out["hookSpecificOutput"]
    assert "updatedInput" in out["hookSpecificOutput"], (
        "the auto-rewrite leg must survive regardless of suppression -- a bare truthiness "
        "test on host_is_windows=None would invert this on the one host (os.name == 'nt') "
        "the suite exists for"
    )


# THE LEG-SCOPED CELL (finding 4): a suppressed find-exec command still gets


def test_leg_scoped_suppression_preserves_rewrite_drops_advisory(monkeypatch):
    cmd = "find . -exec rm {} \\;"
    out_windows = _run_isolated("find-exec-rewrite", cmd, True, "h6-leg-true", monkeypatch)
    out_macos = _run_isolated("find-exec-rewrite", cmd, False, "h6-leg-false", monkeypatch)

    assert "updatedInput" in out_windows["hookSpecificOutput"]
    assert "additionalContext" in out_windows["hookSpecificOutput"]

    assert "updatedInput" in out_macos["hookSpecificOutput"], (
        "the auto-rewrite must survive suppression -- envelope-scoped suppression would "
        "silently disable this guard's free auto-rewrite on macOS/Linux, making Windows and "
        "non-Windows EXECUTE DIFFERENT COMMANDS for identical agent input"
    )
    assert "additionalContext" not in out_macos["hookSpecificOutput"], (
        "the advisory prose leg should be dropped on a non-Windows host"
    )
    assert (
        out_windows["hookSpecificOutput"]["updatedInput"]
        == out_macos["hookSpecificOutput"]["updatedInput"]
    ), "the rewrite itself must be byte-identical on both hosts"


# THE TWO-GUARD-OVERLAP CELL (finding 5): shadowing. A command tripping both


def test_shadowing_previously_shadowed_guard_may_now_surface(monkeypatch, capsys):
    cmd = "find . -exec rm {} \\;"
    out_true = dispatch.evaluate_payload_json(
        json.dumps(_payload(cmd, session_id="h6-shadow-true")), host_is_windows=True
    )
    out_false = dispatch.evaluate_payload_json(
        json.dumps(_payload(cmd, session_id="h6-shadow-false")), host_is_windows=False
    )
    assert out_true["hookSpecificOutput"]["updatedInput"] == out_false["hookSpecificOutput"]["updatedInput"]
    assert "additionalContext" in out_true["hookSpecificOutput"]
    assert "additionalContext" not in out_false["hookSpecificOutput"]


@pytest.mark.parametrize("advisory_value", list(AdvisoryValue))
def test_deny_envelope_never_suppressed_for_any_advisory_value(advisory_value):
    deny_envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "denied for h6 AC-5 unit test",
        }
    }
    assert (
        suppress_advisory(
            deny_envelope,
            advisory_value=advisory_value,
            band=GuardBand.ADVISORY_REWRITE,
            host_is_windows=False,
        )
        is False
    )


# (b) A CHAIN-LEVEL case: a deny-capable guard registered AFTER a suppressed
# for `_FAKE_CHAIN`), so the case is not already answered by chain ordering


def test_suppressed_advisory_then_later_deny_still_fires(monkeypatch):
    suppressible_allow = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "a pure advisory that should be fully suppressed",
        }
    }
    later_deny = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "a later not_cost_argued guard's deny",
        }
    }
    fake_chain = [
        GuardEntry(
            "fake-suppressible-advisory",
            lambda: suppressible_allow,
            False,
            GuardBand.ADVISORY_REWRITE,
            AdvisoryValue.WINDOWS_COST_ONLY,
        ),
        GuardEntry(
            "fake-later-deny-not-cost-argued",
            lambda: later_deny,
            False,
            GuardBand.ADVISORY_REWRITE,
            AdvisoryValue.NOT_COST_ARGUED,
        ),
    ]
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: list(fake_chain))
    payload = _payload("echo h6-overlap-probe", session_id="h6-overlap-chain")
    out = dispatch.evaluate_payload_json(json.dumps(payload), host_is_windows=False)
    assert out == later_deny, (
        "the first guard's pure advisory was suppressed on non-Windows and the chain "
        "continued to the second guard's deny, which must still fire regardless of the "
        "first guard's classification"
    )
