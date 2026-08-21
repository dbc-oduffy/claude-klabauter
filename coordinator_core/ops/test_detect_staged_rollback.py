"""Tests for coordinator_core.ops.detect_staged_rollback.

All fixtures are real, throwaway git repos built under pytest's `tmp_path` —
never against this repo's own working tree. Each helper commits a small
history and stages content by writing + `git add`, mirroring the exact shape
of the 2026-07-28 staged-rollback incident this module was built to catch.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.ops import detect_staged_rollback as _dsr
from coordinator_core.ops.detect_staged_rollback import (
    EXIT_BOTH_FINDINGS,
    EXIT_CLEAN,
    EXIT_MASS_DELETION_FINDING,
    EXIT_ROLLBACK_FINDING,
    MASS_DELETION_ABS_FLOOR,
    MASS_DELETION_OVERRIDE_ENV,
    MASS_DELETION_RATIO_THRESHOLD,
    MIN_ROLLBACK_DEPTH,
    MIN_ROLLBACK_PATHS,
    OVERRIDE_ENV,
    find_mass_deletion,
    find_rollback_candidates,
    main,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(repo):
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "rollback-test@example.com")
    _git(repo, "config", "user.name", "rollback-test")
    return repo


def _commit_file(repo, name, content, message):
    # Scoped to `-- name`: a bare `git commit` commits the WHOLE index, which
    # would silently sweep up another path's already-staged rollback content
    # (built earlier in the same fixture loop) into an unrelated commit.
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message, "--", name)


def _stage_file(repo, name, content):
    (repo / name).write_text(content)
    _git(repo, "add", name)


def _env(**overrides):
    base = dict(os.environ)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Clean / trivial cases


def test_clean_index_no_staged_changes(tmp_path):
    repo = _init_repo(tmp_path / "clean")
    _commit_file(repo, "f.txt", "one\n", "c1")
    assert find_rollback_candidates(str(repo)) == []
    assert main([str(repo)], env=_env()) == 0


def test_empty_repo_no_commits_no_crash(tmp_path):
    repo = _init_repo(tmp_path / "empty")
    assert find_rollback_candidates(str(repo)) == []
    assert main([str(repo)], env=_env()) == 0


def test_ordinary_new_staged_content_does_not_fire(tmp_path):
    repo = _init_repo(tmp_path / "ordinary")
    _commit_file(repo, "f.txt", "one\n", "c1")
    _stage_file(repo, "f.txt", "brand new content never committed before\n")
    assert find_rollback_candidates(str(repo)) == []
    assert main([str(repo)], env=_env()) == 0


# ---------------------------------------------------------------------------
# Single-file cases — must NOT fire alone unless deep


def test_single_file_shallow_match_does_not_fire(tmp_path):
    """Staging back to the IMMEDIATELY prior version (depth 1) of one file
    is the ordinary 'undo my last edit' shape and must not fire alone."""
    repo = _init_repo(tmp_path / "shallow")
    _commit_file(repo, "f.txt", "v1\n", "c1")
    _commit_file(repo, "f.txt", "v2\n", "c2")
    _stage_file(repo, "f.txt", "v1\n")

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 1
    assert candidates[0].depth == 1
    assert candidates[0].depth < MIN_ROLLBACK_DEPTH

    assert main([str(repo)], env=_env()) == 0


def test_single_file_deep_match_fires(tmp_path):
    """One file jumping back past >= MIN_ROLLBACK_DEPTH of its own
    intervening commits is the shape the incident actually produced —
    must fire even though only one path is involved."""
    repo = _init_repo(tmp_path / "deep")
    _commit_file(repo, "f.txt", "v1\n", "c1")
    _commit_file(repo, "f.txt", "v2\n", "c2")
    _commit_file(repo, "f.txt", "v3\n", "c3")
    _commit_file(repo, "f.txt", "v4\n", "c4")
    _stage_file(repo, "f.txt", "v1\n")  # 3 commits touching f.txt skipped back

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 1
    assert candidates[0].depth == 3
    assert candidates[0].depth >= MIN_ROLLBACK_DEPTH
    assert candidates[0].matched_subject == "c1"
    assert [s.subject for s in candidates[0].skipped] == ["c4", "c3", "c2"]

    rc = main([str(repo)], env=_env())
    assert rc == EXIT_ROLLBACK_FINDING


# ---------------------------------------------------------------------------
# Multi-file breadth


def test_two_paths_below_breadth_and_shallow_does_not_fire(tmp_path):
    repo = _init_repo(tmp_path / "two-shallow")
    for name in ("a.txt", "b.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 2
    assert len(candidates) < MIN_ROLLBACK_PATHS
    assert all(c.depth < MIN_ROLLBACK_DEPTH for c in candidates)

    assert main([str(repo)], env=_env()) == 0


def test_three_paths_breadth_fires_even_at_shallow_depth(tmp_path):
    """>= MIN_ROLLBACK_PATHS distinct rollback candidates fires regardless
    of how shallow each individual match is — this is the incident's actual
    shape reproduced: nine files, each an exact single-step-back match."""
    repo = _init_repo(tmp_path / "breadth")
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 3

    rc = main([str(repo)], env=_env())
    assert rc == 1


def test_incident_shape_nine_paths_mixed_depth_fires(tmp_path):
    """Reproduces the 2026-07-28 incident's actual shape: many files, each
    staged as an exact match to some older commit, at varying depths — the
    real event this detector exists to catch."""
    repo = _init_repo(tmp_path / "incident")
    for i in range(9):
        name = f"file{i}.py"
        # Each file gets a history depth of (i % 4) + 2 commits, so depths
        # vary across the staged set the way the real incident's nine files
        # each matched a different point in their own history.
        depth = (i % 4) + 1
        for v in range(depth + 1):
            _commit_file(repo, name, f"v{v}\n", f"{name} c{v}")
        _stage_file(repo, name, "v0\n")

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 9

    rc = main([str(repo)], env=_env())
    assert rc == 1


# ---------------------------------------------------------------------------
# Override


def test_override_env_exits_zero_but_still_prints_findings(tmp_path, capsys):
    repo = _init_repo(tmp_path / "override")
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    rc = main([str(repo)], env=_env(**{OVERRIDE_ENV: "1"}))
    assert rc == 0

    captured = capsys.readouterr()
    assert "a.txt" in captured.err
    assert "b.txt" in captured.err
    assert "c.txt" in captured.err
    # The already-applied override is confirmed without re-naming the key
    # (B6/B8, docs/wiki/guard-messaging.md § Register) -- see
    # OVERRIDE_ENV not appearing anywhere in rendered stderr below.
    assert OVERRIDE_ENV not in captured.err
    assert "override is set" in captured.err


def test_override_env_zero_value_still_blocks(tmp_path):
    """An explicit "0" is treated as not-set — mirrors the sibling gates'
    override convention (a truthy env var, not merely 'the var exists')."""
    repo = _init_repo(tmp_path / "override-zero")
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    rc = main([str(repo)], env=_env(**{OVERRIDE_ENV: "0"}))
    assert rc == 1


# ---------------------------------------------------------------------------
# Report content


def test_report_names_paths_matched_commit_and_undone_subjects(tmp_path, capsys):
    repo = _init_repo(tmp_path / "report")
    _commit_file(repo, "f.txt", "v1\n", "original work")
    _commit_file(repo, "f.txt", "v2\n", "second edit")
    _commit_file(repo, "f.txt", "v3\n", "third edit")
    _stage_file(repo, "f.txt", "v1\n")

    rc = main([str(repo)], env=_env())
    assert rc == 1

    captured = capsys.readouterr()
    assert "f.txt" in captured.err
    assert "original work" in captured.err
    assert "second edit" in captured.err
    assert "third edit" in captured.err


# ---------------------------------------------------------------------------
# Argv handling


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_exits_clean(flag, capsys):
    """Regression: `--help` was taken as a repo-root path, so the CLI died on
    `FileNotFoundError: '--help'` — a traceback where a usage block belongs.
    Asserted on `main` rather than the trampoline because that is where the
    handling lives; the bareword CLI forwards argv verbatim."""
    rc = main([flag], env=_env())

    assert rc == 0
    assert "usage: detect-staged-rollback" in capsys.readouterr().out


def test_unknown_option_is_a_usage_error_not_a_repo_root(capsys):
    rc = main(["--bogus"], env=_env())

    assert rc == 2
    captured = capsys.readouterr()
    assert "--bogus" in captured.err
    assert "usage: detect-staged-rollback" in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Mass-deletion tripwire — state/bug-backlog/2026-08-10-nothing-on-the-commit-
# path-can-see-a-mas-486778a10476.yaml. `_staged_blobs` (and therefore
# `find_rollback_candidates`) skips status-D entries BY DESIGN, so a commit
# dropping every tracked file (the 2026-08-10 empty-tree incident,
# 0a3462b72) was invisible to every check above this section. These tests
# exercise the independent check that closes that gap.


def _commit_many(repo, names, content="v1\n"):
    for name in names:
        (repo / name).write_text(content)
    _git(repo, "add", *names)
    _git(repo, "commit", "-q", "-m", f"add {len(names)} files")


def _stage_delete(repo, names):
    for name in names:
        (repo / name).unlink()
    _git(repo, "rm", "-q", "--cached", *names)


def test_find_mass_deletion_returns_none_when_nothing_staged_for_deletion(tmp_path):
    repo = _init_repo(tmp_path / "no-deletions")
    _commit_file(repo, "f.txt", "v1\n", "c1")
    _stage_file(repo, "f.txt", "v2\n")

    assert find_mass_deletion(str(repo)) is None
    assert main([str(repo)], env=_env()) == 0


def test_ordinary_single_deletion_does_not_fire(tmp_path):
    """A normal commit that happens to delete one of several tracked files
    must not trip either the ratio or the absolute-floor leg."""
    repo = _init_repo(tmp_path / "single-delete")
    names = [f"f{i}.txt" for i in range(10)]
    _commit_many(repo, names)
    _stage_delete(repo, ["f0.txt"])

    finding = find_mass_deletion(str(repo))
    assert finding is not None
    assert finding.deleted_count == 1
    assert finding.tracked_total == 10
    assert finding.ratio == pytest.approx(0.1)

    assert main([str(repo)], env=_env()) == 0


def test_ratio_at_or_above_production_threshold_fires(tmp_path):
    """Scale-invariant check against the REAL production constant (no
    monkeypatch): a repo losing >= MASS_DELETION_RATIO_THRESHOLD of its
    tracked files fires regardless of absolute size — mirrors the incident
    shape (18,506/18,506 = 1.0) at a fixture-friendly scale (9/10 = 0.9)."""
    repo = _init_repo(tmp_path / "ratio-fires")
    names = [f"f{i}.txt" for i in range(10)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:9])  # 9 of 10 -> ratio 0.9

    finding = find_mass_deletion(str(repo))
    assert finding.ratio == pytest.approx(0.9)
    assert finding.ratio >= MASS_DELETION_RATIO_THRESHOLD

    rc = main([str(repo)], env=_env())
    assert rc == EXIT_MASS_DELETION_FINDING


def test_ratio_matching_historical_legitimate_prune_does_not_fire(tmp_path):
    """Calibration check: the largest legitimate single-commit deletion ratio
    ever observed in this repo's own history (`e6783a68bd0`, "prune(reclaim):
    drop 1,709 pre-July example-doctrine-mirror-repo files reclaimed by DoE") was 1,709/3,382
    ~= 0.505. Reproduced here at fixture scale (10/20 files, same ratio) —
    this must NOT fire, or the threshold would have blocked that real,
    PM-legitimate commit."""
    repo = _init_repo(tmp_path / "legit-prune")
    names = [f"f{i}.txt" for i in range(20)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:10])  # 10 of 20 -> ratio 0.5, below 0.505 case

    finding = find_mass_deletion(str(repo))
    assert finding.ratio < MASS_DELETION_RATIO_THRESHOLD

    assert main([str(repo)], env=_env()) == 0


def test_absolute_floor_fires_independent_of_ratio(tmp_path, monkeypatch):
    """The floor leg must catch a huge deletion inside a huge repo even when
    the ratio stays low. MASS_DELETION_ABS_FLOOR is monkeypatched DOWN for
    fixture speed only (the production value, 5127, is exercised for real by
    the ratio tests above via the OR'd threshold logic) — this test's job is
    the floor's own arithmetic, not the production number."""
    monkeypatch.setattr(_dsr, "MASS_DELETION_ABS_FLOOR", 3)

    repo = _init_repo(tmp_path / "floor-fires")
    names = [f"f{i}.txt" for i in range(100)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:3])  # 3 of 100 -> ratio 0.03, well below MASS_DELETION_RATIO_THRESHOLD

    finding = find_mass_deletion(str(repo))
    assert finding.deleted_count == 3
    assert finding.ratio < MASS_DELETION_RATIO_THRESHOLD

    rc = main([str(repo)], env=_env())
    assert rc == EXIT_MASS_DELETION_FINDING


