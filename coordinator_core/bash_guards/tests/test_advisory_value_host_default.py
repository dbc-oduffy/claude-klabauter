"""Both-host verdict-parity matrix + the confinement overlap test (H6,
docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md).

AC-3 requires each of the 16 advisory-emitting guards OBSERVED FIRING
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

Two guards (`validate-commit`, `probe-spray`) have no cheap, deterministic
single-call trigger -- `check_validate_commit` reads real git-repo staged
state, and `check_probe_spray`'s ring-buffer recurrence needs several prior
calls in the same session before it fires. Both are named explicitly below
(never silently dropped) and exercised via the REAL suppression predicate
against a representative envelope shaped exactly like each guard's own
`_advisory(...)`/`allow_advisory(...)` return, rather than through a live
trigger command.

Spec backlink: docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md
(example-doctrine-repo) row H6.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

from coordinator_core.bash_guards import _platform_verdict, dispatch
from coordinator_core.bash_guards import guard_branch_set_precedence, guard_longlived_branch_naming
from coordinator_core.bash_guards._advisory_value import AdvisoryValue, suppress_advisory
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry
from coordinator_core.daily_day import local_day

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


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
    `evaluate_payload_json`.

    `cwd` (docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C2):
    `branch-set-precedence`/`longlived-branch-naming` are hazard-repo-scoped
    (see `_cwd_for` below) -- every other row keeps the original hardcoded
    `"/tmp"` default."""
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


def _git(argv, cwd):
    subprocess.run(["git"] + argv, cwd=cwd, check=True, capture_output=True, timeout=10, **_NO_WINDOW)


#: `branch-set-precedence`'s own advisory candidate branch, dated TODAY so
#: AC16's `should_prompt_rename` leg short-circuits False on "span already
#: covers today" (see `_alternative_liveness._trigger_guard_branch_set_
#: precedence`'s own docstring for the full age-vs-span-date reasoning this
#: mirrors).
_H6_TODAY = local_day()
_H6_BRANCH_SET_CANDIDATE = "work/h6-other/%s" % _H6_TODAY
_H6_BRANCH_SET_TARGET = "work/h6-machine/%s" % _H6_TODAY


