"""
coordinator_core.benchmarks.op_fixtures — per-op params_json + scope registry for the
18 COMPUTE_ONLY ops the qsub-01 latency benchmark harness measures.

Purpose: gives C6 (harness.py) and C7 (CLI runner) a single importable structure to
iterate: ``op -> {params_json, scope, repo_required}``. Each entry pairs a
representative wire-params payload with the correct invocation scope so the harness
can build the right ``python -m coordinator_core.invoke <op> '<params>' [--repo <path>]``
argv per op, without any consumer re-deriving the bare/worktree split by hand.

FIXTURE STRATEGY (EM-decided, pinned in the plan): the 14 worktree-scoped ops are
benchmarked against a PINNED SYNTHETIC fixture repo checked into
``coordinator_core/benchmarks/fixtures/repo/`` — reproducible across machines, not
Claude-klabauter's live drifting ``state/`` tree. The checked-in tree holds only *content*
(state/handoffs, state/goals, state/initiatives, docs/plans); it deliberately does
NOT check in a literal ``.git`` directory, because a nested ``.git`` under a tracked
worktree is invisible to plain ``git add`` (git treats it as an embedded repo boundary
and refuses to recurse into it) and is actively hostile to this repo's own
destructive-operation guard rails (rm/git-store safety hooks refuse to let anyone
delete a checked-in ``.git`` tree, and refuse to let it be *created* under version
control the normal way either). Instead, ``materialize_fixture_repo()`` copies the
checked-in content tree into a fresh temp directory and ``git init``s + commits it
there on demand, once per benchmark run — deterministic content, real git plumbing
(needed by ``coverage.gate``'s ``git rev-list`` / ``merge-base`` calls), zero
machine-specific drift.

Two repo_root shapes are required by the 14 ops (verified against
``coordinator_core/ipc.py::_OP_KEY_SCOPE``):
  - 13 ops key on ``common_dir`` — ``--repo`` must point at ``<fixture>/.git``
    (handlers derive the worktree root themselves via ``main_worktree_root()``,
    i.e. ``common_dir.parent``).
  - ``coverage.gate`` alone keys on ``show_top`` — ``--repo`` must point at the
    fixture worktree root directly (it passes ``repo_root`` straight through to
    ``coordinator_core.coverage.run_coverage_gate`` with no ``.parent`` derivation).

Spec backlink: docs/plans/2026-07-10-qsub-01-latency-benchmark-harness.md § C4
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional

from coordinator_core.benchmarks.timer import SUBPROCESS_CREATIONFLAGS, SUBPROCESS_TIMEOUT_S

# ---------------------------------------------------------------------------
# Fixture repo paths
# ---------------------------------------------------------------------------

# The checked-in, git-free content tree consumed by materialize_fixture_repo().
FIXTURE_CONTENT_DIR: Path = Path(__file__).parent / "fixtures" / "repo"

_SESSION_ID = "bench-session-001"
_HANDOFF_ID = "2026-01-01_000000_bench-fixture-handoff"
_HANDOFF_2_ID = "2026-01-02_000000_bench-fixture-handoff-2"
_ROADMAP_ID = "bench-roadmap-01"
_DELIVERABLE_ID = "dlv-bench-fixture-0000000"


def materialize_fixture_repo(dest: Optional[Path] = None) -> Path:
    """Copy the checked-in fixture content tree into a real git repo and return its root.

    Purpose: the single owning function for turning the checked-in, git-free
    ``fixtures/repo/`` content tree into an actual on-disk git repository — the
    shape every worktree-scoped op under test requires (``common_dir`` =
    ``<worktree>/.git``; ``show_top`` = ``<worktree>``).

    Args:
        dest: destination directory for the materialized repo. When omitted, a
              fresh ``tempfile.mkdtemp()`` directory is used (caller is responsible
              for cleanup in that case — the harness/CLI runner owns the lifetime
              of a benchmark-run's fixture instance).

    Returns:
        The materialized repo's worktree root (contains ``.git/``, ``state/``,
        ``docs/``).

    Negative-spec: does NOT mutate ``FIXTURE_CONTENT_DIR`` — copies only, never
    writes back into the checked-in tree.
    """
    if dest is None:
        dest = Path(tempfile.mkdtemp(prefix="claude-klabauter-bench-fixture-"))
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(FIXTURE_CONTENT_DIR, dest)

    _run_git(dest, ["init", "-q", "-b", "main"])
    _run_git(dest, ["config", "user.email", "bench@fixture.local"])
    _run_git(dest, ["config", "user.name", "Benchmark Fixture"])
    _run_git(dest, ["add", "-A"])
    _run_git(dest, ["commit", "-q", "-m", "bench-fixture: seed synthetic repo state"])

    # coverage.gate's default range is `git merge-base origin/main HEAD..HEAD` — it
    # needs a real origin/main ref to resolve non-INDETERMINATE, and a second commit
    # gives git rev-list an actual chain to walk (representative of real invocation
    # cost, not a degenerate single-commit no-op). A local bare "origin" remote is
    # sufficient; no network access required.
    # Review: code-reviewer (Slice B F1, P1) — nested INSIDE dest (not a dest.parent
    # sibling) so harness.run()'s single `shutil.rmtree(worktree_root)` cleans this up
    # too; a sibling path previously escaped that cleanup and leaked one bare repo per run.
    origin_dir = dest / ".bench-origin.git"
    _run_git(dest, ["clone", "-q", "--bare", str(dest), str(origin_dir)])
    _run_git(dest, ["remote", "add", "origin", str(origin_dir)])
    _run_git(dest, ["fetch", "-q", "origin"])
    _run_git(dest, ["branch", "-q", "--track", "origin-main-tracking", "origin/main"])

    (dest / "state" / "handoffs" / "2026-01-03_000000_bench-fixture-handoff-3.md").write_text(
        (
            "---\n"
            "title: \"bench-fixture-3 — third synthetic handoff (post-origin-main commit)\"\n"
            "created: 2026-01-03\n"
            "branch: \"work/bench/2026-01-01\"\n"
            "status: open\n"
            "kind: session-handoff\n"
            "workstream: bench-fixture\n"
            "category: infra\n"
            "summary: \"Synthetic handoff added after the origin/main fixture commit, "
            "giving coverage.gate a non-empty default range to walk.\"\n"
            "pickup_ready: false\n"
            "roadmap_id: \"bench-roadmap-01\"\n"
            "stub_id: \"bench-03\"\n"
            "blocks: []\n"
            "initiative: null\n"
            "---\n\n"
            "## What Was Accomplished\n\n"
            "Synthetic fixture handoff #3 — added on top of the origin/main fixture "
            "commit so `coverage.gate`'s default range has a real, non-empty chain.\n"
        ),
        encoding="utf-8",
    )
    _run_git(dest, ["add", "-A"])
    _run_git(dest, ["commit", "-q", "-m", "bench-fixture: add post-origin-main commit"])
    return dest


def _run_git(cwd: Path, args: list) -> None:
    """Run a git subprocess in ``cwd``, raising loudly on any non-zero exit.

    Purpose: fixture materialization must fail loud, never silently produce a
    half-initialized repo that later ops would smoke-fail against for the wrong
    reason (a broken fixture, not a broken op). ``git`` is a GUI-subsystem binary
    on Windows (exempt from the console-popup guard by convention), but the
    portable ``creationflags`` form is applied anyway — zero-cost, no-op off
    Windows, and keeps this call site pattern-consistent with every other
    subprocess call in this package.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_S,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        # Review: code-reviewer (Slice B F3, P2) — a hung git subprocess (e.g.
        # blocked on a lock file) must fail loud like any other fixture-
        # materialization error, not wedge the run forever.
        raise RuntimeError(
            f"op_fixtures: git {' '.join(args)} timed out after "
            f"{SUBPROCESS_TIMEOUT_S}s in {cwd}: {exc}"
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"op_fixtures: git {' '.join(args)} failed in {cwd}: "
            f"exit={result.returncode} stderr={result.stderr!r}"
        )


