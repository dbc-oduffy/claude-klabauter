"""dispatch.terminal_commit on a mise run lands the records the run minted and every sourced plan's
seam sidecars, and leaves a foreign run's records untracked.

Fixture: the receipt test's inventory-mode repo plus a second sourced plan.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SPINE_REL = "state/mise-inventory/run.spine.md"
INVENTORY_REL = "state/mise-inventory/run.md"
PLAN2_REL = "docs/plan2.md"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    (repo / "state" / "mise-inventory").mkdir(parents=True)
    (repo / INVENTORY_REL).write_text(
        "---\nrun_id: run\n---\n\n## Chunk table\n\n"
        "| ID | Spec path | Summary | Footprint | Deps | Verification | Complexity | Disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| P1 | `{driver.PLAN_REL}` | s | `a.py` | - | v | S | pending |\n"
        f"| P2 | `{PLAN2_REL}` | s | `z.py` | - | v | S | pending |\n",
        encoding="utf-8",
    )
    (repo / SPINE_REL).write_text(
        "---\nrun_id: run\nderived_from: mise inventory record\n---\n\n## Tasks\n", encoding="utf-8"
    )
    (repo / "docs").mkdir(exist_ok=True)
    (repo / PLAN2_REL).write_text("# plan two\n", encoding="utf-8")
    plan_seam = Path(driver.PLAN_REL).with_suffix(".seam.yaml").as_posix()
    (repo / plan_seam).write_text("v: 1\n", encoding="utf-8")
    _git(repo, "add", "state/mise-inventory", "docs")
    _git(repo, "commit", "-q", "-m", "inventory")
    request = CommitRequest(chunks=(ChunkCommit(id="P1.C3", title="t3", paths=("a.py",)),), plan_path=SPINE_REL)
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")
    outbox = parent / "lessons-outbox"
    outbox.mkdir()
    noop = parent / "noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(terminal_commit, "CLOSE_JOB_SCRIPT", str(noop))
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    return {"repo": repo, "script": script, "plan_seam": plan_seam}


def _fire(env) -> dict:
    return terminal_commit._handler(
        {
            "script_path": env["script"],
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
        },
        repo_root=env["repo"] / ".git",
    )


def test_minted_records_and_both_plans_sidecars_land_foreign_stays(env):
    repo = env["repo"]
    inv = repo / "state" / "mise-inventory"
    (inv / "run-t1.md").write_text("tranche\n", encoding="utf-8")
    (inv / "run-t1-p2.spine.md").write_text("part spine\n", encoding="utf-8")
    (inv / "other-t1.spine.md").write_text("foreign spine\n", encoding="utf-8")
    (repo / env["plan_seam"]).write_text("v: 2\n", encoding="utf-8")
    new_wave = Path(PLAN2_REL).with_suffix("").as_posix() + ".wave-1.seam.yaml"
    (repo / new_wave).write_text("wave: 1\n", encoding="utf-8")

    out = _fire(env)

    assert out["committed"] is True, out
    shown = set(_git(repo, "show", "--name-only", "--format=", out["sha"]).split()) | set(
        _git(repo, "diff", "--name-only", f"{out['coded_sha']}^", out["sha"]).split()
    )
    for rel in (
        "state/mise-inventory/run-t1.md",
        "state/mise-inventory/run-t1-p2.spine.md",
        env["plan_seam"],
        new_wave,
    ):
        assert rel in shown, (rel, sorted(shown))
    assert "state/mise-inventory/other-t1.spine.md" not in shown
    status = _git(repo, "status", "--porcelain", "--", "state/mise-inventory")
    assert "?? state/mise-inventory/other-t1.spine.md" in status
    for rel in ("run-t1.md", "run-t1-p2.spine.md"):
        assert rel not in status
    residue = (out.get("engine_residue") or {})
    assert "state/mise-inventory/run-t1.md" not in str(residue)
