"""Tests for coordinator_core.bash_guards.block_disarm_marker_sentinel_creation.

Covers the DENY set (redirection, `touch`/`cp`/`mv`/`install`/`ln`/`tee`,
`sed -i`, `python -c`, `dd of=`), the ALLOW set (reads and `rm`), chaining/
env-assignment shell shapes, indirection-wrapper hardening, and -- critically
-- dispatch-level (end-to-end via `dispatch.evaluate_payload_json`) coverage:
`offer-git-c`'s allow+updatedInput short-circuit is exactly what hid the
analogous ordering bug in `block_worktree_creation.py`/`block_worktree_
sentinel_creation.py`, and a guard-level-only suite would stay green while
this guard is unreachable for `cd <dir> && <cmd>` shapes.

Also covers the property this guard specifically exists to close: a
blanket-disarm marker -- however it is scoped or which bands it names --
can NEVER suppress this guard, because it is registered in
`GuardBand.CONFINEMENT_DENY`, the one band no marker can ever cover.

Pure Python -- no shell spawns, no filesystem writes.

Spec backlink: coordinator_core/bash_guards/block_disarm_marker_sentinel_creation.py
Precedent test shape: coordinator_core/bash_guards/tests/test_block_worktree_sentinel_creation.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.bash_guards import _blanket_disarm as bd
from coordinator_core.bash_guards import block_disarm_marker_sentinel_creation as guard
from coordinator_core.bash_guards import block_worktree_sentinel_creation as worktree_guard
from coordinator_core.bash_guards import block_approval_sentinel_creation as doctrine_guard
from coordinator_core.bash_guards import dispatch


SENTINEL = bd.MARKER_BASENAME
WORKTREE_SENTINEL = ".coordinator-override-worktree-guard"
DOCTRINE_SENTINEL = ".coordinator-doctrine-edit-approved"


def _payload(command, agent_id=None, agent_type=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _reason(out):
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_no_sentinel_mention_allows(self):
        assert guard.check(_payload("git status && ls -la")) is None


class TestRedirectionDenies:
    def test_bare_gt_denies(self):
        _reason(guard.check(_payload("echo ok > %s" % SENTINEL)))

    def test_attached_gt_denies(self):
        _reason(guard.check(_payload("echo ok >%s" % SENTINEL)))

    def test_append_denies(self):
        _reason(guard.check(_payload("echo ok >> %s" % SENTINEL)))

    def test_redirect_into_settings_home_denies(self):
        # The real marker path is settings-home-scoped, not repo-root --
        # basename matching must still catch it under an arbitrary prefix.
        _reason(guard.check(_payload("echo ok > /home/op/.coordinator-claude-settings/%s" % SENTINEL)))


class TestCaseFoldedTargetMatch:
    """H4 (2026-07-30 M13/M19 review finding): the filesystem this fleet
    defaults to (macOS APFS) is case-insensitive-but-case-preserving, so
    `touch .COORDINATOR-BASH-GUARDS-DISARMED` creates a file
    `_blanket_disarm`'s `path.exists()` reader finds as the live marker --
    the Bash-leg detector must deny it too, not only the exact-case
    spelling."""

    def test_upper_case_sentinel_denies(self):
        _reason(guard.check(_payload("touch %s" % SENTINEL.upper())))

    def test_mixed_case_sentinel_denies(self):
        mixed = "".join(
            c.upper() if i % 2 == 0 else c for i, c in enumerate(SENTINEL)
        )
        _reason(guard.check(_payload("touch %s" % mixed)))

    def test_upper_case_sentinel_denies_end_to_end(self):
        out = dispatch.evaluate_payload_json(
            json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "touch %s" % SENTINEL.upper()}}
            )
        )
        assert out and '"deny"' in json.dumps(out)

    def test_case_varied_python_c_mention_denies(self):
        code = "open('%s', 'w').close()" % SENTINEL.upper()
        _reason(guard.check(_payload("python3 -c \"%s\"" % code)))


class TestFileArgCommandsDeny:
    def test_touch_denies(self):
        _reason(guard.check(_payload("touch %s" % SENTINEL)))

    def test_touch_with_settings_home_prefix_denies(self):
        _reason(guard.check(_payload("touch $COORDINATOR_SETTINGS_HOME/%s" % SENTINEL)))

    def test_cp_dest_denies(self):
        _reason(guard.check(_payload("cp somefile %s" % SENTINEL)))

    def test_mv_denies(self):
        _reason(guard.check(_payload("mv somefile %s" % SENTINEL)))

    def test_tee_denies(self):
        _reason(guard.check(_payload("echo ok | tee %s" % SENTINEL)))


class TestSedInplaceDenies:
    def test_bare_dash_i_denies(self):
        _reason(guard.check(_payload("sed -i 's/Scope: time/Scope: machine-total/' %s" % SENTINEL)))

    def test_sed_without_inplace_flag_allows(self):
        assert guard.check(_payload("sed 's/a/b/' %s" % SENTINEL)) is None


class TestPythonDashCDenies:
    def test_open_write_mode_denies(self):
        code = "open('%s', 'w').close()" % SENTINEL
        _reason(guard.check(_payload("python3 -c \"%s\"" % code)))

    def test_unrelated_python_c_allows(self):
        assert guard.check(_payload("python3 -c \"print('hello world')\"")) is None


class TestDdOfDenies:
    def test_dd_of_denies(self):
        _reason(guard.check(_payload("dd if=/dev/null of=%s" % SENTINEL)))

    def test_dd_of_with_conv_denies(self):
        _reason(guard.check(_payload("dd if=/dev/null of=%s conv=notrunc" % SENTINEL)))


class TestChainedAndEnvPrefixedShapes:
    def test_semicolon_chained_denies(self):
        _reason(guard.check(_payload("cd /tmp; touch %s" % SENTINEL)))

    def test_and_chained_denies(self):
        _reason(guard.check(_payload("cd /tmp && touch %s" % SENTINEL)))

    def test_leading_env_assignment_still_denies(self):
        _reason(guard.check(_payload("FOO=bar touch %s" % SENTINEL)))

    def test_git_dash_c_prefixed_denies(self):
        _reason(guard.check(_payload("git -C /tmp status; touch %s" % SENTINEL)))


class TestAllowReadsAndRemoval:
    def test_rm_allows(self):
        assert guard.check(_payload("rm %s" % SENTINEL)) is None

    def test_cat_allows(self):
        assert guard.check(_payload("cat %s" % SENTINEL)) is None

    def test_ls_allows(self):
        assert guard.check(_payload("ls -la %s" % SENTINEL)) is None

    def test_stat_allows(self):
        assert guard.check(_payload("stat %s" % SENTINEL)) is None

    def test_test_and_rm_allows(self):
        assert guard.check(_payload("test -f %s && rm %s" % (SENTINEL, SENTINEL))) is None

    def test_unrelated_touch_allows(self):
        assert guard.check(_payload("touch somefile.txt")) is None

    def test_near_miss_filename_allows(self):
        assert guard.check(_payload("touch %s-typo" % SENTINEL)) is None


class TestDoesNotCatchTheOtherSentinels:
    """This guard protects a THIRD, different sentinel than the two prior
    sentinel-creation guards -- confirm none of the three cross-catches
    another's target basename."""

    def test_disarm_guard_does_not_catch_worktree_sentinel(self):
        assert guard.check(_payload("touch %s" % WORKTREE_SENTINEL)) is None

    def test_disarm_guard_does_not_catch_doctrine_sentinel(self):
        assert guard.check(_payload("touch %s" % DOCTRINE_SENTINEL)) is None

    def test_worktree_guard_does_not_catch_disarm_sentinel(self):
        assert worktree_guard.check(_payload("touch %s" % SENTINEL)) is None

    def test_doctrine_guard_does_not_catch_disarm_sentinel(self):
        assert doctrine_guard.check(_payload("touch %s" % SENTINEL)) is None


