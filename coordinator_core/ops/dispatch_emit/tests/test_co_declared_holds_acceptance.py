"""Acceptance: siblings co-declaring DELIVERY.md both land; a halted hold lands through a per-path release."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.commit import commit_paths
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import co_declared_settle as cds
from coordinator_core.ops.dispatch_emit import halted_hold_release, own_hunk_stage, release_halted_path
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FIRST = "ask-20261027T000000-aaaaaa"
SECOND = "ask-20261027T000000-bbbbbb"
DELIVERY = "DELIVERY.md"
HALTED = "ask-20261027T000000-8f2097"


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


class _World:
    def __init__(self, root, baseline):
        self.root = root
        self.baseline = baseline


@pytest.fixture
def world(tmp_path, monkeypatch):
    for cmd in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        _git(tmp_path, *cmd)
    (tmp_path / DELIVERY).write_text("head\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    baselines: dict = {}
    monkeypatch.setattr(hha, "_baseline", lambda root, run: baselines.get(run))

    def baseline(run, text):
        sha = git_objects.write_object(resolve_git_common_dir(tmp_path), b"blob", text.encode())
        baselines[run] = {DELIVERY: sha}

    return _World(tmp_path, baseline)


def test_two_siblings_co_declaring_delivery_md_both_land_their_own_hunks(world):
    repo = world.root
    first_lines, second_lines = "first1\nfirst2\n", "second1\n"
    (repo / DELIVERY).write_text("head\n" + first_lines + second_lines)
    world.baseline(FIRST, "head\n")
    world.baseline(SECOND, "head\n" + first_lines)

    stage, still = cds.settle(repo, FIRST, [(DELIVERY, SECOND)])
    assert still == [] and list(stage) == [DELIVERY]
    diffs = own_hunk_stage.read_diff(repo, [DELIVERY])
    blob = cds.settled_blob(repo, DELIVERY, diffs[DELIVERY].head_bytes, stage[DELIVERY])
    commit_paths(repo, [DELIVERY], "first run lands", supplied_blobs={DELIVERY: blob})

    assert _git(repo, "show", f"HEAD:{DELIVERY}") == "head\n" + first_lines
    assert (repo / DELIVERY).read_text() == "head\n" + first_lines + second_lines

    # The first run has landed, so the second lands the remaining worktree lines whole.
    commit_paths(repo, [DELIVERY], "second run lands")
    assert _git(repo, "show", f"HEAD:{DELIVERY}") == "head\n" + first_lines + second_lines


def test_a_line_both_siblings_edit_still_holds_the_landing(world):
    repo = world.root
    (repo / DELIVERY).write_text("both\n")
    world.baseline(FIRST, "head\n")
    world.baseline(SECOND, "head\n")
    holds = [(DELIVERY, SECOND)]
    assert cds.settle(repo, FIRST, holds) == ({}, holds)


def test_a_halted_hold_lands_through_a_per_path_release_with_a_reason(world):
    repo = world.root
    rdir = record_dir(repo, HALTED)
    rdir.mkdir(parents=True)
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {DELIVERY: "peer-held"}}}),
    )
    (repo / DELIVERY).write_text("head\nmine\n")
    holds = [(DELIVERY, HALTED)]
    world.baseline(FIRST, "head\n")
    world.baseline(HALTED, "head\n")
    assert cds.settle(repo, FIRST, holds) == ({}, holds)

    out = release_halted_path.release_path(repo, HALTED, DELIVERY, "the dirt is the landing run's own append")

    assert out["released"] is True
    assert halted_hold_release.without_released_paths(repo, holds) == []
    assert cds.settle(repo, FIRST, holds) == ({}, [])
    commit_paths(repo, [DELIVERY], "lands whole after the release")
    assert _git(repo, "show", f"HEAD:{DELIVERY}") == "head\nmine\n"


# --- through the real dispatch.land_run entry ---------------------------------------------------

from coordinator_core.ops.dispatch_emit import peer_hunk_gate  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: E402,F401

LANDED_PATH = "pkg/execute_review.py"


def _halted_sibling(root) -> None:
    rdir = root / "state" / "warp" / HALTED
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/b.yaml"}), encoding="utf-8")
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {LANDED_PATH: "peer-held"}}}),
        encoding="utf-8",
    )
    prod._write(root, "state/sizings/b.yaml", 'plan: "docs/plans/b.md"\n')
    prod._write(
        root, "docs/plans/b.md",
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + LANDED_PATH + "\n```\n",
    )


def test_land_run_lands_a_halted_hold_only_after_a_per_path_release_with_a_reason(repo):
    from coordinator_core.ops.dispatch_emit import release_halted_path as rhp

    _halted_sibling(repo)
    prod._author(repo, LANDED_PATH, "x = 1\n")
    prod._declare(repo, LANDED_PATH)
    prod._record_dir(repo)
    prod._write(repo, "scratch/review.md", "reviewed\n")
    review = {
        **prod.REVIEWED,
        "wave_reviews": [{"verdict": "approve", "sidecar_path": "scratch/review.md"}],
    }
    head = prod._git(["rev-parse", "HEAD"], repo).strip()

    held = prod._land(repo, inline_review=review)
    assert held.get("refused") == peer_hunk_gate.PEER_HUNKS_CO_DECLARED, held
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == head

    blank = rhp._handler({"run_id": HALTED, "path": LANDED_PATH, "reason": " "}, repo_root=repo / ".git")
    assert blank["ok"] is False
    out = rhp._handler(
        {"run_id": HALTED, "path": LANDED_PATH, "reason": "the dirt is the landing run's own"},
        repo_root=repo / ".git",
    )
    assert out["released"] is True

    landed = prod._land(repo, inline_review=review)
    assert landed.get("committed") is True, landed
    assert prod._git(["show", f"HEAD:{LANDED_PATH}"], repo) == "x = 1\n"
