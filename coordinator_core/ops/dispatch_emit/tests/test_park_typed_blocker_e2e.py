"""End to end through `dispatch.stage_wait_status`: a run parked on a typed blocker resumes by the engine once the blocker clears, a dead blocker holds it for the PM, and one sweep over 50 records stays inside the brightline."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core import ipc, warp_live
from coordinator_core.ask_spool import record as spool
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout, ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import (
    ask_gate, halt_split_tree, pending_oracle, run_close_hand_landed, run_token, stage_wait, stranded_wait,
)
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_DEPENDENTS_UNSTARTED, RESUME_RUN_ID_ARG, RUN_DIR_ROOT, record_dir,
)
from coordinator_core.ops.dispatch_emit.ask_stage import StageWaiting, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL as GATE_REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import _put as put_gate_sizing
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL
from coordinator_core.session_baton import store
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.complete

A = "ask-20261127T090000-aaaaaa"
B = "ask-20261127T090000-bbbbbb"
SIZING_A = GATE_REL
SIZING_B = "state/sizings/2026-11/2026-11-27-blocker-bbbbbb.yaml"
SHA = "c" * 40
SID = "sid-park-e2e"
SCRIPT = "scratch/warp/x.mjs"
SWEEP_RECORDS = 50
SWEEP_BUDGET_MS = 50


def _git_repo(root: Path) -> Path:
    """A real repo: a bare `.git` directory resolves the engine root to `.git` itself."""
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(halt_split_tree, "send_live_gem", lambda *_a, **_k: False)
    return _git_repo(tmp_path / "repo")


@pytest.fixture
def submission_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(halt_split_tree, "send_live_gem", lambda *_a, **_k: False)
    root = _git_repo(tmp_path / "work")
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("sequence these", session_id=SID)["ok"]
    (root / "docs" / "plans").mkdir(parents=True)
    (root / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    (root / "scratch" / "warp").mkdir(parents=True)
    (root / "scratch" / "warp" / "mine.workflow.mjs").write_text("// run script\n", encoding="utf-8")
    return root


def _status(root: Path, run_id: str) -> dict:
    """`dispatch.stage_wait_status` through the op registry, as a caller outside the process reaches it."""
    return ipc.dispatch_from_hook("dispatch.stage_wait_status", {"run_id": run_id}, origin_worktree=str(root))


def _spooled(root: Path) -> list[dict]:
    return [
        json.loads(spool.read_entry(spool.entry_path(str(root), sid))[1])["tool_input"]["args"]
        for sid in spool.list_pending(str(root))
    ]


def _marker(root: Path, run_id: str, **fields: object) -> None:
    rdir = record_dir(root, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "halted.json").write_text(json.dumps(fields), encoding="utf-8")


def _park(root: Path, blockers: list, **kw: object) -> None:
    run_token.mint(root, A, "a.mjs", session_id="a-sess", sizing_rel=SIZING_A)
    stage_wait.park(root, A, blockers, ["pkg/a.py"], session_id="a-sess", sizing_rel=SIZING_A, **kw)


def test_run_end_blocker_consumed_reports_an_engine_refire_the_gate_admits(repo: Path) -> None:
    run_token.mint(repo, B, "b.mjs", session_id="b-sess", sizing_rel=SIZING_B)
    _park(repo, [B])
    waiting = _status(repo, A)
    assert waiting["status"] == "waiting" and waiting["blockers"] == [{"kind": "run_end", "ref": B, "state": "live"}]

    run_token.consume(repo, B)
    assert stranded_wait.on_idle_tick([repo]) is False

    reply = _status(repo, A)
    assert (reply["status"], reply["resumed_by"], reply["session"], reply["action"]) == ("resumed", "engine", None, "refire")
    (args,) = _spooled(repo)
    assert args[RESUME_RUN_ID_ARG] == A and args["sizing"] == SIZING_A

    (repo / "state" / "sizings").mkdir(parents=True)
    put_gate_sizing(repo)
    gate_params = {"sizing_path": args["sizing"], "run_id": args[RESUME_RUN_ID_ARG], "require_live_token": True}
    assert "spent or gone" in json.dumps(ask_gate._handler(gate_params, repo))
    run_token.mint(repo, A, f"{RUN_DIR_ROOT}/{A}.workflow.mjs", session_id="a-sess", sizing_rel=SIZING_A, carry_prior=True)
    assert "spent or gone" not in json.dumps(ask_gate._handler(gate_params, repo))


def test_run_end_blocker_that_ended_without_a_consume_is_resumed_by_the_idle_tick(repo: Path) -> None:
    _park(repo, [B])
    assert stranded_wait.on_idle_tick([repo]) is True
    reply = _status(repo, A)
    assert (reply["status"], reply["resumed_by"], reply["action"]) == ("resumed", "engine", "refire")
    assert len(_spooled(repo)) == 1
    assert stranded_wait.on_idle_tick([repo]) is False and len(_spooled(repo)) == 1


def test_run_landed_blocker_resumes_by_close_landed_at_the_marker_sha(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shipped: list[dict] = []

    def _ship(params: dict, repo_root: Path | None = None) -> dict:
        shipped.append(dict(params))
        return {"ok": True}

    monkeypatch.setattr(run_close_hand_landed, "_handler", _ship)
    _park(repo, [{"kind": "run_landed", "ref": B}], resume_action="close_landed")
    assert stranded_wait.on_idle_tick([repo]) is False and shipped == []

    _marker(repo, B, status="landed", sha=SHA)
    assert stranded_wait.on_idle_tick([repo]) is True

    assert shipped == [{"run_id": A, "sha": SHA}]
    reply = _status(repo, A)
    assert (reply["status"], reply["resumed_by"], reply["session"], reply["action"], reply["sha"]) == (
        "resumed", "engine", None, "close_landed", SHA,
    )
    assert _spooled(repo) == []
    assert stranded_wait.on_idle_tick([repo]) is False and len(shipped) == 1


def test_halted_dependents_unstarted_blocker_is_a_pm_flag_never_proceeded(repo: Path) -> None:
    _park(repo, [B])
    _marker(repo, B, status="halted", halted=HALT_DEPENDENTS_UNSTARTED, sizing_rel=SIZING_B)
    moment = time.time()
    spaced = stranded_wait.REFIRE_COOLDOWN_S + 1
    for n in range(stranded_wait.MAX_STRANDED_REFIRES + 2):
        stranded_wait.sweep(repo, now=moment + n * spaced)

    reply = _status(repo, A)
    assert reply["status"] == "attention" and reply["action"] is None and reply["resumed_by"] is None
    flag = Path(reply["flag"])
    assert flag.is_file() and json.loads(flag.read_text(encoding="utf-8"))["kind"] == "pending_oracle"
    record = stage_wait.read(repo, A)
    assert "proceeded_without" not in record and record["started"] is False
    assert _spooled(repo) == []
    assert _status(repo, A)["status"] == "attention"

    flag.unlink()
    assert stranded_wait.sweep(repo, now=moment + 10 * spaced)[0]["action"] == "pm-resolved"
    ended = _status(repo, A)
    assert (ended["status"], ended["resumed_by"], ended["session"]) == ("resumed", "pm", None)


def test_pending_oracle_flag_path_is_the_one_the_status_op_reports(repo: Path) -> None:
    _park(repo, [B])
    _marker(repo, B, status="halted", halted=HALT_DEPENDENTS_UNSTARTED, sizing_rel=SIZING_B)
    stranded_wait.sweep(repo, now=time.time())
    row = stranded_wait._flag_row("run_end", B)
    assert Path(_status(repo, A)["flag"]) == pending_oracle.flag_path(repo, A, row)


def test_status_op_reads_a_halted_blockers_marker_through_the_dispatch_root(repo: Path) -> None:
    _park(repo, [B])
    _marker(repo, B, status="halted", halted=HALT_DEPENDENTS_UNSTARTED, sizing_rel=SIZING_B)
    assert _status(repo, A)["blockers"] == [{"kind": "run_end", "ref": B, "state": "dead"}]


def _fanout(root: Path, asks: list[dict]) -> dict:
    return ask_fanout.fanout({"asks": asks, "session_id": SID}, root)


def _sizing_of(root: Path, intent: str) -> str:
    for path in (root / "state" / "sizings").rglob("*.yaml"):
        if yaml.safe_load(path.read_text(encoding="utf-8")).get("intent") == intent:
            return path.relative_to(root).as_posix()
    raise AssertionError(f"no sizing for {intent!r}")


def test_a_chain_of_submissions_unblocks_itself_one_per_blocker_kind(submission_repo: Path) -> None:
    root = submission_repo
    peer = "ask-20261127T080000-cccccc"
    run_token.mint(root, peer, SCRIPT, session_id=None)
    out = _fanout(root, [
        {"ask": "first", "label": "a"},
        {"ask": "second", "label": "b", "after": ["a"]},
        {"ask": "third", "label": "c", "after_run": {"run_id": peer, "kind": "run_landed"}},
    ])
    assert out["exit_code"] == 0, out
    run_of = {r["label"]: r["run_id"] for r in out["runs"]}
    docs = {i: yaml.safe_load((root / _sizing_of(root, i)).read_text(encoding="utf-8")) for i in ("second", "third")}
    assert contract.decode_stage_blockers(docs["second"]["em_analysis"]) == [{"kind": "run_end", "ref": run_of["a"]}]
    assert contract.decode_stage_blockers(docs["third"]["em_analysis"]) == [{"kind": "run_landed", "ref": peer}]

    for label, blocker_kind in (("b", "run_end"), ("c", "run_landed")):
        intent = {"b": "second", "c": "third"}[label]
        run_id, sizing = run_of[label], f"state/sizings/staged-{label}.yaml"
        staged = {"intent": intent, "exit_criterion": {"statement": "done"}, "deliverable_id": "dlv-xs-abc123",
                  "em_analysis": docs[intent]["em_analysis"]}
        (root / sizing).write_text(yaml.safe_dump(staged), encoding="utf-8", newline="\n")
        run_token.mint(root, run_id, SCRIPT, session_id=None, sizing_rel=sizing)
        with pytest.raises(StageWaiting) as parked:
            stage(root, run_id=run_id, sizing_rel=sizing, writes=["pkg/a.py"])
        assert [b["kind"] for b in parked.value.wait["typed_blockers"]] == [blocker_kind]

    run_token.mint(root, run_of["a"], SCRIPT, session_id=None)
    run_token.consume(root, run_of["a"])
    _marker(root, peer, status="landed", sha=SHA)
    run_token.discard(root, peer)
    stranded_wait.on_idle_tick([root])

    refired = {a[RESUME_RUN_ID_ARG] for a in _spooled(root)}
    assert refired == {run_of["b"], run_of["c"]}
    for label in ("b", "c"):
        reply = _status(root, run_of[label])
        assert (reply["status"], reply["resumed_by"], reply["session"], reply["action"]) == ("resumed", "engine", None, "refire")


def _park_fifty(root: Path, owners: int) -> None:
    for n in range(SWEEP_RECORDS):
        owner = f"ask-20261127T11{n % owners:04d}-{n % owners:06x}"
        if not run_token.is_live(root, owner):
            run_token.mint(root, owner, "o.mjs", session_id="o-sess")
        stage_wait.park(
            root, f"ask-20261127T10{n:04d}-{n:06x}", [owner], ["pkg/a.py"], session_id="w-sess", sizing_rel=SIZING_A,
        )


def _warm_sweep_ms(root: Path, sweeps: int = 20) -> float:
    """Mean process time of one steady-state sweep; the warm engine keeps its record cache between ticks."""
    assert stranded_wait.sweep(root, now=time.time()) == []
    start = time.process_time()
    for step in range(sweeps):
        assert stranded_wait.sweep(root, now=time.time() + step) == []
    return (time.process_time() - start) * 1000 / sweeps


def test_one_sweep_over_fifty_records_is_under_the_budget_with_no_spawn(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _park_fifty(repo, owners=5)
    spawns: list = []

    def _spawn(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("sweep spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _spawn)
    warm = _warm_sweep_ms(repo)
    print(f"SWEEP records={SWEEP_RECORDS} owners=5 warm_process_ms={warm:.2f} spawns={len(spawns)}")
    assert spawns == []
    assert warm < SWEEP_BUDGET_MS
