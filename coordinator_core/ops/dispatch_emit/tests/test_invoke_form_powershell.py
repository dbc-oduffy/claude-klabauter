"""Worker prompts spell coordinator-invoke in the host shell's grammar: PowerShell cannot parse the POSIX form."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

from coordinator_core.codex_runner import workflow as agents
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.invoke_form import POSIX, POWERSHELL, host_shell, render_invoke_for_shell

_POSIX_FORM = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'
_QUOTED_THEN_WORD = re.compile(r'"[^"\n]*(?:coordinator-invoke|[\w-]+)"\s+[A-Za-z_][\w.]*\.[\w.]+')
_PARAMS = '\'{"run_id":"r1","failing":["a::b"],"base_red":[]}\''


def _checkpoint_prompt_text() -> str:
    """The checkpoint commit prompt literals, JS escapes undone, as the worker would read them."""
    js = emit._checkpoint_commit_js(
        None, repo_root=None, session_id=None, plan_path="docs/plans/p.md", run_base_sha="a" * 40, reds_var="_reds"
    )
    return js.replace('\\"', '"')


def test_grok_render_rewrites_both_launcher_spellings_in_one_prompt(tmp_path):
    from coordinator_core.ops.dispatch_emit.invoke_form import GROK

    simple = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke" dispatch.emit'
    nested = '"${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/bin/coordinator-invoke" dispatch.ask_stage'
    out = render_invoke_for_shell(simple + "\n" + nested, GROK, bin_dir=tmp_path)
    absolute = (tmp_path / "coordinator-invoke").as_posix()
    assert "${" not in out and "COORDINATOR_SETTINGS_HOME" not in out and "CLAUDE_HOME" not in out
    assert out.count(absolute) == 2
    assert f"{absolute} dispatch.emit" in out and f"{absolute} dispatch.ask_stage" in out


def test_posix_render_is_byte_identical():
    text = _checkpoint_prompt_text()
    assert _POSIX_FORM in text
    assert render_invoke_for_shell(text, POSIX) == text


def test_powershell_checkpoint_prompt_has_no_posix_expansion_or_quoted_word_shape():
    out = render_invoke_for_shell(_checkpoint_prompt_text(), POWERSHELL)
    assert "${" not in out.replace("${{", "")
    assert ":-$HOME" not in out
    assert not _QUOTED_THEN_WORD.search(out)
    assert "coordinator-invoke dispatch.checkpoint_reds" in out
    assert "coordinator-invoke ceremony.commit_v2" in out


def test_powershell_render_covers_other_clis_and_unquoted_spelling():
    text = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/sizing-assemble" x ' \
        "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/backlog-grind-assemble y"
    assert render_invoke_for_shell(text, POWERSHELL) == "sizing-assemble x backlog-grind-assemble y"


def test_host_shell_follows_os():
    assert host_shell("nt") == POWERSHELL
    assert host_shell("posix") == POSIX


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_rendered_command_parses_in_powershell_and_posix_form_does_not():
    script = (
        "$e=$null; $t=$null; "
        "[void][System.Management.Automation.Language.Parser]::ParseInput($env:PARSE_CMD,[ref]$t,[ref]$e); $e.Count"
    )

    def errors(cmd: str) -> int:
        r = subprocess.run(["pwsh", "-NoProfile", "-Command", script], capture_output=True, text=True,
                           env={**os.environ, "PARSE_CMD": cmd})
        return int(r.stdout.strip())

    posix = f"{_POSIX_FORM} dispatch.checkpoint_reds --repo C:/x {_PARAMS}"
    assert errors(posix) > 0
    assert errors(render_invoke_for_shell(posix, POWERSHELL)) == 0


def test_run_agent_hands_the_worker_the_host_shell_form(tmp_path, monkeypatch):
    seen = []

    def spawn(argv, prompt, **_kw):
        seen.append(prompt)
        raise OSError("stubbed spawn")

    monkeypatch.setattr(agents, "_run_admitted", spawn)
    monkeypatch.setattr(agents, "resolve_codex", lambda **_kw: [sys.executable])
    monkeypatch.setattr(agents, "preflight_auth", lambda **_kw: None)
    monkeypatch.setattr(agents, "_persona", lambda _t: "")
    monkeypatch.setattr(agents, "shape_codex_child", lambda *a, **kw: None)
    brief = f"run `{_POSIX_FORM} dispatch.checkpoint_reds '<params>'`"
    with pytest.raises(agents.AgentFailed):
        agents.run_agent(brief, {}, repo_root=tmp_path, artifact_dir=tmp_path / "art", backend="codex")
    text = (tmp_path / "art" / "prompt.txt").read_text(encoding="utf-8")
    if os.name == "nt":
        assert "run `coordinator-invoke dispatch.checkpoint_reds '<params>'`" in text
        assert ":-$HOME" not in text.split("Preserve all operation arguments")[1]
    else:
        assert brief in text
