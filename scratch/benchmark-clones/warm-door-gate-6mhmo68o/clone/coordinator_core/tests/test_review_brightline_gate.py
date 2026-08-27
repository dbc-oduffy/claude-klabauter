"""Characterization tests for coordinator_core.ops.review_brightline_gate.

Built against a disposable git repo fixture (real `git` subprocess calls —
this module shells out, so tests exercise it end-to-end rather than mocking
subprocess) so the parity assertions match the bash oracle byte-for-byte on
the `range=... VERDICT=...` output line.

Port of: review-brightline-gate.sh (DoE b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.coverage import _DagChainResult, _resolve_numstat_row_path
from coordinator_core.ops.review_brightline_gate import (
    _classify_surface,
    _is_noise_path,
    _is_planning_artifact_path,
    _is_prose_bearing_path,
    _substance_weight,
    _sum_loc,
    _SUBSTANCE_WEIGHT_CONTENT,
    _SUBSTANCE_WEIGHT_RENAME,
    main,
)
from coordinator_core.session import claims

# Declared, not excused: per this file's module docstring, the module under test
# itself shells out to `git` -- the parity assertions match a bash oracle byte-for-byte
# on real `range=... VERDICT=...` output, which mocking git would falsify. Each test
# spawns/mutates its own repo per test (distinct commit graphs per scenario).
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
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


# ---------------------------------------------------------------------------
# _classify_surface / _sum_loc — unit-level helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _enumerate_owned_batons — AC20: live + archive union, mid-ceremony archive
# ---------------------------------------------------------------------------


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
    """Same as _write_baton but records the holder under the LEGACY
    ``consumed_by`` field instead of ``claimed_by`` — DR-084 regression
    coverage for the dual-vocabulary corpus."""
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
    """Same as ``_write_baton`` plus an arbitrary extra frontmatter line —
    used to combine ``claimed_by`` with a gate-state field (e.g.
    ``deployment_state``) without adding a kwarg to the shared helper's
    existing call sites."""
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


# ---------------------------------------------------------------------------
# AC21 — two owned-set definitions, an asserted (not merely prose) invariant
# that each names the other rather than silently diverging unremarked.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_closing_session_id — dual-vocabulary fallback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# --from-handoff — ownership-scan-error undercount distinguishability
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# main — unfiltered path
# ---------------------------------------------------------------------------


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
    """Faithfully-reproduced bash-oracle quirk: a syntactically-valid but
    genuinely-empty range (identical trees) crashes silently (exit 1, no
    stdout, no stderr) under the bash oracle's `set -euo pipefail` — see
    module negative-spec."""
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


# ---------------------------------------------------------------------------
# main — --session-id path
# ---------------------------------------------------------------------------


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
    """AC5 regression: a zero-match session-scoped scan must not fabricate
    a permissive verdict on zero examined commits. Fails against pre-fix
    code, which emitted `VERDICT=single-reviewer-ok` here."""
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


def test_session_id_recovers_via_session_aware_floor_past_peer_commits(
    tmp_path, capsys, monkeypatch
):
    """C2 regression: the session's own trailer-carrying commit sits BEFORE
    the passed-in range's start (modeling a shared-branch merge-base that
    has advanced past this session's own commits as a peer pushed after
    it) — the initial range-scoped filter matches zero, but the
    session-aware floor (an unbounded trailer search) must recover this
    session's own commit and measure it, rather than reporting
    `indeterminate` or picking up the peer's commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    # This session's own commit — carries the trailer, comes FIRST.
    (repo / "own.py").write_text("mine = 1\n", encoding="utf-8")
    _git(repo, "add", "own.py")
    _git(repo, "commit", "-q", "-m", "own change\n\nSession-Id: session-under-test")
    own_sha = _git(repo, "rev-parse", "HEAD").strip()

    # A peer's commit, pushed AFTER this session's own commit, carrying a
    # DIFFERENT session's trailer — this is what "range" below will start
    # from, modeling a merge-base that has advanced past `own_sha`.
    (repo / "peer.py").write_text("theirs = 1\n", encoding="utf-8")
    _git(repo, "add", "peer.py")
    _git(repo, "commit", "-q", "-m", "peer change\n\nSession-Id: some-peer-session")
    peer_sha = _git(repo, "rev-parse", "HEAD").strip()

    monkeypatch.chdir(repo)

    # range = peer_sha..HEAD (== peer_sha..peer_sha, empty) models the
    # merge-base-advanced-past-own-commits scenario: the passed range does
    # not contain own_sha at all.
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
    """Reviewer P3 (coordinatorcode-reviewer-168fdc70, Finding 1): when the
    session's own earliest commit reachable from HEAD IS the repo root
    commit, `_resolve_session_floor` returns `f"{root_sha}^"` — unresolvable,
    since the root commit has no parent. The retry's
    `git log floor..HEAD` then exits non-zero with empty stdout;
    `_session_scoped` discards that return code and only checks
    `if retry_shas:`, so this must degrade cleanly to VERDICT=indeterminate
    (exit 0) rather than crash or fabricate a permissive verdict.

    Deliberately does NOT reuse `_init_repo` — that helper always creates a
    preceding "init" commit first, so `own_sha^` always resolves and this
    gap goes unexercised."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    # The session's own commit IS the repo root — no parent exists.
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

    # range = root_sha..HEAD (== root_sha..root_sha, empty) so the initial
    # range-scoped filter matches zero, forcing the session-aware-floor
    # retry path — whose floor (root_sha^) is unresolvable.
    rc = main(["--session-id", "root-only-session", f"{root_sha}..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "VERDICT=indeterminate" in captured.out
    assert "VERDICT=single-reviewer-ok" not in captured.out
    assert "VERDICT=PARTITION-MANDATORY" not in captured.out
    assert "gate vacuous" in captured.err


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
    assert "VERDICT=single-reviewer-ok" in captured.out


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


# ---------------------------------------------------------------------------
# _is_noise_path / _compute_chain_oracle — 2026-08-04 ceremony-bookkeeping
# noise-exclusion widening.
# Spec backlink: cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-mandatory-does-not-halt.md
#   § "Two smaller observations"
# ---------------------------------------------------------------------------


def test_is_noise_path_covers_review_trail_subagent_share_ceremony():
    assert _is_noise_path("state/review-trail/2026-08-03-232111-abc.json") is True
    assert _is_noise_path("state/review-trail/findings/foo.json") is True
    assert _is_noise_path("state/subagent-share/sess-id/coordinatorexecutor-1.md") is True
    assert _is_noise_path("state/ceremony/wsc/abc-20260803T230805Z.json") is True


def test_is_noise_path_cross_repo_scoped_to_inbox_and_archive_not_readme():
    assert _is_noise_path("cross-repo/inbox/2026-08-04-some-memo.md") is True
    assert _is_noise_path("cross-repo/archive/2026-08-04-some-memo.md") is True
    # README.md lives at the cross-repo/ root, NOT under inbox/ or archive/ —
    # a hand-edit there must stay reviewable, so the noise prefix is scoped
    # to the two memo subdirs, never the bare `cross-repo/` prefix.
    assert _is_noise_path("cross-repo/README.md") is False


def test_is_noise_path_memo_outbox_already_covered_by_existing_alternation():
    """`state/[^/]+-outbox/` (pre-existing) already matches `state/memo-outbox/`
    — this is a regression guard proving that alternation, not a new one, is
    what covers it (see the C1 comment block's "already caught" note)."""
    assert _is_noise_path("state/memo-outbox/some-memo.md") is True


def test_is_noise_path_sizings_and_audits_not_excluded():
    """state/sizings/ and state/audits/ carry human/EM-authored routing
    rationale and analysis prose (scout_evidence, intent, audit findings) —
    measured and deliberately EXCLUDED from the noise list, unlike pure
    ceremony bookkeeping."""
    assert _is_noise_path("state/sizings/2026-08-04-some-sizing.yaml") is False
    assert _is_noise_path("state/audits/2026-08-04-some-audit.md") is False


def test_resolve_numstat_row_path_braced_rename_resolves_to_destination_for_noise():
    """AC1: a compact braced rename row (`{old => new}` with a shared
    prefix/suffix hoisted out of the braces) must noise-drop identically to
    the bare destination path — the literal rename fragment starting `{`
    fails `_is_noise_path`'s anchored alternation even though the
    destination plainly matches it."""
    row = "{state/handoffs => archive/handoffs/2026-08}/2026-08-12-x.md"
    resolved = _resolve_numstat_row_path(row)
    assert resolved == "archive/handoffs/2026-08/2026-08-12-x.md"
    assert _is_noise_path(resolved) == _is_noise_path("archive/handoffs/2026-08/2026-08-12-x.md")
    assert _is_noise_path(resolved) is True


def test_resolve_numstat_row_path_mid_path_brace_not_always_leading():
    """The braced form is not always anchored at the start of the row —
    `cross-repo/{inbox => archive}/y.md` hoists a shared PREFIX
    (`cross-repo/`) and SUFFIX (`/y.md`) around the braces, unlike the
    leading-brace form above."""
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
    """AC3: a normal non-rename path passes through byte-identical — this is
    what makes applying the resolution unconditionally to every row safe."""
    path = "coordinator_core/ops/review_brightline_gate.py"
    assert _resolve_numstat_row_path(path) == path


def test_resolve_numstat_row_path_single_definition_shared_with_workstream_complete():
    """AC7: exactly one definition of the rename-resolution logic exists —
    `workstream_complete/__init__.py` imports and re-exports the same
    function object from `coverage.py` rather than keeping its own copy."""
    from coordinator_core import workstream_complete

    assert workstream_complete._resolve_numstat_row_path is _resolve_numstat_row_path


def test_is_noise_path_lockfiles_pnpm_and_bun_are_noise_package_json_is_not():
    """AC6: pnpm-lock.yaml and bun.lockb are noise (regenerated, not authored
    intent); package.json is NOT (a real dependency change); poetry.lock and
    package-lock.json remain noise — no regression on the pre-existing set."""
    assert _is_noise_path("pnpm-lock.yaml") is True
    assert _is_noise_path("bun.lockb") is True
    assert _is_noise_path("package.json") is False
    assert _is_noise_path("poetry.lock") is True
    assert _is_noise_path("package-lock.json") is True


# ---------------------------------------------------------------------------
# _is_prose_bearing_path / chain+session oracle mandate exemption — C1a,
# 2026-08-12. Spec backlink:
# docs/plans/2026-08-12-review-mandate-guides-the-split.md § C1a, AC1.
# ---------------------------------------------------------------------------


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
    """Judgment call (C1a dispatch brief): a `.yaml` under a code directory
    (a fixture, a runtime-read config) is STILL prose-bearing — classification
    is by extension only, no directory-based carve-out. See
    `_is_prose_bearing_path`'s docstring for the reasoning."""
    assert _is_prose_bearing_path("coordinator_core/tests/fixtures/foo.yaml") is True
    assert _is_prose_bearing_path("coordinator_core/ops/config.yaml") is True


def test_session_id_range_prose_only_commit_single_reviewer_ok_ac1(
    tmp_path, capsys, monkeypatch
):
    """AC1, session-scoped range path (`_session_scoped`, ~L1334): a
    `--session-id`-filtered range whose only matching commit is `.md`/`.yaml`
    yields commits=0 and VERDICT=single-reviewer-ok."""
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


# ---------------------------------------------------------------------------
# _substance_weight / _accumulate_countable_rows — AC2/AC3 change-substance
# weighting, C2, 2026-08-12. Spec backlink:
# docs/plans/2026-08-12-review-mandate-guides-the-split.md § C2, AC2, AC3.
# ---------------------------------------------------------------------------


def test_substance_weight_zeroes_only_content_identical_rename():
    assert _substance_weight("R", 0, 0) == _SUBSTANCE_WEIGHT_RENAME
    assert _substance_weight("R", 3, 1) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("A", 0, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("M", 0, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("D", 5, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("", 0, 0) == _SUBSTANCE_WEIGHT_CONTENT
    # Review: code-reviewer — P3: pin "C" (copy) deliberately, not by accident
    # of "R" being the only exempted branch. A copy adds a NEW surface, not a
    # content-identical move, so it stays at full weight even at 0 added/0
    # deleted (a copy with no line-level diff, e.g. a copy-then-immediate-
    # revert-detected-as-identical case) — unlike "R", which zeroes exactly
    # that shape.
    assert _substance_weight("C", 0, 0) == _SUBSTANCE_WEIGHT_CONTENT
    assert _substance_weight("C", 5, 0) == _SUBSTANCE_WEIGHT_CONTENT


def test_parse_show_numstat_pairs_interleaved_rename_to_its_own_row(tmp_path):
    """Review: code-reviewer — P2: every prior rename test puts the rename
    as the ONLY row in its commit, leaving `_parse_show_numstat`'s positional
    raw/numstat pairing unexercised for the case most likely to break it — a
    single commit touching several files where a rename/copy is interleaved
    among plain add/modify rows. Asserts each file's raw status pairs to its
    OWN numstat row, not a neighbor's."""
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


# ---------------------------------------------------------------------------
# _is_planning_artifact_path / chain_oracle planning-artifact de-weight —
# 2026-08-06 (C7, AC8). Spec backlink: docs/plans/2026-08-05-coverage-gate-
# planning-artifact-class.md § C7.
# ---------------------------------------------------------------------------


def test_is_planning_artifact_path_covers_ratified_prefixes():
    assert _is_planning_artifact_path("docs/plans/2026-08-05-foo.md") is True
    assert _is_planning_artifact_path("docs/research/2026-08-05-foo.md") is True
    assert _is_planning_artifact_path("docs/problems/2026-08-05-foo.md") is True
    assert _is_planning_artifact_path("state/plan-sidecars/2026-08-05-foo.C1.md") is True


def test_is_planning_artifact_path_excludes_doctrine_paths():
    """docs/decisions/, docs/reference/, docs/wiki/ are doctrine, not
    planning artifacts, per the 2026-08-06 EM ruling — they must stay at
    full chain_oracle LOC weight."""
    assert _is_planning_artifact_path("docs/decisions/DR-123-foo.md") is False
    assert _is_planning_artifact_path("docs/reference/foo.md") is False
    assert _is_planning_artifact_path("docs/wiki/foo.md") is False


# ---------------------------------------------------------------------------
# C2 (2026-08-08) — discriminate a previous close's batons from the ones
# THIS close is capping. Spec backlink:
# docs/plans/2026-08-08-discriminate-a-previous-close-s-batons-f.md
# ---------------------------------------------------------------------------


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


