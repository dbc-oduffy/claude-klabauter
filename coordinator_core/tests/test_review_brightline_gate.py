from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.coverage import _DagChainResult, _resolve_numstat_row_path
from coordinator_core.ops import review_brightline_gate
from coordinator_core.ops.review_brightline_gate import (
    _classify_surface,
    _is_noise_path,
    _is_planning_artifact_path,
    _is_prose_bearing_path,
    _session_scoped,
    _substance_weight,
    _sum_loc,
    _SUBSTANCE_WEIGHT_CONTENT,
    _SUBSTANCE_WEIGHT_RENAME,
    main,
)
from coordinator_core.session import claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")


def _commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def _commit_file_with_trailer(
    repo: Path, name: str, content: str, message: str, session_id: str
) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"{message}\n\nSession-Id: {session_id}")


def test_classify_surface_test_dir_wins_over_extension():
    assert _classify_surface("coordinator/tests/test_foo.py") == "test"
    assert _classify_surface("tests/foo.py") == "test"


def test_classify_surface_extensions():
    assert _classify_surface("bin/tool.sh") == "shell"
    assert _classify_surface("lib/mod.py") == "python"
    assert _classify_surface("app.tsx") == "js"
    assert _classify_surface("config.yaml") == "config"
    assert _classify_surface("README.md") == "doctrine"
    assert _classify_surface("engine.cpp") == "cpp"
    assert _classify_surface("Makefile") == "other"


def test_sum_loc_matches_grep_oe_substring_semantics():
    text = "1 file changed, 10 insertions(+), 3 deletions(-)\n"
    total, matched = _sum_loc(text)
    assert matched is True
    assert total == 13


def test_sum_loc_zero_matches_returns_unmatched():
    total, matched = _sum_loc("")
    assert (total, matched) == (0, False)


