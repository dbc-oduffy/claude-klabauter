"""agy-coordinator launches agy with EM defaults and does not shadow a bare agy."""

import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "agy-coordinator.py"
ALIAS = Path(__file__).resolve().parents[1] / "coordinator-agy.py"
spec = importlib.util.spec_from_file_location("agy_coordinator", BIN)
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)


def test_interactive_argv_skips_permissions_and_sets_em_defaults():
    from coordinator_core.agent_pipe.model_tiers import AGY_EM_EFFORT, model_for_role

    argv = ac.interactive_argv("agy", ["--add-dir", "/tmp/work"])
    assert argv[:2] == ["agy", "--dangerously-skip-permissions"]
    assert "-p" not in argv and "-r" not in argv
    assert argv[-2:] == ["--add-dir", "/tmp/work"]
    assert argv[argv.index("--model") + 1] == model_for_role("agy", "em")
    assert argv[argv.index("--effort") + 1] == AGY_EM_EFFORT


def test_interactive_argv_respects_caller_overrides():
    argv = ac.interactive_argv("agy", ["--model", "gemini-4-neon", "--effort=medium", "--mode", "plan"])
    assert "--dangerously-skip-permissions" not in argv
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == "gemini-4-neon"
    assert "--effort" not in argv
    assert "--effort=medium" in argv

    sandbox_argv = ac.interactive_argv("agy", ["--model=gemini-4-hydrogen", "--sandbox"])
    assert "--dangerously-skip-permissions" not in sandbox_argv
    assert "--model" not in sandbox_argv
    assert "--model=gemini-4-hydrogen" in sandbox_argv


def test_headless_argv_is_one_turn_without_resume():
    from coordinator_core.agent_pipe.model_tiers import AGY_EM_EFFORT, model_for_role

    argv = ac.headless_argv("agy", "hello", ["--add-dir", "/work"])
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("-p") + 1] == "hello"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == model_for_role("agy", "em")
    assert argv[argv.index("--effort") + 1] == AGY_EM_EFFORT
    assert argv[-2:] == ["--add-dir", "/work"]
    assert "-r" not in argv and "--conversation" not in argv

    custom_fmt = ac.headless_argv("agy", "hello", ["--output-format", "text"])
    assert custom_fmt.count("--output-format") == 1
    assert custom_fmt[custom_fmt.index("--output-format") + 1] == "text"


def test_strip_coordinator_is_not_forwarded():
    assert ac.strip_coordinator(["--coordinator", "--add-dir", "/tmp/work"]) == ["--add-dir", "/tmp/work"]
    assert "--coordinator" not in ac.interactive_argv("agy", ac.strip_coordinator(["--coordinator"]))


def test_main_dispatches_interactive_and_headless(monkeypatch):
    seen = []
    monkeypatch.setattr(ac, "agy_binary", lambda: "/usr/local/bin/agy")
    monkeypatch.setattr(ac, "exec_agy", lambda argv: seen.append(argv) or 0)

    assert ac.main(["--coordinator", "--add-dir", "/repo"]) == 0
    assert seen[-1][0] == "/usr/local/bin/agy"
    assert "--coordinator" not in seen[-1]
    assert "-p" not in seen[-1]

    assert ac.main(["-p", "hi", "--add-dir", "/repo"]) == 0
    assert seen[-1][seen[-1].index("-p") + 1] == "hi"
    assert "--add-dir" in seen[-1]

    assert ac.main(["--print=hi-eq"]) == 0
    assert seen[-1][seen[-1].index("-p") + 1] == "hi-eq"

    with pytest.raises(SystemExit):
        ac.main(["-p"])


def test_coordinator_agy_alias_delegates():
    assert ALIAS.is_file()
    assert "agy-coordinator.py" in ALIAS.read_text(encoding="utf-8")
