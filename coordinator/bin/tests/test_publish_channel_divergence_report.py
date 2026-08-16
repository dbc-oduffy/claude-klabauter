"""coordinator/bin/tests/test_publish_channel_divergence_report.py — C14
(docs/plans/2026-08-15-klabauter-release-channels.md): discharges
promotion-by-memory. The PM owns the promotion ACT; what must not depend on
memory — theirs or an EM's — is NOTICING one is due. This exercises
`report_candidate_divergence`, called from `publish.py`'s `main()` after the
round lands, for every property the plan body names:

  - quiet skip when no candidate branch exists yet (today's live state —
    the machine-local klabauter mirror repo is still on `main`, C4
    unexecuted, path via `machine-local get repos.claude_klabauter`);  # abs-path-ok: docstring citation, no live path use
  - quiet skip for a dest whose declared `track_ref` is the default
    `origin/main` (no candidate channel to diverge) or is unregistered;
  - below-threshold divergence stays silent (no per-round nagging);
  - advisory wording at the commits threshold, escalated wording at the
    higher one;
  - advisory wording at the days-since-last-promotion threshold even with a
    small commit count, sourced from the merge-base date (no promotion
    record exists yet) with that fallback named plainly in the report;
  - reports three numbers: actual distance, the promote recommendation, and
    the post-promotion delta (always 0 — promotion fast-forwards `main` onto
    the candidate ref in this topology);
  - never raises, regardless of what the underlying git calls do (reported,
    never gated — a publish must not fail on this report's account).

Uses a non-klabauter row key throughout (`some-other-mirror`), matching
`test_publish_dest_declared_ref.py`'s convention, so the test itself would
fail if the check were ever special-cased to klabauter.

Run: python -m pytest coordinator/bin/tests/test_publish_channel_divergence_report.py -q -p no:cacheprovider --maxfail=1
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(*args: str, cwd: Path, env: dict | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
        env=env,
    )


def _git_out(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    )
    return result.stdout.strip()


def _init_git_repo(root: Path, *, branch: str = "main") -> None:
    """Real `git init` fixture — every git call below goes via `cwd=`, never
    a `cd` into the clone, matching the module-under-test's own
    outside-the-clone discipline."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", branch, cwd=root)
    _git("config", "user.email", "publish-channel-divergence-test@claude-klabauter.test", cwd=root)
    _git("config", "user.name", "Publish Channel Divergence Test", cwd=root)
    _git("config", "commit.gpgsign", "false", cwd=root)
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep", cwd=root)
    _git("commit", "-m", "chore: init", cwd=root)


def _commit_empty(root: Path, message: str, *, iso_date: str | None = None) -> str:
    env = None
    if iso_date is not None:
        import os

        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = iso_date
        env["GIT_COMMITTER_DATE"] = iso_date
    _git("commit", "--allow-empty", "-m", message, cwd=root, env=env)
    return _git_out("rev-parse", "HEAD", cwd=root)


def _set_origin_main_ref(root: Path, sha: str) -> None:
    """Points `refs/remotes/origin/main` at `sha` directly, without a real
    remote — `git rev-list`/`git merge-base` only need the ref to exist, not
    a configured `origin` remote, and this keeps the fixture fast."""
    _git("update-ref", "refs/remotes/origin/main", sha, cwd=root)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_channel_divergence_report_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW_KEY = "some-other-mirror"  # deliberately not klabauter — see module docstring