class TestNotIdentityGated:
    def test_denies_without_any_identity_fields(self):
        _reason(guard.check(_payload("touch %s" % SENTINEL)))

    def test_denies_with_subagent_identity(self):
        out = guard.check(
            _payload(
                "touch %s" % SENTINEL,
                agent_id="a0123456789abcdef",
                agent_type="coordinator:executor",
            )
        )
        _reason(out)


class TestNoOverride:
    def test_env_var_shaped_override_does_not_allow(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_DISARM_MARKER_GUARD", "1")
        _reason(guard.check(_payload("touch %s" % SENTINEL)))


class TestDenyMessageDiscipline:
    def test_deny_reason_never_names_the_sentinel(self):
        reason = _reason(guard.check(_payload("touch %s" % SENTINEL)))
        assert SENTINEL not in reason

    def test_deny_reason_is_offer_shaped(self):
        reason = _reason(guard.check(_payload("touch %s" % SENTINEL)))
        assert "!-prefixed prompt" not in reason
        assert "bypasses this hook entirely" not in reason
        assert "EM/PM" in reason


class TestReasonClassSpecificMessages:
    def test_direct_deny_message_unchanged(self):
        reason = _reason(guard.check(_payload("touch %s" % SENTINEL)))
        assert "this command would create or modify the file" in reason
        assert SENTINEL not in reason

    def test_indirection_deny_does_not_assert_creation(self):
        reason = _reason(guard.check(_payload("bash bin/some-script.sh")))
        assert "this command would create or modify the file" not in reason
        assert "cannot examine" in reason
        assert "NOT because the payload was found" in reason
        assert SENTINEL not in reason

    def test_recursive_indirection_deny_still_redacts_the_sentinel(self):
        reason = _reason(guard.check(_payload('bash -c "touch %s"' % SENTINEL)))
        assert SENTINEL not in reason
        assert "cannot examine" in reason


class TestIndirectionWrapperShapesDeny:
    def test_bash_dash_c_denies(self):
        _reason(guard.check(_payload('bash -c "touch %s"' % SENTINEL)))

    def test_env_sh_dash_c_denies(self):
        _reason(guard.check(_payload('env sh -c "touch %s"' % SENTINEL)))

    def test_xargs_denies(self):
        _reason(guard.check(_payload("echo %s | xargs touch" % SENTINEL)))

    def test_heredoc_fed_bash_denies(self):
        cmd = "bash <<'EOF'\ntouch %s\nEOF" % SENTINEL
        _reason(guard.check(_payload(cmd)))

    def test_unrelated_bash_dash_c_allows(self):
        assert guard.check(_payload('bash -c "echo hello"')) is None

    def test_python_dash_m_allows(self):
        assert guard.check(_payload("python3 -m pytest")) is None


class TestReachableThroughTheDispatchChain:
    """Guard-level tests are not sufficient -- this guard must sit ahead of
    `offer-git-c` in the real registered chain, same regression class as
    the two sibling sentinel guards (see `block_worktree_sentinel_
    creation.py`'s own test module docstring)."""

    @staticmethod
    def _decision(command):
        out = dispatch.evaluate_payload_json(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        )
        return "deny" if (out and '"deny"' in json.dumps(out)) else "allow"

    def test_bare_touch_denied_end_to_end(self):
        assert self._decision("touch %s" % SENTINEL) == "deny"

    def test_cd_prefixed_touch_denied_end_to_end(self):
        assert self._decision("cd /tmp && touch %s" % SENTINEL) == "deny"

    def test_git_dash_c_prefixed_touch_denied_end_to_end(self):
        assert self._decision("git -C /tmp status; touch %s" % SENTINEL) == "deny"

    def test_rm_allowed_end_to_end(self):
        assert self._decision("rm %s" % SENTINEL) == "allow"

    def test_unrelated_touch_allowed_end_to_end(self):
        assert self._decision("touch somefile.txt") == "allow"

    def test_bash_dash_c_denied_end_to_end(self):
        assert self._decision('bash -c "touch %s"' % SENTINEL) == "deny"


class TestMarkerCannotSuppressThisGuard:
    """The property this guard exists to close: `GuardBand.CONFINEMENT_
    DENY` is unconditionally non-suppressible by ANY blanket-disarm marker
    (see `_blanket_disarm.py`'s own "BAND-SCOPED SUPPRESSION"), so a live,
    active, machine-total, EM-scoped disarm marker must still leave THIS
    guard denying -- proving the loop is actually closed, not merely
    documented as closed.
    """

    @pytest.fixture(autouse=True)
    def _isolated_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bd, "settings_home", lambda: tmp_path)
        bd._cache.clear()
        yield
        bd._cache.clear()

    @staticmethod
    def _write_marker(tmp_path, text):
        (tmp_path / bd.MARKER_BASENAME).write_text(text, encoding="utf-8")

    @staticmethod
    def _decision(command):
        out = dispatch.evaluate_payload_json(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "session_id": "sess-em-marker",
                }
            )
        )
        return "deny" if (out and '"deny"' in json.dumps(out)) else "allow"

    def test_active_machine_total_marker_naming_advisory_rewrite_still_denies(self, tmp_path):
        now = datetime.now(timezone.utc)
        self._write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {now.isoformat()}\n"
            "Bands: advisory-rewrite,platform-conditioned-deny\nReason: dev box\n",
        )
        # Sanity: the marker really is active (otherwise this test would
        # pass vacuously).
        assert bd.disarm_status({"session_id": "sess-em-marker"}).active is True
        assert self._decision("touch %s" % SENTINEL) == "deny"

    def test_marker_attempting_to_name_confinement_deny_is_rejected_and_still_denies(self, tmp_path):
        now = datetime.now(timezone.utc)
        self._write_marker(
            tmp_path,
            f"Scope: machine-total\nSince: {now.isoformat()}\n"
            "Bands: advisory-rewrite,confinement-deny\nReason: dev box\n",
        )
        # The whole marker is rejected for naming confinement-deny -- it is
        # not even "active" in the band-blind sense any more (M18).
        assert bd.disarm_status({"session_id": "sess-em-marker"}).active is False
        assert self._decision("touch %s" % SENTINEL) == "deny"

    def test_creating_the_marker_itself_is_the_denied_action(self, tmp_path):
        """The sharpest form of the loop-closure property: even the Bash
        command that would WRITE a fresh, more permissive disarm marker is
        itself denied by this guard before it ever reaches disk."""
        assert self._decision("touch %s" % SENTINEL) == "deny"
        assert not (tmp_path / bd.MARKER_BASENAME).exists()


