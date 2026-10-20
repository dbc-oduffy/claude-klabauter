"""em_only_act: a row whose only act is an agent-forbidden command is em-performed."""

from __future__ import annotations

from coordinator_core.bash_guards.guard_subagent_heavy_ue_launch import launches_heavy
from coordinator_core.ops.dispatch_emit.em_only_act import forbidden_sole_act, row_is_em_performed

CMD = "pwsh -NoProfile -File scripts/build-plugin.ps1 -EngineVersion 5.8 -Plugins X"


def _row(body, **kw):
    raw = {"id": "B1", "title": "Build plugin", "change_kind": "verification",
           "agent_type": "coordinator:executor", "body": body}
    raw.update(kw)
    return raw


def test_example_game_repo_b1_row():
    raw = _row("Run the build:\n\n`%s`\n" % CMD, writes=[])
    assert forbidden_sole_act(raw) == CMD
    assert row_is_em_performed(raw) is True


def test_windows_backslash_form():
    raw = _row("`pwsh -File scripts\\build-plugin.ps1 -EngineVersion 5.8`", writes=[])
    assert forbidden_sole_act(raw) is not None


def test_fenced_posix_runuat():
    raw = _row("```\nRunUAT.sh BuildPlugin -Plugin=X\n```\n", writes=[])
    assert forbidden_sole_act(raw) == "RunUAT.sh BuildPlugin -Plugin=X"


def test_unrealbuildtool_exe_form():
    raw = _row('`& "Engine\\Build\\UnrealBuildTool.exe" Target Win64`', writes=[])
    assert forbidden_sole_act(raw) is not None


def test_write_act_keeps_agent():
    assert forbidden_sole_act(_row("`%s`" % CMD, writes=["a.cpp"])) is None
    assert row_is_em_performed(_row("`%s`" % CMD, writes=["a.cpp"])) is False


def test_writes_absent_is_unknown():
    assert forbidden_sole_act(_row("`%s`" % CMD)) is None


def test_writes_under_and_appends_keep_agent():
    assert forbidden_sole_act(_row("`%s`" % CMD, writes=[], writes_under=["src/"])) is None
    assert forbidden_sole_act(_row("`%s`" % CMD, writes=[], appends=["log.md"])) is None


def test_prose_mention_is_not_a_run():
    assert forbidden_sole_act(_row("Run scripts/build-plugin.ps1 later.", writes=[])) is None


def test_grep_mention_in_span_is_not_a_run():
    assert forbidden_sole_act(_row("`rg build-plugin.ps1 docs`", writes=[])) is None


def test_declared_performer_em():
    assert row_is_em_performed({"performer": "em", "body": "nothing"}) is True
    assert row_is_em_performed({"performer": "em"}) is True


def test_non_str_body():
    assert forbidden_sole_act({"writes": [], "body": None}) is None


def test_launches_heavy_prefilter_path():
    assert launches_heavy("git log -- build-plugin.ps1") is False
    assert launches_heavy("cat x") is False
    assert launches_heavy(CMD) is True


def test_powershell_backtick_continuation_in_fence():
    body = "```powershell\npwsh -File scripts/build-plugin.ps1 `\n  -EngineVersion 5.8\n```\n"
    span = forbidden_sole_act(_row(body, writes=[]))
    assert span is not None and "build-plugin.ps1" in span


def test_blocked_on_em_only_capability_is_em_performed():
    for cap in ("Workflow tool", "the workflow tool (EM session only)", "Agent tool", "SendMessage"):
        raw = _row("Do it.", writes=["a.py"], blocked_on={"capability": cap, "probe": "x"})
        assert row_is_em_performed(raw) is True, cap


def test_blocked_on_an_ordinary_capability_stays_agent():
    raw = _row("Do it.", writes=["a.py"], blocked_on={"capability": "docker daemon", "probe": "docker ps"})
    assert row_is_em_performed(raw) is False
    assert row_is_em_performed(_row("Do it.", writes=["a.py"], blocked_on="Workflow")) is False
