"""
coordinator_core.ops.tests.test_queue_scaffold_baton — unit tests for
``coordinator_core.ops.queue_scaffold_baton`` (``handoff.scaffold_from_queue`` op).

Coverage:
  (a) registry — "handoff.scaffold_from_queue" registered (own import guard; this
      module is not yet in _EAGER_OP_MODULES per the brief, so imported directly).
  (b) solo shape — one queue entry scaffolds one baton, across all three families.
  (c) themed shape — a queue.cluster-style envelope scaffolds one baton carrying
      every constituent item.
  (d) category is the literal "queue-derived-baton" — both in the returned envelope
      AND written directly into the frontmatter (never inherited/backfilled).
  (e) source_entries echoes {id, path} for every input row, solo and themed.
  (f) needs_disambiguation mirrors handoff_author_fork.py's shape exactly (plans
      then goals, sequential — never both at once).
  (g) locked_rmw write path — file lands under state/handoffs/, predecessor: none,
      no collision on repeated invocation (unique filenames); forced filename
      collision (MutateAbort) returns an error reply without a partial write.
  (h) input validation — neither/both of entry/cluster supplied → error reply.
  (i) unresolvable origin_handoff (OSError enumerating handoffs_dir) → error
      reply naming handoffs_dir, no baton written.
  (j) themed cluster spanning all three queue families in one invocation —
      each item's family-specific class context is correctly attributed.

Spec backlink: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C5
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects (CBR #12).
# MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY
import coordinator_core.ops.queue_scaffold_baton  # noqa: F401 — registration is a later
# chunk's job; this module is not yet in _EAGER_OP_MODULES, so import it directly here
# to fire its @register_op side effect.

from coordinator_core.ipc import _REGISTRY

assert "handoff.scaffold_from_queue" in _REGISTRY, (
    "import guard failed: 'handoff.scaffold_from_queue' not in _REGISTRY — "
    "coordinator_core.ops.queue_scaffold_baton @register_op did not fire"
)

from coordinator_core.ops.queue_scaffold_baton import (  # noqa: E402
    CATEGORY_QUEUE_DERIVED_BATON,
    _handler,
)


def _run(coro):
    """Execute a coroutine synchronously (test helper)."""
    return asyncio.run(coro)


_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_git_repo(root: Path) -> Path:
    """Create a minimal git repo at ``root``; return its common_dir (.git path)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root), capture_output=True, check=True, creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.email", "queue-scaffold-test@claude-klabauter.test"],
        cwd=str(root), capture_output=True, check=True, creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.name", "Queue Scaffold Test"],
        cwd=str(root), capture_output=True, check=True, creationflags=_NO_WIN,
    )
    return (root / ".git").resolve()


