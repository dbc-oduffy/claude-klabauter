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

import shlex

from coordinator_core.bash_guards import guard_head_tail_rewrite as guard
from coordinator_core.bash_guards import _dialect
from coordinator_core.search import census as _census
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


def _ps_rewrite(cmd, session_id="sess1"):
    return _enveloped(guard.check_head_tail_plumbing_rewrite)(
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
        assert _ps_rewrite("ls . | Sort-Object | Select-Object -First 5") is None

    def test_unrecognized_upstream_generator_silent_not_advised(self):
        assert _ps_rewrite("Get-Process | Select-Object -First 5") is None

    def test_no_select_object_at_all_is_a_genuine_clean(self):
        assert _ps_rewrite("Get-Process") is None

    def test_empty_command_allows(self):
        assert _ps_rewrite("") is None

    def test_bash_leg_unchanged_when_dialect_omitted(self):
        out = _enveloped(guard.check_head_tail_plumbing_rewrite)("find . -type f | head -n 5")
        assert out is not None
        assert "updatedInput" in out["hookSpecificOutput"]


def _payload(cmd, cwd):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd, "description": "list files"},
        "cwd": str(cwd),
    }


class TestFindCensusServesInProcess:

    @staticmethod
    def _bash_root(path):
        # Bash de-escapes the backslashes of a native Windows path before the
        # guard sees it (`_command_tokenizer`), so the bash leg is fed the
        # forward-slash form a real bash session would pass.
        return shlex.quote(path.as_posix())

    def _fixture(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\n")
        (tmp_path / "b.txt").write_text("two\n")
        (tmp_path / "c.log").write_text("three\n")
        return tmp_path

    def test_served_answer_replaces_command_with_true(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "find %s -type f -name '*.txt' | head -n 5" % self._bash_root(census_dir)
        out = _enveloped(guard.check_head_tail_plumbing_rewrite)(cmd, payload=_payload(cmd, tmp_path))
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["updatedInput"]["command"] == "true"
        assert hso["updatedInput"]["description"] == "list files"
        assert "python3" not in hso.get("additionalContext", "")
        assert "Answered in-process" in hso["additionalContext"]

    def test_served_answer_matches_real_find_equivalence(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "find %s -type f -name '*.txt' | head -n 5" % self._bash_root(census_dir)
        out = _enveloped(guard.check_head_tail_plumbing_rewrite)(cmd, payload=_payload(cmd, tmp_path))
        hso = out["hookSpecificOutput"]
        context = hso["additionalContext"]
        rendered = context.split("\n\n", 1)[1]
        expected = sorted(
            [
                str(census_dir / "a.txt").replace("\\", "/"),
                str(census_dir / "b.txt").replace("\\", "/"),
            ]
        )
        assert rendered.splitlines()[: len(expected)] == expected
        assert hso["updatedInput"]["command"] == "true"

    def test_no_payload_keeps_old_generator_rewrite(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "find %s -type f -name '*.txt' | head -n 5" % self._bash_root(census_dir)
        out = _enveloped(guard.check_head_tail_plumbing_rewrite)(cmd)
        assert out is not None
        assert "python3" in out["hookSpecificOutput"]["updatedInput"]["command"] or (
            "python" in out["hookSpecificOutput"]["updatedInput"]["command"]
        )

    def test_shape_census_declines_falls_back_to_generator_rewrite(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "find %s -type d | head -n 5" % self._bash_root(census_dir)
        out = _enveloped(guard.check_head_tail_plumbing_rewrite)(cmd, payload=_payload(cmd, tmp_path))
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["updatedInput"]["command"] != "true"

    def test_walk_budget_exceeded_falls_back_not_crashes(self, tmp_path, monkeypatch):
        census_dir = self._fixture(tmp_path)
        monkeypatch.setattr(_census, "WALK_BUDGET_ENTRIES", 0)
        cmd = "find %s -type f | head -n 5" % self._bash_root(census_dir)
        out = _enveloped(guard.check_head_tail_plumbing_rewrite)(cmd, payload=_payload(cmd, tmp_path))
        assert out is not None
        assert out["hookSpecificOutput"]["updatedInput"]["command"] != "true"

    def test_powershell_leg_also_serves(self, tmp_path):
        census_dir = self._fixture(tmp_path)
        cmd = "ls %s | Select-Object -First 5" % census_dir
        out = _enveloped(guard.check_head_tail_plumbing_rewrite)(
            cmd, dialect=_dialect.Dialect.POWERSHELL, payload=_payload(cmd, tmp_path)
        )
        assert out is not None
        assert "updatedInput" in out["hookSpecificOutput"]

        find_cmd = "find %s -type f -name '*.txt' | Select-Object -First 5" % census_dir
        out2 = _enveloped(guard.check_head_tail_plumbing_rewrite)(
            find_cmd,
            dialect=_dialect.Dialect.POWERSHELL,
            payload=_payload(find_cmd, tmp_path),
        )
        assert out2 is not None
        assert out2["hookSpecificOutput"]["updatedInput"]["command"] == "true"


class TestGeneratorScriptEscapesAndFallsBack:
    def test_windows_path_and_backslash_regex_compile(self):
        win = "C:" + "\\Users\\x\\new"  # abs-path-ok: synthetic escape fixture
        for kind, parsed in (
            ("grep", {"flags": "", "pattern": r"foo\s+\d\U", "targets": [win]}),
            ("find", {"path": win, "name_pattern": r"*\t*", "only_files": True}),
            ("ls", {"path": win}),
        ):
            script = "\n".join(guard._bt_build_generator_lines(kind, parsed))
            compile(script, "<t>", "exec")

    def test_uncompilable_script_leaves_original_command(self, monkeypatch):
        monkeypatch.setattr(
            guard, "_bt_build_generator_lines", lambda kind, parsed: ['x = "C:\\Users"[']
        )
        assert _enveloped(guard.check_head_tail_plumbing_rewrite)("grep -r foo src | head -5") is None


def test_rewrite_and_advisory_are_records_carrying_a_subagent_route():
    from coordinator_core.guard_messages.alternative_record import (
        AlternativeRecord,
        Audience,
        EscalationRoute,
        Override,
    )
    from coordinator_core.guard_messages.render import render

    rewrite = guard.check_head_tail_plumbing_rewrite("grep -rn foo src | head -5")
    advisory = guard.check_head_tail_plumbing_rewrite("grep -rn foo src | head -c 5")
    for record, kind in ((rewrite, "rewrite"), (advisory, "advisory")):
        assert isinstance(record, AlternativeRecord) and record.verdict_kind == kind
        assert any(isinstance(m, Override) for m in record.members)
        assert any(isinstance(m, EscalationRoute) for m in record.members)
        assert "COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING" not in str(render(record, Audience.SUBAGENT))


class TestEmRenderKeepsLegacyText:
    """`em_text` makes the EM render the legacy envelope byte for byte; the
    subagent render drops the override pointer."""

    def test_advisory_em_render_is_the_legacy_text_with_the_override_pointer(self):
        from coordinator_core.guard_messages.alternative_record import Audience
        from coordinator_core.guard_messages.render import render

        record = guard.check_head_tail_plumbing_rewrite("ls | head -c 10")
        assert record.verdict_kind == "advisory"
        em = render(record, Audience.EM)["hookSpecificOutput"]["additionalContext"]
        assert em == record.em_text
        assert em.startswith("Advisory: '... | head' truncates output")
        assert "COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING" not in str(
            render(record, Audience.SUBAGENT)
        )

    def test_census_serve_is_stamped_and_carries_no_permission_decision(self, tmp_path):
        from coordinator_core.guard_messages.alternative_record import Audience
        from coordinator_core.guard_messages.render import render

        (tmp_path / "a.txt").write_text("x")
        cmd = "find %s -type f -name '*.txt' | head -n 5" % str(tmp_path).replace("\\", "/")
        record = guard.check_head_tail_plumbing_rewrite(cmd, payload=_payload(cmd, tmp_path))
        assert record.updated_input["command"] == "true"
        hso = render(record, Audience.EM)["hookSpecificOutput"]
        assert hso["additionalContext"].startswith("[coordinator] [Answered in-process")
        assert "permissionDecision" not in hso
