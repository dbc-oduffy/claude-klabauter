"""Execute default-emitter verifier commands at an argv-observing runner boundary."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, cli, prompt_fragments
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import MIN_STAGE_BYTES, stage_literal_runs
from coordinator_core.ops.workflow_scaffold import _js_string_literal
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELECTORS = [
    "coordinator_core/ops/workflow_fire/tests/test_headless.py",
    "coordinator_core/ops/workflow_fire/tests/test_headless_sandboxed_forward.py",
    "coordinator_core/warm/tests/test_sandboxed_spawn.py",
]
SPECIAL = ["space path.py", "apostrophe's.py", 'double"quote.py', r"back\slash.py",
           "$dollar.py", "`tick`.py", ";semicolon.py", "$(touch SENTINEL).py"]


def _run(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                            **no_console_creationflags(), **kwargs)
    assert result.returncode == 0, (argv, result.stdout, result.stderr)
    return result.stdout


@pytest.fixture
def emitted(tmp_path, monkeypatch):
    # The directory fixture disables hoisting for text assertions; this oracle uses defaults.
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "export const meta = { phases: [] };\n")
    assert cli.main(["--ask", "add a thing", "--ask-source", "pm", "--repo-root", str(tmp_path)]) == 0
    scripts = list(tmp_path.rglob("*.mjs"))
    script = next(p.read_text(encoding="utf-8") for p in scripts
                  if "_verifyTest = await " in p.read_text(encoding="utf-8"))
    assert "const _PF" in script, "default hoisting did not run"
    assert list(tmp_path.rglob("prompts/verify-tests*.md")), "default staging did not run"
    return tmp_path, script


def _prompt(emitted, targets=SELECTORS, paths=SPECIAL):
    root, script = emitted
    node = shutil.which("node")
    assert node, "node required to evaluate the emitted workflow expression"
    start = script.index("_verifyTest = await ") + len("_verifyTest = await ")
    end = script.index("; } catch", start)
    call = script[start:end]
    defs = "\n".join(line for line in script.splitlines()
                     if re.match(r"const (_PFC?\d+|_briefPrecedenceTail|_agentRead|_agentHead\d*) = ", line))
    fn_start = script.index("function _agentDefsClause() {")
    fn_end = script.index("\n}", fn_start) + 2
    program = (
        defs + "\n" + script[fn_start:fn_end] + "\n"
        + "const _writes = []; const _runId = 'oracle-run';\n"
        + "const _manifest = " + json.dumps({"test_targets": targets, "review_declared_paths": paths}) + ";\n"
        + "const agent = async (prompt, options) => { if (options.label !== 'verify-tests') throw Error('wrong leg'); return prompt; };\n"
        + "(async () => { console.log(JSON.stringify(await " + call + ")); })().catch(e => { console.error(e); process.exitCode = 1; });"
    )
    prompt = json.loads(_run([node, "-"], input=program))
    # Keep each execution surface separate; interpolation would conceal a split command.
    fragments = []
    seen = set()

    def follow(text):
        fragments.append(text)
        for path in re.findall(r"Read ([^\n]+?) in full and follow it as the rest of this brief\.", text):
            candidate = Path(path)
            assert candidate.is_file(), path
            assert candidate.is_relative_to(root), path
            assert candidate not in seen, f"repeated staged pointer: {path}"
            seen.add(candidate)
            follow(candidate.read_text(encoding="utf-8"))

    follow(prompt)
    assert any("candidate commit sha" in text for text in fragments), "emission lost measured guidance"
    return root, fragments


def _test_command(fragments):
    commands = [line.strip().strip("`") for text in fragments for line in text.splitlines()
                if " coordinator_core.ops.test_leg_run " in line and not line.lstrip().startswith("& ")]
    assert len(commands) == 1, commands
    assert "Read " not in commands[0], commands[0]
    return commands[0]


def _record(root, command, *, measured=False):
    emitted_command = command
    bash = shutil.which("bash")
    assert bash, "Bash execution coverage unavailable on this host"
    recorder = root / "argv recorder.py"
    receipt = root / "receipt.json"
    recorder.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        + ("from coordinator_core.invoke.__main__ import _build_arg_parser\n"
           "args = _build_arg_parser().parse_intermixed_args(sys.argv[1:])\n"
           "value = {'op': args.op, 'params': json.loads(args.params_json) if args.params_json else None}\n"
           if measured else "value = sys.argv[1:]\n")
        + "Path(" + repr(str(receipt)) + ").write_text(json.dumps(value), encoding='utf-8')\n",
        encoding="utf-8",
    )
    if measured:
        # Substitute only the executable at the dispatch boundary; the shell transports emitted JSON.
        prefix = command[:command.index("dispatch.measured_rev")].strip()
        command = command.replace(prefix, shlex.quote(os.sys.executable) + " " + shlex.quote(str(recorder)), 1)
    else:
        mod_tok = "coordinator_core.ops.test_leg_run"
        end = command.index(mod_tok) + len(mod_tok)
        command = shlex.quote(os.sys.executable) + " " + shlex.quote(str(recorder)) + command[end:]
    env = dict(os.environ, PYTHONPATH=str(Path(ask_compose.__file__).resolve().parents[3]))
    _run([bash, "-c", command], cwd=root, env=env)
    assert not (root / "SENTINEL").exists(), "shell operand executed"
    assert receipt.is_file(), "the emitted command did not reach the recorder"
    value = json.loads(receipt.read_text(encoding="utf-8"))
    print(json.dumps({"shell": bash, "executable": os.sys.executable,
                      "emitted_command": emitted_command, "command": command, "receipt": value}))
    return value


def test_default_emitter_test_command_delivers_all_selectors(emitted):
    root, fragments = _prompt(emitted)
    command = _test_command(fragments)
    argv = _record(root, command)
    assert argv[argv.index("--") + 1:] == SELECTORS, {"command": command, "argv": argv}


def test_staged_read_target_instruction_retains_nonempty_scope(emitted):
    root, fragments = _prompt(emitted)
    surfaces = [text for text in fragments if "Run the scoped test targets: [" in text]
    assert surfaces, "emission lost the scoped instruction"
    for text in surfaces:
        match = re.search(r"Run the scoped test targets: \[([^\]]*)\]", text)
        assert match, {"split_target_instruction": text}
        assert match.group(1).split(", ") == SELECTORS, text
    for text in fragments:
        for line in text.splitlines():
            if " coordinator_core.ops.test_leg_run " in line and not line.lstrip().startswith("& "):
                assert not line.rstrip().endswith("--"), line
                argv = _record(root, _test_command([text]))
                assert argv[argv.index("--") + 1:] == SELECTORS


def test_default_emitter_measured_command_transports_one_json_object(emitted):
    root, fragments = _prompt(emitted)
    commands = [command for text in fragments for command in re.findall(
        r"First run `(.*?dispatch\.measured_rev.*?)`(?= with run_id|;)", text, re.S)]
    assert len(commands) == 1, commands
    value = _record(root, commands[0], measured=True)
    assert value == {"op": "dispatch.measured_rev", "params": {"run_id": "oracle-run", "paths": SPECIAL}}, value


def test_default_emitter_test_operands_are_shell_data(emitted):
    root, fragments = _prompt(emitted, targets=SPECIAL)
    argv = _record(root, _test_command(fragments))
    assert argv[argv.index("--") + 1:] == SPECIAL


def test_empty_targets_keep_the_existing_runner_fallback(emitted):
    root, fragments = _prompt(emitted, targets=[], paths=[])
    argv = _record(root, _test_command(fragments))
    assert argv[argv.index("--") + 1:] == []


def test_empty_measured_paths_are_a_json_array(emitted):
    root, fragments = _prompt(emitted, targets=[], paths=[])
    commands = [command for text in fragments for command in re.findall(
        r"First run `(.*?dispatch\.measured_rev.*?)`(?= with run_id|;)", text, re.S)]
    assert len(commands) == 1, commands
    value = _record(root, commands[0], measured=True)
    assert value == {"op": "dispatch.measured_rev", "params": {"run_id": "oracle-run", "paths": []}}


@pytest.mark.parametrize("prefix,suffix", [
    ("Run `command '", "' --repo r`\n"),
    ("command -- ", " --tail\n"),
    ("Targets: [", "]\n"),
    ("```bash\ncommand -- ", " --tail\n```\n"),
    ("```powershell\n& command -- ", " --tail\n```\n"),
])
def test_dynamic_edges_keep_commands_and_lists_together(prefix, suffix):
    paragraph = "Unrelated static instruction. " * 40
    sink = {}
    def stage(name, text):
        sink[name] = text
        return _js_string_literal(f"Read staged/{name}.md\n")
    source = _js_string_literal(paragraph + "\n\n" + prefix) + " + operand + " + _js_string_literal(suffix + "\n" + paragraph)
    result = stage_literal_runs(source, "oracle", stage)
    assert sink, "legitimate large static paragraphs must still stage"
    assert all(prefix not in text and suffix not in text for text in sink.values()), sink
    evaluated = json.loads(_run([shutil.which("node"), "-"], input=
        "const operand = 'DATA'; console.log(JSON.stringify(" + result + "));"))
    assert prefix + "DATA" + suffix in evaluated, evaluated


def test_balanced_static_command_stages_and_unknown_escapes_remain_inline():
    sink = {}
    text = "Static instruction. " * MIN_STAGE_BYTES + " `complete command`"
    stage = lambda name, body: sink.setdefault(name, body) and _js_string_literal("pointer")
    stage_literal_runs(_js_string_literal(text), "balanced", stage)
    assert sink == {"balanced": text}
    source = "'" + "x" * MIN_STAGE_BYTES + r"\u00e9' + operand"
    assert stage_literal_runs(source, "unknown", stage) == source
