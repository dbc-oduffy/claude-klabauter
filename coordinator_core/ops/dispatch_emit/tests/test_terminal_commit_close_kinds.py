"""dispatch.terminal_commit classifies the run's close kind and pays each kind's legs.

Fixtures are the budget driver's plan-run repo rewired per kind: an inventory-mode mise run,
an XS run staged on a minted spine under a gitignored run dir, and S ``spec-dispatch`` plans.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import run_close, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

MISE_SPINE = "state/mise-inventory/run.spine.md"
MISE_INVENTORY = "state/mise-inventory/run.md"
XS_SPINE = "scratch/warp/xsrun/xs.spine.md"
LIVE = {"ran", "discharged", "not-applicable", "failed", "queued"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, **no_console_creationflags()
    )


def _marker(repo: Path, script: str, chunk_id: str, plan_rel: str) -> None:
    request = CommitRequest(chunks=(ChunkCommit(id=chunk_id, title="t", paths=("a.py",)),), plan_path=plan_rel)
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")


@pytest.fixture
def base(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
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
    return repo, script


def _fire(repo: Path, script: str) -> dict:
    return terminal_commit._handler(
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
        },
        repo_root=repo / ".git",
    )


def _spec_dispatch_plan(
    scope_line: str, extra_row: str = "", newline: str = "\n"
) -> bytes:
    text = driver._PLAN.replace("status: executing\n", f"status: executing\n{scope_line}\n")
    if extra_row:
        text = text.replace("  deferred: false\n```", "  deferred: false\n" + extra_row + "```")
    return text.replace("\n", newline).encode("utf-8")


_DEFERRED_ROW = (
    "- id: C4\n  title: t4\n  change_kind: code-edit\n  surface: a.py\n"
    "  disposition: backlogged\n  deferred: true\n"
)


def _assert_whole_block(close: dict, kind: str) -> None:
    assert close["kind"] == kind and close["applies"] is True
    assert set(run_close.RUN_BOUND_LEGS) <= set(close["legs"])
    for leg in run_close.RUN_BOUND_LEGS:
        result = close["legs"][leg]
        assert result["status"] in LIVE, (leg, result)
        if result["status"] in ("failed", "not-applicable"):
            assert result["detail"], (leg, result)
    for leg in run_close.SESSION_BOUND_LEGS:
        assert close["legs"][leg]["status"] == "session-bound"


def _mise(repo: Path, script: str) -> None:
    (repo / "state" / "mise-inventory").mkdir(parents=True)
    (repo / MISE_INVENTORY).write_text(
        "---\nrun_id: run\n---\n\n## Chunk table\n\n"
        "| ID | Spec path | Summary | Footprint | Deps | Verification | Complexity | Disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| P1 | `{driver.PLAN_REL}` | s | `a.py` | - | v | S | pending |\n",
        encoding="utf-8",
    )
    (repo / MISE_SPINE).write_text(
        "---\nrun_id: run\nderived_from: mise inventory record\n---\n\n## Tasks\n", encoding="utf-8"
    )
    _git(repo, "add", "state/mise-inventory")
    _git(repo, "commit", "-q", "-m", "inventory")
    _marker(repo, script, "P1.C3", MISE_SPINE)


def test_mise_run_marks_plan_reconciliation_and_memo_lifecycle_not_applicable(base):
    repo, script = base
    _mise(repo, script)

    out = _fire(repo, script)

    close = out["close"]
    _assert_whole_block(close, "mise-run")
    for leg in ("plan-reconciliation", "memo-lifecycle"):
        assert close["legs"][leg]["status"] == "not-applicable", (leg, close["legs"][leg])
        assert leg not in close["outstanding"]
    assert close["legs"]["review"]["status"] == "queued"
    assert close["outstanding"] == []
    assert close["legs"]["memo-lifecycle"]["detail"] == run_close.kind_presets(
        "mise-run", plan_governs=False
    )["memo-lifecycle"].detail


def test_xs_run_on_a_minted_spine_has_no_governing_plan(base):
    repo, script = base
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    spine = repo / XS_SPINE
    spine.parent.mkdir(parents=True)
    spine.write_text(
        "---\nplan_id: pln-xs-aaaaaa\nderived_from: sizing object\n---\n\n## Tasks\n\n"
        "```yaml plan-tasks\n- id: C3\n  title: t3\n  change_kind: code-edit\n"
        "  surface: a.py\n  disposition: open\n  deferred: false\n```\n",
        encoding="utf-8",
    )
    _marker(repo, script, "C3", XS_SPINE)

    out = _fire(repo, script)

    close = out["close"]
    _assert_whole_block(close, "xs-s-run")
    assert close["legs"]["plan-reconciliation"] == {"status": "not-applicable", "detail": "XS run: no plan governs"}
    assert "plan-reconciliation" not in close["outstanding"]
    assert close["legs"]["review"] == {
        "status": "failed", "detail": terminal_commit._NOT_LANDED,
    }
    assert "review" in close["outstanding"]


def test_wave_fire_xs_lane_ask_route_request_with_baton_is_accepted(base):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import XS_ROW_ID, mint_xs_spine, xs_spine_plan_id

    repo, script = base
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    stem = "2026-10-13-xs-lane"
    sizing_rel = f"state/sizings/{stem}.yaml"
    (repo / sizing_rel).parent.mkdir(parents=True)
    (repo / sizing_rel).write_text("deliverable_id: dlv-xs-lane\n", encoding="utf-8")
    sizing = {
        "intent": "xs lane",
        "deliverable_id": "dlv-xs-lane",
        "exit_criterion": {"statement": "a.py exists"},
    }
    run_dir = repo / "scratch" / "warp" / "run-xs"
    run_dir.mkdir(parents=True)
    spine_text, spine_path = mint_xs_spine(sizing, sizing_rel=sizing_rel, writes=["a.py"], out_dir=run_dir)
    spine_path.write_text(spine_text, encoding="utf-8")
    spine_rel = spine_path.relative_to(repo).as_posix()
    plan_id = xs_spine_plan_id(stem)
    request = CommitRequest(
        chunks=(
            ChunkCommit(id=XS_ROW_ID, title="xs lane", paths=("a.py",)),
            ChunkCommit(id="sizing", title="sizing record", paths=(sizing_rel,)),
        ),
        deliverable_id="dlv-xs-lane",
        repo_root=repo.as_posix(),
        plan_path=spine_rel,
    )
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")
    inline_review = copy.deepcopy(driver.INLINE_REVIEW)
    inline_review["plan_id"] = plan_id
    inline_review["integration_stem"] = f"{plan_id}.review-wave-bookkeeping"

    out = terminal_commit._handler(
        {
            "incomplete_chunks": [],
            "script_path": script,
            "session_id": driver.SESSION_ID,
            "inline_review": inline_review,
            "held": [],
        },
        repo_root=repo / ".git",
    )

    close = out["close"]
    _assert_whole_block(close, "xs-s-run")
    assert close["legs"]["plan-reconciliation"]["status"] == "not-applicable"
    assert "error" not in out, out


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_s_spec_dispatch_plan_with_a_deferred_row_is_not_held_to_the_harvest(base, newline):
    repo, script = base
    (repo / driver.PLAN_REL).write_bytes(
        _spec_dispatch_plan("scope_mode: spec-dispatch  # S lane, fired as a script", _DEFERRED_ROW, newline)
    )
    _git(repo, "add", "-A", "docs")
    _git(repo, "commit", "-q", "-m", "s plan")

    out = _fire(repo, script)

    close = out["close"]
    _assert_whole_block(close, "xs-s-run")
    assert close["legs"]["plan-reconciliation"]["status"] == "ran", close["legs"]["plan-reconciliation"]
    assert close["outstanding"] == []


def test_plan_run_keeps_the_deferred_harvest(base):
    repo, script = base
    (repo / driver.PLAN_REL).write_bytes(_spec_dispatch_plan("scope_mode: standard", _DEFERRED_ROW))
    _git(repo, "add", "-A", "docs")
    _git(repo, "commit", "-q", "-m", "plan")

    close = _fire(repo, script)["close"]

    assert close["kind"] == "plan-run"
    assert close["legs"]["plan-reconciliation"] == {
        "status": "failed", "detail": "deferred rows unharvested: C4",
    }
    assert close["outstanding"] == ["plan-reconciliation"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("---\nderived_from: mise inventory record\n---\n", ("mise-run", False)),
        ("---\nderived_from: 'sizing object'\n---\n", ("xs-s-run", False)),
        ("---\nscope_mode: spec-dispatch\n---\n", ("xs-s-run", True)),
        ("---\nscope_mode: spec-dispatch  # S lane, x\n---\n", ("xs-s-run", True)),
        ('---\r\nscope_mode: "spec-dispatch"\r\ntitle: t\r\n---\r\n', ("xs-s-run", True)),
        ("---\nscope_mode: standard\n---\n", ("plan-run", True)),
        ("---\ntitle: spec-dispatch scope_mode\n---\n", ("plan-run", True)),
        ("no frontmatter", ("plan-run", True)),
        (None, ("plan-run", True)),
    ],
)
def test_close_kind_reads_the_frontmatter(text, expected):
    assert terminal_commit._close_kind(text) == expected
