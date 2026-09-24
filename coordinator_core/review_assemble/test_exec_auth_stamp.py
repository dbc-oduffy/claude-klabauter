"""
coordinator_core.review_assemble.test_exec_auth_stamp — co-located pytest for
coordinator_core.review_assemble.exec_auth_stamp.stamp_execution_authorization.

Run: python -m pytest coordinator_core/review_assemble/test_exec_auth_stamp.py -q

Spec backlink: DoE-claude:pln-computed-skills-b8-review-ci-c-ffa5ad, chunk C6
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.review_assemble.exec_auth_stamp import (
    EXIT_BUSINESS_FAIL,
    EXIT_OK,
    EXIT_USAGE,
    main,
    restamp_execution_authorization,
    stamp_execution_authorization,
    stamp_invocation_authorization,
)
from coordinator_core.pickup_assemble.stamp_check import stamp_check

import pytest
import yaml
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _canonical_body_sha(repo: Path, text: str) -> str:
    fm_count = 0
    out_lines: list[str] = []
    for line in text.splitlines():
        if line.rstrip(" \t") == "---":
            fm_count += 1
            continue
        if fm_count >= 2:
            out_lines.append(line + "\n")
    body = "".join(out_lines)
    result = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=str(repo),
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=15,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.decode("utf-8").strip()


_PLAN_TEXT = """---
title: "test plan"
status: draft
---

# Test Plan

Some body content.
"""


def test_stamp_writes_all_four_fields(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    expected_sha = _canonical_body_sha(tmp_path, _PLAN_TEXT)

    exit_code, result = stamp_execution_authorization(
        str(plan_path),
        "PM",
        "make it so",
        at="2026-07-24",
        repo_root=tmp_path,
    )

    assert exit_code == EXIT_OK
    assert result["applied"] is True
    assert result["sha"] == expected_sha

    written = plan_path.read_text(encoding="utf-8")
    assert "execution_authorized_by: PM" in written
    assert "execution_authorized_at: '2026-07-24'" in written or "execution_authorized_at: 2026-07-24" in written
    assert f"execution_authorized_sha: {expected_sha}" in written
    assert 'execution_authorized_note: "make it so"' in written or "execution_authorized_note: make it so" in written
    # Body untouched.
    assert "Some body content." in written


def test_stamp_is_idempotent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    first_exit, first_result = stamp_execution_authorization(
        str(plan_path), "PM", "make it so", at="2026-07-24", repo_root=tmp_path
    )
    assert first_exit == EXIT_OK
    assert first_result["applied"] is True

    second_exit, second_result = stamp_execution_authorization(
        str(plan_path), "PM", "make it so", at="2026-07-24", repo_root=tmp_path
    )
    assert second_exit == EXIT_OK
    assert second_result["applied"] is False


def test_append_note_appends_rather_than_replaces(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    first_exit, first_result = stamp_execution_authorization(
        str(plan_path), "PM", "make it so", at="2026-07-24", repo_root=tmp_path
    )
    assert first_exit == EXIT_OK
    assert first_result["applied"] is True

    second_exit, second_result = stamp_execution_authorization(
        str(plan_path),
        "PM",
        "re-stamped after stale-bookkeeping amendment",
        at="2026-07-25",
        repo_root=tmp_path,
        append_note=True,
    )
    assert second_exit == EXIT_OK
    assert second_result["applied"] is True

    written = plan_path.read_text(encoding="utf-8")
    assert "make it so" in written
    assert "re-stamped after stale-bookkeeping amendment" in written
    # Original text is a prefix of the new note -- not clobbered.
    note_line = next(
        line for line in written.splitlines() if line.startswith("execution_authorized_note:")
    )
    assert note_line.index("make it so") < note_line.index("re-stamped after stale-bookkeeping amendment")


def test_append_note_repeat_is_idempotent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    stamp_execution_authorization(
        str(plan_path), "PM", "make it so", at="2026-07-24", repo_root=tmp_path
    )
    first_exit, first_result = stamp_execution_authorization(
        str(plan_path),
        "PM",
        "re-stamped after amendment",
        at="2026-07-25",
        repo_root=tmp_path,
        append_note=True,
    )
    assert first_exit == EXIT_OK
    assert first_result["applied"] is True
    written_once = plan_path.read_text(encoding="utf-8")

    # Re-running the identical append must be a genuine no-op -- it must NOT
    # grow the note a second time.
    second_exit, second_result = stamp_execution_authorization(
        str(plan_path),
        "PM",
        "re-stamped after amendment",
        at="2026-07-25",
        repo_root=tmp_path,
        append_note=True,
    )
    assert second_exit == EXIT_OK
    assert second_result["applied"] is False
    written_twice = plan_path.read_text(encoding="utf-8")
    assert written_twice == written_once


def test_append_note_onto_absent_note_is_a_plain_set(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    exit_code, result = stamp_execution_authorization(
        str(plan_path),
        "PM",
        "first reason",
        at="2026-07-24",
        repo_root=tmp_path,
        append_note=True,
    )
    assert exit_code == EXIT_OK
    assert result["applied"] is True

    written = plan_path.read_text(encoding="utf-8")
    note_line = next(
        line for line in written.splitlines() if line.startswith("execution_authorized_note:")
    )
    assert not note_line.startswith("execution_authorized_note: \\n")
    assert "first reason" in note_line


def test_cli_rejects_note_and_append_note_together(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    from coordinator_core.review_assemble.exec_auth_stamp import EXIT_USAGE, main

    exit_code = main(
        [
            "stamp",
            str(plan_path),
            "--by",
            "PM",
            "--note",
            "a",
            "--append-note",
            "b",
        ]
    )
    assert exit_code == EXIT_USAGE


def test_missing_plan_is_business_fail(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    exit_code, result = stamp_execution_authorization(
        str(tmp_path / "docs" / "plans" / "nope.md"),
        "PM",
        "make it so",
        repo_root=tmp_path,
    )
    assert exit_code == EXIT_BUSINESS_FAIL
    assert "not found" in result["error"]


def test_invocation_mint_is_fresh_per_pickup_stamp_check(tmp_path: Path) -> None:
    """Mints via `stamp_invocation_authorization` on an unstamped plan, then
    reads it back through `pickup_assemble.stamp_check.stamp_check` -- the
    independent READ-side verb that recomputes the plan-body hash via its
    own recipe. This test exists to fail loudly the moment the mint's hash
    recipe and `stamp_check`'s recipe ever diverge: both are documented as
    routing through the same canonical `git hash-object --stdin` recipe,
    but only a cross-module round-trip like this one actually proves it,
    rather than each module's own tests independently agreeing with
    themselves."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    utterance = "yes, go ahead and execute this plan"
    exit_code, result = stamp_invocation_authorization(
        str(plan_path),
        utterance,
        "/execute-plan",
        at="2026-07-24",
        repo_root=tmp_path,
    )
    assert exit_code == EXIT_OK
    assert result["applied"] is True

    check_exit, gate = stamp_check("docs/plans/2026-07-24-test-plan.md", repo_root=tmp_path)
    assert check_exit == EXIT_OK
    assert gate["verdict"] == "match"

    written = plan_path.read_text(encoding="utf-8")
    assert "execution_authorized_by: PM" in written
    assert utterance in written


