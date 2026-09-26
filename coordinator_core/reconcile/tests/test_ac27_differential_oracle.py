"""
coordinator_core.reconcile.tests.test_ac27_differential_oracle -- corpus-axis
fixtures for the AC27 oracle (docs/plans/2026-07-26-gate-resolution-widen-and-
migrate.md, DoE-claude, C12 GATE).

Covers the blind spot the evaluator axis structurally cannot see: a corpus
migration (moving `gate_dependency` prose into `blocking_notes`) that flips a
baton's verdict under an UNCHANGED evaluator. The reproduction fixture pins
the evaluator explicitly to `CORPUS_AXIS_EVALUATOR_SHA` (loaded from a git ref,
never a live import of `coordinator_core.reconcile.gate_eval`) so the test
asserts the ORACLE's behaviour and is not itself blown off course by a
sibling chunk's concurrent `blocking_notes`-dominance edit to that module's
working-tree copy.

Also covers the appeared/disappeared bucketing this axis requires: a baton
absent from one corpus state is never reported as a verdict delta.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from coordinator_core.reconcile import ac27_differential_oracle as oracle
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


@pytest.fixture()
def corpus_repo(tmp_path: Path) -> Path:
    root = tmp_path / "corpus-repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "archive" / "handoffs").mkdir(parents=True)
    (root / "archive" / "completed").mkdir(parents=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "chore: seed corpus repo")
    return root


def _write_handoff(root: Path, rel_path: str, frontmatter: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{textwrap.dedent(frontmatter).strip()}\n---\nbody\n", encoding="utf-8")
    return path


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def test_corpus_axis_flags_migration_that_moves_prose_out_of_gate_dependency(
    corpus_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact defect this axis exists to catch: `gate_dependency` prose
    (which the pinned evaluator DOES consult, via the legacy prose-fallback
    path) moved to `blocking_notes` (which that SAME pinned evaluator does
    NOT consult, by construction at CORPUS_AXIS_EVALUATOR_SHA) with
    `blocked_by` empty throughout. The verdict must flip surface ("held") ->
    clear, and the corpus axis must flag it as a delta -- deleting the corpus
    axis (or silently falling back to only the evaluator axis) makes this
    test fail because `run_corpus_axis` would not exist / would not see it.
    """
    _write_handoff(
        corpus_repo, "state/handoffs/migrating-baton.md",
        """
        id: migrating-baton
        status: open
        deployment_state: awaiting_gate
        blocked_by: []
        gate_dependency: "needs a Windows box to verify"
        """,
    )
    _commit_all(corpus_repo, "seed migrating-baton, prose in gate_dependency")

    _write_handoff(
        corpus_repo, "state/handoffs/migrating-baton.md",
        """
        id: migrating-baton
        status: open
        deployment_state: awaiting_gate
        blocked_by: []
        gate_dependency: ""
        blocking_notes: "needs a Windows box to verify"
        """,
    )
    # Deliberately left UNCOMMITTED -- the corpus axis compares the last

    monkeypatch.setattr(oracle, "REPO_KEYS", (("repos.fixture", "FixtureRepo"),))
    monkeypatch.setattr(oracle, "_resolve_repo_root", lambda _key: corpus_repo)

    result = oracle.run_corpus_axis(old_corpus_ref="HEAD")

    assert result["total_deltas"] == 1
    assert result["total_appeared"] == 0
    assert result["total_disappeared"] == 0
    delta = result["deltas"][0]
    assert delta["handoff_id"] == "migrating-baton"
    assert delta["old_corpus_verdict"] == "surface"
    assert delta["new_corpus_verdict"] == "clear"


def test_corpus_axis_does_not_report_appeared_or_disappeared_as_deltas(
    corpus_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_handoff(
        corpus_repo, "state/handoffs/only-at-old.md",
        """
        id: only-at-old
        status: open
        deployment_state: awaiting_gate
        blocked_by: []
        gate_dependency: "some prose"
        """,
    )
    _commit_all(corpus_repo, "seed only-at-old")
    (corpus_repo / "state" / "handoffs" / "only-at-old.md").unlink()

    _write_handoff(
        corpus_repo, "state/handoffs/only-at-new.md",
        """
        id: only-at-new
        status: open
        deployment_state: awaiting_gate
        blocked_by: []
        gate_dependency: "some other prose"
        """,
    )

    monkeypatch.setattr(oracle, "REPO_KEYS", (("repos.fixture", "FixtureRepo"),))
    monkeypatch.setattr(oracle, "_resolve_repo_root", lambda _key: corpus_repo)

    result = oracle.run_corpus_axis(old_corpus_ref="HEAD")

    assert result["total_deltas"] == 0
    assert result["total_appeared"] == 1
    assert result["total_disappeared"] == 1
    assert result["appeared"][0]["handoff_id"] == "only-at-new"
    assert result["disappeared"][0]["handoff_id"] == "only-at-old"


def test_check_transitive_import_isolation_flags_only_the_diverged_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "iso-repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "module_a.py").write_text("A = 1\n", encoding="utf-8")
    (root / "module_b.py").write_text("B = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pre")
    pre_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    (root / "module_a.py").write_text("A = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "post")
    post_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            oracle, "_GATE_EVAL_TRANSITIVE_IMPORT_PATHS", ("module_a.py", "module_b.py"),
        )
        warnings = oracle._check_transitive_import_isolation(root, pre_sha, post_sha)

    assert len(warnings) == 1
    assert "module_a.py" in warnings[0]
    assert not any("module_b.py" in w for w in warnings)


def test_check_transitive_import_isolation_silent_when_byte_identical(
    tmp_path: Path,
) -> None:
    root = tmp_path / "iso-repo-clean"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "module_a.py").write_text("A = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pre")
    pre_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    (root / "unrelated.py").write_text("U = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "post")
    post_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(oracle, "_GATE_EVAL_TRANSITIVE_IMPORT_PATHS", ("module_a.py",))
        warnings = oracle._check_transitive_import_isolation(root, pre_sha, post_sha)

    assert warnings == []


def test_run_both_axes_keeps_the_two_axes_under_distinct_keys(
    corpus_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "REPO_KEYS", (("repos.fixture", "FixtureRepo"),))
    monkeypatch.setattr(oracle, "_resolve_repo_root", lambda _key: corpus_repo)

    result = oracle.run_both_axes(old_corpus_ref="HEAD")

    assert "evaluator_axis" in result
    assert "corpus_axis" in result
    assert "deltas" in result["evaluator_axis"]
    assert "deltas" in result["corpus_axis"]
    assert result["corpus_axis"]["fixed_evaluator_sha"] == oracle.CORPUS_AXIS_EVALUATOR_SHA
    assert result["corpus_axis"]["old_corpus_ref"] == "HEAD"
