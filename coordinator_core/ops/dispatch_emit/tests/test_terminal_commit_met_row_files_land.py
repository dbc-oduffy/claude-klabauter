"""dispatch.terminal_commit lands or refuses every dirty file a met ledger row cites, and never
hands a gitignored or machine-local path to the commit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.terminal_commit_ledger import met_row_files
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run1"
SIZING = "state/sizings/20260101-a.yaml"
_SID = "11111111-2222-3333-4444-555555555555"
_SETTER = "coordinator/bin/setter.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(root, _SETTER, "old\n")
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship a.py\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def _stage(repo: Path, paths: tuple = ("a.py",)) -> None:
    _write(repo, "a.py", "a\n")
    _write(repo, "reports/C1.md", "DONE: wired\n")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=paths, report="reports/C1.md"),))
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")


def _review() -> dict:
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": reviewed()["wave_reviews"],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1},
        "integration": {"sidecar": "s", "unresolved": [], "confinement_violations": 0},
    }


def _call(repo: Path, verdict: dict) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _review(),
        "session_id": _SID, "ledger_verdict": verdict,
    }
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _met(**fields) -> dict:
    return {"items": [{"id": "R1", "status": "met", "wired": True, **fields}]}


def test_met_row_file_left_dirty_refuses_and_names_it(repo):
    _stage(repo)
    _write(repo, _SETTER, "new\n")
    out = _call(repo, _met(root_read=_SETTER))
    assert out.get("refused") == terminal_commit.POST_COMMIT_PATH_UNLANDED, out
    assert out.get("unlanded") == [_SETTER], out
    assert _SETTER in out["error"]
    assert "a.py" not in _git(repo, "ls-files")


def test_met_row_file_cited_by_met_by_refuses_too(repo):
    _stage(repo)
    _write(repo, _SETTER, "new\n")
    out = _call(repo, _met(met_by=f"edited {_SETTER}"))
    assert out.get("refused") == terminal_commit.POST_COMMIT_PATH_UNLANDED, out


def test_met_row_file_in_the_landed_set_commits(repo):
    _stage(repo, ("a.py", _SETTER))
    _write(repo, _SETTER, "new\n")
    out = _call(repo, _met(root_read=_SETTER))
    assert out["committed"] is True, out
    assert _SETTER in _git(repo, "log", "--name-only", "--format=")


def test_met_row_naming_a_clean_file_does_not_refuse(repo):
    _stage(repo)
    out = _call(repo, _met(root_read=_SETTER))
    assert out["committed"] is True, out


def test_gitignored_and_machine_local_paths_never_ride_the_commit(repo):
    ignored = "scratch/gen/f.txt"
    local = ".coordinator-local/gen/r.md"
    _write(repo, "a.py", "a\n")
    _write(repo, ignored, "x\n")
    _write(repo, local, "r\n")
    _write(repo, "reports/C1.md", f"DONE: wired\ncreated-under-prefix: {ignored}\ncreated-under-prefix: {local}\n")
    request = CommitRequest(chunks=(ChunkCommit(
        id="C1", title="t", paths=("a.py",), prefixes=("scratch/gen/", ".coordinator-local/gen/"),
        report="reports/C1.md",
    ),))
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    out = _call(repo, _met())
    assert out["committed"] is True, out
    landed = _git(repo, "log", "--name-only", "--format=")
    assert "a.py" in landed
    assert "scratch/" not in landed and ".coordinator-local" not in landed


def test_met_row_files_names_only_existing_files_of_met_items(repo):
    verdict = {"items": [
        {"id": "R1", "status": "met", "root_read": [_SETTER, "coordinator/bin", "missing.py"]},
        {"id": "R2", "status": "not_met", "root_read": "README.md"},
    ]}
    assert met_row_files(repo, verdict) == (_SETTER,)