def test_invocation_mint_converges_and_ignores_a_later_at_while_body_unchanged(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    utterance = "yes, go ahead and execute this plan"

    first_exit, first_result = stamp_invocation_authorization(
        str(plan_path), utterance, "/execute-plan", at="2026-07-24", repo_root=tmp_path
    )
    assert first_exit == EXIT_OK
    assert first_result["applied"] is True
    written_once = plan_path.read_text(encoding="utf-8")

    second_exit, second_result = stamp_invocation_authorization(
        str(plan_path), utterance, "/execute-plan", at="2026-07-24", repo_root=tmp_path
    )
    assert second_exit == EXIT_OK
    assert second_result["applied"] is False
    written_twice = plan_path.read_text(encoding="utf-8")
    assert written_twice == written_once

    # Date-boundary case: a different `at=` on an otherwise-identical
    # re-invocation, with the plan body unchanged, must still be a no-op --
    # the recorded `execution_authorized_at` is not allowed to drift merely
    # because a later call happened on a different day.
    third_exit, third_result = stamp_invocation_authorization(
        str(plan_path), utterance, "/execute-plan", at="2026-07-25", repo_root=tmp_path
    )
    assert third_exit == EXIT_OK
    assert third_result["applied"] is False
    written_thrice = plan_path.read_text(encoding="utf-8")
    assert written_thrice == written_once


def test_invocation_mint_stale_substantive_still_halts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_rel = "docs/plans/2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    exit_code, result = stamp_invocation_authorization(
        str(plan_path),
        "yes, go ahead and execute this plan",
        "/execute-plan",
        at="2026-07-24",
        repo_root=tmp_path,
    )
    assert exit_code == EXIT_OK
    assert result["applied"] is True

    _git(tmp_path, "add", plan_rel)
    _git(tmp_path, "commit", "-m", "mint execution authorization")

    stamped_text = plan_path.read_text(encoding="utf-8")
    plan_path.write_text(
        stamped_text + "\n## Acceptance criteria\n\n- a new criterion appeared\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", plan_rel)
    _git(tmp_path, "commit", "-m", "amend acceptance criteria")

    check_exit, gate = stamp_check(plan_rel, repo_root=tmp_path)
    assert check_exit == EXIT_OK
    assert gate["verdict"] == "stale-substantive"


