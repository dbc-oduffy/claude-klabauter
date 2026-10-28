
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
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return mem


_LEVEL_ENV = "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL_GUARD-MEMORY-STORE-CAP"


@pytest.fixture(autouse=True)
def _warn_level_and_isolated_lessons(monkeypatch, tmp_path):
    """Guard level pinned to warn; the lessons root is pinned so no test can write into the real repo."""
    monkeypatch.setenv(_LEVEL_ENV, "warn")
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path / "lessons-root"))
    (tmp_path / "lessons-root").mkdir()


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
    def test_over_1000_bytes_denied(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + ("x" * 2100)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert "cap" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_at_or_under_1000_bytes_passes(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + _rows(5)
        assert len(content.encode("utf-8")) <= guard.MAX_MEMORY_MD_BYTES
        result = guard.check(_write_payload(target, content))
        assert result is None


class TestRowCountLimit:
    def test_6_rows_denied(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + _rows(6)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert "rows" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_5_rows_passes(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + _rows(5)
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
        assert "chars" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_row_at_100_chars_passes(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        row = "- " + ("a" * 98)
        assert len(row) == guard.MAX_ROW_CHARS
        content = "# Memory Index\n\n" + row
        result = guard.check(_write_payload(target, content))
        assert result is None


class TestBodyFileLimit:
    def test_body_file_over_800_bytes_denied(self, memory_dir):
        target = str(memory_dir / "some-lesson.md")
        content = "x" * 900
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert "800" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_body_file_at_800_bytes_passes(self, memory_dir):
        target = str(memory_dir / "some-lesson.md")
        content = "x" * 800
        result = guard.check(_write_payload(target, content))
        assert result is None


class TestShrinkTowardComplianceCarveOut:
    def test_shrinking_edit_on_over_cap_file_permitted(self, memory_dir):
        target = memory_dir / "MEMORY.md"
        over_cap_content = "# Memory Index\n\n" + ("y" * 3000)
        target.write_text(over_cap_content, encoding="utf-8")
        assert len(over_cap_content.encode("utf-8")) > guard.MAX_MEMORY_MD_BYTES

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
        # DR-345 flips this guard hard-deny; the previous negative assertion
        # here pinned the ADVISORY shape on purpose (see the DR-277-flip
        # comment this replaces). Per spec
        # state/tasks/2026-08-21-memory-cap-hard-deny-and-count-cap.md, a
        # deny is now the correct, intended result.
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "permissionDecisionReason" in result["hookSpecificOutput"]


class TestCaseVariedTargetDenied:

    def test_case_varied_memory_md_denied(self, memory_dir):
        home = memory_dir.parent.parent.parent.parent
        target = str(
            home / ".Claude" / "Projects" / "-Some-project" / "Memory" / "MEMORY.md"
        )
        content = "# Memory Index\n\n" + ("x" * 2100)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "permissionDecisionReason" in result["hookSpecificOutput"]

    def test_case_varied_body_file_denied(self, memory_dir):
        home = memory_dir.parent.parent.parent.parent
        target = str(
            home / ".Claude" / "Projects" / "-Some-project" / "Memory" / "some-lesson.md"
        )
        content = "x" * 900
        result = guard.check(_write_payload(target, content))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "permissionDecisionReason" in result["hookSpecificOutput"]


class TestDenyMessageNamesDisambiguatingSlug:

    def test_memory_md_deny_names_project_slug(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + ("x" * 2100)
        result = guard.check(_write_payload(target, content))
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "-Some-project/memory/MEMORY.md" in reason

    def test_body_file_deny_names_project_slug(self, memory_dir):
        target = str(memory_dir / "some-lesson.md")
        content = "x" * 900
        result = guard.check(_write_payload(target, content))
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
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

        content = "x" * 900
        result_a = guard.check(_write_payload(str(mem_a / "some-lesson.md"), content))
        result_b = guard.check(_write_payload(str(mem_b / "some-lesson.md"), content))

        reason_a = result_a["hookSpecificOutput"]["permissionDecisionReason"]
        reason_b = result_b["hookSpecificOutput"]["permissionDecisionReason"]
        assert reason_a != reason_b
        assert "-project-a" in reason_a
        assert "-project-b" in reason_b


class TestExtendedLengthPrefixAsymmetry:

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
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "permissionDecisionReason" in result["hookSpecificOutput"]


class TestDenyMessageShape:
    def test_row_count_deny_names_eviction(self, memory_dir):
        target = str(memory_dir / "MEMORY.md")
        content = "# Memory Index\n\n" + _rows(6)
        result = guard.check(_write_payload(target, content))
        assert "vict" in result["hookSpecificOutput"]["permissionDecisionReason"]
        reason = guard._deny_reason_rows("d", content.splitlines()[2:])
        assert "evict" in reason.lower()
        assert "state/lessons/" not in reason

    def test_file_count_failed_eviction_deny_names_the_oldest_and_one_instruction(self):
        reason = guard._deny_reason_file_count("slug/memory/", 5, "mem-00.md")
        assert "mem-00.md" in reason
        assert reason.count("then retry") == 1
        assert "state/lessons/" not in reason
        assert len(reason.encode("utf-8")) <= 220, "220-byte guard prose cap"


class TestFileCountCap:
    """DR-345's fifth limit -- MAX_MEMORY_FILES = 20, fired ONLY on creation
    of a NEW body file. An edit/rewrite of an existing body file must
    always pass, or a full store becomes permanently uneditable (spec
    state/tasks/2026-08-21-memory-cap-hard-deny-and-count-cap.md)."""

    def _seed_body_files(self, memory_dir, n: int) -> None:
        import os
        import time

        now = time.time()
        for i in range(n):
            p = memory_dir / f"body-{i}.md"
            p.write_text("x", encoding="utf-8")
            stamp = now - (n - i) * 10
            os.utime(p, (stamp, stamp))

    def test_at_cap_plus_new_filename_allowed_and_oldest_evicted(self, memory_dir):
        self._seed_body_files(memory_dir, guard.MAX_MEMORY_FILES)
        target = str(memory_dir / "new-one.md")
        assert guard.check(_write_payload(target, "y")) is None
        assert not (memory_dir / "body-0.md").exists()
        assert (memory_dir / "body-1.md").exists()

    def test_bash_verdict_evicts_and_returns_none(self, memory_dir):
        self._seed_body_files(memory_dir, guard.MAX_MEMORY_FILES)
        assert guard.memory_cap_verdict(memory_dir, "new-one.md") is None
        assert not (memory_dir / "body-0.md").exists()
        log = memory_dir.parent / "memory-evictions.jsonl"
        assert log.read_text(encoding="utf-8").count("\n") == 1

    def test_eviction_oserror_is_a_deny_naming_the_oldest(self, memory_dir, monkeypatch):
        self._seed_body_files(memory_dir, guard.MAX_MEMORY_FILES)

        def boom(_d):
            raise OSError("locked")

        monkeypatch.setattr(guard, "evict_oldest", boom)
        reason = guard.memory_cap_verdict(memory_dir, "new-one.md")
        assert "body-0.md" in reason
        result = guard.check(_write_payload(str(memory_dir / "new-one.md"), "y"))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_20_existing_plus_existing_filename_allowed(self, memory_dir):
        self._seed_body_files(memory_dir, guard.MAX_MEMORY_FILES)
        target = str(memory_dir / "body-0.md")
        result = guard.check(_write_payload(target, "y" * 5))
        assert result is None

    def test_19_existing_plus_new_filename_allowed(self, memory_dir):
        self._seed_body_files(memory_dir, guard.MAX_MEMORY_FILES - 1)
        target = str(memory_dir / "new-one.md")
        result = guard.check(_write_payload(target, "y"))
        assert result is None

    def test_memory_md_never_counted_as_body_file(self, memory_dir):
        (memory_dir / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
        self._seed_body_files(memory_dir, guard.MAX_MEMORY_FILES - 1)
        target = str(memory_dir / "new-one.md")
        result = guard.check(_write_payload(target, "y"))
        assert result is None


class TestRowLengthDenyNamesWhatItCounted:

    def test_the_deny_says_the_count_covers_pre_existing_rows(self):
        reason = guard._deny_reason_row_length("repo", ["- " + "x" * 120])

        assert "pre-existing ones included" in reason
        assert "before any write lands" in reason

    def test_the_deny_quotes_a_row_to_start_from(self):
        row = "- [A memory with a very long title indeed](f.md) — " + "y" * 90
        reason = guard._deny_reason_row_length("repo", [row])

        assert "- [A memory with a very long" in reason
        assert row not in reason, "the whole row would blow the prose budget"

    def test_the_deny_stays_inside_the_prose_budget(self):
        rows = ["- [" + "z" * 200 + "](f.md)"] * 9

        assert len(guard._deny_reason_row_length("claude-klabauter", rows).encode()) <= 220

    def test_a_short_row_is_quoted_whole(self):
        row = "- [Short](f.md) — hook"

        assert row in guard._deny_reason_row_length("repo", [row])


class TestByteDenyNamesWhereTheBytesAre:
    """An index that grew in its PREAMBLE must not be told to delete a memory.

    Measured 2026-09-11 on example-store-repo: an operator corrected the index header,
    pushed the file over the byte cap by doing so, and read "evict a row
    yourself (stale fact, then doctrine dup, then oldest)". Followed literally
    that trades a fact for documentation about the cap. They rewrote the header
    instead — the right move, and not the one the message asked for.
    """

    def test_a_preamble_heavy_file_is_told_to_trim_prose_first(self):
        rows = ["- [A](a.md) — hook"] * 9

        reason = guard._deny_reason_bytes("example-store-repo", 2149, rows)

        assert "NOT rows" in reason
        assert reason.index("Trim the prose") < reason.index("evict a row")

    def test_a_row_heavy_file_still_gets_the_eviction_ladder_alone(self):
        rows = ["- " + "x" * 90] * 20

        reason = guard._deny_reason_bytes("repo", 2100, rows)

        assert "NOT rows" not in reason
        assert "Evict a row yourself" in reason

    def test_both_shapes_state_the_ladder(self):
        for rows, size in ((["- [A](a.md) — hook"] * 9, 2149), (["- " + "x" * 90] * 20, 2100)):
            reason = guard._deny_reason_bytes("repo", size, rows)
            assert "stale fact, then doctrine dup, then oldest" in reason
            assert len(reason.encode()) <= 220


class TestClaudeHomeConventionA:
    @staticmethod
    def _isolate(monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("CLAUDE_HOME", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        return home

    @staticmethod
    def _big(root):
        mem = root / "projects" / "-p" / "memory"
        mem.mkdir(parents=True)
        return guard.check(_write_payload(str(mem / "MEMORY.md"), "x" * 2500))

    def test_claude_home_governs_dot_claude_projects(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        parent = tmp_path / "ch"
        parent.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(parent))
        assert self._big(parent / ".claude") is not None

    def test_claude_home_direct_projects_not_governed(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        parent = tmp_path / "ch"
        parent.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(parent))
        assert self._big(parent) is None

    def test_claude_config_dir_governs_projects(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        assert self._big(cfg) is not None

    @pytest.mark.parametrize("bad", ["relative/dir", "SUFFIX"])
    def test_bad_claude_home_does_not_raise_and_keeps_home_roots(self, monkeypatch, tmp_path, bad):
        home = self._isolate(monkeypatch, tmp_path)
        value = str(tmp_path / "x" / ".claude") if bad == "SUFFIX" else bad
        monkeypatch.setenv("CLAUDE_HOME", value)
        roots = guard._guarded_project_roots()
        assert home / ".claude" / "projects" in roots
        assert self._big(home / ".claude") is not None


class TestOverCapStoreCanShrink:
    def test_deleting_down_to_a_smaller_index_passes_while_over_cap(self, memory_dir):
        target = memory_dir / "MEMORY.md"
        target.write_text("# Memory Index\n\n" + _rows(9), encoding="utf-8")
        smaller = "# Memory Index\n\n" + _rows(8)
        assert guard.check(_write_payload(str(target), smaller)) is None

    def test_growing_an_over_cap_index_is_still_denied(self, memory_dir):
        target = memory_dir / "MEMORY.md"
        target.write_text("# Memory Index\n\n" + _rows(8), encoding="utf-8")
        result = guard.check(_write_payload(str(target), "# Memory Index\n\n" + _rows(9)))
        assert result is not None


class TestOverCapNonGrowingEditLandsWithAdvisory:
    def test_same_size_edit_of_overcap_body_lands_with_advisory(self, memory_dir):
        target = memory_dir / "fat.md"
        target.write_text("y" * 1000, encoding="utf-8")
        result = guard.check(_edit_payload(str(target), old="y" * 10, new="z" * 10))
        out = result["hookSpecificOutput"]
        assert "permissionDecision" not in out
        ctx = out["additionalContext"]
        assert "1000B" in ctx and "800B" in ctx and "200B over" in ctx

    def test_same_size_edit_of_overcap_index_lands_with_advisory(self, memory_dir):
        target = memory_dir / "MEMORY.md"
        target.write_bytes(("# Memory Index\n\n" + _rows(8)).encode("utf-8"))
        size = target.stat().st_size
        result = guard.check(_edit_payload(str(target), old="short", new="SHORT", replace_all=True))
        out = result["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert f"{size}B" in out["additionalContext"] and "1000B" in out["additionalContext"]

    def test_growing_overcap_body_is_a_plain_shorten_deny(self, memory_dir, tmp_path):
        target = memory_dir / "fat.md"
        target.write_text("y" * 1000, encoding="utf-8")
        result = guard.check(_edit_payload(str(target), old="y" * 10, new="y" * 11))
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "Shorten it" in out["permissionDecisionReason"]
        assert list((tmp_path / "lessons-root").rglob("*")) == []


def _edit_index_payload(index, old: str, new: str) -> dict:
    return _edit_payload(str(index), old=old, new=new)


class TestMemoryMdBreachEvictsOldest:
    """An Edit/MultiEdit of MEMORY.md over the row or byte cap evicts oldest until it fits."""

    @staticmethod
    def _seed(memory_dir, n: int = 5):
        import os

        rows = []
        for i in range(n):
            p = memory_dir / f"b{i}.md"
            p.write_text(f"---\ntype: user\n---\nbody {i}\n", encoding="utf-8")
            os.utime(p, (1000 + i, 1000 + i))
            rows.append(f"- [b{i}](b{i}.md) note")
        index = memory_dir / "MEMORY.md"
        index.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return index

    def test_edit_adding_a_sixth_row_evicts_oldest_and_lands(self, memory_dir):
        index = self._seed(memory_dir)
        result = guard.check(
            _edit_index_payload(index, "- [b4](b4.md) note\n", "- [b4](b4.md) note\n- [n](n.md) new\n")
        )
        assert result is None
        assert not (memory_dir / "b0.md").exists()
        assert "b0.md" not in index.read_text(encoding="utf-8")
        assert (memory_dir.parent / "memory-evictions.jsonl").exists()

    def test_multiedit_over_byte_cap_evicts_until_it_fits(self, memory_dir):
        index = self._seed(memory_dir)
        text = index.read_text(encoding="utf-8").replace(" note", " " + "n" * 75)
        index.write_text(text, encoding="utf-8")
        payload = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(index),
                "edits": [{"old_string": "- [b4]", "new_string": "# " + "h" * 560 + "\n- [b4]"}],
            },
        }
        assert guard.check(payload) is None
        assert not (memory_dir / "b0.md").exists()
        assert (memory_dir / "b1.md").exists()
        assert len(index.read_bytes()) <= guard.MAX_MEMORY_MD_BYTES

    def test_write_of_memory_md_over_row_cap_stays_a_plain_deny(self, memory_dir):
        index = self._seed(memory_dir)
        result = guard.check(_write_payload(str(index), "# Memory Index\n\n" + _rows(6)))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert (memory_dir / "b0.md").exists()
        assert not (memory_dir.parent / "memory-evictions.jsonl").exists()

    def test_edit_producing_an_overlong_row_stays_a_plain_deny(self, memory_dir):
        index = self._seed(memory_dir)
        result = guard.check(
            _edit_index_payload(index, "- [b4](b4.md) note", "- [b4](b4.md) " + "q" * 120)
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "chars" in result["hookSpecificOutput"]["permissionDecisionReason"]
        assert (memory_dir / "b0.md").exists()
        assert not (memory_dir.parent / "memory-evictions.jsonl").exists()

    def test_edit_whose_old_string_sits_in_the_evicted_row_is_denied_and_evicts_nothing(
        self, memory_dir
    ):
        index = self._seed(memory_dir)
        result = guard.check(
            _edit_index_payload(
                index, "- [b0](b0.md) note\n", "- [b0](b0.md) note\n- [n](n.md) new\n- [m](m.md) m\n"
            )
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert (memory_dir / "b0.md").exists()
        assert "b0.md" in index.read_text(encoding="utf-8")
        assert not (memory_dir.parent / "memory-evictions.jsonl").exists()

    def test_edit_over_cap_with_nothing_to_evict_is_a_deny(self, memory_dir):
        index = memory_dir / "MEMORY.md"
        index.write_text(_rows(5), encoding="utf-8")
        result = guard.check(_edit_index_payload(index, "short", "short" + "s" * 300))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
