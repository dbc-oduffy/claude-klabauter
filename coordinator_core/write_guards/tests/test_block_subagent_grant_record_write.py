"""Behavioral tests for
coordinator_core.write_guards.block_subagent_grant_record_write -- the
Write-tool-channel leg that denies a dispatched subagent's direct
Write/Edit/MultiEdit/NotebookEdit against the CLAUDE.md write-grant record
itself (chunk C9,
docs/plans/2026-08-08-discriminate-the-caller-on-the-write-grant.md).

Three ACs pinned here:

  AC12 (TestBaseCases) -- EM-inline write allowed; subagent write to the
      grant record denied across all four MATCHERS tools; a sibling path in
      the same session directory (not the grant record) allowed -- proves
      filename-anchored, not directory-anchored; a similarly-named file
      OUTSIDE `.git/coordinator-sessions/` allowed -- proves path-anchored,
      not filename-alone; a non-MATCHERS tool_name allowed (defense-in-
      depth pin).

  AC13 (TestScopeEqualsEnforcement) -- real scope equals stated scope, per
      DR-104 requirement (2): deny EXACTLY
      `.git/coordinator-sessions/<sid>/claude-md-write-grant.json` -- no
      WIDER (other files in that directory allowed) and no NARROWER (any
      `<sid>` value matches; the guard is session-id-agnostic). Named
      precedent: DR-104 cites `check_blanket_git_add`'s documented scope
      gap (doctrine claimed a wider deny scope than the code enforced) --
      this case exists so C9 does not repeat that gap. Reuses the shape
      `test_block_unauthorized_claude_md_write.py::TestRealScopeEqualsStatedScope`
      already established for the sibling grant-adjacent guard.

  AC14 (TestLinkedWorktreeResolution) -- the resolution case: a synthetic
      linked-worktree fixture (mirroring
      `coordinator_core/git/test_git_dir.py::
      test_linked_worktree_absolute_gitdir_with_commondir_resolves_to_common`)
      proves the guard gates the grant record at its git-common-dir-
      RESOLVED location, not a literal `<worktree>/.git`-joined path -- a
      lexical-only match would silently fail to guard the real write target
      in a linked worktree.

Plus a registration-reachability case (TestReachableThroughEngine): the
dispatcher entrypoint `coordinator_core.write_guards.engine.check()` (not
this module's `check()` directly) denies the same subagent-shaped payload,
confirming the leg is reachable through auto-discovery -- a second,
independent check on the same property C10 already pins via
`_discover_guards()`.

Payload/assertion shape follows
`test_block_unauthorized_claude_md_write.py`'s conventions (a `_payload`
builder, `_deny`/`_allow` helpers).
"""

from __future__ import annotations

from coordinator_core.session.claude_md_grant import _GRANT_FILENAME
from coordinator_core.write_guards import block_subagent_grant_record_write as guard
from coordinator_core.write_guards import engine


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


def _notebook_payload(notebook_path: str, *, agent_id: str, cwd: str) -> dict:
    return {
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": notebook_path},
        "cwd": cwd,
        "agent_id": agent_id,
    }


