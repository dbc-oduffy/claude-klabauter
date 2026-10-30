"""
coordinator_core.ops.dispatch_emit.tests.test_grind_drain_record_by_file

Purpose: end-to-end pin that the grind's ledger-sweep:drain leg lands a run record whose fields are lossy
to retype (non-ASCII, ``$``, backticks, quotes, ``%``, newlines, over 10000 chars) byte-identical, with no
model copying record bytes and no ``drain sweep did not complete`` hand-back.

The emitted script (default arguments) runs under node. Its stubbed drain agent does only what a real one
may: it takes the prompt it was handed, files it as the agent's own transcript, and runs the one
``grind-row run-record`` command the prompt names -- the record segment is never typed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract
from coordinator_core.tests.git_seed import seeded_repo

from .test_grind_compose import _FIXTURE_PROFILE_DIR, _compose

_REPO_ROOT = Path(__file__).resolve().parents[4]
_RUN_ID = "20261017T000000Z"
_SESSION = "sess-drain-record"
_PROFILE = "fixture"

# 10000+ chars of text that is lossy to retype: non-ASCII, shell metacharacters, quotes, percent, newlines.
_LOSSY = (
    "naïve café — 日本語 \U0001f600 $HOME `whoami` $(id) \"dq\" 'sq' %s %25 100% \\n\\\\ back\\slash\n"
    "line2\r\n\ttabbed ${X:-y} ;|&<> !#  \n"
) * 80

_HELPER_PY = r'''
"""Stands in for the drain agent: files the prompt as its transcript, then runs the prompt's one command."""
import io, json, os, re, shlex, sys, contextlib
from pathlib import Path

prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
claude_home = Path(os.environ["CLAUDE_CONFIG_DIR"])
transcript = claude_home / "projects" / "p" / os.environ["CLAUDE_CODE_SESSION_ID"] / "subagents" / "workflows" / "w" / "agent-1.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8")

line = next(l for l in prompt.splitlines() if "grind-row run-record" in l and l.startswith("${COORDINATOR_SETTINGS_HOME"))
argv = shlex.split(line.split("grind-row ", 1)[1])
dec_pieces = re.findall(r"^printf %s (\S+) >> \S+\.dec$", prompt, flags=re.M)
argv = ["".join(dec_pieces) if a.startswith("$(cat ") else a for a in argv]
assert "--record-urlenc" not in argv, "record bytes must not ride the command"
from coordinator_core.backlog_grind_assemble import grind_rows
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    code = grind_rows.main(argv)
reply = {"outcome": "recorded" if code == grind_rows.EXIT_OK else "record-failed"}
if code == grind_rows.EXIT_OK:
    printed = json.loads(out.getvalue())
    reply["swept"] = printed.get("swept", True)
    reply["sweep_reason"] = printed.get("sweep_reason", "")
else:
    reply["reason"] = err.getvalue()
