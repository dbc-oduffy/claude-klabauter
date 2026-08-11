"""Characterization tests for coordinator_core.ops.review_brightline_gate.

Built against a disposable git repo fixture (real `git` subprocess calls —
this module shells out, so tests exercise it end-to-end rather than mocking
subprocess) so the parity assertions match the bash oracle byte-for-byte on
the `range=... VERDICT=...` output line.

Port of: review-brightline-gate.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.coverage import _DagChainResult
from coordinator_core.ops.deliverable_equivalence import _reset_equivalence_map_cache
from coordinator_core.ops.review_brightline_gate import (
    _classify_surface,
    _compute_chain_oracle,
    _compute_plan_oracle,
    _enumerate_owned_batons,
    _find_governing_plans,
    _is_noise_path,
    _is_planning_artifact_path,
    _resolve_closing_session_id,
    _sum_loc,
    main,
)
from coordinator_core.session import claims


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


def test_ac20_enumerate_owned_batons_unions_live_and_archive(tmp_path):
    """AC20 red test: 2 owned batons for the same closing session, ONE of which
    archives mid-ceremony (workstream-complete Step 2.7 stamps+archives every
    owned childless predecessor during the SAME close this gate runs in).
    Before the widened traversal, a state/-only rglob returns 1 (the archived
    baton is invisible); after widening, it must return both.
    """
    repo = tmp_path / "repo"
    sid = "closing-sid-ac20"
    _init_repo(repo)

    live_baton = repo / "state" / "handoffs" / "still-live.md"
    _write_baton(live_baton, sid)
    _write_claim(repo, sid, live_baton.name)

    archived_baton = repo / "archive" / "handoffs" / "2026-07" / "archived-mid-ceremony.md"
    _write_baton(archived_baton, sid)

    # AC-C18b-4 regression guard: the handoff CLAIM RECORD for the archived
    # baton (independent of the frontmatter mirror asserted below) must
    # survive the ship-and-archive this fixture models — ship/archive release
    # a "plan"-class claim only, never "handoff" (see
    # claims.list_claims_by_session_checked's own lifecycle docstring).
    #
    # Review: code-reviewer slice 2 (2026-07-27), Finding 1 — this fixture
    # hand-writes the claim dir and reads it straight back; it does NOT call
    # `handoff_transition._ship` or `wsc_commit._native_cs_release_artifact`,
    # so it does not itself prove those call sites leave a handoff claim
    # alone. `coordinator_core/session/tests/test_claims.py::
    # test_list_claims_by_session_survives_real_ship_call_site` drives the
    # real `_ship` mutator for that half of the guarantee; the `wsc_commit.py`
    # ship-step wiring (hardcoded `artifact_class="plan"`) remains verified by
    # citation only, not by execution — see that function's own docstring.
    _write_claim(repo, sid, archived_baton.name)

    owned, scan_errors = _enumerate_owned_batons(repo, sid)
    assert scan_errors == []
    paths = {p for p, _fm in owned}

    assert len(owned) == 2, (
        "expected both the live and the archived owned baton to be counted; "
        f"got {sorted(str(p) for p in paths)}"
    )
    assert live_baton in paths
    assert archived_baton in paths

    # AC20 also requires the archived baton's claim record to still be legible
    # at gate-read time — the frontmatter must still carry claimed_by == sid.
    archived_fm = dict(owned)[archived_baton]
    assert archived_fm.get("claimed_by") == sid

    claimed = claims.list_claims_by_session(sid, cwd=str(repo))
    assert ("handoff-claims", archived_baton.name) in claimed, (
        "the archived baton's handoff claim record must still be present at "
        f"gate-read time; got {claimed}"
    )


def test_ac20_enumerate_owned_batons_also_checks_archive_completed(tmp_path):
    """archive/completed/ is the other archive root Step 2.7 can land a
    closed-out baton under; must also be unioned in."""
    repo = tmp_path / "repo"
    sid = "closing-sid-ac20-completed"
    _init_repo(repo)

    completed_baton = repo / "archive" / "completed" / "2026-07" / "wsc-closed.md"
    _write_baton(completed_baton, sid)
    _write_claim(repo, sid, completed_baton.name)

    owned, _scan_errors = _enumerate_owned_batons(repo, sid)
    paths = {p for p, _fm in owned}

    assert completed_baton in paths


def test_ac20_enumerate_owned_batons_no_double_count_when_path_seen_twice(tmp_path):
    """A baton must not be double-counted if it appears reachable from more
    than one searched root (defensive de-dup on the resolved absolute path)."""
    repo = tmp_path / "repo"
    sid = "closing-sid-ac20-dedup"
    _init_repo(repo)

    baton = repo / "state" / "handoffs" / "dup.md"
    _write_baton(baton, sid)
    _write_claim(repo, sid, baton.name)

    # archive/handoffs pointed at the SAME directory as state/handoffs (via
    # symlink), so the duplicate-path branch is genuinely exercised rather
    # than merely asserted.
    (repo / "archive").mkdir(parents=True, exist_ok=True)
    try:
        (repo / "archive" / "handoffs").symlink_to(repo / "state" / "handoffs")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    owned, _scan_errors = _enumerate_owned_batons(repo, sid)
    assert len(owned) == 1


def test_ac20_enumerate_owned_batons_resolves_legacy_consumed_by(tmp_path):
    """DR-084 regression: a baton recorded under the LEGACY ``consumed_by``
    vocabulary (concentrated in archive/handoffs and archive/completed, per
    this function's own docstring) must still be counted as owned — a raw
    ``fm.get("claimed_by")`` read silently drops it."""
    repo = tmp_path / "repo"
    sid = "closing-sid-legacy-vocab"
    _init_repo(repo)

    legacy_archived = repo / "archive" / "handoffs" / "2026-06" / "legacy.md"
    _write_legacy_baton(legacy_archived, sid)
    _write_claim(repo, sid, legacy_archived.name)

    owned, _scan_errors = _enumerate_owned_batons(repo, sid)
    paths = {p for p, _fm in owned}

    assert legacy_archived in paths, (
        "legacy consumed_by-vocabulary baton was not recognised as owned by "
        f"{sid!r}; got {sorted(str(p) for p in paths)}"
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


def test_ac18_gated_baton_still_appears_in_owned_set(tmp_path):
    """AC18, direction one: ownership is not suppressed by being gated. A
    baton owned by the closing session AND carrying
    ``deployment_state: awaiting_gate`` must still appear in that session's
    owned set — gate state is never an ownership input.
    """
    repo = tmp_path / "repo"
    sid = "closing-sid-ac18-gated"
    _init_repo(repo)

    gated_baton = repo / "state" / "handoffs" / "gated-but-owned.md"
    _write_baton_with_extra_frontmatter(
        gated_baton, sid, "deployment_state: awaiting_gate"
    )
    _write_claim(repo, sid, gated_baton.name)

    owned, _scan_errors = _enumerate_owned_batons(repo, sid)
    paths = {p for p, _fm in owned}

    assert gated_baton in paths, (
        "a baton claimed by the closing session must remain in its owned "
        f"set regardless of gate state; got {sorted(str(p) for p in paths)}"
    )


def test_ac18_unowned_ready_to_fire_baton_not_ownership_relevant(tmp_path):
    """AC18, direction two (the converse): a baton that is
    ``ready_to_fire`` and owned by NOBODY is not treated as gate-relevant
    via the ownership path — gate state does not confer ownership. Assert
    the unowned baton is absent from a real session's owned set, not that
    the function short-circuits on an empty session id.
    """
    repo = tmp_path / "repo"
    sid = "closing-sid-ac18-unowned"

    unowned_baton = repo / "state" / "handoffs" / "unowned-ready-to-fire.md"
    unowned_baton.parent.mkdir(parents=True, exist_ok=True)
    unowned_baton.write_text(
        "\n".join(
            [
                "---",
                f'title: "{unowned_baton.name}"',
                "kind: session-handoff",
                "status: open",
                "deployment_state: ready_to_fire",
                "---",
                "body",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    owned, _scan_errors = _enumerate_owned_batons(repo, sid)
    paths = {p for p, _fm in owned}

    assert unowned_baton not in paths, (
        "an unowned ready_to_fire baton must never appear in an owned set — "
        "gate state does not confer ownership"
    )


# ---------------------------------------------------------------------------
# AC21 — two owned-set definitions, an asserted (not merely prose) invariant
# that each names the other rather than silently diverging unremarked.
# ---------------------------------------------------------------------------


def test_ac21_enumerate_owned_batons_names_the_sibling_owned_set_definition():
    """AC21: `_enumerate_owned_batons` (this module's owned-set entry point,
    now delegating to `build_ownership_index`) must name
    `find_all_consumed_handoffs` — the OTHER owned-set definition
    (`coordinator_core.ops.ceremony.resolver`, consumed_by-DAG-first, no
    archive/completed/ scope) — as a distinct, non-unified sibling, rather
    than leaving the two-definitions divergence unasserted.

    NOTE: `build_ownership_index` (`coordinator_core/ops/ownership_index.py`)
    and `find_all_consumed_handoffs` itself
    (`coordinator_core/ops/ceremony/resolver.py`) are outside this chunk's
    write-scope (both landed in a preceding wave) — the reverse-direction
    cross-reference on THOSE two files is a follow-up, not covered by this
    assertion or this chunk."""
    from coordinator_core.ops.review_brightline_gate import _enumerate_owned_batons

    doc = _enumerate_owned_batons.__doc__ or ""
    assert "find_all_consumed_handoffs" in doc, (
        "_enumerate_owned_batons must name find_all_consumed_handoffs as the "
        "other owned-set definition"
    )


# ---------------------------------------------------------------------------
# _resolve_closing_session_id — dual-vocabulary fallback
# ---------------------------------------------------------------------------


def test_resolve_closing_session_id_falls_back_to_legacy_consumed_by(tmp_path, monkeypatch):
    """DR-084 regression: when $CLAUDE_CODE_SESSION_ID is unset, the seed
    baton's holder must resolve even when recorded under the legacy
    ``consumed_by`` field rather than ``claimed_by``."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    repo = tmp_path / "repo"
    seed = repo / "state" / "handoffs" / "seed-legacy.md"
    _write_legacy_baton(seed, "legacy-seed-sid")

    resolved = _resolve_closing_session_id(repo, str(seed))

    assert resolved == "legacy-seed-sid"


# ---------------------------------------------------------------------------
# --from-handoff — ownership-scan-error undercount distinguishability
# ---------------------------------------------------------------------------


def test_from_handoff_flags_ownership_undercount_distinct_from_genuine_empty(
    tmp_path, capsys, monkeypatch
):
    """Review: code-reviewer — Finding 4 (P2) regression test. When
    `_enumerate_owned_batons` returns an empty owned set ALONGSIDE non-empty
    `scan_errors`, the emitted BRIGHTLINE basis must mark the degraded case
    (`ownership_scan_degraded=true`) — distinguishable in control flow, not
    merely a stderr note that's easy to miss/scroll past."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    seed = repo / "state" / "handoffs" / "seed.md"
    _write_baton(seed, "closing-sid-undercount")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "closing-sid-undercount")
    monkeypatch.setattr(
        rbg,
        "_enumerate_owned_batons",
        lambda repo_root, sid: ([], ["archive/handoffs: Permission denied"]),
    )
    # XB-6 cross-repo resolution is orthogonal to this test's ownership-undercount
    # concern; stub it to "no sibling repos" so this stays a single-repo fixture.
    monkeypatch.setattr(rbg, "_resolve_cross_repo_roots", lambda repo_root: {})

    rc = rbg.main(["--from-handoff", str(seed), "HEAD~1..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "ownership_scan_degraded=true" in captured.out
    assert "UNDERCOUNT" in captured.err


def test_from_handoff_genuine_empty_owned_set_not_flagged_degraded(
    tmp_path, capsys, monkeypatch
):
    """Sibling to the above: a genuinely-empty owned set (no scan errors)
    must NOT be flagged degraded — the seed-only fallback is the correct,
    non-degraded behavior in that case."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    seed = repo / "state" / "handoffs" / "seed.md"
    _write_baton(seed, "closing-sid-genuine")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "closing-sid-genuine")
    monkeypatch.setattr(
        rbg,
        "_enumerate_owned_batons",
        lambda repo_root, sid: ([], []),
    )
    # XB-6 cross-repo resolution is orthogonal to this test's ownership-undercount
    # concern; stub it to "no sibling repos" so this stays a single-repo fixture.
    monkeypatch.setattr(rbg, "_resolve_cross_repo_roots", lambda repo_root: {})

    rc = rbg.main(["--from-handoff", str(seed), "HEAD~1..HEAD"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "ownership_scan_degraded" not in captured.out
    assert "UNDERCOUNT" not in captured.err


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


# ---------------------------------------------------------------------------
# C13 (AC16/AC17) — _find_governing_plans deliverable_id join canonicalization.
#
# Manifest: state/audits/2026-08-03-deliverable-id-join-sites.md, row #1.
# Pin: a DECLARED fork pair (baton on the loser leg, plan on the winner leg,
# or vice versa) must still join — a missed equivalence here silently
# mis-gates which plans "govern" a baton. An UNDECLARED pair (no equivalence
# entry) must NOT join — canonicalize() never invents a merge.
# ---------------------------------------------------------------------------


# Review: coordinatorcode-reviewer-d28cb0a8 Finding 1 — the equivalence-map
# memo is memoized at module scope for the process lifetime, so without a
# reset on both setup and teardown the last test in this file to resolve it
# pins that tmp_path root's map for every later test in the same pytest
# worker. Mirrors the fixture in coordinator_core/ops/test_draft_plan_aging.py.
@pytest.fixture(autouse=True)
def _reset_equivalence_memo():
    _reset_equivalence_map_cache()
    yield
    _reset_equivalence_map_cache()


def _write_equivalence_map(repo_root: Path, loser: str, winner: str) -> None:
    state_dir = repo_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "deliverable-equivalence.yaml").write_text(
        f"entries:\n  - loser: {loser}\n    winner: {winner}\n    evidence: test\n",
        encoding="utf-8",
    )
    _reset_equivalence_map_cache()


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


def test_find_governing_plans_declared_pair_still_matches(tmp_path):
    """AC17 — a baton naming the LOSER leg still finds a plan declaring the
    WINNER leg once the pair is declared in state/deliverable-equivalence.yaml.
    Un-canonicalized raw equality would silently miss this join."""
    _write_equivalence_map(tmp_path, loser="dlv-loser-abc123", winner="dlv-winner-def456")
    plan_path = _write_governed_plan(tmp_path, deliverable_id="dlv-winner-def456")

    baton_fm = {"deliverable_id": "dlv-loser-abc123"}
    matches = _find_governing_plans(tmp_path, baton_fm)

    assert plan_path in matches, (
        "declared fork pair must still join — canonicalize() should have "
        "mapped the baton's loser-leg id onto the plan's winner-leg id"
    )


def test_find_governing_plans_undeclared_pair_does_not_match(tmp_path):
    """AC17 — two genuinely different, UNDECLARED ids must never join.
    canonicalize() only ever maps a declared loser; absence of an entry is
    never treated as a silent merge."""
    _write_equivalence_map(tmp_path, loser="dlv-loser-abc123", winner="dlv-winner-def456")
    plan_path = _write_governed_plan(tmp_path, deliverable_id="dlv-unrelated-999999")

    baton_fm = {"deliverable_id": "dlv-also-unrelated-000000"}
    matches = _find_governing_plans(tmp_path, baton_fm)

    assert plan_path not in matches, (
        "undeclared pair must not join — canonicalize() must not invent a "
        "merge between ids absent from the equivalence map"
    )


def test_find_governing_plans_raw_frontmatter_untouched(tmp_path):
    """AC17 writer/emitter pin, negative form: canonicalize() is read-side
    only. Confirm the join above never rewrites the plan file or the baton
    dict it was handed — the on-disk frontmatter and the caller's own dict
    still carry the RAW ids afterward."""
    _write_equivalence_map(tmp_path, loser="dlv-loser-abc123", winner="dlv-winner-def456")
    plan_path = _write_governed_plan(tmp_path, deliverable_id="dlv-winner-def456")
    baton_fm = {"deliverable_id": "dlv-loser-abc123"}

    _find_governing_plans(tmp_path, baton_fm)

    assert baton_fm["deliverable_id"] == "dlv-loser-abc123"
    assert "deliverable_id: dlv-winner-def456" in plan_path.read_text(encoding="utf-8")


def test_compute_plan_oracle_verdict_input_changes_for_declared_pair(tmp_path):
    """AC16/AC17 — the join feeds `plan_oracle`, the value `_determine_tier`
    compares against `chain_oracle` to decide the B-vs-none VERDICT split.
    A declared pair must pull the governing plan's code-bearing rows into
    plan_steps/plan_surfaces (changing plan_oracle's inputs from the
    zero-match baseline); an undeclared pair must not."""
    _write_equivalence_map(tmp_path, loser="dlv-loser-abc123", winner="dlv-winner-def456")
    _write_governed_plan(tmp_path, deliverable_id="dlv-winner-def456")

    declared_baton = (tmp_path / "baton.md", {"deliverable_id": "dlv-loser-abc123"})
    declared_result = _compute_plan_oracle(tmp_path, [declared_baton])
    assert declared_result["plan_steps"] == 1
    assert declared_result["plan_surfaces"] == {"fixture/surface"}
    assert len(declared_result["matched_plan_paths"]) == 1

    undeclared_baton = (tmp_path / "baton.md", {"deliverable_id": "dlv-totally-unrelated"})
    undeclared_result = _compute_plan_oracle(tmp_path, [undeclared_baton])
    assert undeclared_result["plan_steps"] == 0
    assert undeclared_result["plan_surfaces"] == set()
    assert undeclared_result["matched_plan_paths"] == set()


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


def test_compute_chain_oracle_drops_noise_but_keeps_mixed_commit_code_loc(
    tmp_path, monkeypatch
):
    """File-granularity contract: a MIXED commit (one noise file, one code
    file) keeps its code-path LOC and drops only the noise-path LOC; a
    FULLY-noise commit contributes nothing to any chain metric."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)

    # Commit 1: fully noise (review-trail JSON only) — must contribute
    # nothing to chain_loc/chain_commits/chain_surfaces.
    noise_dir = repo / "state" / "review-trail"
    noise_dir.mkdir(parents=True)
    (noise_dir / "record.json").write_text('{"a": 1}\n' * 5, encoding="utf-8")
    _git(repo, "add", "state/review-trail/record.json")
    _git(repo, "commit", "-q", "-m", "ceremony: noise-only commit")
    noise_only_sha = _git(repo, "rev-parse", "HEAD").strip()

    # Commit 2: MIXED — one noise file (subagent-share sidecar) + one code
    # file. Only the code file's LOC/surface should count.
    (repo / "state" / "subagent-share").mkdir(parents=True)
    (repo / "state" / "subagent-share" / "sidecar.md").write_text(
        "notes\n" * 3, encoding="utf-8"
    )
    (repo / "b.py").write_text("y = 2\nz = 3\n", encoding="utf-8")
    _git(repo, "add", "state/subagent-share/sidecar.md", "b.py")
    _git(repo, "commit", "-q", "-m", "mixed commit")
    mixed_sha = _git(repo, "rev-parse", "HEAD").strip()

    fake_result = _DagChainResult(shas=[noise_only_sha, mixed_sha])
    monkeypatch.setattr(
        rbg, "_derive_dag_chain_set", lambda *a, **kw: fake_result
    )
    # _compute_chain_oracle's `git show --numstat` call runs against the
    # process cwd (no `cwd=` passed) — chdir into the fixture repo, matching
    # the existing test suite's convention (see other tests using main()).
    monkeypatch.chdir(repo)

    result = _compute_chain_oracle(repo, [(repo / "seed.md", {})], "closing-sid")

    # Fully-noise commit dropped entirely: only the mixed commit's code path
    # counts toward chain_commits.
    assert result["chain_commits"] == 1
    # b.py: 2 insertions, 0 deletions == 2 LOC; the noise files contribute 0.
    assert result["chain_loc"] == 2
    assert result["chain_surfaces"] == {"python"}


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


def test_planning_artifact_prefixes_shared_with_coverage_module():
    """Regression guard: the brightline gate must resolve to the SAME prefix
    tuple as coverage.py's crediting classifier, not a re-duplicated one —
    otherwise the two gate legs can silently diverge on which paths are
    planning artifacts."""
    from coordinator_core import coverage as _coverage
    from coordinator_core.ops import review_brightline_gate as _brightline

    assert (
        _brightline._PLANNING_ARTIFACT_PATH_PREFIXES
        is _coverage._PLANNING_ARTIFACT_PATH_PREFIXES
    )
    # The predicate is the symbol brightline's own call site actually uses
    # (_compute_chain_oracle), so its identity is what stops a re-duplication
    # from going unnoticed while the tuple import stays technically correct.
    assert _brightline._is_planning_artifact_path is _coverage._is_planning_artifact_path


def test_compute_chain_oracle_deweights_plan_prose_not_code(tmp_path, monkeypatch):
    """AC8 negative case, watched to fail before the fix: a commit that is
    ENTIRELY a large plan-prose edit must NOT drive chain_oracle to the same
    reviewer-count recommendation a same-sized code commit would. Before the
    fix both commits produced chain_oracle=3 (1500 LOC each, undifferentiated
    at full weight); after the fix the plan commit's LOC is scaled by
    `_PLANNING_LOC_WEIGHT` and its chain_oracle drops to 1, while the code
    commit is untouched."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)

    # Commit 1: a 1500-line plan-prose edit under docs/plans/.
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "big-plan.md").write_text(
        "line of plan prose\n" * 1500, encoding="utf-8"
    )
    _git(repo, "add", "docs/plans/big-plan.md")
    _git(repo, "commit", "-q", "-m", "plan: large plan prose commit")
    plan_sha = _git(repo, "rev-parse", "HEAD").strip()

    # Commit 2: a 1500-line CODE edit — must stay at full weight.
    (repo / "big.py").write_text("x = 1\n" * 1500, encoding="utf-8")
    _git(repo, "add", "big.py")
    _git(repo, "commit", "-q", "-m", "code: large code commit")
    code_sha = _git(repo, "rev-parse", "HEAD").strip()

    monkeypatch.chdir(repo)

    fake_plan_result = _DagChainResult(shas=[plan_sha])
    monkeypatch.setattr(rbg, "_derive_dag_chain_set", lambda *a, **kw: fake_plan_result)
    plan_result = rbg._compute_chain_oracle(repo, [(repo / "seed.md", {})], "closing-sid")

    fake_code_result = _DagChainResult(shas=[code_sha])
    monkeypatch.setattr(rbg, "_derive_dag_chain_set", lambda *a, **kw: fake_code_result)
    code_result = rbg._compute_chain_oracle(repo, [(repo / "seed.md", {})], "closing-sid")

    # De-weighted: 1500 * 0.2 = 300 chain_loc -> 1 + 300 // 500 = 1.
    assert plan_result["chain_loc"] == 300
    assert plan_result["chain_oracle"] == 1

    # Code stays at full weight: 1500 chain_loc -> 1 + 1500 // 500 = 4.
    assert code_result["chain_loc"] == 1500
    assert code_result["chain_oracle"] == 4
    assert plan_result["chain_oracle"] < code_result["chain_oracle"]