def _write_baton(path: Path, claimed_by: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'title: "{path.name}"',
                "kind: session-handoff",
                "status: claimed",
                f"claimed_by: {claimed_by}",
                "---",
                "body",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_legacy_baton(path: Path, consumed_by: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'title: "{path.name}"',
                "kind: session-handoff",
                "status: consumed",
                f"consumed_by: {consumed_by}",
                "---",
                "body",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_claim(repo: Path, sid: str, basename: str) -> None:
    """Write a ``handoff-claims`` claim-record for ``sid``/``basename`` under
    the repo's session hub (``.git/coordinator-sessions/handoff-claims/``) —
    the CLAIM-STORE ``build_ownership_index`` actually reads post-C19b-rewire.
    A ``claimed_by``/``consumed_by`` frontmatter field alone (written by
    ``_write_baton``/``_write_legacy_baton``) no longer suffices to register a
    baton as owned; this is the companion fixture call every test below needs
    for a baton it expects to appear in the owned set."""
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-07-27T00:00:00Z", encoding="utf-8")


def _write_baton_with_extra_frontmatter(path: Path, claimed_by: str, extra_fm: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'title: "{path.name}"',
                "kind: session-handoff",
                "status: claimed",
                f"claimed_by: {claimed_by}",
                extra_fm,
                "---",
                "body",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_unfiltered_small_diff_single_reviewer_ok(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    monkeypatch.chdir(repo)

    rc = main(["HEAD~1..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "range=HEAD~1..HEAD" in captured.out
    assert "commits=1" in captured.out
    assert "VERDICT=single-reviewer-ok" in captured.out


def test_unfiltered_commits_threshold_trips_partition_mandatory(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    for i in range(5):
        _commit_file(repo, f"f{i}.py", f"v = {i}\n", f"add f{i}")
    monkeypatch.chdir(repo)

    rc = main(["HEAD~5..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "commits=5" in captured.out
    assert "VERDICT=PARTITION-MANDATORY" in captured.out


def test_unfiltered_die_silent_gate_on_empty_range(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)

    rc = main(["HEAD..HEAD"])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert captured.err == ""


def test_unfiltered_bogus_range_die_silent_gate(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)

    rc = main(["bogus..range..totally-invalid"])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert captured.err == ""


def test_bare_argv_on_shared_work_branch_refuses_instead_of_sweeping_whole_branch(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "-b", "work/2026-09-21_batch")
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    monkeypatch.chdir(repo)

    rc = main([])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert "work/2026-09-21_batch" in captured.err
    assert "--session-id" in captured.err


def test_bare_argv_on_shared_work_branch_with_session_id_still_resolves(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "-b", "work/2026-09-21_batch")
    _commit_file_with_trailer(repo, "b.py", "y = 2\n", "add b", "sess-abc")
    monkeypatch.chdir(repo)

    rc = main(["--session-id", "sess-abc"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "VERDICT=" in captured.out


def test_bare_argv_on_non_shared_branch_unaffected(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature/not-shared")
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    monkeypatch.chdir(repo)

    rc = main([])
    captured = capsys.readouterr()

    assert rc == 0
    assert "VERDICT=" in captured.out


def test_session_id_missing_argument_exits_1(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)

    rc = main(["--session-id"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "--session-id requires an argument" in captured.err


def test_session_id_invalid_chars_exits_1(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.chdir(repo)

    rc = main(["--session-id", "bad id with spaces", "HEAD~1..HEAD"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "--session-id must match" in captured.err


def test_session_id_zero_match_is_vacuous_not_fatal(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    monkeypatch.chdir(repo)

    rc = main(["--session-id", "nonexistent-session-xyz", "HEAD~2..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "filtered_to=0" in captured.out
    assert "VERDICT=indeterminate" in captured.out
    assert "VERDICT=single-reviewer-ok" not in captured.out
    assert "VERDICT=PARTITION-MANDATORY" not in captured.out
    assert "gate vacuous" in captured.err


def test_session_id_zero_match_falls_back_to_uncommitted_working_tree(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")

    (repo / "uncommitted.py").write_text("z = 1\n" * 30, encoding="utf-8")
    _git(repo, "add", "uncommitted.py")

    monkeypatch.chdir(repo)

    rc = main(["--session-id", "nonexistent-session-xyz", "HEAD~2..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "VERDICT=indeterminate" not in captured.out
    assert "basis=code-only+uncommitted-tree" in captured.out
    assert "commits=0" in captured.out
    assert "filtered_to=0" in captured.out
    assert "measured the uncommitted working tree" in captured.err


def test_session_id_untrailered_commits_block_a_permissive_verdict(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)

    _commit_file_with_trailer(
        repo, "small.py", "y = 2\n", "small change", "session-under-test"
    )
    base = _git(repo, "rev-parse", "HEAD~1").strip()

    for i in range(3):
        _commit_file(
            repo, f"big{i}.py", "z = 1\n" * 40, f"untrailered work {i}"
        )

    monkeypatch.chdir(repo)

    rc = main(["--session-id", "session-under-test", f"{base}..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "VERDICT=indeterminate" in captured.out
    assert "VERDICT=single-reviewer-ok" not in captured.out
    assert "filtered_to=1" in captured.out
    assert "no Session-Id" in captured.err


def test_session_id_full_trailer_coverage_still_permits_single_reviewer_ok(
    tmp_path, capsys, monkeypatch
):
    """The coverage check must not fire when every commit IS attributed —
    including peer commits carrying a DIFFERENT session's trailer. Complete
    attribution that simply excludes this session is an honest small-diff
    answer, not a blind one."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    _commit_file_with_trailer(
        repo, "mine.py", "y = 2\n", "my change", "session-under-test"
    )
    base = _git(repo, "rev-parse", "HEAD~1").strip()
    _commit_file_with_trailer(
        repo, "peer.py", "z = 3\n", "peer change", "some-peer-session"
    )

    monkeypatch.chdir(repo)

    rc = main(["--session-id", "session-under-test", f"{base}..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "VERDICT=single-reviewer-ok" in captured.out
    assert "VERDICT=indeterminate" not in captured.out


def test_session_id_recovers_via_session_aware_floor_past_peer_commits(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)

    (repo / "own.py").write_text("mine = 1\n", encoding="utf-8")
    _git(repo, "add", "own.py")
    _git(repo, "commit", "-q", "-m", "own change\n\nSession-Id: session-under-test")
    own_sha = _git(repo, "rev-parse", "HEAD").strip()

    # DIFFERENT session's trailer — this is what "range" below will start
    (repo / "peer.py").write_text("theirs = 1\n", encoding="utf-8")
    _git(repo, "add", "peer.py")
    _git(repo, "commit", "-q", "-m", "peer change\n\nSession-Id: some-peer-session")
    peer_sha = _git(repo, "rev-parse", "HEAD").strip()

    monkeypatch.chdir(repo)

    rc = main(["--session-id", "session-under-test", f"{peer_sha}..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "commits=1" in captured.out
    assert "VERDICT=indeterminate" not in captured.out
    assert "VERDICT=single-reviewer-ok" in captured.out
    assert "session-aware floor" in captured.err


def test_session_id_floor_at_repo_root_degrades_to_indeterminate(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "root.py").write_text("root = 1\n", encoding="utf-8")
    _git(repo, "add", "root.py")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "root change\n\nSession-Id: root-only-session",
    )
    root_sha = _git(repo, "rev-parse", "HEAD").strip()

    monkeypatch.chdir(repo)

    rc = main(["--session-id", "root-only-session", f"{root_sha}..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "VERDICT=indeterminate" in captured.out
    assert "VERDICT=single-reviewer-ok" not in captured.out
    assert "VERDICT=PARTITION-MANDATORY" not in captured.out
    assert "gate vacuous" in captured.err


def test_session_scoped_grep_is_not_end_anchored(monkeypatch):
    calls = []

    def fake_run_git(args, cwd=None):
        calls.append(list(args))
        if len(calls) == 2:
            return "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n", 0
        return "", 0

    monkeypatch.setattr(review_brightline_gate, "_run_git", fake_run_git)

    _session_scoped("base..HEAD", "some-session-id")

    assert len(calls) == 4, (
        "expected initial scan + floor query + floor retry + the "
        f"uncommitted-tree fallback (P143-T1), got {calls}"
    )
    grep_args = [a for call in calls for a in call if a.startswith("--grep=")]
    assert len(grep_args) == 3
    for grep_arg in grep_args:
        assert not grep_arg.endswith("$"), (
            f"end-anchored --grep silently drops a Session-Id trailer that "
            f"is not the message's last line: {grep_arg}"
        )


def test_session_id_filters_to_matching_commits_only(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b (no trailer)")
    (repo / "c.py").write_text("z = 3\n", encoding="utf-8")
    _git(repo, "add", "c.py")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "add c\n\nSession-Id: abc123-session",
    )
    monkeypatch.chdir(repo)

    rc = main(["--session-id", "abc123-session", "HEAD~2..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "commits=1" in captured.out
    assert "filtered_to=1" in captured.out
    assert "VERDICT=indeterminate" in captured.out


def test_session_id_merge_commit_does_not_count_merged_in_work(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "-b", "other", base_sha)
    (repo / "other1.py").write_text("o1 = 1\no1b = 2\n", encoding="utf-8")
    (repo / "other2.py").write_text("o2 = 1\no2b = 2\n", encoding="utf-8")
    _git(repo, "add", "other1.py", "other2.py")
    _git(repo, "commit", "-q", "-m", "unrelated peer work (no trailer)")
    other_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "-b", "session-branch", base_sha)
    _commit_file_with_trailer(
        repo, "mine.py", "mine = 1\n", "own change", "merge-session"
    )
    _git(repo, "merge", "-q", "--no-ff", "-m",
         "Merge other into session-branch\n\nSession-Id: merge-session", other_sha)

    monkeypatch.chdir(repo)

    rc = main(["--session-id", "merge-session", f"{base_sha}..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "commits=1" in captured.out
    assert "filtered_to=1" in captured.out
    assert "loc=1 commits=1" in captured.out
    assert "VERDICT=indeterminate" in captured.out
    assert "note: 1 commit(s) in range carry no Session-Id trailer" in captured.err


def _write_governed_plan(repo_root: Path, deliverable_id: str) -> Path:
    plans_dir = repo_root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plans_dir / "2026-08-03-c13-fixture-plan.md"
    plan_path.write_text(
        "---\n"
        "title: fixture\n"
        f"deliverable_id: {deliverable_id}\n"
        "---\n\n"
        "```yaml plan-tasks\n"
        "- id: X1\n"
        "  change_kind: code-edit\n"
        "  surface: fixture/surface\n"
        "  deferred: false\n"
        "  body: fixture row\n"
        "```\n",
        encoding="utf-8",
    )
    return plan_path


def test_is_noise_path_covers_review_trail_subagent_share_ceremony():
    assert _is_noise_path("state/review-trail/2026-08-03-232111-abc.json") is True
    assert _is_noise_path("state/review-trail/findings/foo.json") is True
    assert _is_noise_path("state/subagent-share/sess-id/coordinatorexecutor-1.md") is True
    assert _is_noise_path("state/ceremony/wsc/abc-20260803T230805Z.json") is True


def test_is_noise_path_cross_repo_scoped_to_inbox_and_archive_not_readme():
    assert _is_noise_path("cross-repo/inbox/2026-08-04-some-memo.md") is True
    assert _is_noise_path("cross-repo/archive/2026-08-04-some-memo.md") is True
    assert _is_noise_path("cross-repo/README.md") is False


def test_is_noise_path_memo_outbox_already_covered_by_existing_alternation():
    assert _is_noise_path("state/memo-outbox/some-memo.md") is True


def test_is_noise_path_sizings_and_audits_not_excluded():
    """state/sizings/ and state/audits/ carry human/EM-authored routing
    rationale and analysis prose (scout_evidence, intent, audit findings) —
    measured and deliberately EXCLUDED from the noise list, unlike pure
    ceremony bookkeeping."""
    assert _is_noise_path("state/sizings/2026-08-04-some-sizing.yaml") is False
    assert _is_noise_path("state/audits/2026-08-04-some-audit.md") is False


def test_resolve_numstat_row_path_braced_rename_resolves_to_destination_for_noise():
    row = "{state/handoffs => archive/handoffs/2026-08}/2026-08-12-x.md"
    resolved = _resolve_numstat_row_path(row)
    assert resolved == "archive/handoffs/2026-08/2026-08-12-x.md"
    assert _is_noise_path(resolved) == _is_noise_path("archive/handoffs/2026-08/2026-08-12-x.md")
    assert _is_noise_path(resolved) is True


def test_resolve_numstat_row_path_mid_path_brace_not_always_leading():
    row = "cross-repo/{inbox => archive}/2026-08-12-y.md"
    resolved = _resolve_numstat_row_path(row)
    assert resolved == "cross-repo/archive/2026-08-12-y.md"
    assert _is_noise_path(resolved) is True


def test_resolve_numstat_row_path_bare_rename_resolves_to_destination():
    """AC2: the bare `old/p.md => new/p.md` form (no shared prefix/suffix to
    hoist into braces) must resolve to the DESTINATION for classification —
    a test asserting on the source path would not exercise the fix."""
    row = "docs/plans/2026-07-27-old-name.md => archive/specs/2026-07/2026-07-27-old-name.md"
    resolved = _resolve_numstat_row_path(row)
    assert resolved == "archive/specs/2026-07/2026-07-27-old-name.md"
    assert resolved != "docs/plans/2026-07-27-old-name.md"
    assert _classify_surface(resolved) == _classify_surface(
        "archive/specs/2026-07/2026-07-27-old-name.md"
    )


def test_resolve_numstat_row_path_non_rename_returned_identical():
    path = "coordinator_core/ops/review_brightline_gate.py"
    assert _resolve_numstat_row_path(path) == path


def test_resolve_numstat_row_path_single_definition_shared_with_workstream_complete():
    from coordinator_core import workstream_complete

    assert workstream_complete._resolve_numstat_row_path is _resolve_numstat_row_path


def test_is_noise_path_lockfiles_pnpm_and_bun_are_noise_package_json_is_not():
    assert _is_noise_path("pnpm-lock.yaml") is True
    assert _is_noise_path("bun.lockb") is True
    assert _is_noise_path("package.json") is False
    assert _is_noise_path("poetry.lock") is True
    assert _is_noise_path("package-lock.json") is True


def test_is_noise_path_emitted_memo_schemas_are_noise_by_exact_basename():
    assert _is_noise_path("coordinator_core/contract/cross-repo-memo.schema.json") is True
    assert _is_noise_path("coordinator_core/contract/archived-memo.schema.json") is True


def test_is_noise_path_hand_authored_schema_json_files_are_not_excluded():
    assert _is_noise_path("coordinator_core/frontmatter/schemas/plan.schema.json") is False
    assert _is_noise_path("coordinator_core/contract/change-signal.schema.json") is False
    assert _is_noise_path("coordinator_core/contract/schema-decline-record.schema.json") is False


def test_is_prose_bearing_path_covers_md_yaml_extensions():
    assert _is_prose_bearing_path("docs/plans/2026-08-12-foo.md") is True
    assert _is_prose_bearing_path("README.markdown") is True
    assert _is_prose_bearing_path("state/sizings/2026-08-12-foo.yaml") is True
    assert _is_prose_bearing_path("coordinator/config.yml") is True


def test_is_prose_bearing_path_excludes_code_extensions():
    assert _is_prose_bearing_path("coordinator_core/ops/review_brightline_gate.py") is False
    assert _is_prose_bearing_path("a.sh") is False
    assert _is_prose_bearing_path("a.json") is False


def test_is_prose_bearing_path_extension_only_no_code_directory_carveout():
    assert _is_prose_bearing_path("coordinator_core/tests/fixtures/foo.yaml") is True
    assert _is_prose_bearing_path("coordinator_core/ops/config.yaml") is True


def test_session_id_range_prose_only_commit_single_reviewer_ok_ac1(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)

    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text("prose\n" * 10, encoding="utf-8")
    _git(repo, "add", "docs/plan.md")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "prose-only commit\n\nSession-Id: prose-session",
    )
    monkeypatch.chdir(repo)

    rc = main(["--session-id", "prose-session", "HEAD~1..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "commits=0" in captured.out
    assert "VERDICT=single-reviewer-ok" in captured.out


def test_substance_weight_zeroes_only_content_identical_rename():
    assert _substance_weight("R", 0, 0) == _SUBSTANCE_WEIGHT_RENAME
    assert _substance_weight("R", 3, 1) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("A", 0, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("M", 0, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("D", 5, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("", 0, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("C", 0, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("C", 5, 0) == _SUBSTANCE_WEIGHT_CONTENT


def test_parse_show_numstat_pairs_interleaved_rename_to_its_own_row(tmp_path):
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)

    (repo / "rename_source.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "modify_target.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", "rename_source.py", "modify_target.py")
    _git(repo, "commit", "-q", "-m", "seed files")

    _git(repo, "mv", "rename_source.py", "rename_dest.py")
    (repo / "modify_target.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    (repo / "added_file.py").write_text("c = 1\nd = 2\n", encoding="utf-8")
    _git(repo, "add", "modify_target.py", "added_file.py")
    _git(repo, "commit", "-q", "-m", "mixed commit: rename + modify + add")
    mixed_sha = _git(repo, "rev-parse", "HEAD").strip()

    show_out = _git(repo, "show", "--raw", "--numstat", "--format=%H", mixed_sha)
    per_commit = rbg._parse_show_numstat(show_out)
    rows = per_commit[mixed_sha]

    by_path = {
        _resolve_numstat_row_path(path): (added, deleted, status)
        for added, deleted, path, status in rows
    }
    assert by_path["rename_dest.py"] == ("0", "0", "R")
    assert by_path["modify_target.py"] == ("1", "0", "M")
    assert by_path["added_file.py"] == ("2", "0", "A")


def test_is_planning_artifact_path_covers_ratified_prefixes():
    assert _is_planning_artifact_path("docs/plans/2026-08-05-foo.md") is True
    assert _is_planning_artifact_path("docs/research/2026-08-05-foo.md") is True
    assert _is_planning_artifact_path("docs/problems/2026-08-05-foo.md") is True
    assert _is_planning_artifact_path("state/plan-sidecars/2026-08-05-foo.C1.md") is True


def test_is_planning_artifact_path_excludes_doctrine_paths():
    assert _is_planning_artifact_path("docs/decisions/DR-123-foo.md") is False
    assert _is_planning_artifact_path("docs/reference/foo.md") is False
    assert _is_planning_artifact_path("docs/wiki/foo.md") is False


def _recording_plan_oracle(seen_plan_batons):
    def _inner(repo_root, owned_batons):
        seen_plan_batons.append(list(owned_batons))
        return {
            "plan_oracle": 1,
            "plan_steps": 0,
            "plan_surfaces": set(),
            "plan_repos": set(),
            "matched_plan_paths": set(),
        }

    return _inner


def test_the_verdict_line_discloses_that_its_numbers_are_code_only(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    monkeypatch.chdir(repo)

    rc = main(["HEAD~1..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "basis=code-only" in captured.out
    assert "VERDICT=" in captured.out


def test_a_prose_only_commit_still_discloses_the_basis(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "notes.md", "prose\n", "add prose")
    monkeypatch.chdir(repo)

    main(["HEAD~1..HEAD"])

    assert "basis=code-only" in capsys.readouterr().out
