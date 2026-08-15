"""
coordinator_core.ops.session.tests.test_stranded_baton_drain

Tests for C3 (docs/plans/2026-08-05-stranded-baton-drainage-make-the-detecto.md):
session.boot_sweep._sweep_consumed_handoffs' new stranded-baton late-supersede
drain — `_stranded_supersede_candidates` (candidate selection) and
`_drain_stranded_predecessors` (dispatch), exercised end-to-end via the
session.boot_sweep._handler composite op.

Coverage:
  (a) exactly-one candidate successor -> the predecessor is superseded
      (deployment_state: continued, continued_into set) AND archived.
  (b) more than one candidate successor -> fails closed; predecessor
      untouched, still non-terminal, still under state/handoffs/.
  (c) kind: roadmap-baton predecessor -> declined; untouched even though it
      is otherwise an exactly-one-candidate STRANDED shape.
  (d) already-superseded (second boot pass) -> no-op; idempotent, no crash,
      no duplicate mutation (the predecessor is gone from state/handoffs/
      after the first pass, so the second pass selects no candidates at all).
  (e) a live (non-terminal, non-archived) successor present -> HELD, not
      STRANDED; the predecessor is left completely untouched.

(a)/(d) exercise the full dispatch path (git repo, real supersede + archival
move) via session.boot_sweep._handler. (b)/(c)/(e) are pinned at the pure
candidate-selection layer (_stranded_supersede_candidates) — no mutation is
ever attempted for these three shapes, so there is nothing for a git-backed
integration test to additionally prove.

Spec backlink: pln-stranded-baton-drainage-make-t-f4a679 § C3

Negative-spec:
  - Does NOT re-test baton_drift_sweep.py's own STRANDED/HELD/TIP classification
    matrix (coordinator_core/ops/test_baton_drift_sweep.py already owns that) —
    this file only pins the exactly-one-candidate NARROWING this drain adds on
    top of it, and the dispatch/idempotency/commit-fold behavior around it.
  - Does NOT touch baton_drift_sweep.py or its own test file (out of scope for
    this chunk).
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — ops/__init__.py triggers all op registrations
import coordinator_core.ops.session.boot_sweep  # noqa: F401 — fires @register_op("session.boot_sweep")

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.archive_handoffs import _collect_all_handoff_paths
from coordinator_core.ops.session.boot_sweep import (
    _handler,
    _stranded_supersede_candidates,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "session.boot_sweep"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.session.boot_sweep @register_op did not fire"
)

_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# (b)/(c)/(e) — pure candidate-selection fixtures, no git repo needed.
# Mirrors coordinator_core/ops/test_baton_drift_sweep.py's own _write_handoff
# helper (this drain's candidate selection composes that module's tested
# primitives directly — see _stranded_supersede_candidates' own docstring).
# ---------------------------------------------------------------------------


def _write_handoff(
    path: Path,
    *,
    predecessor: Optional[str] = "none",
    status: Optional[str] = None,
    deployment_state: Optional[str] = None,
    kind: Optional[str] = None,
) -> None:
    lines = ["---"]
    if predecessor is not None:
        lines.append(f"predecessor: {predecessor}")
    if status is not None:
        lines.append(f"status: {status}")
    if deployment_state is not None:
        lines.append(f"deployment_state: {deployment_state}")
    if kind is not None:
        lines.append(f"kind: {kind}")
    lines.append("---")
    lines.append("# handoff")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_more_than_one_candidate_successor_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child1 = root / "archive" / "handoffs" / "2026-07" / "child1.md"
    child2 = root / "archive" / "handoffs" / "2026-06" / "child2.md"
    _write_handoff(parent, predecessor="none", status="claimed", deployment_state="in_flight")
    _write_handoff(child1, predecessor="parent.md")
    _write_handoff(child2, predecessor="parent.md")

    dag_index = _collect_all_handoff_paths(root)
    candidates = _stranded_supersede_candidates(root, dag_index)

    assert candidates == [], (
        "more than one candidate successor must never be swept — fail closed, "
        f"got {candidates!r}"
    )


def test_roadmap_baton_kind_is_declined(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "archive" / "handoffs" / "2026-07" / "child.md"
    _write_handoff(
        parent, predecessor="none", status="claimed", deployment_state="in_flight",
        kind="roadmap-baton",
    )
    _write_handoff(child, predecessor="parent.md")

    dag_index = _collect_all_handoff_paths(root)
    candidates = _stranded_supersede_candidates(root, dag_index)

    assert candidates == [], (
        "kind: roadmap-baton must be declined by design, even in an otherwise "
        f"exactly-one-candidate STRANDED shape; got {candidates!r}"
    )


def test_live_successor_present_is_held_not_swept(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "state" / "handoffs" / "child.md"  # live, not archived, non-terminal
    _write_handoff(parent, predecessor="none", status="claimed", deployment_state="in_flight")
    _write_handoff(child, predecessor="parent.md")

    dag_index = _collect_all_handoff_paths(root)
    candidates = _stranded_supersede_candidates(root, dag_index)

    assert candidates == [], (
        f"a live successor makes this HELD, not STRANDED — must never be swept; "
        f"got {candidates!r}"
    )


def test_exactly_one_terminal_referencer_is_the_only_selected_shape(tmp_path: Path) -> None:
    """Positive control for the two negative tests above: the SAME shape minus
    the ambiguity/decline/liveness twist IS selected — proves the negatives
    above are excluded by their own named condition, not by some unrelated
    fixture mistake."""
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "archive" / "handoffs" / "2026-07" / "child.md"
    _write_handoff(parent, predecessor="none", status="claimed", deployment_state="in_flight")
    _write_handoff(child, predecessor="parent.md")

    dag_index = _collect_all_handoff_paths(root)
    candidates = _stranded_supersede_candidates(root, dag_index)

    assert candidates == [(parent.resolve(), child.resolve())]


# ---------------------------------------------------------------------------
# (a)/(d) — full dispatch path: real git repo, session.boot_sweep._handler.
# Self-contained fixture per the session test package convention (see
# test_sweep_consumed_handoffs.py's own docstring on this) — not shared with
# test_boot_sweep.py's BootRepo.
# ---------------------------------------------------------------------------


class StrandedDrainRepo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        args_list = list(args)
        if (
            len(args_list) >= 3
            and args_list[0] == "commit"
            and args_list[1] == "-m"
            and "Session-Id:" not in args_list[2]
        ):
            args_list[2] = f"{args_list[2]}\n\nSession-Id: {_DEFAULT_TEST_SESSION_ID}"
        return subprocess.run(
            ["git"] + args_list, cwd=str(self.root), capture_output=True, check=True,
        )

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root), capture_output=True, check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    def seed_predecessor(
        self,
        name: str,
        *,
        status: str = "claimed",
        deployment_state: Optional[str] = "in_flight",
        kind: Optional[str] = None,
    ) -> Path:
        """Full schema-valid state/handoffs/<name> — this file IS re-validated
        by handoff.archive_transition's supersede mutation, so it needs the
        full required set (title/created/branch/status/predecessor), unlike
        the successor helper below."""
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f'title: "Test Predecessor {name}"',
            "created: 2026-01-01",
            "branch: work/test/2026-01-01",
            f"status: {status}",
            'predecessor: "none"',
        ]
        if deployment_state is not None:
            lines.append(f"deployment_state: {deployment_state}")
        if kind is not None:
            lines.append(f"kind: {kind}")
        fm_block = "\n".join(lines)
        content = f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add predecessor {name}")
        return path

    def seed_archived_successor(self, name: str, predecessor: str, yyyymm: str = "2026-07") -> Path:
        """Minimal (not schema-validated by any call site) archive/handoffs/
        <yyyymm>/<name> naming `predecessor` via its filename."""
        path = self.root / "archive" / "handoffs" / yyyymm / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"---\npredecessor: {predecessor}\n---\n\n# handoff\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add archived successor {name}")
        return path

    def path_exists(self, repo_rel: str) -> bool:
        return (self.root / repo_rel).exists()

    def git_status_clean(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(self.root), capture_output=True,
        )
        return result.stdout.strip() == b""


@pytest.fixture
def stranded_repo(tmp_path) -> StrandedDrainRepo:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git"] + list(args), cwd=str(repo_root), capture_output=True, check=True)

    _git("init", "-b", "main")
    _git("config", "user.email", "stranded-drain-test@claude-klabauter.test")
    _git("config", "user.name", "Stranded Drain Test")
    _git("config", "commit.gpgsign", "false")

    for d in ("state/handoffs", "archive/handoffs"):
        (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / d / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return StrandedDrainRepo(repo_root)


def test_exactly_one_candidate_is_superseded_and_archived(stranded_repo):
    """(a): the predecessor gets deployment_state:continued + continued_into,
    AND is git-mv'd into archive/handoffs/ — d6 running late instead of never."""
    stranded_repo.seed_predecessor("2026-07-01-parent.md")
    stranded_repo.seed_archived_successor("child.md", predecessor="2026-07-01-parent.md")

    result = _run(_handler({}, repo_root=stranded_repo.common_dir))

    assert result["exit_code"] in (0, 2), f"unexpected result: {result!r}"
    assert not stranded_repo.path_exists("state/handoffs/2026-07-01-parent.md"), (
        "superseded-and-cleared predecessor must be moved out of state/handoffs/"
    )

    archived_path = stranded_repo.root / "archive" / "handoffs" / "2026-07" / "2026-07-01-parent.md"
    assert archived_path.exists(), f"expected archive destination missing: {archived_path}"
    archived_text = archived_path.read_text(encoding="utf-8")
    assert "deployment_state: continued" in archived_text
    assert "continued_into:" in archived_text
    assert stranded_repo.git_status_clean()


