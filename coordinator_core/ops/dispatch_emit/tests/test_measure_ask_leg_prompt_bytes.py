"""The per-leg prompt-bytes probe counts hoisted fragments, called helpers and staged files, not the literal prefix."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
_SPEC = importlib.util.spec_from_file_location("measure_ask_leg_prompt_bytes", _REPO / "scripts" / "measure_ask_leg_prompt_bytes.py")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)
# Resolved at import, before the per-test home quarantine hides the real plugin install.
_PLUGIN_ROOT = probe.resolve_plugin_root()

_SCRIPT = """\
function helper(x) {
  return 'twelve bytes' + x;
}
const a = await agent('ab' + helper(1) + ' tail', { label: 'one', agentType: 'coordinator:op-relay', effort: 'low' });
const b = await agent('Read run/prompts/big.md in full.' + dyn, { label: 'two', model: 'opus' });
const c = await agent(dyn, { label: 'three', agentType: 'coordinator:git-commit-agent' });
"""


def test_literals_helpers_and_staged_files_are_counted_and_options_are_not():
    staged = {"big": "x" * 500}
    out = probe.measure_script(_SCRIPT, staged)
    one, two, three = out["legs"]
    assert (one["label"], one["model"], one["effort"]) == ("one", "haiku", "low")
    assert one["prompt_bytes"] == len("ab") + len(" tail") + len("twelve bytes")
    assert (two["model"], two["staged_bytes"]) == ("opus", 500)
    assert two["prompt_bytes"] == len("Read run/prompts/big.md in full.") + 500
    assert (three["prompt_bytes"], three["model"]) == (0, "haiku")
    assert out["total_prompt_bytes"] == sum(r["prompt_bytes"] for r in out["legs"])


def test_a_missing_staged_body_counts_zero_and_unicode_counts_utf8_bytes():
    out = probe.measure_script("agent('é\\n' + 'Read r/prompts/gone.md.', { label: 'u' });", {})
    assert out["legs"][0]["prompt_bytes"] == len("é\n".encode()) + len("Read r/prompts/gone.md.")
    assert out["legs"][0]["staged_bytes"] == 0


def test_template_static_text_counts_and_its_interpolation_does_not():
    out = probe.measure_script("agent(`abc${someValue}def`, { label: 't' });", {})
    assert out["legs"][0]["prompt_bytes"] == len("abcdef")


def test_named_legs_are_summed_by_label_prefix():
    script = "agent('aaaa', { label: 'late-rows-read' }); agent('bb', { label: 'plan' }); agent('c', { label: 'halt-reverify' });"
    named = probe.measure_script(script, {})["named_legs"]
    assert named["labels"] == ["late-rows-read", "halt-reverify"]
    assert named["summed_prompt_bytes"] == 5


def test_the_default_ask_reports_staged_files_and_more_than_the_literal_prefix(capsys, monkeypatch):
    assert _PLUGIN_ROOT, "plugin root did not resolve at import; plan-blitz.mjs is unreachable"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    assert probe.main() == 0
    out = json.loads(capsys.readouterr().out)
    by_label = {r["label"]: r for r in out["legs"]}
    for key in ("label", "agent_type", "model", "effort", "prompt_bytes"):
        assert all(key in r for r in out["legs"])
    assert by_label["late-rows-read"]["agent_type"] == "coordinator:op-relay"
    assert by_label["late-rows-read"]["prompt_bytes"] > 1000
    assert by_label["size"]["staged_bytes"] > 10000
    assert out["named_legs"]["summed_prompt_bytes"] > 0
    assert out["total_prompt_bytes"] == sum(r["prompt_bytes"] for r in out["legs"])


@pytest.mark.parametrize("args,expected", [("(a, b)", "a"), ("(f(1, 2), {x: 1})", "f(1, 2)"), ("(solo)", "solo")])
def test_first_argument_stops_at_the_top_level_comma(args, expected):
    assert probe.first_argument(args, args) == expected
