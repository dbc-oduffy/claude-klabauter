"""
coordinator_core.execute_plan_assemble.tests.test_stamp_refuses_contradiction
— coverage for the C14 contradiction-gate wiring in `close_out_and_stamp`
(Item 29, docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md
§ C14): `coordinator/bin/lib/plan_completeness.py::compute_contradiction`
is called in-process, on the branch that would otherwise stamp
`implemented`, and a fired contradiction downgrades `status_target` to
`None` (no stamp at all) rather than to `landed` -- same posture as the
sibling goal-falsifier gate (`test_close_out_goal_refusal.py`).

Row shapes exercised (per the row's own body: "an all-spun_off plan and a
plan with an unreported chunk are both refused; a clean plan stamps"):

  - all-`spun_off` plan: the sole row is resolved (not `open`, so the
    pre-existing `rows_resolved < rows_total` gate does NOT fire) but no
    row is `coded` -- `rows_coded == 0` is the NEW leg this chunk adds.
  - a plan with an unreported chunk: read here as a `backlogged` row
    (a chunk that was never coded/reported, resolved by being shelved
    rather than delivered) -- resolved the same way, `rows_coded == 0`
    the same way. `compute_contradiction`'s sibling `chunks_reported <
    chunks_total` leg (its `"landed"`-claim branch) is out of reach of
    this integration by construction: the call site below passes
    `chunks_total=None`/`chunks_reported=0` (the reader's own UNKNOWN
    values) rather than re-deriving emitted-workflow chunk counts, so
    that leg never fires here -- see close_out_and_stamp.py's own
    comment at the call site for why.
  - a clean plan (one `coded` row, disposition_ref a real ancestor sha):
    no contradiction, ships `implemented` as before.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Git/repo test-isolation helpers (mirrors test_close_out_and_stamp.py's own
# and test_close_out_goal_refusal.py's own -- a fresh `git init`-ed
# `tmp_path`, never this repo's own working tree).
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _init_repo(root: Path) -> None:
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "t@t"], root)
    _run_git(["config", "user.name", "test"], root)


def _head_sha(root: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], root).stdout.strip()


def _run_close_out(root: Path, plan_rel: str, *, dry_run: bool = True) -> tuple[int, dict]:
    return coas.close_out_and_stamp(plan_rel, repo_root=root, dry_run=dry_run)


_PLAN_TEMPLATE = """---
title: "Fixture plan — contradiction gate"
created: 2026-09-26
author: test-fixture
status: executing
branch: "work/test-fixture/2026-09-26"
plan_id: "pln-fixture-contradiction-gate-000001"
deliverable_id: "dlv-fixture-contradiction-gate-000001"
---

# Fixture plan — contradiction gate

## Tasks

```yaml plan-tasks
{rows}
```
"""

_SPUN_OFF_ROW = (
    "- id: C1\n"
    "  title: Only chunk\n"
    "  change_kind: code-edit\n"
    "  surface: fixture.py\n"
    "  disposition: spun_off\n"
    "  disposition_detail: 'moved to a follow-up plan'\n"
    "  body: |\n"
    "    The only chunk.\n"
)

_BACKLOGGED_ROW = (
    "- id: C1\n"
    "  title: Only chunk\n"
    "  change_kind: code-edit\n"
    "  surface: fixture.py\n"
    "  disposition: backlogged\n"
    "  disposition_detail: 'shelved, never coded'\n"
    "  body: |\n"
    "    The only chunk.\n"
)

_CODED_ROW = (
    "- id: C1\n"
    "  title: Only chunk\n"
    "  change_kind: code-edit\n"
    "  surface: fixture.py\n"
    "  disposition: coded\n"
    "  disposition_ref: {sha}\n"
    "  disposition_detail: 'landed'\n"
    "  body: |\n"
    "    The only chunk.\n"
)


def _seed_plan(root: Path, rows: str, dest_name: str = "plan.md") -> Path:
    text = _PLAN_TEMPLATE.format(rows=rows)
    dest = root / dest_name
    dest.write_text(text, encoding="utf-8")
    _run_git(["add", dest_name], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return dest


def _land_the_shipping_chunk(root: Path) -> str:
    """Lands a real commit this repo's own history can verify as an
    ancestor of `HEAD` -- the sha the clean plan's `disposition_ref`
    (spine evidence) points at."""
    (root / "fixture.py").write_text("v1", encoding="utf-8")
    _run_git(["add", "fixture.py"], root)
    _run_git(["commit", "-q", "-m", "land the only chunk"], root)
    return _head_sha(root)


class TestContradictionGateRefusesTheStamp:
    def test_all_spun_off_plan_is_refused(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _SPUN_OFF_ROW)

        exit_code, result = _run_close_out(root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["status_target"] is None
        assert result["stamped"] is False
        assert result["open_chunk_ids"] == []  # the pre-existing gate does NOT fire
        assert result["contradiction_gate"] == {
            "claim": "implemented",
            "reason": "rows_coded == 0",
        }
        assert "computed CONTRADICTION" in result["message"]

    def test_unreported_backlogged_chunk_plan_is_refused(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _BACKLOGGED_ROW)

        exit_code, result = _run_close_out(root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["status_target"] is None
        assert result["stamped"] is False
        assert result["open_chunk_ids"] == []
        assert result["contradiction_gate"] == {
            "claim": "implemented",
            "reason": "rows_coded == 0",
        }

    def test_clean_plan_still_stamps_implemented(self, tmp_path):
        root = tmp_path
        _init_repo(root)
        _seed_plan(root, _CODED_ROW.format(sha="0" * 40))
        sha = _land_the_shipping_chunk(root)
        # Rewrite the row's `disposition_ref` to the real landed sha, same
        # two-step "seed placeholder, then land, then point at the real
        # sha" sequencing `test_close_out_and_stamp.py` uses.
        plan_path = root / "plan.md"
        plan_path.write_text(
            _PLAN_TEMPLATE.format(rows=_CODED_ROW.format(sha=sha)), encoding="utf-8"
        )
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "point disposition_ref at the real sha"], root)

        exit_code, result = _run_close_out(root, "plan.md")

        assert exit_code == coas.EXIT_OK
        assert result["status_target"] == "implemented"
        assert "contradiction_gate" not in result