print(json.dumps(reply))
'''

_HARNESS_JS = r"""
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
const [,, scriptPath, helper, python, promptPath, lossyPath] = process.argv;
const src = fs.readFileSync(scriptPath, 'utf8').replace(/\r\n/g, '\n').replace(/export const meta = \{[\s\S]*?\n\};\n/, '');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const lossy = fs.readFileSync(lossyPath, 'utf8');
const rowNo = (prompt) => { const m = /row(\d+)/.exec(prompt); return m ? Number(m[1]) : 0; };
const drains = [];
const reply = async (opts, prompt) => {
  const label = opts.label || '';
  const rows = [...new Set([...prompt.matchAll(/"row_id":"(row\d+)"/g)].map((m) => m[1]))];
  if (label === 'triage') return { rows: rows.map((r) => ({ row: r, verdict: 'confirmed-bug', tshirt_size: 'S', has_tradeoff: false, evidence: 'e', fix_plan: 'p' })), stale: [] };
  if (label === 'fix') return { outcome: 'done', touched_files: ['f' + rowNo(prompt) + '.py'] };
  if (label.startsWith('verify')) return { outcome: 'pass', exit_code: 0, output: {} };
  if (label === 'commit') {
    return {
      committed: true, sha: 'abc123', already_landed: true,
      rows: rows.filter((r) => r !== 'row6').map((r) => ({ row_id: r, outcome: 'settled', sha: 'abc123' })),
      unlanded: rows.includes('row6') ? [{ row_id: 'row6', reason: lossy }] : [],
    };
  }
  if (label.startsWith('ledger-sweep')) {
    fs.writeFileSync(promptPath, prompt, 'utf8');
    const out = execFileSync(python, [helper, promptPath], { encoding: 'utf8', env: process.env });
    const drained = JSON.parse(out.trim().split('\n').pop());
    drains.push({ opts: { model: opts.model, effort: opts.effort }, drained });
    return drained;
  }
  return {};
};
const agent = (prompt, opts = {}) => reply(opts, prompt);
const budget = { spent: () => 0, remaining: () => null, total: null };
const fn = new AsyncFunction('args', 'agent', 'phase', 'log', 'budget', 'parallel', src);
const result = await fn(
  { run_stamp: process.env.RUN_ID, script_path: 'x.mjs', profile_dir: process.env.PROFILE_DIR, repo_root: process.env.REPO_ROOT, run_token: 'tok' },
  agent, () => {}, () => {}, budget, async (xs) => Promise.all(xs),
);
console.log('RESULT ' + JSON.stringify({ drains, result: result === undefined ? null : result }));
"""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_record_lossy_to_retype_lands_byte_identical_with_no_drain_sweep_hand_back(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    repo = tmp_path / "repo"
    repo.mkdir()
    seeded_repo(repo, readme="x")
    (repo / "state" / "bug-backlog").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for name, text in (
        ("grind.mjs", _compose()), ("helper.py", _HELPER_PY), ("harness.mjs", _HARNESS_JS), ("lossy.txt", _LOSSY),
    ):
        (scratch / name).write_bytes(text.encode("utf-8"))
    env = {
        **__import__("os").environ,
        "RUN_ID": _RUN_ID, "PROFILE_DIR": str(_FIXTURE_PROFILE_DIR), "REPO_ROOT": repo.as_posix(),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"), "CLAUDE_CODE_SESSION_ID": _SESSION,
        "PYTHONPATH": str(_REPO_ROOT), "PYTHONIOENCODING": "utf-8",
    }
    for var in ("CLAUDE_SESSION_ID", "COORDINATOR_SESSION_ID"):
        env.pop(var, None)
    proc = subprocess.run(
        [node, str(scratch / "harness.mjs"), str(scratch / "grind.mjs"), str(scratch / "helper.py"), sys.executable,
         str(scratch / "prompt.txt"), str(scratch / "lossy.txt")],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=120, cwd=str(repo), env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout.rsplit("RESULT ", 1)[1])

    assert len(seen["drains"]) == 1, seen["drains"]
    drain = seen["drains"][0]
    assert drain["drained"]["outcome"] == "recorded" and drain["drained"].get("swept") is not False, drain
    assert drain["opts"]["effort"] == "low"

    prompt = (scratch / "prompt.txt").read_text(encoding="utf-8")
    open_marker, close_marker = contract.record_markers(_RUN_ID)
    # The open marker is followed by a `<len> <sha256>` digest line, then the record.
    segment = re.search(
        rf"^{re.escape(open_marker)}\n\d+ [0-9a-f]{{64}}\n(\S+)\n{re.escape(close_marker)}", prompt, flags=re.M
    ).group(1)
    assert len(segment) > 10000
    scripted = json.loads(unquote(segment))
    unlanded = [h for h in scripted["handed_back"] if h.get("reason") == _LOSSY or _LOSSY in str(h.get("reason", ""))]
    assert unlanded, "the lossy field must reach the record, or the pin proves nothing"

    landed_path = repo / "state" / "queue-grind" / _PROFILE / "runs" / f"{_RUN_ID}.json"
    assert landed_path.is_file(), proc.stderr
    landed = json.loads(landed_path.read_text(encoding="utf-8"))
    assert {k: v for k, v in landed.items() if k not in ("usage", "usage_reason")} == scripted
    assert any(_LOSSY in str(h.get("reason", "")) for h in landed["handed_back"])
    assert not any(contract.SWEEP_FAILED_HEAD in str(h.get("reason", "")) for h in landed["handed_back"])
    assert contract.SWEEP_FAILED_HEAD not in json.dumps(seen["result"])


def test_the_emitted_drain_prompt_carries_a_marker_and_no_record_relay():
    script = _compose()
    start = script.index("ledger-sweep:drain")
    drain = script[max(0, start - 6000): start + 2500]
    assert "--record-urlenc" not in script
    assert f"--{contract.RECORD_FROM_TRANSCRIPT_FLAG}" in script
    assert "_piece_blocks" not in drain and "_recEnc" in script
    assert "effort: 'low'" in drain or '"effort":"low"' in drain or "effort: \"low\"" in drain
