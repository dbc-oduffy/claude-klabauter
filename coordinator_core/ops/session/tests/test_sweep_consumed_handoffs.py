"""
coordinator_core.ops.session.tests.test_sweep_consumed_handoffs

Real-git-fixture tests for `session.sweep_consumed_handoffs` (K-047).

Mirrors the governed real-git-fixture pattern already used by
`coordinator_core/ops/fleet/tests/test_archive_terminal_handoffs.py` (this
op reuses that module's own `plan_sweep`/`_acquire_sweep_lock` verbatim, so
the same throwaway-repo-per-test convention applies here) — one throwaway
repo per test function, real handoff detritus committed to
`state/handoffs/`, never an empty tree.

Coverage:
  - a `shipped` handoff (the exact shape `consumed_handoff_stamp.py`'s
    stamp+ship follow-up commit just produced) is swept into
    `archive/handoffs/`, landing its own commit, and a receipt row records
    `"applied"`.
  - a second fire over the same corpus is a no-op (`"nothing-to-do"`), not
    a double-move — the idempotency the op's docstring claims.
  - a live-claimed, non-terminal handoff is retained (not archived) and
    contributes no receipt count.
  - an absent/non-positive `cap` is a setup error, never an unbounded
    default.
  - the failure path: `plan_sweep` raising is caught, reported as
    `exit_code: 1`, and recorded in the receipt as `"failed"` — the sweep
    never returns silently.
  - a contended lock is a first-class non-error skip, recorded as
    `"skipped-contended"`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet import _sweep_receipt
from coordinator_core.ops.fleet.archive_terminal_handoffs import _sweep_lock_path
from coordinator_core.ops.session.sweep_consumed_handoffs import _handler, _SWEEP_KEY
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors the
    # sibling fixture in fleet/tests/test_archive_terminal_handoffs.py.
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result


def _common_dir(repo: Path) -> Path:
    result = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(result.stdout.strip()).resolve()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _seed(repo: Path, name: str, fm_extra: str) -> Path:
    """Write + commit one live handoff under state/handoffs/ — real detritus,
    the same shape `consumed_handoff_stamp.py`'s follow-up commit leaves
    behind on disk once it flips `deployment_state` to a terminal value.
    """
    path = repo / "state" / "handoffs" / name
    _write(path, f'---\ntitle: "{name}"\ncreated: 2026-01-01\n{fm_extra}\n---\n\nBody.\n')
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return path


def _cid(name: str) -> str:
    return f"state/handoffs/{name}"


def _receipt_rows(common_dir: Path) -> list:
    path = _sweep_receipt.receipt_path(common_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init_repo(root)
    return root


def test_a_shipped_handoff_is_swept_and_receipted(repo: Path):
    name = "2026-01-01-shipped-consumed.md"
    parent = _seed(
        repo, name,
        'status: claimed\ndeployment_state: shipped\nshipped_in: ' + _git(repo, "rev-parse", "HEAD").stdout.strip(),
    )
    cid = _cid(name)
    common_dir = _common_dir(repo)

    result = _handler({"cap": 10}, repo_root=common_dir)

    acted_ids = [item["id"] for item in result.get("acted", [])]
    assert acted_ids == [cid], result
    assert not parent.exists(), "swept handoff must be gone from state/handoffs/"
    archived = list((repo / "archive" / "handoffs").rglob(name))
    assert len(archived) == 1, archived

    rows = _receipt_rows(common_dir)
    assert rows, "every exit path must record a receipt row (AC-3)"
    assert rows[-1]["sweep"] == _SWEEP_KEY
    assert rows[-1]["outcome"] == "applied"
    assert rows[-1]["count"] == 1


def test_a_second_fire_over_the_same_corpus_is_a_no_op(repo: Path):
    name = "2026-01-02-shipped-consumed-again.md"
    _seed(
        repo, name,
        'status: claimed\ndeployment_state: shipped\nshipped_in: ' + _git(repo, "rev-parse", "HEAD").stdout.strip(),
    )
    common_dir = _common_dir(repo)

    first = _handler({"cap": 10}, repo_root=common_dir)
    assert first.get("acted"), first

    archived_before = sorted((repo / "archive" / "handoffs").rglob(name))
    second = _handler({"cap": 10}, repo_root=common_dir)
    archived_after = sorted((repo / "archive" / "handoffs").rglob(name))

    assert second.get("acted") == [], (
        "a second fire over an already-swept corpus must be a no-op, "
        f"not a double-move; got {second!r}"
    )
    assert archived_before == archived_after

    rows = _receipt_rows(common_dir)
    assert rows[-1]["outcome"] == "nothing-to-do"


def test_a_live_claimed_non_terminal_handoff_is_retained(repo: Path):
    name = "2026-01-03-in-flight.md"
    handoff_path = _seed(repo, name, "status: claimed\ndeployment_state: in_flight")
    common_dir = _common_dir(repo)

    result = _handler({"cap": 10}, repo_root=common_dir)

    assert result.get("acted") == [], result
    assert handoff_path.exists(), "an in_flight handoff must never be swept"

    rows = _receipt_rows(common_dir)
    assert rows[-1]["outcome"] == "nothing-to-do"
    assert rows[-1]["count"] == 0


def test_absent_cap_is_a_setup_error_never_unbounded(repo: Path):
    common_dir = _common_dir(repo)
    result = _handler({}, repo_root=common_dir)
    assert result["exit_code"] == 1
    assert "cap" in result["error"]

    result = _handler({"cap": 0}, repo_root=common_dir)
    assert result["exit_code"] == 1

    result = _handler({"cap": -1}, repo_root=common_dir)
    assert result["exit_code"] == 1


def test_plan_sweep_failure_is_reported_and_receipted_never_silent(repo: Path):
    name = "2026-01-04-shipped-consumed.md"
    _seed(
        repo, name,
        'status: claimed\ndeployment_state: shipped\nshipped_in: ' + _git(repo, "rev-parse", "HEAD").stdout.strip(),
    )
    common_dir = _common_dir(repo)

    with patch(
        "coordinator_core.ops.session.sweep_consumed_handoffs.plan_sweep",
        side_effect=RuntimeError("boom"),
    ):
        result = _handler({"cap": 10}, repo_root=common_dir)

    assert result["exit_code"] == 1
    assert "boom" in result["error"]

    rows = _receipt_rows(common_dir)
    assert rows, "a failed sweep must still leave a receipt row (AC-3)"
    assert rows[-1]["outcome"] == "failed"
    assert "boom" in rows[-1].get("detail", "")


def test_contended_lock_is_a_first_class_skip_never_an_error(repo: Path):
    common_dir = _common_dir(repo)
    lock_path = _sweep_lock_path(common_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")  # a fresh (non-stale) lock

    try:
        result = _handler({"cap": 10}, repo_root=common_dir)
    finally:
        lock_path.unlink(missing_ok=True)

    assert result["exit_code"] == 0
    assert result.get("contended") is True

    rows = _receipt_rows(common_dir)
    assert rows[-1]["outcome"] == "skipped-contended"


def test_repo_root_none_is_a_setup_error() -> None:
    result = _handler({"cap": 10}, repo_root=None)
    assert result["exit_code"] == 1
    assert "repo_root" in result["error"]
