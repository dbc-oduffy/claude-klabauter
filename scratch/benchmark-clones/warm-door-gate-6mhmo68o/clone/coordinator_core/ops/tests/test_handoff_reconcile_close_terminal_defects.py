"""
coordinator_core.ops.tests.test_handoff_reconcile_close_terminal_defects

Regression coverage for the two break-class defects reported cross-repo
(cross-repo/inbox/2026-08-10-doe-claude-em-reconcile-close-terminal-and-scrub-
key.md § 1-2), both landing on `handoff.reconcile_close_terminal` and its
`handoff_transition._close` seam:

  (1) `pickup_ready: true` used to survive a terminal close — `_close` never
      touched the field, so a closed baton kept advertising as live pickup
      work. Fixed at the `_close` seam (handoff_transition.py) so EVERY
      caller of close (not just this op) gets the fix.
  (2) The op used to stamp `closed_reason: displaced` even when a live
      successor already named the candidate via `predecessor:` — the
      schema's own `displaced` definition forbids that combination
      ("replaced with NO lineage edge — with an edge it's continued, not
      closed"). Fixed by running the SAME live-lineage-edge guard
      `handoff.archive_transition` mode="chain" already runs internally,
      BEFORE any mutation, in `handoff_reconcile_close_terminal._handler`.

Deliberately a NEW, narrowly-scoped file — not a restoration of the deleted
`coordinator_core/ops/tests/test_handoff_reconcile_close_terminal.py`
(culled 2026-08-07, commit 1d4e686a9, "the spawn-heavy Windows-poison test
set"; restoration is tracked separately per that commit's own ledger,
state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md). This file
follows that commit's own guidance for any NEW real-git-touching test —
`coordinator_core/ops/ceremony/tests/fixtures/real_git.py`'s model: one
throwaway repo per test, explicit (not ambient/conftest) construction, kept
to the minimum test count that proves the two fixes. `locked_rmw` resolves
its lock directory via `git_common_dir`, which itself shells out to
`git rev-parse` — there is no git-free way to exercise `_close` or
`handoff_reconcile_close_terminal._handler` end-to-end, so a real (not
mocked) repo is unavoidable here, same as every other handoff-op test in
this codebase.

Import guard: `coordinator_core.ops.handoff_reconcile_close_terminal` MUST
be imported at module load time to fire
`@register_op("handoff.reconcile_close_terminal")` — mirrors every other
op-test file's own import-guard precedent.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.ops.handoff_reconcile_close_terminal  # noqa: F401 — fires @register_op
import coordinator_core.ops.handoff_reconcile_close_terminal as _rct_mod
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_reconcile_close_terminal import _handler as _reconcile_handler
from coordinator_core.ops.handoff_transition import _close

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_OP_NAME = "handoff.reconcile_close_terminal"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_reconcile_close_terminal @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Minimal real-git fixture — one throwaway repo per test, explicit import
# only (not a conftest fixture), mirroring ops/ceremony/tests/fixtures/
# real_git.py's model. Trimmed re-derivation of the deleted
# ops/tests/conftest.py HandoffRepo (git show 6f0e89044) — only the pieces
# this file's tests actually need.
# ---------------------------------------------------------------------------


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

    def seed_handoff(
        self,
        name: str,
        status: str,
        *,
        deployment_state: str,
        predecessor: Optional[str] = None,
        pickup_ready: Optional[bool] = None,
        closed_reason: Optional[str] = None,
    ) -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        predecessor_value = f'"{predecessor}"' if predecessor else "null"
        lines = [
            f'title: "Test Handoff {name}"',
            "created: 2026-01-01",
            "branch: work/test/2026-01-01",
            f"status: {status}",
            f"predecessor: {predecessor_value}",
            f"deployment_state: {deployment_state}",
        ]
        if closed_reason is not None:
            lines.append(f"closed_reason: {closed_reason}")
        if pickup_ready is not None:
            lines.append(f"pickup_ready: {'true' if pickup_ready else 'false'}")
        fm_block = "\n".join(lines)
        content = f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n"
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

    def archived_copies(self, name: str) -> list[Path]:
        return [p for p in (self.root / "archive" / "handoffs").rglob("*.md") if p.name == name]


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
    _git("config", "user.email", "reconcile-close-terminal-test@claude-klabauter.test")
    _git("config", "user.name", "reconcile-close-terminal Test")
    _git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")
    return _Repo(root)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Defect 1 — pickup_ready cleared on close (_close seam)
# ---------------------------------------------------------------------------


def test_close_clears_pickup_ready_true(repo):
    """`_close` must flip pickup_ready: true -> false in the SAME write that
    stamps deployment_state:closed — the two fields are one logical state
    (memo § 1)."""
    name = "2026-08-10-pickup-ready-true.md"
    repo.seed_handoff(
        name, "open", deployment_state="ready_to_fire", pickup_ready=True
    )

    result = _close(f"state/handoffs/{name}", "displaced", repo.root, repo.common_dir)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    fm = repo.fm(name)
    assert read_fm_field(fm, "deployment_state") == "closed"
    assert read_fm_field(fm, "closed_reason") == "displaced"
    assert read_fm_field(fm, "pickup_ready") == "false", (
        f"pickup_ready must be flipped to false alongside deployment_state:closed; "
        f"got {read_fm_field(fm, 'pickup_ready')!r}"
    )


def test_close_inserts_pickup_ready_false_when_absent(repo):
    """A record with no pickup_ready key at all gains one (false) on close —
    the field is inserted, not left absent, so the same guarantee applies
    going forward."""
    name = "2026-08-10-no-pickup-ready-field.md"
    repo.seed_handoff(name, "open", deployment_state="in_flight")

    result = _close(f"state/handoffs/{name}", "stale", repo.root, repo.common_dir)

    assert result["exit_code"] == 0, result
    fm = repo.fm(name)
    assert read_fm_field(fm, "pickup_ready") == "false"


def test_close_reruns_on_prior_closed_record_still_advertising_pickup_ready(repo):
    """A record already at deployment_state:closed + matching closed_reason
    from BEFORE this fix shipped (pickup_ready still true) must not be
    treated as a full-target-state no-op — the idempotency gate now also
    requires pickup_ready==false, so a re-close call actually applies the
    fix instead of short-circuiting past it."""
    name = "2026-08-10-pre-fix-closed-record.md"
    repo.seed_handoff(
        name, "open", deployment_state="closed", closed_reason="displaced",
        pickup_ready=True,
    )

    result = _close(f"state/handoffs/{name}", "displaced", repo.root, repo.common_dir)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True, (
        "a pre-fix closed+matching-reason record with pickup_ready still true "
        "must re-apply, not idempotent-no-op past the fix"
    )
    fm = repo.fm(name)
    assert read_fm_field(fm, "pickup_ready") == "false"


def test_close_inserts_pickup_ready_false_when_absent_on_already_closed_record(repo):
    """`pickup_ready` absent (not true/false) on an already-closed record
    with a matching closed_reason falls through the three-way idempotency
    AND (deployment=="closed" and existing_reason==reason and
    existing_pickup_ready=="false") — absent, not "false", so the third
    condition is False and this must re-apply via the else insert-after-
    closed_reason branch (handoff_transition.py line ~1083). Existing
    coverage only exercised "absent" on a non-closed record and "closed+
    matching" with pickup_ready explicitly set — this combination
    (closed+matching+ABSENT) was untested."""
    name = "2026-08-11-closed-no-pickup-ready-field.md"
    repo.seed_handoff(
        name, "open", deployment_state="closed", closed_reason="displaced",
    )

    result = _close(f"state/handoffs/{name}", "displaced", repo.root, repo.common_dir)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True, (
        "pickup_ready absent must not satisfy the idempotency no-op — "
        "must re-apply and insert the field"
    )
    fm = repo.fm(name)
    assert read_fm_field(fm, "pickup_ready") == "false"


def test_close_idempotent_noop_at_full_target_state_including_pickup_ready(repo):
    """Once pickup_ready is already false alongside a matching closed_reason,
    a re-close call IS a genuine no-op (byte-identical write skipped)."""
    name = "2026-08-10-already-fully-closed.md"
    repo.seed_handoff(
        name, "open", deployment_state="closed", closed_reason="displaced",
        pickup_ready=False,
    )

    result = _close(f"state/handoffs/{name}", "displaced", repo.root, repo.common_dir)

    assert result["exit_code"] == 0, result
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# Defect 2 — live-lineage-edge guard refuses displaced over a real successor
# ---------------------------------------------------------------------------


def test_reconcile_close_terminal_refuses_displaced_over_live_lineage_edge(repo):
    """A candidate already named as `predecessor:` by a live successor must
    refuse reason='displaced' outright — the schema defines displaced as
    'NO lineage edge'; with an edge present, continued (not closed) is the
    correct terminal, and this op is scoped to the no-successor shape only.
    No mutation (deployment_state/closed_reason/pickup_ready all untouched)."""
    name = "2026-08-10-has-live-successor.md"
    repo.seed_handoff(name, "open", deployment_state="ready_to_fire", pickup_ready=True)
    repo.seed_handoff(
        "2026-08-10-successor.md", "claimed", deployment_state="in_flight",
        predecessor=name,
    )

    result = _run(_reconcile_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["closed"] is False
    assert result["archived"] is False
    assert "supersede" in (result.get("error") or "").lower(), (
        f"refusal must name the correct route (supersede); got {result.get('error')!r}"
    )
    fm = repo.fm(name)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire", (
        "no mutation may land when the guard refuses — deployment_state must "
        "stay exactly as seeded"
    )
    assert read_fm_field(fm, "pickup_ready") == "true"


def test_reconcile_close_terminal_still_closes_when_no_lineage_edge(repo):
    """Sanity/non-regression: the ordinary no-successor shape this op exists
    for is unaffected by the new guard — still closes + archives, and the
    defect-1 pickup_ready fix lands on the archived copy too."""
    name = "2026-08-10-genuinely-orphaned.md"
    repo.seed_handoff(name, "open", deployment_state="ready_to_fire", pickup_ready=True)

    result = _run(_reconcile_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["closed"] is True
    assert result["archived"] is True
    archived = repo.archived_copies(name)
    assert len(archived) == 1
    split = split_frontmatter(archived[0].read_text(encoding="utf-8"))
    assert read_fm_field(split.fm_text, "deployment_state") == "closed"
    assert read_fm_field(split.fm_text, "closed_reason") == "displaced"
    assert read_fm_field(split.fm_text, "pickup_ready") == "false"


def test_reconcile_close_terminal_refuses_on_indeterminate_guard(repo, monkeypatch):
    """Guard exit_code==2 (indeterminate/fail-closed, e.g. an unscannable
    subtree) must refuse the close outright, same fail-closed posture as
    every other consumer of this guard — `_handler`'s exit_code==2 branch
    (module lines ~393-398) was unverified by any test before this one."""
    name = "2026-08-11-indeterminate-guard.md"
    repo.seed_handoff(name, "open", deployment_state="ready_to_fire")

    async def _fake_indeterminate(params, repo_root=None):
        return {"exit_code": 2, "error": "synthetic: unscannable subtree"}

    monkeypatch.setattr(
        "coordinator_core.ops.handoff_reconcile_close_terminal._handoff_has_live_children",
        _fake_indeterminate,
    )

    result = _run(_reconcile_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["closed"] is False
    assert result["archived"] is False
    assert "indeterminate" in (result.get("error") or "").lower()
    fm = repo.fm(name)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire", (
        "no mutation may land when the guard is indeterminate"
    )


def test_reconcile_close_terminal_idempotent_replay_from_archive_root(repo):
    """A second call against a handoff already closed+archived by a prior
    call resolves under archive/handoffs/ (not state/handoffs/) — the op
    must detect the already-terminal on-disk state and return a clean
    no-op (already_closed/already_archived: True, exit_code 0) rather than
    attempting a mutation `handoff_transition._resolve_path` would refuse.
    Covers module lines ~276-308, untested before this."""
    name = "2026-08-11-archived-already-terminal.md"
    archive_path = repo.root / "archive" / "handoffs" / "2026-08" / name
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        "---\n"
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: closed\n"
        "predecessor: null\n"
        "deployment_state: closed\n"
        "closed_reason: displaced\n"
        "pickup_ready: false\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    repo._git("add", str(archive_path))
    repo._git("commit", "-m", f"archive {name}")

    result = _run(_reconcile_handler(
        {"handoff_path": str(archive_path), "reason": "displaced"},
        repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["closed"] is True
    assert result["already_closed"] is True
    assert result["archived"] is True
    assert result["already_archived"] is True


# ---------------------------------------------------------------------------
# P1 TOCTOU fix — guard/write race between step-0's unlocked pre-check and
# _close's locked write
# ---------------------------------------------------------------------------


def test_close_live_children_recheck_aborts_when_edge_appears_under_lock(repo):
    """Synthetic interleaving pin, at the `_close` seam: a
    `live_children_recheck` callback that reports a live edge appeared
    (exit_code=0) — as if a successor were created between step 0's
    unlocked guard and this write — must abort the write from INSIDE the
    locked mutate closure, proving the recheck runs somewhere that can
    actually prevent the race rather than as a dead parameter."""
    name = "2026-08-11-toctou-recheck.md"
    repo.seed_handoff(name, "open", deployment_state="ready_to_fire")

    def _recheck_reports_live_edge() -> dict:
        return {"exit_code": 0, "children": ["synthetic-successor.md"]}

    result = _close(
        f"state/handoffs/{name}", "displaced", repo.root, repo.common_dir,
        live_children_recheck=_recheck_reports_live_edge,
    )

    assert result["exit_code"] == 1, result
    assert "live-lineage-edge re-check" in (result.get("error") or "")
    fm = repo.fm(name)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire", (
        "the write must be aborted before it commits when the recheck "
        "reports a live edge appeared inside the lock"
    )


# ---------------------------------------------------------------------------
# P1 fix (review: coordinatorcode-reviewer-2d69ff87.md) — restage_src must
# gate on this call having actually authored the close write, not fire
# unconditionally. Two arms: a fresh close (opts in, terminal state lands
# in the archival commit) and an already-closed no-op (does NOT opt in, and
# uncommitted dirt on the src file is not swept into the archival commit).
# ---------------------------------------------------------------------------


def test_reconcile_close_terminal_fresh_close_restages_terminal_state(repo):
    """Fresh close (this call authors the drift): restage_src must opt in,
    and the archival commit must carry the terminal state this call just
    wrote, not the pre-close blob."""
    name = "2026-08-14-fresh-close-restage.md"
    repo.seed_handoff(name, "open", deployment_state="ready_to_fire", pickup_ready=True)

    result = _run(_reconcile_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["closed"] is True
    assert result["already_closed"] is False
    assert result["archived"] is True
    archived = repo.archived_copies(name)
    assert len(archived) == 1
    committed = subprocess.run(
        ["git", "show", f"HEAD:{archived[0].relative_to(repo.root).as_posix()}"],
        cwd=str(repo.root), capture_output=True, check=True,
        **no_console_creationflags(),
    ).stdout.decode()
    split = split_frontmatter(committed)
    assert read_fm_field(split.fm_text, "deployment_state") == "closed", (
        "the archival commit must carry the fresh close's terminal state, "
        "proving restage_src opted in for this call-authored drift"
    )


def test_reconcile_close_terminal_already_closed_noop_does_not_sweep_dirty_src(repo):
    """already_closed=True (this call authors NO drift): restage_src must
    NOT opt in. An unrelated uncommitted edit sitting on the src file (as if
    left by a concurrent session) must NOT be swept into the archival
    commit — the disk/HEAD drift guard must refuse the move outright rather
    than restage and commit content this call never authored."""
    name = "2026-08-14-already-closed-dirty-src.md"
    repo.seed_handoff(
        name, "open", deployment_state="closed", closed_reason="displaced",
        pickup_ready=False,
    )
    # Simulate a concurrent session's uncommitted write landing on src AFTER
    # the committed (already-terminal) state above but BEFORE this call.
    src_path = repo.root / "state" / "handoffs" / name
    dirty_marker = "concurrent-session-uncommitted-marker"
    src_path.write_text(
        src_path.read_text(encoding="utf-8") + f"\n<!-- {dirty_marker} -->\n",
        encoding="utf-8",
    )

    result = _run(_reconcile_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        repo.common_dir,
    ))

    assert result["closed"] is True
    assert result["already_closed"] is True, (
        "_close must be a no-op here — deployment_state/closed_reason/"
        "pickup_ready already match the target state"
    )
    assert result["archived"] is False, (
        "the drift guard must refuse the move rather than restage and "
        "sweep the concurrent session's uncommitted marker into the "
        "archival commit"
    )
    # src must still be on disk, uncommitted, dirty — not archived, and its
    # dirty content must never have reached the git object store.
    assert src_path.exists()
    show_all = subprocess.run(
        ["git", "log", "--all", "-p", "--", f"state/handoffs/{name}"],
        cwd=str(repo.root), capture_output=True, check=True,
        **no_console_creationflags(),
    ).stdout.decode()
    assert dirty_marker not in show_all, (
        "the concurrent-session marker must never be committed — it was "
        "never this call's drift to restage"
    )


def test_reconcile_close_terminal_toctou_recheck_blocks_race(repo, monkeypatch):
    """End-to-end pin: simulate a successor landing in the window between
    step 0's unlocked guard read and _close's own locked write — the
    FIRST guard call (step 0) sees no live children, but the SECOND call
    (the in-lock recheck `_close` now runs) sees the race and must abort
    the write, even though step 0 alone reported it safe."""
    name = "2026-08-11-toctou-e2e.md"
    repo.seed_handoff(name, "open", deployment_state="ready_to_fire")

    real_guard = _rct_mod._handoff_has_live_children
    calls = {"n": 0}

    async def _guard_then_race(params, repo_root=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_guard(params, repo_root)
        return {
            "exit_code": 0,
            "children": [repo.abs_path("synthetic-successor.md")],
        }

    monkeypatch.setattr(
        "coordinator_core.ops.handoff_reconcile_close_terminal._handoff_has_live_children",
        _guard_then_race,
    )

    result = _run(_reconcile_handler(
        {"handoff_path": f"state/handoffs/{name}", "reason": "displaced"},
        repo.common_dir,
    ))

    assert calls["n"] >= 2, "the recheck inside the lock must actually run"
    assert result["exit_code"] == 1, result
    assert result["closed"] is False
    fm = repo.fm(name)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire", (
        "the TOCTOU fix must catch the race and refuse the write even "
        "though step 0's unlocked guard reported it safe"
    )
