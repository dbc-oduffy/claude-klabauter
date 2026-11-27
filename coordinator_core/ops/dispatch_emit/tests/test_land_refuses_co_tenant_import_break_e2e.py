"""The 92bd83 shape end to end: run X halted or landing a shared file whose co-tenant run Y holds an
uncommitted hunk importing a name only Y's uncommitted edit to ``a.py`` defines. The halted print, the
product land and the no-snapshot terminal commit never carry Y's hunk into a committed tree that fails to
import against HEAD."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, land_run, live_run_index, peer_hunk_gate, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER, HALTED_RECORD_PARAM, RECORD_DIR_ROOT, RUN_DIR_ROOT, StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests import test_land_run_unclaimed_peer_dirt_own_hunks as base
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_unclaimed_peer_dirt_own_hunks import (  # noqa: F401
    repo as _land_repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    RUN as HALT_RUN,
    repo as halt_repo,
)
from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TARGET = base.TARGET
Y_RUN = "ask-20261125T120000-bbbbbb"
Y_SESSION = "sess-y"
HEAD_TEXT = "".join(f"v{i} = {i}\n" for i in range(1, 13))
PEER_TEXT = HEAD_TEXT.replace("v1 = 1\n", "from a import NAME\n")
OWN_ONLY = HEAD_TEXT.replace("v10 = 10\n", "v10 = 100\n")
BOTH = PEER_TEXT.replace("v10 = 10\n", "v10 = 100\n")
A_HEAD = "BASE = 1\n"
A_DIRT = "BASE = 1\nNAME = 2\n"
Y_HUNK = "from a import NAME"


def _git(root: Path, *args: str) -> str:
    return base._git(root, *args)


def _head_text(root: Path, rel: str = TARGET) -> str:
    return _git(root, "show", f"HEAD:{rel}")


def _commit_head(root: Path) -> None:
    base._write(root, "a.py", A_HEAD)
    base._write(root, TARGET, HEAD_TEXT)
    _git(root, "add", "a.py", TARGET)
    _git(root, "commit", "-q", "-m", "head")


def _y_live(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run Y: a launched, live peer whose staged footprint names the shared file and ``a.py``."""
    real = touch_record.session_live
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: sid == Y_SESSION or real(sid, cwd))
    footprint = [TARGET, "a.py"]
    run_token.mint(root, Y_RUN, "y.mjs", marker=True, session_id=Y_SESSION)
    live_run_index.stage(root, Y_RUN, footprint, time.time())
    manifest = {"rows": [{"writes": footprint}], "review_declared_paths": [], "doc_writes": []}
    base._write(root, f"{RUN_DIR_ROOT}/{Y_RUN}/manifest.json", json.dumps(manifest))


def _imports_against_head(root: Path) -> None:
    """Execute HEAD's ``a.py`` and shared file as modules; an ImportError is the 92bd83 break."""
    sources = {"a": _head_text(root, "a.py"), "e2e_shared": _head_text(root)}
    saved = {k: sys.modules.get(k) for k in sources}
    try:
        for name, text in sources.items():
            module = types.ModuleType(name)
            sys.modules[name] = module
            exec(compile(text, name, "exec"), module.__dict__)  # noqa: S102 -- a fixture's own text
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


@pytest.fixture
def scene(_land_repo) -> Path:
    """HEAD holds ``a.py`` without NAME and a shared file of twelve assignments; the worktree holds Y's
    uncommitted NAME definition beside Y's uncommitted hunk importing it."""
    _commit_head(_land_repo)
    base._write(_land_repo, "a.py", A_DIRT)
    return _land_repo


def _x_stage(root: Path, *, baselined: bool) -> str:
    """Run X (``base.RUN``) baselines the tree holding Y's hunk, then writes its own hunk."""
    base._write(root, TARGET, PEER_TEXT)
    baseline = wb.capture(root, [TARGET]) if baselined else {}
    base._author(root, TARGET, BOTH)
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=(TARGET,)),), deliverable_id=base.DLV, plan_path=base.PLAN,
    )
    run_dir = f"{RUN_DIR_ROOT}/{base.RUN}"
    base._write(root, f"{run_dir}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=run_dir, rows=(), review_declared_paths=(), marker_path=f"{run_dir}/commit-request.txt",
        write_baseline=baseline,
    )
    base._write(root, f"{run_dir}/manifest.json", json.dumps(manifest.to_json()))
    base._write(root, f"{base.RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{run_dir}/manifest.json\n")
    return run_token.mint(root, base.RUN, f"{base.RUN}.mjs", session_id=base.SELF)