def test_bare_invocation_with_no_words_still_mints(tmp_path: Path) -> None:
    """`utterance` is optional -- the typed command IS the authorization,
    and the words are evidence only when present. A `None` utterance must
    still mint, not refuse."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    exit_code, result = stamp_invocation_authorization(
        str(plan_path), None, "/execute-plan", at="2026-07-24", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    assert result["applied"] is True

    written = plan_path.read_text(encoding="utf-8")
    assert "execution_authorized_by: PM" in written
    assert "/execute-plan" in written


def test_bare_invocation_with_empty_string_utterance_still_mints(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    exit_code, result = stamp_invocation_authorization(
        str(plan_path), "", "/execute-plan", at="2026-07-24", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    assert result["applied"] is True

    written = plan_path.read_text(encoding="utf-8")
    assert "execution_authorized_by: PM" in written
    assert "/execute-plan" in written


def test_bare_invocation_mint_is_fresh_per_pickup_stamp_check(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    exit_code, result = stamp_invocation_authorization(
        str(plan_path), None, "/execute-plan", at="2026-07-24", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    assert result["applied"] is True

    check_exit, gate = stamp_check("docs/plans/2026-07-24-test-plan.md", repo_root=tmp_path)
    assert check_exit == EXIT_OK
    assert gate["verdict"] == "match"


def test_bare_invocation_mint_converges(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    first_exit, first_result = stamp_invocation_authorization(
        str(plan_path), None, "/execute-plan", at="2026-07-24", repo_root=tmp_path
    )
    assert first_exit == EXIT_OK
    assert first_result["applied"] is True
    written_once = plan_path.read_text(encoding="utf-8")

    second_exit, second_result = stamp_invocation_authorization(
        str(plan_path), None, "/execute-plan", at="2026-07-24", repo_root=tmp_path
    )
    assert second_exit == EXIT_OK
    assert second_result["applied"] is False
    written_twice = plan_path.read_text(encoding="utf-8")
    assert written_twice == written_once


def test_invocation_mint_refuses_utterance_with_embedded_newline(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    before = plan_path.read_text(encoding="utf-8")

    exit_code, _result = stamp_invocation_authorization(
        str(plan_path), "yes\nexecute it", "/execute-plan", repo_root=tmp_path
    )
    assert exit_code == EXIT_USAGE
    assert plan_path.read_text(encoding="utf-8") == before


def test_invocation_mint_refuses_non_slash_typed_command(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    before = plan_path.read_text(encoding="utf-8")

    exit_code, _result = stamp_invocation_authorization(
        str(plan_path), "yes, execute it", "execute-plan", repo_root=tmp_path
    )
    assert exit_code == EXIT_USAGE
    assert plan_path.read_text(encoding="utf-8") == before

    exit_code, _result = stamp_invocation_authorization(
        str(plan_path), "yes, execute it", "the EM decided", repo_root=tmp_path
    )
    assert exit_code == EXIT_USAGE
    assert plan_path.read_text(encoding="utf-8") == before


def test_invocation_mint_appends_to_a_pre_existing_review_note(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    pre_exit, pre_result = stamp_execution_authorization(
        str(plan_path), "PM", "earlier review note", at="2026-07-24", repo_root=tmp_path
    )
    assert pre_exit == EXIT_OK
    assert pre_result["applied"] is True

    utterance = "yes, go ahead and execute this plan"
    exit_code, result = stamp_invocation_authorization(
        str(plan_path), utterance, "/execute-plan", at="2026-07-24", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    assert result["applied"] is True

    written = plan_path.read_text(encoding="utf-8")
    note_line = next(
        line for line in written.splitlines() if line.startswith("execution_authorized_note:")
    )
    assert "earlier review note" in note_line
    assert utterance in note_line
    assert note_line.index("earlier review note") < note_line.index(utterance)


_BLOCK_SCALAR_NOTE_PLAN = """---
title: "test plan"
status: draft
execution_authorized_note: |
  PM: yes, go ahead and build it.
  Do the embedder A/B first.
---

# Test Plan

