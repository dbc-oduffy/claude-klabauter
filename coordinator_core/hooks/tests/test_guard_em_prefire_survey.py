"""guard_em_prefire_survey.evaluate: budgets, windows, disarms, arming, fail-open, zero spawn."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core import machine_profile, warp_fire
from coordinator_core.hooks import guard_em_prefire_survey as g
from coordinator_core.session import em_prefire_window as epw
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
    text = res["hookSpecificOutput"]["additionalContext"]
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
    assert res is not None and "permissionDecision" not in res["hookSpecificOutput"]
    assert reason(res).startswith("[em-prefire-survey] 9 exploratory calls")
    assert warp_fire.fire_step() in reason(res)
    assert "cd-prefixed retry" in reason(res) and "em_context" in reason(res)


def test_prefire_deny_arms_the_session_latch_and_under_budget_does_not(env):
    from coordinator_core.hooks._em_write_budget import advised_deny_envelope

    (env.repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (env.repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (env.repo / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    env.write(user())
    env.survey(7)
    assert g.evaluate(env.payload()) is None
    assert advised_deny_envelope(str(env.repo), SID) is None
    env.survey(1, start=7)
    assert g.evaluate(env.payload(tid="c8")) is not None
    assert advised_deny_envelope(str(env.repo), SID) is not None
STAMP = "[G-EM fifa-ingest a73557ab] ingest the feed"


def test_gem_stamped_opening_is_never_denied_prefire(env):
    env.write(user(STAMP))
    env.survey(12)
    assert g.evaluate(env.payload()) is None


def test_unstamped_or_malformed_stamp_stays_armed(env):
    for text in ("ingest the feed", "[G-EM fifa-ingest] x", "[G-EM fifa-ingest zz] x"):
        env.lines = []
        env.write(user(text))
        env.survey(8)
        assert g.evaluate(env.payload()) is not None, text


def test_gem_stamped_opening_is_never_denied_postlanding(env, monkeypatch):
    env.write(user(STAMP), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    for t in ("p1", "p2", "p3", "p4", "p5"):
        assert _step(env, t) is None


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


RELEASE_SEQUENCE = [
    "git -C /r/scratch/mirror rev-parse HEAD",
    "coordinator-invoke dispatch.record_halted_run_landed --describe",
    "session-claim-cli release-artifact artifact state/warp/x/halted.json",
    "coordinator-invoke dispatch.record_halted_run_landed --params-file /r/scratch/p.json",
]


def test_release_sequence_after_halt_receipt_never_denies(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch, ers.PHASE_HALTED)
    for i, cmd in enumerate(RELEASE_SEQUENCE):
        call = {"command": cmd}
        assert g.evaluate(env.payload(tid=f"r{i}", name="Bash", inp=call)) is None
        env.write(use(f"r{i}", "Bash", call))
    assert g.evaluate(env.payload(tid="p1")) is None


@pytest.mark.parametrize("op", ["sizing.resize", "dispatch.terminal_commit", "workflow.revise", "dispatch.some_new_receipt_verb_not_in_map"])
def test_describe_of_any_op_is_exempt_but_never_a_revise(op):
    from coordinator_core.session import em_prefire_window as w

    assert w.lifecycle_op(f"coordinator-invoke {op} --describe") == op + w.DESCRIBE_SUFFIX


def test_exempt_set_derives_from_registry_and_still_counts_reads():
    from coordinator_core.session import em_prefire_window as w

    ops = w.lifecycle_ops()
    assert {"dispatch.record_halted_run_landed", "sizing.resize", "sizing.supersede", "workflow.revise", "dispatch.terminal_commit"} <= ops
    assert w.lifecycle_op("coordinator-invoke dispatch.record_halted_run_landed --params-file p.json") == "dispatch.record_halted_run_landed"
    assert w.lifecycle_op("coordinator-invoke sizing.read_object_fields '{}'") is None
    assert w.lifecycle_op("session-claim-cli release-artifact artifact x") is not None
    assert w.lifecycle_op("session-claim-cli list") is None
    assert w.lifecycle_op("grep -n dispatch.record_halted_run_landed x") is None


def test_mention_of_revise_does_not_disarm_postlanding(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    assert g.evaluate(env.payload(tid="x0")) is None
    mention = {"command": "grep workflow.revise x"}
    _step(env, "g1", "Bash", mention)
    for t in ("p1", "p2", "p3"):
        _step(env, t)
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


def _landed(monkeypatch, phase=ers.PHASE_LANDED_GREEN, known=True):
    """Landed phase; `known` stubs a run footprint covering every default `use()` read, else the manifest is absent."""
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(phase, "run-1"))
    if known:
        names = frozenset(
            ["cur.py", "cur"] + [f"f{c}{i}.py" for c in "abcdefghijklmnopqrstuvwxyz" for i in range(10)]
        )
        monkeypatch.setattr(epw, "run_footprint", lambda *a, **k: names)


def _step(env, tid, name="Read", inp=None):
    """One call as production sees it: the hook evaluates it, then it lands in the transcript."""
    res = g.evaluate(env.payload(tid=tid, name=name, inp=inp))
    env.write(use(tid, name, inp))
    return res


def test_postlanding_fourth_call_denies(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    for t in ("p1", "p2", "p3"):
        assert _step(env, t) is None
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
    monkeypatch.setattr(epw, "run_footprint", lambda *a, **k: frozenset([cli["command"]]))
    for i in range(1, 3):
        env.write(use(f"b{i}", "Bash", cli))
        assert g.evaluate(env.payload(tid=f"b{i}", name="Bash", inp=cli)) is None
    env.write(use("b3", "Bash", cli))
    assert g.evaluate(env.payload(tid="b3", name="Bash", inp=cli)) is not None


def test_workflow_revise_disarms_postlanding(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    assert g.evaluate(env.payload(tid="x0")) is None
    for i in range(1, 5):
        _step(env, f"p{i}")
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


def test_survey_is_guide_never_deny_and_off_the_floor(env):
    env.write(user())
    env.survey(10)
    res = g.evaluate(env.payload())
    assert res is not None and "permissionDecision" not in res["hookSpecificOutput"]
    assert "guard-em-prefire-survey" not in machine_profile.FLOOR_GUARDS


def test_after_engine_ask_fire_verify_calls_are_never_denied(env, monkeypatch):
    """git log, a plain Read, a read-only pytest re-run, workflow.revise that also wrote its params
    file, and a commit-tree/update-ref landing correction: none is ever a deny."""
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(ers.PHASE_LANDED_GREEN, "run1"))
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    calls = [
        ("Bash", {"command": "git log --oneline -5"}),
        ("Read", {"file_path": "x.py"}),
        ("Bash", {"command": "python -m pytest scratch/flake_a.py -q"}),
        ("Bash", {"command": "echo '{}' > scratch/p.json && coordinator-invoke workflow.revise --params-file scratch/p.json"}),
        ("Bash", {"command": "git commit-tree HEAD^{tree} -p HEAD -m fix && git update-ref refs/heads/x HEAD"}),
    ] * 4
    for i, (name, inp) in enumerate(calls):
        res = g.evaluate(env.payload(tid=f"v{i}", name=name, inp=inp))
        assert res is None or "permissionDecision" not in res["hookSpecificOutput"]


def test_handler_returns_empty_without_deny(env):
    import asyncio

    env.write(user())
    assert asyncio.run(g._handler(env.payload())) == {}


def result(tid, text) -> str:
    return _line({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}})


APPLY_OUT = "scratch/wsc-apply.json"
BRIEF_CMD = {"command": "workstream-complete-assemble brief --baton ddf015"}
LEDGER_CMD = {"command": "coordinator-invoke handoff.append_session_ledger '{\"entry\": \"x\"}'"}
APPLY_CMD = {"command": "workstream-complete-assemble apply"}


def _close_sequence(env):
    """coordinator-content-repo-43's close: 3 unrelated reads, then the ceremony calls, its output read and parsed."""
    env.write(user("close the workstream"))
    env.survey(3)
    for i, cmd in enumerate((BRIEF_CMD, BRIEF_CMD, LEDGER_CMD, LEDGER_CMD)):
        assert g.evaluate(env.payload(tid=f"k{i}", name="Bash", inp=cmd)) is None
        env.write(use(f"k{i}", "Bash", cmd), result(f"k{i}", "ok"))
    assert g.evaluate(env.payload(tid="ap", name="Bash", inp=APPLY_CMD)) is None
    env.write(use("ap", "Bash", APPLY_CMD), result("ap", f"exit 4: wrote {APPLY_OUT} (see apply output)"))
    read = {"file_path": f"/r/{APPLY_OUT}"}
    assert g.evaluate(env.payload(tid="rd", inp=read)) is None
    env.write(use("rd", "Read", read))
    parse = {"command": f"jq .status {APPLY_OUT}"}
    assert g.evaluate(env.payload(tid="ps", name="Bash", inp=parse)) is None
    env.write(use("ps", "Bash", parse))


