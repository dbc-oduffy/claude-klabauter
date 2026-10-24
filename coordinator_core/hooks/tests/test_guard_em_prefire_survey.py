"""guard_em_prefire_survey.evaluate: budgets, windows, disarms, arming, fail-open, zero spawn."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core import machine_profile, warp_fire
from coordinator_core.hooks import guard_em_prefire_survey as g
from coordinator_core.session import engine_route_state as ers

SID = "sess-1"


def _line(rec: dict) -> str:
    return json.dumps(rec, separators=(",", ":")) + "\n"


def user(text="go") -> str:
    return _line({"type": "user", "message": {"role": "user", "content": text}})


def tool_result() -> str:
    return _line({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "q", "content": "ok"}]}})


def use(tid, name="Read", inp=None) -> str:
    inp = inp if inp is not None else {"file_path": f"f{tid}.py"}
    return _line({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})


class Env:
    def __init__(self, tmp_path):
        self.repo = tmp_path / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.home = tmp_path / "sh"
        self.transcript = tmp_path / "t.jsonl"
        self.set_marker()
        self.lines: list[str] = []

    def set_marker(self, on=True):
        d = self.repo / ".git" / "coordinator-sessions" / SID
        d.mkdir(parents=True, exist_ok=True)
        marker = d / "em-role-asserted"
        if on:
            marker.write_text("1", encoding="utf-8")
        elif marker.exists():
            marker.unlink()

    def write(self, *lines):
        self.lines.extend(lines)
        self.transcript.write_text("".join(self.lines), encoding="utf-8")

    def survey(self, n, name="Read", start=0):
        self.write(*[use(f"c{start + i}", name, {"file_path": f"f{start + i}.py"} if name == "Read" else {"command": f"ls {start + i}"}) for i in range(n)])

    def payload(self, tid="cur", name="Read", inp=None, **extra):
        p = {
            "session_id": SID,
            "transcript_path": str(self.transcript),
            "cwd": str(self.repo),
            "tool_name": name,
            "tool_input": inp if inp is not None else ({"file_path": "cur.py"} if name == "Read" else {"command": "ls cur"}),
            "tool_use_id": tid,
            "env": {"COORDINATOR_SETTINGS_HOME": str(self.home)},
        }
        p.update(extra)
        return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(ers.PHASE_UNFIRED))

    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    return Env(tmp_path)


def reason(res) -> str:
    text = res["hookSpecificOutput"]["permissionDecisionReason"]
    marker = "[coordinator] "
    return text[len(marker):] if text.startswith(marker) else text


def test_constants():
    assert g.PREFIRE_EXPLORATORY_CALLS_MAX == 8 and g.POSTLANDING_VERIFY_CALLS_MAX == 3
    assert g.ARMING == "role-marker"


def test_deny_texts_name_the_constant_their_window_enforces():
    pre = g.prefire_deny_text(9, g.PREFIRE_EXPLORATORY_CALLS_MAX)
    post = g.postlanding_deny_text(4, g.POSTLANDING_VERIFY_CALLS_MAX)
    assert f"budget {g.PREFIRE_EXPLORATORY_CALLS_MAX} (" in pre and "since the last turn" in pre
    assert f"budget {g.POSTLANDING_VERIFY_CALLS_MAX} (" in post and "since the landing" in post


def test_markerless_session_is_never_denied(env):
    env.set_marker(False)
    env.write(user())
    env.survey(12)
    assert g.evaluate(env.payload()) is None
    env.set_marker(True)
    assert g.evaluate(env.payload(tid="cur2")) is not None


def _holder(monkeypatch, *, sid=SID, status="verified", live=True, raises=False):
    from coordinator_core.group_em import nomination

    def read_record(root, directory=None):
        if raises:
            raise OSError("unreadable")
        return None if sid is None else {"session_id": sid, "repo_root": root}

    calls = []

    def entry_status(rec, *a, **k):
        calls.append("entry_status")
        return {"status": status}

    def is_live(rec):
        calls.append("is_live")
        return nomination.LivenessResult(live, "x")

    monkeypatch.setattr(nomination, "read_record", read_record)
    monkeypatch.setattr(nomination, "entry_status", entry_status)
    monkeypatch.setattr(nomination, "is_live", is_live)
    return calls


def test_live_verified_holder_is_exempt(env, monkeypatch):
    _holder(monkeypatch)
    env.write(user())
    env.survey(12)
    assert g.evaluate(env.payload()) is None


@pytest.mark.parametrize(
    "kw",
    [
        {"sid": "other-sid"},
        {"sid": None},
        {"status": "pending"},
        {"live": False},
        {"raises": True},
    ],
)
def test_non_holder_stale_or_unreadable_record_still_denies(env, monkeypatch, kw):
    _holder(monkeypatch, **kw)
    env.write(user())
    env.survey(12)
    assert g.evaluate(env.payload()) is not None


def test_holder_checks_skipped_for_a_different_session(env, monkeypatch):
    calls = _holder(monkeypatch, sid="other-sid")
    env.write(user())
    env.survey(12)
    assert g.evaluate(env.payload()) is not None
    assert calls == []


def test_ninth_read_denies_with_fire_step(env):
    env.write(user())
    env.survey(8)
    res = g.evaluate(env.payload())
    assert res is not None and res["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert reason(res).startswith("[em-prefire-survey] 9 exploratory calls")
    assert warp_fire.fire_step() in reason(res)
    assert "cd-prefixed retry" in reason(res) and "em_context" in reason(res)


def test_eighth_read_allows(env):
    env.write(user())
    env.survey(7)
    assert g.evaluate(env.payload()) is None


def test_ninth_bash_denies_and_eighth_allows(env):
    env.write(user())
    env.survey(7, "Bash")
    assert g.evaluate(env.payload(name="Bash")) is None
    env.survey(1, "Bash", start=7)
    assert g.evaluate(env.payload(tid="cur2", name="Bash")) is not None


def test_current_call_already_in_transcript_counts_once(env):
    env.write(user())
    env.survey(7)
    env.write(use("cur"))
    assert g.evaluate(env.payload()) is None
    env.write(use("c99"))
    assert g.evaluate(env.payload(tid="c99")) is not None


def test_under_budget_never_probes_phase(env, monkeypatch):
    def probe(*a, **k):
        raise AssertionError("phase probed")

    monkeypatch.setattr(ers, "session_route_phase", probe)
    env.write(user())
    env.survey(3)
    assert g.evaluate(env.payload()) is None


def test_user_record_resets_but_tool_result_does_not(env):
    env.write(user())
    env.survey(8)
    env.write(tool_result())
    assert g.evaluate(env.payload()) is not None
    env.write(user("new turn"))
    assert g.evaluate(env.payload(tid="cur3")) is None


def test_exempt_paths_not_counted(env):
    env.write(user("read docs/a.md for me"))
    env.survey(8)
    p = env.payload(inp={"file_path": "docs/a.md"})
    assert g.evaluate(p) is None
    assert g.evaluate(env.payload(tid="w", inp={"file_path": "/r/state/warp/x/requirements.md"})) is None
    assert g.evaluate(env.payload(tid="i", inp={"file_path": "/r/.coordinator-local/n.md"})) is None
    assert g.evaluate(env.payload(tid="m", inp={"file_path": "/r/state/cross-repo/inbox/m.md"})) is None
    assert g.evaluate(env.payload(tid="n", inp={"file_path": "elsewhere.py"})) is not None


def test_settings_bin_cli_bash_exempt(env):
    env.write(user())
    env.survey(8)
    cli = env.payload(name="Bash", inp={"command": f"{env.home}/bin/coordinator-doc-new --type x"})
    assert g.evaluate(cli) is None
    assert g.evaluate(env.payload(name="Bash", tid="b2", inp={"command": "cat x"})) is not None


LIFECYCLE_CMDS = [
    "python -m coordinator_core.invoke sizing.supersede '{\"sizing_path\": \"state/sizings/a b.yaml\"}'",
    "coordinator-invoke memo.transition '{\"action\": \"claim\"}'",
    "pickup-assemble --fresh",
    "/opt/bin/cross-repo-memo --to coordinator-content-repo",
    "python3 -m coordinator_core.invoke workflow.revise --run r",
]
MENTION_CMDS = [
    "grep -rn sizing.supersede coordinator_core",
    "cat pickup-assemble",
    "echo memo.transition",
    "ls cross-repo-memo",
    "python -m coordinator_core.invoke sizing.supersede x; cat y",
    "coordinator-invoke memo.transition x $(cat y)",
    "git log --grep workflow.revise",
]


@pytest.mark.parametrize("cmd", LIFECYCLE_CMDS)
def test_lifecycle_invocation_counts_toward_no_prefire_budget(env, cmd):
    env.write(user())
    env.survey(7)
    assert g.evaluate(env.payload(name="Bash", tid="l1", inp={"command": cmd})) is None
    env.write(use("l1", "Bash", {"command": cmd}))
    assert g.evaluate(env.payload(name="Bash", tid="b9", inp={"command": "cat x"})) is None
    env.write(use("b9", "Bash", {"command": "cat x"}))
    assert g.evaluate(env.payload(name="Bash", tid="b10", inp={"command": "cat y"})) is not None


@pytest.mark.parametrize("cmd", MENTION_CMDS)
def test_command_merely_mentioning_a_verb_still_counts(env, cmd):
    env.write(user())
    env.survey(8)
    assert g.evaluate(env.payload(name="Bash", tid="m1", inp={"command": cmd})) is not None


@pytest.mark.parametrize("cmd", LIFECYCLE_CMDS[:4])
def test_lifecycle_invocation_not_counted_postlanding(env, monkeypatch, cmd):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    for i in range(1, 4):
        env.write(use(f"p{i}"))
        assert g.evaluate(env.payload(tid=f"p{i}")) is None
    life = {"command": cmd}
    assert g.evaluate(env.payload(tid="l1", name="Bash", inp=life)) is None
    env.write(use("l1", "Bash", life))
    assert g.evaluate(env.payload(tid="p4")) is not None


def test_mention_of_revise_does_not_disarm_postlanding(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    assert g.evaluate(env.payload(tid="x0")) is None
    mention = {"command": "grep workflow.revise x"}
    env.write(use("g1", "Bash", mention), use("p1"), use("p2"), use("p3"))
    assert g.evaluate(env.payload(tid="p4")) is not None


def test_engine_ask_fire_disarms(env):
    env.write(user())
    env.survey(8)
    env.write(use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    assert g.evaluate(env.payload()) is None


def test_allowlisted_skill_disarms_but_other_skills_do_not(env):
    env.write(user())
    env.survey(8)
    env.write(use("s1", "Skill", {"skill": "coordinator:plan"}))
    assert g.evaluate(env.payload()) is not None
    env.write(use("s2", "Skill", {"skill": "coordinator:pickup"}))
    assert g.evaluate(env.payload(tid="cur2")) is None


def test_open_xs_lane_does_not_exempt(env):
    from coordinator_core.session import xs_lane_budget

    env.write(user())
    env.survey(8)
    assert g.evaluate(env.payload()) is not None
    assert xs_lane_budget.open_xs_lane(env.repo, SID, "abc")
    assert g.evaluate(env.payload(tid="cur2")) is not None


@pytest.mark.parametrize("phase", [ers.PHASE_UNKNOWN, ers.PHASE_IN_FLIGHT])
def test_other_phases_allow_prefire(env, monkeypatch, phase):
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(phase, "r1"))
    env.write(user())
    env.survey(10)
    assert g.evaluate(env.payload()) is None


def test_phase_probe_raising_fails_open(env, monkeypatch):
    def probe(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(ers, "session_route_phase", probe)
    env.write(user())
    env.survey(10)
    assert g.evaluate(env.payload()) is None


def test_fail_open_cases(env):
    env.write(user())
    env.survey(10)
    assert g.evaluate(env.payload(agent_id="sub-1")) is None
    assert g.evaluate(env.payload(env={"COORDINATOR_HOOK_EM_PREFIRE_SURVEY_DISABLED": "1", "COORDINATOR_SETTINGS_HOME": str(env.home)})) is None
    assert g.evaluate(env.payload(transcript_path=str(env.transcript) + ".missing")) is None
    assert g.evaluate(env.payload(transcript_path="")) is None
    assert g.evaluate(env.payload(name="Edit")) is None
    assert g.evaluate("not a dict") is None
    assert g.evaluate(env.payload()) is not None
    assert g.evaluate(env.payload(agent_id="sub-1")) is None


def test_subagent_returns_before_any_io(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("io")

    monkeypatch.setattr("coordinator_core.session.em_prefire_window.load_state", boom)
    assert g.evaluate(env.payload(agent_id="sub-1")) is None


def test_strict_only_arming(env, monkeypatch):
    monkeypatch.setattr(g, "ARMING", "strict-only")
    env.write(user())
    env.survey(10)
    assert g.evaluate(env.payload()) is None
    assert g.evaluate(env.payload(env={"COORDINATOR_STRICT_DISPATCH": "1", "COORDINATOR_SETTINGS_HOME": str(env.home)})) is not None


def _landed(monkeypatch, phase=ers.PHASE_LANDED_GREEN):
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(phase, "run-1"))


def test_postlanding_fourth_call_denies(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    assert g.evaluate(env.payload(tid="p1")) is None
    env.write(use("p1"))
    env.write(use("p2"), use("p3"))
    assert g.evaluate(env.payload(tid="p3")) is None
    env.write(use("p4"))
    res = g.evaluate(env.payload(tid="p4"))
    assert res is not None
    assert reason(res).startswith("[em-prefire-survey] 4 exploratory calls since the landing")
    assert "workflow.revise" in reason(res)


def test_postlanding_halted_denies_too(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch, ers.PHASE_HALTED)
    for i in range(1, 4):
        env.write(use(f"p{i}"))
        assert g.evaluate(env.payload(tid=f"p{i}")) is None
    env.write(use("p4"))
    assert g.evaluate(env.payload(tid="p4")) is not None


def test_postlanding_ignores_settings_cli_exemption(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    assert g.evaluate(env.payload(tid="x0")) is None
    cli = {"command": f"{env.home}/bin/cli"}
    for i in range(1, 3):
        env.write(use(f"b{i}", "Bash", cli))
        assert g.evaluate(env.payload(tid=f"b{i}", name="Bash", inp=cli)) is None
    env.write(use("b3", "Bash", cli))
    assert g.evaluate(env.payload(tid="b3", name="Bash", inp=cli)) is not None


def test_workflow_revise_disarms_postlanding(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    assert g.evaluate(env.payload(tid="x0")) is None
    for i in range(1, 6):
        env.write(use(f"p{i}"))
    assert g.evaluate(env.payload(tid="p5")) is not None
    rev = {"command": "python -m coordinator_core.invoke workflow.revise --run r"}
    assert g.evaluate(env.payload(tid="r1", name="Bash", inp=rev)) is None
    env.write(use("r1", "Bash", rev), use("p6"))
    assert g.evaluate(env.payload(tid="p6")) is None


def test_new_engine_ask_fire_disarms_postlanding(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    assert g.evaluate(env.payload(tid="x0")) is None
    env.write(*[use(f"p{i}") for i in range(1, 6)], use("f2", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    assert g.evaluate(env.payload(tid="p9")) is None


def test_new_user_turn_after_landing_never_reopens_prefire(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    env.write(user("next turn"))
    env.survey(10)
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(ers.PHASE_UNFIRED))
    assert g.evaluate(env.payload()) is None


def test_deny_passes_through_guard_level(env, monkeypatch):
    seen = []
    monkeypatch.setattr(g, "apply_guard_level", lambda name, envelope: seen.append(name) or None)
    env.write(user())
    env.survey(10)
    assert g.evaluate(env.payload()) is None
    assert seen == ["guard-em-prefire-survey"]
    assert "guard-em-prefire-survey" not in machine_profile.FLOOR_GUARDS


def test_handler_returns_empty_without_deny(env):
    import asyncio

    env.write(user())
    assert asyncio.run(g._handler(env.payload())) == {}


PUBLISH_SEQUENCE = [
    "python3 -m pytest coordinator_core/ops/dispatch_emit/tests/test_land_run.py -q",
    "t=$(git merge-tree --write-tree aaa bbb) && c=$(git commit-tree $t -p aaa -m x)",
    "git push candidate-source-b abc123:refs/heads/x",
    "cd scratch/claude-klabauter-pub && git fetch origin && git checkout -q abc123",
]
PERCOLATE = "python3 coordinator/bin/percolate-round.py claude-klabauter --source-ref abc123 --invocation-authorized"


def test_publish_sequence_after_landing_is_allowed(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    for i, cmd in enumerate(PUBLISH_SEQUENCE):
        inp = {"command": cmd}
        assert g.evaluate(env.payload(tid=f"s{i}", name="Bash", inp=inp)) is None
        env.write(use(f"s{i}", "Bash", inp))
    grep = {"command": "grep -n percolate-round transcript.jsonl"}
    env.write(use("g1", "Bash", grep))
    assert g.evaluate(env.payload(tid="g1", name="Bash", inp=grep)) is None
    assert g.evaluate(env.payload(tid="pr", name="Bash", inp={"command": PERCOLATE})) is None


def test_second_test_run_and_plain_reads_still_count_postlanding(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    assert g.evaluate(env.payload(tid="r0")) is None
    t = {"command": PUBLISH_SEQUENCE[0]}
    for i in range(2):
        env.write(use(f"t{i}", "Bash", t))
    env.write(use("r1"), use("r2"))
    assert g.evaluate(env.payload(tid="r3")) is not None
    log = {"command": "git log --oneline -5"}
    assert g.evaluate(env.payload(tid="x", name="Bash", inp=log)) is not None
