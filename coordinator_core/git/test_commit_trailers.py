"""coordinator_core.git.test_commit_trailers -- tier-3 (claimed-plan)
Deliverable-Id resolution coverage for `commit_trailers.py`.

Closes the same-session plan-execute residual named in
`archive/specs/2026-08/2026-08-01-deliverable-id-carry-onto-executing-
handoff.md`'s execution note: a session that claims a PLAN and executes it
WITHOUT a handoff never populates `pickup.deliverable_id`, so tiers 1/1a of
`_resolve_deliverable_id` always miss for that session's chunk commits. This
module pins the new tier-3 fallback (`resolve_claimed_plan_path()` -> the
claimed plan's own `deliverable_id` frontmatter field) added to close that
door, plus a regression test for the import-cycle trap named in
`coordinator_core/session/claimed_plan.py`'s negative-spec.

Spec backlink: DR-207 DD#1; archive/specs/2026-08/2026-08-01-deliverable-id-
carry-onto-executing-handoff.md execution note.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.git.commit_trailers import compute_missing_trailer_args

_SID = "12121212-1212-4121-8121-121212121212"
_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _write_shape(repo: Path, sid: str, shape: dict) -> None:
    shape_dir = repo / ".git" / "coordinator-sessions" / sid
    shape_dir.mkdir(parents=True, exist_ok=True)
    (shape_dir / "session-shape.json").write_text(json.dumps(shape), encoding="utf-8")


def _write_plan(repo: Path, rel_path: str, fm_extra: str = 'deliverable_id: "dlv-plan-value"\n') -> None:
    plan_path = repo / rel_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\ntitle: example plan\n{fm_extra}---\n\n# Example plan\n",
        encoding="utf-8",
    )


def _msg_file(repo: Path, text: str = "chore: land trailers\n") -> Path:
    p = repo / "MSG"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# (i) pickup tier populated -> unchanged behaviour, claimed-plan tier never
# consulted.
# ---------------------------------------------------------------------------

def test_pickup_tier_wins_claimed_plan_never_consulted(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(
        repo, _SID,
        {
            "pickup": {"deliverable_id": "dlv-from-pickup"},
            "plan": {"path": "docs/plans/should-not-be-read.md"},
        },
    )
    # No plan file written at all -- if the claimed-plan tier were consulted
    # this would raise/omit rather than silently succeed with the wrong id.
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert "--trailer" in args
    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


# ---------------------------------------------------------------------------
# (ii) pickup tier empty + claimed plan resolvable -> the plan's
# deliverable_id is returned.
# ---------------------------------------------------------------------------

def test_claimed_plan_tier_used_when_pickup_empty(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/example.md"}})
    _write_plan(repo, "docs/plans/example.md", 'deliverable_id: "dlv-plan-value"\n')
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-plan-value" in joined


# ---------------------------------------------------------------------------
# (iii) pickup empty + no claimed plan -> "", no trailer.
# ---------------------------------------------------------------------------

def test_no_pickup_no_claimed_plan_no_trailer(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    # No session-shape.json at all.
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert "Session-Id:" in joined  # session-id resolution still succeeds


# ---------------------------------------------------------------------------
# (iv) claimed plan resolvable but its deliverable_id absent/null/unreadable
# -> "", no trailer, no crash.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fm_extra",
    [
        "",  # deliverable_id absent entirely
        "deliverable_id: null\n",  # literal null
        "deliverable_id: \n",  # blank scalar
    ],
    ids=["absent", "null", "blank"],
)
def test_claimed_plan_missing_deliverable_id_no_crash(tmp_path, monkeypatch, fm_extra):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/example.md"}})
    _write_plan(repo, "docs/plans/example.md", fm_extra)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


def test_claimed_plan_file_missing_no_crash(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/does-not-exist.md"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


# ---------------------------------------------------------------------------
# (v) regression: the deferred import must NOT be hoisted to module scope --
# importing coordinator_core.git.commit_trailers, then forcing the full ops
# eager sweep, must leave all four ops the execution note named still
# registered. Run in a subprocess for a clean module cache (importing
# coordinator_core.ops even once in-process is enough to poison a shared
# session for any other test relying on registry state).
# ---------------------------------------------------------------------------

def test_ops_registry_survives_commit_trailers_import():
    script = (
        "import coordinator_core.git.commit_trailers\n"
        "import coordinator_core.ops as ops\n"
        "ops._eager_import_all()\n"
        "from coordinator_core.ipc import get_op_handler\n"
        "names = ['handoff.normalize', 'handoff.author_fork', "
        "'handoff.correct_body', 'handoff.scaffold_from_queue']\n"
        "missing = [n for n in names if get_op_handler(n) is None]\n"
        "assert not missing, missing\n"
        "print('OK')\n"
    )
    cp = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        creationflags=_CNW,
    )
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    assert "OK" in cp.stdout


# ---------------------------------------------------------------------------
# (vi) tier-0 (artifact-first) coverage -- 2026-08-04 cross-repo memo,
# defect 2: a multi-baton session's commits must each resolve to the
# COMMITTED ARTIFACT's own deliverable_id, not the session's (last-write-
# wins) pickup record.
# ---------------------------------------------------------------------------

def _write_handoff(repo: Path, rel_path: str, deliverable_id: str) -> None:
    _write_plan(repo, rel_path, f'deliverable_id: "{deliverable_id}"\n')


def test_artifact_tier_resolves_each_of_three_commits_independently(tmp_path, monkeypatch):
    """The exact reported scenario: one session, three commits, three
    artifacts with three different deliverable_id values -> three
    DIFFERENT correct trailers, regardless of which pickup claim the
    session-shape record resolved last."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    # Session-shape pickup tier resolves to a THIRD, unrelated value --
    # simulating the reported "last-claim-wins" session state. Tier 0 must
    # win over it whenever a pathspec is supplied.
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-qsent-03"}})

    _write_handoff(repo, "state/handoffs/roadmap-qsent-02.md", "dlv-qsent-02")
    _write_handoff(repo, "state/handoffs/roadmap-qsent-03.md", "dlv-qsent-03")
    _write_handoff(repo, "state/handoffs/qsent-05-rebase.md", "dlv-qsent-05")

    for rel_path, expected in (
        ("state/handoffs/roadmap-qsent-02.md", "dlv-qsent-02"),
        ("state/handoffs/roadmap-qsent-03.md", "dlv-qsent-03"),
        ("state/handoffs/qsent-05-rebase.md", "dlv-qsent-05"),
    ):
        msg = _msg_file(repo, f"archive handoff: {rel_path}\n")
        args = compute_missing_trailer_args(msg, repo, paths=[rel_path])
        joined = " ".join(args)
        assert f"Deliverable-Id: {expected}" in joined, (rel_path, joined)


