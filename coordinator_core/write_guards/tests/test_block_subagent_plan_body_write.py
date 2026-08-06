"""Behavioral tests for coordinator_core.write_guards.block_subagent_plan_body_write
-- the executor plan-body/problem-set immutability guard (see the module's
own docstring for the reference-hook port this is a faithful engine-ification
of).

Covers the 2026-07-24 widening (memo
cross-repo/inbox/2026-07-24-example-doctrine-repo-em-executor-spec-surface-widening.md,
actioning C9(c) of example-doctrine-repo's agent-citizenship provisioning plan): the fast-exit
``_PLAN_BODY_RE`` now also matches ``docs/problems/**/*.md`` in addition to
``docs/plans/**/*.md``, while ``docs/wiki/**`` and ``docs/decisions/**``
remain deliberately excluded (executor authoring targets). The downstream
subagent_type gate (executor / AMBIGUOUS only) is unchanged and re-verified
here rather than assumed.

Also covers the 2026-08-06 C16 narrowing
(docs/plans/2026-08-06-apply-guard-class-census.md § C16): a resolved
coordinator:executor now hard-denies ONLY the plan/problem-set body its
own session sidecar(s) declare (``plan:`` frontmatter field, under
``state/subagent-share/<session_id>/``) as the plan currently being
executed; any OTHER plan/problem-set write from that same executor is
advisory-only (``additionalContext``, no ``permissionDecision``). The
AMBIGUOUS collision-sentinel branch is unaffected by this narrowing and is
re-verified as such below. ``TestExecutorRegexWidening`` and
``TestKindResolutionFailureIsMeasuredNotConfined`` were written against
the PRE-C16 uniform-deny shape and are updated in place here to seed a
matching sidecar where the test's intent is "the executing body still
denies", and to assert advisory (not deny) where the test's intent was
merely "some plan/problem-set path reaches the executor branch".

The back-pointer subagent_type lookup (``_read_backpointer_subagent_type``)
is monkeypatched directly rather than exercised via real
``.git/coordinator-sessions/`` fixtures -- these tests are about the regex
widening and the fast-exit/gate interaction, not the identity-resolution
chain (already covered by the module's own reference-hook port history).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import block_subagent_plan_body_write as guard


def _payload(
    repo_root: Path,
    rel_file_path: str,
    agent_id: str = "aexecutor-teammate-1234567890abcdef",
    session_id: str = "sess-12345678",
    tool_name: str = "Edit",
) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": rel_file_path, "old_string": "x", "new_string": "y"},
        "cwd": str(repo_root),
        "agent_id": agent_id,
        "session_id": session_id,
    }


def _seed_executing_sidecar(repo_root: Path, session_id: str, plan_rel_path: str) -> None:
    """Write a run-report sidecar declaring ``plan_rel_path`` as the plan
    this session's dispatch is executing -- C16's "executing body" signal.
    """
    sidecar_dir = repo_root / "state" / "subagent-share" / session_id
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "C1.md").write_text(
        f"---\nplan: {plan_rel_path}\nchunk: C1\nstatus: in_flight\n---\nbody\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY", raising=False)


def _stub_git_root(repo_root: Path):
    def _fake(cwd):
        return str(repo_root)

    return _fake


def _stub_subagent_type(subagent_type: str):
    def _fake(git_root, agent_id):
        return subagent_type

    return _fake


class TestExecutorRegexWidening:
    """AC1/AC4 — executor writes to docs/problems/** are now blocked;
    docs/wiki/** and docs/decisions/** remain excluded.
    """

    def test_executor_write_to_problems_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)
        monkeypatch.setattr(guard, "_write_hook_emit_log", lambda *a, **kw: None)
        _seed_executing_sidecar(tmp_path, "sess-12345678", "docs/problems/2026-07-24-x.md")

        payload = _payload(tmp_path, "docs/problems/2026-07-24-x.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "docs/problems/2026-07-24-x.md" in reason

    def test_executor_write_to_problems_blocked_backslash_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)
        monkeypatch.setattr(guard, "_write_hook_emit_log", lambda *a, **kw: None)
        _seed_executing_sidecar(tmp_path, "sess-12345678", "docs/problems/2026-07-24-x.md")

        payload = _payload(tmp_path, "docs\\problems\\2026-07-24-x.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_executor_write_to_problems_not_executing_advises(self, tmp_path, monkeypatch):
        """C16 -- a plan/problem-set write that is NOT the sidecar-declared
        executing body is advisory-only, not blocked.
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )
        _seed_executing_sidecar(tmp_path, "sess-12345678", "docs/plans/some-other-plan.md")

        payload = _payload(tmp_path, "docs/problems/2026-07-24-x.md")
        result = guard.check(payload)

        assert result is not None
        hso = result["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert "docs/problems/2026-07-24-x.md" in hso["additionalContext"]

    def test_executor_write_to_wiki_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )

        payload = _payload(tmp_path, "docs/wiki/x.md")
        assert guard.check(payload) is None

    def test_executor_write_to_decisions_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )

        payload = _payload(tmp_path, "docs/decisions/x.md")
        assert guard.check(payload) is None

    def test_executor_write_to_fixture_plan_allowed(self, tmp_path, monkeypatch):
        """A parity fixture under tests/fixtures/ is test data, not a plan body.

        ``_PLAN_BODY_RE`` matches ``docs/plans/`` at any depth, so the static
        emit-parity fixture denied here until 2026-08-04 -- an executor
        migrating a wire value across a fixture and its golden slice could not
        touch the fixture half, splitting a lockstep pair.
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )

        payload = _payload(
            tmp_path,
            "coordinator_core/ops/emit/tests/fixtures/root/docs/plans/2026-07-02-fixture-plan-beta.md",
        )
        assert guard.check(payload) is None

    def test_executor_write_to_plans_under_bare_tests_still_blocked(self, tmp_path, monkeypatch):
        """Negative-spec -- the exemption needs `tests/fixtures/`, not `tests/`.

        A bare ``tests/`` segment must not open the exemption, or any executor
        could reach a real plan body by routing through a `tests/` directory.
        Still routes through the guard (fast-exit regex fires); C16 narrows
        WHICH plan is a hard deny, not whether this path reaches that gate,
        so seed the sidecar with the same key the write targets.
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)
        monkeypatch.setattr(guard, "_write_hook_emit_log", lambda *a, **kw: None)
        _seed_executing_sidecar(tmp_path, "sess-12345678", "docs/plans/2026-07-24-x.md")

        payload = _payload(tmp_path, "pkg/tests/docs/plans/2026-07-24-x.md")
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_executor_write_to_plans_still_blocked(self, tmp_path, monkeypatch):
        """Regression -- pre-existing docs/plans/** executor-block case,
        now gated on the write matching the sidecar-declared executing plan
        (C16). This is THE verification the reshape lives or dies on: if
        the narrowed condition stops firing here, the guard has been
        removed, not reshaped.
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)
        monkeypatch.setattr(guard, "_write_hook_emit_log", lambda *a, **kw: None)
        _seed_executing_sidecar(tmp_path, "sess-12345678", "docs/plans/2026-07-24-x.md")

        payload = _payload(tmp_path, "docs/plans/2026-07-24-x.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_executor_write_to_different_plan_advises_not_denies(self, tmp_path, monkeypatch):
        """C16 pin, negative direction -- a DIFFERENT plan body than the one
        this session's sidecar names as executing advises, it does not deny.
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )
        _seed_executing_sidecar(tmp_path, "sess-12345678", "docs/plans/2026-07-24-x.md")

        payload = _payload(tmp_path, "docs/plans/some-unrelated-plan.md")
        result = guard.check(payload)

        assert result is not None
        hso = result["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert "some-unrelated-plan.md" in hso["additionalContext"]

    def test_executor_no_sidecar_at_all_advises(self, tmp_path, monkeypatch):
        """No run-report sidecar exists for this session at all (e.g. a
        genuinely ad-hoc executor dispatch) -- the guard cannot prove this
        write hits an executing body, so it must not hard-deny.
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )

        payload = _payload(tmp_path, "docs/plans/2026-07-24-x.md")
        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]


class TestNonExecutorPassThrough:
    """AC2 — the fast-exit regex widening must not change the downstream
    subagent_type gate: a non-executor subagent writing to docs/problems/**
    still allows through.
    """

    def test_general_purpose_write_to_problems_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("general-purpose")
        )

        payload = _payload(tmp_path, "docs/problems/2026-07-24-x.md")
        assert guard.check(payload) is None

    def test_general_purpose_write_to_plans_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("general-purpose")
        )

        payload = _payload(tmp_path, "docs/plans/2026-07-24-x.md")
        assert guard.check(payload) is None


class TestKindResolutionFailureIsMeasuredNotConfined:
    """2026-07-30: an EARLIER dispatch confined an unresolvable kind here
    exactly like block_subagent_commit/block_subagent_destructive_action do
    -- REVERTED same day. This guard is per-kind policy, not uniform-deny:
    coordinator:enricher and coordinator:review-integrator are legitimate
    plan-body editors by design, so an unresolvable kind cannot be assumed
    to be executor without reproducing the exact harm the 2026-06-09
    PM ruling ("don't punish legitimate integrator/enricher work on infra
    noise") was written to prevent. The VERDICT is reverted to that ruling's
    original lookup-fail-is-allow default; the measurement-only stderr
    signal is kept so a future, properly-scoped PM conversation about that
    ruling has real frequency data.
    """

    def test_unresolvable_kind_still_allows(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        # Lookup-miss: backpointer chain returns "" (missing/unreadable).
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))

        payload = _payload(tmp_path, "docs/plans/2026-07-30-x.md")
        assert guard.check(payload) is None
        # Measurement-only signal still fires even though the verdict allows.
        assert "kind-resolution-failed" in capsys.readouterr().err

    def test_em_caller_no_agent_id_allowed_and_silent(self, tmp_path, monkeypatch, capsys):
        """The EM main-loop (no agent_id in the payload at all) is not a
        subagent at all -- allowed, and the instrumentation signal (which is
        about SUBAGENT kind-resolution failures specifically) must not fire.
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))

        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "docs/plans/2026-07-30-x.md",
                "old_string": "x",
                "new_string": "y",
            },
            "cwd": str(tmp_path),
        }
        assert guard.check(payload) is None
        assert capsys.readouterr().err == ""

    def test_resolvable_non_executor_kind_allowed_and_silent(self, tmp_path, monkeypatch, capsys):
        """A resolvable kind that isn't coordinator:executor allows exactly
        as before, and does not trip the kind-UNRESOLVED instrumentation
        (its kind resolved fine -- it just isn't executor).
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:enricher")
        )

        payload = _payload(tmp_path, "docs/plans/2026-07-30-x.md")
        assert guard.check(payload) is None
        assert capsys.readouterr().err == ""

    def test_resolvable_executor_kind_still_denied_and_silent(self, tmp_path, monkeypatch, capsys):
        """A resolvable coordinator:executor kind denies exactly as before,
        when the write targets the sidecar-declared executing plan (C16).
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)
        _seed_executing_sidecar(tmp_path, "sess-12345678", "docs/plans/2026-07-30-x.md")

        payload = _payload(tmp_path, "docs/plans/2026-07-30-x.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert capsys.readouterr().err == ""


class TestAmbiguousBranchMessageNamesIdentifyingDetail:
    """Review finding (coordinatorcode-reviewer-54284751, Finding 2, P1):
    the AMBIGUOUS collision-sentinel deny branch is this guard's
    unconditional fail-closed path, and the compressed message must still
    name BOTH the blocked file_path and the colliding agent_id -- an
    operator/EM acting on "ask the EM to re-dispatch cleanly" has nothing
    else in the message to identify the collision by. Pins the dropped
    detail so a future compression pass cannot silently re-drop it (nothing
    pinned this before -- see the review's exit interview).
    """

    def test_ambiguous_branch_names_file_path_and_agent_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("AMBIGUOUS")
        )
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(
            tmp_path,
            "docs/plans/2026-08-03-x.md",
            agent_id="aprobe2-teammate-64cd7f42c270a899",
            session_id="3819c0e9-edc6-449d-a6f5-1bb3ae2c3ca0",
        )
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "docs/plans/2026-08-03-x.md" in reason
        assert "probe2-teammate@session-3819c0e9" in reason
        assert "ambiguous agent identity" in reason
