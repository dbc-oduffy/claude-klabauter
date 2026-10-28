"""dispatch.terminal_commit refuses, before commit_v2, a landing set carrying an unparseable
``.py`` or a blob that drops lines a foreign commit added since the run base."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, terminal_commit
from coordinator_core.ops.dispatch_emit import landing_targeted_tests
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()


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
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


@pytest.fixture(autouse=True)
def _review_verdict_present(monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_floor

    monkeypatch.setattr(review_floor, "verdict_count", lambda *_a, **_k: 1)


def _fire(repo: Path, path: str, **extra) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(path,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []}
    params.update(extra)
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_an_unparseable_py_is_refused_with_the_named_reason(repo):
    (repo / "m.py").write_text('x = "a\nb"\n', encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "m.py")

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.PY_UNPARSEABLE
    assert out["integrity"][0]["path"] == "m.py"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_stubbed_overlap_finding_is_refused_and_the_token_is_released(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)
    seen = {}

    def stub(root, paths, base, *, run_id, plan=None):
        seen["base"] = base
        return [landing_targeted_tests.Finding("ok.py", landing_integrity.SINCE_BASE_OVERLAP, "drops abc1234")]

    monkeypatch.setattr(landing_integrity, "since_base_findings", stub)
    token = run_token.mint(repo, "r1", "run.mjs")

    out = _fire(repo, "ok.py", run_base_sha=head, run_id="r1", run_token=token)

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.SINCE_BASE_OVERLAP
    assert seen["base"] == head
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert run_token.admits(repo, "r1", token, "run.mjs") is True


def test_a_caller_reading_a_name_only_the_worktree_callee_binds_is_refused_by_the_leak_gate(repo):
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "li.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    (pkg / "caller.py").write_text(
        "def run():\n    from pkg import li\n    return li.old()\n", encoding="utf-8"
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed pkg"], repo)
    (pkg / "li.py").write_text(
        "def old():\n    return 1\n\n\ndef new_fn():\n    return 2\n", encoding="utf-8"
    )
    (pkg / "caller.py").write_text(
        "def run():\n    from pkg import li\n    return li.old() + li.new_fn()\n", encoding="utf-8"
    )
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "pkg/caller.py")

    assert out["committed"] is False, out
    text = str(out)
    assert "import_closure" in text and "new_fn" in text, out
    assert _git(["rev-parse", "HEAD"], repo) == head


def _residue_import_repo(repo: Path) -> None:
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "li.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed pkg"], repo)
    (repo / "res.py").write_text("from pkg.li import old\n", encoding="utf-8")


def _residue_gate(repo: Path, landed_text) -> dict:
    return terminal_commit._landing_integrity_gate(
        repo, ["pkg/li.py"], [], None, {}, "r1", residue_py=["res.py"], landed_text=landed_text,
    )


def test_residue_import_check_judges_the_landed_blob_not_the_worktree(repo):
    _residue_import_repo(repo)
    (repo / "pkg" / "li.py").write_text("def new_fn():\n    return 2\n", encoding="utf-8")
    kept = {"pkg/li.py": "def old():\n    return 1\n\n\ndef new_fn():\n    return 2\n"}

    out = _residue_gate(repo, kept.get)

    assert "import_closure" not in str(out.get("integrity", "")), out


def test_residue_import_check_records_provenance_when_the_landed_blob_drops_the_name(repo):
    _residue_import_repo(repo)
    (repo / "pkg" / "li.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    dropped = {"pkg/li.py": "def new_fn():\n    return 2\n"}

    out = _residue_gate(repo, dropped.get)

    assert "refused" not in out and "integrity" not in out, out
    assert out["provenance"][0]["reason"] == landing_integrity.IMPORT_CLOSURE_UNTRACKED, out
    assert "res.py" in str(out["provenance"]), out


def test_a_clean_landing_without_a_base_commits_and_reports_no_base(repo):
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")

    out = _fire(repo, "ok.py")

    assert out["committed"] is True, out
    assert out["since_base_check"] == "no-base"


def _closure_repo(repo: Path, monkeypatch) -> str:
    from types import SimpleNamespace

    from coordinator_core.authoring_leaks import import_closure

    (repo / "caller.py").write_text("x = 1\n", encoding="utf-8")
    hit = SimpleNamespace(path="caller.py", line=1, detail="imports a deleted module")
    monkeypatch.setattr(import_closure, "residue_findings", lambda *_a, **_k: [hit])
    return "caller.py"


def test_an_import_closure_hit_lands_with_no_waiver_and_is_recorded_as_provenance(repo, monkeypatch):
    path = _closure_repo(repo, monkeypatch)
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, path)

    assert out["committed"] is True, out
    assert "refused" not in out
    assert _git(["rev-parse", "HEAD"], repo) != head


def test_claim_findings_leave_the_refusal_list_and_a_refusal_kind_stays(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals

    claim = [
        landing_targeted_tests.Finding("a.py", "import-closure-untracked", "d"),
        landing_targeted_tests.Finding("b.py", "peer-hunks-co-declared", "d"),
    ]
    secret = landing_targeted_tests.Finding("c.py", "secret-in-diff", "d")

    refusing, provenance = refusals._claim_provenance([*claim, secret])

    assert refusing == [secret]
    assert provenance == claim


def test_the_waiver_seam_is_gone_from_the_refusals_module():
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals

    assert not any(hasattr(refusals, n) for n in ("_apply_landing_waiver", "_WAIVABLE_GUARDS", "_WAIVER_ROUTE"))
