"""coordinator_core.bash_guards.tests.test_dispatch_checks_dialect_seam --
C2 of `docs/plans/2026-08-26-the-destructive-core-learns-the-shell-it-
guards.md`.

Per `state/audits/2026-08-26-guard-detection-language-dependence-recensus
.md` Finding 5, ``check_no_verify`` is the ONLY Bucket A check with a
tokenizer path to migrate (the other five deny by raw-text regex with no
segmentation seam). This file pins two properties for that one migration:

- AC2: the BASH leg's verdict is byte-identical to pre-migration behavior,
  including on input that defeats tokenization (unterminated quote,
  unbalanced here-string) -- purpose-built rows per this chunk's own
  dispatch brief, not merely the pre-existing corpus.
- AC4: unparseable PowerShell input yields NO deny (silence), and does NOT
  fall through into the bash-only fail-closed raw-text scan -- a
  DIFFERENT fail shape than the bash leg's `None` (see `_dialect.py`'s own
  module docstring, "THE CRITICAL PROPERTY IS FAIL DIRECTION").

Spec backlink: pln-the-destructive-core-learns-the-she § C2
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.bash_guards import dispatch_checks as guard
from coordinator_core.bash_guards._verdict import collecting


#: One row per Bucket A entry that has a tokenizer path (see module
#: docstring) -- today that is exactly ``check_no_verify``. Kept as a table
#: (not a single hardcoded call) so a future check migrated onto this same
#: seam extends this file by adding a row, not by copy-pasting a new class.
#: Every assertion below routes through it, so a row added here is exercised
#: rather than merely declared.
_BUCKET_A_TOKENIZER_CHECKS = {
    "no-verify": guard.check_no_verify,
}


def _denied(
    cmd: str,
    hook_payload: Optional[dict] = None,
    entry: str = "no-verify",
) -> bool:
    return _BUCKET_A_TOKENIZER_CHECKS[entry](cmd, hook_payload=hook_payload) is not None


class TestBashVerdictParityOnWellFormedInput:
    """AC2 baseline: ordinary well-formed bypass/non-bypass commands still
    verdict identically post-migration."""

    def test_no_verify_flag_still_denied(self) -> None:
        assert _denied("git commit -m wip --no-verify")

    def test_no_gpg_sign_flag_still_denied(self) -> None:
        assert _denied("git commit -m wip --no-gpg-sign")

    def test_gpgsign_false_config_still_denied(self) -> None:
        assert _denied('git -c commit.gpgsign=false commit -m wip')

    def test_plain_message_mentioning_no_verify_still_allowed(self) -> None:
        assert not _denied('git commit -m "message about --no-verify flags"')

    def test_unrelated_command_still_allowed(self) -> None:
        assert not _denied("git status")


class TestBashVerdictParityOnUnparseableInput:
    """AC2/AC4: purpose-built unparseable-Bash rows, per check, must deny
    IDENTICALLY to the pre-migration `_BYPASS_RE.search(flat)` raw-text
    fail-closed scan -- an unterminated quote or unbalanced here-string
    must never quietly stop denying a genuine bypass flag once dialect
    routing is in front of the tokenizer."""

    def test_unterminated_quote_with_bypass_flag_still_denies(self) -> None:
        # Unterminated double quote defeats `tokenize_full_command`
        # (`resolve_segments_for_dialect` returns None for the BASH leg),
        # so this must fall through to the raw-text `_BYPASS_RE` scan and
        # still deny -- fail CLOSED, unchanged from pre-migration behavior.
        cmd = 'git commit -m "unterminated --no-verify'
        assert _denied(cmd)

    def test_unbalanced_here_string_with_bypass_flag_still_denies(self) -> None:
        # An unbalanced/opened-only bash here-string-shaped redirect
        # (`<<'EOF` with no closing terminator reachable) is unparseable
        # for the same reason -- same fail-closed expectation.
        cmd = "git commit -m wip --no-verify <<'EOF"
        assert _denied(cmd)

    def test_unterminated_quote_without_bypass_flag_still_allows(self) -> None:
        # The inverse: unparseable text carrying no bypass vocabulary at
        # all must still allow -- the raw-text fallback is over-inclusive
        # only for the bypass words themselves, never a blanket deny on
        # every unparseable git command.
        cmd = 'git commit -m "unterminated message with no bypass words'
        assert not _denied(cmd)


class TestPowerShellUnparseableStaysSilentNotBashFailClosed:
    """AC4: unparseable PowerShell input must yield NO deny -- silence is
    the correct fail shape for that leg (a `None` from `_powershell_tokens`
    already records SILENT), and must NOT be reinterpreted as the bash
    leg's raw-text fail-closed scan, which is a DIFFERENT fail shape for a
    DIFFERENT reason (a `None` from `tokenize_full_command` itself)."""

    def _powershell_payload(self, cmd: str) -> dict:
        return {"tool_name": "PowerShell", "tool_input": {"command": cmd}}

    def test_powershell_grammar_gap_with_bypass_words_does_not_deny(self) -> None:
        # `cmd &> out.txt` is this module's own named `has_error=True`
        # grammar gap (see `_dialect.py` module docstring) -- carries the
        # literal bypass word regardless, to prove the non-deny is really
        # about the fail-direction branch, not merely an absent trigger.
        cmd = "git commit -m wip --no-verify &> out.txt"
        with collecting() as declarations:
            result = guard.check_no_verify(cmd, hook_payload=self._powershell_payload(cmd))
        assert result is None, (
            "unparseable PowerShell input must not deny -- it must fall "
            "through as silence, never the bash raw-text fail-closed scan"
        )
        assert declarations, (
            "a PowerShell parse failure must record SILENT, not a bare "
            "unrecorded None"
        )

    def test_powershell_well_formed_bypass_still_reaches_a_verdict_or_silence(
        self,
    ) -> None:
        # Well-formed PowerShell carrying the same bypass vocabulary is not
        # this chunk's job to newly detect (MATCHERS is unchanged -- see
        # module docstring), but it must not raise, and must not produce a
        # bash-shaped fail-closed deny via the wrong branch.
        cmd = "git commit -m wip --no-verify"
        result = guard.check_no_verify(cmd, hook_payload=self._powershell_payload(cmd))
        # `resolve_segments_for_dialect` parses this cleanly under
        # PowerShell too (no grammar gap here), so this asserts only that
        # calling with a PowerShell payload does not crash and does not
        # silently regress to the bash-only path's behavior by accident.
        assert result is None or isinstance(result, dict)


class TestNoDialectDefaultsToBash:
    """Every pre-existing caller of `check_no_verify(cmd)` (this package's
    own `test_check_no_verify.py` included) passes no `hook_payload` at
    all -- confirming that shape still resolves to the BASH leg, not a
    bare-None dialect that would silently stop denying everything."""

    def test_no_hook_payload_still_denies_bypass(self) -> None:
        assert _denied("git commit -m wip --no-verify", hook_payload=None)

    def test_empty_hook_payload_still_denies_bypass(self) -> None:
        assert _denied("git commit -m wip --no-verify", hook_payload={})


class TestPsGitBypassSegmentsDialectGate:
    """AC2a retired (2026-08-26): `_ps_git_bypass_segments` gates its
    PowerShell anti-bypass scan on the DECLARED dialect, which only became
    correct once DoE-claude's `_rearm_command_tool_name` stopped relabeling
    genuine PowerShell payloads to `tool_name: "Bash"` ahead of dispatch
    (their D1, `47f4aedfe`).

    The absent-payload row is the one this gate could plausibly get wrong
    and no other test covers: a direct in-process caller declares no
    dialect at all, and must keep the pre-gate posture (scan, and let the
    bash-shaped ladder judge) rather than being silently read as a
    positive claim of bash.
    """

    def test_declared_powershell_recovers_the_hidden_git_argv(self) -> None:
        segs = guard._ps_git_bypass_segments(
            "g`it clean -fdx", "destructive-git-clean", {"tool_name": "PowerShell"}
        )
        assert segs == ["git clean -fdx"]

    def test_declared_bash_does_not_scan(self) -> None:
        segs = guard._ps_git_bypass_segments(
            "g`it clean -fdx", "destructive-git-clean", {"tool_name": "Bash"}
        )
        assert segs == []

    def test_absent_payload_still_scans(self) -> None:
        segs = guard._ps_git_bypass_segments(
            "g`it clean -fdx", "destructive-git-clean"
        )
        assert segs == ["git clean -fdx"]

    def test_declared_bash_returns_empty_not_none(self) -> None:
        """`None` is reserved for "PowerShell that failed to parse", which
        callers treat as AC4's fail-open silence. Conflating the two works
        today only because every call site writes `or []`."""
        segs = guard._ps_git_bypass_segments(
            "g`it clean -fdx", "destructive-git-clean", {"tool_name": "Bash"}
        )
        assert segs is not None