class TestPowerShellDialect:
    """C4e (2026-08-07, guard-dialect-coverage.md row 23): PowerShell-syntax
    coverage via `_dialect`/`_sentinel_creation_guard.SentinelCreationDetector
    .evaluate_for_dialect`. A guard declaring `Dialect.POWERSHELL` must reach
    a correct verdict or record SILENT -- never a bare clean (AC3)."""

    @staticmethod
    def _ps_payload(command):
        return {
            "tool_name": "PowerShell",
            "tool_input": {"command": command},
            "session_id": "sess1",
            "cwd": "/repo",
        }

    def test_new_item_cmdlet_denies(self):
        out = guard.check(self._ps_payload("New-Item %s" % SENTINEL))
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_redirect_denies(self):
        out = guard.check(self._ps_payload("echo ok > %s" % SENTINEL))
        assert out is not None

    def test_unrelated_command_allows_with_no_silence(self):
        from coordinator_core.bash_guards import _verdict

        with _verdict.collecting() as silences:
            out = guard.check(self._ps_payload("Get-ChildItem"))
        assert out is None
        assert silences == []

    def test_grammar_gap_shape_records_silent_not_clean(self):
        from coordinator_core.bash_guards import _verdict

        with _verdict.collecting() as silences:
            out = guard.check(self._ps_payload("Remove-Item x &> out.txt"))
        assert out is None
        assert any(
            s.guard_name == "block_disarm_marker_sentinel_creation" for s in silences
        )

    def test_non_bash_non_powershell_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None