def test_doe_close_sequence_is_uncounted_and_the_ninth_counted_call_denies(env):
    _close_sequence(env)
    # Without the exemptions this tail is the 11th counted call; with them only 3 reads count.
    env.survey(5, start=10)
    assert g.evaluate(env.payload(tid="after")) is not None


def test_unrelated_read_after_apply_still_counts_and_denies(env):
    _close_sequence(env)
    other = {"file_path": "/r/scratch/other.json"}
    env.survey(4, start=10)
    assert g.evaluate(env.payload(tid="o1", inp=other)) is None
    env.write(use("o1", "Read", other))
    res = g.evaluate(env.payload(tid="o2", inp={"file_path": "/r/scratch/third.json"}))
    assert res is not None and reason(res).startswith("[em-prefire-survey] 9 exploratory calls")


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
        _step(env, f"t{i}", "Bash", t)
    _step(env, "r1")
    _step(env, "r2")
    assert g.evaluate(env.payload(tid="r3")) is not None
    log = {"command": "git log --oneline -5"}
    assert g.evaluate(env.payload(tid="x", name="Bash", inp=log)) is not None


def _manifest(env, run_id="run-1"):
    d = env.repo / "state" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({"rows": [{"writes": ["pkg/mod.py", "pkg/tests/test_mod.py"]}], "doc_writes": ["docs/note.md"]}),
        encoding="utf-8",
    )


