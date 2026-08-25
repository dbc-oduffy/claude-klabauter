"""
coordinator_core.execute_plan_assemble.tests.test_close_out_and_stamp_remedy_message
-- pins the `missing_chunk_ids` operator-facing message's remedy text.

Why this file exists: a consolidated commit (the correct call when
per-wave commit agents refuse) reports "N chunk(s) still uncommitted" for a
fully-delivered plan even though the sanctioned recovery -- `plan-tasks-
resolve --coded <sha>` per row, then `close-out-and-stamp` -- already
exists and is documented in `close_out_and_stamp.py`'s own docstring under
"Plan-side disposition_ref evidence". Nothing in the ceremony's OUTPUT
pointed at it. This file pins the remedy now being named in that message.

Message-only change (docs/plans/2026-08-20-wsc-identity-gates-key-on-the-
deliverable.md AC7): the discharge oracle's join/attribution logic is
untouched -- see `close_out_and_stamp.py`'s own module docstring for the
oracle's false-positive/false-negative history this task does not touch.

Register: `docs/wiki/guard-messaging.md` § Register -- one fact, one terse
imperative alternative, no reassurance, no override key.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.execute_plan_assemble import close_out_and_stamp as coas

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES_DIR = (
    _REPO_ROOT / "coordinator" / "bin" / "tests" / "fixtures" / "plan-tasks-spine"
)
_FIXTURE_VALID_SPINE = _FIXTURES_DIR / "valid-spine-with-deferrals.md"
_DLV_VALID_SPINE = "dlv-fixture-valid-spine-000001"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo(root: Path) -> None:
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "t@t"], root)
    _run_git(["config", "user.name", "test"], root)


def _seed_plan(root: Path, fixture_path: Path, dest_name: str = "plan.md") -> Path:
    dest = root / dest_name
    dest.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    _run_git(["add", dest_name], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


def _commit_chunk(
    root: Path,
    plan_rel: str,
    chunk_id: str,
    *,
    deliverable_id: Optional[str] = None,
) -> None:
    plan_file = root / plan_rel
    with plan_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- {chunk_id} landed -->\n")
    _run_git(["add", plan_rel], root)
    message_args = ["-m", f"{chunk_id}: land chunk"]
    if deliverable_id:
        message_args += ["-m", f"Deliverable-Id: {deliverable_id}"]
    _run_git(["commit", "-q", *message_args], root)


def _resolve_chunk_coded(root: Path, plan_rel: str, chunk_id: str) -> None:
    """Stamp `chunk_id`'s spine row `coded` against HEAD, the way
    `plan-tasks-resolve --coded` does.

    C3 (2026-08-21, "the close ceremony stops paying for the join") DELETED
    the commit-subject/`Deliverable-Id` join outright -- see
    `_determine_shipped`'s own docstring. Landing a commit whose subject
    leads with the chunk id therefore proves nothing to the oracle; the two
    surviving evidence paths are a `disposition: coded` row's own
    `disposition_ref` and a legacy Dispatch Ledger `committed <sha>` cell,
    both pure sha-ancestry checks. A fixture that only commits is a fixture
    the oracle correctly reports as missing.
    """
    sha = _run_git(["rev-parse", "HEAD"], root).stdout.strip()
    plan_file = root / plan_rel
    lines = plan_file.read_text(encoding="utf-8").splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.rstrip() == f"- id: {chunk_id}":
            lines.insert(idx + 1, "  disposition: coded\n")
            lines.insert(idx + 2, f"  disposition_ref: '{sha}'\n")
            break
    else:  # pragma: no cover -- fixture drift, not a runtime path
        raise AssertionError(f"{chunk_id} not found in {plan_rel}")
    plan_file.write_text("".join(lines), encoding="utf-8")
    _run_git(["add", plan_rel], root)
    _run_git(["commit", "-q", "-m", f"resolve {chunk_id} coded"], root)


def _run_close_out(
    monkeypatch: pytest.MonkeyPatch, root: Path, plan_rel: str
) -> tuple[int, dict]:
    monkeypatch.chdir(root)
    exit_code, result = coas.close_out_and_stamp(plan_rel, repo_root=root)
    return exit_code, result


class TestMissingChunkIdsRemedyMessage:
    def test_still_uncommitted_message_names_plan_tasks_resolve_coded(
        self, tmp_path, monkeypatch
    ):
        """The genuinely-uncommitted (`JOIN_PROVENANCE_JOINED`) branch's
        `message` names `plan-tasks-resolve --coded <sha>` as the remedy
        for a consolidated commit that never led with chunk ids -- AC7."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
        _resolve_chunk_coded(root, "plan.md", "C1")

        exit_code, result = _run_close_out(monkeypatch, root, "plan.md")

        assert exit_code == coas.EXIT_OK, result
        assert result["shipped"] is False
        assert sorted(result["missing_chunk_ids"]) == ["C2a", "C2b"]
        assert "plan-tasks-resolve --coded" in result["message"]
        assert "close-out-and-stamp" in result["message"]

    def test_remedy_message_stays_in_register(self, tmp_path, monkeypatch):
        """No banned move per `docs/wiki/guard-messaging.md` § Register --
        no self-legitimacy/reassurance wrapper, no repeated content-bearing
        claim, no override key named."""
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _FIXTURE_VALID_SPINE)
        _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)

        _exit_code, result = _run_close_out(monkeypatch, root, "plan.md")

        message = result["message"]
        assert message.count("still uncommitted") == 1
        for banned in ("no need to", "don't be alarmed", "harmless", "override"):
            assert banned not in message.lower()
