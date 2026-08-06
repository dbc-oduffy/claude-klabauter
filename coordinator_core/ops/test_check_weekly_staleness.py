"""Tests for coordinator_core.ops.check_weekly_staleness.

Golden oracle snapshotted 2026-07-16
(Port of: check-weekly-staleness.sh, example-doctrine-repo b5a4192c, 2026-07-20):

    real HEADER.md (this repo, on-disk)                       -> MILD  / 0
    cwd outside any git repo                                    -> UNKNOWN / 0
    Week starting: not yet set (placeholder)                    -> UNKNOWN / 0
    reset SHA well-formed but not a commit in this repo         -> UNKNOWN / 0
    days>=5 AND commits>=15                                     -> STALE / 0
    days>=5 XOR commits>=15                                     -> MILD / 0
    neither threshold crossed                                   -> FRESH / 0
    HEADER.md absent                                             -> UNKNOWN / 0
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from coordinator_core.ops import check_weekly_staleness as cws


# ---------------------------------------------------------------------------
# _compute_staleness — pure predicate over HEADER.md text, parity-critical
# ---------------------------------------------------------------------------


def _header(reset_sha: str, week_start: str) -> str:
    return (
        "# Week Changelog\n\n"
        f"**Week starting:** {week_start}\n"
        f"**Prior week released:** v1.0.0 (commit {reset_sha}, 2026-01-01)\n"
        "**Last /workweek-start:** 2026-01-01\n"
    )


def test_unknown_on_placeholder_week_start(monkeypatch):
    text = _header("abcdef1", "not yet set")
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "UNKNOWN"


def test_unknown_on_missing_sha(monkeypatch):
    text = "**Week starting:** 2026-07-01\n"
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "UNKNOWN"


def test_unknown_on_missing_week_start(monkeypatch):
    text = "**Prior week released:** v1.0.0 (commit abcdef1, 2026-01-01)\n"
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "UNKNOWN"


def test_unknown_on_sha_not_in_repo(monkeypatch):
    text = _header("deadbeef00", "2026-07-01")
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: False)
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "UNKNOWN"


def test_unknown_on_unparseable_week_start_date(monkeypatch):
    text = _header("abcdef1", "2026-13-45")
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "UNKNOWN"


def test_stale_when_both_thresholds_crossed(monkeypatch):
    text = _header("abcdef1", "2026-07-01")  # 15 days ago @ today=2026-07-16
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 20)
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "STALE"


def test_mild_when_only_days_crossed(monkeypatch):
    text = _header("abcdef1", "2026-07-01")  # 15 days ago
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 2)
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "MILD"


def test_mild_when_only_commits_crossed(monkeypatch):
    text = _header("abcdef1", "2026-07-14")  # 2 days ago
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 30)
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "MILD"


def test_fresh_when_neither_threshold_crossed(monkeypatch):
    text = _header("abcdef1", "2026-07-14")  # 2 days ago
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 2)
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "FRESH"


def test_boundary_exactly_five_days_and_fifteen_commits_is_stale(monkeypatch):
    text = _header("abcdef1", "2026-07-11")  # exactly 5 days ago
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 15)
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "STALE"


def test_clamps_future_week_start_to_zero_day_distance(monkeypatch):
    text = _header("abcdef1", "2026-07-20")  # in the future relative to today
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 30)
    assert cws._compute_staleness(text, today=date(2026, 7, 16)) == "MILD"


def test_commit_distance_failure_falls_back_to_zero_not_unknown(monkeypatch):
    """Negative-spec: an unresolvable rev-list falls back to 0 commits,
    exactly like the bash oracle's `|| echo 0` — NOT escalated to UNKNOWN."""
    text = _header("abcdef1", "2026-07-01")  # 15 days ago
    assert cws._commit_distance("nonexistent-sha-xyz") == 0


# ---------------------------------------------------------------------------
# _resolve_state_root — env-override seam and meta-repo/sibling-repo routing
# ---------------------------------------------------------------------------


def test_resolve_state_root_honours_test_override(monkeypatch):
    monkeypatch.setenv("CWS_TEST_STATE_ROOT", "/some/explicit/state")
    assert cws._resolve_state_root() == "/some/explicit/state"