def test_second_boot_pass_is_a_clean_no_op(stranded_repo):
    """(d): idempotent — running the drain twice never re-mutates or crashes.
    After the first pass the predecessor is gone from state/handoffs/, so the
    second pass's own candidate selection finds nothing to act on at all
    (the terminal deployment_state:continued it now carries excludes it from
    ever being re-selected — see _stranded_supersede_candidates' own
    docstring)."""
    stranded_repo.seed_predecessor("2026-07-01-parent.md")
    stranded_repo.seed_archived_successor("child.md", predecessor="2026-07-01-parent.md")

    first = _run(_handler({}, repo_root=stranded_repo.common_dir))
    assert first["exit_code"] in (0, 2)

    archived_path = stranded_repo.root / "archive" / "handoffs" / "2026-07" / "2026-07-01-parent.md"
    assert archived_path.exists()
    text_after_first = archived_path.read_text(encoding="utf-8")

    second = _run(_handler({}, repo_root=stranded_repo.common_dir))
    assert second["exit_code"] in (0, 2), f"second pass must not error: {second!r}"

    assert stranded_repo.git_status_clean(), "second pass must never leave a dirty tree"
    assert archived_path.exists()
    text_after_second = archived_path.read_text(encoding="utf-8")
    assert text_after_second == text_after_first, (
        "second pass must never re-mutate an already-superseded predecessor — "
        "it is not even a candidate any more (deployment_state:continued is "
        "itself the idempotency gate)"
    )
