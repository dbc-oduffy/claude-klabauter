"""reviewer_run: a fake provider executable stands in for the provider CLI."""
from __future__ import annotations

import json
import sys

import pytest

from coordinator_core.agent_pipe import reviewer_run
from coordinator_core.agent_pipe.codex import CodexAdapter
from coordinator_core.agent_pipe.reviewer_provider import ProviderChoice
from coordinator_core.agent_pipe.reviewer_run import FAILED, run_reviewer

CHOICE = ProviderChoice("fake", "fake-opus-1")

FAKE_PROVIDER = '''
import json, sys
mode, model, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
def say(text):
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
if mode == "ok":
    say("thinking")
    say("Review done.\\n```json\\n" + json.dumps({"verdict": "APPROVED_WITH_CONCERNS",
        "findings": [{"finding": "model=" + model, "echo": prompt}]}) + "\\n```")
elif mode == "prose":
    say("looks fine to me")
elif mode == "fail":
    sys.stderr.write("quota exceeded\\n")
    sys.exit(3)
elif mode == "hang":
    import time; time.sleep(30)
'''


@pytest.fixture
def home(tmp_path):
    agents = tmp_path / ".claude" / "plugins" / "coord" / "agents"
    agents.mkdir(parents=True)
    (agents / "rev.md").write_text(
        "---\nname: rev\nmodel: opus\n---\nYou are the DEFINITION-BODY reviewer.\n", encoding="utf-8"
    )
    return tmp_path


def _install_fake(monkeypatch, tmp_path, mode):
    script = tmp_path / "fake_provider.py"
    script.write_text(FAKE_PROVIDER, encoding="utf-8")
    builder = lambda prompt, model: [sys.executable, str(script), mode, model, prompt]  # noqa: E731
    monkeypatch.setitem(reviewer_run._PROVIDERS, "fake", (builder, CodexAdapter))


def test_verdict_and_findings_returned_with_model_and_definition_prompt(monkeypatch, tmp_path, home):
    _install_fake(monkeypatch, tmp_path, "ok")
    out = run_reviewer("coord:rev", "BRIEF-TEXT", CHOICE, home=home)
    assert out["verdict"] == "APPROVED_WITH_CONCERNS"
    assert out["provider"] == "fake" and out["model"] == "fake-opus-1"
    finding = out["findings"][0]
    assert finding["finding"] == "model=fake-opus-1"
    assert "DEFINITION-BODY" in finding["echo"] and "BRIEF-TEXT" in finding["echo"]
    assert "name: rev" not in finding["echo"]


def test_provider_nonzero_exit_is_failed_with_stderr_tail(monkeypatch, tmp_path, home):
    _install_fake(monkeypatch, tmp_path, "fail")
    out = run_reviewer("coord:rev", "b", CHOICE, home=home)
    assert out["verdict"] == FAILED and "quota exceeded" in out["error"]


def test_unparseable_reply_is_failed_not_a_pass(monkeypatch, tmp_path, home):
    _install_fake(monkeypatch, tmp_path, "prose")
    out = run_reviewer("coord:rev", "b", CHOICE, home=home)
    assert out["verdict"] == FAILED and "unparseable" in out["error"]


def test_timeout_is_failed(monkeypatch, tmp_path, home):
    _install_fake(monkeypatch, tmp_path, "hang")
    out = run_reviewer("coord:rev", "b", CHOICE, home=home, timeout=0.5)
    assert out["verdict"] == FAILED and "timed out" in out["error"]


def test_missing_provider_executable_is_failed(monkeypatch, home):
    builder = lambda prompt, model: [str(home / "no-such-cli"), prompt]  # noqa: E731
    monkeypatch.setitem(reviewer_run._PROVIDERS, "fake", (builder, CodexAdapter))
    out = run_reviewer("coord:rev", "b", CHOICE, home=home)
    assert out["verdict"] == FAILED and "could not be started" in out["error"]


def test_unknown_agent_and_unsupported_provider_are_failed(monkeypatch, tmp_path, home):
    _install_fake(monkeypatch, tmp_path, "ok")
    assert run_reviewer("coord:ghost", "b", CHOICE, home=home)["verdict"] == FAILED
    assert run_reviewer("coord:rev", "b", ProviderChoice("grok", "grok-4"), home=home)["verdict"] == FAILED


