"""dispatch.terminal_commit run in-workflow: the run token, the docs stage's files, and the
sizing's `shipped` resolution with its SHA."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
    "wave_reviews": [{"verdict": "PASS", "sidecar_path": "../wave-review.md"}],
}
_SIZING = "state/sizings/20260101-a.yaml"
_PLAN = "docs/plans/p.md"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "wave-review.md").write_text("# review\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    sizing = root / _SIZING
    sizing.parent.mkdir(parents=True)
    sizing.write_text(
        "\n".join(
            [
                "schema: sizing-object",
                "intent: Test intent, verbatim.",
                "estimate:",
                "  tshirt: XS",
                "  provisional: true",
                "route: dispatch",
                "detents: []",
                "fork: null",
                "xl_exit: null",
                "status: routed",
                "premise:",
                "  provenance: read",
                "  evidence: test fixture",
                "",
            ]
        ),
        encoding="utf-8",
    )
    plan = root / _PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\nsizing_object: {_SIZING}\n---\n# p\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _call(repo: Path, docs: tuple = (), **extra) -> dict:
    chunks = (ChunkCommit(id="C1", title="t1", paths=("a.py",)),)
    if docs:
        chunks += (ChunkCommit(id="docs", title="run docs", paths=docs),)
    request = CommitRequest(chunks=chunks, plan_path=_PLAN)
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    params = {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **extra}
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_a_token_not_minted_for_the_run_refuses_before_any_write(repo):
    out = _call(repo, run_id="ask-1", run_token="0" * 32)
    assert out["committed"] is False and out["refused"] == "run-token"
    assert "a.py" not in _git(repo, "log", "--stat", "-1")


def test_minted_token_commits_and_is_redeemed(repo):
    token = run_token.mint(repo, "ask-1", "run.mjs")
    out = _call(repo, run_id="ask-1", run_token=token)
    assert out["committed"] is True, out
    assert run_token.admits(repo, "ask-1", token, "run.mjs") is False
    assert run_token.redeem(repo, "ask-1", token, "run.mjs") is False


def test_a_token_the_guard_already_claimed_still_redeems_once(repo):
    token = run_token.mint(repo, "ask-1", "run.mjs")
    assert run_token.admits(repo, "ask-1", token, "run.mjs")
    first = _call(repo, run_id="ask-1", run_token=token)
    assert first["committed"] is True
    head = _git(repo, "rev-parse", "HEAD")
    second = _call(repo, run_id="ask-1", run_token=token)
    assert second["committed"] is True and second["already_landed"] is True
    assert second["sha"] == first["sha"] and second["refused"] is None
    assert _git(repo, "rev-parse", "HEAD") == head


def _claimed(repo: Path, run_id: str = "ask-1") -> Path:
    return run_token._record_path(repo, run_id, claimed=True)


def test_a_refused_commit_leaves_the_token_admissible(repo, monkeypatch):
    token = run_token.mint(repo, "ask-1", "run.mjs")
    real = terminal_commit.reentrant_dispatch

    def refuse(op, params, repo_root=None):
        if op == "ceremony.commit_v2":
            return {"committed": False, "sha": None, "refused": "undeclared-dirty"}
        return real(op, params, repo_root=repo_root)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", refuse)
    out = _call(repo, run_id="ask-1", run_token=token)
    assert out["committed"] is False
    assert run_token.admits(repo, "ask-1", token, "run.mjs") is True
    assert not _claimed(repo).exists()
    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", real)
    assert _call(repo, run_id="ask-1", run_token=token)["committed"] is True


def test_an_unreadable_script_leaves_the_token_admissible(repo):
    token = run_token.mint(repo, "ask-1", "gone.mjs")
    out = terminal_commit._handler(
        {"script_path": "gone.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "run_id": "ask-1", "run_token": token},
        repo_root=repo / ".git",
    )
    assert out["committed"] is False
    assert out["refused"] == "script-missing"
    assert run_token.admits(repo, "ask-1", token, "gone.mjs") is True
    assert not _claimed(repo).exists()


def test_a_script_that_exists_but_cannot_be_read_still_owes_cannot_read(repo):
    (repo / "dir.mjs").mkdir()
    token = run_token.mint(repo, "ask-1", "dir.mjs")
    out = terminal_commit._handler(
        {"script_path": "dir.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "run_id": "ask-1", "run_token": token},
        repo_root=repo / ".git",
    )
    assert out["committed"] is False and "cannot read" in out["error"]
    assert run_token.admits(repo, "ask-1", token, "dir.mjs") is True
    assert not _claimed(repo).exists()


def test_an_exception_before_landing_releases_the_token(repo, monkeypatch):
    token = run_token.mint(repo, "ask-1", "run.mjs")

    def boom(op, params, repo_root=None):
        raise RuntimeError("commit agent died")

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", boom)
    with pytest.raises(RuntimeError):
        _call(repo, run_id="ask-1", run_token=token)
    assert run_token.admits(repo, "ask-1", token, "run.mjs") is True
    assert not _claimed(repo).exists()


def test_an_exception_after_landing_spends_the_token(repo, monkeypatch):
    token = run_token.mint(repo, "ask-1", "run.mjs")

    def boom(*_a, **_k):
        raise RuntimeError("post-landing failure")

    monkeypatch.setattr(terminal_commit, "_ship_cited_sizing", boom)
    with pytest.raises(RuntimeError):
        _call(repo, run_id="ask-1", run_token=token)
    assert _git(repo, "log", "--format=%s", "-1") != "seed\n"
    assert run_token.admits(repo, "ask-1", token, "run.mjs") is False
    assert not run_token._record_path(repo, "ask-1").exists()
    assert not _claimed(repo).exists()


def test_a_landed_run_leaves_neither_a_live_nor_a_claimed_record(repo):
    token = run_token.mint(repo, "ask-1", "run.mjs")
    assert _call(repo, run_id="ask-1", run_token=token)["committed"] is True
    assert not run_token._record_path(repo, "ask-1").exists()
    assert not _claimed(repo).exists()


def test_declared_doc_writes_ride_the_commit_and_residue_does_not(repo):
    note = repo / "docs" / "delivery" / "ask-1.md"
    note.parent.mkdir(parents=True)
    note.write_text("# delivered\n", encoding="utf-8")
    residue = repo / "scratch" / "warp" / "ask-1" / "briefs" / "C1.md"
    residue.parent.mkdir(parents=True)
    residue.write_text("brief\n", encoding="utf-8")
    out = _call(repo, docs=("docs/delivery/ask-1.md",))
    assert out["committed"] is True, out
    landed = _git(repo, "show", "--name-only", "--format=", out["sha"])
    assert "docs/delivery/ask-1.md" in landed and "scratch/" not in landed
    # `doc_paths` is a candidate list only: the unnamed scratch residue stays out of the tree.
    assert "scratch/warp/ask-1/briefs/C1.md" not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD")


def test_landed_run_ships_its_sizing_with_the_sha_in_a_trailer(repo):
    out = _call(repo)
    assert out["committed"] is True, out
    assert out["sizing_shipped"] == {"sizing": _SIZING, "sha": out["sha"]}
    assert "status: shipped" in (repo / _SIZING).read_text(encoding="utf-8")
    import yaml

    assert yaml.safe_load((repo / _SIZING).read_text(encoding="utf-8"))["resolution"] == {
        "sha": out["sha"], "run_id": "run",
    }
    log = _git(repo, "log", "--format=%B", "-1", "--", _SIZING)
    assert f"Sizing-Shipped: {_SIZING} {out['sha']}" in log
    assert _git(repo, "status", "--porcelain", "--", _SIZING) == ""


def test_an_incomplete_run_leaves_the_sizing_open(repo):
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)), ChunkCommit(id="C2", title="t2", paths=("b.py",))),
        plan_path=_PLAN,
    )
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": ["C2"], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out
    assert "sizing_shipped" not in out
    assert "status: routed" in (repo / _SIZING).read_text(encoding="utf-8")


def test_landed_run_records_its_resolution_off_the_commit(repo):
    import json as _json

    out = _call(repo)
    assert out["committed"] is True, out
    product = _git(repo, "log", "--format=%B", "-1", out["sha"])
    assert f"Resolves-Sizing: 20260101-a XS" in product
    rows = (repo / terminal_commit.SIZING_LEDGER_REL).read_text(encoding="utf-8").splitlines()
    row = _json.loads(rows[-1])
    assert row["resolved_sha"] == out["sha"] and row["tshirt"] == "XS" and row["route"] == "dispatch"
    assert len(row["ask_sha256"]) == 64
    assert terminal_commit.SIZING_LEDGER_REL not in _git(repo, "show", "--name-only", "--format=", out["sha"])


_VERDICT = {"items": [{"id": "R1", "status": "met", "essential": True, "wired": True, "remaining": ["a|b"]}]}
_INLINE = {
    "prep": {"product_files": 3},
    "delivery": {"verdict": "PASS", "claims_unbacked": 0},
    "slices": 2,
    "fixes": 1,
    "tests": {"status": "pass", "run": 12, "failed": 0},
    "criterion": {"status": "met", "observation": "line1\nline2"},
}


def test_the_note_gets_its_verdicts_section_from_the_params_once(tmp_path):
    note = tmp_path / "n.md"
    note.write_text("# delivered\n", encoding="utf-8")
    terminal_commit._write_note_verdicts(note, _VERDICT, _INLINE, tmp_path)
    text = note.read_text(encoding="utf-8")
    assert text.startswith("# delivered\n\n## Verdicts\n")
    assert "| R1 | met | True | True | a\\|b |" in text
    assert "delivery verdict: PASS" in text and "status: pass; run: 12; failed: 0" in text
    assert "- status: met" in text and "observation: line1 line2" in text
    terminal_commit._write_note_verdicts(note, _VERDICT, _INLINE, tmp_path)
    assert note.read_text(encoding="utf-8") == text


def test_the_verdicts_section_prints_the_criterion_reason_only_when_present(tmp_path):
    reason = "met; recorded falsifier did not run as authored: AttributeError"
    with_reason = {**_INLINE, "criterion": {**_INLINE["criterion"], "reason": reason}}
    assert f"- reason: {reason}" in terminal_commit._verdicts_section(_VERDICT, with_reason, tmp_path)
    assert "- reason:" not in terminal_commit._verdicts_section(_VERDICT, _INLINE, tmp_path)


def test_criterion_reason_reads_only_a_non_empty_string():
    assert terminal_commit._criterion_reason({"criterion": {"reason": " why "}}) == "why"
    assert terminal_commit._criterion_reason({"criterion": {"reason": "  "}}) is None
    assert terminal_commit._criterion_reason({"criterion": {"reason": None}}) is None
    assert terminal_commit._criterion_reason(None) is None


@pytest.mark.parametrize("reason,tail", [("falsifier unrun", "; falsifier unrun"), (None, "")])
def test_the_implemented_stamp_prose_carries_the_reason(tmp_path, monkeypatch, reason, tail):
    import coordinator_core.archive_stamp as archive_stamp

    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "---\nreview_stamp:\n  criterion:\n    status: met\n    observation: saw it\n    sidecar: s1\n---\n# p\n",
        encoding="utf-8",
    )
    seen = {}

    def fake(path, **kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(archive_stamp, "cs_stamp_plan_implemented", fake)
    out = terminal_commit._stamp_plan_implemented(tmp_path, "docs/plans/p.md", "abc123", reason=reason)
    assert out == {"plan_status": "implemented"}
    assert seen["prose"] == "criterion met at abc123 per s1" + tail
    assert seen["falsifier_verdict"] == "pass"


def test_a_missing_note_or_inline_review_writes_nothing(tmp_path):
    terminal_commit._write_note_verdicts(tmp_path / "absent.md", _VERDICT, _INLINE, tmp_path)
    assert not (tmp_path / "absent.md").exists()
    note = tmp_path / "n.md"
    note.write_text("# delivered\n", encoding="utf-8")
    terminal_commit._write_note_verdicts(note, _VERDICT, None, tmp_path)
    assert note.read_text(encoding="utf-8") == "# delivered\n"