def _seed_debt(root: Path, name: str, *, title: str, risk: str = "Low.") -> Path:
    path = root / "state" / "debt-backlog" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "created: 2026-01-01",
        f'title: "{title}"',
        'body: "Test body."',
        "status: open",
        'source: "daily-review/2026-01-01"',
        f'risk: "{risk}"',
        'proposed_action: "None required."',
        "---",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _seed_improvement(root: Path, name: str, *, title: str) -> Path:
    path = root / "state" / "improvement-queue" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "created: 2026-01-01",
        f'title: "{title}"',
        'body: "Test body."',
        "status: open",
        'surface: "test-surface"',
        'proposed_action: "None required."',
        'from_repo: "claude-klabauter"',
        "change_kind: doc-edit",
        "---",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _seed_bug(root: Path, name: str, *, title: str, severity: str = "low") -> Path:
    path = root / "state" / "bug-backlog" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "created: 2026-01-01",
        f'title: "{title}"',
        'body: "Test body."',
        "status: open",
        'surface: "test-surface"',
        f"severity: {severity}",
        "---",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _rel(repo_root: Path, path: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _seed_plan(plans_dir: Path, filename: str, *, title: str, plan_id: str) -> None:
    """Write a minimal docs/plans/*.md fixture (mirrors test_handoff_author_fork.py)."""
    plans_dir.mkdir(parents=True, exist_ok=True)
    content = f'---\ntitle: "{title}"\nplan_id: "{plan_id}"\nstatus: draft\n---\n\n# Body\n'
    (plans_dir / filename).write_text(content, encoding="utf-8")


# Eight mutually-unrelated plan titles, none sharing a token with the target
# titles the tests below match against — the only thing standing between the
# query and its intended plan is the resolver's SCORE, never arity.
_UNRELATED_PLAN_FIXTURES = (
    ("2026-07-30-quantum.md", "Quantum Telemetry Shard Rebalancer", "pln-quantum"),
    ("2026-07-30-ledger.md", "Ledger Compaction Backpressure", "pln-ledger"),
    ("2026-07-30-navmesh.md", "Navmesh Streaming Budget Audit", "pln-nav"),
    ("2026-07-30-cipher.md", "Cipher Rotation Key Escrow", "pln-cipher"),
    ("2026-07-30-atlas.md", "Atlas Tile Prefetch Heuristics", "pln-atlas"),
    ("2026-07-30-freight.md", "Freight Manifest Reconciliation", "pln-freight"),
    ("2026-07-30-orchard.md", "Orchard Pruning Scheduler", "pln-orchard"),
    ("2026-07-30-basalt.md", "Basalt Column Erosion Model", "pln-basalt"),
)

_TARGET_PLAN_TITLE = "Baton Supersession Hazard Rollup"
_TARGET_PLAN_ID = "pln-baton-supersession"


def _seed_unrelated_plans(plans_dir: Path) -> None:
    """Seed ``_UNRELATED_PLAN_FIXTURES`` — the distractor corpus for the
    "candidate COUNT must not block resolution" tests."""
    for filename, title, plan_id in _UNRELATED_PLAN_FIXTURES:
        _seed_plan(plans_dir, filename, title=title, plan_id=plan_id)


# ---------------------------------------------------------------------------
# Tests: registration
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registry_is_non_empty(self):
        assert len(_REGISTRY) > 0

    def test_op_registered(self):
        assert "handoff.scaffold_from_queue" in _REGISTRY


# ---------------------------------------------------------------------------
# Tests: input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_neither_entry_nor_cluster_errors(self, tmp_path):
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({}, repo_root=common_dir))
        assert result.get("exit_code") == 1
        assert "entry" in result.get("error", "").lower() or "cluster" in result.get("error", "").lower()

    def test_both_entry_and_cluster_errors(self, tmp_path):
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(
            _handler(
                {
                    "entry": {"path": "state/debt-backlog/a.yaml"},
                    "cluster": {"items": [{"path": "state/debt-backlog/b.yaml"}]},
                },
                repo_root=common_dir,
            )
        )
        assert result.get("exit_code") == 1
        assert "one of" in result.get("error", "").lower()

    def test_repo_root_none_returns_error(self):
        result = _run(_handler({"entry": {"path": "state/debt-backlog/a.yaml"}}, repo_root=None))
        assert result.get("exit_code") == 1
        assert "repo_root" in result.get("error", "").lower()

    def test_entry_missing_path_errors(self, tmp_path):
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({"entry": {"id": "x"}}, repo_root=common_dir))
        assert result.get("exit_code") == 1

    def test_empty_title_falls_back_to_derived_id(self, tmp_path, monkeypatch):
        """No explicit title, entry.title empty, but entry.path is present — the
        title still derives (from id/path stem), so this is NOT an error path.
        A missing title is unreachable once 'path' is required and non-empty;
        this test documents that fallback rather than asserting a dead error."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "fallback-title.yaml", title="Row")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-fallback-title")
        result = _run(
            _handler(
                {
                    "entry": {"path": _rel(repo_root, path), "title": ""},
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"


# ---------------------------------------------------------------------------
# Tests: solo shape, across all three families
# ---------------------------------------------------------------------------


class TestSoloShape:
    def _invoke(self, common_dir, entry, *, monkeypatch, session="sess-scaffold"):
        monkeypatch.setenv("CLAUDE_SESSION_ID", session)
        return _run(
            _handler(
                {
                    "entry": entry,
                    "title": "Solo Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )

    def test_solo_debt_backlog(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "solo-debt.yaml", title="Debt Row", risk="High.")
        entry = {"id": "solo-debt", "path": _rel(repo_root, path), "title": "Debt Row"}
        result = self._invoke(common_dir, entry, monkeypatch=monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["source_entries"] == [{"id": "solo-debt", "path": entry["path"]}]

    def test_solo_bug_backlog(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_bug(repo_root, "solo-bug.yaml", title="Bug Row", severity="critical")
        entry = {"id": "solo-bug", "path": _rel(repo_root, path), "title": "Bug Row"}
        result = self._invoke(common_dir, entry, monkeypatch=monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "critical" in content

    def test_solo_improvement_queue(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_improvement(repo_root, "solo-imp.yaml", title="Improvement Row")
        entry = {"id": "solo-imp", "path": _rel(repo_root, path), "title": "Improvement Row"}
        result = self._invoke(common_dir, entry, monkeypatch=monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "doc-edit" in content

    def test_solo_id_and_title_derived_from_path_when_absent(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "derive-me.yaml", title="Derive Me")
        entry = {"path": _rel(repo_root, path)}
        result = self._invoke(common_dir, entry, monkeypatch=monkeypatch)
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["source_entries"] == [{"id": "derive-me", "path": entry["path"]}]


# ---------------------------------------------------------------------------
# Tests: themed shape (queue.cluster envelope)
# ---------------------------------------------------------------------------


class TestThemedShape:
    def test_themed_cluster_carries_all_items(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        paths = [
            _seed_debt(repo_root, f"cluster-{i}.yaml", title=f"Cluster Row {i}")
            for i in range(3)
        ]
        items = [
            {"id": f"cluster-{i}", "path": _rel(repo_root, p), "title": f"Cluster Row {i}"}
            for i, p in enumerate(paths)
        ]
        cluster = {
            "signal": "tag",
            "value": "widget",
            "suggestedLabel": "Widget cluster",
            "items": items,
        }
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-themed")
        result = _run(
            _handler(
                {
                    "cluster": cluster,
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["source_entries"] == [{"id": i["id"], "path": i["path"]} for i in items]
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "title: Widget cluster" in content

    def test_themed_cluster_title_override(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        paths = [
            _seed_debt(repo_root, f"override-{i}.yaml", title=f"Row {i}") for i in range(3)
        ]
        items = [
            {"id": f"override-{i}", "path": _rel(repo_root, p), "title": f"Row {i}"}
            for i, p in enumerate(paths)
        ]
        cluster = {"signal": "tag", "value": "v", "suggestedLabel": "Auto Label", "items": items}
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-themed-override")
        result = _run(
            _handler(
                {
                    "cluster": cluster,
                    "title": "Explicit Title",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "title: Explicit Title" in content

    def test_empty_cluster_items_errors(self, tmp_path):
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(
            _handler(
                {"cluster": {"signal": "tag", "value": "v", "suggestedLabel": "L", "items": []}},
                repo_root=common_dir,
            )
        )
        assert result.get("exit_code") == 1

    def test_themed_cluster_mixed_families_carries_each_class_context(self, tmp_path, monkeypatch):
        """AC11 requires themed shapes exercised across all three families; a
        tag/keyword cluster from queue.cluster has no reason to respect family
        boundaries, so a mixed-family cluster is the realistic shape this op
        actually receives — this exercises `_class_context_for_items`'s
        `by_family` grouping across all three families in one invocation."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        debt_path = _seed_debt(repo_root, "mix-debt.yaml", title="Mix Debt", risk="Severe.")
        bug_path = _seed_bug(repo_root, "mix-bug.yaml", title="Mix Bug", severity="critical")
        imp_path = _seed_improvement(repo_root, "mix-imp.yaml", title="Mix Improvement")
        items = [
            {"id": "mix-debt", "path": _rel(repo_root, debt_path), "title": "Mix Debt"},
            {"id": "mix-bug", "path": _rel(repo_root, bug_path), "title": "Mix Bug"},
            {"id": "mix-imp", "path": _rel(repo_root, imp_path), "title": "Mix Improvement"},
        ]
        cluster = {
            "signal": "tag",
            "value": "mixed",
            "suggestedLabel": "Mixed Family Cluster",
            "items": items,
        }
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-themed-mixed")
        result = _run(
            _handler(
                {
                    "cluster": cluster,
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["source_entries"] == [{"id": i["id"], "path": i["path"]} for i in items]
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "debt-backlog" in content and "mix-debt" in content
        assert "bug-backlog" in content and "mix-bug" in content
        assert "improvement-queue" in content and "mix-imp" in content
        assert "Severe." in content
        assert "critical" in content
        assert "doc-edit" in content


# ---------------------------------------------------------------------------
# Tests: category is the literal "queue-derived-baton"
# ---------------------------------------------------------------------------


class TestCategoryLiteral:
    def test_category_literal_value(self):
        assert CATEGORY_QUEUE_DERIVED_BATON == "queue-derived-baton"

    def test_category_in_response_envelope(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "cat-check.yaml", title="Cat Check")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-cat")
        result = _run(
            _handler(
                {
                    "entry": {"id": "cat-check", "path": _rel(repo_root, path), "title": "Cat Check"},
                    "title": "Cat Check Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["category"] == "queue-derived-baton"

    def test_category_written_directly_into_frontmatter(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "cat-fm.yaml", title="Cat FM")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-cat-fm")
        result = _run(
            _handler(
                {
                    "entry": {"id": "cat-fm", "path": _rel(repo_root, path), "title": "Cat FM"},
                    "title": "Cat FM Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "category: queue-derived-baton" in content
        assert "category: infra" not in content


# ---------------------------------------------------------------------------
# Tests: needs_disambiguation mirrors handoff_author_fork.py exactly
# ---------------------------------------------------------------------------


class TestDisambiguation:
    def test_needs_disambiguation_plans(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        for slug, pid, title in (("a", "pln-alpha", "Alpha"), ("b", "pln-beta", "Beta")):
            (plans_dir / f"2026-07-07-plan-{slug}.md").write_text(
                f'---\ntitle: "{title}"\nplan_id: "{pid}"\nstatus: draft\n---\n\n# Body\n',
                encoding="utf-8",
            )
        path = _seed_debt(repo_root, "disambig.yaml", title="Disambig Row")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-disambig")
        result = _run(
            _handler(
                {
                    "entry": {"id": "disambig", "path": _rel(repo_root, path), "title": "Disambig Row"},
                    "title": "Disambig Baton",
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "needs_disambiguation", f"unexpected: {result}"
        candidates = result.get("candidates", {})
        assert "plans" in candidates
        plan_ids = [c["plan_id"] for c in candidates["plans"]]
        assert "pln-alpha" in plan_ids and "pln-beta" in plan_ids
        assert not (repo_root / "state" / "handoffs").exists()

    def test_needs_disambiguation_goals(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        goals_dir = repo_root / "state" / "goals"
        goals_dir.mkdir(parents=True, exist_ok=True)
        for slug, gid, title in (("a", "gol-alpha", "Alpha Goal"), ("b", "gol-beta", "Beta Goal")):
            (goals_dir / f"goal-{slug}.yaml").write_text(
                f"---\nid: {gid!r}\ntitle: {title!r}\nstatus: active\n---\n\n# Body\n",
                encoding="utf-8",
            )
        path = _seed_debt(repo_root, "disambig-goal.yaml", title="Disambig Goal Row")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-disambig-goal")
        result = _run(
            _handler(
                {
                    "entry": {
                        "id": "disambig-goal",
                        "path": _rel(repo_root, path),
                        "title": "Disambig Goal Row",
                    },
                    "title": "Disambig Goal Baton",
                    "origin_plan_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "needs_disambiguation", f"unexpected: {result}"
        candidates = result.get("candidates", {})
        assert "goals" in candidates
        goal_ids = [c["goal_id"] for c in candidates["goals"]]
        assert "gol-alpha" in goal_ids and "gol-beta" in goal_ids

    def test_clear_match_auto_resolves_against_many_candidate_plan_directory(
        self, tmp_path, monkeypatch
    ):
        """Regression for the arity defect this dispatch fixes: candidate
        COUNT must not block auto-resolution. Nine plans are seeded; the
        baton's ``title`` (also the default ``match_text``) names exactly
        one of them clearly. That plan must be resolved and stamped — the
        historical ``len(candidates)``-arity branch would have returned
        ``needs_disambiguation`` here purely because the directory holds
        more than one plan, regardless of match quality."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_unrelated_plans(plans_dir)
        _seed_plan(plans_dir, "2026-07-30-target.md", title=_TARGET_PLAN_TITLE, plan_id=_TARGET_PLAN_ID)
        path = _seed_debt(repo_root, "clear-match.yaml", title=_TARGET_PLAN_TITLE)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-many-plans-clear-match")

        result = _run(
            _handler(
                {
                    "entry": {"id": "clear-match", "path": _rel(repo_root, path), "title": _TARGET_PLAN_TITLE},
                    "title": _TARGET_PLAN_TITLE,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["source_entries"] == [{"id": "clear-match", "path": _rel(repo_root, path)}]

    def test_lone_non_matching_plan_does_not_auto_resolve(self, tmp_path, monkeypatch):
        """A LONE plan whose title does not match the baton's title/match_text
        must NOT be auto-resolved — the historical count==1 branch treated a
        single candidate as sufficient on its own, silently stamping an
        unrelated plan as confident provenance. It must instead surface
        ``needs_disambiguation`` (never write a baton with a wrong plan
        pinned)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(plans_dir, "2026-07-30-orchard.md", title="Orchard Pruning Scheduler", plan_id="pln-orchard")
        path = _seed_debt(repo_root, "slug-query.yaml", title="2026-07-30_150000_ab12cd34")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-lone-nonmatch")

        result = _run(
            _handler(
                {
                    "entry": {
                        "id": "slug-query",
                        "path": _rel(repo_root, path),
                        "title": "2026-07-30_150000_ab12cd34",
                    },
                    "title": "2026-07-30_150000_ab12cd34",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "needs_disambiguation", f"unexpected: {result}"
        candidates = result.get("candidates", {})
        assert [c["plan_id"] for c in candidates["plans"]] == ["pln-orchard"]
        assert not (repo_root / "state" / "handoffs").exists()


# ---------------------------------------------------------------------------
# Tests: locked_rmw write path
# ---------------------------------------------------------------------------


class TestWriteMechanics:
    def test_written_to_state_handoffs(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "write-check.yaml", title="Write Check")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-write")
        result = _run(
            _handler(
                {
                    "entry": {"id": "write-check", "path": _rel(repo_root, path), "title": "Write Check"},
                    "title": "Write Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        hpath = Path(result["handoff_path"])
        assert hpath.is_absolute()
        assert hpath.parent == repo_root / "state" / "handoffs"
        assert result["handoff_id"] == hpath.stem

    def test_session_ledger_block_appended_when_absent(self, tmp_path, monkeypatch):
        """AC2 (Review: code-reviewer 49e8b242 P2): a composed body with no
        '## Session Ledger' heading gets the canonical block appended exactly once.

        This test FAILS if the append block in queue_scaffold_baton.py is deleted —
        verified by hand: removing the append (and its guard) drops the count to 0.
        """
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "ledger-absent.yaml", title="Ledger Absent")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-ledger-absent")
        result = _run(
            _handler(
                {
                    "entry": {"id": "ledger-absent", "path": _rel(repo_root, path), "title": "Ledger Absent"},
                    "title": "Ledger Absent Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert content.count("## Session Ledger") == 1, (
            f"expected exactly one '## Session Ledger' heading, "
            f"got {content.count('## Session Ledger')}; content: {content!r}"
        )

    def test_session_ledger_block_not_duplicated_when_present(self, tmp_path, monkeypatch):
        """AC2 (Review: code-reviewer 49e8b242 P2): a caller-supplied ``body`` param
        that already carries a '## Session Ledger' heading is left with exactly one
        (no duplicate append)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "ledger-present.yaml", title="Ledger Present")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-ledger-present")
        result = _run(
            _handler(
                {
                    "entry": {"id": "ledger-present", "path": _rel(repo_root, path), "title": "Ledger Present"},
                    "title": "Ledger Present Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                    "body": "## Session Ledger\n\n<!-- pre-existing -->\n",
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert content.count("## Session Ledger") == 1, (
            f"expected exactly one '## Session Ledger' heading (no duplicate), "
            f"got {content.count('## Session Ledger')}; content: {content!r}"
        )

    def test_predecessor_none(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "pred-check.yaml", title="Pred Check")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-pred")
        result = _run(
            _handler(
                {
                    "entry": {"id": "pred-check", "path": _rel(repo_root, path), "title": "Pred Check"},
                    "title": "Pred Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "predecessor: none" in content

    def test_two_invocations_produce_unique_files(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "atomic-check.yaml", title="Atomic Check")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-atomic")
        params = {
            "entry": {"id": "atomic-check", "path": _rel(repo_root, path), "title": "Atomic Check"},
            "title": "Atomic Baton",
            "origin_plan_id": None,
            "origin_goal_id": None,
        }
        r1 = _run(_handler(params, repo_root=common_dir))
        r2 = _run(_handler(params, repo_root=common_dir))
        assert r1.get("status") == "ok" and r2.get("status") == "ok"
        p1, p2 = Path(r1["handoff_path"]), Path(r2["handoff_path"])
        assert p1 != p2
        assert p1.exists() and p2.exists()

    def test_source_entries_body_carries_family_and_class_context(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "body-check.yaml", title="Body Check", risk="Severe outage risk.")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-body")
        result = _run(
            _handler(
                {
                    "entry": {"id": "body-check", "path": _rel(repo_root, path), "title": "Body Check"},
                    "title": "Body Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        assert "debt-backlog" in content
        assert "body-check" in content
        assert "Severe outage risk." in content

    def test_filename_collision_returns_error_not_partial_write(self, tmp_path, monkeypatch):
        """Force the `_mutate` collision branch (MutateAbort on a non-empty
        old_text) by monkeypatching `_fork_handoff_filename` to return a fixed
        name, then invoking the handler twice: the second invocation must hit
        the collision and return an error reply, never a corrupted/partial
        write over the first baton."""
        import coordinator_core.ops.queue_scaffold_baton as mod

        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "collide.yaml", title="Collide Row")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-collide")
        monkeypatch.setattr(mod, "_fork_handoff_filename", lambda title: "fixed-name.md")
        params = {
            "entry": {"id": "collide", "path": _rel(repo_root, path), "title": "Collide Row"},
            "title": "Collide Baton",
            "origin_plan_id": None,
            "origin_goal_id": None,
        }
        r1 = _run(mod._handler(params, repo_root=common_dir))
        assert r1.get("status") == "ok", f"unexpected: {r1}"
        first_content = Path(r1["handoff_path"]).read_text(encoding="utf-8")

        r2 = _run(mod._handler(params, repo_root=common_dir))
        assert r2.get("exit_code") == 1, f"unexpected: {r2}"
        assert "collision" in r2.get("error", "").lower()
        assert Path(r1["handoff_path"]).read_text(encoding="utf-8") == first_content

    def test_unresolvable_origin_handoffs_dir_errors(self, tmp_path, monkeypatch):
        """The OSError-on-enumerate except-clause (unresolvable origin) must
        surface exit_code=1 naming handoffs_dir, not write a baton with
        silently-null provenance."""
        import coordinator_core.ops.queue_scaffold_baton as mod

        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "unresolvable.yaml", title="Unresolvable Row")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-unresolvable")

        def _raise_oserror(handoffs_dir, session_id):
            raise OSError("simulated permission-denied")

        monkeypatch.setattr(mod, "_resolve_origin_handoff", _raise_oserror)
        result = _run(
            mod._handler(
                {
                    "entry": {
                        "id": "unresolvable",
                        "path": _rel(repo_root, path),
                        "title": "Unresolvable Row",
                    },
                    "title": "Unresolvable Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("exit_code") == 1, f"unexpected: {result}"
        assert "handoffs" in result.get("error", "").lower()
        assert not (repo_root / "state" / "handoffs").exists()

    def test_refuses_when_filename_collides_with_archived_record(self, tmp_path, monkeypatch):
        """out_path's filename already exists under archive/handoffs/ -> error, no write.

        Spec backlink: state/audits/2026-07-26-handoff-live-archive-duplication-
        origin.md (example-doctrine-repo); coordinator_core.handoff_creation_guard."""
        import coordinator_core.ops.queue_scaffold_baton as mod

        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        path = _seed_debt(repo_root, "archived-collide.yaml", title="Archived Collide Row")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-archived-collide")

        colliding_name = "2026-07-22_100600_deadbeef.md"
        archive_dir = repo_root / "archive" / "handoffs" / "2026-07"
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / colliding_name).write_text(
            "---\ntitle: \"Archived\"\nstatus: closed\n---\n\n# Body\n", encoding="utf-8"
        )
        monkeypatch.setattr(mod, "_fork_handoff_filename", lambda title: colliding_name)

        result = _run(
            mod._handler(
                {
                    "entry": {
                        "id": "archived-collide",
                        "path": _rel(repo_root, path),
                        "title": "Archived Collide Row",
                    },
                    "title": "Archived Collide Baton",
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )
        assert result.get("exit_code") == 1, result
        assert "archive" in result.get("error", "").lower(), result
        live_path = repo_root / "state" / "handoffs" / colliding_name
        assert not live_path.exists(), "guard must refuse BEFORE any write to the live path"
