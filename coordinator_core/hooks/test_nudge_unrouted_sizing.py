"""Tests for coordinator_core.hooks.nudge_unrouted_sizing.

Covers the incident replay (a resolved, unblocked sizing route the EM narrated but never
entered), both hard judgment-halt exemptions (fork non-null, pm-decision/xl_exit-null),
room-invocation suppression for both room shapes (Skill invocation for plan/spec-dispatch,
dispatched-agents.txt for dispatch), and the never-raise invariant across every failure path.

Also covers `seam: plan->execute-plan` (second seam): an execution-authorized plan
narrated-and-abandoned before `coordinator:execute-plan` was invoked. Its own boundary
test suite lives near the bottom of this file, under the "seam: plan->execute-plan"
banner — the PM-gate boundary (no `execution_authorized_by` -> never fire, regardless
of status) is asserted first and explicitly, per the dispatch brief's ordering.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import nudge_unrouted_sizing as m
from coordinator_core.session.scope import format_touch_event

# ---------------------------------------------------------------------------
# Fixture content — the real 2026-07-31 incident sizing-object
# (state/sizings/2026-07-31-quick-wrap-a-lightweight-end-of-work-cer.yaml, example-doctrine-repo repo),
# reproduced inline per dispatch brief instruction (do not read it at test time).
# ---------------------------------------------------------------------------

_INCIDENT_SIZING_YAML = """\
schema: sizing-object
intent: |
  maybe we should have a lightweight end-of-work ceremony that isn't as heavy as
  `workstream-complete`.
appetite: medium
estimate:
  tshirt: M
  provisional: true
route: plan
detents:
  - appetite_conform