def test_compute_chain_oracle_mixed_commit_deweights_only_planning_file(
    tmp_path, monkeypatch
):
    """File-granularity contract (mirrors the noise-exclusion test above): a
    MIXED commit touching one planning-artifact file and one code file
    de-weights only the planning-artifact file's LOC, at full code weight
    for the other."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)

    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "plan.md").write_text("prose\n" * 10, encoding="utf-8")
    (repo / "c.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _git(repo, "add", "docs/plans/plan.md", "c.py")
    _git(repo, "commit", "-q", "-m", "mixed plan+code commit")
    mixed_sha = _git(repo, "rev-parse", "HEAD").strip()

    fake_result = _DagChainResult(shas=[mixed_sha])
    monkeypatch.setattr(rbg, "_derive_dag_chain_set", lambda *a, **kw: fake_result)
    monkeypatch.chdir(repo)

    result = rbg._compute_chain_oracle(repo, [(repo / "seed.md", {})], "closing-sid")

    # plan.md: 10 insertions * 0.2 weight = int(2.0) = 2.
    # c.py: 2 insertions at full weight = 2.
    assert result["chain_loc"] == 4
    assert result["chain_surfaces"] == {"doctrine", "python"}


def test_compute_chain_oracle_shares_one_dag_context_across_batons(
    tmp_path, monkeypatch
):
    """C8's win is ONE `_DagChainSetContext` for the whole baton loop, not one
    per baton.

    Pinned by object identity rather than by call count, because the
    per-baton-construction regression is invisible to every other test in this
    file: they monkeypatch `_derive_dag_chain_set` wholesale, so a context
    rebuilt on each iteration still arrives as a `shared_context=` kwarg and
    still satisfies them. That is precisely the shape the plan's Anti-scope 23
    names -- a caching change that lands green and saves nothing, because the
    thing meant to be reused is reconstructed at the call site.
    """
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _git(repo, "rev-parse", "HEAD").strip()

    seen_contexts = []

    def _recording(*args, **kwargs):
        seen_contexts.append(kwargs.get("shared_context"))
        return _DagChainResult(shas=[sha])

    monkeypatch.setattr(rbg, "_derive_dag_chain_set", _recording)
    monkeypatch.chdir(repo)

    owned = [(repo / "one.md", {}), (repo / "two.md", {}), (repo / "three.md", {})]
    rbg._compute_chain_oracle(repo, owned, "closing-sid")

    assert len(seen_contexts) == len(owned), (
        "expected one _derive_dag_chain_set call per owned baton"
    )
    assert all(ctx is not None for ctx in seen_contexts), (
        "every call must receive a shared_context -- a bare call re-derives the "
        "whole DAG for that baton"
    )
    assert len({id(ctx) for ctx in seen_contexts}) == 1, (
        "all batons must share ONE context object; distinct objects mean the "
        "context is being rebuilt per baton and nothing is amortised"
    )


# ---------------------------------------------------------------------------
# C2 (2026-08-08) — discriminate a previous close's batons from the ones
# THIS close is capping. Spec backlink:
# docs/plans/2026-08-08-discriminate-a-previous-close-s-batons-f.md
# ---------------------------------------------------------------------------


def test_ac4_gate_directive_carries_no_depends_on_and_precedes_tail_in_build_order():
    """AC4: assert the ordering by test, not by inheriting it from the plan
    prose. `d-run-chain-plan-brightline-gate` (this gate) must carry no
    `depends_on`, and its directive-list append site
    (`coordinator_core.workstream_complete.__init__.build_directives`) must
    place it strictly BEFORE `d-run-wsc-tail` — the stamp-and-archive step
    that writes `deployment_state`/`shipped_in` onto a baton. If either half
    of this does not hold, the C2 predicate's premise (the baton this close
    is capping is not yet terminal-stamped at gate-scan time) is false and
    the chunk must stop and report rather than ship the predicate."""
    from coordinator_core.workstream_complete.directives_review import (
        build_chain_plan_brightline_gate_directive,
    )

    gate_directive = build_chain_plan_brightline_gate_directive("seed.md")
    assert gate_directive["id"] == "d-run-chain-plan-brightline-gate"
    assert gate_directive["depends_on"] is None, (
        "the gate directive carrying a depends_on would mean it can be "
        "deferred past the tail's stamp — the C2 predicate assumes it never is"
    )

    import inspect

    import coordinator_core.workstream_complete as wsc_module

    build_directives_src = inspect.getsource(wsc_module.build_directives)
    gate_call_idx = build_directives_src.index(
        "build_chain_plan_brightline_gate_directive(gate.consumed_handoff)"
    )
    tail_call_idx = build_directives_src.index(
        "build_wsc_tail_directive(gate.sid, effective_decisions)"
    )
    assert gate_call_idx < tail_call_idx, (
        "d-run-chain-plan-brightline-gate must be appended to the directive "
        "list strictly before d-run-wsc-tail — if this ever inverts, the "
        "baton this close is capping may already be terminal-stamped by the "
        "time the gate scans it, and the C2 predicate below is wrong"
    )


def test_capped_by_earlier_close_true_for_terminal_deployment_state_with_shipped_in():
    """The core predicate: terminal deployment_state + a plausible shipped_in
    sha together mean an EARLIER close already capped this baton."""
    from coordinator_core.ops.review_brightline_gate import _capped_by_earlier_close

    assert _capped_by_earlier_close(
        {"deployment_state": "shipped", "shipped_in": "df8ccac3"}
    ) is True
    assert _capped_by_earlier_close(
        {"deployment_state": "abandoned", "shipped_in": "d2ea184c1234"}
    ) is True


def test_ac2_baton_stamped_during_this_same_close_still_counted():
    """AC2 (write first): the regression AC17/AC20 exist to protect. A baton
    archived during THIS close has NOT yet had its stamp-and-archive step's
    deployment_state/shipped_in land at gate-scan time (per AC4's ordering) —
    it reads a non-terminal deployment_state (or none at all), so the
    predicate must NOT exclude it. A naive "exclude anything terminal"
    predicate would destroy this property; assert directly against the
    predicate function so this test fails first if that regression reappears."""
    from coordinator_core.ops.review_brightline_gate import _capped_by_earlier_close

    # In-flight, not yet stamped — the ordinary "this close is capping it" shape.
    assert _capped_by_earlier_close({"deployment_state": "in_flight"}) is False
    # Archived mid-ceremony (AC20 fixture shape) but no shipped_in yet either.
    assert _capped_by_earlier_close({"claimed_by": "sid", "status": "claimed"}) is False


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


def test_ac3_single_baton_session_chain_owned_batons_identical_to_owned_batons(
    tmp_path, monkeypatch
):
    """AC3: a session owning exactly one baton (the ordinary case) must
    compute BOTH chain_oracle and plan_oracle over the SAME owned set as
    before this change — the single, non-terminal, not-yet-shipped baton is
    never filtered out of either."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    seed = repo / "state" / "handoffs" / "seed.md"
    _write_baton(seed, "closing-sid-ac3")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "closing-sid-ac3")
    monkeypatch.setattr(rbg, "_resolve_cross_repo_roots", lambda repo_root: {})

    seen_batons = []
    seen_plan_batons = []

    def _recording_chain_oracle(repo_root, owned_batons, closing_session_id):
        seen_batons.append(list(owned_batons))
        return {
            "chain_oracle": 1,
            "chain_loc": 0,
            "chain_commits": 0,
            "chain_surfaces": set(),
            "chain_shas": set(),
            "indeterminate": False,
            "notes": [],
        }

    monkeypatch.setattr(rbg, "_compute_chain_oracle", _recording_chain_oracle)
    monkeypatch.setattr(rbg, "_compute_plan_oracle", _recording_plan_oracle(seen_plan_batons))

    rc = rbg.main(["--from-handoff", str(seed), "HEAD~1..HEAD"])
    assert rc == 0
    assert len(seen_batons) == 1
    owned_paths = {p for p, _fm in seen_batons[0]}
    assert seed in owned_paths
    assert len(seen_batons[0]) == 1

    assert len(seen_plan_batons) == 1
    plan_owned_paths = {p for p, _fm in seen_plan_batons[0]}
    assert plan_owned_paths == owned_paths, (
        "plan_oracle must see the same single-baton owned set as chain_oracle"
    )


