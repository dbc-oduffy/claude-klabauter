"""Tests for coordinator_core.bash_guards.guard_head_tail_rewrite's
PowerShell-dialect leg (row 13, docs/reference/guard-dialect-coverage.md)
and (C1, docs/plans/2026-08-21-the-advisory-band-gets-smaller-cheaper-and-
honest.md) the in-process `find`-census SERVE path.

The bash leg (`check_head_tail_plumbing_rewrite(cmd, session_id)`, no
`dialect` argument) is exercised by `test_bx16_multiprobe_and_headtail_
rewrite.py` and left byte-for-byte unchanged by this cohort's conversion
(AC4) -- this file covers ONLY the new `dialect=Dialect.POWERSHELL` branch:
`Select-Object -First`/`-Last` recognized in place of `head`/`tail`, reusing
the dialect-neutral `find`/`ls`/grep-family upstream recognition unchanged.

`TestFindCensusServesInProcess` below covers C1's new SERVE path -- calls
that carry a real `payload` (the shape `dispatch.py`'s chain entry always
supplies) for a `find`-census shape `coordinator_core.search.census`
certifies faithful get an in-process ANSWER (`updatedInput.command ==
"true"`), not a `python3 -c` rewrite; every existing rewrite-shaped test
above and in the sibling `test_bx16_...` file calls `check_head_tail_
plumbing_rewrite` WITHOUT a `payload`, so `_bt_serve_find_census` declines
immediately (no `tool_input` to rewrite) and those calls keep the exact
pre-existing generator-rewrite behavior -- this is why AC4/the sibling
file's byte-for-byte-unchanged claim still holds.

Spec backlink: coordinator_core/bash_guards/guard_head_tail_rewrite.py
"""

from __future__ import annotations

from coordinator_core.bash_guards import guard_head_tail_rewrite as guard
from coordinator_core.bash_guards import _dialect
from coordinator_core.search import census as _census


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

    def test_longer_chain_silent_not_advised_or_rewritten(self):
        # No rewrite offered, AND no advisory: this branch has already
        # computed that no rewrite would help, so the guard stays silent
        # rather than nag (see the guard module's own comment at this
        # branch, backlinking the fleet-wide fire-volume memo).
        assert _ps_rewrite("ls . | Sort-Object | Select-Object -First 5") is None

    def test_unrecognized_upstream_generator_silent_not_advised(self):
        # Same silencing as the longer-chain case above -- no rewrite
        # possible, so no advisory either.
        assert _ps_rewrite("Get-Process | Select-Object -First 5") is None

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


def _payload(cmd, cwd):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd, "description": "list files"},
        "cwd": str(cwd),
    }


class TestFindCensusServesInProcess:
    """C1: on a `payload` carrying a real `tool_input`, a `find`-census
    shape `coordinator_core.search.census` certifies (AC13/AC14) is SERVED
    in-process (AC3) instead of rewritten into a `python3 -c` one-liner."""

    def _fixture(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\n")
        (tmp_path / "b.txt").write_text("two\n")
        (tmp_path / "c.log").write_text("three\n")
        return tmp_path

    def test_served_answer_replaces_command_with_true(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "find %s -type f -name '*.txt' | head -n 5" % census_dir
        out = guard.check_head_tail_plumbing_rewrite(cmd, payload=_payload(cmd, tmp_path))
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["updatedInput"]["command"] == "true"
        assert hso["updatedInput"]["description"] == "list files"
        assert "python3" not in hso.get("additionalContext", "")
        assert "Answered in-process" in hso["additionalContext"]

    def test_served_answer_matches_real_find_equivalence(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "find %s -type f -name '*.txt' | head -n 5" % census_dir
        out = guard.check_head_tail_plumbing_rewrite(cmd, payload=_payload(cmd, tmp_path))
        hso = out["hookSpecificOutput"]
        context = hso["additionalContext"]
        rendered = context.split("\n\n", 1)[1]
        expected = sorted(
            [
                str(census_dir / "a.txt").replace("\\", "/"),
                str(census_dir / "b.txt").replace("\\", "/"),
            ]
        )
        assert rendered.splitlines() == expected

    def test_no_payload_keeps_old_generator_rewrite(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "find %s -type f -name '*.txt' | head -n 5" % census_dir
        out = guard.check_head_tail_plumbing_rewrite(cmd)
        assert out is not None
        assert "python3" in out["hookSpecificOutput"]["updatedInput"]["command"] or (
            "python" in out["hookSpecificOutput"]["updatedInput"]["command"]
        )

    def test_shape_census_declines_falls_back_to_generator_rewrite(self, tmp_path):
        # `-type d` is a certified-declined shape (module docstring's own
        # Negative-spec) -- census.py raises Unanswerable, so this must fall
        # back to the existing generator rewrite, not crash or return None.
        census_dir = self._fixture(tmp_path)
        cmd = "find %s -type d | head -n 5" % census_dir
        out = guard.check_head_tail_plumbing_rewrite(cmd, payload=_payload(cmd, tmp_path))
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["updatedInput"]["command"] != "true"

    def test_walk_budget_exceeded_falls_back_not_crashes(self, tmp_path, monkeypatch):
        census_dir = self._fixture(tmp_path)
        monkeypatch.setattr(_census, "WALK_BUDGET_ENTRIES", 0)
        cmd = "find %s -type f | head -n 5" % census_dir
        out = guard.check_head_tail_plumbing_rewrite(cmd, payload=_payload(cmd, tmp_path))
        assert out is not None
        assert out["hookSpecificOutput"]["updatedInput"]["command"] != "true"

    def test_powershell_leg_also_serves(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "ls %s | Select-Object -First 5" % census_dir
        out = guard.check_head_tail_plumbing_rewrite(
            cmd, dialect=_dialect.Dialect.POWERSHELL, payload=_payload(cmd, tmp_path)
        )
        # `ls` upstream stays the existing generator (census.py answers
        # `find` only) -- this asserts the PowerShell leg still rewrites
        # (unaffected), a regression guard for the shared `_bt_serve_find_
        # census` call site added at this branch.
        assert out is not None
        assert "updatedInput" in out["hookSpecificOutput"]

        find_cmd = "find %s -type f -name '*.txt' | Select-Object -First 5" % census_dir
        out2 = guard.check_head_tail_plumbing_rewrite(
            find_cmd,
            dialect=_dialect.Dialect.POWERSHELL,
            payload=_payload(find_cmd, tmp_path),
        )
        assert out2 is not None
        assert out2["hookSpecificOutput"]["updatedInput"]["command"] == "true"