def _setup_branch_set_repo(tmp_path):
    """A real scratch git repo (never this package's own working tree) with
    a real commit on `_H6_BRANCH_SET_CANDIDATE`, ahead of a real `main` --
    `branch-set-precedence`'s own `ahead_of_main` leg is a live `git
    rev-list` call, not injectable through this test's plain command-string
    `_TRIGGERS` shape. `refs/remotes/origin/main` is written explicitly for
    the same `_branch_set._main_ref()` cwd-blind-resolution reason
    documented on `_alternative_liveness._trigger_guard_branch_set_
    precedence`."""
    repo = tmp_path / "h6-branch-set-repo"
    repo.mkdir()
    cwd = str(repo)
    _git(["init", "-q", "-b", "main"], cwd)
    _git(["config", "user.email", "h6-probe@example.com"], cwd)
    _git(["config", "user.name", "h6-probe"], cwd)
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "."], cwd)
    _git(["commit", "-q", "-m", "init"], cwd)
    _git(["update-ref", "refs/remotes/origin/main", "HEAD"], cwd)
    _git(["checkout", "-q", "-b", _H6_BRANCH_SET_CANDIDATE], cwd)
    (repo / "f.txt").write_text("hello\ncandidate commit\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "candidate commit"], cwd)
    _git(["checkout", "-q", "main"], cwd)
    return cwd


def _cwd_for(name, tmp_path, monkeypatch):
    """Per-guard `cwd` for the matrix tests below -- every row not named
    here keeps the shared `"/tmp"` default. `branch-set-precedence` and
    `longlived-branch-naming` (docs/plans/2026-08-01-branch-creation-seam-
    guards.md, chunk C2) are hazard-repo-scoped, so a bare `"/tmp"` cwd
    makes them silently no-op (`_is_hazard_repo` returns False before any
    name predicate runs) rather than fire. `_is_hazard_repo` is swapped on
    each guard's OWN module attribute for the duration of one test, then
    restored by `monkeypatch` on teardown -- mirrors this package's own
    injection convention (`tests/test_guard_branch_set_precedence.py`'s
    `monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)`)
    rather than depending on this dev machine's real fleet registry."""
    if name == "branch-set-precedence":
        monkeypatch.setattr(guard_branch_set_precedence, "_is_hazard_repo", lambda git_root: True)
        return _setup_branch_set_repo(tmp_path)
    if name == "longlived-branch-naming":
        monkeypatch.setattr(guard_longlived_branch_naming, "_is_hazard_repo", lambda git_root: True)
        return str(tmp_path)
    return "/tmp"


# ---------------------------------------------------------------------------
# Per-guard triggers -- one real, verified-firing command per advisory-
# emitting guard (16 of them), keyed by name. `None` marks the two guards
# with no cheap deterministic single-call trigger (see module docstring).
# ---------------------------------------------------------------------------

_TRIGGERS = {
    # windows_cost_only (3)
    "find-exec-rewrite": "find . -exec rm {} \\;",
    "head-tail-plumbing-rewrite": "find . -type f | head -n 5",
    "plumbing-and-loops": "find . -type f | head -n 5",
    # host_independent (8)
    "inprocess-search": "grep -rn TODO /tmp",
    "sed-range-read-advise": "sed -n '5,10p' /tmp/h6-somefile.txt",
    "cat-heredoc-write-advise": "cat > /tmp/h6-probe.txt <<'EOF'\nhello\nEOF",
    "block-illegal-filename": "echo hi > file:name.txt",
    "multiprobe-banner": 'echo "=== SESSION FACTS ==="; pwd; whoami; date',
    "multiprobe-banner-rewrite": 'echo "=== SESSION FACTS ==="; pwd; whoami; date',
    "grep-via-bash-rewrite": "grep -E '^status:|^deployment_state:|^closed_reason:' /tmp/h6-somefile.txt",
    # `grep -rn` is substitutable residue that `grep-via-bash-rewrite`
    # already claims (H11, 2026-07-30) -- this guard now returns None for
    # it. `-P` is a genuinely GNU-only construct this guard still advises
    # on (`_has_gnu_only_construct`); single segment, so it is not
    # shadowed by the CHAINED-only partial-pipe path either. Reclassified
    # WINDOWS_COST_ONLY -> HOST_INDEPENDENT in the same H11 dispatch: its
    # surviving message is a BSD-vs-GNU (macOS) portability warning, not a
    # Windows spawn-cost argument -- see dispatch.py's own registration
    # comment on this guard's entry for why WINDOWS_COST_ONLY would have
    # silenced it on exactly the host it targets.
    "grep-via-bash-guard": "grep -Pn TODO src/",
    # not_cost_argued (5)
    "offer-git-c": "cd /tmp && git status",
    "validate-commit": None,  # no cheap trigger -- see module docstring
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
    "probe-spray": None,  # no cheap trigger -- see module docstring
    # docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C2. Both
    # never deny (advisory-only), never Windows-cost-argued -- see
    # `_EXPECTED_VALUE` below. `cwd` for both is supplied per-test via
    # `_cwd_for` (hazard-repo-scoped; the shared "/tmp" default is a no-op
    # for either).
    "branch-set-precedence": "git checkout -b %s" % _H6_BRANCH_SET_TARGET,
    "longlived-branch-naming": "git checkout -b migration/h6-longlived-probe",
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
    "probe-spray": AdvisoryValue.NOT_COST_ARGUED,
    "branch-set-precedence": AdvisoryValue.NOT_COST_ARGUED,
    "longlived-branch-naming": AdvisoryValue.NOT_COST_ARGUED,
}


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    """probe-spray's ring buffer and a couple of guards live in
    `tempfile.gettempdir()`-scoped files keyed by session id -- point that
    at a fresh per-test dir so cells never interact with a prior run's
    leftover state (same discipline `test_guard_band_membership.py`'s
    `_SIX_COMBINATIONS` parametrization already uses)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    (tmp_path / "h6-somefile.txt").write_text("one\ntwo\nTODO: probe\nfour\nfive\n", encoding="utf-8")
    # sed/grep triggers above reference /tmp/h6-somefile.txt directly (not
    # tmp_path) since the guards under test are static string parsers that
    # never execute the command -- write the same content there too so a
    # guard that DOES stat the path (inprocess-search) finds it.
    with open("/tmp/h6-somefile.txt", "w", encoding="utf-8") as fh:
        fh.write("one\ntwo\nTODO: probe\nfour\nfive\n")
    yield


# ---------------------------------------------------------------------------
# The 16-guard x {True, False} matrix (AC-3's core requirement).
# ---------------------------------------------------------------------------

_MATRIX_NAMES = [name for name, cmd in _TRIGGERS.items() if cmd is not None]


@pytest.mark.parametrize("name", _MATRIX_NAMES)
def test_windows_true_always_shows(name, monkeypatch, capsys, tmp_path):
    """Every advisory-emitting guard, on Windows, fires its full envelope
    unsuppressed -- this is the identity-bound anti-fail-open control cell
    for every row: it proves the guard demonstrably still detects."""
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


# ---------------------------------------------------------------------------
# Two guards with no cheap live trigger: validate-commit, probe-spray.
# Exercised via the REAL suppression predicate against a representative
# envelope shaped exactly like each guard's own return constructor.
# ---------------------------------------------------------------------------


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


def test_probe_spray_fires_after_ring_recurrence_and_predicate_never_suppresses():
    from coordinator_core.bash_guards import dispatch_checks as dc

    session_id = "h6-probe-spray-recurrence"
    out = None
    for _ in range(6):
        out = dc.check_probe_spray("pwd", session_id)
        if out is not None:
            break
    assert out is not None, "probe-spray never fired across 6 identical bare-command calls"
    assert (
        suppress_advisory(
            out,
            advisory_value=AdvisoryValue.NOT_COST_ARGUED,
            band=GuardBand.ADVISORY_REWRITE,
            host_is_windows=False,
        )
        is False
    )


# ---------------------------------------------------------------------------
# THE THIRD MATRIX COLUMN (finding 1): host_is_windows OMITTED entirely --
# the production call shape -- with the real-host read faked both ways. The
# True/False columns above cannot see the None-path inversion; this proves
# `None` resolves to the REAL host, not to "not Windows".
#
# The fake is installed on `_platform_verdict`'s own module-global `os`, not on
# the `os` module itself. Patching `os.name` globally is what a reader reaches
# for first and it does not work here: `pathlib` branches on `os.name` at
# construction time, so faking a Windows host that way makes every `Path()`
# built anywhere downstream -- including the disarm-marker lookup this call
# reaches -- raise `UnsupportedOperation` on a POSIX machine. Scoping the fake
# to the one module that performs the real-host read keeps the seam under test
# and leaves the rest of the process on its actual platform.
# ---------------------------------------------------------------------------


class _FakeOs:
    """Stands in for `_platform_verdict`'s `os` global, exposing only the one
    attribute the real-host read consults."""

    def __init__(self, name):
        self.name = name


@pytest.mark.parametrize("os_name,expect_suppressed", [("nt", False), ("posix", True)])
def test_none_path_resolves_to_real_host_not_falsy(os_name, expect_suppressed, monkeypatch, capsys):
    monkeypatch.setattr(_platform_verdict, "os", _FakeOs(os_name))
    cmd = _TRIGGERS["find-exec-rewrite"]
    session_id = "h6-none-path-%s" % os_name
    payload = _payload(cmd, session_id=session_id)
    chain = dispatch._build_guard_chain(cmd, session_id, "/tmp", payload, None, None, None)
    entry = next(e for e in chain if e.name == "find-exec-rewrite")
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
    out = dispatch.evaluate_payload_json(json.dumps(payload))  # host_is_windows OMITTED
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


# ---------------------------------------------------------------------------
# THE LEG-SCOPED CELL (finding 4): a suppressed find-exec command still gets
# its auto-rewrite applied on a non-Windows host, even though the advisory
# `additionalContext` is stripped.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# THE TWO-GUARD-OVERLAP CELL (finding 5): shadowing. A command tripping both
# a windows_cost_only guard and a later-registered host_independent guard --
# driven through the FULL, unmodified chain (no isolation) -- documents that
# a previously-shadowed advisory may now surface where the suppressed guard
# used to win.
# ---------------------------------------------------------------------------


def test_shadowing_previously_shadowed_guard_may_now_surface(monkeypatch, capsys):
    # `find . -exec rm {} \;` trips find-exec-rewrite (windows_cost_only,
    # earlier-registered) first in the real chain; it never reaches any
    # later host_independent guard on either host, because find-exec-
    # rewrite ALWAYS returns non-None for this shape (rewrite leg survives
    # suppression) -- so the winner is identical on both hosts for this
    # exact shape, which is itself worth asserting: a rewrite-leg-bearing
    # windows_cost_only guard never un-shadows a later guard, because it
    # never returns None even when suppressed.
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


# ---------------------------------------------------------------------------
# THE OVERLAP LEG (AC-5), written to hold the confinement/deny invariant.
# (a) UNIT-exercise the predicate directly against a deny envelope tagged
#     with each of the four AdvisoryValue members -- False in all four.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# (b) A CHAIN-LEVEL case: a deny-capable guard registered AFTER a suppressed
# one must still fire -- exercised via a controlled two-entry fake chain
# (same monkeypatch technique test_dispatch_blanket_disarm_wiring.py uses
# for `_FAKE_CHAIN`), so the case is not already answered by chain ordering
# the way "no-verify precedes find-exec" trivially is.
# ---------------------------------------------------------------------------


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