def _deny(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is not None, f"expected DENY for: {file_path!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def _allow(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is None, f"expected ALLOW for: {file_path!r}, got {result!r}"


def _make_plain_clone(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _grant_path(repo_root, sid="sess-123"):
    return str(repo_root / ".git" / "coordinator-sessions" / sid / _GRANT_FILENAME)


class TestBaseCases:
    def test_em_inline_write_allowed_no_agent_id(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _allow(_grant_path(repo_root), agent_id="", cwd=str(repo_root))

    def test_subagent_write_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _deny(_grant_path(repo_root), tool_name="Write", cwd=str(repo_root))

    def test_subagent_edit_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _deny(_grant_path(repo_root), tool_name="Edit", cwd=str(repo_root))

    def test_subagent_multiedit_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _deny(_grant_path(repo_root), tool_name="MultiEdit", cwd=str(repo_root))

    def test_subagent_notebookedit_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        result = guard.check(
            _notebook_payload(
                _grant_path(repo_root),
                agent_id="aexecutor-teammate-1234567890abcdef",
                cwd=str(repo_root),
            )
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_sibling_path_in_same_session_dir_allowed(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        sibling = str(
            repo_root
            / ".git"
            / "coordinator-sessions"
            / "sess-123"
            / "plan-body-write-block.log"
        )
        _allow(sibling, cwd=str(repo_root))

    def test_similarly_named_file_outside_coordinator_sessions_allowed(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        outside = str(repo_root / "somewhere" / "else" / _GRANT_FILENAME)
        _allow(outside, cwd=str(repo_root))

    def test_non_matcher_tool_name_allowed(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _allow(_grant_path(repo_root), tool_name="Read", cwd=str(repo_root))


class TestScopeEqualsEnforcement:
    def test_denies_exactly_the_grant_record_not_other_files_in_dir(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        _deny(_grant_path(repo_root, sid="sess-abc"), cwd=str(repo_root))
        _allow(
            str(
                repo_root
                / ".git"
                / "coordinator-sessions"
                / "sess-abc"
                / "some-other-record.json"
            ),
            cwd=str(repo_root),
        )

    def test_denies_regardless_of_specific_session_id_value(self, tmp_path):
        """No NARROWER: the guard does not require a specific `<sid>` value
        -- it matches ANY session-id segment (the exactly-two-remaining-
        segments check `<sid>/<filename>`)."""
        repo_root = _make_plain_clone(tmp_path)
        for sid in ("sess-abc", "another-session-id", "0123456789abcdef"):
            _deny(_grant_path(repo_root, sid=sid), cwd=str(repo_root))


class TestLinkedWorktreeResolution:
    def test_subagent_write_through_worktree_resolves_to_common_dir_and_is_denied(
        self, tmp_path
    ):
        common_dir = tmp_path / "main" / ".git"
        private_gitdir = common_dir / "worktrees" / "wt"
        private_gitdir.mkdir(parents=True)
        (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")

        repo_root = tmp_path / "wt"
        repo_root.mkdir()
        (repo_root / ".git").write_text(
            f"gitdir: {private_gitdir}\n", encoding="utf-8"
        )

        grant_path = str(
            common_dir / "coordinator-sessions" / "sess-123" / _GRANT_FILENAME
        )

        _deny(grant_path, cwd=str(repo_root))

    def test_literal_worktree_dot_git_joined_path_is_not_the_check(self, tmp_path):
        common_dir = tmp_path / "main" / ".git"
        private_gitdir = common_dir / "worktrees" / "wt"
        private_gitdir.mkdir(parents=True)
        (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")

        repo_root = tmp_path / "wt"
        repo_root.mkdir()
        (repo_root / ".git").write_text(
            f"gitdir: {private_gitdir}\n", encoding="utf-8"
        )

        literal_joined = str(
            repo_root / ".git" / "coordinator-sessions" / "sess-123" / _GRANT_FILENAME
        )
        assert literal_joined != str(
            common_dir / "coordinator-sessions" / "sess-123" / _GRANT_FILENAME
        )


# `block_memo_status_hand_edit.py`'s `_TRAVERSAL_RE` reject and


class TestTraversalAndCaseFoldBypasses:
    def test_traversal_segment_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        traversal_path = str(
            repo_root
            / ".git"
            / "coordinator-sessions"
            / "sess-123"
            / "x"
            / ".."
            / _GRANT_FILENAME
        )
        _deny(traversal_path, cwd=str(repo_root))

    def test_differently_cased_candidate_denied(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        cased_path = str(
            repo_root
            / ".git"
            / "Coordinator-Sessions"
            / "SESS-123"
            / _GRANT_FILENAME.upper()
        )
        _deny(cased_path, cwd=str(repo_root))


class TestUNCPathHandling:
    def test_normalize_path_preserves_unc_leading_slash(self):
        assert guard._normalize_path(
            r"\\server\share\.git\coordinator-sessions\sid\x.json"
        ) == "//server/share/.git/coordinator-sessions/sid/x.json"

    def test_unc_shaped_common_dir_still_denies_subagent_write(self, monkeypatch):
        unc_common_dir = r"\\fileserver\repos\claude-klabauter\.git"
        monkeypatch.setattr(
            guard, "_resolve_git_common_dir", lambda cwd: unc_common_dir
        )
        grant_path = (
            unc_common_dir + "\\coordinator-sessions\\sess-123\\" + _GRANT_FILENAME
        )
        _deny(grant_path, cwd="irrelevant")


class TestTraversalResolutionScopeEqualsEnforcement:
    def test_unrelated_traversal_path_allowed(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        unrelated = str(repo_root / "some" / "dir" / ".." / "other" / "file.py")
        _allow(unrelated, cwd=str(repo_root))

    def test_traversal_path_resolving_onto_grant_record_denied(self, tmp_path):
        """A `..`-bearing candidate that RESOLVES onto the grant record
        must still DENY -- keeps the original bypass closed even after the
        over-deny fix (this is a second traversal shape from
        `test_traversal_segment_denied`, using a `../` that steps out of
        and back into the session directory)."""
        repo_root = _make_plain_clone(tmp_path)
        traversal_path = str(
            repo_root
            / ".git"
            / "coordinator-sessions"
            / "sess-123"
            / "sub"
            / ".."
            / ".."
            / "sess-123"
            / _GRANT_FILENAME
        )
        _deny(traversal_path, cwd=str(repo_root))


class TestReachableThroughEngine:
    def test_engine_check_denies_subagent_grant_record_write(self, tmp_path):
        repo_root = _make_plain_clone(tmp_path)
        payload = _payload(
            _grant_path(repo_root), tool_name="Write", cwd=str(repo_root)
        )
        result = engine.evaluate(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