fork: null
xl_exit: null
status: sized
scout_evidence: []
"""


def _git_init(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _write_touched(repo, session_id, *rel_paths):
    d = repo / ".git" / "coordinator-sessions" / session_id
    os.makedirs(d, exist_ok=True)
    (d / "touched.txt").write_text("\n".join(rel_paths) + "\n", encoding="utf-8")


def _write_dispatched_agents(repo, session_id, content):
    d = repo / ".git" / "coordinator-sessions" / session_id
    os.makedirs(d, exist_ok=True)
    (d / "dispatched-agents.txt").write_text(content, encoding="utf-8")


def _write_sizing(repo, rel_path, yaml_text):
    full = repo / rel_path
    os.makedirs(full.parent, exist_ok=True)
    full.write_text(yaml_text, encoding="utf-8")


def _sizing_yaml(route="plan", status="sized", fork="null", xl_exit="null"):
    return (
        "schema: sizing-object\n"
        "intent: test intent\n"
        "appetite: medium\n"
        "estimate:\n"
        "  tshirt: M\n"
        "  provisional: true\n"
        f"route: {route}\n"
        "detents: []\n"
        f"fork: {fork}\n"
        f"xl_exit: {xl_exit}\n"
        f"status: {status}\n"
        "scout_evidence: []\n"
    )


def _transcript_with_skill(tmp_path, name, filename="transcript.jsonl"):
    """Write a transcript whose last assistant turn invoked the Skill tool naming `name`."""
    path = tmp_path / filename
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Skill", "input": {"skill": name}}
                        ]
                    },
                }
            )
            + "\n"
        )
    return str(path)


def _transcript_plain(tmp_path, filename="transcript.jsonl"):
    path = tmp_path / filename
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "taking it into plan now"}]},
                }
            )
            + "\n"
        )
    return str(path)


def _payload(repo, session_id="sess-test", transcript_path=None, **over):
    base = {
        "session_id": session_id,
        "cwd": str(repo),
        "stop_hook_active": False,
        "transcript_path": transcript_path or str(repo / "no-transcript.jsonl"),
        # Default forward-intent tell -- the text half is now a hard precondition
        # (see module docstring's "Text half" section), and the overwhelming
        # majority of pre-existing tests below are exercising state-machine logic
        # unrelated to text, so they carry a generic live tell by default. Tests
        # for the text half itself override this explicitly.
        "last_assistant_message": "taking it into plan now",
    }
    base.update(over)
    return base


def _write_em_session_marker(repo, session_id):
    d = repo / ".git" / "coordinator-sessions" / ".agents" / session_id
    os.makedirs(d, exist_ok=True)
    (d / "em-session-id.txt").write_text("em-parent-session\n", encoding="utf-8")


def _assistant_arrived_record():
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
            },
        }
    )


def _assistant_running_record():
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
            },
        }
    )


def _write_subagent_transcript(parent_transcript_path, agent_id, lines):
    parent = Path(parent_transcript_path)
    sub = parent.parent / parent.stem / "subagents" / f"agent-{agent_id}.jsonl"
    sub.parent.mkdir(parents=True, exist_ok=True)
    sub.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sub


@pytest.fixture(autouse=True)
def _no_env_hatch(monkeypatch):
    monkeypatch.delenv("COORDINATOR_UNROUTED_SIZING_NUDGE_OFF", raising=False)


@pytest.fixture
def repo(tmp_path):
    _git_init(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Incident replay
# ---------------------------------------------------------------------------


def test_incident_replay_fires(repo):
    session_id = "sess-incident"
    rel = "state/sizings/2026-07-31-quick-wrap-a-lightweight-end-of-work-cer.yaml"
    _write_sizing(repo, rel, _INCIDENT_SIZING_YAML)
    _write_touched(repo, session_id, rel)
    transcript = _transcript_plain(repo)
    result = m.op(_payload(repo, session_id=session_id, transcript_path=transcript))
    assert result is not None
    first_line = result["message"].splitlines()[0]
    assert "route: plan is resolved and unblocked" in first_line
    assert "invoke coordinator:plan now" in first_line


# ---------------------------------------------------------------------------
# 2 & 3. Hard judgment-halt exemptions
# ---------------------------------------------------------------------------


def test_fork_appetite_exceeded_does_not_fire(repo):
    session_id = "sess-fork"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(fork="appetite_exceeded"))
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_pm_decision_xl_exit_null_does_not_fire(repo):
    session_id = "sess-pmdecision"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="pm-decision", xl_exit="null"))
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


# ---------------------------------------------------------------------------
# 4. Room already invoked (Skill path) -> silent
# ---------------------------------------------------------------------------


def test_room_invoked_via_skill_does_not_fire(repo):
    session_id = "sess-invoked"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    transcript = _transcript_with_skill(repo, "coordinator:plan")
    result = m.op(_payload(repo, session_id=session_id, transcript_path=transcript))
    assert result is None


def test_room_invoked_for_spec_dispatch_also_checks_coordinator_plan(repo):
    session_id = "sess-invoked-sd"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="spec-dispatch"))
    _write_touched(repo, session_id, rel)
    transcript = _transcript_with_skill(repo, "coordinator:plan")
    result = m.op(_payload(repo, session_id=session_id, transcript_path=transcript))
    assert result is None


def test_unrelated_skill_invocation_does_not_suppress(repo):
    session_id = "sess-unrelated-skill"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    transcript = _transcript_with_skill(repo, "coordinator:shape")
    result = m.op(_payload(repo, session_id=session_id, transcript_path=transcript))
    assert result is not None


# ---------------------------------------------------------------------------
# 5. status: routed -> silent
# ---------------------------------------------------------------------------


def test_status_routed_does_not_fire(repo):
    session_id = "sess-routed"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(status="routed"))
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


# ---------------------------------------------------------------------------
# 6. No sizing object written this session -> silent
# ---------------------------------------------------------------------------


def test_no_sizing_object_written_this_session_does_not_fire(repo):
    session_id = "sess-no-write"
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_touched_file_present_but_no_sizing_entries_does_not_fire(repo):
    session_id = "sess-other-touch"
    _write_touched(repo, session_id, "coordinator_core/hooks/foo.py")
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


# ---------------------------------------------------------------------------
# 7 & 8. Env switch, stop_hook_active, agent_id
# ---------------------------------------------------------------------------


def test_env_hatch_silences(repo, monkeypatch):
    session_id = "sess-env-off"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml())
    _write_touched(repo, session_id, rel)
    monkeypatch.setenv("COORDINATOR_UNROUTED_SIZING_NUDGE_OFF", "1")
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_stop_hook_active_never_fires(repo):
    session_id = "sess-loop"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml())
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id, stop_hook_active=True))
    assert result is None


def test_agent_id_present_never_fires(repo):
    session_id = "sess-subagent"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml())
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id, agent_id="agent-1"))
    assert result is None


# ---------------------------------------------------------------------------
# 9. Never raises on malformed / absent inputs
# ---------------------------------------------------------------------------


def test_non_dict_payload_never_raises():
    assert m.op(["not", "a", "dict"]) is None
    assert m.op(None) is None
    assert m.op("also not a dict") is None


def test_malformed_yaml_does_not_raise_and_does_not_fire(repo):
    session_id = "sess-bad-yaml"
    rel = "state/sizings/x.yaml"
    full = repo / rel
    os.makedirs(full.parent, exist_ok=True)
    full.write_text("route: plan\n  bad: [unterminated\n", encoding="utf-8")
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_absent_sizing_file_does_not_raise_and_does_not_fire(repo):
    session_id = "sess-absent-sizing"
    _write_touched(repo, session_id, "state/sizings/does-not-exist.yaml")
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_absent_state_dir_does_not_raise(tmp_path):
    """No .git at all -> no repo root -> silent, never raises."""
    result = m.op(_payload(tmp_path, session_id="sess-no-git"))
    assert result is None


def test_unreadable_transcript_does_not_raise_and_fires(repo):
    session_id = "sess-bad-transcript"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml())
    _write_touched(repo, session_id, rel)
    result = m.op(
        _payload(repo, session_id=session_id, transcript_path=str(repo / "does-not-exist.jsonl"))
    )
    assert result is not None  # unreadable transcript -> "no evidence found" -> fires


def test_non_dict_message_content_does_not_raise(repo):
    session_id = "sess-odd-content"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml())
    _write_touched(repo, session_id, rel)
    path = repo / "t.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "message": "not-a-dict"}) + "\n")
    result = m.op(_payload(repo, session_id=session_id, transcript_path=str(path)))
    assert result is not None


# ---------------------------------------------------------------------------
# 10. Fire-once
# ---------------------------------------------------------------------------


def test_fires_at_most_once_per_session(repo):
    session_id = "sess-once"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml())
    _write_touched(repo, session_id, rel)
    payload = _payload(repo, session_id=session_id)
    assert m.op(payload) is not None
    assert m.op(payload) is None


# ---------------------------------------------------------------------------
# 11. Leading token names the concrete action
# ---------------------------------------------------------------------------


def test_leading_line_names_concrete_action_for_dispatch_route(repo):
    session_id = "sess-dispatch-route"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="dispatch"))
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id))
    assert result is not None
    first_line = result["message"].splitlines()[0]
    assert "[nudge] route: dispatch is resolved and unblocked" in first_line
    assert "dispatch it now" in first_line


# ---------------------------------------------------------------------------
# route: dispatch — room-invocation evidence is dispatched-agents.txt, not a Skill
# ---------------------------------------------------------------------------


def test_dispatch_route_suppressed_by_dispatched_agents_file(repo):
    session_id = "sess-dispatch-evidence"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="dispatch"))
    _write_touched(repo, session_id, rel)
    _write_dispatched_agents(repo, session_id, "abcdef012345\tsonnet\texecutor\t1234567890\n")
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_dispatch_route_fires_when_dispatched_agents_file_absent(repo):
    session_id = "sess-dispatch-no-evidence"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="dispatch"))
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id))
    assert result is not None


def test_dispatch_route_fires_when_dispatched_agents_file_empty(repo):
    session_id = "sess-dispatch-empty-evidence"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="dispatch"))
    _write_touched(repo, session_id, rel)
    _write_dispatched_agents(repo, session_id, "")
    result = m.op(_payload(repo, session_id=session_id))
    assert result is not None


# ---------------------------------------------------------------------------
# Multiple sizing writes this session — the matching one wins
# ---------------------------------------------------------------------------


def test_first_matching_sizing_object_among_several_touched(repo):
    session_id = "sess-multi"
    routed_rel = "state/sizings/a-routed.yaml"
    unrouted_rel = "state/sizings/b-unrouted.yaml"
    _write_sizing(repo, routed_rel, _sizing_yaml(status="routed"))
    _write_sizing(repo, unrouted_rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, routed_rel, unrouted_rel)
    result = m.op(_payload(repo, session_id=session_id))
    assert result is not None
    assert unrouted_rel in result["message"]


# ---------------------------------------------------------------------------
# Worktree: `.git` FILE resolves the sentinel root
# ---------------------------------------------------------------------------


def test_worktree_style_git_file_resolves_sentinel_root(tmp_path):
    outer = tmp_path / "outer"
    os.makedirs(outer / ".git")
    inner = outer / "worktree"
    os.makedirs(inner)
    real_git = tmp_path / "elsewhere"
    os.makedirs(real_git)
    (inner / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    payload = {
        "session_id": "wt-sess",
        "cwd": str(inner),
        "stop_hook_active": False,
        "transcript_path": str(inner / "none.jsonl"),
    }
    sentinel = m._sentinel_path(payload)
    assert sentinel is not None
    assert sentinel.startswith(str(real_git))


# ---------------------------------------------------------------------------
# Text half — the OVERLAP case is the point: a message carrying both a
# completion report AND a genuine forward-intent tell must still fire. There is
# no suppressor to reorder against (see _text_trips_tell's own docstring) --
# this is a plain positive-match assertion, written first per the dispatch
# brief because it's the case worth catching.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "Fixed the parser and committed. Next I'll take this into `coordinator:plan`.",
        "Done -- tests are green, pushed. Proceeding to invoke coordinator:plan now.",
        "Landed the fix. I'll invoke the plan room now.",
    ],
)
def test_overlap_completion_report_with_forward_intent_still_fires(repo, msg):
    session_id = "sess-overlap-" + str(abs(hash(msg)))
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is not None

    # The fixture carries both a completion report AND a live forward-intent
    # tell; _text_trips_tell has no suppressor to reorder against (see its own
    # docstring), so this is a plain positive-match assertion, not a precedence
    # check against a second branch.
    assert m._FORWARD_INTENT_RE.search(msg), "fixture must carry a live forward-intent tell"
    assert m._text_trips_tell(msg) is True


def test_pure_completion_report_no_forward_intent_does_not_fire(repo):
    session_id = "sess-pure-completion"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    msg = "Fixed the issue, committed, and pushed. Tests are green. Done."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None
    assert m._text_trips_tell(msg) is False


def test_pure_forward_intent_fires(repo):
    session_id = "sess-pure-forward"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    msg = "Taking it into plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is not None
    assert m._text_trips_tell(msg) is True


def test_state_present_but_no_tell_does_not_fire(repo):
    """Proves the text half is load-bearing: a genuinely unblocked, unrouted
    sizing-object alone is no longer enough without a live tell."""
    session_id = "sess-no-tell"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    msg = "Reviewed the sizing object and moved on to something else for now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None
    assert m._text_trips_tell(msg) is False


def test_empty_final_message_does_not_fire(repo):
    session_id = "sess-empty-message"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=""))
    assert result is None


# ---------------------------------------------------------------------------
# EM-discriminator — house-authoritative subagent check via em-session-id.txt,
# not `agent_id` alone.
# ---------------------------------------------------------------------------


def test_subagent_via_em_session_id_marker_does_not_fire(repo):
    session_id = "sess-em-marker-subagent"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    _write_em_session_marker(repo, session_id)
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_is_subagent_session_never_raises_on_unresolvable_repo_root():
    assert m._is_subagent_session("sess", "/nonexistent/path/does-not-exist-xyz") is False


# ---------------------------------------------------------------------------
# Legitimate-wait suppression — an in-flight dispatch is a correct wait, not
# the failure this op targets.
# ---------------------------------------------------------------------------


def test_in_flight_dispatch_running_does_not_fire(repo):
    session_id = "sess-inflight-running"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    transcript = repo / "parent-running.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    now = int(time.time())
    _write_dispatched_agents(repo, session_id, f"agentidrunning\tsonnet\texecutor\t{now}\n")
    _write_subagent_transcript(transcript, "agentidrunning", [_assistant_running_record()])
    result = m.op(_payload(repo, session_id=session_id, transcript_path=str(transcript)))
    assert result is None


def test_in_flight_dispatch_unknown_state_does_not_fire(repo):
    """An unreadable/absent subagent transcript resolves to "unknown", which
    counts as in-flight (fail toward waiting), not toward a false all-clear."""
    session_id = "sess-inflight-unknown"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    transcript = repo / "parent-unknown.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    now = int(time.time())
    _write_dispatched_agents(repo, session_id, f"agentidunknown\tsonnet\texecutor\t{now}\n")
    # No subagent transcript written at all -> subagent_arrival_check reads "unknown".
    result = m.op(_payload(repo, session_id=session_id, transcript_path=str(transcript)))
    assert result is None


def test_in_flight_dispatch_all_arrived_fires(repo):
    session_id = "sess-inflight-arrived"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    transcript = repo / "parent-arrived.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    now = int(time.time())
    _write_dispatched_agents(repo, session_id, f"agentidarrived\tsonnet\texecutor\t{now}\n")
    _write_subagent_transcript(transcript, "agentidarrived", [_assistant_arrived_record()])
    result = m.op(_payload(repo, session_id=session_id, transcript_path=str(transcript)))
    assert result is not None


def test_in_flight_dispatch_aged_out_beyond_staleness_cap_does_not_suppress(repo):
    """A row older than RUNTIME_TRIPWIRE_MAX_TRACK_MIN (default 90 min) is not
    tracked as in-flight at all, even though it is still (per its own transcript)
    "running" -- the staleness cap wins, matching runtime-tripwire-em-check.py's
    own skip-if-too-old behaviour."""
    session_id = "sess-inflight-aged"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    transcript = repo / "parent-aged.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    stale_ts = int(time.time()) - (100 * 60)  # 100 minutes ago > the 90-minute cap
    _write_dispatched_agents(repo, session_id, f"agentidaged\tsonnet\texecutor\t{stale_ts}\n")
    _write_subagent_transcript(transcript, "agentidaged", [_assistant_running_record()])
    result = m.op(_payload(repo, session_id=session_id, transcript_path=str(transcript)))
    assert result is not None


def test_in_flight_check_never_raises_on_arrival_check_exception(repo, monkeypatch):
    session_id = "sess-inflight-exception"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    now = int(time.time())
    _write_dispatched_agents(repo, session_id, f"agentidfail\tsonnet\texecutor\t{now}\n")

    async def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "_arrival_check", _raise)
    # An arrival-check failure degrades that row to "unknown" -> in-flight ->
    # suppressed, and must never raise past op()'s own boundary.
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_dispatch_rows_never_raises_on_malformed_lines(repo):
    session_id = "sess-malformed-dispatch-rows"
    d = repo / ".git" / "coordinator-sessions" / session_id
    os.makedirs(d, exist_ok=True)
    (d / "dispatched-agents.txt").write_text("not-enough-columns\nalso\tbad\n", encoding="utf-8")
    assert m._dispatch_rows(session_id, str(repo)) == []


# ---------------------------------------------------------------------------
# Corrected in-flight determination — `dispatched-agents.txt` is append-only and
# carries no completion record, so a suppressor keyed on its mere non-emptiness
# would go permanently silent after an EM's first dispatch of the session. The
# three-tier resolution (arrived / running / unknown) below is what avoids that:
# "arrived" never suppresses regardless of elapsed time; "running" suppresses
# until the hard RUNTIME_TRIPWIRE_MAX_TRACK_MIN cap; "unknown" suppresses only
# within the row's own per-model runtime-threshold window.
# ---------------------------------------------------------------------------


def test_unknown_state_within_model_window_suppresses(repo):
    """A spawn inside its model's runtime threshold, with no resolvable arrival
    state at all, still suppresses -- this is the safe over-suppression tier."""
    session_id = "sess-unknown-in-window"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    recent_ts = int(time.time()) - (5 * 60)  # 5 min ago, well inside sonnet's 12-min default
    _write_dispatched_agents(repo, session_id, f"agentidwindow\tsonnet\texecutor\t{recent_ts}\n")
    # No subagent transcript written -> arrival state resolves "unknown".
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_unknown_state_aged_past_model_threshold_fires(repo):
    """The same row, once it clears its OWN per-model threshold (but still well
    under the 90-min hard cap), stops suppressing and the nudge fires -- this is
    the case the earlier (file-presence) brief got backwards."""
    session_id = "sess-unknown-past-window"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    aged_ts = int(time.time()) - (15 * 60)  # 15 min ago > sonnet's 12-min default, < 90-min cap
    _write_dispatched_agents(repo, session_id, f"agentidpastwindow\tsonnet\texecutor\t{aged_ts}\n")
    # No subagent transcript written -> arrival state resolves "unknown".
    result = m.op(_payload(repo, session_id=session_id))
    assert result is not None


def test_running_state_past_model_threshold_still_suppresses(repo):
    """A CONFIRMED "running" row outranks the per-model window entirely -- it
    keeps suppressing until the 90-min hard cap, unlike an "unknown" row at the
    same age (previous test), because a positive live-process signal beats a
    mere elapsed-time heuristic."""
    session_id = "sess-running-past-model-window"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    transcript = repo / "parent-running-past-window.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    aged_ts = int(time.time()) - (20 * 60)  # 20 min ago > sonnet's 12-min default, < 90-min cap
    _write_dispatched_agents(repo, session_id, f"agentidrunlong\tsonnet\texecutor\t{aged_ts}\n")
    _write_subagent_transcript(transcript, "agentidrunlong", [_assistant_running_record()])
    result = m.op(_payload(repo, session_id=session_id, transcript_path=str(transcript)))
    assert result is None


def test_arrived_state_never_suppresses_regardless_of_window(repo):
    """A CONFIRMED "arrived" row never counts as in-flight, even freshly
    dispatched and well inside every window -- a positive completion signal
    beats any timer outright."""
    session_id = "sess-arrived-fresh"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    transcript = repo / "parent-arrived-fresh.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    now = int(time.time())
    _write_dispatched_agents(repo, session_id, f"agentidarrivedfresh\tsonnet\texecutor\t{now}\n")
    _write_subagent_transcript(transcript, "agentidarrivedfresh", [_assistant_arrived_record()])
    result = m.op(_payload(repo, session_id=session_id, transcript_path=str(transcript)))
    assert result is not None


def test_malformed_row_alongside_valid_in_window_row_still_suppresses(repo):
    """A malformed row must not abort the scan, and a valid in-window row
    elsewhere in the same file must still suppress."""
    session_id = "sess-malformed-plus-valid"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    recent_ts = int(time.time()) - (2 * 60)
    d = repo / ".git" / "coordinator-sessions" / session_id
    os.makedirs(d, exist_ok=True)
    (d / "dispatched-agents.txt").write_text(
        f"not-enough-columns\nagentidvalid\tsonnet\texecutor\t{recent_ts}\n",
        encoding="utf-8",
    )
    result = m.op(_payload(repo, session_id=session_id))
    assert result is None


def test_in_flight_empty_dispatch_file_does_not_suppress(repo):
    session_id = "sess-inflight-empty-file"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    _write_dispatched_agents(repo, session_id, "")
    assert m._session_has_in_flight_dispatch(_payload(repo, session_id=session_id), session_id, str(repo)) is False
    result = m.op(_payload(repo, session_id=session_id))
    assert result is not None


def test_in_flight_absent_dispatch_file_does_not_suppress(repo):
    session_id = "sess-inflight-absent-file"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    assert m._session_has_in_flight_dispatch(_payload(repo, session_id=session_id), session_id, str(repo)) is False
    result = m.op(_payload(repo, session_id=session_id))
    assert result is not None


def test_runtime_threshold_minutes_matches_model_families():
    assert m._runtime_threshold_minutes("claude-opus-5") == 25
    assert m._runtime_threshold_minutes("claude-sonnet-5") == 12
    assert m._runtime_threshold_minutes("claude-haiku-4-5") == 10
    assert m._runtime_threshold_minutes("") == 25
    assert m._runtime_threshold_minutes("unknown-model") == 25
    assert m._runtime_threshold_minutes("claude-opus-5[1m]") == 25


def test_runtime_threshold_minutes_env_overridable(monkeypatch):
    monkeypatch.setenv("RUNTIME_TRIPWIRE_SONNET_MIN", "3")
    assert m._runtime_threshold_minutes("sonnet") == 3


# ---------------------------------------------------------------------------
# F8 — `execute-plan` must never become a member of the SIZING LOBBY's own
# routable set. Pin the boundary with a test: a red test is a boundary, a
# docstring is a request. (Review: eng-director/the Director of Engineering F8.)
#
# Both of the following are true simultaneously, and this test asserts the
# one that must never regress: the plan->execute-plan seam IS live (see the
# "seam: plan->execute-plan" test section below) as its own SEPARATE
# evaluator (`_find_plan_candidate` / `_plan_execution_authorized_and_active`
# / `_execute_plan_invoked`), with its own state-read, its own criteria
# function, and its own room-invocation-evidence function — it was never
# folded into `_ROUTABLE_ROUTES`, and doing so would be a structural
# regression: `_ROUTABLE_ROUTES` is the sizing-lobby's route allow-list read
# off a `state/sizings/*.yaml` object's own `route` field, and `execute-plan`
# is not, and never will be, a value that field can hold. The two facts
# coexist because they describe different objects: a sizing-object's `route`
# vs. a plan's `execution_authorized_by`/`status` frontmatter.
# ---------------------------------------------------------------------------


def test_execute_plan_is_never_a_routable_route():
    assert "execute-plan" not in m._ROUTABLE_ROUTES


# ---------------------------------------------------------------------------
# F3 — curly-apostrophe normalization, symmetric it/this object, and a widened
# verb set. Fixtures authored FIRST from realistic phrasings the old regex
# missed (11 of 12 probed), per the dispatch brief.
# (Review: eng-director/the Director of Engineering F3.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "I’ll invoke coordinator:plan next.",
        "Next, I’ll take this into plan.",
        "I'll take it into coordinator:plan.",
        "Invoking coordinator:plan now.",
        "Moving into the plan skill.",
        "Kicking off coordinator:plan.",
        "Next step: coordinator:plan.",
        "Heading into coordinator:plan now.",
        "Will invoke coordinator:plan.",
        "Routing to coordinator:plan now.",
        "I'm going to take this into plan.",
    ],
)
def test_widened_forward_intent_phrasings_trip_the_tell(msg):
    assert m._text_trips_tell(msg) is True


# ---------------------------------------------------------------------------
# F4 — the tell must co-occur with a route referent in the same sentence. The
# three probed legitimate PM-question turn-ends below must NOT fire, even
# though each carries a forward-intent tell somewhere in the message.
# (Review: eng-director/the Director of Engineering F4.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "Next I'll need your call on whether shipped belongs in the enum.",
        "Sized it: route plan, fork null. Blocked on a licence question for "
        "you. Next I'll pick this up once you answer.",
        "Proceeding to ask you about the schema split before anything else.",
    ],
)
def test_route_agnostic_forward_intent_does_not_trip_the_tell(msg):
    assert m._text_trips_tell(msg) is False


def test_route_agnostic_forward_intent_does_not_fire_end_to_end(repo):
    session_id = "sess-false-fire-f4"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched(repo, session_id, rel)
    msg = "Next I'll need your call on whether shipped belongs in the enum."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


# ---------------------------------------------------------------------------
# seam: plan->execute-plan — an EM narrating resuming/continuing an
# already-authorized plan's execution, then stopping without invoking
# `coordinator:execute-plan`. THE CRITICAL BOUNDARY: never fires when
# `execution_authorized_by` is absent, regardless of `status` -- that is the
# pre-execute PM authorization gate doing its job. Boundary tests written
# first, per the dispatch brief.
# ---------------------------------------------------------------------------

_PLAN_SLUG = "2026-01-01-a-test-plan"
_PLAN_REL = f"docs/plans/{_PLAN_SLUG}.md"


def _plan_frontmatter(status="executing", execution_authorized_by="pm-example-operator", extra=""):
    lines = ["---", "title: A test plan", f"status: {status}"]
    if execution_authorized_by is not None:
        lines.append(f"execution_authorized_by: {execution_authorized_by}")
    if extra:
        lines.append(extra)
    lines.append("---")
    lines.append("")
    lines.append("# A test plan")
    lines.append("")
    return "\n".join(lines)


def _write_plan(repo, rel_path, text):
    full = repo / rel_path
    os.makedirs(full.parent, exist_ok=True)
    full.write_text(text, encoding="utf-8")


def _transcript_with_execute_plan_skill(tmp_path, filename="transcript.jsonl"):
    return _transcript_with_skill(tmp_path, "coordinator:execute-plan", filename=filename)


# 1a/1b. THE PM-GATE BOUNDARY -- no execution_authorized_by -> never fire,
# whatever the status.


def test_plan_no_execution_authorized_by_status_reviewed_does_not_fire(repo):
    session_id = "sess-plan-no-auth-reviewed"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="reviewed", execution_authorized_by=None))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = f"Taking this into coordinator:execute-plan now for {_PLAN_SLUG}."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


def test_plan_no_execution_authorized_by_status_approved_does_not_fire(repo):
    """Same plan, now at status: approved, STILL no stamp -- still silent. This
    pins the PM-gate boundary explicitly: status alone is never sufficient."""
    session_id = "sess-plan-no-auth-approved"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="approved", execution_authorized_by=None))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = f"Taking this into coordinator:execute-plan now for {_PLAN_SLUG}."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


# 2. Authorized + executing + a forward-intent tell naming execute-plan -> FIRES.


def test_plan_authorized_executing_with_tell_fires(repo):
    session_id = "sess-plan-fires"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is not None
    first_line = result["message"].splitlines()[0]
    assert f"[nudge] {_PLAN_REL}" in first_line
    assert "coordinator:execute-plan" in result["message"]


# 3. Authorized + status: shipped (unknown-status hazard) -> does NOT fire.


def test_plan_authorized_status_shipped_does_not_fire(repo):
    session_id = "sess-plan-shipped"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="shipped"))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None
    assert m._plan_execution_authorized_and_active({"status": "shipped", "execution_authorized_by": "x"}) is False


# 4. Each terminal status -> does NOT fire.


@pytest.mark.parametrize(
    "status", ["landed", "implemented", "deferred", "abandoned", "superseded"]
)
def test_plan_terminal_statuses_do_not_fire(repo, status):
    session_id = "sess-plan-terminal-" + status
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status=status))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


# 5. Authorized + executing but NO tell -> does NOT fire (text half load-bearing here too).


def test_plan_authorized_executing_no_tell_does_not_fire(repo):
    session_id = "sess-plan-no-tell"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Fixed the parser, committed, and pushed. Tests are green."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


# 6. Authorized + tell present but names no route referent -> does NOT fire.


def test_plan_authorized_tell_without_route_referent_does_not_fire(repo):
    session_id = "sess-plan-no-referent"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Next I'll need your call on whether shipped belongs in the enum."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


# 7. Malformed/absent plan frontmatter, unreadable file, no plans touched ->
# does NOT fire, does NOT raise.


def test_plan_malformed_frontmatter_does_not_raise_and_does_not_fire(repo):
    session_id = "sess-plan-malformed"
    full = repo / _PLAN_REL
    os.makedirs(full.parent, exist_ok=True)
    full.write_text("---\nstatus: executing\n  bad: [unterminated\n", encoding="utf-8")
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


def test_plan_no_frontmatter_fence_does_not_raise_and_does_not_fire(repo):
    session_id = "sess-plan-no-fence"
    full = repo / _PLAN_REL
    os.makedirs(full.parent, exist_ok=True)
    full.write_text("# Just a plan, no frontmatter\n", encoding="utf-8")
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


def test_plan_absent_file_does_not_raise_and_does_not_fire(repo):
    session_id = "sess-plan-absent"
    _write_touched(repo, session_id, "docs/plans/does-not-exist.md")
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


# ---------------------------------------------------------------------------
# Event-line touched.txt — Review: code-reviewer (Finding 2). Every fixture
# above is a bare legacy path; `_session_touched_lines` strips the
# verb/timestamp off a REAL `T <ts> <path>` event line (via
# `parse_touch_event`) so the anchored `_SIZING_PATH_RE`/`_PLAN_PATH_RE`
# still match against just the path field.
# ---------------------------------------------------------------------------


def _write_touched_event_lines(repo, session_id, *event_lines):
    d = repo / ".git" / "coordinator-sessions" / session_id
    os.makedirs(d, exist_ok=True)
    (d / "touched.txt").write_text("\n".join(event_lines) + "\n", encoding="utf-8")


def test_event_line_touched_txt_still_fires_for_unrouted_sizing(repo):
    session_id = "sess-event-sizing"
    rel = "state/sizings/x.yaml"
    _write_sizing(repo, rel, _sizing_yaml(route="plan"))
    _write_touched_event_lines(repo, session_id, format_touch_event("T", rel))
    result = m.op(_payload(repo, session_id=session_id))
    assert result is not None
    assert rel in result["message"]


def test_event_line_touched_txt_still_fires_for_execute_plan(repo):
    session_id = "sess-event-plan"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched_event_lines(repo, session_id, format_touch_event("T", _PLAN_REL))
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is not None
    assert _PLAN_REL in result["message"]


def test_no_plans_touched_this_session_does_not_fire(repo):
    session_id = "sess-plan-none-touched"
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is None


# Room-invocation suppression: coordinator:execute-plan already invoked ->
# silent, even with a live tell and no other suppressor tripped.


def test_plan_execute_plan_already_invoked_does_not_fire(repo):
    session_id = "sess-plan-invoked"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched(repo, session_id, _PLAN_REL)
    transcript = _transcript_with_execute_plan_skill(repo)
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(
        _payload(repo, session_id=session_id, transcript_path=transcript, last_assistant_message=msg)
    )
    assert result is None


# Plan slug alone (no skill/route noun) still counts as a route referent.


def test_plan_tell_naming_only_the_plan_slug_fires(repo):
    session_id = "sess-plan-slug-referent"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = f"Next I'll resume {_PLAN_SLUG}."
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=msg))
    assert result is not None


# EM-only, env hatch, stop_hook_active, subagent, and in-flight-dispatch
# guards are shared with the sizing->room seam and are proven generically
# above; this seam-specific case confirms they still apply on the plan path.


def test_plan_agent_id_present_never_fires(repo):
    session_id = "sess-plan-subagent"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Taking this into coordinator:execute-plan now."
    result = m.op(_payload(repo, session_id=session_id, agent_id="agent-1", last_assistant_message=msg))
    assert result is None


def test_plan_fires_at_most_once_per_session(repo):
    session_id = "sess-plan-once"
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched(repo, session_id, _PLAN_REL)
    msg = "Taking this into coordinator:execute-plan now."
    payload = _payload(repo, session_id=session_id, last_assistant_message=msg)
    assert m.op(payload) is not None
    assert m.op(payload) is None


def test_sizing_and_plan_seams_share_one_fire_once_sentinel(repo):
    """Both a resolved-unrouted sizing-object AND an execution-authorized plan
    were touched this session; the sizing seam is checked first and fires,
    claiming the SHARED sentinel -- a fresh op() call for the same session
    fires nothing more, proving the two seams do not hold independent slots."""
    session_id = "sess-shared-sentinel"
    sizing_rel = "state/sizings/x.yaml"
    _write_sizing(repo, sizing_rel, _sizing_yaml(route="plan"))
    _write_plan(repo, _PLAN_REL, _plan_frontmatter(status="executing"))
    _write_touched(repo, session_id, sizing_rel, _PLAN_REL)
    msg = f"Taking it into plan now for {_PLAN_SLUG}, then coordinator:execute-plan."
    payload = _payload(repo, session_id=session_id, last_assistant_message=msg)
    first = m.op(payload)
    assert first is not None
    assert "route: plan is resolved" in first["message"]
    second = m.op(payload)
    assert second is None


def test_plan_criteria_function_direct_unit_cases():
    assert m._plan_execution_authorized_and_active(
        {"execution_authorized_by": "pm", "status": "executing"}
    ) is True
    assert m._plan_execution_authorized_and_active(
        {"execution_authorized_by": "pm", "status": "approved"}
    ) is True
    assert m._plan_execution_authorized_and_active({"status": "executing"}) is False
    assert m._plan_execution_authorized_and_active(
        {"execution_authorized_by": "", "status": "executing"}
    ) is False
    assert m._plan_execution_authorized_and_active(
        {"execution_authorized_by": "pm", "status": "reviewed"}
    ) is False
    assert m._plan_execution_authorized_and_active(
        {"execution_authorized_by": "pm", "status": "shipped"}
    ) is False
    assert m._plan_execution_authorized_and_active(
        {"execution_authorized_by": "pm", "status": None}
    ) is False
    assert m._plan_execution_authorized_and_active({}) is False