def test_mass_deletion_should_fire_boundary_at_production_floor():
    """`_mass_deletion_should_fire` against the REAL, unmonkeypatched
    MASS_DELETION_ABS_FLOOR (5127) — a synthetic MassDeletionFinding, no repo
    fixture needed. `test_absolute_floor_fires_independent_of_ratio` above
    exercises the floor's arithmetic but monkeypatches the constant down to 3
    for fixture speed; this test closes the gap by running the `>=` boundary
    check against the actual production number directly, with a ratio kept
    at 0.0 (below MASS_DELETION_RATIO_THRESHOLD) so only the floor leg can
    fire."""
    below = _dsr.MassDeletionFinding(
        deleted_count=MASS_DELETION_ABS_FLOOR - 1, tracked_total=1_000_000, ratio=0.0
    )
    at = _dsr.MassDeletionFinding(
        deleted_count=MASS_DELETION_ABS_FLOOR, tracked_total=1_000_000, ratio=0.0
    )
    above = _dsr.MassDeletionFinding(
        deleted_count=MASS_DELETION_ABS_FLOOR + 1, tracked_total=1_000_000, ratio=0.0
    )

    assert _dsr._mass_deletion_should_fire(below) is False
    assert _dsr._mass_deletion_should_fire(at) is True
    assert _dsr._mass_deletion_should_fire(above) is True


