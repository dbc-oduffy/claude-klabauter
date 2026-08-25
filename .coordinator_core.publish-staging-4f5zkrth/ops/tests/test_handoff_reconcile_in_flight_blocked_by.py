"""
coordinator_core.ops.tests.test_handoff_reconcile_in_flight_blocked_by

docs/research/2026-08-14-jgate-clearance-recording-seam.md — proof that
`handoff.reconcile_open` now retires structured `blocked_by` residue on a
pickup-claimed (`status: claimed`, `deployment_state: in_flight`) handoff,
which `_is_open` enumerates into `open_handoffs` but which the pre-existing
`_AWAITING_GATE_STATE`-keyed gate-cascade branch never reached (see that
research doc's verdict #5).

Source memo: cross-repo/inbox/2026-08-04-example-market-data-repo-em-pickup-
jgate-cleared-strands-gate-fields.md. EM ruling: this cleanup belongs to
`handoff.reconcile_open`, not `pickup_assemble` — this file proves the
former actually does it, end to end, over a real git fixture (`locked_rmw`
resolves its lock dir via `git rev-parse`, so a real repo is required, same
rationale as the sibling D1/close-terminal test files in this directory).

Import guard: `coordinator_core.ops.handoff_reconcile` MUST be imported at
module load time to fire `@register_op("handoff.reconcile_open")` — mirrors
every other op-test file's own import-guard precedent.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.ops.handoff_reconcile  # noqa: F401 — fires @register_op

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_reconcile import (
    _handler as _reconcile_handler,
    _handle_in_flight_blocked_by_retirement,
)

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_OP_NAME = "handoff.reconcile_open"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_reconcile @register_op did not fire"
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

    def abs_path(self, name: str) -> Path:
        return self.root / "state" / "handoffs" / name

    def seed_shipped_blocker(self, stub_id: str) -> Path:
        """A terminal blocker handoff (deployment_state: shipped) that
        `_blocker_clears_gate` resolves as evidence a `blocked_by` entry
        naming `stub_id` may retire."""
        name = f"{stub_id}.md"
        path = self.abs_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f'title: "Blocker {stub_id}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            "predecessor: null\n"
            f"stub_id: {stub_id}\n"
            "deployment_state: shipped\n"
            "---\n\n# Blocker\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add shipped blocker {stub_id}")
        return path

    def seed_terminal_blocker(self, stub_id: str, deployment_state: str) -> Path:
        """A TERMINAL-but-non-clearing blocker (closed/abandoned) — named in
        the docstring as explicitly non-clearing; distinct from the
        still-live `seed_unshipped_blocker` case."""
        name = f"{stub_id}.md"
        path = self.abs_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f'title: "Blocker {stub_id}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            "predecessor: null\n"
            f"stub_id: {stub_id}\n"
            f"deployment_state: {deployment_state}\n"
            "---\n\n# Blocker\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add {deployment_state} blocker {stub_id}")
        return path

    def seed_continued_blocker(self, stub_id: str, continued_into: str | None) -> Path:
        """A `continued` blocker — clears iff its `continued_into` chain
        resolves to `shipped`; an absent/empty successor is unresolvable."""
        name = f"{stub_id}.md"
        path = self.abs_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        continued_line = f"continued_into: {continued_into}\n" if continued_into else ""
        content = (
            "---\n"
            f'title: "Blocker {stub_id}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            "predecessor: null\n"
            f"stub_id: {stub_id}\n"
            "deployment_state: continued\n"
            f"{continued_line}"
            "---\n\n# Blocker\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add continued blocker {stub_id}")
        return path

    def seed_duplicate_id_blocker(self, stub_id: str, suffix: str, deployment_state: str) -> Path:
        """A second, distinct handoff resolving the SAME stub_id — makes the
        id ambiguous (`_resolve_blocker_deployment_state`'s duplicate-id
        guard), which must never clear."""
        name = f"{stub_id}-{suffix}.md"
        path = self.abs_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f'title: "Blocker {stub_id} ({suffix})"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            "predecessor: null\n"
            f"stub_id: {stub_id}\n"
            f"deployment_state: {deployment_state}\n"
            "---\n\n# Blocker\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add duplicate-id blocker {stub_id} ({suffix})")
        return path

    def seed_unshipped_blocker(self, stub_id: str) -> Path:
        """A live, non-terminal blocker — `_blocker_clears_gate` must never
        clear this one (evidence-based only, never age/prose)."""
        name = f"{stub_id}.md"
        path = self.abs_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f'title: "Blocker {stub_id}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            "predecessor: null\n"
            f"stub_id: {stub_id}\n"
            "deployment_state: open\n"
            "---\n\n# Blocker\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add unshipped blocker {stub_id}")
        return path

    def seed_in_flight(
        self,
        name: str,
        *,
        blocked_by: list,
        no_longer_blocked_by: list | None = None,
        blocking_notes: str | None = None,
        with_gate_evidence: bool = False,
        gate_evidence_legs: str | None = None,
    ) -> Path:
        """A pickup-claimed handoff: status=claimed, deployment_state=in_flight
        — the exact shape `_claim` always produces, and the shape `_is_open`
        admits into `open_handoffs` but the pre-fix gate-cascade branch never
        handled."""
        path = self.abs_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        blocked_by_line = "blocked_by: [" + ", ".join(blocked_by) + "]\n"
        no_longer_line = ""
        if no_longer_blocked_by is not None:
            no_longer_line = (
                "no_longer_blocked_by: ["
                + ", ".join(no_longer_blocked_by)
                + "]\n"
            )
        blocking_notes_line = f"blocking_notes: {blocking_notes!r}\n" if blocking_notes else ""
        if with_gate_evidence:
            legs = gate_evidence_legs if gate_evidence_legs is not None else "  legs: []\n"
            gate_evidence_block = "gate_evidence:\n" + legs
        else:
            gate_evidence_block = ""
        content = (
            "---\n"
            f'title: "Test Handoff {name}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: claimed\n"
            "predecessor: null\n"
            "claimed_by: test-session-001\n"
            "deployment_state: in_flight\n"
            f"{blocked_by_line}"
            f"{no_longer_line}"
            f"{blocking_notes_line}"
            f"{gate_evidence_block}"
            "---\n\n# Handoff\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add in_flight handoff {name}")
        return path


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
    _git("config", "user.email", "reconcile-in-flight-blocked-by-test@claude-klabauter.test")
    _git("config", "user.name", "reconcile-in-flight-blocked-by Test")
    _git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")
    return _Repo(root)


def _run(coro):
    return asyncio.run(coro)


def _reconcile(repo: _Repo, *, dry_run: bool) -> dict:
    # The reason is supplied in BOTH directions, not just for dry_run=False.
    # `_resolve_dry_run` makes the LOADED POLICY the sole source of truth, and
    # `policy_loader._resolve_policy_path` discovers a repo-resident
    # `auto-reconcile-policy.local.yaml` by walking up from the process cwd —
    # the real checkout's, never this fixture's. Claude-klabauter's own overlay
    # declares `dry_run: false`, so an unreasoned `dry_run=True` here was
    # REFUSED and the "preview" actually applied, making every assertion in
    # test_dry_run_previews_without_writing fail on this repo. These tests
    # assert an explicit posture in both directions and must name it in both.
    return _run(
        _reconcile_handler(
            {
                "dry_run": dry_run,
                "dry_run_override_reason": (
                    "test: prove in-flight blocked_by retirement — pinned against "
                    "the ambient repo policy so the fixture governs its own posture"
                ),
            },
            repo_root=repo.common_dir,
        )
    )


def _fm_dict(path: Path) -> dict:
    import yaml

    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return yaml.safe_load(split.fm_text) or {}


def test_claimed_in_flight_shipped_blocker_moves_not_deletes(repo):
    """AC core: a claimed+in_flight handoff whose blocked_by names a shipped
    blocker gets that entry MOVED into no_longer_blocked_by (never dropped
    from both), and deployment_state stays untouched (in_flight, not flipped)."""
    repo.seed_shipped_blocker("blocker-shipped-01")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-shipped.md", blocked_by=["blocker-shipped-01"],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0
    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
    ]
    assert any(
        e["handoff_id"] == "2026-01-01-in-flight-shipped" and e["applied"]
        for e in gate_entries
    ), f"expected an applied retirement entry, got {gate_entries!r}"

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == [], fm
    assert fm.get("no_longer_blocked_by") == ["blocker-shipped-01"], fm
    assert fm.get("deployment_state") == "in_flight", (
        "in-flight retirement must never flip deployment_state"
    )
    assert fm.get("status") == "claimed"


def test_union_invariant_holds_across_resolution(repo):
    """The union of blocked_by + no_longer_blocked_by is invariant across a
    resolution — same total membership, entries just migrate columns."""
    repo.seed_shipped_blocker("blocker-shipped-02")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-union.md", blocked_by=["blocker-shipped-02"],
    )
    before = _fm_dict(handoff_path)
    before_union = set(before.get("blocked_by") or []) | set(
        before.get("no_longer_blocked_by") or []
    )

    _reconcile(repo, dry_run=False)

    after = _fm_dict(handoff_path)
    after_union = set(after.get("blocked_by") or []) | set(
        after.get("no_longer_blocked_by") or []
    )
    assert after_union == before_union == {"blocker-shipped-02"}


def test_unshipped_blocker_left_untouched(repo):
    """Evidence-based only: an unresolved (still-open) blocker's id must stay
    in blocked_by — never retired on age, prose, or absence of evidence."""
    repo.seed_unshipped_blocker("blocker-unshipped-01")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-unshipped.md", blocked_by=["blocker-unshipped-01"],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0
    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-unshipped"
    ]
    assert gate_entries == [], f"nothing should be retired, got {gate_entries!r}"

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == ["blocker-unshipped-01"]
    assert not fm.get("no_longer_blocked_by")


def test_dry_run_previews_without_writing(repo):
    """dry_run (the default) must compute the retirement candidate but never
    write it to disk."""
    repo.seed_shipped_blocker("blocker-shipped-03")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-dry-run.md", blocked_by=["blocker-shipped-03"],
    )
    before_text = handoff_path.read_text(encoding="utf-8")

    result = _reconcile(repo, dry_run=True)
    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-dry-run"
    ]
    assert len(gate_entries) == 1
    assert gate_entries[0]["applied"] is False
    assert gate_entries[0]["blocker_ids"] == ["blocker-shipped-03"]

    after_text = handoff_path.read_text(encoding="utf-8")
    assert after_text == before_text, "dry_run must never write"


def test_awaiting_gate_path_unchanged_by_this_branch(repo):
    """Regression guard: an awaiting_gate handoff with a shipped structured
    blocker still routes through the pre-existing gate-cascade-clear verb
    (FLIP semantics — deployment_state -> ready_to_fire), not the new
    in-flight-only branch, and still records via the gate-cascade-clear
    action name, not blocked-by-retire-in-flight."""
    repo.seed_shipped_blocker("blocker-shipped-04")
    name = "2026-01-01-awaiting-gate.md"
    path = repo.abs_path(name)
    content = (
        "---\n"
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        "predecessor: null\n"
        "deployment_state: awaiting_gate\n"
        "blocked_by: [blocker-shipped-04]\n"
        "---\n\n# Handoff\n\nBody.\n"
    )
    path.write_text(content, encoding="utf-8")
    repo._git("add", str(path))
    repo._git("commit", "-m", "add awaiting_gate handoff")

    # gate_eval's compute-time CLEAR verdict (distinct from this branch's own
    # act-time `_blocker_clears_gate` evidence check) additionally requires
    # the shipped blocker to carry `shipped_in` — stamp it directly for this
    # regression fixture.
    blocker_path = repo.abs_path("blocker-shipped-04.md")
    blocker_text = blocker_path.read_text(encoding="utf-8")
    blocker_text = blocker_text.replace(
        "deployment_state: shipped\n",
        "deployment_state: shipped\nshipped_in: 0123456789abcdef0123456789abcdef01234567\n",
    )
    blocker_path.write_text(blocker_text, encoding="utf-8")
    repo._git("add", str(blocker_path))
    repo._git("commit", "-m", "stamp shipped_in on blocker")

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0
    entries = [
        e for e in result["gates_cleared"]
        if e.get("handoff_id") == "2026-01-01-awaiting-gate"
    ]
    assert len(entries) == 1
    assert entries[0]["action"] == "gate-cascade-clear"

    fm = _fm_dict(path)
    assert fm.get("deployment_state") == "ready_to_fire"
    assert fm.get("blocked_by") == []


def test_idempotent_second_armed_pass_is_a_true_no_op(repo):
    """Review: staff-eng Finding 9 — run armed twice; the second pass must
    record no second applied entry and leave the file bytes unchanged."""
    repo.seed_shipped_blocker("blocker-shipped-05")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-idempotent.md", blocked_by=["blocker-shipped-05"],
    )

    first = _reconcile(repo, dry_run=False)
    applied_first = [
        e for e in first["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-idempotent"
        and e.get("applied")
    ]
    assert len(applied_first) == 1
    after_first_text = handoff_path.read_text(encoding="utf-8")

    second = _reconcile(repo, dry_run=False)
    applied_second = [
        e for e in second["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-idempotent"
        and e.get("applied")
    ]
    assert applied_second == [], f"second pass must not re-apply, got {applied_second!r}"
    assert handoff_path.read_text(encoding="utf-8") == after_first_text, (
        "idempotent second pass must not change file bytes"
    )


def test_prepopulated_no_longer_blocked_by_dedupes_via_insert_branch(repo):
    """Review: staff-eng Finding 9 — `no_longer_blocked_by` pre-populated with
    an unrelated id exercises the `_replace` (field already present) branch
    and the dedupe guard, both untested before this fix."""
    repo.seed_shipped_blocker("blocker-shipped-06")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-prepop.md",
        blocked_by=["blocker-shipped-06"],
        no_longer_blocked_by=["blocker-already-retired-99"],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == []
    assert set(fm.get("no_longer_blocked_by") or []) == {
        "blocker-already-retired-99", "blocker-shipped-06",
    }


def test_mixed_batch_partial_retirement(repo):
    """Review: staff-eng Finding 9 — one handoff with both a shipped and an
    unshipped blocker proves PARTIAL retirement (only the clearing id moves)."""
    repo.seed_shipped_blocker("blocker-shipped-07")
    repo.seed_unshipped_blocker("blocker-unshipped-07")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-mixed.md",
        blocked_by=["blocker-shipped-07", "blocker-unshipped-07"],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == ["blocker-unshipped-07"]
    assert fm.get("no_longer_blocked_by") == ["blocker-shipped-07"]


@pytest.mark.parametrize("deployment_state", ["closed", "abandoned"])
def test_terminal_non_clearing_blocker_left_untouched(repo, deployment_state):
    """Review: staff-eng Finding 9 — closed/abandoned is the interesting
    non-clearing case because it IS terminal, unlike the still-live
    `unshipped` case already covered — must still never clear."""
    stub_id = f"blocker-{deployment_state}-01"
    repo.seed_terminal_blocker(stub_id, deployment_state)
    handoff_path = repo.seed_in_flight(
        f"2026-01-01-in-flight-{deployment_state}.md", blocked_by=[stub_id],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0
    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == f"2026-01-01-in-flight-{deployment_state}"
    ]
    assert gate_entries == [], f"nothing should be retired, got {gate_entries!r}"

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == [stub_id]
    assert not fm.get("no_longer_blocked_by")


def test_continued_chain_to_shipped_clears(repo):
    """Review: code-reviewer nit — the `continued` shape is unresolvable/
    non-clearing UNLESS the continued_into chain terminates at `shipped`;
    this is the one `_blocker_clears_gate` chain-walk case that DOES clear,
    proving the retirement branch actually reuses the shared chain-walk
    (not just the literal `shipped` check)."""
    repo.seed_shipped_blocker("blocker-shipped-chain-terminus")
    repo.seed_continued_blocker(
        "blocker-continued-01", continued_into="blocker-shipped-chain-terminus",
    )
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-continued.md", blocked_by=["blocker-continued-01"],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0
    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-continued"
    ]
    assert any(e["applied"] for e in gate_entries), gate_entries

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == []
    assert fm.get("no_longer_blocked_by") == ["blocker-continued-01"]


def test_continued_chain_with_no_successor_never_clears(repo):
    """`continued` with no continued_into successor is unresolvable — must
    never clear (distinct from the terminating-at-shipped case above)."""
    handoff_path_and_stub = "blocker-continued-dangling-01"
    repo.seed_continued_blocker(handoff_path_and_stub, continued_into=None)
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-continued-dangling.md",
        blocked_by=[handoff_path_and_stub],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0
    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-continued-dangling"
    ]
    assert gate_entries == [], f"nothing should be retired, got {gate_entries!r}"

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == [handoff_path_and_stub]
    assert not fm.get("no_longer_blocked_by")


def test_ambiguous_duplicate_id_blocker_never_clears(repo):
    """Two distinct handoffs resolving the same stub_id makes the id
    ambiguous (`_resolve_blocker_deployment_state`'s duplicate-id guard) —
    must never clear even though one of the duplicates is `shipped`."""
    stub_id = "blocker-ambiguous-01"
    repo.seed_duplicate_id_blocker(stub_id, "a", "shipped")
    repo.seed_duplicate_id_blocker(stub_id, "b", "open")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-ambiguous.md", blocked_by=[stub_id],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0
    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-ambiguous"
    ]
    assert gate_entries == [], f"ambiguous id must never clear, got {gate_entries!r}"

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == [stub_id]
    assert not fm.get("no_longer_blocked_by")


def test_deleted_blocker_never_clears(repo):
    """A `blocked_by` id naming a blocker that resolves nowhere (never
    seeded / already removed) is unresolvable — must never clear."""
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-deleted-blocker.md",
        blocked_by=["blocker-never-existed-01"],
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0
    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-deleted-blocker"
    ]
    assert gate_entries == [], f"unresolvable id must never clear, got {gate_entries!r}"

    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == ["blocker-never-existed-01"]
    assert not fm.get("no_longer_blocked_by")


def test_concurrent_retirement_no_op_branch_in_mutate(repo):
    """Review: code-reviewer nit — exercises the `if not live_retiring:`
    no-op branch inside the `mutate` closure directly: a caller passes an
    enumeration-time candidate that a concurrent writer already retired on
    disk before this call's lock acquisition. Calls
    `_handle_in_flight_blocked_by_retirement` directly (bypassing the full
    handler) to construct that race precisely."""
    repo.seed_shipped_blocker("blocker-shipped-race-01")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-race.md", blocked_by=["blocker-shipped-race-01"],
    )
    handoff = {
        "id": "2026-01-01-in-flight-race",
        "blocked_by": ["blocker-shipped-race-01"],
        "_path": str(handoff_path),
    }

    # Simulate a concurrent second writer that already moved the blocker
    # from blocked_by -> no_longer_blocked_by between enumeration and now.
    text = handoff_path.read_text(encoding="utf-8")
    text = text.replace(
        "blocked_by: [blocker-shipped-race-01]\n",
        "blocked_by: []\nno_longer_blocked_by: [blocker-shipped-race-01]\n",
    )
    handoff_path.write_text(text, encoding="utf-8")
    repo._git("add", str(handoff_path))
    repo._git("commit", "-m", "simulate concurrent retirement")

    gates_cleared: list = []
    surfaced: list = []
    intercepted = _run(_handle_in_flight_blocked_by_retirement(
        handoff, repo.root, repo.common_dir, False, gates_cleared, surfaced,
    ))

    assert intercepted is True
    entries = [e for e in gates_cleared if e.get("handoff_id") == handoff["id"]]
    assert len(entries) == 1, entries
    assert entries[0]["applied"] is False
    assert entries[0]["blocker_ids"] == []
    assert "no-op" in entries[0]["message"]

    after_text = handoff_path.read_text(encoding="utf-8")
    fm = _fm_dict(handoff_path)
    assert fm.get("blocked_by") == []
    assert fm.get("no_longer_blocked_by") == ["blocker-shipped-race-01"]
    assert after_text == text, "the no-op path must not rewrite the file"


def test_gate_evidence_surface_carries_leg_detail(repo):
    """Review: code-reviewer nit — the surfaced entry's `evidence` field must
    carry the actual gate_evidence legs, not a hardcoded empty list, so an
    operator can see WHAT evidence blocked the auto-retire."""
    repo.seed_shipped_blocker("blocker-shipped-legs-01")
    repo.seed_in_flight(
        "2026-01-01-in-flight-evidence-legs.md",
        blocked_by=["blocker-shipped-legs-01"],
        with_gate_evidence=True,
        gate_evidence_legs=(
            "  covers_prose: false\n"
            "  legs:\n"
            "    - kind: human\n"
            "      reason: manual review pending\n"
        ),
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0

    surfaced_entries = [
        e for e in result["surfaced"]
        if e.get("handoff_id") == "2026-01-01-in-flight-evidence-legs"
    ]
    assert len(surfaced_entries) == 1, result["surfaced"]
    evidence = surfaced_entries[0]["evidence"]
    assert evidence == [{"kind": "human", "reason": "manual review pending"}], evidence


def test_blocking_notes_untouched(repo):
    """Review: staff-eng Finding 9 — an explicit dispatch constraint
    (blocking_notes is advisory prose the resolver never reads) asserted
    nowhere before this fix."""
    repo.seed_shipped_blocker("blocker-shipped-08")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-notes.md",
        blocked_by=["blocker-shipped-08"],
        blocking_notes="do not touch this prose",
    )

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0

    fm = _fm_dict(handoff_path)
    assert fm.get("blocking_notes") == "do not touch this prose"


def test_gate_evidence_present_surfaces_instead_of_auto_retiring(repo):
    """Review: staff-eng Finding 4 (EM ruling) — an in_flight handoff
    carrying a gate_evidence block must surface rather than auto-retire,
    even with a structurally-shipped blocker and armed (dry_run=False),
    mirroring the awaiting_gate path's own surface-only invariant."""
    repo.seed_shipped_blocker("blocker-shipped-09")
    handoff_path = repo.seed_in_flight(
        "2026-01-01-in-flight-evidence.md",
        blocked_by=["blocker-shipped-09"],
        with_gate_evidence=True,
    )
    before_text = handoff_path.read_text(encoding="utf-8")

    result = _reconcile(repo, dry_run=False)
    assert result["exit_code"] == 0

    gate_entries = [
        e for e in result["gates_cleared"]
        if e.get("action") == "blocked-by-retire-in-flight"
        and e.get("handoff_id") == "2026-01-01-in-flight-evidence"
    ]
    assert gate_entries == [], f"must not auto-retire while evidence present, got {gate_entries!r}"

    surfaced_entries = [
        e for e in result["surfaced"]
        if e.get("handoff_id") == "2026-01-01-in-flight-evidence"
    ]
    assert len(surfaced_entries) == 1, f"expected a surfaced entry, got {result['surfaced']!r}"
    assert "gate_evidence" in surfaced_entries[0]["reason"]

    assert handoff_path.read_text(encoding="utf-8") == before_text, (
        "must never write while gate_evidence is present, armed or not"
    )
