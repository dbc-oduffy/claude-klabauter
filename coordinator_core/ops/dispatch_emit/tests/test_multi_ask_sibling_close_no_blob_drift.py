"""Two children of one multi-ask share a session. Child B's own size stage rewrites its sizing and
record file after the expectation was taken: the terminal commit re-reads them and lands with no
blob-drift, while a product-path drift still refuses. On the ask-run path the delivery note the
call itself rewrites is declared at its current bytes and is not re-read."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.review_mint import wave_bookkeeping
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

SID = "11111111-2222-3333-4444-555555555555"
PARENT = "ask-parent-1"
RUN_A = "ask-a"
RUN_B = "ask-b"
SIZING = {RUN_A: "state/sizings/a.yaml", RUN_B: "state/sizings/b.yaml"}
PRODUCT = {RUN_A: "foo_a.py", RUN_B: "foo_b.py"}
B_RECORD = f"state/warp/{RUN_B}/notes.jsonl"
B_NOTE = f"docs/delivery/{RUN_B}.md"
SEED = "seed\n"


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _touch(repo, rel, text, kind, ts):
    touch_record.append_event(
        touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SID),
        session_id=SID, agent_id=None, verb="T", path=rel,
        content_hash=_sha(text), kind=kind, timestamp=ts,
    )


def _sizing_text(run_id: str, body: str) -> str:
    return f"em_analysis:\n  ask_parent: {contract.ask_parent_marker(PARENT, run_id)}\n  {body}\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for run_id in (RUN_A, RUN_B):
        _write(root, SIZING[run_id], _sizing_text(run_id, "premise: seed"))
        _write(root, PRODUCT[run_id], SEED)
    _write(root, B_RECORD, SEED)
    _write(root, B_NOTE, SEED)
    _write(root, "README.md", SEED)
    _write(root, "docs/plans/ex.md", "---\nplan_id: pln-ex-abc123\n---\n# Ex\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)

    parent = contract.ParentRecord(
        parent_id=PARENT,
        runs=tuple(
            contract.SiblingRun(run_id=r, label=r, slice_sizing=SIZING[r]) for r in (RUN_A, RUN_B)
        ),
    )
    for run_id in (RUN_A, RUN_B):
        _write(root, contract.run_parent_copy_rel(run_id), json.dumps(parent.to_json()))
        rec = warp_run_deliverable.record_dir(root, run_id)
        rec.mkdir(parents=True, exist_ok=True)
        (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING[run_id]}), encoding="utf-8")
    return root


def _fire(root, monkeypatch, tmp_path, run_id, declared, ask_run=False):
    outside = tmp_path / "record.md"
    outside.write_text("record\n", encoding="utf-8", newline="")
    monkeypatch.setattr(
        wave_bookkeeping, "bookkeep_wave",
        lambda *a, **k: {"sidecar_path": str(outside), "review_firings": [{"leg": "r"}]},
    )
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(declared)),),
        session_id=SID, plan_path="docs/plans/ex.md",
    )
    script = f"{run_id}.mjs"
    if ask_run:
        run_dir = root / ask_contract.RUN_DIR_ROOT / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
        manifest = ask_contract.StageManifest(
            run_dir=f"{ask_contract.RUN_DIR_ROOT}/{run_id}", rows=(), review_declared_paths=(),
            marker_path=f"{ask_contract.RUN_DIR_ROOT}/{run_id}/commit-request.txt",
        )
        (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
        body = f"// s\n{ask_contract.ASK_MANIFEST_MARKER}{ask_contract.RUN_DIR_ROOT}/{run_id}/manifest.json\n"
    else:
        body = "// s\n" + render_marker(request) + "\n"
    (root / script).write_text(body, encoding="utf-8", newline="")
    token = run_token.mint(root, run_id, script)
    seen: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, **kw):
        if op == "ceremony.commit_v2":
            seen.append(dict(params))
        return real(op, params, **kw)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": SID, "run_id": run_id,
         "run_token": token,
         "inline_review": {
             "integration_stem": "rev-stem", "slices": 1, "fixes": 0, "prep_sidecar": "prep.md",
             "plan_id": "pln-ex-abc123", "wave_sidecar_paths": ["w.md"],
         }},
        repo_root=root / ".git",
    )
    return out, seen


def _expect_b(root, product_drift: bool):
    """Record B's expectations at the seed bytes (B's sizing as a READ), then B's own edit and the
    own-stage rewrites with no touch event."""
    for i, rel in enumerate((SIZING[RUN_B], B_RECORD, B_NOTE, PRODUCT[RUN_B])):
        _touch(root, rel, SEED if rel != SIZING[RUN_B] else _sizing_text(RUN_B, "premise: seed"),
               touch_record.KIND_READ if rel == SIZING[RUN_B] else touch_record.KIND_WRITE, 1.0 + i)
    if product_drift:
        _write(root, PRODUCT[RUN_B], "drifted\n")
    else:
        _write(root, PRODUCT[RUN_B], SEED + "# edit\n")
        _touch(root, PRODUCT[RUN_B], SEED + "# edit\n", touch_record.KIND_WRITE, 9.0)


def test_ask_run_path_delivery_note_rewrite_lands_and_is_not_reread(repo, tmp_path, monkeypatch):
    _expect_b(repo, product_drift=False)
    _write(repo, SIZING[RUN_B], _sizing_text(RUN_B, "premise: filled by the size stage"))
    _write(repo, B_RECORD, SEED + '{"stage": "size"}\n')
    _write(repo, B_NOTE, SEED + "note rewritten after the expectation\n")
    out, seen = _fire(repo, monkeypatch, tmp_path, RUN_B, [PRODUCT[RUN_B], SIZING[RUN_B], B_RECORD], ask_run=True)
    assert out["committed"] is True, out
    assert sorted(out["run_artifacts"]["reread"]) == sorted([SIZING[RUN_B], B_RECORD])
    expected = seen[0]["expected_content_sha256"]
    assert expected[B_NOTE] == _sha((repo / B_NOTE).read_text(encoding="utf-8"))
    assert SIZING[RUN_B] not in expected


def test_child_bs_own_stage_rewrites_are_reread_and_land(repo, tmp_path, monkeypatch):
    declared = [PRODUCT[RUN_B], SIZING[RUN_B], B_RECORD, B_NOTE]
    _expect_b(repo, product_drift=False)
    _write(repo, SIZING[RUN_B], _sizing_text(RUN_B, "premise: filled by the size stage"))
    _write(repo, B_RECORD, SEED + '{"stage": "size"}\n')
    _write(repo, B_NOTE, SEED + "note\n")
    out, seen = _fire(repo, monkeypatch, tmp_path, RUN_B, declared)
    assert out["committed"] is True, out
    assert sorted(out["run_artifacts"]["reread"]) == sorted([SIZING[RUN_B], B_RECORD, B_NOTE])
    expected = seen[0].get("expected_content_sha256", {})
    assert SIZING[RUN_B] not in expected
    assert B_RECORD not in expected


def test_child_bs_product_path_drift_still_refuses(repo, tmp_path, monkeypatch):
    declared = [PRODUCT[RUN_B], SIZING[RUN_B], B_RECORD, B_NOTE]
    _expect_b(repo, product_drift=True)
    _write(repo, SIZING[RUN_B], _sizing_text(RUN_B, "premise: filled by the size stage"))
    out, _ = _fire(repo, monkeypatch, tmp_path, RUN_B, declared)
    assert out["committed"] is False, out
    assert out["refused"] == "blob-drift"
    assert [d["path"] for d in out["blob_drift"]] == [PRODUCT[RUN_B]]
