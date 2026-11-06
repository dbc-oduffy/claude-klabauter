"""A sentinel `em_context` submitted at default engine-ask args reaches the composed sizing-scout and plan-author prompts on every emit path.

Drives `submit_engine_ask.evaluate` for the params, then `dispatch.emit` on them as the relay would.
The minted-sizing case is `xfail(strict=True)` and names its drop site.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops import sizing_intake_contract as contract
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import ask_fanout as fanout
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.session_baton import store

SENTINEL = ("SENTINEL-EM-CONTEXT-7f3a " + "pinned citation form [rag:tool(args)]. " * 8).strip()
SID = "sid-arrival"
VERBATIM = "split these two jobs please, they are unrelated"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
SIZING_REL = "state/sizings/2026-11-06-arrival.yaml"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn(VERBATIM, session_id=SID)["ok"]
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


def _payload(root: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-1",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


def _inline_params(root: Path, args: dict) -> dict:
    """The params `evaluate` hands the emit at default budgets."""
    seen: list[dict] = []

    def emit(params, _root):
        seen.append(params)
        return {"ok": True, "path": "scratch/x.mjs", "run_id": "ask-20261106T000000-aaaaaa", "fire_args": {}}

    mod.evaluate(_payload(root, args), emit=emit)
    assert seen, "the hook never reached the emit"
    return seen[0]


def _deferred_params(root: Path, args: dict, monkeypatch) -> dict:
    """The params file a deferred script's relay reads, as the hook wrote it."""
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    release = threading.Event()
    try:
        env = mod.evaluate(_payload(root, args), emit=lambda params, r: release.wait(30) and {})
    finally:
        release.set()
        for worker in threading.enumerate()[1:]:
            worker.join(10)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    (script,) = (root / "scratch" / "warp").glob("defer-*.workflow.mjs")
    text = script.read_text(encoding="utf-8")
    params_file = json.loads(re.search(r"^const PARAMS_FILE = (.*);$", text, re.M).group(1))
    return json.loads(Path(params_file).read_text(encoding="utf-8"))


def _emit_script(root: Path, params: dict) -> str:
    """The run script `dispatch.emit` composes for `params`, as the relay's command would."""
    params = {**params, "target_root": root.as_posix(), "session_id": SID}
    result = _dispatch_emit(params, repo_root=root)
    assert result.get("ok", True) is not False, result
    return Path(result["path"] if Path(result["path"]).is_absolute() else root / result["path"]).read_text(encoding="utf-8")


def _assert_scout_carries(script: str) -> None:
    assert SENTINEL in script, "the sentinel is absent from the composed script"
    scout = re.search(r"label: 'size'", script)
    assert scout, "no sizing-scout leg"
    prompt = script[script.rfind("agent(", 0, scout.start()) : scout.start()]
    assert EM_BRIEF_VAR in prompt, "the sizing-scout prompt does not open with the EM brief"


def _assert_planner_carries(script: str) -> None:
    """The embedded plan-blitz legs run under the brief-prefixing `agent` shadow."""
    assert SENTINEL in script, "the sentinel is absent from the composed script"
    assert f"const agent = (p, o) => _askAgent({EM_BRIEF_VAR} + p, o);" in script, "the plan-author legs carry no EM brief"


@pytest.fixture
def m_plus(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_M_PLUS)


def test_digest_is_the_hash_of_the_block_text_with_em_context_writes():
    block = contract.with_em_context(None, f"  {SENTINEL}\n")
    body = block.split(contract.EM_CONTEXT_MARKER + "\n", 1)[1]
    assert contract.em_context_digest(body) == contract.em_context_digest(SENTINEL)
    assert contract.em_context_digest(SENTINEL) != contract.em_context_digest(SENTINEL + "x")
    assert contract.EM_CONTEXT_DIGEST_PARAM == "em_context_digest"


def test_inline_emit_params_carry_the_sentinel(repo):
    params = _inline_params(repo, {"ask": "build it", "source": "relayed", "em_context": SENTINEL})
    assert len(SENTINEL) > mod.DEFERRED_BRIEF_INLINE_MAX
    assert SENTINEL in params["ask_brief"]
    _assert_scout_carries(_emit_script(repo, params))


def test_deferred_spilled_brief_reaches_the_composed_script(repo, monkeypatch):
    params = _deferred_params(repo, {"ask": "build it", "source": "relayed", "em_context": SENTINEL}, monkeypatch)
    assert "ask_brief_file" in params and "ask_brief" not in params
    _assert_scout_carries(_emit_script(repo, params))