def test_resolve_state_root_sibling_repo_uses_git_root_state(monkeypatch, tmp_path):
    monkeypatch.delenv("CWS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setattr(cws, "_git_root", lambda: str(tmp_path))
    monkeypatch.setattr(cws, "_claude_home", lambda: "/nonexistent/claude/home")
    assert cws._resolve_state_root() == str(tmp_path / "state")


def test_resolve_state_root_meta_repo_routes_to_claude_klabauter(monkeypatch, tmp_path):
    monkeypatch.delenv("CWS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setattr(cws, "_git_root", lambda: str(tmp_path))
    monkeypatch.setattr(cws, "_claude_home", lambda: str(tmp_path))
    monkeypatch.setattr(cws, "_claude_klabauter_root", lambda: "/claude-klabauter/root")
    assert cws._resolve_state_root() == str(Path("/claude-klabauter/root") / "state")


def test_resolve_state_root_meta_repo_unresolvable_claude_klabauter_returns_none(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CWS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setattr(cws, "_git_root", lambda: str(tmp_path))
    monkeypatch.setattr(cws, "_claude_home", lambda: str(tmp_path))
    monkeypatch.setattr(cws, "_claude_klabauter_root", lambda: None)
    assert cws._resolve_state_root() is None


def test_resolve_state_root_no_git_root_returns_none(monkeypatch):
    monkeypatch.delenv("CWS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setattr(cws, "_git_root", lambda: None)
    assert cws._resolve_state_root() is None


# ---------------------------------------------------------------------------
# main() — end-to-end, exit code and stdout parity
# ---------------------------------------------------------------------------


def _write_header(tmp_path: Path, body: str) -> None:
    changelog_dir = tmp_path / "week-changelog"
    changelog_dir.mkdir(parents=True, exist_ok=True)
    (changelog_dir / "HEADER.md").write_text(body, encoding="utf-8")


def test_main_prints_unknown_when_state_root_unresolvable(monkeypatch, capsys):
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: None)
    rc = cws.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "UNKNOWN"


def test_main_prints_unknown_when_header_absent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: str(tmp_path))
    rc = cws.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "UNKNOWN"


def test_main_prints_fresh_and_returns_zero(monkeypatch, tmp_path, capsys):
    _write_header(tmp_path, _header("abcdef1", "2026-07-14"))
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 1)
    monkeypatch.setattr(cws, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 7, 16))}))
    rc = cws.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "FRESH"


def test_main_always_exits_zero_even_on_stale(monkeypatch, tmp_path, capsys):
    _write_header(tmp_path, _header("abcdef1", "2026-07-01"))
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 20)
    rc = cws.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() in {"STALE", "MILD", "FRESH", "UNKNOWN"}


# ---------------------------------------------------------------------------
# --root argv path (Finding 2, slicecheck-arch-weekly-staleness-root-thread)
# ---------------------------------------------------------------------------


def test_main_root_flag_bypasses_resolve_state_root(monkeypatch, tmp_path, capsys):
    _write_header(tmp_path, _header("abcdef1", "2026-07-14"))
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: 1 / 0)
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 1)
    monkeypatch.setattr(cws, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 7, 16))}))
    rc = cws.main(["--root", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "FRESH"


def test_main_root_equals_form_bypasses_resolve_state_root(monkeypatch, tmp_path, capsys):
    _write_header(tmp_path, _header("abcdef1", "2026-07-14"))
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: 1 / 0)
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 1)
    monkeypatch.setattr(cws, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 7, 16))}))
    rc = cws.main([f"--root={tmp_path}"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "FRESH"


def test_main_no_root_falls_through_to_resolve_state_root(monkeypatch, tmp_path, capsys):
    _write_header(tmp_path, _header("abcdef1", "2026-07-14"))
    calls = []
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: (calls.append(1), str(tmp_path))[1])
    monkeypatch.setattr(cws, "_sha_exists", lambda sha, cwd=None: True)
    monkeypatch.setattr(cws, "_commit_distance", lambda sha, cwd=None: 1)
    monkeypatch.setattr(cws, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 7, 16))}))
    rc = cws.main([])
    assert rc == 0
    assert calls == [1]
    assert capsys.readouterr().out.strip() == "FRESH"


def test_main_blank_root_treated_as_absent_not_ambient_cwd(monkeypatch, capsys):
    """Finding 1 (P1): --root "" must route to _resolve_state_root(), not
    fall through to Path("") which resolves against ambient process cwd."""
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: None)
    rc = cws.main(["--root", ""])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "UNKNOWN"


def test_sha_exists_and_commit_distance_use_explicit_cwd_not_ambient(monkeypatch, tmp_path):
    """Finding 2 (P1, sliceroutine-signals-ac5-gate-slice2 cross-file + slicecheck-
    arch-weekly-staleness-root-thread): the git-derived staleness facts must run
    against the resolved root, not ambient process cwd — otherwise --root only
    steers *which HEADER.md* is read while STALE/MILD/FRESH is still computed
    against whatever repo happens to be ambient cwd."""
    captured_cwds = []

    def _fake_run(args, **kwargs):
        captured_cwds.append(kwargs.get("cwd"))

        class _Result:
            returncode = 0
            stdout = "0\n"

        return _Result()

    monkeypatch.setattr(cws.subprocess, "run", _fake_run)
    assert cws._sha_exists("deadbeef", cwd=str(tmp_path)) is True
    assert cws._commit_distance("deadbeef", cwd=str(tmp_path)) == 0
    assert captured_cwds == [str(tmp_path), str(tmp_path)]
