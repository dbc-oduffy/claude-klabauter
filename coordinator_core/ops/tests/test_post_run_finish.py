"""
coordinator_core.ops.tests.test_post_run_finish

Purpose: `post_run.finish` composes linkfix -> h1_normalize -> nav_index -> linkfix -> commit over a
run's scope, leaves the run's paths clean, commits evidence, spares a peer's dirty file, and
reports (never halts on) an unresolvable link.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import post_run_finish
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "00000000-0000-4000-8000-000000000000"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _write(tmp_path, "README.md", "init\n")
    _write(tmp_path, "docs/decisions/DR-12-new-slug.md", "# DR-12\n\nA decision.\n")
    _write(tmp_path, "docs/wiki/old/target.md", "---\ntitle: Target\n---\n# Target\n\nBody.\n")
    _write(tmp_path, "peer.txt", "peer\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _run_files(root: Path, broken_target: str = "../old/moved.md") -> list[str]:
    _write(
        root, "docs/wiki/fresh/page.md",
        "---\ntitle: Fresh Page\n---\n# Wrong Heading\n\nSee [t](../old/target.md) and "
        f"[m]({broken_target}) and [d](../../decisions/DR-12-old-slug.md).\n",
    )
    _write(root, "state/evidence/run-1.json", "{}\n")
    return ["docs/wiki/fresh/page.md"]


def _porcelain(root: Path) -> list[str]:
    return [ln[3:] for ln in _git(root, "status", "--porcelain", "-uall").splitlines()
            if not ln[3:].startswith(".coordinator-local/")]


@pytest.mark.parametrize("pipeline", ["lessons", "distill"])
def test_finish_leaves_the_run_clean_and_spares_a_peer(tmp_path, monkeypatch, pipeline):
    root = _seed(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    touched = _run_files(root, broken_target="../old/target.md")
    (root / "peer.txt").write_text("peer dirt\n", encoding="utf-8")

    out = post_run_finish._handler(
        {"pipeline": pipeline, "run_id": "r1", "touched": touched, "evidence": ["state/evidence/run-1.json"]},
        repo_root=root,
    )

    assert out["exit_code"] == 0, out
    assert out["commit"]["committed"] is True
    page = (root / "docs/wiki/fresh/page.md").read_text(encoding="utf-8")
    assert page.startswith("---\ntitle: Fresh Page\n---\n# Fresh Page\n")
    assert "DR-12-new-slug.md" in page and "DR-12-old-slug" not in page
    assert (root / "docs/wiki/fresh/_index.md").is_file()
    tracked = _git(root, "ls-files").splitlines()
    assert {"docs/wiki/fresh/page.md", "docs/wiki/fresh/_index.md", "state/evidence/run-1.json"} <= set(tracked)
    assert _porcelain(root) == ["peer.txt"]
    assert "peer dirt" not in _git(root, "show", "HEAD:peer.txt")


def test_an_unresolvable_link_is_advisory_and_does_not_halt_the_commit(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    touched = _run_files(root, broken_target="../old/does-not-exist-anywhere.md")

    out = post_run_finish._handler(
        {"pipeline": "lessons", "run_id": "r2", "touched": touched, "evidence": ["state/evidence/run-1.json"]},
        repo_root=root,
    )

    assert out["exit_code"] == 0 and out["commit"]["committed"] is True
    assert any("does-not-exist-anywhere.md" in r and "unresolvable" in r for r in out["residual"])
    assert _porcelain(root) == []


def test_files_outside_the_run_scope_are_not_committed(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    touched = _run_files(root, broken_target="../old/target.md")
    _write(root, "docs/wiki/other/stray.md", "# Stray\n")

    post_run_finish._handler({"pipeline": "lessons", "run_id": "r3", "touched": touched}, repo_root=root)

    assert "docs/wiki/other/stray.md" not in _git(root, "ls-files").splitlines()
    assert "docs/wiki/other/stray.md" in _porcelain(root)


def test_rerun_converges_with_nothing_to_commit(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    touched = _run_files(root, broken_target="../old/target.md")
    params = {"pipeline": "lessons", "run_id": "r4", "touched": touched}
    post_run_finish._handler(params, repo_root=root)

    again = post_run_finish._handler(params, repo_root=root)

    assert again["exit_code"] == 0 and again["commit"]["committed"] is False
    assert all(s["changed"] == [] for s in again["steps"])


def _fake_audit(monkeypatch, unexplained, evidence=()):
    import sys
    import types

    calls = []
    mod = types.ModuleType("coordinator_core.post_run.p5_audit")
    mod.run = lambda result, porcelain, **_kw: (calls.append((result, porcelain)) or
                                         {"unexplained": list(unexplained), "evidence": list(evidence)})
    monkeypatch.setattr(post_run_finish, "p5_audit", mod)
    return calls


def test_distill_unexplained_path_fails_before_the_commit(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    touched = _run_files(root, broken_target="../old/target.md")
    calls = _fake_audit(monkeypatch, ["stray/file.txt"])
    head = _git(root, "rev-parse", "HEAD")

    out = post_run_finish._handler(
        {"pipeline": "distill", "run_id": "r5", "touched": touched, "result": {"a": 1}, "porcelain": [" M x"]},
        repo_root=root,
    )

    assert out["exit_code"] == 1 and out["unexplained"] == ["stray/file.txt"] and out["commit"] is None
    assert calls == [({"a": 1}, [" M x"])]
    assert _git(root, "rev-parse", "HEAD") == head


def test_distill_audit_evidence_joins_the_commit_and_lessons_skips_the_audit(tmp_path, monkeypatch):
    root = _seed(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    touched = _run_files(root, broken_target="../old/target.md")
    _write(root, "state/evidence/deletes.txt", "a.md\n")
    calls = _fake_audit(monkeypatch, [], ["state/evidence/deletes.txt"])

    lessons = post_run_finish._handler({"pipeline": "lessons", "run_id": "r6", "touched": touched}, repo_root=root)
    assert calls == [] and lessons["exit_code"] == 0
    assert "state/evidence/deletes.txt" not in _git(root, "ls-files").splitlines()

    distill = post_run_finish._handler({"pipeline": "distill", "run_id": "r7", "touched": touched}, repo_root=root)
    assert distill["exit_code"] == 0 and len(calls) == 1
    assert "state/evidence/deletes.txt" in _git(root, "ls-files").splitlines()


@pytest.mark.parametrize(
    "params",
    [{"run_id": "r"}, {"pipeline": "archive", "run_id": "r"}, {"pipeline": "lessons"}, {"pipeline": "lessons", "run_id": "r", "touched": "x"}],
)
def test_bad_params_are_refused(tmp_path, params):
    with pytest.raises(ValueError):
        post_run_finish._handler(params, repo_root=tmp_path)
