"""`--plan <path>` on workstream-complete-assemble brief/apply feeds
`decisions.governing_plan_path`; the no-governing-plan point names the flag."""
from __future__ import annotations

import json

from coordinator_core import workstream_complete as wsc
from coordinator_core.workstream_complete import apply as wsc_apply
from coordinator_core.workstream_complete.judgments import build_no_governing_plan_judgment_point


def test_take_plan_flag_both_spellings_and_missing_value():
    assert wsc.take_plan_flag(["--plan", "p.md", "--decisions", "{}"]) == (["--decisions", "{}"], "p.md", None)
    assert wsc.take_plan_flag(["--plan=p.md"]) == ([], "p.md", None)
    assert wsc.take_plan_flag(["--plan"])[2] is not None


def test_merge_plan_flag_never_overrides_decisions():
    assert wsc.merge_plan_flag(None, "p.md") == {"governing_plan_path": "p.md"}
    assert wsc.merge_plan_flag({"governing_plan_path": "x.md"}, "p.md") == {"governing_plan_path": "x.md"}
    assert wsc.merge_plan_flag({"a": 1}, None) == {"a": 1}


def test_brief_cli_passes_plan_into_decisions(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(wsc, "brief", lambda decisions, *a, **k: seen.update(decisions) or {})
    assert wsc.main(["brief", "--plan", "docs/plans/p.md"]) == wsc.EXIT_OK
    assert seen == {"governing_plan_path": "docs/plans/p.md"}
    capsys.readouterr()


def test_brief_cli_rejects_plan_without_value(capsys):
    assert wsc.main(["brief", "--plan"]) == wsc.EXIT_USAGE


def test_apply_cli_passes_plan_into_decisions(monkeypatch, capsys):
    seen = {}

    def fake_apply(decisions=None):
        seen.update(decisions or {})
        return 0, {}

    monkeypatch.setattr(wsc_apply, "apply", fake_apply)
    assert wsc_apply.main(["--plan", "docs/plans/p.md"]) == 0
    assert seen == {"governing_plan_path": "docs/plans/p.md"}
    json.loads(capsys.readouterr().out)


def test_no_governing_plan_evidence_names_the_flag():
    assert "--plan" in build_no_governing_plan_judgment_point()["evidence"]