def common_dir(worktree_root: Path) -> Path:
    """Return the ``--repo`` value for the 13 ``common_dir``-keyed worktree ops."""
    return worktree_root / ".git"


def show_top_dir(worktree_root: Path) -> Path:
    """Return the ``--repo`` value for ``coverage.gate`` (the sole ``show_top`` op)."""
    return worktree_root


# ---------------------------------------------------------------------------
# Op fixture registry
#
# scope: "bare"     — invoked WITHOUT --repo (4 none/central-scoped ops).
#        "worktree" — invoked WITH --repo (14 worktree-scoped ops).
# repo_key: for scope="worktree" only — "common_dir" or "show_top"; selects which
#           of common_dir()/show_top_dir() the harness must pass as --repo.
# ---------------------------------------------------------------------------

COMPUTE_ONLY_FIXTURES: Dict[str, dict] = {
    # --- 4 bare/none-scoped ops (ping + 3 advisory hooks) ---------------------
    "ping": {
        "params_json": "{}",
        "scope": "bare",
    },
    "hooks.nudge_unauthorized_handoff": {
        "params_json": "{}",
        "scope": "bare",
    },
    "hooks.postuse_advisory_dispatch": {
        "params_json": "{}",
        "scope": "bare",
    },
    "hooks.suggest_sonnet_research": {
        "params_json": '{"tool_name": "Bash", "tool_input": {"command": "git status"}}',
        "scope": "bare",
    },
    # --- 14 worktree-scoped ops ------------------------------------------------
    "ceremony.session_instructions": {
        "params_json": f'{{"sid": "{_SESSION_ID}"}}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "commit.anchors": {
        "params_json": f'{{"session_id": "{_SESSION_ID}", "nature": "chore"}}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "coverage.gate": {
        "params_json": "{}",
        "scope": "worktree",
        "repo_key": "show_top",
    },
    "deliverable.rollup": {
        "params_json": f'{{"deliverable_id": "{_DELIVERABLE_ID}"}}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "goal.match_candidates": {
        "params_json": '{"text": "bench fixture goal"}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "handoff.has_live_children": {
        # __WORKTREE__ is substituted by params_json_for() with the materialized fixture
        # repo's absolute worktree root — a bare relative candidate resolves against the
        # *invoking process's cwd* (contained_path's Path.resolve() semantics), not the
        # fixture worktree, and would spuriously fail the containment check.
        "params_json": (
            '{"candidate": "__WORKTREE__/state/handoffs/' + _HANDOFF_ID + '.md"}'
        ),
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "handoff.lineage_ancestry": {
        "params_json": f'{{"handoff_id": "{_HANDOFF_2_ID}"}}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "handoff.match_candidates": {
        "params_json": '{"text": "bench fixture handoff"}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "hooks.nudge_em_code_dispatch": {
        "params_json": '{"tool_name": "Write", "tool_input": {"file_path": "foo.py"}}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "hooks.nudge_foreground_agent_dispatch": {
        "params_json": '{"tool_name": "Bash", "tool_input": {"command": "git status"}}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "initiative.serve_set": {
        "params_json": "{}",
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "plan.match_candidates": {
        "params_json": '{"text": "bench fixture plan"}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "records.query": {
        "params_json": '{"type": "handoff", "format": "paths"}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
    "roadmap.serve": {
        "params_json": f'{{"roadmap_id": "{_ROADMAP_ID}"}}',
        "scope": "worktree",
        "repo_key": "common_dir",
    },
}


def repo_arg_for(op: str, worktree_root: Path) -> Optional[Path]:
    """Resolve the ``--repo`` argument value for ``op`` given a materialized worktree root.

    Returns ``None`` for bare-scoped ops (no ``--repo`` is passed at all).
    """
    entry = COMPUTE_ONLY_FIXTURES[op]
    if entry["scope"] == "bare":
        return None
    if entry["repo_key"] == "show_top":
        return show_top_dir(worktree_root)
    return common_dir(worktree_root)


_WORKTREE_TOKEN = "__WORKTREE__"


def params_json_for(op: str, worktree_root: Path) -> str:
    """Return ``op``'s final ``params_json`` string with ``__WORKTREE__`` substituted.

    Purpose: most entries' ``params_json`` is a static, self-contained literal. A
    handful of ops (currently ``handoff.has_live_children``) accept a path param
    that the callee resolves against the *invoking process's cwd*, not the
    ``--repo`` value — for those, ``COMPUTE_ONLY_FIXTURES[op]["params_json"]``
    carries an ``__WORKTREE__`` token that must be substituted with the
    materialized fixture repo's absolute worktree root before use. Plain
    ``str.replace`` (not ``str.format``) is used deliberately — every other
    entry's ``params_json`` is a literal JSON object containing unescaped ``{``/
    ``}`` braces, which ``str.format`` would misparse as replacement fields. This
    is the single place substitution happens — consumers should call this instead
    of reading ``COMPUTE_ONLY_FIXTURES[op]["params_json"]`` directly.
    """
    template = COMPUTE_ONLY_FIXTURES[op]["params_json"]
    return template.replace(_WORKTREE_TOKEN, str(worktree_root))
