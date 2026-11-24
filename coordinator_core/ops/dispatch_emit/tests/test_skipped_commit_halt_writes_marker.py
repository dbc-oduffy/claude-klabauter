"""The no-run-token skipped-commit halt and the co-tenant halt, composed as the script emits them, record `halted.json` at halt time and hand-land consistently."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, halted_run, terminal_commit
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(shutil.which("node") is None, reason="node absent"),
]

RUN = "ask-20261124T094746-40ed59"
_SIZING = "state/sizings/x.yaml"
_SCRIPT = "run.mjs"

_SKIPPED = """
let _halted = null, _runId = %s, _sizingRel = %s, _RUN_TOKEN = null, _alreadyDelivered = null;
let _nextAction = { kind: 'terminal_commit', op: null, params: {} };
if (!_halted && %s
console.log(JSON.stringify(_halted));
"""

_CO_TENANT = """
const _TEST_GUARD = true;
let _halted = null, _runId = %s, _testResult = %s;
let _reviewReverify = null;
const _testRan = () => %s;
%s
console.log(JSON.stringify(_halted));
"""

# The halt-record tail the composed script runs after the commit stage, over the halt it just set.
_TAIL = """
const calls = [];
const engineOp = async (op, params) => { calls.push([op, params]); return { committed: true, sha: 'stub' }; };
let _halted = %s, _runId = %s, _RUN_TOKEN = %s, _commit = null, _haltCommit = null;
let _consumedReturns = {}, _docs = null;
%s
(async () => {
  if (false) {}
%s
  console.log(JSON.stringify({ calls, haltCommit: _haltCommit }));
})();
"""


def _node(program: str) -> dict:
    out = subprocess.run(
        [shutil.which("node"), "-"], input=program, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    return json.loads(out)


def _skipped_halt() -> dict:
    return _node(_SKIPPED % (json.dumps(RUN), json.dumps(_SIZING), ask_compose._NO_RUN_TOKEN_COMMIT_SKIPPED_JS))


def _co_tenant_halt() -> dict:
    verdict = (
        "{ status: 'error', sidecar_path: 'a.md', tests_run: 0, "
        f"zero_run_cause: '{ask_compose.CO_TENANT_HALTED_CAUSE}', co_tenant: {{ owner: 'b', module: 'm' }} }}"
    )
    body = ask_compose._test_verdict_empty_halt_js(_SIZING).replace("TEST_GUARD", "_TEST_GUARD")
    return _node(_CO_TENANT % (json.dumps(RUN), verdict, ask_compose._TEST_RAN_JS, body))


def _composed_halt_record_tail() -> str:
    """The `else if` arm `_append_halt_record_commit` composes, taken from the composer, not retyped."""
    lines: list[str] = []
    ask_compose._append_halt_record_commit(lines)
    start = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("else if (_halted && !_RUN_TOKEN"))
    return "\n".join(lines[start:])


def _run_tail(halted: dict, run_token: str) -> dict:
    return _node(
        _TAIL % (
            json.dumps(halted), json.dumps(RUN), run_token,
            ask_compose._halt_record_no_token_fn(_SCRIPT), _composed_halt_record_tail(),
        )
    )


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    (tmp_path / _SCRIPT).write_text("// script\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "one", _SCRIPT)
    _git(tmp_path, "commit", "-q", "-m", "seed")
    rd = record_dir(tmp_path, RUN)
    rd.mkdir(parents=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": "staged", "staged_at": "2026-11-24T09:49:09Z"}), encoding="utf-8")
    (rd / "run.json").write_text(json.dumps({"sizing_rel": _SIZING}) + "\n", encoding="utf-8")
    return tmp_path, rd


def test_skipped_commit_halt_reply_names_the_halt_not_null() -> None:
    halted = _skipped_halt()
    assert halted["halted"] == "commit" and halted["reason"] == ask_compose.NO_RUN_TOKEN_COMMIT_REASON


def test_co_tenant_halt_reply_names_the_halt_with_no_halt_commit() -> None:
    assert _co_tenant_halt()["halted"] == ask_compose.CO_TENANT_HALTED_CAUSE


@pytest.mark.parametrize("halt", [_skipped_halt, _co_tenant_halt], ids=["skipped-commit", "co-tenant"])
def test_a_halt_with_no_run_token_records_its_marker_at_halt_time_and_hand_land_records_it(repo, halt) -> None:
    root, rd = repo
    halted_by = halt()
    ran = _run_tail(halted_by, "null")
    assert [c[0] for c in ran["calls"]] == ["dispatch.terminal_commit"], ran
    params = ran["calls"][0][1]
    assert params["run_id"] == RUN and params["halted_by"]["halted"] == halted_by["halted"]

    # Execute the op the script just called: the marker must exist with no watcher and no run token.
    replied = terminal_commit._handler(params, repo_root=root / ".git")
    assert replied.get("error") is None and replied["halted"] == halted_by["halted"], replied
    marker = rd / halted_run.HALTED_FILE
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["halted"] == halted_by["halted"]

    out = op._handler({"run_id": RUN, "sha": _git(root, "rev-parse", "HEAD")}, root)
    assert out["ok"] is True and out["written"] is True and out["recorded"].endswith(halted_run.HALTED_FILE), out
    assert "never ran" not in json.dumps(out) and "never halted" not in json.dumps(out), out
    landed = json.loads(marker.read_text(encoding="utf-8"))
    assert landed["status"] == halted_run.STATUS_LANDED and landed["halted"] == halted_by["halted"]


@pytest.mark.parametrize("halt", [_skipped_halt, _co_tenant_halt], ids=["skipped-commit", "co-tenant"])
def test_a_halt_that_holds_a_run_token_leaves_the_record_to_the_token_gated_leg(halt) -> None:
    ran = _run_tail(halt(), json.dumps("a" * 32))
    assert ran["calls"] == [] and ran["haltCommit"] is None


def test_the_composed_commit_stage_carries_the_no_token_record_arm() -> None:
    script = ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id=RUN, session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", run_base_sha="a" * 40, in_run_commit=True, script_path=_SCRIPT, **_review_kw(),
    )
    assert "async function _recordHaltNoToken()" in script
    assert script.count("_haltCommit = await _recordHaltNoToken();") >= 1


def _review_kw() -> dict:
    from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

    return dict(REVIEW_KW)


def test_a_run_whose_script_wrote_no_marker_still_hand_lands_as_its_real_halt(repo) -> None:
    root, rd = repo
    (rd / "overlay-verdict.json").write_text(
        json.dumps({"zero_run_cause": ask_compose.CO_TENANT_HALTED_CAUSE}), encoding="utf-8"
    )
    out = op._handler({"run_id": RUN, "sha": _git(root, "rev-parse", "HEAD")}, root)
    assert out["ok"] is True and out["written"] is True and out["halted"] == ask_compose.CO_TENANT_HALTED_CAUSE, out
