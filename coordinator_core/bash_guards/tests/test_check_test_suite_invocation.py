"""Tests for coordinator_core.bash_guards.check_test_suite_invocation.

Covers both deny legs (subagent-identity, fail-closed; machine-wide suite
mutex, fail-open) and the classifier's named failure mode: a scoped-LOOKING
path argument that is actually the whole suite because it is (or is an
ancestor of) a configured ``testpaths`` root.

Pure Python -- no shell spawns and no writes outside ``tmp_path``
(Windows+macOS first-class; no POSIX-only path or uid assumptions). The
``testpaths`` seam is fed by writing a real ``pyproject.toml`` into a tmp
directory used as the payload ``cwd``, and the git-root resolver is
monkeypatched so no git repo is required.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.session import core as session_core
from coordinator_core.session import grant as grant_module

_AGENT_ID = "a0123456789abcdef"
_GRANT_SID = "s-grant-leg-test"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake repo root whose pytest config pins this repo's real shape:
    ``testpaths = ["coordinator_core", "coordinator/tests"]``."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["coordinator_core", "coordinator/tests"]\n',
        encoding="utf-8",
    )
    (tmp_path / "coordinator_core" / "frontmatter" / "tests").mkdir(parents=True)
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    # Default this fixture's EM to a granted Tier-U session -- these tests
    # exercise the identity/mutex legs, not the grant leg (which has its own
    # dedicated `grant_repo` fixture and TestGrantLeg class below, driven
    # through the real session/grant module rather than this stub).
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, None))
    return tmp_path


def _payload(command, cwd, agent_id=None, extra=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(cwd),
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if extra:
        p.update(extra)
    return p


def _reason(out):
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


@pytest.fixture
def free_mutex(monkeypatch):
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)


@pytest.fixture
def held_mutex(monkeypatch):
    monkeypatch.setattr(
        guard,
        "_mutex_holder",
        lambda: {
            "pid": 4242,
            "owner": "em-session-abc",
            "started_at": "2026-07-23T10:00:00Z",
            "cmd": "python3 -m pytest",
        },
    )


# ---------------------------------------------------------------------------
# Subagent identity leg
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "pytest",
        "python3 -m pytest",
        "python -m pytest -m 'not cadence and not pending_fix'",
        "npm test",
        "npm run test",
        "yarn test",
        "cargo test",
        "go test ./...",
        "jest",
        "vitest run",
        "python3 -m unittest",
        "python3 -m unittest discover",
        "make test",
        "npm test -- src/thing.test.js",
        "pnpm run test -- src/lib/data/leak-suite.test.ts",
        "pnpm run test -- a.test.ts b.test.ts",
        "yarn test src/thing.test.js",
        "bun run test -- src/thing.test.js",
        "npm test -- -t some_name",
    ],
)
def test_subagent_suite_shaped_denied(repo, free_mutex, command):
    out = guard.check(_payload(command, repo, agent_id=_AGENT_ID))
    reason = _reason(out)
    assert reason.startswith("Run the tests you actually touched:")
    assert "reshaped so the command text parses differently" in reason


@pytest.mark.parametrize(
    "command",
    [
        "pytest coordinator_core/frontmatter/tests/test_x.py",
        "pytest coordinator_core/frontmatter/tests/test_x.py::test_case",
        "pytest -k schema_validate",
        "pytest -m 'not slow' coordinator_core/frontmatter/tests/test_x.py",
        "python3 -m pytest coordinator_core/bash_guards/tests/test_y.py -q",
        "cargo test my_filter",
        "go test ./pkg/thing",
        "go test",
        "jest src/thing.test.js",
        "python3 -m unittest pkg.mod.TestCase.test_x",
        "bun test src/thing.test.js",
    ],
)
def test_subagent_scoped_allowed(repo, free_mutex, command):
    assert guard.check(_payload(command, repo, agent_id=_AGENT_ID)) is None


def test_subagent_testpaths_root_is_not_a_scope(repo, free_mutex):
    """`pytest coordinator_core/` looks scoped and is the entire suite."""
    reason = _reason(guard.check(_payload("pytest coordinator_core/", repo, agent_id=_AGENT_ID)))
    assert "no test file, directory, or node-id scope" in reason


@pytest.mark.parametrize("command", ["pytest coordinator/", "pytest .", "pytest coordinator_core"])
def test_subagent_testpaths_ancestor_is_not_a_scope(repo, free_mutex, command):
    assert guard.check(_payload(command, repo, agent_id=_AGENT_ID)) is not None


def test_compound_command_hiding_a_suite_run_denied(repo, free_mutex):
    out = guard.check(
        _payload("cd /some/repo && python3 -m pytest 2>&1 | tee /tmp/log", repo, agent_id=_AGENT_ID)
    )
    assert _reason(out)


def test_wrapped_and_path_qualified_runner_denied(repo, free_mutex):
    out = guard.check(_payload("CI=1 timeout 900 .venv/bin/pytest", repo, agent_id=_AGENT_ID))
    assert _reason(out)


def test_flag_operand_is_not_credited_as_a_scope(repo, free_mutex):
    """`-m <expr>` selects markers, not paths — its operand must never be
    mistaken for a positional scope argument."""
    out = guard.check(_payload("pytest -m 'not cadence'", repo, agent_id=_AGENT_ID))
    assert _reason(out)


def test_non_test_command_allowed(repo, free_mutex):
    assert guard.check(_payload("git status --porcelain", repo, agent_id=_AGENT_ID)) is None
    assert guard.check(_payload("ls coordinator_core/", repo, agent_id=_AGENT_ID)) is None


def test_heredoc_prose_mentioning_pytest_is_not_misread_as_an_invocation(repo, free_mutex):
    """A heredoc body is stdin DATA, never shell command text. Once the
    shared tokenizer started treating a bare newline as a segment boundary
    (2026-07-30, closing the multi-line-command bypass), a heredoc body
    written to disk via `cat <<EOF ... EOF` would fragment into its own
    per-line segments unless heredoc bodies are stripped before
    classification -- and a line of prose starting with `pytest` would then
    misclassify as a live suite invocation. `_segment_argvs` strips heredoc
    bodies first (mirroring `block_worktree_creation.check()`), so this
    stays allowed."""
    cmd = (
        "cat <<'EOF' > review.md\n"
        "pytest coordinator_core/tests\n"
        "EOF"
    )
    assert guard.check(_payload(cmd, repo, agent_id=_AGENT_ID)) is None


# ---------------------------------------------------------------------------
# Package-script arg forwarding (2026-07-30 classifier correction)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base", ["npm", "pnpm", "yarn", "bun"])
@pytest.mark.parametrize(
    "args",
    [
        ["test"],
        ["t"],
        ["run", "test"],
        ["run-script", "test"],
    ],
)
def test_classify_package_manager_script_shape_is_unscoped_no_args(repo, base, args):
    """A bare package-script invocation (no forwarded args at all) is
    unscoped for every base -- including ``bun``, since with nothing
    forwarded there is no real scope for ``_classify_js_runner`` to find
    either."""
    label = guard._classify_package_manager(base, args, [], str(repo))
    assert label == "%s test" % base


@pytest.mark.parametrize("base", ["npm", "pnpm", "yarn"])
@pytest.mark.parametrize(
    "args",
    [
        ["test", "--", "src/thing.test.js"],
        ["run", "test", "--", "src/thing.test.js"],
        ["run-script", "test", "--", "src/thing.test.js"],
    ],
)
def test_classify_package_manager_script_shape_is_unscoped_with_args(repo, base, args):
    """The bug this fixes: a package-script invocation stays unscoped
    REGARDLESS of trailing args, for every base except bun's bare-`test`
    carve-out (covered separately below) -- the package manager is not
    obliged to forward those args to the runner."""
    label = guard._classify_package_manager(base, args, [], str(repo))
    assert label == "%s test" % base


def test_classify_package_manager_bun_run_test_is_unscoped_like_the_others(repo):
    """`bun run test -- <path>` is the SCRIPT form (not bun's built-in
    runner) and takes the unconditional-unscoped path like the other three
    package managers, even with a real path forwarded."""
    (repo / "src").mkdir()
    (repo / "src" / "thing.test.js").write_text("", encoding="utf-8")
    label = guard._classify_package_manager(
        "bun", ["run", "test", "--", "src/thing.test.js"], [], str(repo)
    )
    assert label == "bun test"


def test_classify_package_manager_bun_direct_runner_stays_scope_aware(repo):
    """`bun test <path>` invokes bun's own built-in runner directly, with no
    forwarding layer to lose the path across -- it must still find the real
    scope and return None."""
    (repo / "src").mkdir()
    (repo / "src" / "thing.test.js").write_text("", encoding="utf-8")
    label = guard._classify_package_manager(
        "bun", ["test", "src/thing.test.js"], [], str(repo)
    )
    assert label is None


def test_is_package_script_label():
    assert guard._is_package_script_label("npm test")
    assert guard._is_package_script_label("pnpm test")
    assert not guard._is_package_script_label("bun")
    assert not guard._is_package_script_label("pytest")
    assert not guard._is_package_script_label("jest")


def test_deny_offers_direct_runner_invocation_for_package_script(repo, free_mutex):
    out = guard.check(
        _payload("pnpm run test -- x.test.ts", repo, agent_id=_AGENT_ID)
    )
    reason = _reason(out)
    assert "pnpm exec vitest run" in reason


def test_deny_omits_package_script_offer_for_non_package_manager(repo, free_mutex):
    out = guard.check(_payload("pytest", repo, agent_id=_AGENT_ID))
    reason = _reason(out)
    assert "pnpm exec vitest run" not in reason


# ---------------------------------------------------------------------------
# tox/nox spelling-gap fix (2026-08-03) -- both `_RUNNER_PREFILTER_RE` and
# `_classify_tokens` bypassed BOTH the identity leg and the grant leg for a
# bare `tox`/`nox` invocation. Spec backlink: `_classify_tox_nox`'s own
# docstring, and the module docstring's 2026-08-03 classifier-correction
# note.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "tox",
        "tox -e py311",
        "tox -e py311,py312",
        "tox -- tests/test_x.py",
        "nox",
        "nox -s tests",
        "nox -s tests -- tests/test_x.py::test_case",
    ],
)
def test_classify_tox_nox_always_unscoped(command):
    """No shape of `tox`/`nox` -- bare, environment-scoped, or with a `--`
    posargs tail naming a real test file/node-id -- classifies as anything
    but unscoped. Environment/session selection is not a DR-088 Tier-T
    scope, and posargs-forwarding reliability is unverified (same class of
    risk as the pnpm arg-forwarding fix)."""
    matches = guard.classify_command(command)
    assert [m.tier for m in matches] == ["U"]


@pytest.mark.parametrize("command", ["tox", "tox -e py311", "nox", "nox -s tests"])
def test_subagent_tox_nox_denied(repo, free_mutex, command):
    """Layer-3 identity-leg regression: before the fix, neither runner was
    in the prefilter at all, so a dispatched subagent's bare `tox`/`nox`
    skipped this guard entirely -- no classification, no identity deny."""
    out = guard.check(_payload(command, repo, agent_id=_AGENT_ID))
    reason = _reason(out)
    assert reason.startswith("Run the tests you actually touched:")


@pytest.mark.parametrize("command", ["tox", "nox"])
def test_em_tox_nox_needs_tier_u_grant(grant_repo, free_mutex, command):
    """Tier-F/U grant-check-leg regression: before the fix, the top-level
    EM's `tox`/`nox` invocation never reached the grant leg at all -- it was
    silently allowed with no grant, no record, and no deny."""
    out = guard.check(_payload(command, grant_repo))
    reason = _reason(out)
    assert "Tier-U" in reason
    assert "authorization grant" in reason


@pytest.mark.parametrize("command", ["tox", "nox"])
def test_em_tox_nox_allowed_with_live_grant(grant_repo, free_mutex, command):
    _write_live_session(grant_repo, _GRANT_SID)
    assert grant_module.write_tier_u_grant(
        "pm", "yes, run the full suite", session_id=_GRANT_SID, cwd=str(grant_repo)
    )
    assert guard.check(_payload(command, grant_repo)) is None


def test_runner_prefilter_matches_tox_and_nox():
    """Negative-spec pin for the actual bypass mechanism: a future narrowing
    of `_RUNNER_PREFILTER_RE` that drops either token must fail loudly here,
    not silently reopen the whole-guard skip these two runners had."""
    assert guard._RUNNER_PREFILTER_RE.search("tox")
    assert guard._RUNNER_PREFILTER_RE.search("nox")


def test_runner_recognized_true_for_tox_and_nox():
    assert guard._runner_recognized(["tox"])
    assert guard._runner_recognized(["nox", "-s", "tests"])


# ---------------------------------------------------------------------------
# Top-level EM (no agent_id) + mutex leg
# ---------------------------------------------------------------------------

def test_top_level_em_allowed_when_mutex_free(repo, free_mutex):
    assert guard.check(_payload("pytest", repo)) is None


def test_top_level_em_denied_when_mutex_held(repo, held_mutex):
    reason = _reason(guard.check(_payload("pytest", repo)))
    assert reason.startswith("Wait for the in-flight suite run")
    assert "4242" in reason
    assert "em-session-abc" in reason
    assert "2026-07-23T10:00:00Z" in reason


def test_scoped_run_allowed_even_when_mutex_held(repo, held_mutex):
    cmd = "pytest coordinator_core/frontmatter/tests/test_x.py"
    assert guard.check(_payload(cmd, repo)) is None


def test_mutex_leg_fails_open_when_module_lacks_holder(repo, monkeypatch):
    """The mutex module is a sibling workstream; a partially-landed or absent
    one must degrade to "nobody holds it", never block every test command."""
    fake = types.ModuleType("coordinator_core.testing.suite_mutex")
    monkeypatch.setitem(sys.modules, "coordinator_core.testing.suite_mutex", fake)
    assert guard._mutex_holder() is None
    assert guard.check(_payload("pytest", repo)) is None


def test_mutex_leg_fails_open_when_holder_raises(repo, monkeypatch):
    fake = types.ModuleType("coordinator_core.testing.suite_mutex")

    def _boom():
        raise RuntimeError("mutex backend unavailable")

    fake.holder = _boom
    monkeypatch.setitem(sys.modules, "coordinator_core.testing.suite_mutex", fake)
    assert guard.check(_payload("pytest", repo)) is None


def test_subagent_leg_denies_even_when_mutex_raises(repo, monkeypatch):
    """Fail-open applies to the MUTEX leg only; identity stays fail-closed."""
    fake = types.ModuleType("coordinator_core.testing.suite_mutex")

    def _boom():
        raise RuntimeError("mutex backend unavailable")

    fake.holder = _boom
    monkeypatch.setitem(sys.modules, "coordinator_core.testing.suite_mutex", fake)
    assert guard.check(_payload("pytest", repo, agent_id=_AGENT_ID)) is not None


# ---------------------------------------------------------------------------
# Identity keying: presence of the TOP-LEVEL agent_id, nothing else
# ---------------------------------------------------------------------------

def test_nested_tool_response_agent_id_is_not_an_identity(repo, free_mutex):
    """A nested ``tool_response.agent_id`` must not false-positive a main-loop
    call — only the top-level key is an identity."""
    payload = _payload(
        "pytest", repo, extra={"tool_response": {"agent_id": _AGENT_ID}}
    )
    assert guard.check(payload) is None


def test_empty_agent_id_is_not_a_subagent(repo, free_mutex):
    assert guard.check(_payload("pytest", repo, agent_id="")) is None
    assert guard.check(_payload("pytest", repo, agent_id="   ")) is None


def test_workflow_shaped_agent_id_denied_without_any_backpointer(repo, free_mutex):
    """Presence-keying, not type-keying: a Workflow-phase agent has no
    dispatched-agents.txt row, so a type-keyed guard would exempt it."""
    assert guard.check(_payload("pytest", repo, agent_id="a" + "0f1e2d3c4b5a6978")) is not None


# ---------------------------------------------------------------------------
# Escape hatch + non-Bash payloads
# ---------------------------------------------------------------------------

def test_override_env_allows(repo, free_mutex, monkeypatch):
    monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
    assert guard.check(_payload("pytest", repo, agent_id=_AGENT_ID)) is None


def test_override_env_read_inline_not_at_import(repo, free_mutex, monkeypatch):
    monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "0")
    assert guard.check(_payload("pytest", repo, agent_id=_AGENT_ID)) is not None
    monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
    assert guard.check(_payload("pytest", repo, agent_id=_AGENT_ID)) is None


def test_non_bash_tool_allowed(repo, free_mutex):
    payload = _payload("pytest", repo, agent_id=_AGENT_ID)
    payload["tool_name"] = "Write"
    assert guard.check(payload) is None


def test_malformed_payload_allowed(repo, free_mutex):
    assert guard.check({"tool_name": "Bash"}) is None
    assert guard.check({"tool_name": "Bash", "tool_input": {}}) is None
    assert guard.check({"tool_name": "Bash", "tool_input": {"command": ""}}) is None


# ---------------------------------------------------------------------------
# Configured fast_test_cmd / full_test_cmd equality leg
# ---------------------------------------------------------------------------

def test_configured_cmd_leg_reuses_the_canonical_resolver(repo, free_mutex, monkeypatch):
    """A command the generic classifier would allow is still denied when it
    reproduces a configured tier verbatim."""
    configured = "pytest coordinator_core/frontmatter/tests/test_x.py"
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd("fast_test_cmd", configured, 0)],
    )
    # The generic classifier reads this as Tier T (a real path scope) and
    # allows it; the equality leg still catches it as a configured tier.
    assert guard._classify_tokens(
        guard._tokens(configured), ["coordinator_core"], str(repo)
    ) is None
    out = guard.check(_payload(configured, repo, agent_id=_AGENT_ID))
    assert "configured fast_test_cmd" in _reason(out)


def test_configured_cmd_leg_ignores_unrelated_commands(repo, free_mutex, monkeypatch):
    monkeypatch.setattr(
        guard,
        "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd(
            "fast_test_cmd", "pytest coordinator_core/frontmatter/tests/test_x.py", 0
        )],
    )
    other = "pytest coordinator_core/bash_guards/tests/test_y.py"
    assert guard.check(_payload(other, repo, agent_id=_AGENT_ID)) is None


def test_configured_cmd_leg_degrades_silently_without_a_resolver(tmp_path):
    assert guard._configured_test_cmds(str(tmp_path)) == []
    assert guard._configured_test_cmds(None) == []


# ---------------------------------------------------------------------------
# Regression: a malformed `configured` (e.g. bound to a bare string instead
# of a list of `ConfiguredCmd` 3-tuples) must degrade this belt-and-braces leg
# to "no match" rather than crash the whole PreToolUse(Bash) guard chain. This
# is the exact shape of a fleet-wide guard crash reported by a sibling repo
# (`ValueError: not enough values to unpack (expected 2, got 1)`), caused by
# `configured` transiently being a bare `repo_root` string during a prior
# refactor's edit window -- iterating a string yields 1-char items.
# ---------------------------------------------------------------------------

def test_matches_configured_cmd_does_not_raise_on_malformed_configured():
    assert guard._matches_configured_cmd(
        [["pytest", "coordinator_core"]], "/some/repo/root"
    ) is None


def test_classify_command_core_does_not_raise_on_malformed_configured():
    matches = guard._classify_command_core(
        "pytest coordinator_core", None, [], "/some/repo/root"
    )
    assert isinstance(matches, list)


def test_classify_command_core_chained_fast_test_cmd_classifies_both_segments_as_tier_f():
    """Regression (2026-07-25 cockpit Tier-F-unreachable report), UPDATED
    2026-07-25 for R1 (cross-repo/inbox/2026-07-25-example-doctrine-repo-em-validate-
    tier-u-shape-ruling.md): with ``fast_test_cmd`` chained (``pnpm run
    typecheck && pnpm run test``) and invoked verbatim, BOTH segments are
    matched by the configured-cmd containment leg (not just the trailing
    ``pnpm run test`` segment the generic classifier alone would see).
    Originally this test asserted both classified Tier F unconditionally
    -- that was the classify-by-key bug R1 closed. Per-segment shape now
    decides: ``pnpm run typecheck`` is not itself suite-shaped (the
    generic classifier's ``_classify_package_manager`` only recognizes a
    ``test``-named script), so its shape is "scoped" in the narrow sense
    of "not a runner invocation at all" and it classifies Tier F.
    ``pnpm run test`` HAS no scope-narrowing shape this classifier
    understands (unlike pytest's file/dir/node-id args) -- it is always
    the unscoped-runner shape -- so it now correctly classifies Tier U,
    not Tier F, even though it verbatim-matches the configured
    fast_test_cmd. The segment-set containment matching itself (both
    segments recognized as belonging to the one chained configured
    command) is still exercised here and still correct -- only the
    resulting per-segment TIER changed."""
    chained = "pnpm run typecheck && pnpm run test"
    matches = guard._classify_command_core(
        chained, None, [], [guard.ConfiguredCmd("fast_test_cmd", chained, 0)]
    )
    assert len(matches) == 2
    by_text = {m.matched_text: m.tier for m in matches}
    assert by_text["pnpm run typecheck"] == "F"
    assert by_text["pnpm run test"] == "U"


def test_classify_command_core_explicit_tie_classifies_tier_u():
    """A repo that EXPLICITLY declares full_test_cmd identical to
    fast_test_cmd (resolver rc=0 for both -- not the rc=3 fallback) still
    classifies Tier U, deliberately (DR-088's unscoped-runner-invocation
    disjunct) -- see ``ConfiguredCmd``'s docstring and the tie-break comment
    in ``_classify_command_core``."""
    cmd = "python3 -m pytest coordinator_core/"
    matches = guard._classify_command_core(
        cmd, None, [],
        [
            guard.ConfiguredCmd("fast_test_cmd", cmd, 0),
            guard.ConfiguredCmd("full_test_cmd", cmd, 0),
        ],
    )
    assert len(matches) == 1
    assert matches[0].tier == "U"


def test_classify_command_core_fallback_tie_classifies_tier_u():
    """A tie arising from the resolver's FALLBACK (full_test_cmd unconfigured,
    resolver returns the fast tier's own string with rc=3) also classifies
    Tier U -- the first test in this repo to exercise rc=3 at all."""
    cmd = "python3 -m pytest coordinator_core/"
    matches = guard._classify_command_core(
        cmd, None, [],
        [
            guard.ConfiguredCmd("fast_test_cmd", cmd, 0),
            guard.ConfiguredCmd("full_test_cmd", cmd, 3),
        ],
    )
    assert len(matches) == 1
    assert matches[0].tier == "U"


def test_matches_configured_cmd_still_matches_well_formed_configured():
    configured = "pytest coordinator_core/frontmatter/tests/test_x.py"
    segments_argv = [guard._tokens(configured)]
    result = guard._matches_configured_cmd(
        segments_argv, [guard.ConfiguredCmd("fast_test_cmd", configured, 0)]
    )
    assert result == "the repo's configured fast_test_cmd"


def test_matches_configured_cmd_containment_for_a_chained_configured_command():
    """Regression (2026-07-25 cockpit Tier-F-unreachable report): a chained
    configured command (``pnpm run typecheck && pnpm run test``) is matched
    by segment-set containment -- every one of ITS segments present
    somewhere in the invocation's segment set -- not by impossible
    whole-string equality against a single invocation segment."""
    configured = "pnpm run typecheck && pnpm run test"
    segments_argv = [guard._tokens("pnpm run typecheck"), guard._tokens("pnpm run test")]
    result = guard._matches_configured_cmd(
        segments_argv, [guard.ConfiguredCmd("fast_test_cmd", configured, 0)]
    )
    assert result == "the repo's configured fast_test_cmd"


def test_matches_configured_cmd_containment_requires_all_configured_segments():
    """Only running HALF of a chained configured command must not match --
    containment requires every configured segment to be present, not just
    one of them."""
    configured = "pnpm run typecheck && pnpm run test"
    segments_argv = [guard._tokens("pnpm run test")]
    result = guard._matches_configured_cmd(
        segments_argv, [guard.ConfiguredCmd("fast_test_cmd", configured, 0)]
    )
    assert result is None


# ---------------------------------------------------------------------------
# Regression: sys.modules registration before exec_module (the resolver
# module uses `@dataclass` at module scope, same as the real
# coordinator-resolve-validation-cmd.py; on Python versions where dataclasses'
# `sys.modules.get(cls.__module__)` lookup fires during `exec_module`, a
# module never registered in sys.modules raises there, and the blanket
# `except Exception: return []` swallows it -- silently collapsing every
# Tier-F/Tier-U distinction to Tier U).
# ---------------------------------------------------------------------------

# The `@dataclass` decorator here is the load-bearing part of this fixture:
# without a module-scope dataclass this resolver would import cleanly even
# with the sys.modules-registration bug present, and the regression test
# would pass whether or not the bug was fixed.
_MINIMAL_RESOLVER_SRC = '''\
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ResolveResult:
    stdout: str
    returncode: int
    stderr: str = ""


def resolve_fast_test_cmd(repo_root):
    return ResolveResult(stdout="python3 -m pytest -m 'not cadence'", returncode=0)


def resolve_full_test_cmd(repo_root):
    return ResolveResult(stdout="python3 -m pytest", returncode=0)
'''


def _write_minimal_resolver(repo_root):
    bin_dir = Path(repo_root) / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "coordinator-resolve-validation-cmd.py").write_text(
        _MINIMAL_RESOLVER_SRC, encoding="utf-8"
    )


def test_configured_test_cmds_resolves_both_tiers_via_a_real_resolver_module(tmp_path):
    _write_minimal_resolver(tmp_path)
    tiers = {entry.tier: entry.cmd for entry in guard._configured_test_cmds(str(tmp_path))}
    assert tiers["fast_test_cmd"] == "python3 -m pytest -m 'not cadence'"
    assert tiers["full_test_cmd"] == "python3 -m pytest"


def test_configured_test_cmds_native_resolving_one_tier_still_gets_the_other_via_by_path(
    tmp_path, monkeypatch
):
    """Review: code-reviewer — Finding 2 regression. A native leg that
    resolves only ONE tier must not discard the by-path shim's coverage of
    the other -- fallback is per-tier, not all-or-nothing."""
    _write_minimal_resolver(tmp_path)
    monkeypatch.setattr(
        guard, "_configured_test_cmds_native",
        lambda root: [guard.ConfiguredCmd("fast_test_cmd", "native fast cmd", 0)],
    )
    tiers = {entry.tier: entry.cmd for entry in guard._configured_test_cmds(str(tmp_path))}
    # The native-resolved tier is kept as-is (by-path is never asked for it).
    assert tiers["fast_test_cmd"] == "native fast cmd"
    # The tier native did NOT resolve is filled in from the by-path shim.
    assert tiers["full_test_cmd"] == "python3 -m pytest"


def test_configured_test_cmds_resolved_at_most_once_per_check_call(repo, free_mutex, monkeypatch):
    """A command that is suite-shaped ONLY via the configured-cmd-equality
    leg (the generic classifier alone says "scoped") must resolve
    ``_configured_test_cmds`` exactly once per ``check()`` call, not once in
    ``_matches_configured_cmd`` and again in ``_matched_tiers``."""
    calls = []
    configured_cmd = "pytest coordinator_core/frontmatter/tests/test_x.py"

    def _counting(root):
        calls.append(root)
        return [guard.ConfiguredCmd("fast_test_cmd", configured_cmd, 0)]

    monkeypatch.setattr(guard, "_configured_test_cmds", _counting)
    # The generic classifier reads this as Tier T (a real path scope) and
    # allows it; only the configured-cmd equality leg catches it as the
    # whole suite -- this is the shape that reaches BOTH call sites.
    assert guard._classify_tokens(
        guard._tokens(configured_cmd), ["coordinator_core"], str(repo)
    ) is None
    guard.check(_payload(configured_cmd, repo))
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Tier-U authorization-grant leg (DR-088 layer 5)
#
# Unlike the fixtures above (which monkeypatch `resolve_git_root` to avoid
# needing a real repo), the grant leg's session resolution
# (``coordinator_core.session.grant.check_tier_u_grant`` ->
# ``core.resolve_session_id``/``core.session_dir``) shells out to real git
# for the common-dir, so these fixtures build an ACTUAL tmp git repo -- same
# idiom as ``coordinator_core/session/tests/test_grant.py``'s ``_make_repo``.
# ---------------------------------------------------------------------------

def _make_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _write_live_session(repo, sid):
    """A session whose meta.json makes it read LIVE (fresh last_activity,
    Layer-2 recency path -- same shape as test_grant.py's ``_live_session``)."""
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(
        json.dumps({"pid": "999", "last_activity": session_core.now_iso()}),
        encoding="utf-8",
    )
    return sdir


@pytest.fixture
def grant_repo(tmp_path, monkeypatch):
    """A real tmp git repo with the calling session id pinned via
    ``COORDINATOR_SESSION_ID`` (tier-1 of ``resolve_session_id``'s chain), no
    grant written yet."""
    repo = _make_git_repo(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _GRANT_SID)
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    return repo


class TestGrantLeg:
    def test_em_tier_u_no_grant_denied(self, grant_repo, free_mutex):
        """AC-1: EM, Tier-U command, no grant -> deny naming the grant."""
        out = guard.check(_payload("pytest", grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason

    def test_em_tier_u_live_grant_allowed(self, grant_repo, free_mutex):
        """AC-2: a live grant for THIS session allows the same command."""
        _write_live_session(grant_repo, _GRANT_SID)
        assert grant_module.write_tier_u_grant(
            "pm", "yes, run the full suite", session_id=_GRANT_SID, cwd=str(grant_repo)
        )
        assert guard.check(_payload("pytest", grant_repo)) is None

    def test_em_tier_f_no_grant_denied(self, grant_repo, free_mutex, monkeypatch):
        """AC-4 (2026-08-04 PM ruling, tier-f-is-grant-gated): the configured
        fast_test_cmd, matched verbatim AND genuinely SCOPED in shape, is
        Tier F -- and Tier F is no longer exempt from this leg. Prior to
        2026-08-04 this test asserted the opposite (Tier F ungated even with
        zero grant); the PM ruled the grant ask is the escape hatch for
        every suite tier, so a Tier-F match with no live grant now denies
        exactly like Tier U."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", "pytest tests/test_x.py", 0)],
        )
        out = guard.check(_payload("pytest tests/test_x.py", grant_repo))
        reason = _reason(out)
        assert "authorization grant" in reason

    def test_em_tier_f_with_live_grant_allowed(self, grant_repo, free_mutex, monkeypatch):
        """AC-4: the same Tier-F command IS allowed once the calling
        session holds a live Tier-U grant -- the grant leg gates Tier F
        through the SAME grant record as Tier U, there is no separate
        Tier-F grant."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", "pytest tests/test_x.py", 0)],
        )
        _write_live_session(grant_repo, _GRANT_SID)
        assert grant_module.write_tier_u_grant(
            "pm", "yes, run the fast tier", session_id=_GRANT_SID, cwd=str(grant_repo)
        )
        assert guard.check(_payload("pytest tests/test_x.py", grant_repo)) is None

    def test_em_tier_f_with_fast_tier_unscoped_declaration_still_denied(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """AC-4: a repo carrying ``fast_tier_unscoped_reason`` still requires
        a grant for a genuinely-scoped Tier-F command -- the R6 declaration
        exit is gated to the Tier-U leg only (PM-ruled 2026-08-04: no Tier-F
        equivalent exists or is to be added), so it must not discharge Tier
        F even when the declaration is present and would have matched had
        this command been Tier U."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", "pytest tests/test_x.py", 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        out = guard.check(_payload("pytest tests/test_x.py", grant_repo))
        reason = _reason(out)
        assert "authorization grant" in reason

    def test_em_tier_f_chained_no_grant_denied_as_tier_u(self, grant_repo, free_mutex, monkeypatch):
        """Regression (2026-07-25 cockpit Tier-F-unreachable report),
        UPDATED 2026-07-25 for R1 (cross-repo/inbox/2026-07-25-example-doctrine-repo-
        em-validate-tier-u-shape-ruling.md): a CHAINED configured
        fast_test_cmd (``pnpm run typecheck && pnpm run test``), invoked
        verbatim, still has BOTH segments recognized by the configured-cmd
        containment leg (not just the trailing ``pnpm run test`` segment a
        naive per-segment scan would see) -- that containment-matching fix
        is still correct and still covered. But ``pnpm run test`` has no
        scope-narrowing shape this classifier understands, so it is an
        unscoped-runner SHAPE regardless of matching the configured key --
        R1 means this now correctly denies as Tier U (a misconfigured
        fast_test_cmd per R2), not Tier F."""
        chained = "pnpm run typecheck && pnpm run test"
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", chained, 0)],
        )
        out = guard.check(_payload(chained, grant_repo))
        assert out is not None
        assert "Tier-U" in _reason(out)

    def test_em_chained_fast_test_cmd_half_still_denied_as_tier_u(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Running only HALF of a chained fast_test_cmd (``pnpm run test``
        alone, omitting ``pnpm run typecheck``) must still be denied as
        Tier U -- segment-set containment requires ALL of the configured
        command's segments to be present, not just one."""
        chained = "pnpm run typecheck && pnpm run test"
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", chained, 0)],
        )
        out = guard.check(_payload("pnpm run test", grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason

    def test_em_chained_full_test_cmd_verbatim_still_denied_as_tier_u(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """A chained configured ``full_test_cmd``, invoked verbatim, is
        Tier U (not Tier F) -- ``full_test_cmd`` is always grant-gated
        regardless of how many segments it contains."""
        chained = "pnpm run typecheck && pnpm run test"
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("full_test_cmd", chained, 0)],
        )
        out = guard.check(_payload(chained, grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason

    def test_em_tier_u_explicit_tie_denied_no_fast_route_named(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Regression: with fast_test_cmd and full_test_cmd EXPLICITLY
        declared identical (resolver rc=0 for both), ``check()`` still
        DENIES as Tier U through the full deny path -- not merely a "U"
        label from ``classify_command`` -- proving the widened
        ``ConfiguredCmd`` shape survives BOTH shape-guards
        (``_matches_configured_cmd`` and ``_classify_command_core``'s
        ``well_formed`` filter) in lockstep. Before the fix, a shape
        mismatch between the two guards would silently degrade this leg to
        "no configured-cmd match" for every repo while every existing test
        stayed green.

        Also asserts the deny's remediation text omits the unreachable
        ``fast_test_cmd`` Tier-F route and names the tie explicitly."""
        cmd = "pytest"
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [
                guard.ConfiguredCmd("fast_test_cmd", cmd, 0),
                guard.ConfiguredCmd("full_test_cmd", cmd, 0),
            ],
        )
        out = guard.check(_payload(cmd, grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason
        assert "ungated by this leg" not in reason
        assert "Tier F, ungated" not in reason
        assert "No Tier-F escape" in reason

    def test_em_tier_u_fallback_tie_denied(self, grant_repo, free_mutex, monkeypatch):
        """A tie arising from the resolver's FALLBACK (full_test_cmd
        unconfigured, resolver returns the fast tier's own string with
        rc=3) also denies as Tier U through ``check()``, same as the
        explicit-declaration tie above."""
        cmd = "pytest"
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [
                guard.ConfiguredCmd("fast_test_cmd", cmd, 0),
                guard.ConfiguredCmd("full_test_cmd", cmd, 3),
            ],
        )
        out = guard.check(_payload(cmd, grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason
        assert "ungated by this leg" not in reason
        assert "No Tier-F escape" in reason

    def test_em_tier_u_non_tie_deny_no_longer_names_fast_route(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Regression (tier-f-is-grant-gated C3, the SECOND dud-offer this
        function has shipped): a NON-tie Tier-U deny (no configured
        fast_test_cmd matches this command at all) is the highest-traffic
        refusal in the fleet, and after C1/C2 land the fast suite has no
        live Tier-F escape at all -- naming it as an alternative here would
        be false on this path exactly as it was on the tie path. Prior to
        2026-08-04 this test asserted the opposite (the fast_test_cmd line
        present); the PM ruling that the grant ask IS the escape hatch means
        neither branch may offer it any more."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", "pytest -m 'not cadence'", 0)],
        )
        out = guard.check(_payload("pytest", grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "ungated by this leg" not in reason
        assert "Tier F, ungated" not in reason
        assert "No Tier-F escape" not in reason
        assert "authorization grant" in reason.lower()

    def test_em_tier_f_only_deny_no_dead_fast_route(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """AC6 regression net: a Tier-F-ONLY match (no Tier-U match in the
        same invocation) must not advertise the repo's configured
        fast_test_cmd as an ungated alternative either -- the dead
        ``fast_route`` offer is gone from BOTH branches ``check()`` can
        reach after the 2026-08-04 flip, not only the tie/Tier-U path this
        function's docstring already recorded once before."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", "pytest tests/test_x.py", 0)],
        )
        out = guard.check(_payload("pytest tests/test_x.py", grant_repo))
        reason = _reason(out)
        assert "ungated by this leg" not in reason
        assert "Tier F, ungated" not in reason
        assert "authorization grant" in reason.lower()
        assert "tier-u-grant-cli grant pm" in reason

    def test_real_resolver_module_configured_cmd_leg_resolves_via_real_module(
        self, grant_repo, free_mutex
    ):
        """Regression: with the sys.modules-registration bug present, a real
        resolver module (module-scope `@dataclass`, same as production)
        raised inside `exec_module`, `_configured_test_cmds` silently
        degraded to `[]`. Confirms the configured-cmd leg resolves via a
        real resolver file (not just a monkeypatched stub) -- i.e. the
        fast_test_cmd match is genuinely recognized, not silently dropped.

        Updated 2026-07-25 for R1 (cross-repo/inbox/2026-07-25-example-doctrine-repo-
        em-validate-tier-u-shape-ruling.md): the fixture's fast_test_cmd
        (``python3 -m pytest -m 'not cadence'``, a marker filter -- not a
        file/dir/node-id scope) is an unscoped-runner SHAPE, so it now
        correctly denies as Tier U (R2: a repo whose fast_test_cmd is
        unscoped has no reachable Tier F) rather than being silently
        allowed as Tier F. The original assertion (allowed, no denial)
        encoded the classify-by-key bug R1 closed."""
        _write_minimal_resolver(grant_repo)
        fast_cmd = "python3 -m pytest -m 'not cadence'"
        out = guard.check(_payload(fast_cmd, grant_repo))
        assert out is not None
        assert "Tier-U" in _reason(out)

    def test_tier_t_scoped_no_grant_allowed(self, grant_repo, free_mutex):
        """AC-4: a scoped (Tier T) command is unaffected by this leg."""
        cmd = "pytest path/to/test_x.py"
        assert guard.check(_payload(cmd, grant_repo)) is None

    def test_subagent_tier_u_with_live_grant_still_denied_as_subagent(self, grant_repo, free_mutex):
        """AC-5: a live grant never buys a subagent a suite run -- the
        subagent reason wins, not the grant reason."""
        _write_live_session(grant_repo, _GRANT_SID)
        assert grant_module.write_tier_u_grant(
            "pm", "yes", session_id=_GRANT_SID, cwd=str(grant_repo)
        )
        out = guard.check(_payload("pytest", grant_repo, agent_id=_AGENT_ID))
        reason = _reason(out)
        assert reason.startswith("Run the tests you actually touched:")

    def test_grant_leg_fails_open_only_on_import_error(self, grant_repo, free_mutex, monkeypatch):
        """AC-6: an ImportError (the module genuinely absent) degrades to
        ALLOW -- an uninstalled sibling workstream must not wedge the
        machine."""
        real_import = __import__

        def _fake_import(name, *args, **kwargs):
            if name == "coordinator_core.session":
                raise ImportError("no module named coordinator_core.session")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _fake_import)
        assert guard.check(_payload("pytest", grant_repo)) is None

    def test_grant_leg_does_not_fail_open_on_a_non_import_error(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """AC-6 (reversed): ``check_tier_u_grant`` documents "this function
        never raises" -- an unexpected exception OTHER than ImportError is a
        defect in the authority control itself and must surface, not be
        silently converted into a grant. This replaces the old fails-open
        behavior this test used to assert (deliberately reversed -- see
        Finding 1 of the dr088-tier-u-grant review)."""

        def _boom(cwd=None, *, session_id=None):
            raise RuntimeError("grant backend unavailable")

        monkeypatch.setattr(grant_module, "check_tier_u_grant", _boom)
        with pytest.raises(RuntimeError, match="grant backend unavailable"):
            guard.check(_payload("pytest", grant_repo))

    def test_grant_reason_wins_over_mutex_when_both_apply(self, grant_repo, held_mutex):
        """AC-7: ordering -- grant denial precedes the mutex leg. A session
        with no standing to ask is told so, not told to wait."""
        out = guard.check(_payload("pytest", grant_repo))
        reason = _reason(out)
        assert "authorization grant" in reason
        assert "Wait for the in-flight suite run" not in reason


# ---------------------------------------------------------------------------
# R6 declaration exit (DR-088 amendment, 2026-07-25) -- a repo may declare its
# fast tier legitimately unscoped via `fast_tier_unscoped_reason` in
# coordinator.local.md. This is the fix for the live repro: claude-klabauter's own
# `fast_test_cmd` (a marker-based filter, no path/node-id scope) classifies
# Tier U by shape (R1/R2) and, absent this leg, denied the top-level EM
# outright even though `coordinator.local.md` already carries the
# declaration and names `check_test_suite_invocation.py` as its intended
# consumer.
# ---------------------------------------------------------------------------

_CLAUDE_KLABAUTER_FAST_TEST_CMD = (
    "python3 -m pytest -m 'not cadence and not pending_fix and not designed_red' -n auto"
)


class TestR6DeclaredUnscopedFastTier:
    def test_declared_unscoped_fast_tier_verbatim_allowed_no_grant(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """The live repro: claude-klabauter's own configured fast_test_cmd, invoked
        VERBATIM by the top-level EM with the declaration present and no
        Tier-U grant on disk, is allowed."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        assert guard.check(_payload(_CLAUDE_KLABAUTER_FAST_TEST_CMD, grant_repo)) is None

    def test_declared_unscoped_fast_tier_double_quoted_marker_expr_allowed(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Quoting-style variance (double quotes around the -m expression
        instead of the configured single quotes) still matches -- both
        sides are shlex-tokenized, so the quote CHARACTER never enters the
        comparison."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        double_quoted = (
            'python3 -m pytest -m "not cadence and not pending_fix and not designed_red" -n auto'
        )
        assert guard.check(_payload(double_quoted, grant_repo)) is None

    def test_declared_unscoped_fast_tier_extra_inner_whitespace_allowed(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Incidental extra whitespace INSIDE the quoted -m expression (a
        reproduction artifact, not a semantic change to the marker filter)
        still matches -- this is the actual bug: shlex preserves internal
        whitespace verbatim, so two runs of whitespace previously failed
        tuple equality even after the quoting-style fix."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        extra_ws = (
            "python3 -m pytest  -m 'not cadence  and not pending_fix and  not designed_red'   -n auto"
        )
        assert guard.check(_payload(extra_ws, grant_repo)) is None

    def test_absent_declaration_denies_the_same_unscoped_fast_test_cmd(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Without the declaration, the identical command is still denied
        as Tier U -- the declaration is what discharges the authority
        check, not the shape match alone."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(guard, "_fast_tier_unscoped_declaration", lambda root: "")
        out = guard.check(_payload(_CLAUDE_KLABAUTER_FAST_TEST_CMD, grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason

    def test_bare_pytest_still_denied_despite_declaration(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """The declaration covers ONLY the literal (token-normalized)
        fast_test_cmd -- a bare, genuinely unscoped `pytest` invocation
        (never matching the configured command at all) must still deny."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        out = guard.check(_payload("pytest", grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason

    def test_scoped_dir_pytest_still_denied_despite_declaration(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """`pytest coordinator_core/` -- a DIFFERENT command from the
        configured fast_test_cmd -- must still deny even with the
        declaration present; the declaration is not a blanket exemption for
        any invocation of the runner."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        out = guard.check(_payload("pytest coordinator_core/", grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason

    def test_superset_invocation_still_denied_despite_declaration(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Appending an extra flag to the configured fast_test_cmd (a
        superset invocation in the SAME segment) must not silently pass --
        the declaration's shape-match is exact-segment containment, not a
        prefix/fuzzy match, so an extra token changes the segment's own
        argv tuple and the containment check no longer finds it."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        superset = _CLAUDE_KLABAUTER_FAST_TEST_CMD + " --extra-unscoped-thing"
        out = guard.check(_payload(superset, grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason

    def test_declared_unscoped_fast_tier_still_denied_when_mutex_held(
        self, grant_repo, held_mutex, monkeypatch
    ):
        """The R6 declaration discharges the AUTHORITY (grant) leg only --
        it must not bypass the mutex (resource) leg."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        out = guard.check(_payload(_CLAUDE_KLABAUTER_FAST_TEST_CMD, grant_repo))
        reason = _reason(out)
        assert "Wait for the in-flight suite run" in reason

    def test_declared_unscoped_fast_tier_never_widens_subagent_rung(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """A subagent invoking the exact declared fast_test_cmd is still
        denied at the identity leg -- R6 is EM-only by construction (the
        identity leg runs first and denies every subagent outright)."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        out = guard.check(_payload(_CLAUDE_KLABAUTER_FAST_TEST_CMD, grant_repo, agent_id=_AGENT_ID))
        reason = _reason(out)
        assert reason.startswith("Run the tests you actually touched:")

    def test_declaration_never_covers_full_test_cmd(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """The declaration's reach is `fast_test_cmd` ONLY -- an identical
        string configured under `full_test_cmd` (not `fast_test_cmd`) must
        still deny, since `_matches_declared_fast_test_cmd` filters `configured`
        to the `fast_test_cmd` tier before testing containment."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("full_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        out = guard.check(_payload(_CLAUDE_KLABAUTER_FAST_TEST_CMD, grant_repo))
        reason = _reason(out)
        assert "Tier-U" in reason
        assert "authorization grant" in reason

    def test_chained_configured_fast_test_cmd_still_tier_u_even_with_declaration(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """DR-088-correctness regression: a CHAINED configured value must
        stay denied as Tier U regardless of the R6 declaration -- the
        declaration exit is scoped to the fast_test_cmd match test, but a
        chained command still requires ALL its segments present, and this
        guards that a declaration cannot be (mis)used to launder a chained
        command through as well."""
        chained = "pnpm run typecheck && pnpm run test"
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", chained, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "chained fast tier for demonstration purposes",
        )
        out = guard.check(_payload(chained, grant_repo))
        # A chained fast_test_cmd is still recognized by containment and,
        # since the declaration covers it too, allowed here -- the DR-088
        # single-command amendment forbids CONFIGURING a chained
        # fast_test_cmd in the first place (a repo-config violation), it
        # does not add a second guard-side prohibition on top of R6's
        # shape/token match. Assert the containment leg still requires
        # every segment (unchanged from the pre-R6 behaviour) by running
        # only half the chain.
        assert out is None
        half = "pnpm run test"
        out_half = guard.check(_payload(half, grant_repo))
        reason_half = _reason(out_half)
        assert "Tier-U" in reason_half

    def test_chained_declared_fast_test_cmd_verbatim_still_allowed(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Regression (EM prior art, tierf-s2-guards Finding 1 fix-up): a
        repo whose OWN declared ``fast_test_cmd`` is itself a two-segment
        chained command (cockpit's real case: ``pnpm run typecheck && pnpm
        run test``), invoked VERBATIM with the R6 declaration present and
        no Tier-U grant, must stay ALLOWED -- this is the exact structural
        lockout
        ``cross-repo/archive/2026-07-25-example-cockpit-repo-em-tier-f-escape-
        hatch-unreachable-for-chained-fast-test-cmd.md`` (realized_by
        8d94ebb9) fixed, and the set-equality tightening for Finding 1 must
        not rebreak it: the invocation's segment set equals the declared
        command's segment set here, so it still satisfies the exact match."""
        chained = "pnpm run typecheck && pnpm run test"
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", chained, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        assert guard.check(_payload(chained, grant_repo)) is None

    def test_chained_declared_fast_test_cmd_plus_scoped_extra_segment_still_denied(
        self, grant_repo, free_mutex, monkeypatch
    ):
        """Regression (code-reviewer Finding 1, tierf-s2-guards): a chained
        invocation whose FIRST segment is the bare declared fast_test_cmd
        (satisfying R6's exact-match test on its own) and whose SECOND
        segment is a DIFFERENT, scoped invocation of the same runner (a
        test path appended, Tier F) must NOT be discharged by the R6
        declaration -- the declaration speaks for exactly the declared
        string, never for an extra segment it never named. Before the fix,
        ``"U" in matched_tiers`` was True (the first segment is Tier U by
        shape) and ``_matches_declared_fast_test_cmd`` was satisfied by
        one-directional containment, so the WHOLE chain -- including the
        Tier-F second segment -- was allowed with zero grant on disk."""
        monkeypatch.setattr(
            guard, "_configured_test_cmds",
            lambda root: [guard.ConfiguredCmd("fast_test_cmd", _CLAUDE_KLABAUTER_FAST_TEST_CMD, 0)],
        )
        monkeypatch.setattr(
            guard, "_fast_tier_unscoped_declaration",
            lambda root: "marker-based fast/full split; no path subset is meaningful",
        )
        chained = _CLAUDE_KLABAUTER_FAST_TEST_CMD + " && " + _CLAUDE_KLABAUTER_FAST_TEST_CMD + " tests/test_x.py"
        out = guard.check(_payload(chained, grant_repo))
        reason = _reason(out)
        assert out is not None
        assert "authorization grant" in reason


class TestConfiguredCmdReachability:
    """The configured-tier leg must fire in a CONSUMER repo -- one that
    declares ``fast_test_cmd``/``full_test_cmd`` and does not host
    ``coordinator/bin/``.

    Regression cover for the three defects that together made a repo's own
    declared whole-suite command classify Tier T (ungated for subagents, no
    grant required of the EM) purely because it names a directory:

      1. the resolver was loaded only from ``<repo_root>/coordinator/bin/``,
         which exists in claude-klabauter alone -- every other repo silently
         got no configured commands at all;
      2. the head token was compared by basename, so a repo DECLARING
         ``python -m pytest …`` (which the resolver normalizes to
         ``python3``) never matched the operator typing ``python``;
      3. the match was whole-segment equality, so any incidental ``-q`` /
         ``--tb=short`` / ``2>&1`` slipped the leg entirely.
    """

    @pytest.fixture
    def declared_repo(self, tmp_path, monkeypatch):
        """A consumer-shaped repo: declares both tiers in
        ``coordinator.local.md``, has NO ``coordinator/bin/``."""
        (tmp_path / "coordinator.local.md").write_text(
            "---\n"
            "project_type: general\n"
            "fast_test_cmd: python -m pytest coordinator/tests\n"
            "full_test_cmd: python -m pytest coordinator/tests\n"
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        return tmp_path

    def test_resolves_without_coordinator_bin(self, declared_repo):
        assert not (declared_repo / "coordinator" / "bin").exists()
        tiers = {c.tier for c in guard._configured_test_cmds(str(declared_repo))}
        assert tiers == {"fast_test_cmd", "full_test_cmd"}

    @pytest.mark.parametrize("command", [
        "python -m pytest coordinator/tests",
        "python3 -m pytest coordinator/tests",
        "timeout 600 python -m pytest coordinator/tests -q 2>&1 | tail -20",
        ".venv/bin/python -m pytest coordinator/tests -x --tb=short",
    ])
    def test_declared_suite_denied_for_subagent(self, declared_repo, free_mutex,
                                                command):
        reason = _reason(guard.check(
            _payload(command, declared_repo, agent_id=_AGENT_ID)))
        assert "configured" in reason

    @pytest.mark.parametrize("command", [
        "python -m pytest",
        "timeout 600 python -m pytest -q 2>&1 | tail -20",
    ])
    def test_declared_suite_needs_grant_for_em(self, declared_repo, free_mutex,
                                               monkeypatch, command):
        """DR-088 R1 (EM-ratified 2026-07-25 amendment, ``docs/decisions/
        DR-088-test-breadth-ladder-tiered-invocation-authority.md``): tier is
        a property of invocation SHAPE, and a tie between ``fast_test_cmd``
        and ``full_test_cmd`` is explicitly NOT the discriminator. This pins
        the genuinely unscoped disjunct instead -- a bare ``python -m
        pytest`` names no test file, directory, or node-id, so it is Tier U
        on shape alone regardless of this repo's configured commands, and an
        ungranted EM is denied.

        Repoints a stale pre-R1 assertion that this repo's OWN declared
        command (``python -m pytest coordinator/tests``, itself directory-
        scoped) was Tier U merely because ``fast_test_cmd == full_test_cmd``
        here -- R1 names that exact command as its own worked counterexample
        and rejects tie-detection by name. See
        ``test_declared_tie_with_scoped_shape_still_needs_grant`` below
        for that counterexample, pinned directly."""
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
        assert "Tier-U" in _reason(guard.check(_payload(command, declared_repo)))

    def test_declared_tie_with_scoped_shape_still_needs_grant(
            self, declared_repo, free_mutex, monkeypatch):
        """DR-088 R1's own worked example (2026-07-25 EM-ratified amendment):
        a repo may declare the IDENTICAL scoped command under both
        ``fast_test_cmd`` and ``full_test_cmd`` and remain Tier F -- this
        fixture's ``declared_repo`` shape (``python -m pytest coordinator/
        tests``, itself directory-scoped) is that exact example, named
        verbatim in the amendment text. Tie is not the tier discriminator;
        shape is -- but PM-ruled 2026-08-04, Tier F is no longer exempt from
        the grant leg either way, so this now denies absent a grant just
        like Tier U (repoints the pre-2026-08-04 assertion that it was
        ungated)."""
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
        out = guard.check(_payload(
            "python -m pytest coordinator/tests", declared_repo))
        assert "authorization grant" in _reason(out)

    def test_declared_tie_with_scoped_shape_allowed_with_live_grant(
            self, declared_repo, free_mutex, monkeypatch):
        """The companion case: the same Tier-F tie shape IS allowed once a
        live Tier-U grant is held -- one grant record covers both tiers."""
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, None))
        assert guard.check(_payload(
            "python -m pytest coordinator/tests", declared_repo)) is None

    @pytest.mark.parametrize("command", [
        "python -m pytest coordinator/tests/test_foo.py",
        "python -m pytest coordinator/tests/test_foo.py::test_bar -q",
    ])
    def test_narrower_than_declared_stays_ungated(self, declared_repo, free_mutex,
                                                  monkeypatch, command):
        """Containment must not swallow genuinely scoped runs: a path BELOW
        the declared one does not carry the declared token, so it stays
        Tier T -- ungated even for a subagent with no grant."""
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
        assert guard.check(
            _payload(command, declared_repo, agent_id=_AGENT_ID)) is None

    # -----------------------------------------------------------------------
    # Regression: a fast tier that STRICTLY NARROWS the full tier (the natural
    # way to scope one -- append a path) must stay reachable as Tier F.
    # -----------------------------------------------------------------------

    @pytest.fixture
    def narrowing_repo(self, tmp_path, monkeypatch):
        """example-market-data-repo's real shape (observed 2026-07-30): the fast
        tier is the full tier plus a directory scope, so the full command's
        tokens are a STRICT SUBSET of the fast command's."""
        (tmp_path / "coordinator.local.md").write_text(
            "---\n"
            "project_type: general\n"
            "fast_test_cmd: python dev.py test tests\n"
            "full_test_cmd: python dev.py test\n"
            "---\n",
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "dev.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        return tmp_path

    def test_fast_that_strictly_narrows_full_is_tier_f(self, narrowing_repo):
        """Because the configured-cmd match is token containment, the fast
        invocation also "contains" the full command; the full-before-fast
        tie-break then classified the repo's OWN fast tier as Tier U, leaving
        it no reachable Tier F -- so every /validate and workday-complete
        Step-1 gate refused rather than ran, and did so SILENTLY (the wrap
        recorded a declined gate, never a failing one)."""
        matches = guard.classify_command(
            "python dev.py test tests", cwd=str(narrowing_repo))
        assert [m.tier for m in matches] == ["F"]

    def test_narrowing_does_not_ungate_the_full_suite(self, narrowing_repo):
        """The other half of the contract: preferring the fast tier on a
        strict-narrowing repo must NOT ungate the real whole-suite command,
        which carries no scope token of its own."""
        matches = guard.classify_command(
            "python dev.py test", cwd=str(narrowing_repo))
        assert [m.tier for m in matches] == ["U"]

    # -----------------------------------------------------------------------
    # Review: code-reviewer — Finding 1 regression. A bare single-token
    # configured `fast_test_cmd` (no declared arguments) must not swallow a
    # genuinely narrower invocation of that runner into Tier F/U.
    # -----------------------------------------------------------------------

    @pytest.fixture
    def bare_runner_repo(self, tmp_path, monkeypatch):
        """A consumer-shaped repo whose configured `fast_test_cmd` is a BARE
        runner invocation with no declared arguments (relying on the repo's
        own pytest config for scope -- a legitimate, common pattern)."""
        (tmp_path / "coordinator.local.md").write_text(
            "---\n"
            "project_type: general\n"
            "fast_test_cmd: pytest\n"
            "---\n",
            encoding="utf-8",
        )
        (tmp_path / "coordinator_core" / "frontmatter" / "tests").mkdir(parents=True)
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
        return tmp_path

    def test_bare_configured_fast_test_cmd_does_not_swallow_a_scoped_run(
        self, bare_runner_repo, free_mutex
    ):
        """A bare `fast_test_cmd: pytest` must not classify a genuinely
        scoped, narrower invocation of the same runner into Tier F/U -- that
        run stays ungated even for a subagent with no grant."""
        cmd = "pytest coordinator_core/frontmatter/tests/test_x.py::test_y"
        assert guard.check(
            _payload(cmd, bare_runner_repo, agent_id=_AGENT_ID)) is None

    def test_bare_configured_fast_test_cmd_still_leaves_unscoped_run_denied(
        self, bare_runner_repo, free_mutex
    ):
        """Coverage is not lost by the Finding 1 fix -- an unscoped `pytest`
        invocation is still denied, just via the generic shape classifier
        rather than the (now correctly inert for this shape) containment
        leg."""
        out = guard.check(_payload("pytest", bare_runner_repo, agent_id=_AGENT_ID))
        reason = _reason(out)
        assert "no test file, directory, or node-id scope" in reason


class TestRunnerRecognizedNonPythonFamilies:
    """``_runner_recognized`` used to grant an unconditional ``True`` for
    ``npm``/``pnpm``/``yarn``/``bun``, ``cargo``, ``go``, and ``make``/
    ``gmake`` on the base binary name alone -- the same fail-open R9/R7
    closed for the python family (``python dev.py test``), reopened here.

    ``_classify_cargo``/``_classify_go``/``_classify_package_manager``/
    ``_classify_make`` each return ``None`` for TWO structurally different
    reasons the old blanket ``True`` could not tell apart: (a) genuinely
    scoped (a filter/target positional was found), and (b) the argv is not a
    recognized test-invocation shape for that runner at all (``cargo watch
    -x test``, ``go doc test``, ``npm run typecheck -- test``). Case (b) let
    a ``full_test_cmd``-configured repo classify an unrelated command as
    Tier F merely because it happened to CONTAIN the literal token ``test``
    somewhere in its args (the containment leg's own job), combined with the
    base binary matching. Each pair below pins BOTH reasons for one family:
    the genuinely-scoped shape still resolves Tier F, and the unrecognized
    shape now resolves Tier U instead of the old silent Tier F.

    ``make`` has no scoped invocation shape at all (see
    ``_classify_make``'s own docstring -- "there is no scoped form of `make
    test`"), so there is no "genuinely scoped" pairing for it: whenever its
    shape is recognized, ``_classify_make`` itself already returns a
    non-``None`` label and the ``_runner_recognized`` gate is never even
    consulted (it only fires when the generic classifier already said
    ``None``). One test suffices for that family.
    """

    @pytest.fixture
    def cargo_repo(self, tmp_path, monkeypatch):
        (tmp_path / "coordinator.local.md").write_text(
            "---\n"
            "project_type: general\n"
            "full_test_cmd: cargo test\n"
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        return tmp_path

    def test_cargo_scoped_filter_still_resolves_tier_f(self, cargo_repo):
        matches = guard.classify_command("cargo test my_filter", cwd=str(cargo_repo))
        assert [m.tier for m in matches] == ["F"]

    def test_cargo_watch_unrecognized_shape_resolves_tier_u_not_f(self, cargo_repo):
        """The verified repro: ``cargo watch -x test`` merely CONTAINS the
        literal token ``test`` (as ``-x``'s own argument), so it satisfies
        the ``full_test_cmd`` containment leg, but ``watch`` is not a
        recognized ``cargo test``/``cargo nextest run`` head at all --
        ``_classify_cargo`` returns ``None`` because it has no opinion, not
        because the shape is confirmed scoped. Must resolve Tier U, never
        the old silent Tier F."""
        matches = guard.classify_command("cargo watch -x test", cwd=str(cargo_repo))
        assert [m.tier for m in matches] == ["U"]

    def test_cargo_watch_denied_for_subagent_through_real_check(
        self, cargo_repo, free_mutex
    ):
        """Same repro, through the real ``check()`` entry point (not just the
        classifier in isolation) -- a dispatched subagent is denied outright
        on identity alone regardless of tier, so this pins that the deny
        fires and is not accidentally allowed as an ungated Tier-F run."""
        reason = _reason(guard.check(
            _payload("cargo watch -x test", cargo_repo, agent_id=_AGENT_ID)))
        assert "no test file, directory, or node-id scope" in reason

    def test_cargo_watch_needs_a_tier_u_grant_for_the_em_through_real_check(
        self, cargo_repo, free_mutex, monkeypatch
    ):
        """The EM-side half of the same repro: with no live Tier-U grant,
        ``cargo watch -x test`` must be denied pending a grant -- not
        silently allowed as the repo's configured (ungated) Tier F."""
        monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
        reason = _reason(guard.check(_payload("cargo watch -x test", cargo_repo)))
        assert "Tier-U" in reason

    @pytest.fixture
    def go_repo(self, tmp_path, monkeypatch):
        (tmp_path / "coordinator.local.md").write_text(
            "---\n"
            "project_type: general\n"
            "full_test_cmd: go test\n"
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        return tmp_path

    def test_go_scoped_package_still_resolves_tier_f(self, go_repo):
        matches = guard.classify_command("go test ./pkg/foo", cwd=str(go_repo))
        assert [m.tier for m in matches] == ["F"]

    def test_go_unrecognized_shape_resolves_tier_u_not_f(self, go_repo):
        """``go doc test`` is not ``go test`` -- ``doc`` is not a recognized
        head, so ``_classify_go`` returns ``None`` with no opinion, even
        though the invocation contains the literal token ``test`` and so
        satisfies the containment leg. Must resolve Tier U."""
        matches = guard.classify_command("go doc test", cwd=str(go_repo))
        assert [m.tier for m in matches] == ["U"]

    @pytest.fixture
    def make_repo(self, tmp_path, monkeypatch):
        (tmp_path / "coordinator.local.md").write_text(
            "---\n"
            "project_type: general\n"
            "full_test_cmd: make test\n"
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        return tmp_path

    def test_make_unrecognized_first_target_resolves_tier_u_not_f(self, make_repo):
        """``make build test`` -- make's own first-positional-is-the-target
        rule means ``build`` is the real target here, with ``test`` merely a
        SECOND positional make would also treat as a target. The old
        base-name-only ``_runner_recognized`` granted Tier F purely because
        the invocation contains the literal token ``test`` and the base is
        ``make``; the fix asks whether the FIRST target is itself a suite
        target, which it is not here, so this must resolve Tier U."""
        matches = guard.classify_command("make build test", cwd=str(make_repo))
        assert [m.tier for m in matches] == ["U"]

    @pytest.fixture
    def pm_repo(self, tmp_path, monkeypatch):
        (tmp_path / "coordinator.local.md").write_text(
            "---\n"
            "project_type: general\n"
            "full_test_cmd: npm test\n"
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        return tmp_path

    def test_bun_test_genuinely_scoped_path_still_resolves_tier_f(
        self, tmp_path, monkeypatch
    ):
        """``bun test`` is the one package-manager shape that stays
        scope-aware (see ``_classify_package_manager``'s own docstring), so
        a real on-disk path argument is a genuine Tier-T-looking scope --
        this must still resolve Tier F once it satisfies the containment
        leg, exactly like the python/cargo/go "genuinely scoped" cases.
        Uses its own ``bun``-declared repo -- ``pm_repo`` declares ``npm
        test``, which a ``bun`` invocation never cfg-matches (different
        head), so this needs a fixture that actually declares ``bun test``."""
        (tmp_path / "coordinator.local.md").write_text(
            "---\n"
            "project_type: general\n"
            "full_test_cmd: bun test\n"
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
        monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.test.ts").write_text("", encoding="utf-8")
        matches = guard.classify_command(
            "bun test src/foo.test.ts", cwd=str(tmp_path))
        assert [m.tier for m in matches] == ["F"]

    def test_npm_run_typecheck_unrecognized_shape_resolves_tier_u_not_f(
        self, pm_repo
    ):
        """``npm run typecheck -- test`` is not a package-script TEST
        invocation at all (``typecheck`` is the script, ``test`` is just a
        trailing forwarded arg) -- ``_classify_package_manager`` returns
        ``None`` with no opinion, even though the literal token ``test``
        satisfies the ``full_test_cmd: npm test`` containment leg. Must
        resolve Tier U, the same class of bug as ``cargo watch -x test``."""
        matches = guard.classify_command(
            "npm run typecheck -- test", cwd=str(pm_repo))
        assert [m.tier for m in matches] == ["U"]


def test_norm_head_collapses_python_family():
    for token in ("python", "python3", "python3.14", "/usr/bin/python3",
                  ".venv/bin/python", "C:\\Python\\python.exe"):
        assert guard._norm_head(token) == "python"
    assert guard._norm_head("/usr/bin/pytest") == "pytest"
    assert guard._norm_head("pythonic-runner") == "pythonic-runner"


def test_norm_arg_collapses_whitespace_runs():
    assert guard._norm_arg("not  cadence   and not pending_fix") == (
        "not cadence and not pending_fix"
    )
    assert guard._norm_arg("  leading and trailing  ") == "leading and trailing"


def test_matches_declared_fast_test_cmd_exact_still_rejects_superset_through_norm_head():
    """Review: code-reviewer — R6's authority exit (`_matches_declared_
    fast_test_cmd`) depends on exact-equality semantics, and `exact=True`
    now runs through `_norm_head` (the same python-family head-collapsing
    the containment legs use) after the shared-helper refactor. A head
    token difference alone (`python` vs `python3`) must still collapse to a
    match, but a SUPERSET of the declared command (an extra token appended
    to an otherwise head-collapsed-identical invocation) must still be
    refused -- exact-equality on the argument tuple is preserved even
    though the head normalizes."""
    declared = "python -m pytest -m 'not cadence'"
    configured = [guard.ConfiguredCmd("fast_test_cmd", declared, 0)]

    exact_argv = [guard._tokens("python3 -m pytest -m 'not cadence'")]
    assert guard._matches_declared_fast_test_cmd(exact_argv, configured) is True

    superset_argv = [guard._tokens("python3 -m pytest -m 'not cadence' --extra-unscoped-thing")]
    assert guard._matches_declared_fast_test_cmd(superset_argv, configured) is False


# ---------------------------------------------------------------------------
# R9 -- subagent Tier-T precision leg (DR-088 amendment, 2026-07-28)
#
# The ruling: for a caller carrying a top-level ``agent_id``, Tier T is
# file-and-node-id precision, not directory precision. § Decision always
# defined Tier T as what the caller "authored or touched"; the mechanism
# enforced path-scoped and dropped the relevance half. These tests pin both
# halves of the ruling -- what the leg newly refuses, and the four carve-outs
# it must not break.
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_with_test_dir(repo):
    """``repo`` plus a real on-disk test directory to name as an argument.

    R9's decision is ``os.path.isdir``-keyed, not spelling-keyed, so a
    directory argument only tests anything when the directory actually
    exists -- a fixture whose dirs are notional would make every assertion
    below vacuously pass.
    """
    (repo / "coordinator_core" / "frontmatter" / "tests" / "sub").mkdir(parents=True)
    return repo


@pytest.mark.parametrize(
    "command",
    [
        "pytest coordinator_core/frontmatter/tests/sub",
        "pytest coordinator_core/frontmatter/tests/sub/",
        "python3 -m pytest coordinator_core/frontmatter/tests/sub -q",
        ".venv/bin/python -m pytest coordinator_core/frontmatter/tests/sub -q",
        # -k narrows the selection but does not narrow the ARGUMENT: the
        # ruling is directory-precision, stated literally.
        "pytest coordinator_core/frontmatter/tests/sub -k test_thing",
    ],
)
def test_subagent_directory_argument_denied(repo_with_test_dir, free_mutex, command):
    reason = _reason(guard.check(_payload(command, repo_with_test_dir, agent_id=_AGENT_ID)))
    assert "Directory arg" in reason
    assert "DR-088 R9" in reason


def test_r9_deny_instructs_reporting_the_substitution_to_the_dispatcher(
    repo_with_test_dir, free_mutex
):
    """Inbound memo gap (example-market-data-repo-em, 2026-07-28): when R9 denies
    and the agent narrows to a file/node-id command, the dispatching EM
    never learned that the brief's breadth was refused. The deny text must
    now instruct the agent to report that substitution back explicitly."""
    reason = _reason(guard.check(
        _payload("pytest coordinator_core/frontmatter/tests/sub", repo_with_test_dir,
                 agent_id=_AGENT_ID)
    ))
    assert "report the substitution" in reason
    assert "dispatcher" in reason
    assert "override was not invoked" in reason


@pytest.mark.parametrize(
    "command",
    [
        # Node id -- bounded to one test by construction, permitted touched
        # or not. This is what keeps executor pre-existing-failure
        # verification legal.
        "pytest coordinator_core/frontmatter/tests/sub/test_x.py::test_case",
        # File -- self-bounding.
        "pytest coordinator_core/frontmatter/tests/sub/test_x.py",
        # No path argument at all.
        "pytest -k schema_validate",
        # A directory-shaped token that is a FLAG OPERAND, not a positional.
        "pytest --rootdir coordinator_core/frontmatter/tests/sub "
        "coordinator_core/frontmatter/tests/sub/test_x.py::test_case",
    ],
)
def test_subagent_precision_shapes_still_allowed(repo_with_test_dir, free_mutex, command):
    assert guard.check(_payload(command, repo_with_test_dir, agent_id=_AGENT_ID)) is None


@pytest.mark.parametrize(
    "command",
    [
        "pytest coordinator_core/frontmatter/tests/sub",
        "pytest coordinator_core/frontmatter/tests/sub -k test_thing",
    ],
)
def test_em_directory_argument_unaffected(repo_with_test_dir, free_mutex, command):
    """R9 narrows the subagent rung ONLY. Directory-level Tier T for the
    top-level EM stays exactly as it was -- the amendment is explicit that
    this is not a fleet-wide narrowing of the EM path."""
    assert guard.check(_payload(command, repo_with_test_dir)) is None


def test_r9_is_additive_suite_shaped_still_gets_the_identity_diagnosis(repo, free_mutex):
    """A suite-shaped subagent command is an IDENTITY problem, not a
    precision one, and must keep saying so. R9 runs only when the command is
    not suite-shaped, so it can only ever deny what was previously allowed --
    never restate a deny another leg owns with a worse diagnosis."""
    reason = _reason(guard.check(_payload("pytest coordinator_core/", repo, agent_id=_AGENT_ID)))
    assert reason.startswith("Run the tests you actually touched:")
    assert "Directory arg" not in reason


def test_r9_deny_leads_with_the_agents_own_touched_test_files(
    repo_with_test_dir, free_mutex, monkeypatch
):
    """Posture: offer the better command, don't merely refuse the worse one."""
    monkeypatch.setattr(
        guard,
        "_agent_touched_test_files",
        lambda raw, sid, root: ["tests/test_alpha.py", "tests/test_beta.py"],
    )
    reason = _reason(guard.check(
        _payload("pytest coordinator_core/frontmatter/tests/sub", repo_with_test_dir,
                 agent_id=_AGENT_ID)
    ))
    assert reason.startswith("Run the test files YOU touched:")
    assert "pytest tests/test_alpha.py" in reason
    assert "pytest tests/test_beta.py" in reason


def test_r9_empty_touched_set_still_denies_and_names_the_precise_shapes(
    repo_with_test_dir, free_mutex, monkeypatch
):
    """An agent that has edited nothing yet is not exempt -- files and node
    ids stay permitted because they are self-bounding, but a directory is
    still refused, and the deny has to say what to type instead."""
    monkeypatch.setattr(guard, "_agent_touched_test_files", lambda raw, sid, root: [])
    reason = _reason(guard.check(
        _payload("pytest coordinator_core/frontmatter/tests/sub", repo_with_test_dir,
                 agent_id=_AGENT_ID)
    ))
    assert "touched no test files yet" in reason
    assert "::test_the_case_you_changed" in reason


def test_touched_set_read_never_falls_back_to_the_session_level_file(tmp_path, monkeypatch):
    """The session-level ``touched.txt`` belongs to the EM and to other
    agents; borrowing it to phrase "the tests you touched" would launder
    exactly the relevance R9 asserts. An unresolvable agent id reads as an
    empty set, never as the session's."""
    sessions = tmp_path / ".git" / "coordinator-sessions"
    (sessions / "sess1").mkdir(parents=True)
    (sessions / "sess1" / "touched.txt").write_text(
        "tests/test_someone_elses.py\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda root: tmp_path / ".git"
    )
    # Unrecognised agent-id shape -> canonicalization fails closed.
    assert guard._agent_touched_test_files("not-a-valid-agent-id", "sess1", str(tmp_path)) == []


def test_pytest_directory_args_fails_open_without_a_cwd():
    """Without a cwd a directory is indistinguishable from a filter string.
    Denying on shape alone would refuse ``cargo test my_filter``-alikes; the
    identity leg still covers every suite-shaped subagent command."""
    assert guard._pytest_directory_args([["pytest", "tests"]], None) == []


def test_walk_pytest_args_is_the_single_source_for_operand_grammar():
    """Both consumers -- ``_classify_pytest`` and ``_pytest_directory_args``
    -- must read pytest's flag-operand grammar from one walk. A second
    hand-rolled walk would drift on the next flag-table edit and start
    reading a ``--rootdir`` value as a positional scope."""
    scoped, positionals = guard._walk_pytest_args(
        ["--rootdir", "tests", "-k", "expr", "tests/test_x.py"]
    )
    assert scoped is True
    assert positionals == ["tests/test_x.py"]


# R9 -- unexpanded-glob routearound close (2026-08-03, example-retrieval-repo Finding 2)
#
# ``_pytest_directory_args`` decided "this positional names a directory" via
# a literal ``os.path.isdir`` check, so an unexpanded glob positional (the
# shell would expand it to the same directory breadth R9 exists to refuse,
# but the guard sees only the un-expanded pattern) slipped past leg 0
# entirely. These tests pin the glob-expansion close and its two deliberate
# posture calls -- files-only and zero-match.
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_with_glob_fixtures(repo_with_test_dir):
    """``repo_with_test_dir`` plus files inside the real ``sub`` directory,
    so a files-only glob has something real to match against."""
    sub = repo_with_test_dir / "coordinator_core" / "frontmatter" / "tests" / "sub"
    (sub / "test_a.py").write_text("", encoding="utf-8")
    (sub / "test_b.py").write_text("", encoding="utf-8")
    return repo_with_test_dir


@pytest.mark.parametrize(
    "command",
    [
        # Un-expanded glob covering the real `sub` directory -- the shell
        # would expand this to `coordinator_core/frontmatter/tests/sub`,
        # exactly the directory-precision shape R9 refuses literally.
        "pytest coordinator_core/frontmatter/*/sub",
        "pytest coordinator_core/frontmatter/tests/*/",
    ],
)
def test_subagent_glob_covering_a_real_directory_denied(
    repo_with_glob_fixtures, free_mutex, command
):
    reason = _reason(guard.check(_payload(command, repo_with_glob_fixtures, agent_id=_AGENT_ID)))
    assert "Directory arg" in reason
    assert "DR-088 R9" in reason


def test_em_glob_covering_a_real_directory_unaffected(repo_with_glob_fixtures, free_mutex):
    """Constraint 1: R9 (including its glob-expansion close) applies to the
    subagent rung only -- the top-level EM (no ``agent_id``) is unaffected,
    same as the literal-directory case in ``test_em_directory_argument_
    unaffected`` above."""
    assert guard.check(
        _payload("pytest coordinator_core/frontmatter/*/sub", repo_with_glob_fixtures)
    ) is None


def test_subagent_glob_matching_only_files_is_not_denied(repo_with_glob_fixtures, free_mutex):
    """Posture call: a glob that expands to files ONLY (no directory among
    the matches) is not refused. Files are self-bounding by this function's
    own contract for a literal file argument, and a glob that only ever
    reaches files makes the identical scoping claim a literal file list
    would -- narrowing the runner-facing grammar of the argument does not
    change what it is scoped to."""
    command = "pytest coordinator_core/frontmatter/tests/sub/test_*.py"
    assert guard.check(_payload(command, repo_with_glob_fixtures, agent_id=_AGENT_ID)) is None


def test_subagent_glob_matching_nothing_fails_open(repo_with_glob_fixtures, free_mutex):
    """Posture call: a glob that expands to NOTHING is not refused --
    fail-OPEN, mirroring ``_pytest_directory_args``'s own no-``cwd``
    precedent: a pattern with zero matches names no directory breadth to
    point the deny at. The identity leg still denies any suite-shaped
    subagent command regardless."""
    command = "pytest coordinator_core/frontmatter/tests/sub/nonexistent_*"
    assert guard.check(_payload(command, repo_with_glob_fixtures, agent_id=_AGENT_ID)) is None


def test_subagent_glob_shaped_node_id_still_never_returned(repo_with_glob_fixtures, free_mutex):
    """Constraint 2: a positional containing ``::`` is bounded to one test by
    construction and is never returned, glob-shaped or not -- this is what
    keeps an executor's pre-existing-failure verification legal."""
    command = "pytest coordinator_core/frontmatter/tests/sub/test_*.py::test_case"
    assert guard.check(_payload(command, repo_with_glob_fixtures, agent_id=_AGENT_ID)) is None


def test_pytest_directory_args_glob_covering_a_directory_is_returned(repo_with_test_dir):
    """Unit-level pin on ``_pytest_directory_args`` directly, independent of
    ``check()``'s identity/mutex plumbing."""
    argv = ["pytest", "coordinator_core/frontmatter/*/sub"]
    assert guard._pytest_directory_args([argv], str(repo_with_test_dir)) == [
        "coordinator_core/frontmatter/*/sub"
    ]
