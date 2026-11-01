"""land_run: the sibling "freed" note and the withhold read one decision.

The incident shape: a halted spine-only sibling withholds a path whose kept hunks a hand commit already put
at HEAD; the landing run declares the same path and carries further hunks of its own. The landing run
commits the path, and nothing names the sibling as its holder. A sibling whose kept hunks are only in the
worktree still withholds the path.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, sibling_claims
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SRC = "src/a.py"
STILL_HELD = "src/b.py"
_N = 600


def _lines(**edits: str) -> str:
    """A `_N`-line file; `edits` maps a line index (`l5`) to its replacement text."""
    return "".join(f"{edits.get(f'l{i}', f'x{i} = 0')}\n" for i in range(_N))


# The kept hunks sit near both ends of a file far longer than the diff bound apart; the hand commit that put
# them at HEAD also carried a peer's hunk between them, so HEAD is not blob-equal to the kept bytes.
BASE = _lines()
KEPT = _lines(l5="x5 = 1", l590="x590 = 1")
HAND = _lines(l5="x5 = 1", l300="x300 = 2", l590="x590 = 1")
OWN = _lines(l5="x5 = 1", l300="x300 = 2", l590="x590 = 1", l100="x100 = 3")


def _halted_spine_only(repo: Path, kept_in_head: bool) -> str:
    """Halted sibling with no manifest: its plan spine declares SRC and its halt record withholds it."""
    prod._write(repo, SRC, BASE)
    prod._write(repo, STILL_HELD, "b = 0\n")
    prod._git(["add", "."], repo)
    prod._git(["commit", "-q", "-m", "seed src"], repo)
    base = prod._git(["rev-parse", "HEAD"], repo).strip()
    blobs = [
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=text, capture_output=True, text=True,
            check=True, **no_console_creationflags(),
        ).stdout.strip()
        for text in (KEPT, "b = 1\n")
    ]
    if kept_in_head:
        prod._write(repo, SRC, HAND)
        prod._git(["commit", "-q", "-am", "hand commit of the kept hunks"], repo)
    prod._write(repo, STILL_HELD, "b = 1\n")  # the run's other withheld path stays dirty: the run is never released whole
    stamp = datetime.fromtimestamp(time.time() - 300, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_id = f"ask-{stamp}-f54544"
    rdir = f"state/warp/{run_id}"
    prod._write(repo, f"{rdir}/run.json", json.dumps({"sizing_rel": "state/sizings/sp.yaml"}))
    prod._write(repo, f"{rdir}/halted.json", json.dumps({
        "status": "halted", "run_base_sha": base, "sizing_rel": "state/sizings/sp.yaml",
        "run_artifacts": {"withheld": {SRC: "halt-record-only", STILL_HELD: "halt-record-only"}},
    }))
    prod._write(repo, f"{rdir}/kept.json", json.dumps({
        "chunks": {"X1": {"paths": {SRC: blobs[0], STILL_HELD: blobs[1]}}}, "plan_gate": None,
    }))
    prod._write(repo, "state/sizings/sp.yaml", 'plan: "docs/plans/sp.md"\n')
    prod._write(
        repo, "docs/plans/sp.md",
        "# P\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + SRC + "\n```\n",
    )
    return run_id


def _land_own_hunks(repo: Path) -> dict:
    prod._author(repo, SRC, OWN)
    prod._declare(repo, SRC)
    prod._record_dir(repo)
    return prod._land(repo)


def _withheld_names(out: dict) -> str:
    return json.dumps({"w": out.get("withheld"), "a": (out.get("run_artifacts") or {}).get("withheld")})


def test_kept_hunks_at_head_free_the_path_and_the_landing_run_commits_it(repo, caplog):
    sibling = _halted_spine_only(repo, kept_in_head=True)

    with caplog.at_level("WARNING", logger=sibling_claims.__name__):
        out = _land_own_hunks(repo)

    assert out["committed"] is True, out
    assert SRC in prod._tree(repo), out
    assert sibling not in _withheld_names(out), out
    assert land_run.foreign_halted_withheld(repo, prod.RUN, None, [SRC]) == {}


@pytest.mark.parametrize("kept_in_head", [True, False])
def test_the_freed_note_and_the_withhold_name_the_same_decision(repo, caplog, kept_in_head):
    sibling = _halted_spine_only(repo, kept_in_head=kept_in_head)
    prod._write(repo, SRC, OWN)

    with caplog.at_level("WARNING", logger=sibling_claims.__name__):
        sibling_claims.sibling_run_claims(repo, prod.RUN, [SRC], own_declared=[SRC])

    freed = any("freed for landing run" in r.getMessage() and SRC in r.getMessage() for r in caplog.records)
    withheld = land_run.foreign_halted_withheld(repo, prod.RUN, None, [SRC]) == {SRC: sibling}
    assert freed is kept_in_head and withheld is not kept_in_head, (freed, withheld)


def test_kept_hunks_only_in_the_worktree_still_withhold(repo):
    sibling = _halted_spine_only(repo, kept_in_head=False)
    prod._write(repo, SRC, KEPT)

    out = _land_own_hunks(repo)

    assert SRC not in prod._tree(repo), out
    assert land_run.foreign_halted_withheld(repo, prod.RUN, None, [SRC]) == {SRC: sibling}
    assert sibling in _withheld_names(out), out


def test_hunks_far_apart_match_alone_and_a_missing_one_still_holds():
    from coordinator_core.ops.dispatch_emit import halted_hold_release as hr

    assert hr._hunks_present(BASE, KEPT, HAND)
    assert not hr._hunks_present(BASE, KEPT, _lines(l5="x5 = 1", l300="x300 = 2"))
    assert not hr._hunks_present(BASE, KEPT, BASE)
