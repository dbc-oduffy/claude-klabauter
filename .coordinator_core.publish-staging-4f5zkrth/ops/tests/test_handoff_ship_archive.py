"""
coordinator_core.ops.tests.test_handoff_ship_archive

Regression coverage for `handoff.ship_and_archive`'s graceful-partial branch:
when no `sha` is supplied (or none is yet resolvable), Step 2 (`_ship`) stamps
`deployment_state: shipped` and leaves it UNCOMMITTED, and Step 3 (the fleet
archive act path) skips archival because `shipped_in` is still absent. That
branch's docstring, pre-`4541069c3` (2026-08-13), said the stamp "must first
be COMMITTED before boot_sweep's batch path can act on it" — written when the
outcome of a `restage_src=False` archival attempt against that uncommitted
stamp was a soft skip. Post-`4541069c3`, `archive_and_commit`'s disk/HEAD
drift guard turns that attempt into a HARD REFUSAL instead. This file drives
the graceful-partial branch to that exact uncommitted state, then attempts
the archival a later batch pass would attempt (`restage_src=False`, the
`fleet.archive_shipped_handoffs` standalone-op/`session.boot_sweep` shape),
and pins the refusal.

Spec backlink: docs/plans/2026-08-14-placeholder-summaries-and-the-drift-guards-uncounted-callers.md § C2

Deliberately a NEW, narrowly-scoped file — the prior
`coordinator_core/ops/tests/test_handoff_ship_archive.py` was culled
2026-08-07 (commit 1d4e686a9, "the spawn-heavy Windows-poison test set").
This file follows that commit's own guidance for any NEW real-git-touching
test — one throwaway repo per test, explicit (not ambient/conftest)
construction, kept to the minimum test count that proves this one defect
class. Mirrors `test_handoff_reconcile_close_terminal_defects.py`'s `_Repo`
fixture shape (the structurally identical case this defect was fixed
alongside, `b51246a1ead1`).

Import guard: `coordinator_core.ops.handoff_ship_archive` MUST be imported at
module load time to fire `@register_op("handoff.ship_and_archive")` — mirrors
every other op-test file's own import-guard precedent.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.ops.handoff_ship_archive  # noqa: F401 — fires @register_op
import coordinator_core.ops.handoff_stamp  # noqa: F401 — fires @register_op
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op
import coordinator_core.ops.fleet.archive_shipped_handoffs as _archive_shipped_mod

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_ship_archive import _handler as _ship_archive_handler
from coordinator_core.ops.handoff_stamp import _handler as _stamp_handler

# Declared, not excused: this file spawns a real process (git) because the
# property under test is git's own disk/HEAD drift behaviour, which no
# fixture stands in for. See test_handoff_reconcile_close_terminal_defects.py's
# identical pytestmark for the same reasoning.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_OP_NAME = "handoff.ship_and_archive"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_ship_archive @register_op did not fire"
)


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, check=True,
            **no_console_creationflags(),
        )

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root), capture_output=True, check=True,
            **no_console_creationflags(),
        )
        return Path(result.stdout.decode().strip()).resolve()

    @property
    def head_sha(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.root), capture_output=True, check=True,
            **no_console_creationflags(),
        )
        return result.stdout.decode().strip()

    def is_dirty(self, rel: str) -> bool:
        """True iff `rel` (repo-relative) has uncommitted content vs HEAD."""
        result = subprocess.run(
            ["git", "diff", "--quiet", "--", rel],
            cwd=str(self.root), capture_output=True,
            **no_console_creationflags(),
        )
        return result.returncode == 1

    def seed_handoff(self, name: str, *, deployment_state: str) -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f'title: "Test Handoff {name}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            "predecessor: null\n"
            f"deployment_state: {deployment_state}\n"
            "---\n\n# Handoff\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def abs_path(self, name: str) -> str:
        return str(self.root / "state" / "handoffs" / name)

    def fm(self, name: str) -> str:
        text = (self.root / "state" / "handoffs" / name).read_text(encoding="utf-8")
        split = split_frontmatter(text)
        assert split is not None
        return split.fm_text


@pytest.fixture
def repo(tmp_path) -> _Repo:
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, check=True,
            **no_console_creationflags(),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "ship-archive-test@claude-klabauter.test")
    _git("config", "user.name", "ship-archive Test")
    _git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")
    return _Repo(root)


def _run(coro):
    return asyncio.run(coro)


def test_ship_and_archive_graceful_partial_then_batch_pass_hard_refuses(repo):
    """AC4/AC5: drive `handoff.ship_and_archive`'s graceful-partial branch (no
    sha supplied) to its uncommitted `deployment_state: shipped` state, then
    attempt archival the way a later batch pass would (`restage_src=False`,
    same shape as the standalone `fleet.archive_shipped_handoffs` op /
    `session.boot_sweep`'s batch sweep). Post-4541069c3 this is a HARD
    REFUSAL reading the disk/HEAD drift message, not the soft
    "must be committed first" skip the pre-guard docstring described.
    """
    name = "2026-08-14-graceful-partial.md"
    repo.seed_handoff(name, deployment_state="ready_to_fire")
    rel = f"state/handoffs/{name}"

    # --- Step: graceful-partial ship_and_archive call, no sha ---
    result = _run(_ship_archive_handler({"handoff_path": rel}, repo.common_dir))

    assert result["exit_code"] == 0, result
    assert result["shipped"] is True
    assert result["archived"] is False
    assert result["archive_skip_reason"] is not None
    assert "shipped_in" in result["archive_skip_reason"], result

    fm = repo.fm(name)
    assert read_fm_field(fm, "deployment_state") == "shipped"
    assert repo.is_dirty(rel), (
        "the graceful-partial branch must leave deployment_state:shipped "
        "UNCOMMITTED on disk — that is the exact state the drift guard reacts to"
    )

    # --- Simulate a later stamp of shipped_in (still uncommitted — mirrors a
    # subsequent handoff.stamp call landing before any commit happens) ---
    stamp_res = _run(_stamp_handler(
        {"handoff_path": rel, "sha": repo.head_sha, "kind": "ship-commit"},
        repo.common_dir,
    ))
    assert stamp_res["exit_code"] == 0, stamp_res
    assert repo.is_dirty(rel), "shipped_in stamp must also land uncommitted"

    # --- A later BATCH pass (restage_src=False — the standalone op / boot_sweep
    # shape, never the holder-initiated composite) attempts to archive ---
    act = _run(_archive_shipped_mod._handle_act(
        "already-terminal", repo.root, [rel], restage_src=False, common_dir=repo.common_dir,
    ))

    acted_ids = [item.get("id") for item in act.get("acted", [])]
    failed_ids = [item.get("id") for item in act.get("failed", [])]
    assert rel not in acted_ids, (
        "a restage_src=False batch pass must NOT succeed in archiving the "
        f"op-authored uncommitted stamp; acted={act.get('acted')}"
    )
    assert rel in failed_ids, act
    failed_item = next(item for item in act["failed"] if item["id"] == rel)
    assert "disk/HEAD drift" in failed_item["reason"], failed_item
    assert "refusing move" in failed_item["reason"], failed_item

    # The handoff must still be sitting, un-archived, in state/handoffs/.
    assert (repo.root / "state" / "handoffs" / name).is_file()