def test_mass_deletion_override_permits_commit_but_still_reports(tmp_path, capsys):
    repo = _init_repo(tmp_path / "mass-override")
    names = [f"f{i}.txt" for i in range(10)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:9])

    rc = main([str(repo)], env=_env(**{MASS_DELETION_OVERRIDE_ENV: "1"}))
    assert rc == 0

    captured = capsys.readouterr()
    assert "f0.txt" in captured.err
    # Same B6/B8 register discipline as the rollback-override test above --
    # the confirmation text no longer re-names the key.
    assert MASS_DELETION_OVERRIDE_ENV not in captured.err
    assert "override is set" in captured.err


def test_mass_deletion_override_zero_value_still_blocks(tmp_path):
    repo = _init_repo(tmp_path / "mass-override-zero")
    names = [f"f{i}.txt" for i in range(10)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:9])

    rc = main([str(repo)], env=_env(**{MASS_DELETION_OVERRIDE_ENV: "0"}))
    assert rc == EXIT_MASS_DELETION_FINDING


@pytest.mark.parametrize("value", ["false", "no", "off", "true", "yes", "2", " 1", "1 ", ""])
def test_mass_deletion_override_arms_on_nothing_but_the_literal_1(tmp_path, value):
    """Only ``"1"`` disarms the mass-deletion tripwire (PM-authorized
    2026-08-21).

    `false`/`no`/`off` are the load-bearing cases and the reason this exists:
    under the previous `not in ("", "0")` comparator each of them ARMED the
    override, so an operator spelling "leave the tripwire on" turned it off and
    got no signal that they had. `true`/`yes`/`2` are the same comparator seen
    from the other side, and the padded forms pin that no stripping happens —
    a value is the arming value or it is not.

    The tripwire's founding incident was an empty tree staged against 18,506
    files, so the direction this fails in is the whole point: an unrecognized
    value leaves the check ARMED.
    """
    repo = _init_repo(tmp_path / f"mass-override-{value.strip() or 'empty'}")
    names = [f"f{i}.txt" for i in range(10)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:9])

    rc = main([str(repo)], env=_env(**{MASS_DELETION_OVERRIDE_ENV: value}))

    assert rc == EXIT_MASS_DELETION_FINDING, (
        f"{value!r} must not arm the override — only the literal '1' does"
    )


