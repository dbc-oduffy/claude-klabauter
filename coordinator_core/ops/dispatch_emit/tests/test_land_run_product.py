"""dispatch.land_run product mode: one scoped commit over a run's declared writes and record dir."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, land_run_contract, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    UNCHANGED_SINCE_HALT_REASON,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
RUN = "plan-20261021T100000-aaaaaa"
SCRIPT = "run-a.mjs"
PLAN = "docs/plans/p.md"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
LAUNCHED: list = []


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _write(root, PLAN, "---\ntitle: p\n---\nbody\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    LAUNCHED.clear()
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: LAUNCHED.append(a) or True)
    return root


def _declare(root: Path, *paths: str, plan: str | None = PLAN) -> None:
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(paths)),), plan_path=plan
    )
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")


def _author(root: Path, rel: str, text: str) -> None:
    """Write ``rel`` as the run session would: a claim plus a recorded hash of the bytes written."""
    _write(root, rel, text)
    session_scope.touch(SELF, rel, cwd=str(root))
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


def _record_dir(root: Path) -> list:
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask\n")
    return [f"{RECORD_DIR_ROOT}/{RUN}/run.json", f"{RECORD_DIR_ROOT}/{RUN}/ask.md"]


def _land(root: Path, **extra) -> dict:
    params = {
        "script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
        "session_id": SELF, "run_id": RUN, **extra,
    }
    return land_run._handler(params, repo_root=root / ".git")


def _tree(root: Path, rev: str = "HEAD") -> list:
    return sorted(_git(["show", "--name-only", "--format=", rev], root).split())


def _message(root: Path, rev: str = "HEAD") -> str:
    return _git(["log", "-1", "--format=%B", rev], root)


def _dirty(root: Path) -> set:
    return {ln[3:] for ln in _git(["status", "--porcelain", "-uall"], root).splitlines()}


def test_lands_a_declared_write_and_the_record_dir_in_one_commit(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    record = _record_dir(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _land(repo)

    assert out["committed"] is True, out
    assert _git(["rev-parse", "HEAD~1"], repo).strip() == head
    assert _tree(repo) == sorted(["src/a.py", *record])
    assert "Lands-Chunks: C1" in _message(repo)
    assert "Inline-Review: applies rev-stem -- execute-review: 1 slices, 0 fixes" in _message(repo)
    assert set(out) <= land_run_contract.REPLY_KEYS | {"nothing_to_commit"}, set(out) - land_run_contract.REPLY_KEYS
    assert out["review_stamp"] == "deferred" and out["plan_status"] == "deferred"
    assert out["sizing_shipped"] is None


def test_a_call_carrying_only_task_output_path_lands_like_inline_params(repo, tmp_path):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    record = _record_dir(repo)
    digest = {"result": {"next_action": {"params": {
        "script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
    }}}}
    output = tmp_path / "task-output.json"
    output.write_text(json.dumps(digest), encoding="utf-8")

    out = land_run._handler(
        {"task_output_path": str(output), "session_id": SELF, "run_id": RUN}, repo_root=repo / ".git"
    )

    assert out["committed"] is True, out
    assert _tree(repo) == sorted(["src/a.py", *record])


def test_the_run_token_admits_one_landing_and_a_repeat_answers_already_landed(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    _record_dir(repo)
    token = run_token.mint(repo, RUN, SCRIPT, session_id=SELF)

    first = _land(repo, run_token=token)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    second = _land(repo, run_token=token)

    assert first["committed"] is True and first["sha"] == head, first
    assert second["committed"] is True and second["already_landed"] is True and second["sha"] == head, second
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert run_token.spent_receipt(repo, RUN, token, SCRIPT)["sha"] == head


def _ask_script(root: Path, *paths: str) -> None:
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(paths)),), plan_path=PLAN)
    _write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker,
    )
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")


def test_an_unmet_ledger_refuses_before_any_write_and_leaves_the_token_redeemable(repo, monkeypatch):
    _author(repo, "src/a.py", "a = 1\n")
    _ask_script(repo, "src/a.py")
    token = run_token.mint(repo, RUN, SCRIPT, session_id=SELF)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    refusal = {"committed": False, "sha": None, "error": "unmet", "refused": "ledger-unmet"}
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (refusal, None, []))

    out = _land(repo, run_token=token)

    assert out["refused"] == "ledger-unmet"
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert not (repo / RECORD_DIR_ROOT / RUN / "usage.json").exists()
    assert run_token.admits(repo, RUN, token, SCRIPT)


def test_an_ask_run_stamps_usage_as_the_landed_marker_and_lands_its_plan(repo, monkeypatch):
    _author(repo, "src/a.py", "a = 1\n")
    _ask_script(repo, "src/a.py")
    _write(repo, PLAN, "---\ntitle: p\n---\nbody edited\n")
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))

    out = _land(repo)

    assert out["committed"] is True, out
    usage = json.loads((repo / RECORD_DIR_ROOT / RUN / "usage.json").read_text(encoding="utf-8"))
    assert usage["as_of"] == "terminal-commit"
    assert f"{RECORD_DIR_ROOT}/{RUN}/usage.json" in _tree(repo)
    assert PLAN in _tree(repo)


def test_blob_drift_refuses_and_leaves_the_token_redeemable(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    _record_dir(repo)
    token = run_token.mint(repo, RUN, SCRIPT, session_id=SELF)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    _write(repo, "src/a.py", "a = 2  # edited after the session's last recorded write\n")

    out = _land(repo, run_token=token)

    assert out["committed"] is False and out["refused"] == "blob-drift", out
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert run_token.admits(repo, RUN, token, SCRIPT)


def test_a_refused_call_leaves_the_token_redeemable(repo):
    _declare(repo, "src/a.py")
    token = run_token.mint(repo, RUN, SCRIPT, session_id=SELF)

    out = _land(repo, run_token=token, inline_review={"slices": 1})

    assert out["refused"] == "unreviewed"
    assert "stranded" not in out
    assert run_token.admits(repo, RUN, token, SCRIPT)


def test_missing_run_outcome_names_every_absent_param(repo):
    out = land_run._handler({"script_path": SCRIPT}, repo_root=repo / ".git")
    assert out["refused"] == "missing-run-outcome"
    assert out["missing"] == ["incomplete_chunks", "inline_review"]


def test_undeclared_dirt_is_never_staged_and_surfaces_in_neither_list(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _author(repo, "src/claimed_but_undeclared.py", "x = 1\n")
    _declare(repo, "src/a.py")
    record = _record_dir(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert _tree(repo) == sorted(["src/a.py", *record])
    assert "src/claimed_but_undeclared.py" in _dirty(repo)
    assert "src/claimed_but_undeclared.py" not in json.dumps(out)
    assert out["stranded"] == {}


def test_a_peer_held_declared_path_is_withheld_whole_not_a_run_wide_refusal(repo):
    _write(repo, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "C1", "writes": ["src/a.py"]}]}))
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    record = _record_dir(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert _tree(repo) == sorted(record)
    assert "src/a.py" in _dirty(repo)
    assert out["stranded"] == {"C1": ["src/a.py"]}
    assert "Row-Unlanded: C1 src/a.py peer-held" in _message(repo)
    assert "Lands-Chunks" not in _message(repo)


def test_the_close_job_is_written_and_launched_once_with_the_landed_sha(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    _record_dir(repo)

    out = _land(repo)

    key = Path(SCRIPT).stem
    record = json.loads((repo / ".coordinator-local" / "close-jobs" / f"{key}.json").read_text(encoding="utf-8"))
    assert record["terminal_sha"] == out["sha"]
    assert record["inputs"]["credited_rows"] == ["C1"]
    assert "review-stamp" in record["legs"] and record["legs"]["review-stamp"]["status"] == "queued"
    assert len(LAUNCHED) == 1
    assert out["close"]["legs"]["review-stamp"]["status"] == "queued"


def test_scratch_is_never_committed_and_is_named(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _author(repo, "scratch/tmp.txt", "x\n")
    _declare(repo, "src/a.py", "scratch/tmp.txt")
    _record_dir(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert "scratch/tmp.txt" not in _tree(repo)
    assert "Row-Unlanded: C1 scratch/tmp.txt never-commit" in _message(repo)


def test_an_undeclared_deletion_of_a_tracked_prefix_claim_refuses_the_whole_commit(repo):
    _write(repo, "pre/keep.py", "k\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "pre"], repo)
    (repo / "pre" / "keep.py").unlink()
    _write(repo, "rep.md", "created-under-prefix: pre/keep.py\nDONE: x\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=(), prefixes=("pre",), report="rep.md"),), plan_path=PLAN
    )
    _write(repo, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    _record_dir(repo)

    out = _land(repo)

    assert out["committed"] is False
    assert "undeclared deletion" in out["error"]


def test_a_declared_deletion_lands_as_a_removal(repo):
    _write(repo, "src/old.py", "o\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "old"], repo)
    (repo / "src" / "old.py").unlink()
    _declare(repo, "src/old.py")
    _record_dir(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert _git(["ls-files", "src/old.py"], repo).strip() == ""
    assert "Removes declared write(s): src/old.py" in _message(repo)


def _declare_two(root: Path) -> None:
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),
            ChunkCommit(id="C2", title="t2", paths=("src/b.py",)),
        ),
        plan_path=PLAN,
    )
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")


def test_a_held_chunk_is_stranded_and_its_writes_stay_dirty(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _author(repo, "src/b.py", "b = 1\n")
    _declare_two(repo)
    _record_dir(repo)

    out = _land(repo, held=[{"chunk": "C1", "reason": "waiting"}])

    assert out["committed"] is True, out
    assert "src/a.py" not in _tree(repo) and "src/b.py" in _tree(repo)
    assert "src/a.py" in _dirty(repo)
    assert out["incomplete_reasons"] == {"C1": "held: waiting"}
    assert out["stranded"] == {"C1": ["src/a.py"]}
    assert "Lands-Chunks: C2" in _message(repo)


def test_nothing_credited_landing_refuses_and_names_the_stranded_work(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    _record_dir(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _land(repo, held=[{"chunk": "C1", "reason": "waiting"}])

    assert out["committed"] is False and out["refused"] == "no-credited-chunk-landed", out
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_no_credited_landing_halt_names_credit_chunks_before_finish_or_land(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    _record_dir(repo)

    out = _land(repo, held=[{"chunk": "C1", "reason": "waiting"}])

    msg = out["error"]
    assert "credit_chunks" in msg and "Finish or land" not in msg
    assert msg.index("credit_chunks") < msg.index("finish or land")


def test_mode_ambiguous_params_are_refused(repo):
    out = land_run._handler(
        {"halted_by": {"halted": "x"}, "grind": {"rows": []}}, repo_root=repo / ".git"
    )
    assert out["refused"] == "mode-ambiguous"


def _halt_fixture(root: Path):
    _write(root, "prod.py", "x = 1\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "base"], root)
    _write(root, "prod.py", "x = 2\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "halt"], root)
    halt = _git(["rev-parse", "HEAD"], root).strip()
    _write(root, "prod.py", "x = 1\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "revert"], root)
    _write(root, "prod.py", "x = 2\n")  # the halt's own bytes, left dirty against HEAD
    return halt


def _resume(halt: str, **extra) -> dict:
    return {"run_id": RUN, "halt_sha": halt, "blocking": ["R1"], **extra}


def _verdict() -> dict:
    return {"items": [{"id": "R1", "status": "met"}]}


def test_since_halt_with_no_declared_paths_refuses_the_met_row(repo):
    halt = _halt_fixture(repo)
    detail: dict = {}

    refused = land_run._unchanged_since_halt(repo, _resume(halt), _verdict(), None, None, detail)

    assert refused and refused[0]["reason"] == UNCHANGED_SINCE_HALT_REASON
    assert detail["outcome"] == "no-declared-paths"


def test_since_halt_reads_the_declared_paths_from_the_manifest(repo):
    halt = _halt_fixture(repo)
    manifest = repo / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "R1", "writes": ["prod.py"]}]}), encoding="utf-8")
    detail: dict = {}

    unchanged = land_run._unchanged_since_halt(repo, _resume(halt), _verdict(), manifest, None, detail)
    withheld = land_run._unchanged_since_halt(
        repo, _resume(halt, run_artifacts={"withheld": {"prod.py": "peer-held"}}), _verdict(), manifest, None, detail
    )
    _write(repo, "prod.py", "x = 3\n")
    changed = land_run._unchanged_since_halt(repo, _resume(halt), _verdict(), manifest, None, detail)

    assert unchanged and unchanged[0]["reason"] == UNCHANGED_SINCE_HALT_REASON
    assert detail["outcome"] == "unchanged" and detail["base"] == halt
    assert withheld == []
    assert changed == []


def test_since_halt_exemptions_owe_no_diff(repo):
    halt = _halt_fixture(repo)
    manifest = repo / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "R1", "writes": ["prod.py"]}]}), encoding="utf-8")

    verified = land_run._unchanged_since_halt(
        repo, _resume(halt, verified_only={"R1": {"command": "pytest", "observed": "3 passed"}}),
        _verdict(), manifest, None, {},
    )
    landed = land_run._unchanged_since_halt(repo, _resume(halt, unlanded=[]), _verdict(), manifest, None, {})

    assert verified == [] and landed == []
