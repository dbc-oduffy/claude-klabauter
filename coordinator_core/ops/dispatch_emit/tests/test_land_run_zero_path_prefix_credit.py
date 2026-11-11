"""dispatch.land_run refuses to credit a row that declares a prefix, lands no path and leaves files under it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.ops.dispatch_emit import credit_gate, land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000e5"
RUN = "ask-20261110T100000-eeeeee"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-aaaaaa"
PLAN = "docs/plans/p.md"
REPORT_X1 = "reports/X1.md"
REPORT_X2 = "reports/X2.md"
PRODUCT = "exp/r5/a.txt"
PREFIX = "docs/decisions/"
DRS = (PREFIX + "DR-1.md", PREFIX + "DR-2.md")
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
        touch_record.sink_path(session_dir_for(root / ".git", SELF)),
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


def _declare(root: Path) -> str:
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="X1", title="t", paths=(PRODUCT,), report=REPORT_X1),
            ChunkCommit(id="X2", title="t2", paths=(), prefixes=(PREFIX,), report=REPORT_X2),
        ),
        deliverable_id=DLV, plan_path=PLAN,
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


def _land(root: Path, token: str) -> dict:
    review = {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": [PRODUCT], "foreign_claims": [], "slice_files": [PRODUCT]},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": ["X1"], "inline_review": review,
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _stage(root: Path, x2_report: str, *, authored: tuple = DRS) -> str:
    _author(root, PRODUCT, "product\n")
    for rel in authored:
        _author(root, rel, f"{rel}\n")
    _write(root, REPORT_X1, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    _write(root, REPORT_X2, x2_report)
    return _declare(root)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def test_a_credited_prefix_row_with_no_landed_path_and_files_under_its_prefix_is_refused(repo):
    token = _stage(repo, "DONE: X2\ncreated-under-prefix: none\n")
    before = _head(repo)

    out = _land(repo, token)

    assert out.get("refused") == credit_gate.REFUSED_CREDITED_PREFIX_ZERO_PATHS, out
    assert out["unlanded"] == {"X2": list(DRS)}
    assert _head(repo) == before


def test_the_same_row_lands_when_its_report_lists_the_files_after_a_bare_label(repo):
    token = _stage(repo, "DONE: X2\ncreated-under-prefix:\n" + "\n".join(DRS) + "\n")

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert set(DRS) <= set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())


def test_the_same_row_lands_when_nothing_sits_under_its_prefix(repo):
    token = _stage(repo, "DONE: X2\ncreated-under-prefix: none\n", authored=())

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert "Lands-Chunks" in _git(repo, "log", "-1", "--format=%B", "HEAD")