def test_mass_deletion_override_arms_on_the_literal_1(tmp_path):
    """The counterpart to the above: the sanctioned value still works, so the
    strictness is a narrowing and not an accidental removal of the override."""
    repo = _init_repo(tmp_path / "mass-override-one")
    names = [f"f{i}.txt" for i in range(10)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:9])

    rc = main([str(repo)], env=_env(**{MASS_DELETION_OVERRIDE_ENV: "1"}))
    assert rc == EXIT_CLEAN


def test_mass_deletion_check_independent_of_rollback_check(tmp_path):
    """A commit that fires the mass-deletion check but has NO rollback
    candidates (nothing staged matches an older blob — every staged path is
    a pure deletion) must still block, and the rollback override must not
    accidentally suppress it. Exit code EXIT_MASS_DELETION_FINDING (not
    EXIT_ROLLBACK_FINDING/EXIT_BOTH_FINDINGS) is itself the proof: the two
    checks' outcomes are signalled independently, which is what lets a
    wrapper (`install_claude_klabauter_precommit_hook`) scope a bypass to the check
    that actually fired instead of conflating the two under one exit code."""
    repo = _init_repo(tmp_path / "independent")
    names = [f"f{i}.txt" for i in range(10)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:9])

    assert find_rollback_candidates(str(repo)) == []

    rc = main([str(repo)], env=_env(**{OVERRIDE_ENV: "1"}))
    assert rc == EXIT_MASS_DELETION_FINDING