def _fire_and_land(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch, known=False)
    _manifest(env)


def test_unrelated_work_after_landing_is_never_charged(env, monkeypatch):
    _fire_and_land(env, monkeypatch)
    test = {"command": "python -m pytest pkg/tests/test_mod.py -q"}
    assert _step(env, "t1", "Bash", test) is None
    for i in range(3):
        assert _step(env, f"r{i}", "Read", {"file_path": f"experiments/r51/cell{i}/record.json"}) is None
    judge = {"command": "python experiments/runner.py judge --help"}
    assert _step(env, "j1", "Bash", judge) is None
    assert _step(env, "j2", "Bash", {"command": "python experiments/runner.py score cell4"}) is None


def test_reverification_of_the_landed_run_denies_past_three(env, monkeypatch):
    _fire_and_land(env, monkeypatch)
    reads = [
        {"file_path": "pkg/mod.py"},
        {"file_path": str(env.repo / "pkg" / "tests" / "test_mod.py")},
        {"file_path": "./pkg/mod.py"},
    ]
    for i, inp in enumerate(reads):
        assert _step(env, f"v{i}", "Read", inp) is None
    res = _step(env, "v3", "Read", {"file_path": "docs/note.md"})
    assert res is not None
    assert reason(res).startswith("[em-prefire-survey] 4 exploratory calls since the landing")


def test_unrelated_reads_do_not_mask_reverification_count(env, monkeypatch):
    _fire_and_land(env, monkeypatch)
    for i in range(5):
        assert _step(env, f"u{i}", "Read", {"file_path": f"elsewhere/{i}.py"}) is None
    for i in range(3):
        assert _step(env, f"v{i}", "Read", {"file_path": "pkg/mod.py"}) is None
    assert _step(env, "v3", "Grep", {"pattern": "x", "path": "pkg"}) is not None


def test_missing_manifest_never_charges_unrelated_calls(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch, known=False)
    for i in range(5):
        assert _step(env, f"u{i}", "Read", {"file_path": f"elsewhere/{i}.py"}) is None
    assert _step(env, "g", "Grep", {"pattern": "x"}) is None
    assert _step(env, "b", "Bash", {"command": "ls elsewhere"}) is None


def test_missing_manifest_still_charges_the_run_sizing(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch, known=False)
    sizing = {"file_path": "state/sizings/2026-10-24-thing-1.yaml"}
    for i in range(3):
        assert _step(env, f"v{i}", "Read", sizing) is None
    assert _step(env, "v3", "Read", sizing) is not None


def test_deny_route_names_the_non_survey_exemptions():
    text = g.prefire_deny_text(9, g.PREFIRE_EXPLORATORY_CALLS_MAX)
    assert "files you wrote with Write/Edit" in text and "user turn" in text and "cross-repo-memo" in text


def test_self_written_scratch_read_is_not_charged_through_evaluate(env):
    path = "scratch/mine.py"
    env.write(user(), use("w", "Write", {"file_path": path, "content": "x"}))
    env.survey(8)
    assert g.evaluate(env.payload(tid="rw", inp={"file_path": path})) is None
    assert g.evaluate(env.payload(tid="rp", inp={"file_path": "product/x.py"})) is not None


def test_product_survey_ninth_read_is_still_denied(env):
    env.write(user(), use("w", "Write", {"file_path": "scratch/mine.py", "content": "x"}))
    env.survey(8)
    res = g.evaluate(env.payload(tid="cur", inp={"file_path": "coordinator_core/ipc.py"}))
    assert res is not None and "permissionDecision" not in res["hookSpecificOutput"]


def test_memo_send_naming_landed_run_is_not_charged_after_landing(env, monkeypatch):
    env.write(user(), use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    _landed(monkeypatch)
    memo = {"command": "cross-repo-memo send --body run-1 landed"}
    for i in range(g.POSTLANDING_VERIFY_CALLS_MAX + 2):
        assert _step(env, f"m{i}", "Bash", memo) is None
    for t in ("p1", "p2", "p3"):
        assert _step(env, t) is None
    assert g.evaluate(env.payload(tid="p4")) is not None


def test_budget_resets_on_a_new_user_turn_through_evaluate(env):
    env.write(user())
    env.survey(8)
    assert g.evaluate(env.payload(tid="over")) is not None
    env.write(user("next task"))
    env.survey(7, start=20)
    assert g.evaluate(env.payload(tid="after")) is None


def test_budget_resets_on_a_new_in_progress_task_through_evaluate(env):
    env.write(user())
    env.survey(8)
    assert g.evaluate(env.payload(tid="over")) is not None
    env.write(use("t", "TaskUpdate", {"taskId": "2", "status": "in_progress"}))
    env.survey(7, start=20)
    assert g.evaluate(env.payload(tid="after")) is None
