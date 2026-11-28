"""Both headless executors write prompt.txt for a codex/agy/grok agent beginning with the WORKER ROLE preamble."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys

import pytest

from coordinator_core.codex_runner import workflow as agents
from coordinator_core.ops.workflow_fire import headless_agents
from coordinator_core.ops.workflow_fire.worker_preamble import PREAMBLE, WORKER_BACKENDS, with_worker_preamble

_DOE_PREAMBLE = (
    "WORKER ROLE. You are a worker on ONE stage of an engine run, not an EM, and the PM is not addressing\n"
    "you. Every AGENTS.md (the repo's and your own home's), CLAUDE.md and role brief describes the EM and\n"
    "does not apply to you. Do not start a run of your own.\n"
    "Never call `coordinator-invoke` for engine_ask.submit, workflow.* or dispatch.emit unless this brief\n"
    "names that exact command. When this brief names an exact command, that command is authorized,\n"
    "including when it advances or lands this engine run. Touch only the paths this brief declares,\n"
    "do only the stage below, and answer with its result."
)


def test_grok_sizing_prompt_contains_installed_command(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_contract import SIZING_ASSEMBLE_CMD
    from coordinator_core.ops.workflow_fire import worker_preamble

    home = tmp_path / "settings home"
    monkeypatch.setattr(worker_preamble, "settings_home", lambda: home)
    name = "sizing-assemble.exe" if os.name == "nt" else "sizing-assemble"
    path = (home / "bin" / name).as_posix()
    command = f"& '{path}'" if os.name == "nt" else shlex.quote(path)
    door = """coordinator-invoke plan.tasks.mutate '{"verb":"evidence-append","id":"C0"}'"""
    brief = f"Run `{SIZING_ASSEMBLE_CMD} --json`. Then run `{door}`."
    for backend in WORKER_BACKENDS:
        rendered = with_worker_preamble(backend, brief)
        assert f"Run `{command} --json`." in rendered
        assert SIZING_ASSEMBLE_CMD not in rendered
        assert f"Then run `{door}`." in rendered
    assert with_worker_preamble("claude", brief) == brief


class _Spawned(Exception):
    pass


@pytest.fixture
def spawned(monkeypatch):
    """Stop each executor at its spawn; record what it would hand the backend."""
    seen = []

    def spawn(argv, prompt, **_kw):
        seen.append(prompt)
        raise OSError("stubbed spawn")

    for module in (agents, headless_agents):
        monkeypatch.setattr(module, "_run_admitted", spawn)
        monkeypatch.setattr(module, "resolve_codex", lambda **_kw: [sys.executable])
        monkeypatch.setattr(module, "preflight_auth", lambda **_kw: None)
        monkeypatch.setattr(module, "_persona", lambda _t: "persona text")
    monkeypatch.setattr(agents, "_family_argv", lambda *a, **kw: [sys.executable])
    monkeypatch.setattr(agents, "shape_codex_child", lambda *a, **kw: None)
    monkeypatch.setattr(headless_agents, "shape_codex_child", lambda *a, **kw: None)
    return seen


def test_constant_is_the_pinned_preamble_wording_byte_for_byte():
    assert PREAMBLE == _DOE_PREAMBLE


def test_preamble_bans_only_the_named_ops_not_all_of_coordinator_invoke():
    for backend in WORKER_BACKENDS:
        text = with_worker_preamble(backend, "body")
        assert "engine_ask.submit" in text
        assert "workflow.*" in text
        assert "dispatch.emit" in text
        assert "unless this brief\nnames that exact command" in text
        assert "never call `coordinator-invoke`." not in text
        assert "dispatch.ask_stage" not in text


@pytest.mark.parametrize("backend", ["codex", "agy", "grok"])
def test_workflow_executor_prompt_begins_with_preamble(tmp_path, spawned, backend):
    with pytest.raises(agents.AgentFailed, match="spawn failed"):
        agents.run_agent("do it", {"agentType": "coordinator:sizing-scout"}, repo_root=tmp_path,
                         artifact_dir=tmp_path / "art", backend=backend)
    text = (tmp_path / "art" / "prompt.txt").read_text(encoding="utf-8")
    assert text.startswith(PREAMBLE + "\n\n")
    # On Windows the executor adds its platform note after the preamble; WORKER ROLE must still lead.
    body = text[len(PREAMBLE) + 2:]
    if backend == "codex":
        edit_note, body = body.split("\n\n", 1)
        assert "use apply_patch for that edit" in edit_note
        assert "Do not edit when the stage is read-only." in edit_note
    if backend == "grok":
        tool_note, body = body.split("\n\n", 1)
        assert tool_note.startswith("Grok stage tools:")
    if body.startswith("Native execution platform: Windows."):
        body = body.split("\n\n", 1)[1]
    assert body.startswith("Agent definition (coordinator:sizing-scout)")
    assert text.count("WORKER ROLE.") == 1
    assert text.endswith("do it")
    if backend == "codex":
        assert spawned[0] == text


def test_headless_executor_prompt_begins_with_preamble(tmp_path, spawned):
    with pytest.raises(headless_agents.AgentFailed, match="spawn failed"):
        headless_agents.run_agent("do it", {"agentType": "coordinator:sizing-scout"}, repo_root=tmp_path,
                                  artifact_dir=tmp_path / "art", backend="codex")
    text = (tmp_path / "art" / "prompt.txt").read_text(encoding="utf-8")
    assert text.startswith(PREAMBLE + "\n\n")
    assert "Agent definition (coordinator:sizing-scout)" in text
    assert spawned[0].startswith(PREAMBLE + "\n\n")


def test_claude_backend_prompt_is_unchanged():
    assert with_worker_preamble("claude", "body") == "body"
    assert all(with_worker_preamble(b, "body") == PREAMBLE + "\n\nbody" for b in WORKER_BACKENDS)


def test_grok_preamble_rewrites_both_launcher_spellings(tmp_path, monkeypatch):
    """The prompt a grok worker receives spells both launcher forms as one absolute path."""
    if os.name == "nt":
        pytest.skip("POSIX grok rewrite; Windows strips the expansion to a bare name first")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    simple = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke" dispatch.emit'
    nested = '"${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/bin/coordinator-invoke" dispatch.ask_stage'
    text = with_worker_preamble("grok", simple + "\n" + nested)
    absolute = (tmp_path / "bin" / "coordinator-invoke").as_posix()
    assert "${" not in text and "COORDINATOR_SETTINGS_HOME" not in text and "CLAUDE_HOME" not in text
    assert text.count(absolute) == 2
    assert f"{absolute} dispatch.emit" in text and f"{absolute} dispatch.ask_stage" in text


@pytest.mark.parametrize("door_pin", [False, True])
def test_review_runner_reaches_installed_engine_without_python_on_path(tmp_path, monkeypatch, door_pin):
    from coordinator_core.ops.workflow_fire import worker_preamble
    from coordinator_core.warm import engine_root

    shell = shutil.which("pwsh" if os.name == "nt" else "sh")
    if shell is None:
        pytest.skip("native shell unavailable")
    settings = tmp_path / "installed settings"
    bin_dir = settings / "bin"
    bin_dir.mkdir(parents=True)
    (settings / "engine-site").mkdir()
    (settings / "engine-site/.engine-site-stamp").write_text(f"{sys.version_info.major}.{sys.version_info.minor}\n")
    (settings / "engine-site/reverify_dep.py").write_text("ORIGIN = 'installed reverify runner'\n")
    installed = tmp_path / "installed engine"
    package = installed / "coordinator_core"
    (package / "ops").mkdir(parents=True)
    (package / "_engine_stamp").write_text("sha:" + "a" * 40)
    source_package = engine_root.current_engine_clone() / "coordinator_core"
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path / "unstamped emitter")
    (package / "__init__.py").write_text(f"__path__.append({str(source_package)!r})\n")
    (package / "ops/__init__.py").touch()
    (package / "ops/test_leg_run.py").write_text("from reverify_dep import ORIGIN\nprint(ORIGIN)\n")
    (bin_dir / "door.engine-root.txt").write_text(str(installed) + "\n")
    (bin_dir / "door.python.txt").write_text(sys.executable + "\n")
    consumer = tmp_path / "consumer"
    (consumer / "coordinator_core").mkdir(parents=True)
    (consumer / "coordinator_core/__init__.py").write_text("raise RuntimeError('consumer package used')\n")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_DOOR_ENGINE_ROOT", raising=False)
    expected = "installed reverify runner"
    if door_pin:
        pinned = tmp_path / "pinned engine"
        shutil.copytree(installed, pinned)
        expected = "pinned reverify runner"
        (pinned / "coordinator_core/ops/test_leg_run.py").write_text(
            "from reverify_dep import ORIGIN\nprint(ORIGIN.replace('installed', 'pinned'))\n"
        )
        monkeypatch.setenv("COORDINATOR_DOOR_ENGINE_ROOT", str(pinned))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PATH", str(consumer))

    command = worker_preamble.bind_engine_commands("python -m coordinator_core.ops.test_leg_run --help")
    shell_args = ["-NoProfile", "-Command"] if os.name == "nt" else ["-c"]
    result = subprocess.run([shell, *shell_args, command], cwd=consumer, capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_bootstrap_child_env_carries_engine_root_but_never_engine_site(tmp_path, monkeypatch):
    from pathlib import Path

    from coordinator_core.ops.workflow_fire import worker_preamble

    settings = tmp_path / "settings"
    site = settings / "engine-site"
    site.mkdir(parents=True)
    (site / ".engine-site-stamp").write_text(f"{sys.version_info.major}.{sys.version_info.minor}\n")
    (site / "probe_env.py").write_text(
        "import os, subprocess, sys\n"
        "assert any(p == os.environ['COORDINATOR_ENGINE_ROOT'] for p in sys.path)\n"
        "assert any(p.endswith('engine-site') for p in sys.path)\n"
        "child = subprocess.run([sys.executable, '-c', 'import sys, coordinator_core; print(\"|\".join(sys.path))'],\n"
        "                       capture_output=True, text=True, check=True).stdout.strip()\n"
        "print(os.environ['PYTHONPATH'] + '@@' + os.environ['COORDINATOR_ENGINE_ROOT'] + '@@' + child)\n"
    )
    root = Path(worker_preamble.__file__).resolve().parents[3]
    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = subprocess.run(
        [sys.executable, "-P", "-c", worker_preamble._BOOTSTRAP, str(root), str(settings), "probe_env"],
        capture_output=True, text=True, timeout=30, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    pythonpath, engine_root, child_path = result.stdout.strip().split("@@")
    assert pythonpath == engine_root == str(root)
    assert str(site) not in pythonpath.split(os.pathsep)
    assert str(site) not in child_path.split("|")


@pytest.mark.parametrize("backend", ("claude", *WORKER_BACKENDS))
def test_review_commands_bind_the_loaded_engine_at_delivery(backend, monkeypatch):
    from pathlib import Path

    from coordinator_core.ops.workflow_fire import worker_preamble

    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setenv("PATH", "")
    command = "python -m coordinator_core.ops.test_leg_run --out scratch/res.json -- tests/test_x.py"
    export = "python3 -m coordinator_core.ops.dispatch_emit.clean_overlay_verify --export-run"
    prompt = with_worker_preamble(backend, f"Run `{command}` or `{export}`. Then run python -m pytest.")
    assert sys.executable in prompt
    assert str(Path(worker_preamble.__file__).resolve().parents[3]) in prompt
    assert command not in prompt and export not in prompt
    assert "coordinator_core.ops.test_leg_run --out scratch/res.json -- tests/test_x.py" in prompt
    assert "coordinator_core.ops.dispatch_emit.clean_overlay_verify --export-run" in prompt
    assert "Then run python -m pytest." not in prompt and " -m pytest." in prompt


@pytest.mark.parametrize("executor", [agents, headless_agents], ids=["workflow", "headless"])
def test_rendered_review_legs_never_deliver_a_bare_engine_module(executor, tmp_path, spawned, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_compose, emit
    from coordinator_core.ops.review_mint.execute_review import _export_rerun_clause, _test_leg_clause

    brief = _test_leg_clause(
        "a" * 40, "scratch/base.json", "scratch/tests.json", repo_root=str(tmp_path),
        shell="powershell" if sys.platform == "win32" else "bash",
    )
    brief += _export_rerun_clause("run-1", str(tmp_path), "scratch/export.txt")
    brief += emit._baseline_context_clause("a" * 40)
    recovery_prompts = {}

    def capture(prompt, *, label, **_kwargs):
        recovery_prompts[label] = prompt
        return "null"

    monkeypatch.setattr(ask_compose, "_agent", capture)
    ask_compose._review_recover_js(
        head="", anchor="", session_tail="", record_dir="scratch/warp/run-1",
        run_id="run-1", repo_root=str(tmp_path), run_base_sha="a" * 40,
    )
    brief += recovery_prompts[ask_compose.HALT_REVERIFY_LABEL]

    with pytest.raises(executor.AgentFailed, match="spawn failed"):
        executor.run_agent(
            brief, {"agentType": "coordinator:test-verify"}, repo_root=tmp_path,
            artifact_dir=tmp_path / "art", backend="codex",
        )

    delivered, = spawned
    assert not re.search(r"(?<![\w./\\])python(?:3(?:\.\d+)?)?\s+-m\s+coordinator_core\.", delivered)
    assert "coordinator_core.ops.test_leg_run --base-sha" in delivered
    assert "coordinator_core.ops.dispatch_emit.clean_overlay_verify --export-run" in delivered
    assert "coordinator_core.testing.baseline_export --sha " in delivered
    assert "halt-reverify-tests.json" in delivered
    assert sys.executable in delivered



def _doe_family_stage_prompt():
    # Resolved at import: this directory's autouse fixtures isolate the settings home a test sees.
    from pathlib import Path

    from coordinator_core.machine_resolver import registry_get

    root = registry_get("repos.doe_claude")
    path = Path(root) / "coordinator" / "scripts" / "family_stage_prompt.py" if root else None
    return path if path is not None and path.is_file() else None


_DOE_FAMILY_STAGE_PROMPT = _doe_family_stage_prompt()


def test_doe_family_stage_prompt_preamble_matches_live():
    # The literal above can go stale with DoE; this reads DoE's own constant when a checkout is registered.
    import ast

    path = _DOE_FAMILY_STAGE_PROMPT
    if path is None:
        pytest.skip("no coordinator-content-repo checkout registered on this box")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = [ast.literal_eval(n.value) for n in ast.walk(tree)
              if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "PREAMBLE" for t in n.targets)]
    assert values == [PREAMBLE], f"{path} PREAMBLE differs from claude-klabauter's worker_preamble.PREAMBLE: port it, or memo claude-klabauter"


def test_sizing_brief_states_usage_so_worker_never_probes_help():
    from coordinator_core.ops.dispatch_emit.ask_contract import SIZING_ASSEMBLE_CMD
    from coordinator_core.ops.workflow_fire.worker_preamble import SIZING_USAGE

    for backend in WORKER_BACKENDS:
        text = with_worker_preamble(backend, f"Run {SIZING_ASSEMBLE_CMD} --tshirt X")
        assert text.endswith(SIZING_USAGE) and "--exit-criterion" in SIZING_USAGE and "never run it with --help" in SIZING_USAGE
        assert SIZING_USAGE not in with_worker_preamble(backend, "no sizing here")