def test_codex_argv_carries_model_flag_before_prompt(monkeypatch):
    monkeypatch.setattr("coordinator_core.agent_pipe.codex.resolve_codex", lambda: ["codex"])
    assert reviewer_run._codex_argv("P", "gpt-x") == [
        "codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral", "-m", "gpt-x", "P",
    ]


def test_agy_argv_carries_model_flag(monkeypatch):
    argv = reviewer_run._agy_argv("P", "gem-x")
    assert argv[-2:] == ["--model", "gem-x"] and "P" in argv


def test_cli_main_prints_json_line(monkeypatch, tmp_path, home, capsys):
    _install_fake(monkeypatch, tmp_path, "ok")
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("BRIEF"))
    monkeypatch.setattr(
        reviewer_run, "run_reviewer", lambda a, b, c, **k: run_reviewer(a, b, c, home=home)
    )
    assert reviewer_run.main(["coord:rev", "fake", "fake-opus-1"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "APPROVED_WITH_CONCERNS"


def test_verdict_outside_the_allowed_set_is_failed(monkeypatch, tmp_path, home):
    _install_fake(monkeypatch, tmp_path, "ok")
    out = run_reviewer("coord:rev", "B", CHOICE, home=home, verdicts=["SOUND", "BROKEN"])
    assert out["verdict"] == FAILED and "APPROVED_WITH_CONCERNS" in out["error"]


def test_verdict_inside_the_allowed_set_passes(monkeypatch, tmp_path, home):
    _install_fake(monkeypatch, tmp_path, "ok")
    out = run_reviewer("coord:rev", "B", CHOICE, home=home, verdicts=["APPROVED_WITH_CONCERNS"])
    assert out["verdict"] == "APPROVED_WITH_CONCERNS"


def test_agy_delta_whitespace_survives_json_stream():
    deltas = ['{"verdict":"READY","findings":[{"finding":"one', ' ', 'two"}]}']
    stream = "\n".join(json.dumps({"event":"step_update", "step_update": {
        "step_type":"agent_response", "text_delta":delta}}) for delta in deltas)
    reply = reviewer_run._last_message(stream, reviewer_run._AgyMessages())
    assert reviewer_run._verdict_from(reply)["findings"][0]["finding"] == "one two"


def test_nested_verdict_does_not_replace_outer_reply():
    reply = {"verdict":"FAIL", "findings":[{"verdict":"OK", "finding":"nested evidence"}]}
    assert reviewer_run._verdict_from(json.dumps(reply)) == reply


@pytest.mark.parametrize("reply", [{"verdict":"OK"}, {"verdict":"OK", "findings":"omitted"},
                                   {"verdict":"OK", "findings":["unstructured"]}])
def test_malformed_findings_fail_closed(monkeypatch, tmp_path, home, reply):
    script = tmp_path / "malformed_provider.py"
    script.write_text("import json\nprint(json.dumps({'type':'item.completed','item':"
                      + repr({"type":"agent_message", "text":json.dumps(reply)}) + "}))\n")
    monkeypatch.setitem(reviewer_run._PROVIDERS, "fake",
        (lambda prompt, model: [sys.executable, str(script)], CodexAdapter))
    out = run_reviewer("coord:rev", "brief", CHOICE, home=home)
    assert out["verdict"] == FAILED and "findings array" in out["error"]


def test_array_wrapped_verdict_is_not_a_top_level_result():
    assert reviewer_run._verdict_from('[{"verdict":"OK","findings":[]}]') is None


@pytest.mark.parametrize("provider,secret", [("codex","OPENAI_API_KEY"), ("agy","GEMINI_API_KEY"), ("grok","XAI_API_KEY")])
def test_named_review_uses_local_account_env_not_ambient_paid_key(monkeypatch, provider, secret):
    monkeypatch.setenv(secret, "do-not-forward")
    monkeypatch.delenv("CODEX_RUNNER_ALLOW_API_KEY", raising=False)
    assert secret not in reviewer_run._provider_env(provider)