def test_both_checks_firing_and_unresolved_returns_the_combined_code(tmp_path):
    """Both checks fire in the SAME commit and neither is overridden — the
    aggregate EXIT_BOTH_FINDINGS code, distinct from either check firing
    alone."""
    repo = _init_repo(tmp_path / "both-fire")
    # Mass-fixture setup FIRST, and staged BEFORE the rollback fixture's own
    # commits: `_commit_many`'s bare `git commit` commits the WHOLE index, so
    # doing it after `a/b/c.txt` were already staged (as an earlier revision
    # of this test did) silently swept their staged rollback content into an
    # unrelated commit, leaving nothing staged to detect (0 candidates, not
    # 3). `_commit_file` below is scoped (`-- name`), so it never touches
    # whatever this leg already staged for deletion.
    #
    # 100 mass-fixture files plus the 3 rollback-fixture files below (103
    # tracked total) — deleting 95 of the 100 clears the 0.90 ratio
    # threshold against that combined total (95/103 ~= 0.922), unlike a
    # smaller fixture sized only against the mass-deletion files alone.
    names = [f"f{i}.txt" for i in range(100)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:95])

    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    assert len(find_rollback_candidates(str(repo))) == 3
    mass_finding = find_mass_deletion(str(repo))
    assert mass_finding is not None
    assert mass_finding.ratio >= MASS_DELETION_RATIO_THRESHOLD

    rc = main([str(repo)], env=_env())
    assert rc == EXIT_BOTH_FINDINGS


def test_both_checks_firing_resolved_by_only_one_override_returns_the_other(tmp_path):
    """When both checks fire but only ONE override is set, the resolved
    check drops out and the exit code reflects exactly the check still
    blocking — never the aggregate code, and never 0."""
    repo = _init_repo(tmp_path / "both-fire-one-resolved")
    # Same ordering (mass fixture staged BEFORE the rollback fixture's own
    # commits) and same 103-tracked-total sizing as the sibling "both fire"
    # test above — see that test's comment for why either detail wrong
    # breaks this fixture.
    names = [f"f{i}.txt" for i in range(100)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:95])

    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    rc_rollback_overridden = main([str(repo)], env=_env(**{OVERRIDE_ENV: "1"}))
    assert rc_rollback_overridden == EXIT_MASS_DELETION_FINDING

    rc_mass_overridden = main([str(repo)], env=_env(**{MASS_DELETION_OVERRIDE_ENV: "1"}))
    assert rc_mass_overridden == EXIT_ROLLBACK_FINDING