def _x_lands(root: Path, token: str) -> dict:
    review = {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": [TARGET], "foreign_claims": [], "slice_files": [TARGET]},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }
    return land_run._handler(
        {
            "script_path": f"{base.RUN}.mjs", "incomplete_chunks": [], "inline_review": review,
            "session_id": base.SELF, "run_id": base.RUN, "run_token": token,
        },
        repo_root=root / ".git",
    )


def _halted_scene(root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """X halted at the ledger check withholding the shared file; its recorded baseline holds Y's hunk."""
    _commit_head(root)
    base._write(root, TARGET, PEER_TEXT)
    peer_blob = _git(root, "hash-object", "-w", TARGET).strip()
    base._write(root, "a.py", A_DIRT)
    base._write(root, TARGET, BOTH)
    record = f"{RECORD_DIR_ROOT}/{HALT_RUN}"
    base._write(root, f"{record}/halted.json", json.dumps({
        "status": "halted", "halted": "ledger-check", "run_artifacts": {"withheld": {TARGET: "halt-record-only"}},
    }))
    base._write(root, f"{record}/manifest.json", json.dumps({"rows": [], "write_baseline": {TARGET: peer_blob}}))
    _y_live(root, monkeypatch)
    return record


def test_the_halted_record_prints_the_shared_file_as_own_hunk_or_co_tenant_never_the_whole_tree(halt_repo, monkeypatch) -> None:
    record = _halted_scene(halt_repo, monkeypatch)

    out = terminal_commit._handler({"run_id": HALT_RUN, HALTED_RECORD_PARAM: LEDGER}, repo_root=halt_repo / ".git")

    prints = json.loads((halt_repo / record / WITHHELD_PRINTS_FILE).read_text(encoding="utf-8"))
    printed = prints[TARGET]
    assert printed != blob_sha(BOTH.encode()), (printed, out)
    assert printed in (blob_sha(OWN_ONLY.encode()), {"kind": "co_tenant", "owner": Y_RUN}), printed
    assert Y_HUNK not in _head_text(halt_repo), out
    _imports_against_head(halt_repo)


def test_a_land_of_the_separable_shared_file_commits_nothing_carrying_the_co_tenant_hunk(scene, monkeypatch) -> None:
    _y_live(scene, monkeypatch)
    token = _x_stage(scene, baselined=True)

    out = _x_lands(scene, token)

    assert out.get("committed") is False and out.get("refused") == "no-credited-chunk-landed", out
    assert _head_text(scene) == HEAD_TEXT and _head_text(scene, "a.py") == A_HEAD
    assert (scene / TARGET).read_text(encoding="utf-8") == BOTH
    _imports_against_head(scene)


def test_a_land_of_the_unseparable_shared_file_never_carries_the_co_tenant_hunk(scene, monkeypatch) -> None:
    _y_live(scene, monkeypatch)
    token = _x_stage(scene, baselined=False)

    out = _x_lands(scene, token)

    assert out.get("committed") is not True and Y_HUNK not in _head_text(scene), out
    assert _head_text(scene, "a.py") == A_HEAD, out
    assert (scene / TARGET).read_text(encoding="utf-8") == BOTH
    _imports_against_head(scene)


def test_terminal_commit_without_a_residue_snapshot_refuses_an_image_importing_an_uncommitted_name(halt_repo, monkeypatch) -> None:
    root = halt_repo
    _commit_head(root)
    base._write(root, "a.py", A_DIRT)
    base._write(root, TARGET, BOTH)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "e2e-self")
    monkeypatch.setattr(terminal_commit, "_worktree_snapshot", lambda *_a, **_k: None)
    head = _git(root, "rev-parse", "HEAD").strip()
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(TARGET,)),))
    base._write(root, "run.mjs", "// emitted\n" + render_marker(request) + "\n")

    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": reviewed()}, repo_root=root / ".git",
    )

    assert out["committed"] is False and out["refused"] == landing_integrity.LOST_DEFINITION, out
    assert out["integrity"][0]["path"] == TARGET
    assert _git(root, "rev-parse", "HEAD").strip() == head


def test_after_y_lands_x_lands_and_the_committed_tree_imports(scene) -> None:
    base._write(scene, TARGET, PEER_TEXT)
    _git(scene, "add", "a.py", TARGET)
    _git(scene, "commit", "-q", "-m", "y lands")
    token = _x_stage(scene, baselined=True)

    out = _x_lands(scene, token)

    assert out.get("committed") is True, out
    assert _head_text(scene) == BOTH
    _imports_against_head(scene)


def test_the_holding_and_import_checks_spawn_nothing(scene, monkeypatch) -> None:
    _y_live(scene, monkeypatch)
    _x_stage(scene, baselined=False)

    def boom(*_a, **_k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)

    held = peer_hunk_gate.landing_holds(scene, base.SELF, base.RUN, base.PLAN, [TARGET], [], [TARGET], [TARGET], None)
    found = landing_integrity.committed_image_import_findings(
        scene, [TARGET], [], {TARGET: BOTH.encode()}, residue_py=None,
    )

    assert TARGET in held
    assert [f.reason for f in found] == [landing_integrity.LOST_DEFINITION]
