"""The default ask script's top-level functions reference no binding that is declared only inside a block.

The Workflow runner executes the script body directly, so a hoisted function that names a
`const`/`let` declared inside an `if` block throws `ReferenceError: X is not defined` at run time
while `dispatch.ask_gate` stays green. This ratchet runs each top-level function declaration under
node with the runtime globals stubbed and fails on any `is not defined`. A temporal-dead-zone error
(`Cannot access 'X' before initialization`) is out of scope: the calls run before the body does, so
a top-level binding is legitimately uninitialised.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_plan_blitz, ask_roadmap
from coordinator_core.warm.caller_context import resolve_caller_context

_REPO_ROOT = str(Path(ask_compose.__file__).resolve().parents[3])
# Resolved at import: the suite-wide fixture clears CLAUDE_PLUGIN_ROOT before each test.
_PLUGIN_ROOT = resolve_caller_context().plugin_root
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

_HARNESS = r"""
import fs from 'node:fs';
const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta');
const stubs = new Set(JSON.parse(process.argv[3]));
const stub = () => new Proxy(function () {}, {
  get: (_t, k) => (k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : stub()),
  apply: () => stub(),
});
const scope = new Proxy({}, {
  has: (_t, k) => typeof k === 'string' && stubs.has(k),
  get: (_t, k) => (k === Symbol.unscopables ? undefined : stub()),
});
const names = [...new Set([...src.matchAll(/^(?:async )?function\s*\*?\s*([A-Za-z0-9_$]+)\s*\(/gm)].map((m) => m[1]))];
const calls = names.map((n) => `if (typeof ${n} === 'function') { __names.push(${JSON.stringify(n)}); await __call(${JSON.stringify(n)}, ${n}); }`);
const body = 'with (__scope) {\n' + calls.join('\n') + '\n' + src + '\n}';
const bad = [];
const ran = [];
const __call = async (n, fn) => {
  try { await fn(); } catch (e) {
    if (e instanceof ReferenceError && /is not defined/.test(e.message)) bad.push(n + ': ' + e.message);
  }
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
try { await new AsyncFunction('__scope', '__call', '__names', body)(scope, __call, ran); } catch (e) {
  if (e instanceof ReferenceError && /is not defined/.test(e.message)) bad.push('<top level>: ' + e.message);
}
process.on('unhandledRejection', () => {});
console.log(JSON.stringify({ bad, ran: ran.length }));
"""

# Runtime globals the Workflow runner injects; everything else a function names must be declared in the script.
_RUNTIME_GLOBALS = ["args", "phase", "agent", "workflow", "parallel", "log", "sleep", "bash", "emit", "scratchpad"]


def _compose() -> str:
    return ask_compose.compose_ask_script(
        repo_root=_REPO_ROOT,
        prompt="add a writes field to a schema",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        script_path="x.mjs",
        plan_blitz_text=ask_plan_blitz.load_plan_blitz_text(_PLUGIN_ROOT),
        roadmap_blitz_text=ask_roadmap.load_roadmap_blitz_text(_PLUGIN_ROOT),
    )


def _run(tmp_path: Path, script: str) -> dict:
    (tmp_path / "s.mjs").write_text(script, encoding="utf-8")
    (tmp_path / "h.mjs").write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(tmp_path / "h.mjs"), str(tmp_path / "s.mjs"), json.dumps(_RUNTIME_GLOBALS)],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=_NO_CONSOLE,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
]


def test_default_ask_script_hoisted_functions_reference_only_visible_bindings(tmp_path):
    result = _run(tmp_path, _compose())
    assert result["ran"] > 0, "the harness found no top-level function to run"
    assert result["bad"] == [], "hoisted function names a binding not visible at top level:\n" + "\n".join(result["bad"])


def test_a_hoisted_function_naming_a_block_scoped_binding_is_caught(tmp_path):
    script = _compose()
    head, sep, tail = script.partition("\n};\n")
    assert sep, "the script's meta block did not end where expected"
    seeded = head + sep + "function _seededProbe() { return _blockOnly; }\nif (true) { const _blockOnly = 1; }\n" + tail
    result = _run(tmp_path, seeded)
    assert any(b.startswith("_seededProbe:") for b in result["bad"])