def test_ac1_chain_oracle_excludes_baton_capped_by_earlier_close(tmp_path, monkeypatch):
    """AC1: a session owning 2 batons, one already terminal-stamped by an
    EARLIER close (terminal deployment_state + shipped_in), computes BOTH
    the chain oracle and the plan oracle only over the baton THIS close is
    capping — narrowing one side and not the other would manufacture
    spurious plan_oracle!=chain_oracle disagreement (tier=B)."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    sid = "closing-sid-ac1"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.setattr(rbg, "_resolve_cross_repo_roots", lambda repo_root: {})

    capped_earlier = repo / "archive" / "completed" / "2026-07" / "earlier-close.md"
    capping_now = repo / "state" / "handoffs" / "this-close.md"
    monkeypatch.setattr(
        rbg,
        "_enumerate_owned_batons",
        lambda repo_root, closing_sid: (
            [
                (
                    capped_earlier,
                    {
                        "claimed_by": sid,
                        "deployment_state": "shipped",
                        "shipped_in": "df8ccac3",
                    },
                ),
                (capping_now, {"claimed_by": sid, "deployment_state": "in_flight"}),
            ],
            [],
        ),
    )
    seen_plan_batons = []
    monkeypatch.setattr(rbg, "_compute_plan_oracle", _recording_plan_oracle(seen_plan_batons))

    seen_batons = []

    def _recording_chain_oracle(repo_root, owned_batons, closing_session_id):
        seen_batons.append(list(owned_batons))
        return {
            "chain_oracle": 1,
            "chain_loc": 0,
            "chain_commits": 0,
            "chain_surfaces": set(),
            "chain_shas": set(),
            "indeterminate": False,
            "notes": [],
        }

    monkeypatch.setattr(rbg, "_compute_chain_oracle", _recording_chain_oracle)

    seed = capping_now
    seed.parent.mkdir(parents=True, exist_ok=True)
    _write_baton(seed, sid)

    rc = rbg.main(["--from-handoff", str(seed), "HEAD~1..HEAD"])
    assert rc == 0
    assert len(seen_batons) == 1
    owned_paths = {p for p, _fm in seen_batons[0]}
    assert owned_paths == {capping_now}, (
        f"expected only the baton this close is capping ({capping_now}); "
        f"got {owned_paths}"
    )

    assert len(seen_plan_batons) == 1
    plan_owned_paths = {p for p, _fm in seen_plan_batons[0]}
    assert plan_owned_paths == {capping_now}, (
        "plan_oracle must exclude the same earlier-close-capped baton as "
        f"chain_oracle; expected {{capping_now}}, got {plan_owned_paths}"
    )


def test_p1_seed_baton_stamped_by_own_earlier_pass_still_counted(tmp_path, monkeypatch):
    """P1 regression (review-integrator, 2026-08-08): `_capped_by_earlier_close`'s
    ordering premise (gate strictly precedes `d-run-wsc-tail`) holds only
    WITHIN one `build_directives()` call, not across a second pass of the
    SAME close. If `/workstream-complete` re-derives directives for this
    close after `d-run-wsc-tail` already stamped the seed baton, the seed
    reads its OWN terminal deployment_state + shipped_in at gate-scan time —
    it must still be counted by both oracles, never read as "capped by an
    earlier close." The seed IS the baton this close is capping, by
    definition, whatever its stamp state."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    sid = "closing-sid-p1-rerun"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.setattr(rbg, "_resolve_cross_repo_roots", lambda repo_root: {})

    seed = repo / "state" / "handoffs" / "this-close-seed.md"
    seed.parent.mkdir(parents=True, exist_ok=True)
    _write_baton(seed, sid)

    # Ownership index reflects the RE-RUN state: this pass's own tail
    # (`d-run-wsc-tail`) already stamped the seed terminal + shipped_in
    # during pass 1, and the ownership index resolves it via the ARCHIVE
    # path (as it would post-archive), while the seed argument passed to
    # `--from-handoff` is still the pre-archive `state/handoffs/` path — the
    # comparison must be by resolved path identity, not raw path equality.
    monkeypatch.setattr(
        rbg,
        "_enumerate_owned_batons",
        lambda repo_root, closing_sid: (
            [
                (
                    seed,
                    {
                        "claimed_by": sid,
                        "deployment_state": "shipped",
                        "shipped_in": "df8ccac3",
                    },
                ),
            ],
            [],
        ),
    )

    seen_plan_batons = []
    monkeypatch.setattr(rbg, "_compute_plan_oracle", _recording_plan_oracle(seen_plan_batons))

    seen_chain_batons = []

    def _recording_chain_oracle(repo_root, owned_batons, closing_session_id):
        seen_chain_batons.append(list(owned_batons))
        return {
            "chain_oracle": 1,
            "chain_loc": 0,
            "chain_commits": 0,
            "chain_surfaces": set(),
            "chain_shas": set(),
            "indeterminate": False,
            "notes": [],
        }

    monkeypatch.setattr(rbg, "_compute_chain_oracle", _recording_chain_oracle)

    rc = rbg.main(["--from-handoff", str(seed), "HEAD~1..HEAD"])
    assert rc == 0

    assert len(seen_chain_batons) == 1
    chain_owned_paths = {p for p, _fm in seen_chain_batons[0]}
    assert seed in chain_owned_paths, (
        "the seed baton must never be excluded by _capped_by_earlier_close "
        "— it is, by definition, the baton THIS close is capping, even when "
        "already stamped terminal by this close's own earlier pass"
    )

    assert len(seen_plan_batons) == 1
    plan_owned_paths = {p for p, _fm in seen_plan_batons[0]}
    assert seed in plan_owned_paths, (
        "plan_oracle must retain the seed baton on the same re-run shape as "
        "chain_oracle"
    )


