"""The emitted family leg: one family attempt, one claude fallback on a read-only role (never on a write row), both results kept.

Most assertions read the emitted JS text; the fallback-reply tests execute the emitted `_familyRun` in node."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, family_invoke_record, stage_family, stage_family_binding
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_RUN = "run-1"
_OPTED = {"stage_family_binding": stage_family_binding.BINDING_REF}


def _script(monkeypatch, sizing: dict | None, sizing_rel: str | None = "sizing.yaml") -> str:
    monkeypatch.setattr(ask_compose, "load_sizing", lambda root, rel: sizing)
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt=None if sizing_rel else "add a thing", sizing_rel=sizing_rel, run_id=_RUN, session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", **REVIEW_KW,
    )


def _family_fn(script: str) -> str:
    start = script.index("  async function _familyRun(r) {")
    return script[start : script.index("\n  }\n", start)]


def test_opted_in_script_carries_the_family_arm_in_the_row_loop_and_the_late_loop(monkeypatch):
    script = _script(monkeypatch, _OPTED)
    assert script.count("r.family ? _familyRun(r) : ") == 2
    late = script[script.index("for (const r of _lateRows) {"):]
    assert "async () => r.family ? _familyRun(r) : r.group ? _groupRun(r)" in late[:400]
    row_loop = script[script.index("for (const r of _manifest.rows.filter((x) => x.wave === w))"):]
    assert "r.kept_from ? _keptRun(r) : r.family ? _familyRun(r) : r.group ? _groupRun(r)" in row_loop


def test_non_opted_in_script_carries_no_family_bytes(monkeypatch):
    for sizing, rel in (({}, "sizing.yaml"), ({"stage_family_binding": ""}, "sizing.yaml")):
        script = _script(monkeypatch, sizing, rel)
        assert "_familyRun" not in script and "_FAMILY_RESULT_SCHEMA" not in script


def test_non_opted_in_script_equals_the_script_with_the_leg_disabled(monkeypatch):
    plain = _script(monkeypatch, {})
    monkeypatch.setattr(ask_compose, "_family_enabled", lambda *a: False)
    assert _script(monkeypatch, _OPTED) == plain


def test_foreign_binding_ref_keeps_the_leg_and_is_refused_at_stage_time(monkeypatch):
    assert "async function _familyRun(r)" in _script(monkeypatch, {"stage_family_binding": "elsewhere"})


def test_family_leg_reruns_on_claude_only_for_blocked_failed_or_unresolved(monkeypatch):
    fn = _family_fn(_script(monkeypatch, _OPTED))
    for verdict in stage_family.FALLBACK_VERDICTS:
        assert f"_fam.verdict === '{verdict}'" in fn
    assert "'OK'" in fn and "OK" not in stage_family.FALLBACK_VERDICTS
    # the family attempt runs once, on the runner; the fallback once, on the claude brief
    assert fn.count("await agent(") == 2
    assert "agentType: r.agent_type" in fn and "agentType: r.fallback_agent_type" in fn
    assert "brief_path: r.fallback_brief_path" in fn
    assert "schema: _FAMILY_RESULT_SCHEMA" in fn


def test_unparseable_reply_synthesizes_the_failed_family_attempt_before_the_fallback(monkeypatch):
    fn = _family_fn(_script(monkeypatch, _OPTED))
    synth = fn.index("if (!_fam) _fam = { family: r.family, stage: r.stage_role, exit_code: -1, output_path: '', verdict: 'FAILED', blocked_reason: 'family-stage-runner reply missing or unparseable' }")
    assert synth < fn.index("r.fallback_agent_type")


def test_record_keys_match_stage_family_stage_record(monkeypatch):
    fn = _family_fn(_script(monkeypatch, _OPTED))
    fam = {"family": "agy", "stage": "research", "exit_code": 1, "output_path": "scratch/x", "verdict": "FAILED", "blocked_reason": "out of credits"}
    fb = {"family": "claude", "stage": "research", "exit_code": 0, "output_path": "", "verdict": "OK"}
    rec = stage_family.stage_record("research", fam, fb)
    for key in rec:
        assert re.search(rf"\b{key}:", fn), key
    assert "fallback_for: _fam.family" in fn
    assert rec["attempts"][1]["fallback_for"] == "agy" and rec["reported_family"] == "claude"
    # a result that kept the family never claims claude; one that fell back never claims the family
    assert "reported_family: _fam.family" in fn
    assert "reported_family: 'claude'" in fn or 'reported_family: "claude"' in fn
    schema_keys = re.search(r"_FAMILY_RESULT_SCHEMA = (\{.*\});", _script(monkeypatch, _OPTED)).group(1)
    for key in stage_family._RESULT_KEYS:
        assert f'"{key}"' in schema_keys


def test_fallback_failure_is_blocked_and_still_keeps_both_attempts(monkeypatch):
    fn = _family_fn(_script(monkeypatch, _OPTED))
    assert "(_fbOk ? 'DONE: ' : 'BLOCKED: ')" in fn
    assert "attempts: [_fam, { ..._fb, fallback_for: _fam.family }]" in fn


@pytest.mark.parametrize("bad", ["", None])
def test_bare_prompt_run_carries_the_leg_because_its_sizing_is_minted_at_run_time(monkeypatch, bad):
    script = _script(monkeypatch, _OPTED, sizing_rel=bad)
    assert "async function _familyRun(r)" in script
    assert script.count("r.family ? _familyRun(r) : ") == 2


def test_sizing_family_without_binding_carries_the_leg(monkeypatch):
    # ask_stage routes rows to the family runner on a bare `family:`; the compose gate must agree.
    monkeypatch.setattr(ask_compose, "load_sizing", lambda root, rel: {"family": "codex"})
    assert ask_compose._family_enabled("REPO", "sizing.yaml") is True


def test_sizing_with_neither_opt_in_omits_the_leg(monkeypatch):
    monkeypatch.setattr(ask_compose, "load_sizing", lambda root, rel: {})
    assert ask_compose._family_enabled("REPO", "sizing.yaml") is False


def test_row_without_a_fallback_brief_keeps_the_family_attempt_and_never_reruns_on_an_empty_brief(monkeypatch):
    fn = _family_fn(_script(monkeypatch, {"family": "codex"}))
    guard = fn.index("if (r.stage_role === 'write' || !r.fallback_brief_path) {")
    assert guard < fn.index("brief_path: r.fallback_brief_path")
    assert "return 'BLOCKED: ' + r.id + ' via ' + _fam.family" in fn[guard : fn.index("brief_path: r.fallback_brief_path")]
    assert fn.count("await agent(") == 2


def test_row_without_a_prompt_brief_never_spawns_the_cli_or_dispatches_an_agent(monkeypatch):
    fn = _family_fn(_script(monkeypatch, {"family": "codex"}))
    first = fn.index("if (!r.fallback_brief_path) return 'BLOCKED: '")
    assert first < fn.index("await agent(") and first < fn.index("_famStart(r)")
    assert "family_authored:false" in fn[first : fn.index("let _fam = null;")]


_NODE = shutil.which("node")
_FAILED_FAMILY = {"family": "codex", "stage": "impl", "exit_code": 1, "output_path": "", "verdict": "FAILED", "blocked_reason": "no stage work"}
_ROW = {"id": "C1", "family": "codex", "stage_role": "impl", "fallback_brief_path": "b.md", "agent_type": "a", "fallback_agent_type": "b", "model": "m"}


def _run_family(monkeypatch, tmp_path, fallback_reply: dict, *, row: dict = _ROW, family_reply: dict = _FAILED_FAMILY) -> tuple[str, dict]:
    """Execute the emitted `_familyRun` in node: the family attempt answers `family_reply`, the fallback `fallback_reply`.
    The returned record also carries `calls`, the number of `agent()` dispatches the leg made."""
    script = _script(monkeypatch, _OPTED)
    body = script[script.index("  const _FAMILY_RESULT_SCHEMA = ") : script.index("\n  }\n", script.index("  async function _familyRun(r) {")) + 4]
    prog = (
        "const _rowPrompt = () => 'P'; const log = () => {}; const _famOk = {};\n"
        f"const _replies = [{json.dumps({'v': family_reply})}, {json.dumps(fallback_reply)}]; let _n = 0;\n"
        "async function agent() { const x = _replies[_n++]; if (x.throw) throw new Error(x.throw); return x.v; }\n"
        f"{body}\n_familyRun({json.dumps(row)}).then((r) => console.log(r + ' ' + JSON.stringify({{ calls: _n }})));\n"
    )
    path = tmp_path / "family.js"
    path.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        [_NODE, str(path)], capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    head, _, rest = out.partition(" {")
    rec, _, calls = ("{" + rest).rpartition(" {")
    return head, {**json.loads(rec), **json.loads("{" + calls)}


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_done_fallback_reply_is_consumed_as_an_ok_attempt(monkeypatch, tmp_path):
    head, rec = _run_family(monkeypatch, tmp_path, {"v": "DONE: C1\nStatus: DONE"})
    fb = rec["attempts"][1]
    assert head.startswith("DONE: C1 via claude fallback after codex FAILED")
    assert (fb["verdict"], fb["exit_code"], fb["fallback_for"]) == ("OK", 0, "codex") and "blocked_reason" not in fb


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize(
    "reply, cause",
    [
        ({"throw": "boom"}, "dispatch threw: boom"),
        ({"v": None}, "reply missing (dispatch returned nothing)"),
        ({"v": {"x": 1}}, "reply was not text (object)"),
        ({"v": "  \n"}, "reply was empty"),
        ({"v": "PARTIAL: C1\nStatus: PARTIAL\ngate-blocker: codex worker performed no stage work\n"}, "replied PARTIAL, gate-blocker: codex worker performed no stage work"),
        ({"v": "**BLOCKED:** C1 cannot read the brief\nmore"}, "replied **BLOCKED:** C1 cannot read the brief"),
    ],
)
def test_non_done_fallback_reply_records_its_actual_cause(monkeypatch, tmp_path, reply, cause):
    head, rec = _run_family(monkeypatch, tmp_path, reply)
    fb = rec["attempts"][1]
    assert head.startswith("BLOCKED: C1 via claude fallback after codex FAILED")
    assert fb["verdict"] == "FAILED" and fb["blocked_reason"] == f"claude fallback {cause}"


def test_unminted_sizing_path_carries_the_leg(monkeypatch):
    def _missing(root, rel):
        raise FileNotFoundError(rel)

    monkeypatch.setattr(ask_compose, "load_sizing", _missing)
    assert ask_compose._family_enabled("REPO", "sizing.yaml") is True


def test_family_attempt_runs_the_row_start_snapshot_first(monkeypatch):
    script = _script(monkeypatch, _OPTED)
    fn = _family_fn(script)
    assert "await agent(_famStart(r) + _rowPrompt(r)," in fn
    start = script[script.index("  const _famStart = (r) => "):]
    assert f"-m {ask_compose.FAMILY_CREDIT_MODULE} start --root REPO --run-id {_RUN} --row ' + r.id + " in start[:800]


def test_checkpoint_message_names_the_family_ok_rows_from_the_commit_scope(monkeypatch):
    """One `_famOk`, declared beside `_landed`, the map the row loop already writes from the same scope
    the family leg runs in: a second or nested declaration shadows it and drops the line silently."""
    script = _script(monkeypatch, _OPTED)
    assert script.count("const _famOk = {};") == 1
    assert "  const _landed = {};\n  const _famOk = {};" in script
    assert "if (typeof _famOk === 'object') _famOk[r.id] = _fam.verdict === 'OK';" in _family_fn(script)
    assert "'\\nCheckpoint-Family-Ok: ' + done.filter((i) => _famOk[i]).join(', ')" in script


_AUTHORED_OK = {"family": "codex", "stage": "write", "exit_code": 0, "output_path": "", "verdict": "OK", "family_authored": True, "vendor_model": "gpt-x"}
_WRITE_ROW = {**_ROW, "stage_role": "write", "writes": ["pkg/a.py"]}


def test_result_schema_requires_family_authored_boolean(monkeypatch):
    schema = json.loads(re.search(r"_FAMILY_RESULT_SCHEMA = (\{.*\});", _script(monkeypatch, _OPTED)).group(1))
    assert schema["required"] == [*stage_family._RESULT_KEYS, stage_family.FAMILY_AUTHORED_KEY]
    assert schema["properties"][stage_family.FAMILY_AUTHORED_KEY] == {"type": "boolean"}


def test_runner_brief_names_the_engine_invoker_as_the_only_vendor_route(monkeypatch):
    script = _script(monkeypatch, _OPTED)
    start = script[script.index("  const _famStart = (r) => ") : script.index("  async function _familyRun(r) {")]
    assert f"-m {family_invoke_record.INVOKE_MODULE} --root REPO --run-id {_RUN} --row ' + r.id + ' --family ' + r.family" in start
    assert "' --stage ' + r.stage_role" in start and "' --prompt ' + r.fallback_brief_path" in start and "r.writes.map((w) => JSON.stringify(String(w))).join(' ')" in start
    assert "only vendor call" in start and "Never edit or write a file yourself" in start
    assert start.index(ask_compose.FAMILY_CREDIT_MODULE) < start.index(family_invoke_record.INVOKE_MODULE)


def test_engine_invoker_module_exists_and_exposes_the_row_cli():
    import importlib.util
    import subprocess
    import sys

    assert importlib.util.find_spec(family_invoke_record.INVOKE_MODULE) is not None
    out = subprocess.run(
        [sys.executable, "-m", family_invoke_record.INVOKE_MODULE, "--help"],
        capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0
    for flag in ("--root", "--run-id", "--row", "--family", "--stage", "--prompt", "--writes"):
        assert flag in out.stdout


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("authored", [{},{"family_authored": False}, {"family_authored": "true"}])
def test_write_row_without_family_authored_true_halts_blocked_and_never_calls_the_fallback(monkeypatch, tmp_path, authored):
    reply = {k: v for k, v in _AUTHORED_OK.items() if k != "family_authored"} | authored
    head, rec = _run_family(monkeypatch, tmp_path, {"v": "DONE: C1\nStatus: DONE"}, row=_WRITE_ROW, family_reply=reply)
    assert head.startswith("BLOCKED: C1 via codex BLOCKED (family_authored:false: ")
    assert "no claude fallback" in head
    assert rec["calls"] == 1 and rec["reported_family"] == "codex" and len(rec["attempts"]) == 1
    assert rec["attempts"][0]["family_authored"] is False
    assert rec["attempts"][0]["blocked_reason"].startswith("family_authored:false: ")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_write_row_family_failure_never_calls_the_fallback_whatever_the_verdict(monkeypatch, tmp_path):
    head, rec = _run_family(monkeypatch, tmp_path, {"v": "DONE: C1\nStatus: DONE"}, row=_WRITE_ROW)
    assert head.startswith("BLOCKED: C1 via codex FAILED (family_authored:false: no stage work)")
    assert rec["calls"] == 1 and rec["reported_family"] == "codex"


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_write_row_authored_ok_is_done_with_its_model(monkeypatch, tmp_path):
    head, rec = _run_family(monkeypatch, tmp_path, {"v": "unused"}, row=_WRITE_ROW, family_reply=_AUTHORED_OK)
    assert head.startswith("DONE: C1 via codex")
    assert rec["calls"] == 1 and rec["attempts"][0]["family_authored"] is True and rec["attempts"][0]["vendor_model"] == "gpt-x"


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_read_only_role_still_falls_back_on_an_unauthored_ok_result(monkeypatch, tmp_path):
    reply = {**_AUTHORED_OK, "stage": "research", "family_authored": False}
    head, rec = _run_family(monkeypatch, tmp_path, {"v": "DONE: C1\nStatus: DONE"}, row={**_ROW, "stage_role": "research"}, family_reply=reply)
    assert head.startswith("DONE: C1 via claude fallback after codex BLOCKED")
    assert rec["calls"] == 2 and rec["attempts"][0]["blocked_reason"].startswith("family_authored:false")
