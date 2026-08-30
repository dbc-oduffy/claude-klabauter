"""
coordinator_core.ops.tests.test_handoff_stamp_targeted

Tests for `coordinator_core.ops.handoff_stamp_targeted.ship_stamp_only` — the
targeted, single-lock-hold composition of `handoff.archive_transition`'s
`mode="stamp_only"`, without that op's per-call `housekeeping.cycle` fan-in.

Coverage:
  (a) insert            — stamps shipped_in + flips deployment_state:shipped,
                          pickup_ready:false, in ONE write.
  (b) idempotent-reship  — shipped_in already present, no --sha; stamp_only
                          proceeds and re-ships cleanly (warning + stamped=False).
  (c) Position A refusal — no --sha, no prior shipped_in: refuses the flip,
                          exit_code 1, deployment_state left untouched.
  (d) AC6 refusal        — --sha supplied that does not canonically match an
                          already-present shipped_in: refused loudly.
  (e) AC6b no-op         — --sha supplied that DOES canonically match the
                          (8-char-truncated) prior value: proceeds, not refused.
  (f) force replace      — force=True + sha replaces an existing shipped_in.
  (g) containment escape — handoff_path outside state/handoffs/: usage error.
  (h) missing handoff_path / repo_root — usage/setup errors.
  (i) force without sha  — usage error.
  (j) kind without sha   — usage error.
  (k) SHA-quoting parity — all-numeric and scientific-notation SHAs are
                          single-quoted on write (build_stamp_mutate reused,
                          not re-derived).
  (l) BUDGET (this chunk's own, per plan C2 body): <=2ms process time, 0
      subprocess spawns, warm (module imported at collection time, before
      any timing loop runs).

Spec backlink: coordinator_core/ops/handoff_stamp_targeted.py
Governing plan: docs/plans/2026-08-30-the-stamp-stops-paying-for-a-sweep-that.md, C2
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.ops.handoff_stamp_targeted import (
    chain_archive_handoff,
    ship_stamp_only,
    supersede_archive_handoff,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True, check=True)


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "stamp-targeted-test@claude-klabauter.test")
    _git(repo, "config", "user.name", "Stamp Targeted Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: initial skeleton")
    return repo


def _seed_handoff(repo: Path, name: str, extra_fm: str = "") -> Path:
    """Schema-valid, pre-cutoff (created: 2026-01-01) handoff — avoids the
    post-2026-05-29 category/summary/shipped_in-required cross-field rules
    so the fixture stays minimal (mirrors coordinator_core/test_archive_stamp.py
    :: _seed_handoff)."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'claimed_at: "2026-01-01T00:00:00Z"\n'
        "claimed_by: test-session-id\n"
        'predecessor: "none"\n'
        f"{extra_fm}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) Insert — stamp + ship in one write
# ---------------------------------------------------------------------------


