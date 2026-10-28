"""Repro of the PreToolUse(Workflow) no-rewrite: a Workflow engine-ask call the DoE dispatcher neither rewrites nor denies.

Each case drives `preuse-workflow-dispatch.py` as the harness does (the registered `-c` loader argv, JSON on stdin) and
asserts the contract: the call is rewritten or denied by name, never passed through silently. A case that reproduces
the pass-through at base is `xfail(strict=True)` naming the suspect, so the fix that lands flips it to a failure that
demands the marker's removal. Cases the harness alone can settle are named xfails carrying the evidence that settles them.

The dispatcher and its hook-run launcher are located through `CLAUDE_PLUGIN_ROOT` (else the sibling coordinator-content-repo checkout);
door-bound cases skip where either is absent. The engine is a stamped copy run cold, as the halted-refire sibling does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ENGINE_SOURCE = Path(__file__).resolve().parents[3]
_PLUGIN_ROOT = Path(
    os.environ.get("CLAUDE_PLUGIN_ROOT") or _ENGINE_SOURCE.parent / "coordinator-content-repo" / "coordinator"
)
_HOOKS_JSON = _PLUGIN_ROOT / "hooks" / "hooks.json"
_DISPATCHER = _PLUGIN_ROOT / "hooks" / "scripts" / "preuse-workflow-dispatch.py"
_BOOT = _PLUGIN_ROOT / "hooks" / "scripts" / "_hook_boot.py"
_STUB = "coordinator:engine-ask"


def _need(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"{path} is absent (set CLAUDE_PLUGIN_ROOT to the coordinator-content-repo coordinator dir)")


def _registered_argv(plugin_root: Path) -> list[str]:
    """The exact argv the PreToolUse(Workflow) registration launches, with ${CLAUDE_PLUGIN_ROOT} expanded."""
    doc = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    for entry in doc["hooks"]["PreToolUse"]:
        if entry.get("matcher") != "Workflow":
            continue
        hook = entry["hooks"][0]
        return [hook["command"], *(a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root)) for a in hook["args"])]
    raise AssertionError("no PreToolUse matcher 'Workflow' registration in hooks.json")


def _fire(
    stdin: bytes, *, plugin_root: Path = _PLUGIN_ROOT, env: dict | None = None, cwd: Path | None = None,
    argv: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess, float]:
    """Run the registered hook command once; return the result and its wall seconds."""
    cmd = argv or _registered_argv(plugin_root)
    cmd = [sys.executable if cmd[0] == "python3" else cmd[0], *cmd[1:]]
    started = time.perf_counter()
    proc = subprocess.run(
        cmd, input=stdin, capture_output=True, timeout=60, cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})}, **no_console_creationflags(),
    )
    return proc, time.perf_counter() - started


def _outcome(proc: subprocess.CompletedProcess) -> str:
    """rewritten | denied:<reason> | passed-through (no decision, no rewrite)."""
    text = proc.stdout.decode("utf-8", "replace").strip()
    if not text:
        return "passed-through"
    hso = json.loads(text.splitlines()[-1]).get("hookSpecificOutput", {})
    if hso.get("permissionDecision") == "deny":
        return "denied:" + str(hso.get("permissionDecisionReason", ""))
    if isinstance(hso.get("updatedInput"), dict) and hso["updatedInput"].get("scriptPath"):
        return "rewritten"
    return "passed-through"


def _assert_taken_or_named(proc: subprocess.CompletedProcess) -> None:
    outcome = _outcome(proc)
    assert outcome != "passed-through", (
        "Workflow engine-ask call passed through unrewritten with no deny",
        proc.returncode, proc.stdout[:300], proc.stderr[-400:],
    )


def _payload(repo: Path, **over) -> dict:
    base = {
        "hook_event_name": "PreToolUse", "tool_name": "Workflow", "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "transcript_path": str(repo / "missing-transcript.jsonl"), "cwd": str(repo), "tool_use_id": "toolu_repro",
        "tool_input": {"name": _STUB, "args": {"sizing": "state/sizings/none.yaml"}},
    }
    base.update(over)
    return base


# (a) post-compact payload shape against the real door.
# test_submit_engine_ask_halted_refire supplies the stamped-engine + door fixtures and the halted-sizing builder.
from test_submit_engine_ask_halted_refire import _halted_repo, door, engine_root  # noqa: E402,F401


def test_a_post_compact_payload_is_rewritten(door, tmp_path, record_property):
    """A new session id and a transcript path that no longer exists, as after SessionStart:compact, with no
    session-start record for that id, no once-file or marker under the session hub, and a nonsense settings-home hub."""
    _, env = door
    halted = _halted_repo(tmp_path, "alpha")
    hub = Path(env["COORDINATOR_SETTINGS_HOME"]) / "session-hub"
    hub.mkdir(parents=True, exist_ok=True)
    (hub / "post-compact-session.flag").write_text("1", encoding="utf-8")
    payload = _payload(
        halted.repo, session_id="99999999-0000-4000-8000-000000000000",
        transcript_path=str(tmp_path / "rotated" / "gone.jsonl"),
        tool_input={"name": _STUB, "args": {"sizing": halted.sizing_rel}},
    )
    proc, wall = _fire(
        json.dumps(payload).encode("utf-8"), env={**env, "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT)}, cwd=halted.repo,
    )
    record_property("a_wall_s", round(wall, 3))
    assert _outcome(proc) == "rewritten", (proc.stdout[:300], proc.stderr[-400:])


def test_a_post_compact_non_ascii_payload_is_rewritten(door, tmp_path):
    """A model-authored brief carrying non-ASCII bytes reaches the dispatcher's stdin read intact."""
    _, env = door
    halted = _halted_repo(tmp_path, "bravo")
    tool_input = {
        "name": _STUB, "args": {"sizing": halted.sizing_rel}, "brief": "a ← b — c “d” Ł",
    }
    proc, _ = _fire(
        json.dumps(_payload(halted.repo, tool_input=tool_input), ensure_ascii=False).encode("utf-8"),
        env={**env, "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT)}, cwd=halted.repo,
    )
    assert _outcome(proc) == "rewritten", (proc.stdout[:300], proc.stderr[-400:])


# (b) launch-anchor dependency: the bootstrap path does not resolve.
@pytest.mark.xfail(
    strict=True,
    reason="REPRODUCES at base: a CLAUDE_PLUGIN_ROOT that does not resolve to _hook_boot.py makes the registered "
    "`-c` loader write a stderr banner and exit 0 with no decision; the dispatcher never runs, so no dispatcher-side "
    "fix reaches it. Settle on live fires from the C2 ledger (no entry record for the tool_use_id).",
)
def test_b_unresolvable_plugin_root_is_not_a_silent_pass(tmp_path):
    _need(_HOOKS_JSON)
    argv = _registered_argv(tmp_path / "no-such-plugin-root")
    proc, _ = _fire(json.dumps(_payload(tmp_path)).encode("utf-8"), argv=argv)
    assert b"COORDINATOR HOOK SEAM" in proc.stderr  # the seam's only signal is the banner
    _assert_taken_or_named(proc)


# (c) unparseable stdin.
@pytest.mark.xfail(
    strict=True,
    reason="REPRODUCES at base: main() catches the json parse failure and falls through; an engine-ask stub name in "
    "raw text answers nothing, so the stub fires unrewritten. Fix: deny by name (C3).",
)
def test_c_unparseable_stdin_naming_a_submission_stub_is_denied():
    _need(_DISPATCHER)
    truncated = b'{"tool_name":"Workflow","tool_input":{"name":"coordinator:engine-ask","args":{"ask":"cut mid'
    proc, _ = _fire(truncated)
    _assert_taken_or_named(proc)


def test_c_unparseable_stdin_for_an_unrelated_call_stays_silent():
    """The other side: raw text naming no submission stub is not the dispatcher's to deny."""
    _need(_DISPATCHER)
    proc, _ = _fire(b'{"tool_name":"Workflow","tool_input":{"name":"some-other')
    assert _outcome(proc) == "passed-through"


# (d) sibling PreToolUse(Workflow) registrations.
def test_d_pretooluse_workflow_registrations_are_the_known_set():
    """Every PreToolUse registration whose matcher admits `Workflow`; a new one must be judged for answering allow
    beside the dispatcher's rewrite."""
    _need(_HOOKS_JSON)
    doc = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    hits = []
    for entry in doc["hooks"]["PreToolUse"]:
        if re.fullmatch(entry.get("matcher", ""), "Workflow"):
            hits.append(" ".join(h.get("command", "") + " " + " ".join(h.get("args", [])) for h in entry["hooks"]))
    assert len(hits) == 2, hits
    assert any("guard_brief_prescribed_method" in h for h in hits)
    assert any("preuse-workflow-dispatch.py" in h for h in hits)


# Whether the harness keeps updatedInput when guard_brief_prescribed_method (exit 0, no decision) runs beside the
# dispatcher is merge order inside Claude Code and cannot be driven in-process; settle it from the C2 ledger exit record
# (outcome=rewritten while the stub halts with 'did not rewrite'). PostToolUse matchers cannot affect a PreToolUse rewrite.


# (e) dispatcher killed before its write.
@pytest.mark.xfail(
    strict=True,
    reason="REPRODUCES at base: a dispatcher killed (the registration's 15s timeout) before its write leaves nothing "
    "on stdout and no record; the harness treats a timed-out hook as non-blocking, so the call passes through. "
    "The fix is the C2 entry record (never-invoked / killed / crashed told apart) plus a stub halt naming it.",
)
def test_e_dispatcher_killed_before_write_leaves_a_ledger_record(tmp_path):
    _need(_DISPATCHER)
    payload = json.dumps(_payload(tmp_path)).encode("utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(_DISPATCHER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(tmp_path / "home")}, **no_console_creationflags(),
    )
    proc.stdin.write(payload)
    proc.stdin.close()
    proc.kill()
    out, _ = proc.communicate()
    assert out.strip(), "killed before write: no decision on stdout"


# (f) import-time crash before main().
@pytest.mark.xfail(
    strict=True,
    reason="REPRODUCES at base: a top-level import failure (a missing sibling helper module) raises before main(); "
    "exit 1 with a traceback is non-blocking in the harness, so the call passes through unrewritten. "
    "The dispatcher has no crash boundary around its imports.",
)
def test_f_import_time_crash_is_denied_by_name(tmp_path):
    _need(_DISPATCHER)
    lone = tmp_path / "hooks" / "scripts"
    lone.mkdir(parents=True)
    shutil.copy2(_DISPATCHER, lone / _DISPATCHER.name)  # no _stop_family_runner beside it
    proc = subprocess.run(
        [sys.executable, str(lone / _DISPATCHER.name)], input=json.dumps(_payload(tmp_path)).encode("utf-8"),
        capture_output=True, timeout=60, **no_console_creationflags(),
    )
    assert proc.returncode != 0 and b"ModuleNotFoundError" in proc.stderr  # the crash is real
    _assert_taken_or_named(proc)