def test_no_paths_arg_is_byte_identical_to_before_the_fix(tmp_path, monkeypatch):
    """Omitting `paths` entirely (every caller not yet updated to pass its
    own pathspec) must reproduce the pre-fix session-only resolution with
    zero behaviour change -- tier 0 never activates without a pathspec."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


def test_artifact_tier_falls_back_to_session_when_paths_carry_no_deliverable_id(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    # A committed path with no frontmatter at all, and one that does not
    # exist on disk -- neither carries a deliverable_id, so tier 0 must
    # yield nothing and the session tier must still answer.
    (repo / "README.md").write_text("no frontmatter here\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["README.md", "does/not/exist.md"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


def test_artifact_tier_raises_on_genuinely_divergent_multi_artifact_commit(
    tmp_path, monkeypatch
):
    """Two paths in the SAME commit naming two DIFFERENT non-empty
    deliverable_id values is not guessed at -- fails loud, matching the
    posture `coordinator_core.ops.deliverable_carry.DivergentDeliverableIdError`
    already established for its own plan-vs-predecessor divergent join."""
    from coordinator_core.ops.deliverable_carry import DivergentDeliverableIdError

    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_handoff(repo, "state/handoffs/a.md", "dlv-a")
    _write_handoff(repo, "state/handoffs/b.md", "dlv-b")
    msg = _msg_file(repo)

    with pytest.raises(DivergentDeliverableIdError):
        compute_missing_trailer_args(
            msg, repo, paths=["state/handoffs/a.md", "state/handoffs/b.md"]
        )
