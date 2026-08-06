"""Behavioral tests for coordinator_core.write_guards.guard_memory_store_cap.

Covers each of AC14's four limits on its deny side and its passing side, an
unrelated write passing untouched, and the shrink-toward-compliance
carve-out (the single most important passing case -- denying a shrinking
edit on an over-cap file would trap that file permanently).
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import guard_memory_store_cap as guard


def _write_payload(file_path: str, content: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}


def _edit_payload(file_path: str, old: str, new: str, replace_all: bool = False) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": file_path,
            "old_string": old,
            "new_string": new,
            "replace_all": replace_all,
        },
    }


@pytest.fixture
def memory_dir(monkeypatch, tmp_path):
    home = tmp_path / "home"
    mem = home / ".claude" / "projects" / "-Some-project" / "memory"
    mem.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    return mem


def _rows(n: int, prefix: str = "x") -> str:
    return "\n".join(f"- [{prefix}{i}](slug{i}.md) short" for i in range(n))


class TestUnrelatedPathPassesUntouched:
    def test_unrelated_write_allowed(self, tmp_path):
        target = str(tmp_path / "some" / "other" / "file.py")
        result = guard.check(_write_payload(target, "print(1)\n"))
        assert result is None

    def test_config_write_under_home_claude_is_allowed(self, memory_dir):
        home_claude = memory_dir.parent.parent.parent
        target = str(home_claude / "settings.json")
        result = guard.check(_write_payload(target, "{}"))
        assert result is None

    def test_non_intercepted_tool_allowed(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        result = guard.check({"tool_name": "Read", "tool_input": {"file_path": target}})
        assert result is None


class TestBytesLimit:
    def test_over_2000_bytes_denied(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + ("x" * 2100)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert "cap" in result["hookSpecificOutput"]["additionalContext"]

    def test_at_or_under_2000_bytes_passes(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + _rows(5)
        assert len(content.encode("utf-8")) <= guard.MAX_MEMORY_MD_BYTES
        result = guard.check(_write_payload(target, content))
        assert result is None


class TestRowCountLimit:
    def test_21_rows_denied(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + _rows(21)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert "rows" in result["hookSpecificOutput"]["additionalContext"]

    def test_20_rows_passes(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + _rows(20)
        result = guard.check(_write_payload(target, content))
        assert result is None


class TestRowLengthLimit:
    def test_row_over_100_chars_denied(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        long_row = "- [" + ("a" * 90) + "](x.md) padding padding"
        assert len(long_row) > guard.MAX_ROW_CHARS
        content = "# Memory Index\n\n" + long_row
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert "chars" in result["hookSpecificOutput"]["additionalContext"]

    def test_row_at_100_chars_passes(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        row = "- " + ("a" * 98)
        assert len(row) == guard.MAX_ROW_CHARS
        content = "# Memory Index\n\n" + row
        result = guard.check(_write_payload(target, content))
        assert result is None


class TestBodyFileLimit:
    def test_body_file_over_1500_bytes_denied(self, memory_dir):
        target = str(memory_dir / "some-lesson.md")
        content = "x" * 1600
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert "1500" in result["hookSpecificOutput"]["additionalContext"]

    def test_body_file_at_1500_bytes_passes(self, memory_dir):
        target = str(memory_dir / "some-lesson.md")
        content = "x" * 1500
        result = guard.check(_write_payload(target, content))
        assert result is None


class TestShrinkTowardComplianceCarveOut:
    def test_shrinking_edit_on_over_cap_file_permitted(self, memory_dir):
        target = memory_dir / "MEMORY.md"
        over_cap_content = "# Memory Index\n\n" + ("y" * 3000)
        target.write_text(over_cap_content, encoding="utf-8")
        assert len(over_cap_content.encode("utf-8")) > guard.MAX_MEMORY_MD_BYTES

        # Shrinks the file, but the result is STILL over cap.
        smaller_still_over_cap = "# Memory Index\n\n" + ("y" * 2500)
        assert len(smaller_still_over_cap.encode("utf-8")) > guard.MAX_MEMORY_MD_BYTES
        assert len(smaller_still_over_cap) < len(over_cap_content)

        result = guard.check(
            _edit_payload(str(target), old="y" * 3000, new="y" * 2500)
        )
        assert result is None

    def test_growing_edit_on_over_cap_file_still_denied(self, memory_dir):
        target = memory_dir / "MEMORY.md"
        over_cap_content = "# Memory Index\n\n" + ("y" * 2100)
        target.write_text(over_cap_content, encoding="utf-8")

        result = guard.check(
            _edit_payload(str(target), old="y" * 2100, new="y" * 2100 + "z" * 500)
        )
        assert result is not None
        # Review: coordinator:code-reviewer -- restore the negative assertion
        # (deleted rather than inverted during the DR-277 flip) so a
        # regression reintroducing "permissionDecision: deny" is caught.
        assert "permissionDecision" not in result["hookSpecificOutput"]
        assert "additionalContext" in result["hookSpecificOutput"]


class TestCaseVariedTargetDenied:
    """macOS/APFS is case-insensitive-but-case-preserving: a Write to
    Projects/-Some-project/Memory/MEMORY.md lands inside the same real
    guarded memory/ directory on disk. os.path.normcase is a no-op on
    POSIX, so this guard must casefold explicitly (mirrors
    block_home_dir_memo_delivery's own case test).
    (Review: code-reviewer -- case bypass in this guard, 2026-07-31.)
    """

    def test_case_varied_memory_md_denied(self, memory_dir):
        home = memory_dir.parent.parent.parent.parent
        target = str(
            home / ".Claude" / "Projects" / "-Some-project" / "Memory" / "MEMORY.md"
        )
        content = "# Memory Index\n\n" + ("x" * 2100)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        # Review: coordinator:code-reviewer -- restore the negative assertion
        # (deleted rather than inverted during the DR-277 flip) so a
        # regression reintroducing "permissionDecision: deny" is caught.
        assert "permissionDecision" not in result["hookSpecificOutput"]
        assert "additionalContext" in result["hookSpecificOutput"]

    def test_case_varied_body_file_denied(self, memory_dir):
        home = memory_dir.parent.parent.parent.parent
        target = str(
            home / ".Claude" / "Projects" / "-Some-project" / "Memory" / "some-lesson.md"
        )
        content = "x" * 1600
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]
        assert "additionalContext" in result["hookSpecificOutput"]


class TestDenyMessageNamesDisambiguatingSlug:
    """Review finding (coordinatorcode-reviewer-54284751, Finding 4, P2):
    the guard's scope is a wildcard per-project directory
    (``<home>/.claude/projects/<any-slug>/memory/**``), so multiple distinct
    project memory stores can each hold a same-named ``MEMORY.md``/body
    file. A deny message naming only the bare filename can't tell an
    operator working across several project memory stores WHICH one
    tripped. Pins the project-relative ``<slug>/memory/<filename>`` form
    the message must carry."""

    def test_memory_md_deny_names_project_slug(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + ("x" * 2100)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "-Some-project/memory/MEMORY.md" in reason

    def test_body_file_deny_names_project_slug(self, memory_dir):
        target = str(memory_dir / "some-lesson.md")
        content = "x" * 1600
        result = guard.check(_write_payload(target, content))
        assert result is not None
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "-Some-project/memory/some-lesson.md" in reason

    def test_same_filename_in_different_project_slugs_is_distinguished(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        mem_a = home / ".claude" / "projects" / "-project-a" / "memory"
        mem_b = home / ".claude" / "projects" / "-project-b" / "memory"
        mem_a.mkdir(parents=True)
        mem_b.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("CLAUDE_HOME", raising=False)

        content = "x" * 1600
        result_a = guard.check(_write_payload(str(mem_a / "some-lesson.md"), content))
        result_b = guard.check(_write_payload(str(mem_b / "some-lesson.md"), content))

        reason_a = result_a["hookSpecificOutput"]["additionalContext"]
        reason_b = result_b["hookSpecificOutput"]["additionalContext"]
        assert reason_a != reason_b
        assert "-project-a" in reason_a
        assert "-project-b" in reason_b


class TestExtendedLengthPrefixAsymmetry:
    """Finding 1 (review, coordinatorcode-reviewer-d278904f, 2026-08-03): the
    containment gate's resolved+casefolded comparison correctly detects a
    candidate whose `.resolve()` grows the `\\\\?\\` extended-length prefix
    while the guarded root's does not, but the relative-parts extraction
    immediately below it sliced the RAW (non-prefix-stripped) strings by
    length -- under this exact asymmetry the slice offset by the prefix's
    width, `len(parts) == 3` failed, and the guard silently treated a
    governed MEMORY.md write as unrelated (fail-open). Simulates the
    asymmetry the same way
    test_block_home_dir_memo_delivery.test_extended_length_prefix_asymmetry_still_denies
    does: the candidate's resolve grows the prefix, the root's does not.
    """

    def test_over_cap_write_still_denied_under_prefix_asymmetry(self, monkeypatch, memory_dir):
        from pathlib import Path

        target_path = memory_dir / "MEMORY.md"
        target = str(target_path)

        real_resolve = Path.resolve

        def fake_resolve(self, *a, **kw):
            result = real_resolve(self, *a, **kw)
            if str(self).endswith("MEMORY.md"):
                return Path("\\\\?\\" + str(result))
            return result

        monkeypatch.setattr(Path, "resolve", fake_resolve)

        content = "# Memory Index\n\n" + ("x" * 2100)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        # Review: coordinator:code-reviewer -- restore the negative assertion
        # (deleted rather than inverted during the DR-277 flip) so a
        # regression reintroducing "permissionDecision: deny" is caught.
        assert "permissionDecision" not in result["hookSpecificOutput"]
        assert "additionalContext" in result["hookSpecificOutput"]


class TestDenyMessageShape:
    def test_row_count_deny_names_eviction_and_routing_test(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + _rows(21)
        result = guard.check(_write_payload(target, content))
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "evict" in reason
        assert "stale fact" in reason
        assert "doctrine dup" in reason
        assert "oldest" in reason
        assert "auto-trim" in reason
