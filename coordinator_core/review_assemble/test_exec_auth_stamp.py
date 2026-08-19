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
    stamp_execution_authorization,
    stamp_invocation_authorization,
)
from coordinator_core.pickup_assemble.stamp_check import stamp_check

import pytest

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
