"""dispatch.terminal_commit records an ``import-closure-untracked`` finding when a landing ``.py``
imports a name only an uncommitted or withheld file binds. The finding is provenance by the closed
refusal table (``landing_refusal_kinds``), so the commit still lands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()
REASON = landing_integrity.IMPORT_CLOSURE_UNTRACKED


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


@pytest.fixture(autouse=True)
def _review_verdict_present(monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_floor

    monkeypatch.setattr(review_floor, "verdict_count", lambda *_a, **_k: 1)


def _fire(repo: Path, *paths: str) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(paths)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []}
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _importer(repo: Path) -> None:
    (repo / "a.py").write_text("from pkg.b import f\n", encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("def f():\n    return 1\n", encoding="utf-8")


def _reasons(out: dict) -> list:
    return [e["reason"] for e in out.get("provenance") or []]


def test_an_importer_of_a_name_only_an_uncommitted_file_binds_is_recorded(repo):
    _importer(repo)

    out = _fire(repo, "a.py")

    assert REASON in _reasons(out), out
    entry = next(e for e in out["provenance"] if e["reason"] == REASON)
    assert entry["path"] == "a.py" and "pkg.b" in entry["detail"], entry


def test_the_same_importer_records_nothing_when_the_callee_is_in_the_landing(repo):
    _importer(repo)

    out = _fire(repo, "a.py", "pkg/b.py")

    assert out["committed"] is True, out
    assert REASON not in _reasons(out), out


def test_the_same_importer_records_nothing_when_head_already_binds_the_name(repo):
    (repo / "pkg" / "b.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "callee"], repo)
    (repo / "a.py").write_text("from pkg.b import f\n", encoding="utf-8")

    out = _fire(repo, "a.py")

    assert out["committed"] is True, out
    assert REASON not in _reasons(out), out


def _gate(repo: Path, **kw) -> dict:
    return terminal_commit._landing_integrity_gate(repo, ["a.py", "pkg/b.py"], [], None, {}, "r1", **kw)


def test_the_gate_records_the_finding_when_the_callee_is_in_the_withheld_set(repo):
    _importer(repo)

    out = _gate(repo, withheld=["pkg/b.py"])

    assert "refused" not in out, out
    entry = next(e for e in out["provenance"] if e["reason"] == REASON)
    assert entry["path"] == "a.py", entry


def test_the_gate_records_nothing_for_the_same_landing_without_the_withheld_set(repo):
    _importer(repo)

    out = _gate(repo)

    assert REASON not in _reasons(out), out
