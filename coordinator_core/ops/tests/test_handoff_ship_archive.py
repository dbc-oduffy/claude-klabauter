"""
coordinator_core.ops.tests.test_handoff_ship_archive

Formerly regression coverage for `handoff.ship_and_archive`'s graceful-partial
branch (no `sha` supplied → Step 2 stamps `deployment_state: shipped`
uncommitted, Step 3 attempts a `restage_src=False` archival and hits
`archive_and_commit`'s disk/HEAD drift HARD REFUSAL, post-`4541069c3`,
2026-08-13). That scenario is unreachable since 2026-08-25 (C1b, docs/plans/
2026-08-25-the-handoff-auto-archive-comes-back-capped.md): Step 3's archive
leg (`ops/fleet/archive_shipped_handoffs.py`) was deleted as SUBSUMED into
`fleet.archive_completed_handoffs`, and `handoff_ship_archive.py` was not
migrated onto the successor (the successor drops a live-claim-gate opt-out
this op depends on — a safety decision, not a repoint) — the op now fails
loudly at invocation instead. This file now pins THAT loud-failure contract
instead; see the single test's own docstring for the coverage this replaces
and where the underlying disk/HEAD-drift guard is still exercised directly.

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

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_ship_archive import _handler as _ship_archive_handler

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


def test_ship_and_archive_is_inoperative_since_c1b_subsumption(repo):
    """`handoff.ship_and_archive`'s archive leg was `ops/fleet/archive_shipped_
    handoffs.py`, deleted 2026-08-25 (C1b, docs/plans/2026-08-25-the-handoff-
    auto-archive-comes-back-capped.md — "the sibling op is subsumed") without
    migrating this caller onto `fleet.archive_completed_handoffs`: the
    successor drops the live-claim-gate opt-out this op depends on, so the
    migration was left as an open safety decision, not a repoint
    (`handoff_ship_archive.py`'s own guarded-import comment). The op now
    fails LOUDLY at invocation with exit_code:1 and a named reason, rather
    than silently reaching the graceful-partial/disk-HEAD-drift-refusal
    scenario this test used to pin against the deleted module.

    This REPLACES the former
    `test_ship_and_archive_graceful_partial_then_batch_pass_hard_refuses`
    (AC4/AC5 coverage for the graceful-partial-then-batch-archive sequence):
    that sequence is unreachable now that step 1 (`handoff.ship_and_archive`
    itself) hard-fails before ever reaching the archive leg. The underlying
    disk/HEAD-drift refusal it exercised is still live in
    `archive_terminal_handoffs.py` (see that module's own docstring, "landing
    on archive_and_commit's own disk/HEAD drift refusal at act time") and is
    exercised directly, without the ship_and_archive front door, by
    `coordinator_core/ops/fleet/tests/test_archive_and_commit_disk_head_
    drift.py` — no coverage of the drift guard itself was lost, only this
    op's (currently inoperative) composite front door onto it.
    """
    name = "2026-08-14-graceful-partial.md"
    repo.seed_handoff(name, deployment_state="ready_to_fire")
    rel = f"state/handoffs/{name}"

    result = _run(_ship_archive_handler({"handoff_path": rel}, repo.common_dir))

    assert result["exit_code"] == 1, result
    assert "inoperative" in result["error"], result
    assert "archive_shipped_handoffs" in result["error"], result

    # The op stamps deployment_state:shipped (Step 2) BEFORE discovering its
    # archive leg (Step 3) is inoperative -- it fails loudly, but not before
    # that first mutation. Pinning what actually happens, not a stronger
    # all-or-nothing claim the current implementation does not make.
    fm = repo.fm(name)
    assert read_fm_field(fm, "deployment_state") == "shipped"

    # The handoff must still be sitting, un-archived, in state/handoffs/.
    assert (repo.root / "state" / "handoffs" / name).is_file()