def test_ac2_baton_stamped_during_this_same_close_still_counted_by_plan_oracle(
    tmp_path, monkeypatch
):
    """AC2, plan-oracle side: a baton archived and stamped during THIS same
    close (non-terminal at gate-scan time, per AC4's ordering) must still be
    counted by plan_oracle, not only chain_oracle — the same AC17/AC20
    property, asserted against the other oracle."""
    import coordinator_core.ops.review_brightline_gate as rbg

    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "b.py", "y = 2\n", "add b")
    sid = "closing-sid-ac2-plan"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.setattr(rbg, "_resolve_cross_repo_roots", lambda repo_root: {})

    archived_this_close = repo / "archive" / "completed" / "2026-08" / "this-close.md"
    monkeypatch.setattr(
        rbg,
        "_enumerate_owned_batons",
        lambda repo_root, closing_sid: (
            [
                (
                    archived_this_close,
                    {"claimed_by": sid, "deployment_state": "in_flight"},
                ),
            ],
            [],
        ),
    )

    seen_plan_batons = []
    monkeypatch.setattr(rbg, "_compute_plan_oracle", _recording_plan_oracle(seen_plan_batons))

    def _stub_chain_oracle(repo_root, owned_batons, closing_session_id):
        return {
            "chain_oracle": 1,
            "chain_loc": 0,
            "chain_commits": 0,
            "chain_surfaces": set(),
            "chain_shas": set(),
            "indeterminate": False,
            "notes": [],
        }

    monkeypatch.setattr(rbg, "_compute_chain_oracle", _stub_chain_oracle)

    seed = archived_this_close
    seed.parent.mkdir(parents=True, exist_ok=True)
    _write_baton(seed, sid)

    rc = rbg.main(["--from-handoff", str(seed), "HEAD~1..HEAD"])
    assert rc == 0
    assert len(seen_plan_batons) == 1
    plan_owned_paths = {p for p, _fm in seen_plan_batons[0]}
    assert archived_this_close in plan_owned_paths


