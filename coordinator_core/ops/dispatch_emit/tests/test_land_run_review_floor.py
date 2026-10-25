"""dispatch.land_run review floor: a code-editing run needs a wave verdict; a docs/state-only run does not;
the commit carries the Ledger-Receipt trailer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest, record_dir
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c5"
RUN = "ask-20261025T100000-c5c5c5"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-aaaaaa"
PLAN = "docs/plans/p.md"
REPORT = "reports/X1.md"
CODE = "coordinator_core/mod.py"
DOC = "docs/wiki/note.md"
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _author(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
    session_scope.touch(SELF, rel, cwd=str(root))
    touch_record.append_event(
        touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF),
        session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: XS\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship it\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _write(
        root, PLAN,
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: X1\n  title: t\n  disposition: open\n  body: |\n    b\n```\n",
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _stage(root: Path, path: str) -> str:
    _author(root, path, f"{path}\n")
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=(path,), report=REPORT),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    return run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)


def _land(root: Path, token: str, path: str, verdicts: int, *, with_sidecar: bool = True) -> dict:
    reviews = []
    for i in range(verdicts):
        entry = {"verdict": "APPROVED"}
        if with_sidecar:
            rel = f"scratch/reviews/r{i}.md"
            _write(root, rel, "verdict: APPROVED\n")
            entry["sidecar_path"] = rel
        else:
            entry["sidecar_path"] = f"scratch/reviews/missing{i}.md"
        reviews.append(entry)
    review = {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "prep": {"run_base_sha": None, "product_files": [path], "foreign_claims": [], "slice_files": [path]},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
        "wave_reviews": reviews,
    }
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": ["X1"], "inline_review": review,
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _message(root: Path) -> str:
    return _git(root, "log", "-1", "--format=%B", "HEAD")


def test_a_code_run_with_an_empty_wave_is_refused_and_commits_nothing(repo):
    token = _stage(repo, CODE)
    head = _git(repo, "rev-parse", "HEAD")

    out = _land(repo, token, CODE, verdicts=0)

    assert out["committed"] is False and out["refused"] == "review-floor", out
    assert out["code_paths"] == [CODE]
    assert "review: 0 verdicts" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_a_docs_only_run_with_an_empty_wave_lands_with_a_zero_receipt(repo):
    token = _stage(repo, DOC)

    out = _land(repo, token, DOC, verdicts=0)

    assert out.get("committed") is True, out
    assert "Ledger-Receipt: review: 0 verdicts" in _message(repo)


def test_a_code_run_with_one_verdict_lands_with_a_one_verdict_receipt(repo):
    token = _stage(repo, CODE)

    out = _land(repo, token, CODE, verdicts=1)

    assert out.get("committed") is True, out
    assert "Ledger-Receipt: review: 1 verdicts" in _message(repo)


def test_a_verdict_without_an_existing_sidecar_file_refuses_review_floor(repo):
    token = _stage(repo, CODE)
    head = _git(repo, "rev-parse", "HEAD")

    out = _land(repo, token, CODE, verdicts=1, with_sidecar=False)

    assert out["committed"] is False and out["refused"] == "review-floor", out
    assert "review: 0 verdicts" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_a_repo_relative_sidecar_is_counted_from_an_unrelated_cwd(repo, tmp_path, monkeypatch):
    token = _stage(repo, CODE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    out = _land(repo, token, CODE, verdicts=1)

    assert out.get("committed") is True, out
    assert "Ledger-Receipt: review: 1 verdicts" in _message(repo)


def test_a_declared_code_run_whose_disk_prep_reports_no_product_file_lands(repo, monkeypatch):
    token = _stage(repo, CODE)

    def _disk_prep(root, session_id, run_id, request, inline_review):
        inline_review["prep"] = {"run_base_sha": None, "product_files": 0, "foreign_claims": [], "slice_files": []}
        return {}, None, True

    monkeypatch.setattr(land_run, "_reconcile_prep", _disk_prep)

    out = _land(repo, token, CODE, verdicts=0)

    assert out.get("refused") != "review-floor", out


def test_a_relayed_zero_product_prep_does_not_exempt_a_code_run(repo, monkeypatch):
    token = _stage(repo, CODE)

    def _relay_only(root, session_id, run_id, request, inline_review):
        inline_review["prep"] = {"run_base_sha": None, "product_files": 0, "foreign_claims": [], "slice_files": []}
        return {}, None, False

    monkeypatch.setattr(land_run, "_reconcile_prep", _relay_only)

    out = _land(repo, token, CODE, verdicts=0)

    assert out["committed"] is False and out["refused"] == "review-floor", out


def test_the_ledger_gate_fails_a_met_item_on_a_code_run_with_no_verdict(repo):
    _stage(repo, CODE)
    run_dir = record_dir(repo, RUN)
    refused = land_run._ledger_gate(
        repo, run_dir, VERDICT, None, (), {}, None, {"wave_reviews": []}, None, None, code_paths=[CODE],
    )[0]
    assert refused is not None and refused["refused"] == "ledger-unmet"
    assert "review: 0 verdicts" in refused["error"]
    _write(repo, "scratch/reviews/gate.md", "verdict: APPROVED\n")
    wave = {"wave_reviews": [{"verdict": "APPROVED", "sidecar_path": "scratch/reviews/gate.md"}]}
    ok = land_run._ledger_gate(
        repo, run_dir, VERDICT, None, (), {}, None, wave, None, None, code_paths=[CODE],
    )[0]
    assert ok is None
    bare = land_run._ledger_gate(
        repo, run_dir, VERDICT, None, (), {}, None, {"wave_reviews": [{"verdict": "APPROVED"}]}, None, None,
        code_paths=[CODE],
    )[0]
    assert bare is not None and bare["refused"] == "ledger-unmet"
