"""codex-coordinator launches codex as a coordinator EM and does not shadow a bare codex."""

import importlib.util
import os
import subprocess
import types
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parents[1]
BIN = BIN_DIR / "codex-coordinator.py"
ALIAS = BIN_DIR / "coordinator-codex.py"
spec = importlib.util.spec_from_file_location("codex_coordinator", BIN)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

BYPASS = "--dangerously-bypass-approvals-and-sandbox"
EXPECTED_ENV = "COORDINATOR_CODEX_EXPECTED_MODEL"


def test_interactive_argv_leads_with_bypass_then_the_callers_args():
    argv = cc.interactive_argv("codex", ["--cd", "/work"])
    assert argv == ["codex", BYPASS, "--cd", "/work"]
    assert "--model" not in argv and "-m" not in argv


@pytest.mark.parametrize("extra", [["--yolo"], [BYPASS], ["--yolo", BYPASS, "--yolo"], ["--cd", "/w", "--yolo"]])
def test_a_caller_yolo_or_bypass_folds_into_exactly_one_bypass(extra):
    argv = cc.interactive_argv("codex", extra)
    assert argv.count(BYPASS) == 1 and "--yolo" not in argv
    assert argv[1] == BYPASS
    assert [a for a in argv[2:]] == [a for a in extra if a not in (BYPASS, "--yolo")]


def test_a_yolo_after_the_option_terminator_is_the_callers_prompt_text():
    argv = cc.interactive_argv("codex", ["--", "--yolo"])
    assert argv == ["codex", BYPASS, "--", "--yolo"]


def test_strip_coordinator_drops_only_that_flag():
    assert cc.strip_coordinator(["--coordinator", "--cd", "/w", "--coordinator"]) == ["--cd", "/w"]


def _launcher(monkeypatch, *, nt: bool):
    """The real unimatrix-codex-launch module with its os/subprocess seen as a mocked platform."""
    mod = cc.load_launcher()
    monkeypatch.setattr(cc, "load_launcher", lambda: mod)
    calls = {"call": [], "execv": []}

    def fake_call(argv, **kw):
        calls["call"].append((argv, kw))
        return 7

    mod.subprocess = types.SimpleNamespace(call=fake_call, DEVNULL=subprocess.DEVNULL, run=None, Popen=None)
    mod.os = types.SimpleNamespace(**{**vars(os), "name": "nt" if nt else "posix",
                                      "execv": lambda path, argv: calls["execv"].append((path, argv))})
    monkeypatch.setattr("shutil.which", lambda name: "codex-bin")
    monkeypatch.setenv(EXPECTED_ENV, "stale")
    return calls


def test_main_on_windows_inherits_the_console_with_one_model_and_the_env_set(monkeypatch):
    from coordinator_core.agent_pipe.model_tiers import model_for_role

    calls = _launcher(monkeypatch, nt=True)
    assert cc.main(["--coordinator", "--yolo", "--cd", "/w"]) == 7
    assert calls["execv"] == []
    (argv, kw), = calls["call"]
    assert argv[:2] == ["codex-bin", BYPASS]
    assert argv.count(BYPASS) == 1 and "--coordinator" not in argv
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == model_for_role("codex", "em")
    assert os.environ[EXPECTED_ENV] == model_for_role("codex", "em")
    assert "timeout" not in kw and "creationflags" not in kw
    assert "stdout" not in kw and "stderr" not in kw and "stdin" not in kw


def test_main_on_posix_execs_codex_with_one_model_and_the_env_set(monkeypatch):
    from coordinator_core.agent_pipe.model_tiers import model_for_role

    calls = _launcher(monkeypatch, nt=False)
    cc.main(["--yolo"])
    assert calls["call"] == []
    (path, argv), = calls["execv"]
    assert path == "codex-bin"
    assert argv[:2] == ["codex-bin", BYPASS]
    assert argv.count("--model") == 1
    assert os.environ[EXPECTED_ENV] == model_for_role("codex", "em")


def test_a_callers_own_model_is_not_doubled_and_clears_the_verifier_env(monkeypatch):
    calls = _launcher(monkeypatch, nt=True)
    cc.main(["--model", "custom"])
    (argv, _), = calls["call"]
    assert argv.count("--model") == 1 and argv[argv.index("--model") + 1] == "custom"
    assert EXPECTED_ENV not in os.environ


def test_alias_delegates_to_codex_coordinator_through_runpy():
    text = ALIAS.read_text(encoding="utf-8")
    assert "runpy.run_path" in text and 'with_name("codex-coordinator.py")' in text
    assert BIN.is_file()
