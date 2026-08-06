"""Behavioral tests for coordinator_core.write_guards.nudge_terminal_artifact_edit
-- the delivered-plan-forward-binding-instruction advisory guard.

Spec: commit 257448d7 (the incident), c7b2484a (the revert), 8a0bcafd (the
prose-level correction this guard mechanizes); and the 2026-08-04 composite
terminal signal + content discrimination extension (real incident:
docs/plans/2026-08-03-scope-guard-peer-claim-release.md, status: approved,
all 14 ACs **met**).

Covers, in priority order:
  1. All-ACs-met + status: approved -> fires. The real incident. Primary AC.
  2. The pre-existing terminal-status leg still fires -- not regressed.
  3. A genuinely live plan (non-terminal status, no/undischarged AC table)
     stays silent regardless of edit content.
  4. Terminal plan + correspondence-shaped edit -> silent (new behaviour --
     the guard used to nag here).
  5. Terminal plan + instruction-shaped edit -> fires, names a real, on-disk
     live alternative.
  6. Malformed/absent frontmatter, non-existent file, non-plan path -> silent,
     no exception.
  7. Internal error -> silent, never fails the write.
  Plus: never emits a permissionDecision; module contract
  (CLASS/PRIORITY/MATCHERS).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import nudge_terminal_artifact_edit as guard

_INSTRUCTION_TEXT = "Going forward, the next session must rework the retry loop before building C7."
_CORRESPONDENCE_TEXT = (
    "Correction 2026-08-04: this claim was later refuted by a cross-repo "
    "memo finding; see state/handoffs/2026-08-04-refuted-claim.md for the record."
)

_AC_TABLE_ALL_MET = """\
| AC | Description | Evidence |
| --- | --- | --- |
| AC1 | first thing works | **met** -- `3bdfbfb17`. |
| AC2 | second thing works | **met** -- `301e7492c`. |
"""

_AC_TABLE_NOT_ALL_MET = """\
| AC | Description | Evidence |
| --- | --- | --- |
| AC1 | first thing works | ☐ |
| AC2 | second thing works | ☐ |
"""


def _payload(tool_name, file_path, cwd=None, **extra):
    tool_input = {"file_path": file_path}
    tool_input.update(extra)
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def _advisory_text(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


def _write_doc(tmp_path: Path, rel_path: str, status: str | None, body: str = "content") -> Path:
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if status is None:
        target.write_text(body, encoding="utf-8")
    else:
        target.write_text(
            f"---\nstatus: {status}\n---\n\n{body}\n",
            encoding="utf-8",
        )
    return target


# --------------------------------------------------------------------------------------
# 1. AC1-equivalent: the real incident -- status: approved + all ACs met -> fires.
# --------------------------------------------------------------------------------------


class TestAllAcsMetFires:
    def test_all_acs_met_status_approved_plus_instruction_fires(self, tmp_path):
        rel = "docs/plans/2026-08-03-scope-guard-peer-claim-release.md"
        _write_doc(tmp_path, rel, "approved", body=_AC_TABLE_ALL_MET)
        result = guard.check(
            _payload("Edit", rel, cwd=str(tmp_path), old_string="x", new_string=_INSTRUCTION_TEXT)
        )
        text = _advisory_text(result)
        assert rel in text
        assert "met" in text.lower()

    def test_all_acs_met_non_terminal_status_still_fires(self, tmp_path):
        """The AC-table leg is sufficient on its OWN -- status need not be terminal."""
        rel = "docs/plans/2026-08-04-draft-but-all-acs-met.md"
        _write_doc(tmp_path, rel, "draft", body=_AC_TABLE_ALL_MET)
        result = guard.check(
            _payload("Edit", rel, cwd=str(tmp_path), old_string="x", new_string=_INSTRUCTION_TEXT)
        )
        _advisory_text(result)


# --------------------------------------------------------------------------------------
# 2. Pre-existing terminal-status leg -- must not regress.
# --------------------------------------------------------------------------------------


class TestFiresOnTerminalStatus:
    @pytest.mark.parametrize("status", ["implemented", "shipped", "superseded"])
    def test_terminal_status_advises(self, tmp_path, status):
        _write_doc(tmp_path, "docs/plans/2026-07-01-example.md", status, body=_INSTRUCTION_TEXT)
        result = guard.check(
            _payload(
                "Edit",
                "docs/plans/2026-07-01-example.md",
                cwd=str(tmp_path),
                old_string="x",
                new_string=_INSTRUCTION_TEXT,
            )
        )
        text = _advisory_text(result)
        assert "docs/plans/2026-07-01-example.md" in text
        assert status in text
        assert "state/handoffs/" in text or "state/sizings/" in text

    def test_fires_on_problems_dir(self, tmp_path):
        _write_doc(tmp_path, "docs/problems/2026-07-01-issue.md", "implemented")
        result = guard.check(
            _payload("Write", "docs/problems/2026-07-01-issue.md", cwd=str(tmp_path))
        )
        # Write has no delta -- falls back to whole-body classification; the
        # fixture body ("content") carries no instruction tell, so this is
        # silent unless we give it instruction-shaped content.
        assert result is None
        _write_doc(
            tmp_path, "docs/problems/2026-07-01-issue.md", "implemented", body=_INSTRUCTION_TEXT
        )
        result = guard.check(
            _payload("Write", "docs/problems/2026-07-01-issue.md", cwd=str(tmp_path))
        )
        _advisory_text(result)

    def test_fires_on_research_dir(self, tmp_path):
        _write_doc(
            tmp_path, "docs/research/2026-07-01-spike.md", "shipped", body=_INSTRUCTION_TEXT
        )
        result = guard.check(
            _payload("MultiEdit", "docs/research/2026-07-01-spike.md", cwd=str(tmp_path))
        )
        _advisory_text(result)

    def test_notebook_edit_uses_notebook_path(self, tmp_path):
        _write_doc(tmp_path, "docs/plans/2026-07-01-nb.md", "implemented", body=_INSTRUCTION_TEXT)
        result = guard.check(
            {
                "tool_name": "NotebookEdit",
                "tool_input": {"notebook_path": "docs/plans/2026-07-01-nb.md"},
                "cwd": str(tmp_path),
            }
        )
        _advisory_text(result)

    def test_backslash_path_still_matched(self, tmp_path):
        _write_doc(
            tmp_path, "docs/plans/2026-07-01-example.md", "implemented", body=_INSTRUCTION_TEXT
        )
        result = guard.check(
            _payload("Edit", "docs\\plans\\2026-07-01-example.md", cwd=str(tmp_path))
        )
        _advisory_text(result)


# --------------------------------------------------------------------------------------
# 3. Genuinely live plan -> silent regardless of content.
# --------------------------------------------------------------------------------------


class TestSilentOnLivePlan:
    @pytest.mark.parametrize(
        "status", ["draft", "executing", "ready_to_fire", "open"]
    )
    def test_live_status_no_ac_table_passes_through(self, tmp_path, status):
        _write_doc(
            tmp_path, "docs/plans/2026-07-01-example.md", status, body=_INSTRUCTION_TEXT
        )
        result = guard.check(
            _payload(
                "Edit",
                "docs/plans/2026-07-01-example.md",
                cwd=str(tmp_path),
                old_string="x",
                new_string=_INSTRUCTION_TEXT,
            )
        )
        assert result is None

    @pytest.mark.parametrize("status", ["approved", "ratified"])
    def test_non_terminal_status_undischarged_ac_table_passes_through(self, tmp_path, status):
        _write_doc(tmp_path, "docs/plans/2026-07-01-example.md", status, body=_AC_TABLE_NOT_ALL_MET)
        result = guard.check(
            _payload(
                "Edit",
                "docs/plans/2026-07-01-example.md",
                cwd=str(tmp_path),
                old_string="x",
                new_string=_INSTRUCTION_TEXT,
            )
        )
        assert result is None


# --------------------------------------------------------------------------------------
# 4. Terminal plan + correspondence-shaped edit -> silent (new behaviour).
# --------------------------------------------------------------------------------------


class TestSilentOnCorrespondence:
    def test_terminal_plan_with_correspondence_edit_is_silent(self, tmp_path):
        rel = "docs/plans/2026-08-03-scope-guard-peer-claim-release.md"
        _write_doc(tmp_path, rel, "approved", body=_AC_TABLE_ALL_MET)
        result = guard.check(
            _payload(
                "Edit", rel, cwd=str(tmp_path), old_string="x", new_string=_CORRESPONDENCE_TEXT
            )
        )
        assert result is None

    def test_terminal_status_no_content_change_is_silent(self, tmp_path):
        """A terminal plan with an edit that carries no instruction tell at all
        (e.g. no new_string supplied) does not fire -- content discrimination
        applies to every firing tool, not just Edit."""
        rel = "docs/plans/2026-07-01-example.md"
        _write_doc(tmp_path, rel, "implemented", body="plain correspondence, no tells")
        result = guard.check(_payload("Edit", rel, cwd=str(tmp_path)))
        assert result is None


# --------------------------------------------------------------------------------------
# 5. Terminal plan + instruction-shaped edit -> fires, names a real live alternative.
# --------------------------------------------------------------------------------------


class TestNamesRealLiveAlternative:
    def test_names_real_open_handoff(self, tmp_path):
        rel = "docs/plans/2026-08-03-scope-guard-peer-claim-release.md"
        _write_doc(tmp_path, rel, "approved", body=_AC_TABLE_ALL_MET)

        handoff_rel = "state/handoffs/2026-08-03-scope-guard-peer-claim-release.md"
        (tmp_path / "state/handoffs").mkdir(parents=True, exist_ok=True)
        (tmp_path / handoff_rel).write_text(
            "---\nstatus: open\n---\n\n# scope guard peer claim release follow-up\n",
            encoding="utf-8",
        )
        (tmp_path / "state/handoffs/2026-08-01-totally-unrelated-topic.md").write_text(
            "---\nstatus: open\n---\n\n# unrelated\n", encoding="utf-8"
        )

        result = guard.check(
            _payload("Edit", rel, cwd=str(tmp_path), old_string="x", new_string=_INSTRUCTION_TEXT)
        )
        text = _advisory_text(result)
        assert handoff_rel in text, f"expected the real open handoff path in the offer, got: {text}"

    def test_falls_back_to_generic_categories_when_no_candidate(self, tmp_path):
        rel = "docs/plans/2026-08-03-scope-guard-peer-claim-release.md"
        _write_doc(tmp_path, rel, "approved", body=_AC_TABLE_ALL_MET)
        result = guard.check(
            _payload("Edit", rel, cwd=str(tmp_path), old_string="x", new_string=_INSTRUCTION_TEXT)
        )
        text = _advisory_text(result)
        assert "state/handoffs/" in text
        assert "state/sizings/" in text


# --------------------------------------------------------------------------------------
# 6. Malformed/absent frontmatter, non-existent file, non-plan path -> silent.
# --------------------------------------------------------------------------------------


class TestSilentOnNonPlanPath:
    def test_non_plan_path_passes_through(self, tmp_path):
        _write_doc(tmp_path, "state/handoffs/foo.md", "implemented", body=_INSTRUCTION_TEXT)
        result = guard.check(
            _payload("Edit", "state/handoffs/foo.md", cwd=str(tmp_path))
        )
        assert result is None

    def test_non_write_tool_passes_through(self, tmp_path):
        _write_doc(tmp_path, "docs/plans/2026-07-01-example.md", "implemented")
        result = guard.check(
            _payload("Read", "docs/plans/2026-07-01-example.md", cwd=str(tmp_path))
        )
        assert result is None

    def test_empty_file_path_passes_through(self, tmp_path):
        assert guard.check(_payload("Write", "", cwd=str(tmp_path))) is None

    def test_tool_input_not_dict_passes_through(self):
        assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None


class TestSilentOnMissingOrNoFrontmatter:
    def test_new_file_not_yet_on_disk_passes_through(self, tmp_path):
        result = guard.check(
            _payload("Write", "docs/plans/2026-07-01-brand-new.md", cwd=str(tmp_path))
        )
        assert result is None

    def test_no_frontmatter_passes_through(self, tmp_path):
        _write_doc(tmp_path, "docs/plans/2026-07-01-example.md", status=None, body="just prose")
        result = guard.check(
            _payload("Edit", "docs/plans/2026-07-01-example.md", cwd=str(tmp_path))
        )
        assert result is None

    def test_frontmatter_no_status_no_ac_table_passes_through(self, tmp_path):
        target = tmp_path / "docs/plans/2026-07-01-example.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\ntitle: no status here\n---\n\nbody\n", encoding="utf-8")
        result = guard.check(
            _payload("Edit", "docs/plans/2026-07-01-example.md", cwd=str(tmp_path))
        )
        assert result is None


# --------------------------------------------------------------------------------------
# 7. Internal error -> silent, never raises.
# --------------------------------------------------------------------------------------


class TestNeverRaises:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"tool_name": "Edit"},
            {"tool_name": "Edit", "tool_input": None},
            {"tool_name": "Edit", "tool_input": {"file_path": "docs/plans/x.md"}, "cwd": 12345},
            {
                "tool_name": "MultiEdit",
                "tool_input": {"file_path": "docs/plans/x.md"},
                "cwd": "/does/not/exist",
            },
        ],
    )
    def test_internal_error_never_raises_and_stays_silent(self, payload):
        result = guard.check(payload)
        assert result is None


class TestNeverEmitsPermissionDecision:
    def test_terminal_status_envelope_has_no_permission_decision(self, tmp_path):
        _write_doc(
            tmp_path, "docs/plans/2026-07-01-example.md", "implemented", body=_INSTRUCTION_TEXT
        )
        result = guard.check(
            _payload(
                "Edit",
                "docs/plans/2026-07-01-example.md",
                cwd=str(tmp_path),
                old_string="x",
                new_string=_INSTRUCTION_TEXT,
            )
        )
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_module_never_returns_permission_decision_key(self, tmp_path):
        """AC-4: no code path in check() emits a permissionDecision key --
        sweep every terminal-status/path/tool combination the guard fires
        on and assert the key is absent from each returned envelope (a
        docstring-source grep is too strict, since the module's own
        docstring explains the advisory-only contract in prose)."""
        for status in ("implemented", "shipped", "superseded"):
            for subdir in ("plans", "problems", "research"):
                rel = f"docs/{subdir}/2026-07-01-{status}.md"
                _write_doc(tmp_path, rel, status, body=_INSTRUCTION_TEXT)
                for tool_name in ("Write", "Edit", "MultiEdit"):
                    result = guard.check(
                        _payload(
                            tool_name,
                            rel,
                            cwd=str(tmp_path),
                            old_string="x",
                            new_string=_INSTRUCTION_TEXT,
                        )
                    )
                    assert result is not None
                    assert "permissionDecision" not in result["hookSpecificOutput"]


class TestModuleContract:
    def test_class_is_advisory(self):
        assert guard.CLASS == "advisory"

    def test_priority_and_matchers(self):
        assert guard.PRIORITY == 150
        assert guard.MATCHERS == ["Write", "Edit", "MultiEdit", "NotebookEdit"]