def test_insert_stamps_and_ships(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-02-a.md")

    result = ship_stamp_only(
        str(hpath), repo / ".git", sha="abc123def456", kind="ship-commit"
    )

    assert result["exit_code"] == 0, result
    assert result["mode"] == "stamp_only"
    assert result["stamped"] is True
    assert result["superseded"] is False
    assert result["retained"] is False
    assert result["moved"] is False
    assert "retain_kind" not in result
    assert "error" not in result

    text = hpath.read_text(encoding="utf-8")
    assert "shipped_in: abc123de" in text
    assert "shipped_in_kind: ship-commit" in text
    assert "deployment_state: shipped" in text
    assert "pickup_ready: false" in text


# ---------------------------------------------------------------------------
# (b) Idempotent re-ship — shipped_in already present, no --sha
# ---------------------------------------------------------------------------


def test_reship_with_existing_shipped_in_no_sha(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(
        repo, "2026-01-02-b.md",
        extra_fm="shipped_in: deadbeef\nshipped_in_kind: ship-commit\n",
    )

    result = ship_stamp_only(str(hpath), repo / ".git")

    assert result["exit_code"] == 0, result
    assert result["stamped"] is False
    assert any("retained prior value" in w for w in result["warnings"])
    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: shipped" in text
    assert "shipped_in: deadbeef" in text


# ---------------------------------------------------------------------------
# (c) Position A refusal — no --sha, no prior shipped_in
# ---------------------------------------------------------------------------


def test_position_a_refuses_when_shipped_in_unresolvable(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-02-c.md")

    result = ship_stamp_only(str(hpath), repo / ".git")

    assert result["exit_code"] == 1, result
    assert "refusing to flip deployment_state:shipped" in result["error"]
    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: shipped" not in text
    assert "shipped_in" not in text


# ---------------------------------------------------------------------------
# (d)/(e) AC6/AC6b — supplied sha vs already-present shipped_in
# ---------------------------------------------------------------------------


def test_ac6_refuses_discarding_mismatched_sha(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(
        repo, "2026-01-02-d.md",
        extra_fm="shipped_in: 11111111\nshipped_in_kind: ship-commit\n",
    )

    result = ship_stamp_only(str(hpath), repo / ".git", sha="22222222222222222222")

    assert result["exit_code"] == 1, result
    assert "refusing to discard supplied --sha" in result["error"]
    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: shipped" not in text
    assert "shipped_in: 11111111" in text


def test_ac6b_same_commit_restamp_is_not_a_refusal(tmp_path):
    repo = _make_git_repo(tmp_path)
    # prior value is the 8-char truncated form of the full sha below.
    hpath = _seed_handoff(
        repo, "2026-01-02-e.md",
        extra_fm="shipped_in: abc12345\nshipped_in_kind: ship-commit\n",
    )

    result = ship_stamp_only(
        str(hpath), repo / ".git", sha="abc12345def000000000", kind="ship-commit"
    )

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert "deployment_state: shipped" in text


# ---------------------------------------------------------------------------
# (f) force replace
# ---------------------------------------------------------------------------


def test_force_replaces_existing_shipped_in(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(
        repo, "2026-01-02-f.md",
        extra_fm="shipped_in: 11111111\nshipped_in_kind: ship-commit\n",
    )

    result = ship_stamp_only(
        str(hpath),
        repo / ".git",
        sha="99999999999999999999",
        kind="ship-commit",
        force=True,
    )

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert "shipped_in: '99999999'" in text or 'shipped_in: "99999999"' in text
    assert "deployment_state: shipped" in text


# ---------------------------------------------------------------------------
# (g) containment escape
# ---------------------------------------------------------------------------


def test_containment_escape_is_usage_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    outside = repo / "archive" / "handoffs" / "2026-01" / "escaped.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")

    result = ship_stamp_only(str(outside), repo / ".git", sha="abc123def456")

    assert result["exit_code"] == 2, result
    assert "escapes state/handoffs/" in result["error"]


# ---------------------------------------------------------------------------
# (h)/(i)/(j) — usage/setup errors
# ---------------------------------------------------------------------------


def test_missing_handoff_path_is_usage_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = ship_stamp_only("", repo / ".git")
    assert result["exit_code"] == 2


def test_missing_repo_root_is_setup_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-02-g.md")
    result = ship_stamp_only(str(hpath), None)
    assert result["exit_code"] == 1


def test_force_without_sha_is_usage_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-02-h.md")
    result = ship_stamp_only(str(hpath), repo / ".git", force=True)
    assert result["exit_code"] == 2
    assert "'force' requires 'sha'" in result["error"]


def test_kind_without_sha_is_usage_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-02-i.md")
    result = ship_stamp_only(str(hpath), repo / ".git", kind="successor")
    assert result["exit_code"] == 2
    assert "'kind' requires 'sha'" in result["error"]


# ---------------------------------------------------------------------------
# (k) SHA-quoting parity (build_stamp_mutate reused, not re-derived)
# ---------------------------------------------------------------------------


def test_sha_quoting_all_numeric(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-02-j.md")

    result = ship_stamp_only(
        str(hpath), repo / ".git", sha="274671833", kind="ship-commit"
    )

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert "shipped_in: '27467183'" in text or 'shipped_in: "27467183"' in text


def test_sha_quoting_scientific_notation(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-02-k.md")

    result = ship_stamp_only(
        str(hpath), repo / ".git", sha="1958e194", kind="ship-commit"
    )

    assert result["exit_code"] == 0, result
    text = hpath.read_text(encoding="utf-8")
    assert "shipped_in: '1958e194'" in text or 'shipped_in: "1958e194"' in text


# ---------------------------------------------------------------------------
# (l) BUDGET — <=2ms process time, 0 spawns, warm
# ---------------------------------------------------------------------------


def test_budget_zero_spawns_and_under_2ms_warm(tmp_path, monkeypatch):
    """C2's own budget assertion (governing plan's prime exit criterion, part
    a): <=2ms process time, 0 git spawns, warm — module already imported at
    collection time above, so this call pays no first-import cost. Repo/
    fixture setup (git init/commit) happens BEFORE the spawn counter and
    timer are installed, matching the falsifier's own "measured warm"
    convention (docs/plans/2026-08-30-the-stamp-stops-paying-for-a-sweep-
    that.falsifier.py)."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-02-l.md")
    repo_root = repo / ".git"

    calls = {"run": 0, "popen": 0}
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def _counting_run(*args, **kwargs):
        calls["run"] += 1
        return real_run(*args, **kwargs)

    def _counting_popen(*args, **kwargs):
        calls["popen"] += 1
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    monkeypatch.setattr(subprocess, "Popen", _counting_popen)

    start = time.process_time()
    result = ship_stamp_only(str(hpath), repo_root, sha="abc123def456", kind="ship-commit")
    elapsed_ms = (time.process_time() - start) * 1000.0

    assert result["exit_code"] == 0, result
    assert calls["run"] == 0, f"expected 0 subprocess.run spawns, got {calls['run']}"
    assert calls["popen"] == 0, f"expected 0 subprocess.Popen spawns, got {calls['popen']}"
    assert elapsed_ms <= 2.0, (
        f"ship_stamp_only exceeded the 2ms warm process-time budget: {elapsed_ms:.4f}ms"
    )


# ---------------------------------------------------------------------------
# C3 — chain / supersede: move without commit
# (docs/plans/2026-08-30-the-stamp-stops-paying-for-a-sweep-that.md chunk C3)
# ---------------------------------------------------------------------------


def _git_status_clean(repo: Path) -> bool:
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return out.stdout.strip() == ""


# --- chain -------------------------------------------------------------


def test_chain_refuses_non_terminal_deployment_state(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-03-a.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add handoff")

    result = asyncio.run(chain_archive_handoff(str(hpath), repo / ".git"))

    assert result["exit_code"] == 1, result
    assert result["mode"] == "chain"
    assert result["moved"] is False
    assert "not terminal" in result["error"]
    assert hpath.is_file()


def test_chain_moves_and_commits_a_terminal_handoff(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(
        repo, "2026-01-03-b.md",
        extra_fm="deployment_state: shipped\nshipped_in: deadbeef\npickup_ready: false\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add handoff")

    result = asyncio.run(chain_archive_handoff(str(hpath), repo / ".git"))

    assert result["exit_code"] == 0, result
    assert result["mode"] == "chain"
    assert result["moved"] is True
    assert not hpath.exists()
    dest = repo / "archive" / "handoffs" / "2026-01" / "2026-01-03-b.md"
    assert dest.is_file()
    assert _git_status_clean(repo)


def test_chain_containment_escape_is_usage_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    outside = repo / "archive" / "handoffs" / "2026-01" / "escaped.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")

    result = asyncio.run(chain_archive_handoff(str(outside), repo / ".git"))

    assert result["exit_code"] == 2, result
    assert "escapes state/handoffs/" in result["error"]


def test_chain_missing_repo_root_is_setup_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-03-c.md")
    result = asyncio.run(chain_archive_handoff(str(hpath), None))
    assert result["exit_code"] == 1


# --- supersede -----------------------------------------------------------


def test_supersede_requires_continued_into(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-03-d.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add handoff")

    result = asyncio.run(
        supersede_archive_handoff(str(hpath), repo / ".git", continued_into="")
    )

    assert result["exit_code"] == 2, result
    assert result["mode"] == "supersede"
    assert "continued_into" in result["error"]


def test_supersede_flips_status_and_moves(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(repo, "2026-01-03-e.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add handoff")

    result = asyncio.run(
        supersede_archive_handoff(
            str(hpath), repo / ".git", continued_into="2026-01-03-successor.md"
        )
    )

    assert result["exit_code"] == 0, result
    assert result["mode"] == "supersede"
    assert result["superseded"] is True
    assert result["retained"] is False
    assert result["moved"] is True
    dest = repo / "archive" / "handoffs" / "2026-01" / "2026-01-03-e.md"
    assert dest.is_file()
    assert not hpath.exists()
    text = dest.read_text(encoding="utf-8")
    assert "status: claimed" in text
    assert "deployment_state: continued" in text
    assert "continued_into: 2026-01-03-successor.md" in text
    assert "pickup_ready: false" in text
    assert _git_status_clean(repo)


def test_supersede_refuses_a_closed_predecessor(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_handoff(
        repo, "2026-01-03-f.md",
        extra_fm="deployment_state: closed\nclosed_reason: cancelled\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add handoff")

    result = asyncio.run(
        supersede_archive_handoff(
            str(hpath), repo / ".git", continued_into="2026-01-03-successor.md"
        )
    )

    assert result["exit_code"] == 1, result
    assert "deployment_state: closed" in result["error"]
    assert result["superseded"] is False


def test_supersede_refuses_never_claimed_or_shipped(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = repo / "state" / "handoffs" / "2026-01-03-g.md"
    hpath.write_text(
        "---\n"
        'title: "Unclaimed"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "---\n\nbody\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add handoff")

    result = asyncio.run(
        supersede_archive_handoff(
            str(hpath), repo / ".git", continued_into="2026-01-03-successor.md"
        )
    )

    assert result["exit_code"] == 1, result
    assert "was never claimed or shipped" in result["error"]


def test_supersede_containment_admits_already_archived_and_stamps_in_place(tmp_path):
    repo = _make_git_repo(tmp_path)
    archived = repo / "archive" / "handoffs" / "2026-01" / "2026-01-03-h.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        "---\n"
        'title: "Already archived"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'claimed_at: "2026-01-01T00:00:00Z"\n'
        "claimed_by: test-session-id\n"
        'predecessor: "none"\n'
        "---\n\nbody\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add archived handoff")

    result = asyncio.run(
        supersede_archive_handoff(
            str(archived), repo / ".git", continued_into="2026-01-03-successor.md"
        )
    )

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True
    assert result["moved"] is False
    assert "already archived" in result["message"]
    text = archived.read_text(encoding="utf-8")
    assert "deployment_state: continued" in text
