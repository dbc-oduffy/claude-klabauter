"""Tests for coordinator_core.bash_guards.guard_head_tail_rewrite's
PowerShell-dialect leg (row 13, docs/reference/guard-dialect-coverage.md).

The bash leg (`check_head_tail_plumbing_rewrite(cmd, session_id)`, no
`dialect` argument) is exercised by `test_bx16_multiprobe_and_headtail_
rewrite.py` and left byte-for-byte unchanged by this cohort's conversion
(AC4) -- this file covers ONLY the new `dialect=Dialect.POWERSHELL` branch:
`Select-Object -First`/`-Last` recognized in place of `head`/`tail`, reusing
the dialect-neutral `find`/`ls`/grep-family upstream recognition unchanged.

Spec backlink: coordinator_core/bash_guards/guard_head_tail_rewrite.py
"""

from __future__ import annotations

from coordinator_core.bash_guards import guard_head_tail_rewrite as guard
from coordinator_core.bash_guards import _dialect


def _ps_rewrite(cmd, session_id="sess1"):
    return guard.check_head_tail_plumbing_rewrite(
        cmd, session_id, dialect=_dialect.Dialect.POWERSHELL
    )


class TestPowerShellSelectObjectFirstLast:
    def test_ls_piped_into_select_object_first_rewrites(self):
        out = _ps_rewrite("ls . | Select-Object -First 5")
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert "updatedInput" in hso
        assert "python3" in hso["updatedInput"]["command"] or "python" in hso[
            "updatedInput"
        ]["command"]

    def test_select_object_last_rewrites(self):
        out = _ps_rewrite("ls . | Select-Object -Last 3")
        assert out is not None
        assert "updatedInput" in out["hookSpecificOutput"]

    def test_sls_alias_for_select_object_recognized(self):
        out = _ps_rewrite("ls . | select -First 5")
        assert out is not None
        assert "updatedInput" in out["hookSpecificOutput"]

    def test_unrecognized_count_form_advises_not_rewrites(self):
        out = _ps_rewrite("ls . | Select-Object -Skip 1")
        assert out is not None
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_first_and_last_together_advises_not_rewrites(self):
        out = _ps_rewrite("ls . | Select-Object -First 5 -Last 2")
        assert out is not None
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_longer_chain_advises_not_rewrites(self):
        out = _ps_rewrite("ls . | Sort-Object | Select-Object -First 5")
        assert out is not None
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_unrecognized_upstream_generator_advises(self):
        out = _ps_rewrite("Get-Process | Select-Object -First 5")
        assert out is not None
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_no_select_object_at_all_is_a_genuine_clean(self):
        assert _ps_rewrite("Get-Process") is None

    def test_empty_command_allows(self):
        assert _ps_rewrite("") is None

    def test_bash_leg_unchanged_when_dialect_omitted(self):
        # No `dialect` argument at all -- must reproduce the exact
        # pre-existing bash-only behavior (AC4).
        out = guard.check_head_tail_plumbing_rewrite("find . -type f | head -n 5")
        assert out is not None
        assert "updatedInput" in out["hookSpecificOutput"]