def test_ac5_ambiguous_deployment_state_or_shipped_in_keeps_baton():
    """AC5: any ambiguity — absent field, malformed sha, unparseable
    frontmatter — resolves toward KEEPING the baton, never dropping it."""
    from coordinator_core.ops.review_brightline_gate import _capped_by_earlier_close

    # Absent deployment_state entirely.
    assert _capped_by_earlier_close({"shipped_in": "df8ccac3"}) is False
    # Absent shipped_in entirely, despite terminal deployment_state.
    assert _capped_by_earlier_close({"deployment_state": "shipped"}) is False
    # Non-terminal deployment_state with a well-formed shipped_in.
    assert _capped_by_earlier_close(
        {"deployment_state": "awaiting_gate", "shipped_in": "df8ccac3"}
    ) is False
    # Malformed shipped_in (not hex).
    assert _capped_by_earlier_close(
        {"deployment_state": "shipped", "shipped_in": "not-a-sha!!"}
    ) is False
    # shipped_in wrong type.
    assert _capped_by_earlier_close(
        {"deployment_state": "shipped", "shipped_in": 12345}
    ) is False
    # deployment_state wrong type.
    assert _capped_by_earlier_close(
        {"deployment_state": None, "shipped_in": "df8ccac3"}
    ) is False
    # Empty dict (unparseable/empty frontmatter).
    assert _capped_by_earlier_close({}) is False
