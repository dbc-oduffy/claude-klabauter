"""
coordinator_core.ops.tests.test_deliverable_cascade — pytest for the
"deliverable.cascade_terminal" op (C6: docs/plans/2026-08-04-terminal-state-propagation-join-keys.md).

Self-contained git-repo harness (mirrors coordinator_core/test_archive_stamp.py's own
`_git`/`_seed_handoff` idiom) rather than the shared `handoff_repo` conftest fixture — this
op's happy path routes through `archive_stamp.stamp_shipped_in`'s ownership guard, which
requires a commit carrying a `Session-Id:` trailer matching the calling session's own id
(`CLAUDE_SESSION_ID`), a per-commit knob `handoff_repo` does not expose.

Run (from repo root): python3 -m pytest coordinator_core/ops/tests/test_deliverable_cascade.py -q
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
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op side effect
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

_handler = cascade_mod._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _git(
    repo: Path, *args: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID
) -> subprocess.CompletedProcess:
    """Mirrors test_archive_stamp.py's own `_git` helper — appends a `Session-Id:`
    trailer to every `commit -m` call so `stamp_shipped_in`'s ownership guard resolves
    the commit as the calling session's own (see module docstring)."""
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


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


class TestHappyPath:
    def test_advances_a_live_ready_to_fire_candidate(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget")

        hp = _seed_handoff(
            repo, "h1.md",
            deliverable_id="dlv-abc-111111",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )

        result = _run(
            {"deliverable_id": "dlv-abc-111111", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 0
        assert result["candidates_matched"] == 1
        assert len(result["advanced"]) == 1
        assert result["refused"] == []
        assert _fm_field(hp, "deployment_state") == "shipped"
        assert _fm_field(hp, "shipped_in") is not None
        assert _fm_field(hp, "advanced_by") == "dlv-abc-111111"
        assert _fm_field(hp, "advanced_at") is not None

    def test_idempotent_second_run_writes_nothing_new(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        touched = repo / "coordinator" / "bin" / "widget.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "touch widget")

        hp = _seed_handoff(
            repo, "h1.md",
            deliverable_id="dlv-abc-222222",
            extra=f"scope:\n  - {touched.relative_to(repo)}\n",
        )
        params = {"deliverable_id": "dlv-abc-222222", "source_kind": "plan", "source_path": "docs/plans/p.md"}

        first = _run(params, repo_root=repo / ".git")
        assert first["exit_code"] == 0
        assert len(first["advanced"]) == 1
        text_after_first = hp.read_text(encoding="utf-8")

        second = _run(params, repo_root=repo / ".git")
        # AC6i: the candidate is now deployment_state:shipped (terminal), so the
        # second pass's own scan excludes it — zero candidates, not a re-write.
        assert second["candidates_matched"] == 0
        assert second["advanced"] == []
        assert hp.read_text(encoding="utf-8") == text_after_first


class TestZeroCandidatesIsLoud:
    def test_no_matching_handoff_is_exit_1(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "state" / "handoffs").mkdir(parents=True)

        result = _run(
            {"deliverable_id": "dlv-nothing-here", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["candidates_matched"] == 0
        assert result["advanced"] == []
        assert "dlv-nothing-here" in result["error"]


class TestPerTargetPredicate:
    def test_awaiting_gate_is_refused_not_flipped(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "h2.md",
            deliverable_id="dlv-gate-333333",
            deployment_state="awaiting_gate",
            extra="gate_dependency: some-subsystem\n",
        )

        result = _run(
            {"deliverable_id": "dlv-gate-333333", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["candidates_matched"] == 1
        assert result["advanced"] == []
        assert len(result["refused"]) == 1
        assert "not consistent with terminal" in result["refused"][0]["reason"]
        assert _fm_field(hp, "deployment_state") == "awaiting_gate"

    def test_claimed_by_live_session_is_refused(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "h3.md",
            deliverable_id="dlv-claim-444444",
            status="claimed",
            deployment_state="in_flight",
            extra="claimed_by: sess-live-1234\n",
        )
        monkeypatch.setattr(
            cascade_mod, "resolve_live_session_ids", lambda: frozenset({"sess-live-1234"})
        )

        result = _run(
            {"deliverable_id": "dlv-claim-444444", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["advanced"] == []
        assert len(result["refused"]) == 1
        assert "claimed by live session" in result["refused"][0]["reason"]
        assert _fm_field(hp, "deployment_state") == "in_flight"

    def test_leg_b_dispatch_narrows_edge_kinds_so_a_live_spinoff_does_not_block_the_advance(
        self, tmp_path, monkeypatch
    ):
        """`_predicate_refusal`'s leg (b) must not inherit `handoff.has_live_children`'s
        archival-shaped default edge set. `forked_from` is the spinoff edge — a live
        spinoff is a niece, not a descendant, and must not refuse this cascade's
        conclusion-shaped advance (example-cockpit-repo-em, 2026-08-05, cross-repo/inbox/
        2026-08-05-example-cockpit-repo-em-wsc-leg-b-counts-spinoffs-as-live-children.md).
        Asserted on the params actually handed to the op, mirroring
        workstream_complete's own `test_leg_b_dispatch_narrows_edge_kinds_so_a_live_
        spinoff_does_not_block_the_close`.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "spinoff-parent.md", deliverable_id="dlv-spinoff-777099")

        captured: dict = {}

        async def _fake_handler(params, repo_root):
            captured.update(params)
            return {"exit_code": 1, "referenced": False}

        import coordinator_core.ops.handoff_children as hc_mod

        monkeypatch.setattr(hc_mod, "_handoff_has_live_children", _fake_handler)

        result = _run(
            {"deliverable_id": "dlv-spinoff-777099", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1  # no scope evidence -- refused on a later leg, harmless here
        assert "edge_kinds" in captured
        edge_kinds = {k.strip() for k in captured["edge_kinds"].split(",")}
        assert edge_kinds == {"predecessor", "additional_predecessors"}
        assert "forked_from" not in edge_kinds
        assert _fm_field(hp, "deployment_state") == "ready_to_fire"

    def test_live_successor_is_refused(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        parent = _seed_handoff(
            repo, "parent.md",
            deliverable_id="dlv-succ-555555",
            deployment_state="in_flight",
        )
        # Child names `parent.md` as its predecessor — reverse_membership sees the
        # parent as still a live merge-parent (mirrors test_archive_stamp.py's own
        # _seed_handoff_with_predecessor idiom).
        child = repo / "state" / "handoffs" / "child.md"
        child.write_text(
            "---\n"
            'title: "Child"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "parent.md"\n'
            "deployment_state: ready_to_fire\n"
            "---\n\n# Handoff\n\nBody.\n",
            encoding="utf-8",
        )
        _git(repo, "add", "state/handoffs/child.md")
        _git(repo, "commit", "-m", "add child")

        result = _run(
            {"deliverable_id": "dlv-succ-555555", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["advanced"] == []
        assert len(result["refused"]) == 1
        assert "live successor" in result["refused"][0]["reason"]
        assert _fm_field(parent, "deployment_state") == "in_flight"


class TestNoCommitEvidence:
    def test_no_scope_paths_is_refused_not_flipped(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h4.md", deliverable_id="dlv-noscope-666666")

        result = _run(
            {"deliverable_id": "dlv-noscope-666666", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["advanced"] == []
        assert len(result["refused"]) == 1
        assert "no commit evidence" in result["refused"][0]["reason"]
        assert _fm_field(hp, "deployment_state") == "ready_to_fire"


def _touch_scope_target(repo: Path, rel: str = "coordinator/bin/widget.sh") -> str:
    """Commit a scope-derivable file, mirroring TestHappyPath's own idiom, and
    return its repo-relative path string for use in a handoff's `scope:` list."""
    touched = repo / rel
    touched.parent.mkdir(parents=True, exist_ok=True)
    touched.write_text("#!/bin/sh\n", encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", f"touch {rel}")
    return rel


def _git_dated(
    repo: Path, msg: str, committer_date_iso: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID
) -> subprocess.CompletedProcess:
    """Mirrors `_git`'s Session-Id-trailer auto-append, but pins the author AND
    committer date via env vars, for deterministic `not_after`/postdate
    testing (see `coordinator_core.test_archive_stamp`'s own `_git_dated` for
    the same idiom against the lower-level `stamp_shipped_in` directly)."""
    msg_full = msg
    if session_id is not None and "Session-Id:" not in msg:
        msg_full = f"{msg}\n\nSession-Id: {session_id}"
    env = {
        **_GIT_ENV,
        "GIT_AUTHOR_DATE": committer_date_iso,
        "GIT_COMMITTER_DATE": committer_date_iso,
    }
    return subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", msg_full],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


class TestSourceDerivedEvidence:
    """(2026-08-04) The false negative this pass closes: a baton whose OWN
    `scope:` paths resolve to no commit (or carry none at all) is no longer
    stuck refusing forever when the artifact that FIRED this cascade —
    `source_path` — plainly has a ship commit of its own."""

    def test_no_scope_but_source_path_has_commit_advances(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan = repo / "docs" / "plans" / "p.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("status: implemented\n", encoding="utf-8")
        _git(repo, "add", str(plan.relative_to(repo)))
        _git(repo, "commit", "-m", "flip plan to implemented")

        hp = _seed_handoff(repo, "h5.md", deliverable_id="dlv-src-888001")  # no scope: at all

        result = _run(
            {"deliverable_id": "dlv-src-888001", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 0
        assert len(result["advanced"]) == 1
        assert result["refused"] == []
        assert _fm_field(hp, "deployment_state") == "shipped"
        assert _fm_field(hp, "shipped_in") is not None


class TestImplausibleScopeMatchRefused:
    """(2026-08-04) Reproduces the reported false positive: a coarse directory
    `scope:` resolves to the most recent commit touching that whole tree —
    entirely unrelated work — and, absent this fix, gets stamped as this
    baton's shipped_in. The `not_after` guard (threaded from the cascade's
    own `at` trigger param) refuses a candidate that postdates it instead."""

    def test_recent_unrelated_commit_in_coarse_scope_is_refused(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        unrelated = repo / "coordinator_core" / "ops" / "ceremony" / "unrelated.py"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("# unrelated\n", encoding="utf-8")
        _git(repo, "add", str(unrelated.relative_to(repo)))
        _git_dated(repo, "unrelated recent commit", "2026-08-04T12:00:00+00:00")

        hp = _seed_handoff(
            repo, "h6.md", deliverable_id="dlv-fp-999001",
            extra="scope:\n  - coordinator_core/ops/ceremony/\n",
        )

        result = _run(
            {
                "deliverable_id": "dlv-fp-999001",
                "source_kind": "plan",
                # Deliberately unresolvable -- forces the fallback to
                # scope-derived resolution, isolating THIS guard.
                "source_path": "docs/plans/never-existed.md",
                "at": "2026-07-01T00:00:00+00:00",
            },
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["advanced"] == []
        assert len(result["refused"]) == 1
        assert "no commit evidence" in result["refused"][0]["reason"]
        assert _fm_field(hp, "deployment_state") == "ready_to_fire"
        assert _fm_field(hp, "shipped_in") is None


class TestGenuineScopeMatchStillResolves:
    """Regression: a scope-derived candidate that genuinely PREDATES the
    cascade's own trigger timestamp still resolves exactly as before this
    pass — the not_after guard must never refuse a plausible match."""

    def test_commit_predating_trigger_still_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        scope = _touch_scope_target(repo, "coordinator/bin/genuine.sh")

        hp = _seed_handoff(
            repo, "h7.md", deliverable_id="dlv-genuine-000001",
            extra=f"scope:\n  - {scope}\n",
        )

        result = _run(
            {
                "deliverable_id": "dlv-genuine-000001",
                "source_kind": "plan",
                "source_path": "docs/plans/never-existed.md",
                "at": "2099-01-01T00:00:00+00:00",
            },
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 0
        assert len(result["advanced"]) == 1
        assert _fm_field(hp, "shipped_in") is not None


class TestChainConvergence:
    """Fix A (2026-08-04): the single-pass cascade could judge a predecessor's
    leg (b) against a successor that had not yet advanced IN THE SAME PASS,
    permanently stranding the predecessor. `_handler` now iterates to a bounded
    fixpoint — see deliverable_cascade.py's own "Fixpoint iteration" docstring
    section for why bare re-evaluation is not sufficient on its own and what
    closes the gap (the `exclude_children_check` thread into leg (b))."""

    def test_two_link_chain_both_end_terminal_in_one_invocation(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        scope = _touch_scope_target(repo)

        a = _seed_handoff(
            repo, "a.md", deliverable_id="dlv-chain2-777001",
            predecessor="none", extra=f"scope:\n  - {scope}\n",
        )
        b = _seed_handoff(
            repo, "b.md", deliverable_id="dlv-chain2-777001",
            predecessor="a.md", extra=f"scope:\n  - {scope}\n",
        )

        result = _run(
            {"deliverable_id": "dlv-chain2-777001", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 0
        assert result["candidates_matched"] == 2
        assert result["refused"] == []
        advanced_paths = {entry["handoff_path"] for entry in result["advanced"]}
        assert advanced_paths == {str(a), str(b)}
        assert _fm_field(a, "deployment_state") == "shipped"
        assert _fm_field(b, "deployment_state") == "shipped"

    def test_three_link_chain_converges(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        scope = _touch_scope_target(repo)

        a = _seed_handoff(
            repo, "a.md", deliverable_id="dlv-chain3-777002",
            predecessor="none", extra=f"scope:\n  - {scope}\n",
        )
        b = _seed_handoff(
            repo, "b.md", deliverable_id="dlv-chain3-777002",
            predecessor="a.md", extra=f"scope:\n  - {scope}\n",
        )
        c = _seed_handoff(
            repo, "c.md", deliverable_id="dlv-chain3-777002",
            predecessor="b.md", extra=f"scope:\n  - {scope}\n",
        )

        result = _run(
            {"deliverable_id": "dlv-chain3-777002", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 0
        assert result["candidates_matched"] == 3
        assert result["refused"] == []
        assert len(result["advanced"]) == 3
        for hp in (a, b, c):
            assert _fm_field(hp, "deployment_state") == "shipped"

    def test_bound_holds_no_infinite_loop_on_never_converging_candidates(self, tmp_path, monkeypatch):
        # Three mutually-unrelated candidates, each claimed by a DIFFERENT live
        # session -- leg (a) refuses every one of them, every pass, forever.
        # Nothing ever advances, so `progressed` goes False after pass 1 and the
        # loop must exit THEN, not spin for `max_passes` more no-op passes.
        repo = tmp_path / "repo"
        _init_repo(repo)

        live_sids = {"sess-A", "sess-B", "sess-C"}
        monkeypatch.setattr(cascade_mod, "resolve_live_session_ids", lambda: frozenset(live_sids))

        hps = []
        for i, sid in enumerate(("sess-A", "sess-B", "sess-C")):
            hp = _seed_handoff(
                repo, f"claimed{i}.md", deliverable_id="dlv-bound-777003",
                status="claimed", deployment_state="in_flight",
                extra=f"claimed_by: {sid}\n",
            )
            hps.append(hp)

        call_count = {"n": 0}
        real_predicate = cascade_mod._predicate_refusal

        async def _counting_predicate(*args, **kwargs):
            call_count["n"] += 1
            return await real_predicate(*args, **kwargs)

        monkeypatch.setattr(cascade_mod, "_predicate_refusal", _counting_predicate)

        result = _run(
            {"deliverable_id": "dlv-bound-777003", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["advanced"] == []
        assert len(result["refused"]) == 3
        for reason_entry in result["refused"]:
            assert "claimed by live session" in reason_entry["reason"]
        # Exactly one pass over all three candidates -- no progress means the
        # loop stops immediately rather than re-evaluating up to len(candidates)
        # more times.
        assert call_count["n"] == 3

    def test_predecessor_stays_refused_when_successor_itself_refused(self, tmp_path, monkeypatch):
        # A's leg (b) sees B as a live successor; B is itself refused (claimed
        # by a live session) on every pass, so B never advances -- A must stay
        # refused too. Iteration must never manufacture convergence for a
        # successor that genuinely never clears its own predicate.
        repo = tmp_path / "repo"
        _init_repo(repo)
        scope = _touch_scope_target(repo)

        monkeypatch.setattr(
            cascade_mod, "resolve_live_session_ids", lambda: frozenset({"sess-live-b"})
        )

        a = _seed_handoff(
            repo, "a.md", deliverable_id="dlv-stuck-777004",
            predecessor="none", extra=f"scope:\n  - {scope}\n",
        )
        b = _seed_handoff(
            repo, "b.md", deliverable_id="dlv-stuck-777004",
            predecessor="a.md", status="claimed", deployment_state="in_flight",
            extra="claimed_by: sess-live-b\n",
        )

        result = _run(
            {"deliverable_id": "dlv-stuck-777004", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["advanced"] == []
        assert len(result["refused"]) == 2
        reasons = {entry["handoff_path"]: entry["reason"] for entry in result["refused"]}
        assert "claimed by live session" in reasons[str(b)]
        assert "live successor" in reasons[str(a)]
        assert _fm_field(a, "deployment_state") == "ready_to_fire"
        assert _fm_field(b, "deployment_state") == "in_flight"

    def test_indeterminate_leg_b_stays_fail_closed_across_passes(self, tmp_path, monkeypatch):
        # exit_code == 2 (indeterminate) from handoff.has_live_children must
        # refuse on every pass, never treated as a green light regardless of
        # how many times the loop re-evaluates it.
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "solo.md", deliverable_id="dlv-indet-777005")

        async def _indeterminate(*args, **kwargs):
            return {"exit_code": 2, "error": "boom: cannot scan"}

        import coordinator_core.ops.handoff_children as hc_mod

        monkeypatch.setattr(hc_mod, "_handoff_has_live_children", _indeterminate)

        result = _run(
            {"deliverable_id": "dlv-indet-777005", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        assert result["exit_code"] == 1
        assert result["advanced"] == []
        assert len(result["refused"]) == 1
        assert "indeterminate" in result["refused"][0]["reason"]
        assert _fm_field(hp, "deployment_state") == "ready_to_fire"

    def test_second_invocation_over_converged_chain_is_clean_no_op(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        scope = _touch_scope_target(repo)

        a = _seed_handoff(
            repo, "a.md", deliverable_id="dlv-chainidem-777006",
            predecessor="none", extra=f"scope:\n  - {scope}\n",
        )
        b = _seed_handoff(
            repo, "b.md", deliverable_id="dlv-chainidem-777006",
            predecessor="a.md", extra=f"scope:\n  - {scope}\n",
        )
        params = {
            "deliverable_id": "dlv-chainidem-777006",
            "source_kind": "plan",
            "source_path": "docs/plans/p.md",
        }

        first = _run(params, repo_root=repo / ".git")
        assert first["exit_code"] == 0
        assert len(first["advanced"]) == 2
        text_a = a.read_text(encoding="utf-8")
        text_b = b.read_text(encoding="utf-8")

        second = _run(params, repo_root=repo / ".git")

        assert second["candidates_matched"] == 0
        assert second["advanced"] == []
        assert second["exit_code"] == 1
        assert a.read_text(encoding="utf-8") == text_a
        assert b.read_text(encoding="utf-8") == text_b
