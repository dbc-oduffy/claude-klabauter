"""Tests for coordinator_core.ops.detect_staged_rollback.

All fixtures are real, throwaway git repos built under pytest's `tmp_path` —
never against this repo's own working tree.

2026-08-21, PM ruling: this module's exact-blob rollback detector (check 1)
was KILLED — see the module's own docstring for the full ruling. Every test
that exercised that check (breadth/depth thresholds, its own override,
"both checks fire" interaction) is gone with it. What remains here covers
the sole surviving check, the mass-deletion tripwire.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.ops import detect_staged_rollback as _dsr
from coordinator_core.ops.detect_staged_rollback import (
    EXIT_CLEAN,
    EXIT_MASS_DELETION_FINDING,
    MASS_DELETION_ABS_FLOOR,
    MASS_DELETION_OVERRIDE_ENV,
    MASS_DELETION_RATIO_THRESHOLD,
    find_mass_deletion,
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
    assert main([str(repo)], env=_env()) == 0


def test_empty_repo_no_commits_no_crash(tmp_path):
    repo = _init_repo(tmp_path / "empty")
    assert main([str(repo)], env=_env()) == 0


def test_ordinary_new_staged_content_does_not_fire(tmp_path):
    repo = _init_repo(tmp_path / "ordinary")
    _commit_file(repo, "f.txt", "one\n", "c1")
    _stage_file(repo, "f.txt", "brand new content never committed before\n")
    assert main([str(repo)], env=_env()) == 0


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
# path-can-see-a-mas-486778a10476.yaml. Commit 0a3462b72 staged git's
# canonical empty tree against an 18,506-file parent; every one of those
# paths was a status-D entry, invisible to the (now-deleted) exact-blob
# rollback check. These tests exercise the check that closes that gap.


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
    drop 1,709 pre-July claude-prime files reclaimed by DoE") was 1,709/3,382
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
    # B6/B8 register discipline (docs/wiki/guard-messaging.md § Register) --
    # the confirmation text does not re-name the key.
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