def _write_registry(registry_dir: Path, *, dest: Path, track_ref: str | None) -> None:
    registry_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "[publish.mirrors]",
        f'"{_ROW_KEY}.path" = "{dest.as_posix()}"',
    ]
    if track_ref is not None:
        lines.append(f'"{_ROW_KEY}.track_ref" = "{track_ref}"')
    (registry_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Threshold constants — pinned so a future reader adjusts the number here,
# not by re-deriving the policy from the docstring prose.
# ---------------------------------------------------------------------------


def test_thresholds_match_plan_delegated_values():
    assert publish._CANDIDATE_DIVERGENCE_ADVISORY_COMMITS == 300
    assert publish._CANDIDATE_DIVERGENCE_ADVISORY_DAYS == 14
    assert publish._CANDIDATE_DIVERGENCE_ESCALATED_COMMITS == 750


# ---------------------------------------------------------------------------
# 1. No candidate branch yet — today's live state (E:/dev/claude-klabauter
#    is still on `main`, C4 unexecuted). Graceful, quiet skip.
# ---------------------------------------------------------------------------


def test_no_candidate_branch_yet_is_a_quiet_skip(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    head = _git_out("rev-parse", "HEAD", cwd=dest)
    _set_origin_main_ref(dest, head)
    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == ""


# ---------------------------------------------------------------------------
# 2. track_ref is the default `origin/main` — no candidate channel exists.
# ---------------------------------------------------------------------------


def test_default_track_ref_has_no_candidate_channel_to_diverge(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    head = _git_out("rev-parse", "HEAD", cwd=dest)
    _set_origin_main_ref(dest, head)
    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref=None)  # defaults to origin/main
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert captured.out == ""


# ---------------------------------------------------------------------------
# 3. Unregistered dest — out of scope, quiet skip.
# ---------------------------------------------------------------------------


def test_unregistered_dest_is_a_quiet_skip(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="candidate")
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.toml").write_text(
        '[publish.mirrors]\n"unrelated-key.path" = "/nowhere"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert captured.out == ""


# ---------------------------------------------------------------------------
# 4. Below both thresholds — silent, deliberately (no per-round nagging).
# ---------------------------------------------------------------------------


def test_below_threshold_divergence_is_silent(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    main_sha = _git_out("rev-parse", "HEAD", cwd=dest)
    _set_origin_main_ref(dest, main_sha)
    _git("checkout", "-b", "candidate", cwd=dest)
    _commit_empty(dest, "one commit ahead")

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert captured.out == ""


# ---------------------------------------------------------------------------
# 5. Advisory wording at the commits threshold — reports the three numbers:
#    actual distance, recommendation, post-promotion delta (0).
# ---------------------------------------------------------------------------


def test_advisory_crossed_by_commits_reports_three_numbers(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    main_sha = _git_out("rev-parse", "HEAD", cwd=dest)
    _set_origin_main_ref(dest, main_sha)
    _git("checkout", "-b", "candidate", cwd=dest)
    for i in range(4):
        _commit_empty(dest, f"commit {i}")

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))
    monkeypatch.setattr(publish, "_CANDIDATE_DIVERGENCE_ADVISORY_COMMITS", 3)
    monkeypatch.setattr(publish, "_CANDIDATE_DIVERGENCE_ESCALATED_COMMITS", 10)

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert "advisory" in captured.out
    assert "candidate" in captured.out
    assert "4 commit" in captured.out  # actual distance
    assert "Recommend promoting" in captured.out  # the recommendation
    assert "to 0" in captured.out  # post-promotion delta
    assert "ESCALATED" not in captured.out


# ---------------------------------------------------------------------------
# 6. Escalated wording once past the higher threshold.
# ---------------------------------------------------------------------------


def test_escalated_wording_past_higher_threshold(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    main_sha = _git_out("rev-parse", "HEAD", cwd=dest)
    _set_origin_main_ref(dest, main_sha)
    _git("checkout", "-b", "candidate", cwd=dest)
    for i in range(6):
        _commit_empty(dest, f"commit {i}")

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))
    monkeypatch.setattr(publish, "_CANDIDATE_DIVERGENCE_ADVISORY_COMMITS", 3)
    monkeypatch.setattr(publish, "_CANDIDATE_DIVERGENCE_ESCALATED_COMMITS", 5)

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert "ESCALATED" in captured.out
    assert "6 commit" in captured.out


# ---------------------------------------------------------------------------
# 7. Days-since-last-promotion alone crosses the threshold, even with a
#    trivially small commit count — sourced from the merge-base date, no
#    promotion record exists, and that fallback is named plainly.
# ---------------------------------------------------------------------------


def test_days_since_threshold_via_merge_base_date_fallback(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    old_date = "2026-07-01T00:00:00+00:00"  # well over 14 days before "today"
    _git(
        "commit", "--allow-empty", "-m", "old base", cwd=dest,
        env=_env_with_dates(old_date),
    )
    main_sha = _git_out("rev-parse", "HEAD", cwd=dest)
    _set_origin_main_ref(dest, main_sha)
    _git("checkout", "-b", "candidate", cwd=dest)
    _commit_empty(dest, "single small commit")  # commit count stays trivial

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))
    # Real thresholds — the days axis alone must trip this, unaided by commits.
    assert publish._CANDIDATE_DIVERGENCE_ADVISORY_COMMITS == 300

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert "advisory" in captured.out
    assert "no promotion record exists" in captured.out
    assert "1 commit" in captured.out


# ---------------------------------------------------------------------------
# 7b. Days-only trigger with 0 commits ahead — a stationary candidate has
#     nothing to promote, so the days axis alone must NOT fire (Review:
#     E-divergence-report finding 1 — a "drops the delta from 0 to 0" nag).
# ---------------------------------------------------------------------------


def test_days_threshold_alone_is_silent_when_zero_commits_ahead(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    old_date = "2026-07-01T00:00:00+00:00"  # well over 14 days before "today"
    _git(
        "commit", "--allow-empty", "-m", "old base", cwd=dest,
        env=_env_with_dates(old_date),
    )
    main_sha = _git_out("rev-parse", "HEAD", cwd=dest)
    _set_origin_main_ref(dest, main_sha)
    _git("checkout", "-b", "candidate", cwd=dest)  # candidate == origin/main, 0 ahead

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert captured.out == ""


def _env_with_dates(iso_date: str) -> dict:
    import os

    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = iso_date
    env["GIT_COMMITTER_DATE"] = iso_date
    return env


# ---------------------------------------------------------------------------
# 8. Never raises — a publish must never fail on this report's account, not
#    even when the underlying git call blows up unexpectedly.
# ---------------------------------------------------------------------------


def test_never_raises_even_when_git_call_explodes(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    main_sha = _git_out("rev-parse", "HEAD", cwd=dest)
    _set_origin_main_ref(dest, main_sha)
    _git("checkout", "-b", "candidate", cwd=dest)
    _commit_empty(dest, "one commit ahead")

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated git failure")

    monkeypatch.setattr(publish, "_git_rev_list_count", _boom)

    publish.report_candidate_divergence(dest, out=sys.stdout)  # must not raise
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# 9. Git error / not-a-git-repo dest — degrades to a quiet skip.
# ---------------------------------------------------------------------------


def test_non_git_dest_is_a_quiet_skip(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "not-a-repo"
    dest.mkdir()
    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    publish.report_candidate_divergence(dest, out=sys.stdout)
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == ""
