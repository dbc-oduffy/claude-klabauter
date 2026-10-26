"""dispatch.terminal_commit lands the final version of a credited row's file: a row whose file the run
wrote v1 then v2 leaves HEAD at v2 with the path clean. One case per reproduction hypothesis H1-H5;
``assert_final_version_landed`` is the shared assertion."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_credit, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261023T120000-cccccc"
EARLIER = "ask-20261023T100000-dddddd"
SIZING = "state/sizings/2026-10-23-final-version-fixture.yaml"
REPORT = "reports/C1.md"
ROW = "a.py"
PREFIXED = "gen/x.py"
V1 = "# version 1\n"
V2 = "# version 2, the run's last write\n"
_SID = "11111111-2222-3333-4444-555555555555"
_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_PARTIAL_REPORT = "status: PARTIAL (C1 in-footprint delivered; tests outside the footprint)\n"
_PREFIX_REPORT = _PARTIAL_REPORT + f"created-under-prefix: {PREFIXED}\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _review() -> dict:
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": reviewed()["wave_reviews"],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [],
                 "slice_files": ["other.py"]},
        "delivery": {"verdict": "FAIL", "product_files": 1},
        "integration": {"sidecar": "s", "unresolved": [], "confinement_violations": 0},
    }


def assert_final_version_landed(repo: Path, rel: str, v2_bytes: bytes) -> None:
    """HEAD's blob for ``rel`` is ``v2_bytes`` and ``rel`` is clean against HEAD."""
    head = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=str(repo), capture_output=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert head == v2_bytes, f"HEAD holds {head!r}, the run's last write was {v2_bytes!r}"
    assert _git(repo, "status", "--porcelain", "--", rel) == "", f"{rel} is dirty against HEAD"


def _build(root: Path, monkeypatch, chunk: ChunkCommit, report_text: str) -> Path:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship a.py\n",
    )
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    _write(root, REPORT, report_text)
    _write(root, "docs/note.md", "note\n")
    request = CommitRequest(
        chunks=(chunk, ChunkCommit(id="docs", title="run docs", paths=("docs/note.md",)))
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    rl.start(root, EARLIER, all_of_pm_ask=False)
    rl.bind(root, EARLIER, SIZING)
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"sizing_rel": SIZING}))
    run_credit.record(root / RECORD_DIR_ROOT / EARLIER, [{"chunk": "C1", "evidence": "tests pass"}], "t")
    _git(root, "add", "-f", RECORD_DIR_ROOT)
    _git(root, "commit", "-q", "-m", "warp: record halted earlier")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    return _build(root, monkeypatch, ChunkCommit(id="C1", title="t", paths=(ROW,), report=REPORT), _PARTIAL_REPORT)


@pytest.fixture
def prefixed_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    chunk = ChunkCommit(id="C1", title="t", paths=(), prefixes=("gen/",), report=REPORT)
    return _build(root, monkeypatch, chunk, _PREFIX_REPORT)


def _call(repo: Path, **extra) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C1"], "inline_review": _review(),
        "session_id": _SID, "ledger_verdict": _VERDICT, **extra,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _checkpoint(repo: Path, rel: str, text: str) -> None:
    _write(repo, rel, text)
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", f"wave checkpoint: {rel}")


def _credited_means_landed(out: dict, repo: Path, rel: str) -> None:
    """A run that credits the row must have landed v2; a refusal that names the strand is the other sound answer."""
    if out.get("regraded", {}).get("C1", "").startswith("credited"):
        assert out.get("committed") is True, out
        assert_final_version_landed(repo, rel, V2.encode())
    else:
        assert "C1" in out.get("incomplete_reasons", {}), out


def test_h1_checkpoint_landed_v1_then_run_wrote_v2(repo):
    _checkpoint(repo, ROW, V1)
    _write(repo, ROW, V2)
    out = _call(repo)
    assert out["committed"] is True, out
    assert out["regraded"]["C1"] == "credited: tests pass"
    assert_final_version_landed(repo, ROW, V2.encode())


def test_h2_credited_row_path_withheld_is_not_credited_as_landed(repo):
    _checkpoint(repo, ROW, V1)
    _write(repo, ROW, V2)
    out = _call(repo, withhold_paths=[{"path": ROW, "reason": "peer holds it"}])
    assert "C1" not in out.get("regraded", {}), out
    assert out["incomplete_reasons"]["C1"] == "withheld: peer holds it"
    _credited_means_landed(out, repo, ROW)


def test_h2_credited_row_path_reverted_is_not_credited_as_landed(repo):
    _checkpoint(repo, ROW, V1)
    _write(repo, ROW, V2)
    out = _call(repo, targeted_tests={"reverted": [ROW]})
    _credited_means_landed(out, repo, ROW)


def test_h3_refire_prefix_claim_of_v1_lands_v2(prefixed_repo):
    _checkpoint(prefixed_repo, PREFIXED, V1)
    _write(prefixed_repo, PREFIXED, V2)
    out = _call(prefixed_repo)
    assert out["committed"] is True, out
    assert_final_version_landed(prefixed_repo, PREFIXED, V2.encode())


def test_h4_index_holds_v1_worktree_holds_v2(repo):
    _write(repo, ROW, V1)
    _git(repo, "add", "--", ROW)
    _write(repo, ROW, V2)
    out = _call(repo)
    assert out["committed"] is True, out
    assert_final_version_landed(repo, ROW, V2.encode())


def test_h5_v2_written_after_the_pre_commit_snapshot(repo, monkeypatch):
    _checkpoint(repo, ROW, V1)
    real = terminal_commit._changed
    fired: list = []

    def changed_then_rewrite(snapshot, worktree_root, paths):
        answer = real(snapshot, worktree_root, paths)
        if not fired:
            fired.append(1)
            _write(worktree_root, ROW, V2)
        return answer

    monkeypatch.setattr(terminal_commit, "_changed", changed_then_rewrite)
    out = _call(repo)
    _credited_means_landed(out, repo, ROW)
