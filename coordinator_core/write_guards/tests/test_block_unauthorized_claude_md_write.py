"""Behavioral tests for
coordinator_core.write_guards.block_unauthorized_claude_md_write -- the
CLAUDE.md-class write guard DR-104 (2026-07-27) reintroduces over DR-058
for one path class only (see the module's own docstring).

Three tests here are load-bearing acceptance criteria per example-doctrine-repo
docs/plans/2026-07-27-claude-md-altitude-triage.md § C4, not coverage:

  AC8 (TestSubagentOriginatedDenied) -- a SUBAGENT-originated payload
      (agent_id present) is denied absent a grant. This is the case the
      +27% growth this guard exists to stop would have needed -- a guard
      that only fires EM-inline is worthless here.

  AC9 (TestRealScopeEqualsStatedScope) -- real scope equals stated scope,
      per path class. Reuses the shape pinned in
      coordinator_core/bash_guards/tests/test_check_blanket_git_add.py
      (the check_blanket_git_add scope-gap this guard's own negative-spec
      names as precedent): assert DENY for every path class this guard's
      docstring claims to cover, and ALLOW for a representative
      NOT-covered case, so the stated scope and the enforced scope are
      pinned against each other rather than merely described in prose.

  AC10 (TestDenyTextNamesAlternativeAndOverride) -- the EMITTED deny text
      names a concrete alternative (the discharge hierarchy, DEC-6) and
      the override path (the C5 grant CLI). Asserted against the RENDERED
      STRING returned by ``check()``, never against the code that builds
      it.

Seam: ``check_claude_md_write_grant`` is monkeypatched directly (module
import binding) to avoid any real ``.git/coordinator-sessions/`` fixture --
this file is about the guard's OWN detection/scope/deny-text behavior, not
the grant module's own persistence semantics (already covered by
``coordinator_core/session/tests/test_claude_md_grant.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.write_guards import block_unauthorized_claude_md_write as guard

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]


def _payload(
    file_path: str,
    *,
    agent_id: str = "aexecutor-teammate-1234567890abcdef",
    tool_name: str = "Edit",
    cwd: str = "/repo",
) -> dict:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path, "old_string": "x", "new_string": "y"},
        "cwd": cwd,
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _no_grant(monkeypatch):
    """Default fixture state: calling session holds NO live grant. Tests
    that need a granted state override this explicitly.
    """
    monkeypatch.setattr(guard, "check_claude_md_write_grant", lambda cwd: (False, None))


def _deny(monkeypatch, file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is not None, f"expected DENY for: {file_path!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def _allow(monkeypatch, file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is None, f"expected ALLOW for: {file_path!r}, got {result!r}"


# ---------------------------------------------------------------------------
# AC8 -- subagent-originated payload (agent_id present) is denied.
# ---------------------------------------------------------------------------


class TestSubagentOriginatedDenied:
    def test_subagent_write_to_claude_md_denied(self, monkeypatch):
        _deny(monkeypatch, "CLAUDE.md")

    def test_subagent_edit_to_claude_md_denied(self, monkeypatch):
        result = guard.check(_payload("coordinator/CLAUDE.md", tool_name="Edit"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_em_inline_write_allowed_no_agent_id(self, monkeypatch):
        """No agent_id -> EM-inline write -> always allow, even absent a
        grant -- this guard governs the DISPATCHED path only (DR-104's own
        new evidence is specifically about the executor path, not
        EM-inline authoring)."""
        _allow(monkeypatch, "CLAUDE.md", agent_id="")

    def test_subagent_write_allowed_with_live_grant(self, monkeypatch):
        monkeypatch.setattr(
            guard, "check_claude_md_write_grant", lambda cwd: (True, {"granted_by": "pm"})
        )
        _allow(monkeypatch, "CLAUDE.md")

    def test_subagent_write_allowed_with_override_env(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
        _allow(monkeypatch, "CLAUDE.md")

    def test_non_write_tool_allowed(self, monkeypatch):
        _allow(monkeypatch, "CLAUDE.md", tool_name="Read")


# ---------------------------------------------------------------------------
# AC8 regression pin -- an EM-acquired grant, written via the DEFAULT
# env-driven acquisition path (``write_claude_md_write_grant`` with no
# explicit ``session_id``), still authorizes a SUBAGENT-shaped payload on
# this guard's real ``check_claude_md_write_grant`` predicate -- not the
# module-monkeypatched shortcut every other test in this file uses. This is
# the inheritance property the whole plan exists to preserve: EM and
# dispatched-subagent turns resolve to the SAME session id, so the grant the
# EM wrote is visible to the guard evaluation a subagent's own tool call
# triggers, with no separate wiring. Paired with the identical payload
# absent any grant, asserting DENY, so the allow leg cannot pass for the
# wrong reason (a mis-shaped payload silently tripping an earlier allow
# branch). ``test_subagent_write_allowed_with_live_grant`` above already
# pins the ALLOW shape against a directly-monkeypatched
# ``check_claude_md_write_grant`` -- what it does NOT cover is the real
# acquisition path (``write_claude_md_write_grant`` -> disk ->
# ``check_claude_md_write_grant``) nor the paired no-grant negative; this
# class adds exactly that missing half.
# ---------------------------------------------------------------------------


def _make_repo(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _live_session(repo, sid):
    from coordinator_core.session import core as session_core

    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(
        json.dumps({"pid": "999", "last_activity": session_core.now_iso()}) + "\n",
        encoding="utf-8",
    )
    return sdir


class TestSubagentInheritsEmAcquiredGrant:
    """AC8, respecified per Review Finding 1 off ``TestSubagentResolvability``
    (module-level, green regardless of this guard) onto this guard's own
    ``check()`` entrypoint -- the only surface where the acquisition-gate
    regression this pin exists to catch could actually be observed."""

    def test_em_acquired_grant_authorizes_subagent_write(self, tmp_path, monkeypatch):
        from coordinator_core.session import claude_md_grant as cmg

        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "ac8-inherit-session")
        _live_session(repo, "ac8-inherit-session")

        # EM-inline turn: default env-driven acquisition, no explicit
        # session_id -- the exact call shape a granting EM makes.
        granted = cmg.write_claude_md_write_grant(
            "pm", "PM said go ahead this session", cwd=str(repo)
        )
        assert granted is True

        # Restore the REAL predicate for this test only -- every other test
        # in this file relies on the autouse ``_no_grant`` monkeypatch, but
        # AC8 is specifically about the real acquisition -> resolution path.
        monkeypatch.setattr(guard, "check_claude_md_write_grant", cmg.check_claude_md_write_grant)

        target = repo / "CLAUDE.md"
        target.write_text("short")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "a much longer replacement body"},
            "cwd": str(repo),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is None, f"expected ALLOW (inherited EM grant), got {result!r}"

    def test_subagent_write_denied_absent_the_grant(self, tmp_path, monkeypatch):
        """Paired negative: identical payload, identical session/repo
        shape, but NO grant written -- proves the allow above is not a
        mis-shaped payload silently tripping an earlier allow leg."""
        from coordinator_core.session import claude_md_grant as cmg

        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "ac8-inherit-session-negative")
        _live_session(repo, "ac8-inherit-session-negative")

        monkeypatch.setattr(guard, "check_claude_md_write_grant", cmg.check_claude_md_write_grant)

        target = repo / "CLAUDE.md"
        target.write_text("short")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "a much longer replacement body"},
            "cwd": str(repo),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None, "expected DENY absent any grant on disk"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# AC9 -- real scope equals stated scope, per path class. Reuses the
# check_blanket_git_add shape: pin DENY for every claimed-covered class and
# ALLOW for a representative not-covered class.
# ---------------------------------------------------------------------------


class TestRealScopeEqualsStatedScope:
    """Every bullet in the module docstring's is_claude_md_class-derived
    class list, pinned by assertion -- not merely described in prose. This
    is the check_blanket_git_add defect (doctrine claimed a wider deny
    scope than the code enforced) in test form, per the guard's own
    negative-spec.
    """

    @pytest.mark.parametrize(
        "file_path",
        [
            "CLAUDE.md",
            "coordinator/CLAUDE.md",
            "global-doctrine/CLAUDE.md",
            "coordinator/templates/CLAUDE.md.tmpl",
            "some/nested/repo/CLAUDE.md",
        ],
    )
    def test_claude_md_class_paths_all_denied(self, monkeypatch, file_path):
        _deny(monkeypatch, file_path)

    def test_self_referential_scope_example_doctrine_repo_own_repo_root_claude_md_denied(self, monkeypatch):
        """Self-referential scope, resolved deliberately (see module
        docstring) -- this guard's own authoring repo's root CLAUDE.md is
        NOT excluded. Excluding it would reproduce the exact
        stated-vs-real scope gap this guard's negative-spec calls out.

        This is a SCOPE test (is the path matched as CLAUDE.md-class at
        all), not a direction test -- the growth/shrink comparison is
        pinned separately (``TestDirectionalDenyGrowthOnly``). Pin growth
        True here so this scope assertion does not depend on this dev
        machine's real example-doctrine-repo checkout's CLAUDE.md byte content.
        """
        monkeypatch.setattr(guard, "_is_growth", lambda *a, **kw: True)
        _deny(monkeypatch, "CLAUDE.md", cwd="/Users/example-operator/X/example-doctrine-repo")

    @pytest.mark.parametrize(
        "file_path",
        [
            "docs/wiki/some-page.md",
            "docs/plans/2026-07-27-x.md",
            "coordinator/CLAUDE.local.md",
            "README.md",
            "src/claude_md_helper.py",
        ],
    )
    def test_non_claude_md_class_paths_allowed(self, monkeypatch, file_path):
        _allow(monkeypatch, file_path)

    def test_backslash_path_still_matched(self, monkeypatch):
        """Windows-style separators normalize before the class check --
        stated scope must not silently narrow on one platform."""
        _deny(monkeypatch, "coordinator\\CLAUDE.md")


# ---------------------------------------------------------------------------
# AC10 -- the emitted deny text names a concrete alternative and the
# override path, asserted against the rendered string.
# ---------------------------------------------------------------------------


class TestDenyTextNamesAlternativeAndOverride:
    def test_deny_text_names_the_discharge_hierarchy(self, monkeypatch):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "discharge" in reason.lower()
        assert "mechanize" in reason.lower()
        assert "wiki" in reason.lower()

    def test_deny_text_does_not_presuppose_wiki_as_default_alternative(self, monkeypatch):
        """Regression for the inversion caught in review-integration: the
        deny text must never read as presupposing wiki-folding as the
        default fold target -- it names the full hierarchy instead.
        """
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "document-bloat-trim.md names the default fold target" not in reason
        assert "default fold target" not in reason

    def test_deny_text_no_longer_names_the_grant_cli_override_path(self, monkeypatch):
        """C4(b), docs/plans/2026-08-13-guard-messages-stop-handing-agents-
        the-keys.md: the resolved ``grant pm`` invocation is DELETED from
        the deny text -- a dispatched subagent is, by construction, the one
        agent forbidden to run it, so rendering it here was a dead affordance
        the EM ruling removes rather than the thing that made the deny
        complete."""
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator_core.session.claude_md_grant grant pm" not in reason

    def test_deny_text_no_longer_attributes_a_grant_command_to_the_em(
        self, monkeypatch
    ):
        """The "Unblock (EM runs this, not you):" line and the grant
        command/precondition beneath it are gone -- "Report BLOCKED to your
        EM instead" is now the whole remediation; see C4(b)."""
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Report BLOCKED to your EM" in reason
        assert "EM runs this, not you" not in reason
        assert "coordinator_core.session.claude_md_grant grant pm" not in reason

    def test_deny_text_no_longer_names_the_rare_use_env_override(self, monkeypatch):
        """The env-override affordance is likewise gone from the rendered
        deny text (see C4(b)) -- ``_OVERRIDE_ENV_VAR`` stays wired in
        ``check()`` (checked first, defense-in-depth) but is no longer
        advertised to the dispatched subagent this deny addresses."""
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE=1" not in reason
        assert guard._OVERRIDE_ENV_VAR not in reason

    def test_deny_text_names_the_target_path(self, monkeypatch):
        result = guard.check(_payload("coordinator/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator/CLAUDE.md" in reason

    def test_grant_cli_interpolates_the_resolved_claude_klabauter_root(self, monkeypatch):
        """The resolved-root branch. Asserting only that the module path appears
        would pass identically on the fallback branch, which is how the original
        dead-end shipped unnoticed -- pin the interpolated root itself."""
        import coordinator_core.claude_klabauter_root as mr

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", lambda: "/opt/some/claude-klabauter")
        assert guard._grant_cli_invocation() == (
            'PYTHONPATH="/opt/some/claude-klabauter" '
            'python3 -m coordinator_core.session.claude_md_grant grant pm '
            '"<verbatim PM note>"'
        )

    def test_grant_cli_falls_back_when_root_unresolvable(self, monkeypatch):
        """RuntimeError is the resolver's one documented failure."""
        import coordinator_core.claude_klabauter_root as mr

        def _raise():
            raise RuntimeError("cannot resolve CLAUDE_KLABAUTER_ROOT")

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", _raise)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_grant_cli_falls_back_on_empty_root(self, monkeypatch):
        import coordinator_core.claude_klabauter_root as mr

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", lambda: "")
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_grant_cli_never_propagates_an_unexpected_resolver_error(
        self, monkeypatch, capsys
    ):
        """A raise here would convert a clean block into a crashed PreToolUse
        guard. An undocumented failure -- a rename, a signature drift -- still
        falls back, but must say so rather than passing for normal operation."""
        import coordinator_core.claude_klabauter_root as mr

        def _raise():
            raise AttributeError("resolver drifted")

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", _raise)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK
        assert "could not resolve the claude-klabauter root" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "hostile_root",
        ['/opt/mak"ima', "/opt/$(whoami)", "/opt/mak`ima`", "/opt/mak\nima"],
    )
    def test_grant_cli_falls_back_on_a_shell_unsafe_root(
        self, monkeypatch, hostile_root
    ):
        """The rendered command is pasted verbatim into a shell. A root that
        would break out of the double quotes yields a remediation that silently
        does the wrong thing -- the same defect class this resolution fixes."""
        import coordinator_core.claude_klabauter_root as mr

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", lambda: hostile_root)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_deny_text_never_dead_ends(self, monkeypatch):
        """Nothing dead-ends -- state why, and give the reader a concrete
        actionable path (binding, design-as-offers). Per the C4(b) ruling
        (docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-
        keys.md), the alternative IS "report BLOCKED to your EM" -- rung-1
        familiar, no unfamiliar artifact, no inspection needed -- not a
        rendered grant-CLI invocation or env-override name. AC7 is
        discharged by the route, not by a runnable command in the text."""
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Report BLOCKED to your EM" in reason
        assert "Target:" in reason
        assert "Reason:" in reason

    def test_deny_text_actionable_line_opens_the_shared_cue_window(self, monkeypatch):
        """Regression pin for a P1 review-integration finding: the deny
        text's own docstring claims the target path, grant command, and
        grant precondition sit inside a cue window that exempts them from
        the C8 byte-prose cap -- but that claim is only true if the actual
        rendered text matches ``_CUE_WINDOW_RE``
        (``coordinator_core.bash_guards._alternative_liveness``). A reword
        that drops the matching phrase (as happened here once already)
        silently moves ~150+ bytes of exempt content into the counted-prose
        budget with no test failure anywhere else in this file to catch it.
        """
        from coordinator_core.bash_guards._alternative_liveness import _CUE_WINDOW_RE

        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        actionable_line = "Report BLOCKED to your EM instead:"
        assert actionable_line in reason
        assert _CUE_WINDOW_RE.search(actionable_line), (
            "the deny text's actionable line no longer matches the shared "
            "cue-window regex -- the target path/grant command/precondition "
            "that follow it will render as counted prose, not exempt "
            "cue-window content"
        )

    def test_deny_text_names_the_structural_reason(self, monkeypatch):
        """AC6: the deny text names the governed surface it was trying to
        write, and the structural reason (a property of every subagent on
        every dispatch, not a judgment on this agent's work)."""
        result = guard.check(_payload("coordinator/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator/CLAUDE.md" in reason
        assert "needs a live CLAUDE.md write grant for this session" in reason


# ---------------------------------------------------------------------------
# Directional deny (guard-class census RESHAPE, C18): deny GROWTH only,
# advise (do not block) on shrink/size-neutral. Both directions pinned
# against a REAL on-disk file -- Write/Edit go through ``_is_growth``'s own
# read of ``abs_file_path``, so a payload targeting a nonexistent path
# always falls into the "cannot determine" -> deny branch (already covered
# by every test above, all of which target a file that does not exist on
# disk). These tests use ``tmp_path`` so the growth/shrink comparison
# actually engages the byte-size measurement.
# ---------------------------------------------------------------------------


class TestDirectionalDenyGrowthOnly:
    def test_write_that_grows_the_file_is_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("short")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "a much longer replacement body"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_write_that_shrinks_the_file_is_advised_not_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("a much longer original body")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "short"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert "additionalContext" in hso

    def test_write_that_is_size_neutral_is_advised_not_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("abcde")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "vwxyz"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_edit_that_grows_the_file_is_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("hello world")
        payload = _payload(str(target), cwd=str(tmp_path))
        payload["tool_input"] = {"file_path": str(target), "old_string": "world", "new_string": "a much longer replacement string"}
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_edit_that_shrinks_the_file_is_advised_not_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("hello a much longer original string")
        payload = _payload(str(target), cwd=str(tmp_path))
        payload["tool_input"] = {
            "file_path": str(target),
            "old_string": "a much longer original string",
            "new_string": "x",
        }
        result = guard.check(payload)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert "additionalContext" in hso

    def test_advisory_no_longer_names_the_grant_cli_invocation(self, monkeypatch, tmp_path):
        """C4(c), docs/plans/2026-08-13-guard-messages-stop-handing-agents-
        the-keys.md (AC-1/AC-2): this leg fires only when ``agent_id`` is
        present -- i.e. only for a dispatched subagent, the exact audience
        forbidden from seeing an unlock statement in any shape. The
        resolved ``PYTHONPATH=... python3 -m
        coordinator_core.session.claude_md_grant grant pm`` invocation used
        to render here unconditionally (the same "shown the button, told
        not to press it" shape the deny leg's C4(b) reshape already closed)
        -- this is that same closure landing on the advisory leg."""
        target = tmp_path / "CLAUDE.md"
        target.write_text("a much longer original body")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "short"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "coordinator_core.session.claude_md_grant grant pm" not in reason
        assert "PYTHONPATH" not in reason

    def test_advisory_still_attributes_the_grant_step_to_the_em_not_the_reader(
        self, monkeypatch, tmp_path
    ):
        """AC6/AC7 companion case on the advisory leg (the C18c reshape):
        the advisory does not block, but that does not exempt it from the
        same attribution standard AC6 sets for the deny leg -- the grant
        step is attributed to the EM, not framed as something the reading
        subagent should itself run. No command is rendered any more (see
        the sibling test above); the attribution is now carried by
        directing the agent to report it as a dependency, not by naming a
        runnable line.
        """
        target = tmp_path / "CLAUDE.md"
        target.write_text("a much longer original body")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "short"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "the EM" in reason
        assert "not this agent's" in reason
        assert "coordinator_core.session.claude_md_grant grant pm" not in reason

    def test_new_file_creation_is_always_growth_and_denied(self, monkeypatch, tmp_path):
        """A CLAUDE.md-class file that does not yet exist has a 0-byte
        baseline -- there is no shrink case for content that does not
        exist yet, so creation is always denied absent a grant."""
        target = tmp_path / "CLAUDE.md"
        assert not target.exists()
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "brand new content"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
