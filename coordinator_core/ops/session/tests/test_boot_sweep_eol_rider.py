"""
coordinator_core.ops.session.tests.test_boot_sweep_eol_rider

Purpose: C8 (docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md
§ C8) — the two cadence-gated riders inside `session.boot_sweep` that fire
`eol.census` and `eol.audit_producers` against the booting repo's own
worktree, each on its own marker/window, each degrading independently to a
`warnings[]` entry on any failure without ever changing the composite's
`exit_code`.

Coverage:
  (a) `_claim_cadence_slot` generalization — two distinct marker names never
      contend: claiming one leaves the other's slot open.
  (b) `_write_eol_sentinel` — writes a JSON verdict sentinel inside
      `common_dir` (never `state/`), best-effort silent on I/O failure.
  (c) `_handler` full pass — first boot on a fresh repo fires BOTH riders
      (`eol_census`/`eol_audit_producers` result dicts both `ran: True`),
      writes both cadence markers AND both verdict sentinels inside
      `common_dir`, and never touches `state/` for either sentinel.
  (d) Cadence gate — a second `_handler` call inside the same window does
      NOT re-fire either rider (`ran: False`, `reason: "within cadence
      window"`); `exit_code` stays 0.
  (e) Degrade-safe — an injected `eol.census` exception folds into
      `warnings[]` with `scope: "eol_census"`, `eol_census["reason"] ==
      "error"`, and never changes `exit_code` (0, no per-item failure) nor
      raises out of `_handler`. Same shape independently for
      `eol.audit_producers`/`scope: "eol_audit_producers"`.
  (f) Negative-spec — `eol.repair` is never imported/called by this module
      (grep-shaped source assertion): the rider dispatches census/audit
      only, per C8's negative spec.

Spec backlinks:
  - Plan chunk C8: docs/plans/2026-08-20-every-repo-detects-its-own-eol-
    drift.md § C8.
  - AC8/AC14: cadence-gated riders, read-only, degrade-safe, own markers.

Negative-spec:
  - Does NOT re-test `eol.census`/`eol.audit_producers`'s own internal
    behavior (bidirectional drift detection, AST offender walk) — that is
    `coordinator_core/ops/eol/tests/test_census.py` and
    `test_audit_producers.py`'s remit; this file only tests the rider
    wiring inside `boot_sweep`.
  - Does NOT exercise the other nine sweeps in `session.boot_sweep` — see
    `test_boot_sweep.py` for that coverage.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import coordinator_core.ops  # noqa: F401 — ops/__init__.py triggers all op registrations
import coordinator_core.ops.session.boot_sweep  # noqa: F401 — fires @register_op("session.boot_sweep")

# Real git spawn is load-bearing: eol.census itself spawns real git
# (ls-files/check-attr/status) against the fixture repo.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.ops.session import boot_sweep
from coordinator_core.ops.session.boot_sweep import _handler


def _run(coro):
    return asyncio.run(coro)


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _common_dir(root: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    return Path(result.stdout.decode().strip()).resolve()


@pytest.fixture
def repo(tmp_path) -> Path:
    return _init_repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# (a) _claim_cadence_slot: independent markers
# ---------------------------------------------------------------------------


def test_distinct_markers_do_not_contend(repo):
    common = _common_dir(repo)
    now = 1_000_000.0

    won_census = boot_sweep._claim_cadence_slot(
        common, boot_sweep._EOL_CENSUS_MARKER_NAME, boot_sweep._EOL_CENSUS_CADENCE_WINDOW_SECONDS, now=now
    )
    won_audit = boot_sweep._claim_cadence_slot(
        common,
        boot_sweep._EOL_AUDIT_PRODUCERS_MARKER_NAME,
        boot_sweep._EOL_AUDIT_PRODUCERS_CADENCE_WINDOW_SECONDS,
        now=now,
    )

    assert won_census is True
    assert won_audit is True
    assert (common / boot_sweep._EOL_CENSUS_MARKER_NAME).is_file()
    assert (common / boot_sweep._EOL_AUDIT_PRODUCERS_MARKER_NAME).is_file()

    # Immediately re-claiming either, still within its own window, loses.
    assert boot_sweep._claim_cadence_slot(
        common, boot_sweep._EOL_CENSUS_MARKER_NAME, boot_sweep._EOL_CENSUS_CADENCE_WINDOW_SECONDS, now=now
    ) is False
    assert boot_sweep._claim_cadence_slot(
        common,
        boot_sweep._EOL_AUDIT_PRODUCERS_MARKER_NAME,
        boot_sweep._EOL_AUDIT_PRODUCERS_CADENCE_WINDOW_SECONDS,
        now=now,
    ) is False


# ---------------------------------------------------------------------------
# (b) _write_eol_sentinel
# ---------------------------------------------------------------------------


def test_write_eol_sentinel_lands_in_common_dir_not_state(repo):
    common = _common_dir(repo)
    verdict = {"violations": [], "violation_count": 0}

    boot_sweep._write_eol_sentinel(common, boot_sweep._EOL_CENSUS_SENTINEL_NAME, verdict)

    sentinel_path = common / boot_sweep._EOL_CENSUS_SENTINEL_NAME
    assert sentinel_path.is_file()
    payload = json.loads(sentinel_path.read_text(encoding="utf-8"))
    assert payload["verdict"] == verdict
    assert "written_at" in payload
    assert not (repo / "state" / boot_sweep._EOL_CENSUS_SENTINEL_NAME).exists()


def test_write_eol_sentinel_silent_on_io_error(tmp_path):
    # A common_dir that does not exist -- write_text raises OSError, swallowed.
    missing = tmp_path / "does-not-exist"
    boot_sweep._write_eol_sentinel(missing, boot_sweep._EOL_CENSUS_SENTINEL_NAME, {"x": 1})
    assert not missing.exists()


# ---------------------------------------------------------------------------
# (c) full _handler pass — both riders fire on first boot
# ---------------------------------------------------------------------------


def test_first_boot_fires_both_eol_riders(repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "eol-rider-test")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    result = _run(_handler({}, repo_root=_common_dir(repo)))

    assert result["exit_code"] == 0
    assert result["eol_census"]["ran"] is True
    assert result["eol_census"]["reason"] == "cadence window elapsed"
    assert result["eol_census"]["result"] is not None
    assert result["eol_audit_producers"]["ran"] is True
    assert result["eol_audit_producers"]["reason"] == "cadence window elapsed"
    assert result["eol_audit_producers"]["result"] is not None

    common = _common_dir(repo)
    assert (common / boot_sweep._EOL_CENSUS_MARKER_NAME).is_file()
    assert (common / boot_sweep._EOL_AUDIT_PRODUCERS_MARKER_NAME).is_file()
    assert (common / boot_sweep._EOL_CENSUS_SENTINEL_NAME).is_file()
    assert (common / boot_sweep._EOL_AUDIT_PRODUCERS_SENTINEL_NAME).is_file()
    assert not (repo / "state" / boot_sweep._EOL_CENSUS_SENTINEL_NAME).exists()
    assert not (repo / "state" / boot_sweep._EOL_AUDIT_PRODUCERS_SENTINEL_NAME).exists()


# ---------------------------------------------------------------------------
# (d) cadence gate — second boot in-window does not re-fire
# ---------------------------------------------------------------------------


def test_second_boot_within_window_skips_both_riders(repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "eol-rider-test-2")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    first = _run(_handler({}, repo_root=_common_dir(repo)))
    assert first["eol_census"]["ran"] is True
    assert first["eol_audit_producers"]["ran"] is True

    second = _run(_handler({}, repo_root=_common_dir(repo)))
    assert second["exit_code"] == 0
    assert second["eol_census"] == {"ran": False, "reason": "within cadence window", "result": None}
    assert second["eol_audit_producers"] == {
        "ran": False, "reason": "within cadence window", "result": None,
    }


# ---------------------------------------------------------------------------
# (e) degrade-safe on injected failures
# ---------------------------------------------------------------------------


def test_eol_census_failure_degrades_to_warning(repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "eol-rider-test-3")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with patch(
        "coordinator_core.ops.session.boot_sweep._eol_census",
        side_effect=RuntimeError("boom"),
    ):
        result = _run(_handler({}, repo_root=_common_dir(repo)))

    assert result["exit_code"] == 0
    assert result["eol_census"] == {"ran": True, "reason": "error", "result": None}
    scopes = {w.get("scope") for w in result["warnings"]}
    assert "eol_census" in scopes
    # audit_producers is unaffected by the census failure.
    assert result["eol_audit_producers"]["ran"] is True


def test_eol_audit_producers_failure_degrades_to_warning(repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "eol-rider-test-4")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with patch(
        "coordinator_core.ops.session.boot_sweep._eol_audit_producers",
        side_effect=RuntimeError("boom"),
    ):
        result = _run(_handler({}, repo_root=_common_dir(repo)))

    assert result["exit_code"] == 0
    assert result["eol_audit_producers"] == {"ran": True, "reason": "error", "result": None}
    scopes = {w.get("scope") for w in result["warnings"]}
    assert "eol_audit_producers" in scopes
    assert result["eol_census"]["ran"] is True


# ---------------------------------------------------------------------------
# (f) negative-spec — never dispatches eol.repair
# ---------------------------------------------------------------------------


def test_boot_sweep_never_imports_eol_repair():
    src = Path(boot_sweep.__file__).read_text(encoding="utf-8")
    assert "coordinator_core.ops.eol.repair" not in src
    assert "import repair" not in src
    assert "_eol_repair" not in src
    assert "eol.repair(" not in src
