"""dispatch.terminal_commit on a mise run mints the review receipt onto the inventory record.

Fixture: the budget driver's plan-run repo rewired as inventory mode -- the script's marker
names a minted spine, whose sibling inventory record maps row ``P1.C3`` to ``docs/plan.md``.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.dispatch_emit import run_close, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

SPINE_REL = "state/mise-inventory/run.spine.md"
INVENTORY_REL = "state/mise-inventory/run.md"


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
    inv = repo / "state" / "mise-inventory"
    inv.mkdir(parents=True)
    (repo / INVENTORY_REL).write_text(
        "---\nrun_id: run\n---\n\n## Chunk table\n\n"
        "| ID | Spec path | Summary | Footprint | Deps | Verification | Complexity | Disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| P1 | `{driver.PLAN_REL}` | s | `a.py` | - | v | S | pending |\n",
        encoding="utf-8",
    )
    (repo / SPINE_REL).write_text(
        "---\nrun_id: run\nderived_from: mise inventory record\n---\n\n## Tasks\n", encoding="utf-8"
    )
    _git(repo, "add", "state/mise-inventory")
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
    return {"repo": repo, "script": script}


def _fire(env, review=None) -> dict:
    return terminal_commit._handler(
        {
            "script_path": env["script"],
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": review or copy.deepcopy(driver.INLINE_REVIEW),
        },
        repo_root=env["repo"] / ".git",
    )


def _stamp(path: Path):
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    return (yaml.safe_load(split.fm_text) or {}).get("review_stamp")


def test_receipt_lands_on_the_inventory_record_not_the_spine(env):
    out = _fire(env)
    repo = env["repo"]

    assert out["committed"] is True and "coded_sha" in out, out
    assert out.get("review_receipt") == INVENTORY_REL, {k: out.get(k) for k in ("rows_coded", "chunks_committed", "review_stamp", "review_stamp_refusal", "error")}
    stamp = _stamp(repo / INVENTORY_REL)
    assert stamp["terminal_commit_sha"] == out["sha"]
    assert stamp["run_base_sha"] == "a" * 40
    assert _stamp(repo / SPINE_REL) is None
    shown = _git(repo, "show", "--name-only", "--format=", out["coded_sha"]).split()
    assert INVENTORY_REL in shown and SPINE_REL not in shown
    assert _git(repo, "status", "--porcelain", "--", INVENTORY_REL) == ""


def test_close_job_record_carries_receipt_and_both_shas(env):
    out = _fire(env)

    record = json.loads((env["repo"] / run_close.close_job_path("run")).read_text(encoding="utf-8"))
    assert record["review_receipt"] == INVENTORY_REL
    assert record["run_base_sha"] == "a" * 40
    assert record["terminal_sha"] == out["sha"]
    assert "review" in record["legs"]
    assert out["close"]["legs"]["review"]["status"] == "queued"


def test_refused_receipt_fails_review_and_records_no_receipt(env):
    review = copy.deepcopy(driver.INLINE_REVIEW)
    review["delivery"]["verdict"] = "FAIL"

    out = _fire(env, review)

    assert out["committed"] is True
    assert out["review_stamp"] == "refused" and "review_receipt" not in out
    assert out["close"]["legs"]["review"]["status"] == "failed"
    assert "review" in out["close"]["outstanding"]
    assert _stamp(env["repo"] / INVENTORY_REL) is None
    record = json.loads((env["repo"] / run_close.close_job_path("run")).read_text(encoding="utf-8"))
    assert record["review_receipt"] is None and "review" not in record["legs"]
