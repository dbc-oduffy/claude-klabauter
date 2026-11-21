"""A schema-invalid halt-commit or commit-stage reply whose raw text records committed=true and a sha
ends with that sha recorded, not rc 1; otherwise the halt-commit site re-asks once."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _usage_limit_helper_js
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_halt_commits_record import _compose
from coordinator_core.win_portability import no_console_creationflags

_RAW = '{"committed": true, "sha": "bd2a376770", "already_landed": false, "error": null'
_HALT_SITE = (
    "try { _haltCommit = await _commitHaltRecord(); } catch (e) { "
    "try { _haltCommit = await _recoverStructuredReply(e, _commitHaltRecord); } catch (e2) { _haltOnUsageLimit(e2); } }"
)
_COMMIT_SITE = "catch (e) { try { _commit = await _recoverStructuredReply(e, null); } catch (e2) { _haltOnUsageLimit(e2); } }"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _run(body: str) -> dict:
    prog = (
        "let _halted = null; const _runId = 'r'; const _refireNextAction = () => {};\n"
        + _usage_limit_helper_js()
        + "\n(async () => {\n"
        + body
        + "\n})().then((v) => console.log(JSON.stringify({ ok: v })), (e) => console.log(JSON.stringify({ threw: String(e.message) })));\n"
    )
    out = subprocess.run(
        [_node(), "-e", prog], capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _parse_error(raw: str) -> str:
    return (
        "const mk = (raw) => { const e = new Error('StructuredReplyInvalid: Expecting value: column 5091 (char 5090)'); "
        f"e.raw = raw; return e; }}; const raw = {json.dumps(raw)};"
    )


def test_both_sites_route_through_the_recovery_helper(monkeypatch):
    script = _compose(monkeypatch, in_run_commit=True)
    assert _HALT_SITE in script
    assert script.count(_COMMIT_SITE) >= 1
    assert "function _recoverStructuredReply(" in script and "function _recordedCommit(" in script
    assert script.count("label: 'halt-commit'") == 1
    assert script.count(ask_compose.RECOVERED_COMMIT_HELPER_JS) >= 1


def test_a_parse_failure_recording_a_commit_returns_its_sha_marked_recovered_without_a_re_ask():
    res = _run(
        _parse_error(_RAW)
        + "let asks = 0; const r = await _recoverStructuredReply(mk(raw), async () => { asks++; return null; });"
        + "return { r, asks };"
    )
    assert res["ok"]["r"]["sha"] == "bd2a376770" and res["ok"]["r"]["recovered"] is True
    assert res["ok"]["r"]["committed"] is True and res["ok"]["asks"] == 0


def test_a_parse_failure_without_a_recorded_commit_re_asks_exactly_once():
    res = _run(
        _parse_error("")
        + "let asks = 0; const r = await _recoverStructuredReply(mk('garbled'), async () => { asks++; return { committed: true, sha: 'abc1234' }; });"
        + "return { r, asks };"
    )
    assert res["ok"]["asks"] == 1 and res["ok"]["r"]["sha"] == "abc1234"


def test_a_second_parse_failure_rethrows_and_a_committed_false_reply_is_not_recovered():
    res = _run(
        _parse_error("")
        + "let asks = 0; const bad = mk('{\"committed\": false, \"sha\": \"deadbeef\"');"
        + "await _recoverStructuredReply(bad, async () => { asks++; throw bad; });"
    )
    assert "threw" in res and "StructuredReplyInvalid" in res["threw"]


def test_a_second_failure_still_recovers_a_sha_its_raw_reply_records():
    res = _run(
        _parse_error(_RAW)
        + "const first = mk('garbled'); return await _recoverStructuredReply(first, async () => { throw mk(raw); });"
    )
    assert res["ok"]["sha"] == "bd2a376770" and res["ok"]["recovered"] is True


def test_a_non_parse_error_and_a_usage_limit_are_never_re_asked():
    res = _run(
        "let asks = 0; const redo = async () => { asks++; return null; };"
        "const out = [];"
        "for (const m of ['spawn ENOENT', \"You've hit your usage limit, resets 3am\"]) {"
        "  try { await _recoverStructuredReply(new Error(m), redo); } catch (e) { out.push(e.message); } }"
        "return { out, asks };"
    )
    assert res["ok"]["asks"] == 0 and len(res["ok"]["out"]) == 2


def test_the_commit_stage_site_recovers_a_recorded_sha_but_never_re_asks():
    res = _run(
        _parse_error(_RAW)
        + "const a = await _recoverStructuredReply(mk(raw), null); let b = null;"
        "try { await _recoverStructuredReply(mk('garbled'), null); } catch (e) { b = 'threw'; }"
        "return { sha: a.sha, b };"
    )
    assert res["ok"] == {"sha": "bd2a376770", "b": "threw"}
