"""
coordinator_core.ops.tests.test_cockpit_ground_truth_regression — the C11 regression
fixture (docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C11, AC9, AC6i).

Purpose: reproduce example-cockpit-repo-em's four written-down ground-truth instances from
their own described shape, and pin the two obligations R1/AC6i attach to this fixture
beyond "reproduces the bug":

  1. Both cascade triggers (a plan reaching `status: implemented`, and a handoff
     concluding terminally-positive) are proved to call the SAME shared entrypoint
     (`deliverable.cascade_terminal` — `deliverable_cascade._handler`), by object
     identity AND by driving each trigger's own real call path — not merely by
     observing that their outcomes agree, which would pass against two independent
     implementations that happen to concur (the exact thing AC6i rules out).
  2. The negative direction: a plan stamp must not advance an unjoined handoff, and
     the read-only backstop sweep (`deliverable.cascade_backstop_sweep`, C6c) must
     never write, even when it detects a real divergence.

SCOPE (finding 8 correction): this file is the cockpit-ground-truth regression fixture
ONLY. C5/C6/C6g/C6b/C6c/C7 each author their own tests; nothing here duplicates them —
C6's own `test_deliverable_cascade.py` already covers the op's happy path, per-target
predicate, and op-level idempotency; C7's own `test_handoff_transition.py` already
covers `_unclaim`'s completeness-check refusal in isolation. This file's job is the
END-TO-END ground-truth shape driven through the REAL trigger call sites
(`plan_status_transition.main()`, `post_commit_tail._run_deliverable_cascade`,
`handoff_transition._handler` verb="unclaim"), plus the two obligations above.

Ground-truth shapes (from cockpit's own report, per C11's body):
  Shape A — a handoff at `status: open` / `deployment_state: ready_to_fire` /
    `pickup_ready: true` whose plan target carries `status: implemented`. Two of
    cockpit's four instances match this shape: the cascade never fired for them
    (they predate C6), so they sat stranded advertising live work against an
    already-shipped plan.
  Shape B — the `in_flight`-through-`drop` variant that produced cockpit's gsd-15
    strand: a handoff claimed (`status: claimed`, `deployment_state: in_flight`)
    against a plan already `implemented`; dropping it (the `unclaim` verb) used to
    silently reset it to `open`/`ready_to_fire` with no record the plan had already
    shipped. The other two of cockpit's four instances match this shape.

FAIL-BEFORE-C7 / PASS-AFTER-C7 demonstration (per dispatch instructions): C7's guard
(`handoff_transition._find_implemented_governing_plan`) is already landed on this tree
and must not be reverted, stashed, or commented out — this file demonstrates the
before/after property with a narrowly-scoped monkeypatch of that ONE guard function,
applied only around the "before" assertion in
`TestGroundTruthShapeB.test_drop_variant_fails_before_c7_guard_passes_after`, restored
before the "after" assertion runs. Shape A's before/after property (the cascade wiring
itself, C6) is demonstrated the same way against `plan_status_transition._run_cascade`
in `TestGroundTruthShapeA.test_ready_to_fire_variant_fails_before_cascade_passes_after`.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_cockpit_ground_truth_regression.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_children  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.handoff_transition as handoff_transition_mod
import coordinator_core.ops.plan_status_transition as plan_status_transition_mod
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import _REGISTRY, get_op_handler
from coordinator_core.ops.cascade_backstop_sweep import _handler as _backstop_handler
from coordinator_core.ops.ceremony.post_commit_tail import (
    OP_DELIVERABLE_CASCADE,
    _run_deliverable_cascade,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.handoff_transition import _handler as _handoff_handler

# ---------------------------------------------------------------------------
# Git/session harness — mirrors test_deliverable_cascade.py's own `_git`/
# `_seed_handoff` idiom (not the shared `handoff_repo` conftest fixture): the
# shape-A path routes through `archive_stamp.stamp_shipped_in`'s ownership
# guard, which requires a commit carrying a `Session-Id:` trailer matching the
# calling session's own id — a per-commit knob `handoff_repo` does not expose.
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_DEFAULT_TEST_SESSION_ID = "22222222-2222-2222-2222-222222222222"


def _git(repo: Path, *args: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID) -> subprocess.CompletedProcess:
    args_list = list(args)
    if (
        len(args_list) >= 3
        and args_list[0] == "commit"
        and args_list[1] == "-m"
        and session_id is not None
        and "Session-Id:" not in args_list[2]
    ):
        args_list[2] = f"{args_list[2]}\n\nSession-Id: {session_id}"
    return subprocess.run(
        ["git", "-C", str(repo), *args_list],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    status: str = "open",
    deployment_state: str = "ready_to_fire",
    deliverable_id: str = "dlv-test-000000",
    predecessor: str = "none",
    extra: str = "",
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        f'predecessor: "{predecessor}"\n'
        f"deployment_state: {deployment_state}\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_plan(repo: Path, name: str, deliverable_id: str, status: str, title: str = "Ground Truth Plan") -> Path:
    """Write a docs/plans/<name>.md carrying deliverable_id + status. Read-only
    surface for both the cascade's plan-trigger scan and _unclaim's completeness
    check — never needs to be git-committed (both read straight off disk)."""
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    # UNQUOTED deliverable_id — `plan_status_transition._stamp_implemented` reads
    # it with the raw-text `read_fm_field` primitive and never unquotes it
    # (only `status` gets that treatment), so a quoted value here would be
    # passed to the cascade verbatim, quotes included, and fail the exact-string
    # join against `_seed_handoff`'s own unquoted `deliverable_id:` field.
    path.write_text(
        "---\n"
        f'title: "{title}"\n'
        f"status: {status}\n"
        f"deliverable_id: {deliverable_id}\n"
        "---\n\n# Plan\n\nBody.\n",
        encoding="utf-8",
    )
    return path


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


def _run(coro):
    return asyncio.run(coro)


def _run_cascade_direct(params: dict, repo_root: Path) -> dict:
    # Dereferences `cascade_mod._handler` fresh on every call (never the
    # module-level `_cascade_handler` alias captured at import time) so a
    # test that monkeypatches the attribute — proving no-re-entry via a
    # counting spy — actually exercises its own patched function.
    return asyncio.run(cascade_mod._handler(params, repo_root=repo_root))


# ---------------------------------------------------------------------------
# Shape A — ready_to_fire + pickup_ready:true handoff joined to an implemented plan
# ---------------------------------------------------------------------------


class TestGroundTruthShapeA:
    def test_ready_to_fire_variant_fails_before_cascade_passes_after(self, tmp_path, monkeypatch):
        """Reproduces cockpit's stranded-baton shape: status:open,
        deployment_state:ready_to_fire, pickup_ready:true, joined by deliverable_id
        to a plan that reaches status:implemented. BEFORE the cascade fires (this
        test's own monkeypatch of `plan_status_transition._run_cascade` stands in
        for the pre-C6 world — C6 is already landed and must not be reverted), the
        handoff sits stranded exactly as cockpit found it. AFTER (real code,
        unpatched), the SAME shape advances through the real `stamp-implemented`
        CLI trigger, with no manual step."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        # --- BEFORE: cascade wiring disabled for this one assertion only ---
        before_touched = repo / "coordinator" / "bin" / "before-widget.sh"
        before_touched.parent.mkdir(parents=True)
        before_touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(before_touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch before-widget")

        before_handoff = _seed_handoff(
            repo, "before.md",
            deliverable_id="dlv-optshape-a-before",
            extra=(
                "pickup_ready: true\n"
                f"scope:\n  - {before_touched.relative_to(repo)}\n"
            ),
        )
        before_plan = _seed_plan(repo, "before-plan.md", "dlv-optshape-a-before", "approved")

        def _disabled_cascade(plan_path, deliverable_id):  # noqa: ANN001 — matches _run_cascade's own signature
            return 0

        with pytest.MonkeyPatch.context() as before_mp:
            before_mp.setattr(plan_status_transition_mod, "_run_cascade", _disabled_cascade)
            exit_code = plan_status_transition_mod.main(["stamp-implemented", "--plan", str(before_plan)])

        assert exit_code == 0
        assert _fm_field(before_plan, "status") == "implemented"
        assert _fm_field(before_handoff, "deployment_state") == "ready_to_fire", (
            "reproduces the cockpit bug: with the cascade trigger disabled, a plan "
            "reaching implemented leaves its joined ready_to_fire handoff stranded"
        )
        assert _fm_field(before_handoff, "status") == "open"

        # --- AFTER: real, unpatched code — a fresh pair, same shape ---
        after_touched = repo / "coordinator" / "bin" / "after-widget.sh"
        after_touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(after_touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch after-widget")

        after_handoff = _seed_handoff(
            repo, "after.md",
            deliverable_id="dlv-optshape-a-after",
            extra=(
                "pickup_ready: true\n"
                f"scope:\n  - {after_touched.relative_to(repo)}\n"
            ),
        )
        after_plan = _seed_plan(repo, "after-plan.md", "dlv-optshape-a-after", "approved")

        exit_code = plan_status_transition_mod.main(["stamp-implemented", "--plan", str(after_plan)])

        assert exit_code == 0
        assert _fm_field(after_plan, "status") == "implemented"
        assert _fm_field(after_handoff, "deployment_state") == "shipped", (
            "the fix: the same shape now advances through the real stamp-implemented "
            "trigger, with no manual operator step"
        )
        assert _fm_field(after_handoff, "advanced_by") == "dlv-optshape-a-after"
        after_text_once_advanced = after_handoff.read_text(encoding="utf-8")

        # AC6i idempotency, asserted against the SAME shared entrypoint object
        # both triggers call (see TestSharedEntrypoint for the identity proof) —
        # a second invocation over the same join-closure writes nothing.
        second = _run_cascade_direct(
            {"deliverable_id": "dlv-optshape-a-after", "source_kind": "plan", "source_path": str(after_plan)},
            repo / ".git",
        )
        assert second["candidates_matched"] == 0
        assert second["advanced"] == []
        assert after_handoff.read_text(encoding="utf-8") == after_text_once_advanced


# ---------------------------------------------------------------------------
# Shape B — in_flight-through-drop, cockpit's gsd-15 strand
# ---------------------------------------------------------------------------


class TestGroundTruthShapeB:
    def test_drop_variant_fails_before_c7_guard_passes_after(self, handoff_repo, monkeypatch):
        """Reproduces cockpit's gsd-15 shape: a claimed, in_flight handoff whose
        governing plan (joined by deliverable_id) is already status:implemented.
        Dropping it (verb: unclaim) is the bug's trigger. C7's guard
        (`_find_implemented_governing_plan`) already landed on this tree and is
        NOT reverted — the before/after property is demonstrated with a narrowly
        scoped monkeypatch of that one guard function, restored before the
        "after" assertion."""
        did = "dlv-optshape-b"
        handoff_repo.seed_handoff(
            "gsd-15-style.md", "claimed",
            deployment_state="in_flight",
            claimed_at="2026-01-02T10:00:00Z",
            claimed_by="session-test-gsd15",
            extra=f'deliverable_id: "{did}"',
        )
        _seed_plan(handoff_repo.root, "gsd-15-plan.md", did, "implemented", title="gsd-15 Governing Plan")
        abs_path = handoff_repo.abs_path("gsd-15-style.md")

        # --- BEFORE: C7's guard disabled for this one assertion only ---
        with pytest.MonkeyPatch.context() as before_mp:
            before_mp.setattr(handoff_transition_mod, "_find_implemented_governing_plan", lambda *a, **k: None)
            before_result = _run(_handoff_handler(
                {"verb": "unclaim", "handoff_path": abs_path},
                repo_root=handoff_repo.common_dir,
            ))

        assert before_result["exit_code"] == 0
        assert before_result["applied"] is True
        assert read_fm_field(split_frontmatter(open(abs_path, encoding="utf-8").read()).fm_text, "deployment_state") == "ready_to_fire", (
            "reproduces the gsd-15 bug: with C7's guard disabled, drop silently "
            "resets an in_flight handoff whose plan is already implemented"
        )

        # --- AFTER: real, unpatched code — a fresh claimed pair, same shape ---
        handoff_repo.seed_handoff(
            "gsd-15-style-2.md", "claimed",
            deployment_state="in_flight",
            claimed_at="2026-01-02T10:00:00Z",
            claimed_by="session-test-gsd15-b",
            extra=f'deliverable_id: "{did}"',
        )
        abs_path_2 = handoff_repo.abs_path("gsd-15-style-2.md")
        original_2 = open(abs_path_2, encoding="utf-8").read()

        after_result = _run(_handoff_handler(
            {"verb": "unclaim", "handoff_path": abs_path_2},
            repo_root=handoff_repo.common_dir,
        ))

        assert after_result["exit_code"] == 1
        assert after_result["applied"] is False
        error_msg = after_result.get("error", "")
        assert "gsd-15 Governing Plan" in error_msg
        assert open(abs_path_2, encoding="utf-8").read() == original_2, (
            "the fix: real C7 guard refuses the drop, no write occurs"
        )


# ---------------------------------------------------------------------------
# AC6i — the SHARED entrypoint, both triggers, idempotency, no re-entry
# ---------------------------------------------------------------------------


class TestSharedEntrypoint:
    def test_both_triggers_resolve_the_identical_entrypoint_object(self):
        """The discriminator AC6i names explicitly: outcome-agreement passes
        against two independent implementations that happen to concur. Object
        identity does not — `plan_status_transition._run_cascade`'s own
        function-local import and `post_commit_tail`'s `get_op_handler`
        resolution must land on the literal same function object registered
        for "deliverable.cascade_terminal", not two functions that behave alike."""
        from coordinator_core.ops.deliverable_cascade import _handler as imported_by_trigger_one

        registered = get_op_handler(OP_DELIVERABLE_CASCADE)
        assert registered is not None
        assert registered is imported_by_trigger_one is cascade_mod._handler, (
            "trigger 1 (plan_status_transition) and trigger 2 (post_commit_tail, "
            "via get_op_handler) must resolve to ONE function object, not two "
            "implementations that happen to agree"
        )

    def test_trigger_two_fan_out_uses_the_same_registered_entrypoint(self, tmp_path):
        """Drives C6b's own real fan-out helper (`_run_deliverable_cascade`) with
        the handler `post_commit_tail.run()` itself would resolve via
        `get_op_handler` — proving trigger 2's REAL wiring reaches the SAME
        candidate-advance behaviour as trigger 1, through the identical
        entrypoint, without re-testing C6b's own full pipeline (its own test
        file already covers `run()` end to end)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        did = "dlv-optshape-a-trigger2"

        touched = repo / "coordinator" / "bin" / "trigger2-widget.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch trigger2-widget")

        # The handoff that "just concluded" (source_kind=handoff, per C6b) — already
        # shipped, is post_commit_tail's own `stamped` entry for this run.
        trigger_handoff = _seed_handoff(
            repo, "trigger2-source.md",
            deliverable_id=did,
            deployment_state="shipped",
            extra="shipped_in: deadbeef1234\n",
        )
        # The sibling still advertising live work, joined by the SAME deliverable_id.
        target_handoff = _seed_handoff(
            repo, "trigger2-target.md",
            deliverable_id=did,
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        resolved_handler = get_op_handler(OP_DELIVERABLE_CASCADE)
        result = asyncio.run(
            _run_deliverable_cascade(
                repo, repo / ".git",
                [str(trigger_handoff.relative_to(repo))],
                resolved_handler,
            )
        )

        assert result["failed"] == []
        assert str(target_handoff) in result["acted"] or any(
            "trigger2-target.md" in a for a in result["acted"]
        )
        assert _fm_field(target_handoff, "deployment_state") == "shipped"
        assert _fm_field(target_handoff, "advanced_by") == did
        # The self-advance guard (C6b) — trigger_handoff (the source itself) is
        # excluded from its own candidate set, never re-advanced by its own cascade.
        assert _fm_field(trigger_handoff, "advanced_by") is None

    def test_no_reentry_advancing_a_candidate_does_not_refire_the_entrypoint(self, tmp_path, monkeypatch):
        """An artifact advanced BY a cascade does not itself trigger one: fan out
        to two live candidates sharing one deliverable_id and assert the shared
        entrypoint is invoked exactly once for the whole closure, not once per
        artifact it goes on to advance."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        did = "dlv-optshape-a-noreentry"

        touched_1 = repo / "coordinator" / "bin" / "noreentry-1.sh"
        touched_1.parent.mkdir(parents=True)
        touched_1.write_text("#!/bin/sh\n", encoding="utf-8")
        touched_2 = repo / "coordinator" / "bin" / "noreentry-2.sh"
        touched_2.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched_1.relative_to(repo)), str(touched_2.relative_to(repo)))
        _git(repo, "commit", "-m", "touch noreentry widgets")

        candidate_a = _seed_handoff(
            repo, "noreentry-a.md",
            deliverable_id=did,
            extra=f"scope:\n  - {touched_1.relative_to(repo)}\n",
        )
        candidate_b = _seed_handoff(
            repo, "noreentry-b.md",
            deliverable_id=did,
            extra=f"scope:\n  - {touched_2.relative_to(repo)}\n",
        )

        original_handler = _REGISTRY[OP_DELIVERABLE_CASCADE]
        call_count = {"n": 0}

        async def _counting_spy(params, repo_root):
            call_count["n"] += 1
            return await original_handler(params, repo_root)

        monkeypatch.setitem(_REGISTRY, OP_DELIVERABLE_CASCADE, _counting_spy)
        monkeypatch.setattr(cascade_mod, "_handler", _counting_spy)

        result = _run_cascade_direct(
            {"deliverable_id": did, "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo / ".git",
        )

        assert len(result["advanced"]) == 2
        assert _fm_field(candidate_a, "deployment_state") == "shipped"
        assert _fm_field(candidate_b, "deployment_state") == "shipped"
        assert call_count["n"] == 1, (
            "advancing candidate_a/candidate_b must not itself re-fire the shared "
            "entrypoint — no re-entry"
        )


# ---------------------------------------------------------------------------
# Negative direction — where trust is earned or lost
# ---------------------------------------------------------------------------


class TestNegativeDirection:
    def test_plan_stamp_does_not_advance_an_unjoined_handoff(self, tmp_path):
        """A plan reaching implemented must NOT advance a handoff carrying a
        DIFFERENT deliverable_id — the join is exact-string, never fuzzy."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        touched = repo / "coordinator" / "bin" / "unjoined-widget.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch unjoined-widget")

        unjoined_handoff = _seed_handoff(
            repo, "unjoined.md",
            deliverable_id="dlv-unjoined-other",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        original_text = unjoined_handoff.read_text(encoding="utf-8")
        plan = _seed_plan(repo, "unjoined-plan.md", "dlv-optshape-unjoined-plan-side", "approved")

        exit_code = plan_status_transition_mod.main(["stamp-implemented", "--plan", str(plan)])

        assert exit_code == 2, "plan flips, but the cascade legitimately resolves no downstream artifact"
        assert _fm_field(plan, "status") == "implemented"
        assert unjoined_handoff.read_text(encoding="utf-8") == original_text, (
            "an unjoined handoff must be byte-identical after a sibling plan's stamp"
        )
        assert _fm_field(unjoined_handoff, "deployment_state") == "ready_to_fire"

    def test_backstop_sweep_never_writes_even_when_it_finds_a_divergence(self, tmp_path):
        """The backstop sweep (deliverable.cascade_backstop_sweep, C6c) is the
        REPORT-ONLY half of R1's design — it must find cockpit's shape-A
        divergence (a live handoff C6's own predicate would clear, joined to a
        plan already on disk as implemented) and must not write a single byte
        while doing so. Anti-scope's second bullet draws this exact line for
        the existing sweep-shipped-handoffs mechanism; C6c's own module
        docstring draws it for itself as a HARD BOUNDARY — this asserts the
        boundary holds against cockpit's own bug shape."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        did = "dlv-optshape-a-backstop"

        stranded_handoff = _seed_handoff(
            repo, "backstop-stranded.md",
            deliverable_id=did,
        )
        _seed_plan(repo, "backstop-plan.md", did, "implemented")
        original_text = stranded_handoff.read_text(encoding="utf-8")

        result = asyncio.run(_backstop_handler({}, repo_root=repo / ".git"))

        assert result["exit_code"] == 0
        divergence_dids = {d["deliverable_id"] for d in result["divergences"]}
        assert did in divergence_dids, "the sweep must SEE cockpit's shape-A divergence"
        assert stranded_handoff.read_text(encoding="utf-8") == original_text, (
            "the sweep must never write, even for a divergence it correctly reports"
        )
        assert _fm_field(stranded_handoff, "deployment_state") == "ready_to_fire"
