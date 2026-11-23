"""A measured-touched path outside the run's write_baseline lands as the run's own: only a proven-foreign
hunk (not in the run's write print, inside a peer holder's baseline delta) is split out of the overlay and
the commit; with no proof the whole file lands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.ops.dispatch_emit import hunk_provenance, land_run, measured_rev, run_token, run_write_prints
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import unclaimed_dirt_stage as uds
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c8"
RUN = "ask-20261116T100000-c8c8c8"
PEER_RUN = "ask-20261116T090000-aaaaaa"
SIZING = "state/sizings/20260101-a.yaml"
DLV = "dlv-alpha-c8c8c8"
PLAN = "docs/plans/p.md"
REPORT = "reports/X1.md"
DECLARED = "pkg/declared.py"
TARGET = "pkg/undeclared.py"
SEED = "".join(f"line{i}\n" for i in range(1, 13))
VERDICT = {"items": [{"id": "R1", "status": "met", "met_by": "a.py", "wired": True}]}
OWN = SEED.replace("line10\n", "own10\n")
BOTH = OWN.replace("line1\n", "peer1\n")


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
    _write(root, DECLARED, SEED)
    _write(root, TARGET, SEED)
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


def _blob(root: Path, text: str) -> str:
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", text.encode("utf-8"))


def _declare(root: Path) -> str:
    """The run declares DECLARED only (baselined); TARGET is edited but outside its write_baseline."""
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=(DECLARED, TARGET), report=REPORT),),
        deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", write_baseline=wb.capture(root, [DECLARED]),
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    return run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)


def _stage(root: Path, monkeypatch, *, print_own: bool = True, peer: bool = True) -> str:
    """TARGET carries the run's line10 edit and a peer's line1 edit; the peer baseline is the seed."""
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, files written by the run\n")
    token = _declare(root)
    _author(root, DECLARED, OWN)
    _author(root, TARGET, BOTH)
    own_print = _blob(root, OWN)
    seed = _blob(root, SEED)
    monkeypatch.setattr(
        run_write_prints, "read",
        lambda r, run: {TARGET: own_print} if print_own and run == RUN else None,
    )
    monkeypatch.setattr(hunk_provenance, "_owners", lambda r, run, p, plan: [PEER_RUN] if peer else [])
    real_baseline = hha._baseline
    monkeypatch.setattr(
        hha, "_baseline", lambda r, run: {TARGET: seed} if run == PEER_RUN else real_baseline(r, run)
    )
    monkeypatch.setattr(hunk_provenance, "_own_chain", lambda *a: False)
    return token


def _land(root: Path, token: str) -> dict:
    slice_files = [DECLARED, TARGET]
    review = {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": slice_files, "foreign_claims": [], "slice_files": slice_files},
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


def test_a_proven_foreign_hunk_in_an_undeclared_touched_path_is_split_out(repo, monkeypatch):
    _stage(repo, monkeypatch)

    got = uds.touched_own_split(repo, RUN, [TARGET])

    assert set(got) == {TARGET}
    assert got[TARGET]["owner"] == PEER_RUN
    assert got[TARGET]["blob"] == _blob(repo, OWN)


def test_without_a_peer_holder_nothing_is_proven_foreign(repo, monkeypatch):
    _stage(repo, monkeypatch, peer=False)

    assert uds.touched_own_split(repo, RUN, [TARGET]) == {}


def test_without_own_evidence_nothing_is_proven_foreign(repo, monkeypatch):
    _stage(repo, monkeypatch, print_own=False)

    assert uds.touched_own_split(repo, RUN, [TARGET]) == {}


def test_a_baselined_path_is_left_to_the_baseline_attribution(repo, monkeypatch):
    _stage(repo, monkeypatch)

    assert uds.touched_own_split(repo, RUN, [DECLARED]) == {}


def test_measured_rev_carries_the_undeclared_path_own_only_in_the_overlay(repo, monkeypatch):
    _stage(repo, monkeypatch)

    record = measured_rev.measured_rev(RUN, [DECLARED, TARGET], repo)

    assert record["own_blobs"] == {TARGET: _blob(repo, OWN)}
    assert record["peer_hunks"][TARGET]["split"] is True
    assert TARGET in measured_rev.touched_paths(repo, RUN)
    assert _git(repo, "show", f"{record['overlay_sha']}:{TARGET}") == OWN
    assert _git(repo, "show", f"{record['overlay_sha']}:{DECLARED}") == OWN


def test_land_run_commits_the_undeclared_touched_path_own_only_and_leaves_the_peer_hunk(repo, monkeypatch):
    token = _stage(repo, monkeypatch)
    measured_rev.touched_record_path(repo, RUN).parent.mkdir(parents=True, exist_ok=True)
    measured_rev.touched_record_path(repo, RUN).write_text(json.dumps([DECLARED, TARGET]), encoding="utf-8")

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == OWN
    assert _git(repo, "show", f"HEAD:{DECLARED}") == OWN
    assert (repo / TARGET).read_text(encoding="utf-8") == BOTH
    assert TARGET in {ln[3:] for ln in _git(repo, "status", "--porcelain").splitlines()}


def test_land_run_lands_the_undeclared_path_whole_when_no_hunk_is_proven_foreign(repo, monkeypatch):
    token = _stage(repo, monkeypatch, peer=False)
    measured_rev.touched_record_path(repo, RUN).parent.mkdir(parents=True, exist_ok=True)
    measured_rev.touched_record_path(repo, RUN).write_text(json.dumps([DECLARED, TARGET]), encoding="utf-8")

    out = _land(repo, token)

    assert out.get("committed") is True, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == BOTH