def test_one_element_asks_deferred_reaches_the_composed_script(repo, monkeypatch):
    args = {"asks": [{"ask": "build it", "source": "relayed", "em_context": SENTINEL}]}
    params = _deferred_params(repo, args, monkeypatch)
    _assert_scout_carries(_emit_script(repo, params))


def _sized_doc(brief: str | None) -> dict:
    doc = contract.intake_record(name="arrival", turn={"text": VERBATIM, "turn": 1}, today="2026-11-06")
    if brief:
        doc["em_analysis"][contract.EM_BRIEF_KEY] = brief
    return doc


def _minted_doc(brief: str | None) -> dict:
    """A sized M sizing as the in-run size stage mints it: a known plan arm, not an intake record."""
    doc = {
        "schema": "sizing-object", "intent": "build it", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": VERBATIM}],
    }
    if brief:
        doc["em_analysis"] = {contract.EM_BRIEF_KEY: brief}
    return doc


def test_multi_item_asks_slice_brief_reaches_the_composed_script(repo):
    out = fanout.fanout(
        {
            "asks": [
                {"ask": "fix a", "writes": ["a.py"], "em_context": SENTINEL},
                {"ask": "fix b", "writes": ["b.py"]},
            ],
            "session_id": SID,
        },
        repo,
    )
    assert out["exit_code"] == 0, out
    rel = out["ready"][0]["sizing"]
    assert SENTINEL in yaml.safe_load((repo / rel).read_text(encoding="utf-8"))["em_analysis"]["em_brief"]
    _assert_scout_carries(_emit_script(repo, {"ask": True, "sizing_path": rel}))


def test_sizing_first_submission_reaches_the_composed_script(repo):
    brief = contract.with_em_context(None, SENTINEL)
    (repo / SIZING_REL).write_text(yaml.safe_dump(_sized_doc(brief)), encoding="utf-8", newline="\n")
    params = _inline_params(repo, {"sizing": SIZING_REL})
    assert "ask_brief" not in params
    _assert_scout_carries(_emit_script(repo, params))


@pytest.mark.xfail(
    strict=True,
    reason="drop site: op.py reads `em_analysis.em_brief` only `if intake_doc is not None`, so a minted (non-intake) sizing's brief is ignored",
)
def test_minted_sizing_em_brief_reaches_the_plan_author(repo, m_plus):
    brief = contract.with_em_context(None, SENTINEL)
    (repo / SIZING_REL).write_text(yaml.safe_dump(_minted_doc(brief)), encoding="utf-8", newline="\n")
    _assert_planner_carries(_emit_script(repo, {"ask": True, "sizing_path": SIZING_REL}))


def test_deferred_chain_hop_carries_the_brief_and_digest(repo, monkeypatch):
    """The M+ hand-off names the sizing and no brief; the deferred script's hop adds the params' brief keys to the chained emit."""
    params = _deferred_params(repo, {"ask": "build it", "source": "relayed", "em_context": SENTINEL}, monkeypatch)
    (script,) = (repo / "scratch" / "warp").glob("defer-*.workflow.mjs")
    carry = json.loads(re.search(r"^const _briefCarry = (.*);$", script.read_text(encoding="utf-8"), re.M).group(1))
    assert carry == {
        "ask_brief_file": params["ask_brief_file"],
        contract.EM_CONTEXT_DIGEST_PARAM: contract.em_context_digest(SENTINEL),
    }


@pytest.mark.parametrize("args", [
    {"ask": "build it", "source": "relayed", "em_context": SENTINEL, "context": ["prose note for the planner"]},
    {"asks": [{"ask": "build it", "source": "relayed", "em_context": SENTINEL, "context": ["prose note for the planner"]}]},
])
def test_digest_names_the_text_after_the_marker(repo, args):
    params = _inline_params(repo, args)
    after = params["ask_brief"].split(contract.EM_CONTEXT_MARKER + "\n", 1)[1]
    assert contract.em_context_digest(after) == params[contract.EM_CONTEXT_DIGEST_PARAM] == contract.em_context_digest(SENTINEL)


def test_no_em_context_stamps_no_digest(repo):
    assert contract.EM_CONTEXT_DIGEST_PARAM not in _inline_params(repo, {"ask": "build it", "source": "relayed"})