Some body content.
"""


def test_invocation_mint_appends_into_a_block_scalar_note(tmp_path: Path) -> None:
    """`authorize-invocation` is /execute-plan Phase 1 step 2 — the mint that
    records the PM's authorizing act. It used to die outright on any plan
    whose `execution_authorized_note` was a `|` or `>` block, because
    `stamp_execution_authorization` routed even an `append_note=True` call
    through `replace_fm_field`, whose block-scalar guard refuses. The refusal
    was total — no flag got past it, and its own advice ("Fix the frontmatter
    manually") is the unattributable hand-stamp the mint exists to prevent.
    Reported cross-repo by example-retrieval-repo-em, 2026-08-20.
    """
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-block-scalar-note.md"
    plan_path.write_text(_BLOCK_SCALAR_NOTE_PLAN, encoding="utf-8")

    exit_code, result = stamp_invocation_authorization(
        str(plan_path),
        typed_command="/execute-plan",
        utterance="go",
        at="2026-08-20",
        repo_root=tmp_path,
    )

    assert exit_code == EXIT_OK, result
    assert result["applied"] is True

    loaded = yaml.safe_load(plan_path.read_text(encoding="utf-8").split("---")[1])
    note = loaded["execution_authorized_note"]
    # The PM's verbatim words survive intact -- the corruption the guard was
    # protecting against does not happen, it simply is no longer the only
    # available outcome.
    assert "PM: yes, go ahead and build it." in note
    assert "Do the embedder A/B first." in note
    assert note.rstrip("\n").endswith(result["note"])
    assert loaded["execution_authorized_by"] == "PM"
    assert loaded["status"] == "draft"


def test_invocation_mint_converges_on_a_block_scalar_note(tmp_path: Path) -> None:
    """Convergence across re-invocation is the whole point of the mint, and
    it has a second failure mode on this shape: `read_fm_field_unquoted`
    returns only the `key:` LINE, so on a block scalar the convergence test
    compared the appended note against the bare `"|"` sigil and never
    matched. Without this, a fixed write path would append the same line on
    every pickup and grow the field without bound."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-block-scalar-converge.md"
    plan_path.write_text(_BLOCK_SCALAR_NOTE_PLAN, encoding="utf-8")

    kwargs = dict(
        typed_command="/execute-plan",
        utterance="go",
        at="2026-08-20",
        repo_root=tmp_path,
    )
    first_code, _ = stamp_invocation_authorization(str(plan_path), **kwargs)
    after_first = plan_path.read_text(encoding="utf-8")
    second_code, second = stamp_invocation_authorization(str(plan_path), **kwargs)

    assert first_code == EXIT_OK
    assert second_code == EXIT_OK
    assert second["applied"] is False
    assert plan_path.read_text(encoding="utf-8") == after_first


def test_block_scalar_note_stamp_is_stamp_check_clean(tmp_path: Path) -> None:
    """The mint's output has to satisfy the reader that gates pickup, not
    merely parse."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-block-scalar-stamp-check.md"
    plan_path.write_text(_BLOCK_SCALAR_NOTE_PLAN, encoding="utf-8")

    stamp_invocation_authorization(
        str(plan_path),
        typed_command="/execute-plan",
        utterance="go",
        at="2026-08-20",
        repo_root=tmp_path,
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "stamp")

    check_exit, gate = stamp_check(
        "docs/plans/2026-08-20-block-scalar-stamp-check.md", repo_root=tmp_path
    )
    assert check_exit == EXIT_OK
    assert gate["verdict"] == "match", gate


def test_replace_outright_on_a_block_scalar_note_refuses_actionably(tmp_path: Path) -> None:
    """`stamp --note` over a block-scalar note stays REFUSED -- the field
    holds the PM's verbatim authorizing words and a single-line replace
    destroys them with no reconstruction. What changed is the surface: this
    used to escape as a raw ValueError traceback out of a frontmatter
    primitive whose advice was "Fix the frontmatter manually" -- a hand-edit,
    recommended for the one field whose entire purpose is machine
    attribution. It is now a clean business failure naming the append verb.
    """
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-block-scalar-replace.md"
    plan_path.write_text(_BLOCK_SCALAR_NOTE_PLAN, encoding="utf-8")
    before = plan_path.read_text(encoding="utf-8")

    exit_code, result = stamp_execution_authorization(
        str(plan_path), "PM", "single line replacement", at="2026-08-20",
        repo_root=tmp_path,
    )

    assert exit_code == EXIT_BUSINESS_FAIL
    assert "block scalar" in result["error"]
    assert "authorize-invocation" in result["error"]
    assert "manually" not in result["error"]
    # Refused means untouched, not half-written.
    assert plan_path.read_text(encoding="utf-8") == before


def test_append_note_flag_still_works_on_a_block_scalar_note(tmp_path: Path) -> None:
    """The alternative the refusal above names has to actually exist —
    remediation text pointing at a verb that does not work is worse than the
    hand-edit advice it replaced."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-block-scalar-append-flag.md"
    plan_path.write_text(_BLOCK_SCALAR_NOTE_PLAN, encoding="utf-8")

    exit_code, result = stamp_execution_authorization(
        str(plan_path), "PM", "an appended line", at="2026-08-20",
        repo_root=tmp_path, append_note=True,
    )

    assert exit_code == EXIT_OK, result
    loaded = yaml.safe_load(plan_path.read_text(encoding="utf-8").split("---")[1])
    assert "PM: yes, go ahead and build it." in loaded["execution_authorized_note"]
    assert "an appended line" in loaded["execution_authorized_note"]


def test_malformed_block_header_fails_clean_not_as_a_traceback(tmp_path: Path) -> None:
    """The discriminator gap the reviewer found: `read_fm_block_scalar` reads
    a MALFORMED header (`|abc`) as "not a block scalar" and skips the append
    branch, while `replace_fm_field`'s looser one-character guard still
    refuses it -- and that ValueError was caught nowhere, so it escaped the
    mint as a raw traceback. Precisely the failure ec55ed90d849 closed for
    the well-formed case, left open for the malformed one."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-malformed-block-header.md"
    plan_path.write_text(
        '---\ntitle: t\nstatus: draft\nexecution_authorized_note: |abc\n---\n\n# Body\ncontent\n',
        encoding="utf-8",
    )

    exit_code, result = stamp_invocation_authorization(
        str(plan_path), typed_command="/execute-plan", utterance="go",
        at="2026-08-20", repo_root=tmp_path,
    )

    assert exit_code == EXIT_BUSINESS_FAIL
    assert "execution_authorized_note" in result["error"]


_MULTILINE_QUOTED_NOTE_PLAN = """---
title: "test plan"
status: draft
execution_authorized_note: 'Scope of this authorization: C1-C3 only. C4 stays gated
  on the DR-287 ruling...'
---

# Test Plan

Some body content.
"""


def test_multiline_quoted_note_refuses_rather_than_truncates(tmp_path: Path) -> None:
    """state/bug-backlog/2026-08-20-review-exec-auth-stamp-truncates-a-multi-
    898a8003e121.yaml: a multi-line SINGLE-QUOTED `execution_authorized_note`
    (YAML's own line-folding, no `|`/`>` header) used to be silently
    corrupted -- `read_fm_block_scalar` only recognises the block-scalar
    shape, so this note fell through to a single-line `replace_fm_field`
    write, truncating the note at its first physical line and stranding
    "on the DR-287 ruling..." as an orphaned line the next parse read as a
    bogus top-level key. Refusing is the fix: the write must not land at
    all, and the file must be byte-identical to before the attempt."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-multiline-quoted-note.md"
    plan_path.write_text(_MULTILINE_QUOTED_NOTE_PLAN, encoding="utf-8")
    before = plan_path.read_text(encoding="utf-8")

    exit_code, result = stamp_execution_authorization(
        str(plan_path), "PM", "single line replacement", at="2026-08-20",
        repo_root=tmp_path,
    )

    assert exit_code == EXIT_BUSINESS_FAIL
    assert "multi-line" in result["error"]
    assert "execution_authorized_note" in result["error"]
    # Refused means untouched -- not half-written with the scope-limiting
    # continuation line stranded as an orphan key.
    assert plan_path.read_text(encoding="utf-8") == before
    assert "on the DR-287 ruling" not in before.split("execution_authorized_note")[0]


def test_multiline_quoted_note_refuses_on_append_note_too(tmp_path: Path) -> None:
    """The refusal applies to `--append-note` (and the `authorize-invocation`
    mint that delegates to it) as much as an outright `--note` replace --
    both routes end at the same single-line write over a value that spans
    more than one physical line."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-multiline-quoted-note-append.md"
    plan_path.write_text(_MULTILINE_QUOTED_NOTE_PLAN, encoding="utf-8")
    before = plan_path.read_text(encoding="utf-8")

    exit_code, result = stamp_invocation_authorization(
        str(plan_path), typed_command="/execute-plan", utterance="go",
        at="2026-08-20", repo_root=tmp_path,
    )

    assert exit_code == EXIT_BUSINESS_FAIL
    assert "multi-line" in result["error"]
    assert plan_path.read_text(encoding="utf-8") == before


def test_cli_stamp_verb_fires_stamp_approved_as_a_separate_commit(tmp_path: Path) -> None:
    """C3 (docs/plans/2026-08-20-the-rungs-get-writers.md): the `stamp` CLI
    verb fires `stamp-approved` after its own exec-auth write returns 0,
    in a SEPARATE lock+commit -- flips `status: draft` -> `status: approved`
    and lands a second commit on top of the exec-auth-stamp commit."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-cli-stamp-approved.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(["stamp", str(plan_path), "--by", "PM", "--note", "make it so"])
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_OK
    written = plan_path.read_text(encoding="utf-8")
    assert "status: approved" in written

    log = _git(tmp_path, "log", "--oneline")
    # exec-auth-stamp's own write is uncommitted (locked_rmw, no git-commit --
    # see module negative-spec); stamp-approved's flip lands its own commit
    # on top of the plan's initial commit.
    assert "stamp status" in log.stdout


def test_cli_stamp_verb_fires_stamp_reviewed_before_stamp_approved(tmp_path: Path) -> None:
    """C5: the `stamp` CLI verb (the `/review` cross-reference exit) fires
    `stamp-reviewed` FIRST, then `stamp-approved` -- two separate rung
    commits land on top of the exec-auth-stamp write, `draft -> reviewed`
    then `reviewed -> approved`."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-cli-stamp-reviewed.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(["stamp", str(plan_path), "--by", "PM", "--note", "make it so"])
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_OK
    written = plan_path.read_text(encoding="utf-8")
    assert "status: approved" in written

    log = _git(tmp_path, "log", "--oneline", "-n", "10")
    assert '"draft" -> reviewed' in log.stdout
    assert '"reviewed" -> approved' in log.stdout


def test_cli_authorize_invocation_never_fires_stamp_reviewed(tmp_path: Path) -> None:
    """C5's premise correction: `authorize-invocation` reaches this binary
    for a plan that may never have been reviewed at all, so it must NOT
    fire `stamp-reviewed` -- only `stamp-approved`. Firing `stamp-reviewed`
    generically from `main()` would stamp `reviewed` onto a never-reviewed
    plan, a status lie this chunk exists to avoid."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-cli-authorize-invocation-no-reviewed.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(
            ["authorize-invocation", str(plan_path), "--typed-command", "/execute-plan"]
        )
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_OK
    written = plan_path.read_text(encoding="utf-8")
    assert "status: approved" in written

    log = _git(tmp_path, "log", "--oneline", "-n", "10")
    assert "-> reviewed" not in log.stdout
    assert '"draft" -> approved' in log.stdout


def test_cli_authorize_invocation_fires_stamp_approved(tmp_path: Path) -> None:
    """AC9's placement decision: `stamp-approved` fires from BOTH CLI verbs,
    including `authorize-invocation` -- invocation-authorized plans reach
    `approved` too, per the EM decision recorded in this chunk's brief."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-cli-authorize-invocation.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(
            ["authorize-invocation", str(plan_path), "--typed-command", "/execute-plan"]
        )
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_OK
    written = plan_path.read_text(encoding="utf-8")
    assert "status: approved" in written


def test_direct_in_process_mint_call_never_flips_status(tmp_path: Path) -> None:
    """The accepted cost of hooking at the CLI-verb layer rather than inside
    the mint (this chunk's placement decision): a caller that invokes
    `stamp_execution_authorization` directly, in-process, bypassing
    `main()`, never fires `stamp-approved` -- status stays whatever it was."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-20-direct-mint-call.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    exit_code, result = stamp_execution_authorization(
        str(plan_path), "PM", "make it so", at="2026-08-20", repo_root=tmp_path
    )

    assert exit_code == EXIT_OK
    assert result["applied"] is True
    written = plan_path.read_text(encoding="utf-8")
    assert "status: draft" in written
    assert "status: approved" not in written


def test_cli_mark_reviewed_leaves_plan_at_reviewed_without_authorizing(tmp_path: Path) -> None:
    """The `mark-reviewed` verb is the review-integration rung producer: it
    flips `draft -> reviewed` and writes NO `execution_authorized_*` field,
    so a reviewed-and-integrated plan awaiting the PM is observably at
    `reviewed` rather than indistinguishable from an unread `draft` (DoE
    memo 2026-08-27-doe-claude-em-stamp-reviewed-bound-to-approval-
    ceremony)."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-27-cli-mark-reviewed.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(["mark-reviewed", str(plan_path)])
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_OK
    written = plan_path.read_text(encoding="utf-8")
    assert "status: reviewed" in written
    assert "execution_authorized_by" not in written
    assert "execution_authorized_sha" not in written

    log = _git(tmp_path, "log", "--oneline", "-n", "10")
    assert '"draft" -> reviewed' in log.stdout


def test_cli_stamp_after_mark_reviewed_still_reaches_approved(tmp_path: Path) -> None:
    """The `stamp` verb keeps its own `stamp-reviewed` fire, which is an
    at-or-past rc-0 no-op once `mark-reviewed` has already run: the PM's
    execution approval still lands `approved` and the four exec-auth
    fields."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-27-cli-mark-reviewed-then-stamp.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert main(["mark-reviewed", str(plan_path)]) == EXIT_OK
        exit_code = main(["stamp", str(plan_path), "--by", "PM", "--note", "make it so"])
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_OK
    written = plan_path.read_text(encoding="utf-8")
    assert "status: approved" in written
    assert "execution_authorized_by: PM" in written


def test_cli_mark_reviewed_is_convergent(tmp_path: Path) -> None:
    """A second `mark-reviewed` on an already-reviewed plan is an rc-0
    no-op (`_stamp_rung`'s at-or-past rule), not an error and not a second
    commit."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-27-cli-mark-reviewed-convergent.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert main(["mark-reviewed", str(plan_path)]) == EXIT_OK
        first_log = _git(tmp_path, "log", "--oneline").stdout
        assert main(["mark-reviewed", str(plan_path)]) == EXIT_OK
        second_log = _git(tmp_path, "log", "--oneline").stdout
    finally:
        os.chdir(cwd)

    assert first_log == second_log
    assert "status: reviewed" in plan_path.read_text(encoding="utf-8")


def test_cli_mark_reviewed_reports_a_failed_rung_in_its_exit_code(tmp_path: Path) -> None:
    """Unlike the `stamp` verb's fail-open side-effect fire, `mark-reviewed`
    exists ONLY to advance the rung -- so a rung that does not advance is a
    non-zero exit, never a stderr line under an rc-0."""
    _init_repo(tmp_path)

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(["mark-reviewed", "docs/plans/does-not-exist.md"])
    finally:
        os.chdir(cwd)

    # Tightened from `!= EXIT_OK`: that also passes for EXIT_USAGE (2), which
    # would mask a regression misclassifying a runtime refusal as an argv
    # error -- Review: coordinator:code-reviewer session 403ab86c Finding 4.
    assert exit_code == EXIT_BUSINESS_FAIL


def test_cli_mark_reviewed_requires_a_plan_path() -> None:
    """Usage error, not a traceback, when the path argument is missing."""
    assert main(["mark-reviewed"]) == EXIT_USAGE


def test_cli_mark_reviewed_refuses_a_frozen_status(tmp_path: Path) -> None:
    """`mark-reviewed` is the first CLI-reachable caller of `stamp-reviewed`
    to actually surface `_stamp_rung`'s frozen-status refusal (AC2) --
    previously masked by the `stamp` verb's own at-or-past no-op almost
    always applying. A `superseded` source must refuse non-zero and leave
    the plan untouched, not silently land `reviewed` over a terminal status.
    Review: coordinator:code-reviewer session 403ab86c Finding 3."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-27-cli-mark-reviewed-frozen.md"
    plan_path.write_text(_PLAN_TEXT.replace("status: draft", "status: superseded"), encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(["mark-reviewed", str(plan_path)])
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_BUSINESS_FAIL
    written = plan_path.read_text(encoding="utf-8")
    assert "status: superseded" in written
    assert "status: reviewed" not in written


def test_cli_mark_reviewed_refuses_an_unexpected_status(tmp_path: Path) -> None:
    """AC5's other refusal branch: a status outside both `_FROZEN_STATUSES`
    and `_FLIPPABLE_STATUSES` (an unparseable/typo'd value) must also abort
    non-zero rather than being silently coerced to `reviewed`.
    Review: coordinator:code-reviewer session 403ab86c Finding 3."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-27-cli-mark-reviewed-unexpected.md"
    plan_path.write_text(_PLAN_TEXT.replace("status: draft", "status: bogus"), encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(["mark-reviewed", str(plan_path)])
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_BUSINESS_FAIL
    written = plan_path.read_text(encoding="utf-8")
    assert "status: bogus" in written
    assert "status: reviewed" not in written


def test_cli_authorize_invocation_never_passes_through_reviewed(tmp_path: Path) -> None:
    """`authorize-invocation` calls `_fire_stamp_approved` only, never
    `_fire_stamp_reviewed` -- a never-reviewed plan reaching the
    `/execute-plan` path must land `approved` directly and must NEVER be
    observed at `reviewed` on the way, since that is the invariant that
    stops a never-reviewed plan being stamped `reviewed` on this path.
    Structural today (no shared call site with the `stamp` verb's two-fire
    sequence); this test is what would fail if a future edit copy-pasted
    that sequence in. Review: coordinator:code-reviewer session 403ab86c
    Finding 5."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-08-27-cli-authorize-invocation-never-reviewed.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")
    _git(tmp_path, "add", str(plan_path.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-m", "add test plan")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exit_code = main(
            ["authorize-invocation", str(plan_path), "--typed-command", "/execute-plan"]
        )
    finally:
        os.chdir(cwd)

    assert exit_code == EXIT_OK
    written = plan_path.read_text(encoding="utf-8")
    assert "status: approved" in written

    log = _git(tmp_path, "log", "--oneline", "-n", "10")
    assert "-> reviewed" not in log.stdout
    assert '"draft" -> approved' in log.stdout


def _stamp_then_edit_body(plan_path: Path, tmp_path: Path, new_body_line: str) -> str:
    """Stamp *plan_path* against `_PLAN_TEXT`, then append *new_body_line*
    to its body -- the common "PM authorized, then the body moved" setup
    for the restamp tests. Returns the pre-edit (originally stamped) sha."""
    exit_code, result = stamp_execution_authorization(
        str(plan_path), "PM", "make it so", at="2026-07-24", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    original_sha = result["sha"]
    edited = plan_path.read_text(encoding="utf-8") + new_body_line
    plan_path.write_text(edited, encoding="utf-8")
    return original_sha


def test_restamp_rebinds_sha_and_records_witness(tmp_path: Path) -> None:
    """AC1: restamp rebinds execution_authorized_sha to the live body,
    records the pre-edit sha as execution_restamped_from_sha, and leaves
    execution_authorized_{by,at,note} byte-identical."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    original_sha = _stamp_then_edit_body(plan_path, tmp_path, "\nMore body.\n")
    edited_text = plan_path.read_text(encoding="utf-8")
    new_sha = _canonical_body_sha(tmp_path, edited_text)
    assert new_sha != original_sha

    exit_code, result = restamp_execution_authorization(
        str(plan_path), "EM:session-1", "EM correction", at="2026-07-26", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    assert result["applied"] is True
    assert result["sha"] == new_sha

    written = plan_path.read_text(encoding="utf-8")
    assert f"execution_authorized_sha: {new_sha}" in written
    assert "execution_authorized_by: PM" in written
    assert 'execution_authorized_note: "make it so"' in written or "execution_authorized_note: make it so" in written
    assert f"execution_restamped_from_sha: {original_sha}" in written
    assert "execution_restamped_by: 'EM:session-1'" in written or "execution_restamped_by: EM:session-1" in written
    assert "EM correction" in written


def test_restamp_chained_keeps_original_from_sha(tmp_path: Path) -> None:
    """AC2: a second edit plus a second restamp leaves _from_sha unchanged
    (the first PM-witnessed sha) and appends the second reason to _note.
    Repeating that second restamp is applied: false."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    original_sha = _stamp_then_edit_body(plan_path, tmp_path, "\nMore body.\n")

    exit_code, result = restamp_execution_authorization(
        str(plan_path), "EM:session-1", "first correction", at="2026-07-26", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    first_restamp_sha = result["sha"]

    # A second edit.
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\nEven more body.\n", encoding="utf-8")

    exit_code2, result2 = restamp_execution_authorization(
        str(plan_path), "EM:session-2", "second correction", at="2026-07-27", repo_root=tmp_path
    )
    assert exit_code2 == EXIT_OK
    assert result2["applied"] is True
    second_restamp_sha = result2["sha"]
    assert second_restamp_sha != first_restamp_sha

    written = plan_path.read_text(encoding="utf-8")
    assert f"execution_restamped_from_sha: {original_sha}" in written
    assert f"execution_authorized_sha: {second_restamp_sha}" in written
    assert "first correction" in written
    assert "second correction" in written

    # Repeating the second restamp (unchanged body) is a no-op.
    exit_code3, result3 = restamp_execution_authorization(
        str(plan_path), "EM:session-2", "second correction", at="2026-07-27", repo_root=tmp_path
    )
    assert exit_code3 == EXIT_OK
    assert result3["applied"] is False


def test_stamp_append_note_over_changed_body_refuses_and_names_restamp(tmp_path: Path) -> None:
    """AC3: `stamp --append-note` over a changed body exits 1, leaves the
    file byte-identical, and names `restamp` in the message. The identical
    call on an unchanged body still succeeds."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    _stamp_then_edit_body(plan_path, tmp_path, "\nMore body.\n")
    before = plan_path.read_text(encoding="utf-8")

    exit_code, result = stamp_execution_authorization(
        str(plan_path),
        "PM",
        "Y",
        at="2026-07-25",
        repo_root=tmp_path,
        append_note=True,
    )
    assert exit_code == EXIT_BUSINESS_FAIL
    assert "restamp" in result["error"]
    after = plan_path.read_text(encoding="utf-8")
    assert after == before

    # The same call on a plan whose body never moved since its stamp still
    # succeeds -- the refusal is keyed on body drift, not the flag alone.
    plan_path_b = plan_dir / "2026-07-24-test-plan-unchanged.md"
    plan_path_b.write_text(_PLAN_TEXT, encoding="utf-8")
    exit_code2, first_result = stamp_execution_authorization(
        str(plan_path_b), "PM", "make it so", at="2026-07-24", repo_root=tmp_path
    )
    assert exit_code2 == EXIT_OK
    exit_code3, result3 = stamp_execution_authorization(
        str(plan_path_b),
        "PM",
        "Y",
        at="2026-07-25",
        repo_root=tmp_path,
        append_note=True,
    )
    assert exit_code3 == EXIT_OK
    assert result3["applied"] is True


def test_stamp_identical_note_over_changed_body_refuses_and_names_escape_routes(
    tmp_path: Path,
) -> None:
    """AC4: `stamp --note <existing note>` over a changed body exits 1,
    leaves the file byte-identical, and the message names both escape
    routes (a new note, or authorize-invocation)."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    _stamp_then_edit_body(plan_path, tmp_path, "\nMore body.\n")
    before = plan_path.read_text(encoding="utf-8")

    exit_code, result = stamp_execution_authorization(
        str(plan_path), "PM", "make it so", at="2026-07-25", repo_root=tmp_path
    )
    assert exit_code == EXIT_BUSINESS_FAIL
    assert "stamp --note" in result["error"]
    assert "authorize-invocation" in result["error"]
    after = plan_path.read_text(encoding="utf-8")
    assert after == before


def test_fresh_authorization_clears_restamp_quartet(tmp_path: Path) -> None:
    """AC5: `authorize-invocation` and `stamp --note <new words>` on a
    restamped plan each exit 0 and leave no execution_restamped_* key."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan-a.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    _stamp_then_edit_body(plan_path, tmp_path, "\nMore body.\n")
    exit_code, _ = restamp_execution_authorization(
        str(plan_path), "EM:session-1", "correction", at="2026-07-26", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    assert "execution_restamped_by" in plan_path.read_text(encoding="utf-8")

    exit_code2, result2 = stamp_execution_authorization(
        str(plan_path), "PM", "genuinely new words", at="2026-07-27", repo_root=tmp_path
    )
    assert exit_code2 == EXIT_OK
    assert result2["applied"] is True
    written = plan_path.read_text(encoding="utf-8")
    assert "execution_restamped_by" not in written
    assert "execution_restamped_at" not in written
    assert "execution_restamped_from_sha" not in written
    assert "execution_restamped_note" not in written

    # Same, via authorize-invocation on a second restamped plan.
    plan_path_b = plan_dir / "2026-07-24-test-plan-b.md"
    plan_path_b.write_text(_PLAN_TEXT, encoding="utf-8")
    _stamp_then_edit_body(plan_path_b, tmp_path, "\nMore body.\n")
    restamp_execution_authorization(
        str(plan_path_b), "EM:session-1", "correction", at="2026-07-26", repo_root=tmp_path
    )
    assert "execution_restamped_by" in plan_path_b.read_text(encoding="utf-8")

    exit_code3, result3 = stamp_invocation_authorization(
        str(plan_path_b), None, "/execute-plan", repo_root=tmp_path
    )
    assert exit_code3 == EXIT_OK
    written_b = plan_path_b.read_text(encoding="utf-8")
    assert "execution_restamped_by" not in written_b


def test_restamp_refuses_pm_shaped_by_and_newline_reason_and_missing_prior(
    tmp_path: Path,
) -> None:
    """AC6: `restamp` refuses --by PM, --by "pm-example-operator" and a reason with a
    real newline, each exit 2. It exits 1 on a plan with no prior
    authorization. None of these calls fires stamp-reviewed/stamp-approved."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    # No prior authorization at all -> exit 1.
    exit_code, result = restamp_execution_authorization(
        str(plan_path), "EM:session-1", "correction", repo_root=tmp_path
    )
    assert exit_code == EXIT_BUSINESS_FAIL

    _stamp_then_edit_body(plan_path, tmp_path, "\nMore body.\n")

    exit_code2, _ = restamp_execution_authorization(
        str(plan_path), "PM", "correction", repo_root=tmp_path
    )
    assert exit_code2 == EXIT_USAGE

    exit_code3, _ = restamp_execution_authorization(
        str(plan_path), "pm-example-operator", "correction", repo_root=tmp_path
    )
    assert exit_code3 == EXIT_USAGE

    exit_code4, _ = restamp_execution_authorization(
        str(plan_path), "EM:session-1", "line one\nline two", repo_root=tmp_path
    )
    assert exit_code4 == EXIT_USAGE

    written = plan_path.read_text(encoding="utf-8")
    assert "status: reviewed" not in written
    assert "status: approved" not in written


def test_restamp_revert_to_witnessed_sha_removes_quartet(tmp_path: Path) -> None:
    """AC11: a restamp whose live body sha equals the existing
    execution_restamped_from_sha (edit, restamp, revert, restamp) rebinds
    execution_authorized_sha and leaves no execution_restamped_* key."""
    _init_repo(tmp_path)
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-24-test-plan.md"
    plan_path.write_text(_PLAN_TEXT, encoding="utf-8")

    original_text = _PLAN_TEXT
    original_sha = _stamp_then_edit_body(plan_path, tmp_path, "\nMore body.\n")

    exit_code, _ = restamp_execution_authorization(
        str(plan_path), "EM:session-1", "correction", at="2026-07-26", repo_root=tmp_path
    )
    assert exit_code == EXIT_OK
    written = plan_path.read_text(encoding="utf-8")
    assert "execution_restamped_from_sha" in written

    # Revert the body back to exactly what the PM originally witnessed.
    # The frontmatter fields written by the stamp/restamp calls above are
    # preserved; only the BODY (below the second `---`) is reverted, by
    # splicing in the original text's own body after the live frontmatter's
    # own second `---` delimiter.
    def _body_after_second_delim(text: str) -> str:
        parts = text.split("---\n", 2)
        return parts[2]

    current = plan_path.read_text(encoding="utf-8")
    fm_part = current[: len(current) - len(_body_after_second_delim(current))]
    plan_path.write_text(fm_part + _body_after_second_delim(original_text), encoding="utf-8")

    reverted_text = plan_path.read_text(encoding="utf-8")
    reverted_sha = _canonical_body_sha(tmp_path, reverted_text)
    assert reverted_sha == original_sha

    exit_code2, result2 = restamp_execution_authorization(
        str(plan_path), "EM:session-2", "revert correction", at="2026-07-28", repo_root=tmp_path
    )
    assert exit_code2 == EXIT_OK
    assert result2["applied"] is True
    assert result2["sha"] == original_sha

    final_written = plan_path.read_text(encoding="utf-8")
    assert f"execution_authorized_sha: {original_sha}" in final_written
    assert "execution_restamped_by" not in final_written
    assert "execution_restamped_at" not in final_written
    assert "execution_restamped_from_sha" not in final_written
    assert "execution_restamped_note" not in final_written
