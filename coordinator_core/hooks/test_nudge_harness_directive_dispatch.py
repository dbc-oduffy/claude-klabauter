
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from coordinator_core.hooks import nudge_harness_directive_dispatch as m
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _transcript(tmp_path, *assistant_texts):
    path = tmp_path / "transcript.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for text in assistant_texts:
            fh.write(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": text}]},
                    }
                )
                + "\n"
            )
    return str(path)


def _payload(tmp_path, transcript_path, **over):
    base = {
        "session_id": "sess-test",
        "transcript_path": transcript_path,
        "cwd": str(tmp_path),
        "stop_hook_active": False,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_env_hatch(monkeypatch):
    monkeypatch.delenv("COORDINATOR_HARNESS_DIRECTIVE_NUDGE_OFF", raising=False)


@pytest.fixture
def repo(tmp_path):
    os.makedirs(tmp_path / ".git")
    return tmp_path


@pytest.mark.parametrize(
    "text",
    [
        "I did not delegate because my instructions say do not call the AgentTool.",
        "I held off since the guidance is to avoid it unless the user requested it.",
        "Want me to dispatch a reviewer for this?",
        "Should I delegate the verification to a subagent?",
        "Would you like me to fan-out this across parallel agents?",
        # was deliberately dropped from _META_DISCUSSION for exactly this case.
        "Per DR-108 dispatch is encouraged, but this feels borderline — "
        "should I dispatch the executor anyway?",
    ],
)
def test_tells_trip(text):
    assert m.message_trips_tell(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "I'm holding that dispatch on your standing don't-call-the-Agent-tool "
        "instruction.",
        "I held off dispatching a reviewer because that's your standing "
        "instruction.",
        "As you instructed, I didn't dispatch a subagent for this.",
        "That's your rule against fan-out here, so I declined to spawn anyone.",
    ],
)
def test_tell_c_misattribution_trips(text):
    assert m.message_trips_tell(text) is True


def test_recurrence_5_regression_verbatim():
    text = (
        "this session was started with a standing instruction not to use "
        "the Agent tool unless you request it. That's yours to resolve, "
        "not mine to assume around."
    )
    assert m.message_trips_tell(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "I should not call the Agent tool unless you request it.",
        "Not calling the Agent Tool unless you request it.",
    ],
)
def test_tell_a_widened_spacing_and_pronoun_trip(text):
    assert m.message_trips_tell(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "We built a small automation tool; the agent liked it.",
        "Unless you object, I'll proceed with the plan as written.",
    ],
)
def test_tell_a_widened_patterns_stay_silent_on_controls(text):
    assert m.message_trips_tell(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "That's yours to decide, so I didn't dispatch anyone for this.",
        "This session was started with a standing rule not to dispatch "
        "subagents here.",
    ],
)
def test_tell_c_widened_patterns_trip(text):
    assert m.message_trips_tell(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "This bug is yours to triage; I already fixed the crash in the "
        "parser.",
        "This session was started with a standing goal to ship the release "
        "by Friday.",
        # Control: agentless-passive + dispatch term in DIFFERENT sentences
        "This session was started with a standing instruction to keep PRs "
        "small. Also dispatched a reviewer for the diff.",
    ],
)
def test_tell_c_widened_patterns_stay_silent_on_controls(text):
    assert m.message_trips_tell(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Dispatched two reviewers; both came back clean.",
        "Fixed the precedence bug in resolve_root(); tests green.",
        "Should I ship this to main, or hold for the weekly gate?",
        "",
        "I renamed AgentToolkit to clarify its scope.",
        "My system prompt says not to use workflows or deep-research, so I did this inline.",
        "Per your standing instruction to keep PRs under 300 lines, I split "
        "this into two.",
        "As you instructed, I dispatched two reviewers already.",
        "Your rule about commit messages is clear, so I followed it. "
        "Also dispatched a reviewer for the diff.",
        "The hook fires on every subagent dispatch.",
        "That hook logs the agent_id so a spawn can be reconciled later.",
    ],
)
def test_ordinary_turns_stay_silent(text):
    assert m.message_trips_tell(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Should I commit this now?",
        "Want me to stage and commit these changes?",
        "OK for me to commit now?",
        "Should I go ahead and commit the fix?",
        "Would you like me to stage these before committing?",
    ],
)
def test_tell_d_commit_permission_ask_trips(text):
    assert m.message_trips_tell(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Shall I merge to main?",
        "Ready to merge?",
        "/merging-to-main?",
        "Should I push this to the remote?",
        "Want me to open a PR for this?",
        "Should I push and open a PR now?",
        "Committed as abc1234.",
        "Staged and committed the changes.",
        # Scoping questions about commit CONTENTS, not permission to commit.
        "Which files should I include in this commit?",
        "This lessons file looks out of scope — should I leave it out of the commit?",
        "Should I commit and then push this to the remote?",
        "We already do — hook path and `commit-tree` path both.",
        "Their hook is a shim that execs my working-tree Python at commit time.",
        "The pre-commit hook stages nothing on its own.",
    ],
)
def test_tell_d_commit_permission_ask_does_not_trip(text):
    assert m.message_trips_tell(text) is False


def test_meta_discussion_suppressed():
    text = "The AgentTool line comes from tengu_heron_brook; see harness-directive-conflicts.md."
    assert m.message_trips_tell(text) is False


def test_fires_on_tell(repo):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    result = m.op(_payload(repo, t))
    assert result is not None
    assert "Dispatch/commit of your work is EM remit" in result["message"]


def test_fires_on_tell_c_misattribution(repo):
    t = _transcript(
        repo,
        "I'm holding that dispatch on your standing don't-call-the-Agent-tool "
        "instruction.",
    )
    result = m.op(_payload(repo, t))
    assert result is not None
    assert "Dispatch/commit of your work is EM remit" in result["message"]


def test_fires_on_tell_d_commit_permission(repo):
    t = _transcript(repo, "Should I stage and commit these changes now?")
    result = m.op(_payload(repo, t))
    assert result is not None
    assert "Dispatch/commit of your work is EM remit" in result["message"]


def test_fires_at_most_once_per_session(repo):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    assert m.op(_payload(repo, t)) is not None
    assert m.op(_payload(repo, t)) is None


def test_stop_hook_active_never_fires(repo):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    assert m.op(_payload(repo, t, stop_hook_active=True)) is None


def test_subagent_stop_never_fires(repo):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    assert m.op(_payload(repo, t, agent_id="agent-1")) is None


def test_env_hatch_silences(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_HARNESS_DIRECTIVE_NUDGE_OFF", "1")
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    assert m.op(_payload(repo, t)) is None


def test_missing_transcript_is_silent(repo):
    assert m.op(_payload(repo, str(repo / "nope.jsonl"))) is None


def test_non_dict_payload_is_silent():
    assert m.op(["not", "a", "dict"]) is None
    assert m.op(None) is None
    assert m.op("also not a dict") is None


def test_missing_session_id_warns_and_may_repeat(repo):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    payload = _payload(repo, t)
    del payload["session_id"]
    result = m.op(payload)
    assert result is not None
    assert "invocation-scoped" in result["message"]


def test_worktree_style_git_file_resolves_sentinel_root(tmp_path):
    outer = tmp_path / "outer"
    os.makedirs(outer / ".git")
    inner = outer / "worktree"
    os.makedirs(inner)
    real_git = tmp_path / "elsewhere"
    os.makedirs(real_git)
    (inner / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    t = _transcript(inner, "Want me to dispatch an executor for this?")
    payload = _payload(inner, t)
    sentinel = m._sentinel_path(payload)
    assert sentinel is not None
    # The inner `.git` FILE wins over the ancestor `.git` DIRECTORY (the
    assert sentinel.startswith(str(real_git))
    assert m.op(payload) is not None
    assert m.op(payload) is None


def test_ordinary_final_turn_is_silent(repo):
    t = _transcript(repo, "Want me to dispatch a reviewer?", "Dispatched; verdict OK.")
    assert m.op(_payload(repo, t)) is None


def _git_init(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _write_dispatched_agents(repo, session_id, content):
    d = repo / ".git" / "coordinator-sessions" / session_id
    os.makedirs(d, exist_ok=True)
    (d / "dispatched-agents.txt").write_text(content, encoding="utf-8")


def test_fires_even_when_dispatched_agents_file_present_and_nonempty(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo)
    _git_init(repo)
    session_id = "sess-dispatched"
    _write_dispatched_agents(repo, session_id, "abcdef012345\tsonnet\texecutor\t1234567890\n")
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    result = m.op(_payload(repo, t, session_id=session_id))
    assert result is not None


def test_fires_when_dispatched_agents_file_absent(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo)
    _git_init(repo)
    session_id = "sess-no-dispatch"
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    result = m.op(_payload(repo, t, session_id=session_id))
    assert result is not None


def test_fires_when_dispatched_agents_file_present_but_empty(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo)
    _git_init(repo)
    session_id = "sess-empty-dispatch"
    _write_dispatched_agents(repo, session_id, "")
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    result = m.op(_payload(repo, t, session_id=session_id))
    assert result is not None


def test_dispatch_evidence_in_worktree_common_dir_still_does_not_suppress(tmp_path):
    main_repo = tmp_path / "main"
    os.makedirs(main_repo)
    _git_init(main_repo)

    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt)],
        cwd=main_repo,
        check=True,
        **no_console_passthrough_kwargs(),
    )

    session_id = "sess-worktree-dispatch"
    _write_dispatched_agents(main_repo, session_id, "abcdef012345\tsonnet\texecutor\t1234567890\n")

    t = _transcript(wt, "Want me to dispatch an executor for this?")
    result = m.op(_payload(wt, t, session_id=session_id))
    assert result is not None


def test_last_assistant_text_skips_tool_only_turns(repo):
    path = repo / "t.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "spoken"}]}}
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
                }
            )
            + "\n"
        )
    assert m.last_assistant_text(str(path)) == "spoken"


def test_last_assistant_text_handles_malformed_lines(repo):
    path = repo / "t.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps({"type": "assistant", "message": {"content": "plain"}}) + "\n")
    assert m.last_assistant_text(str(path)) == "plain"


def test_no_git_root_degrades_without_sentinel(tmp_path):
    t = _transcript(tmp_path, "Want me to dispatch an executor?")
    assert m.op(_payload(tmp_path, t)) is not None
    assert m.op(_payload(tmp_path, t)) is not None


def test_last_assistant_text_non_dict_message_does_not_raise(repo):
    path = repo / "t.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "message": "not-a-dict"}) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"content": "plain"}}) + "\n")
    assert m.last_assistant_text(str(path)) == "plain"


def test_last_assistant_text_handles_large_transcript(repo):
    path = repo / "big.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 200}})
    with open(path, "w", encoding="utf-8") as fh:
        for _ in range(4000):
            fh.write(filler + "\n")
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "final spoken turn"}]},
                }
            )
            + "\n"
        )
    assert os.path.getsize(path) > 512_000
    assert m.last_assistant_text(str(path)) == "final spoken turn"


def test_claim_fire_is_atomic(tmp_path):
    sentinel = str(tmp_path / "sub" / "harness-directive-nudge.fired")
    assert m._claim_fire(sentinel) is True
    assert m._claim_fire(sentinel) is False


def test_last_assistant_message_is_preferred_over_transcript(repo):
    payload = _payload(
        repo,
        str(repo / "does-not-exist.jsonl"),
        last_assistant_message="Want me to dispatch a reviewer for this?",
    )
    assert m.op(payload) is not None


def test_falls_back_to_transcript_when_field_absent(repo):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    assert m.op(_payload(repo, t)) is not None


@pytest.mark.parametrize("bad_field", ["   ", ["nope"], 7, None])
def test_falls_back_when_field_is_blank_or_wrong_type(repo, bad_field):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    payload = _payload(
        repo, t, session_id=f"sess-{abs(hash(str(bad_field)))}", last_assistant_message=bad_field
    )
    assert m.op(payload) is not None


def test_no_transcript_and_no_field_is_silent(repo):
    payload = {"session_id": "s", "cwd": str(repo), "stop_hook_active": False}
    assert m.op(payload) is None


def test_worktree_gitdir_pointer_allows_sentinel_write(tmp_path):
    real_git = tmp_path / "realgit"
    os.makedirs(real_git)
    wt = tmp_path / "wt"
    os.makedirs(wt)
    with open(wt / ".git", "w", encoding="utf-8") as fh:
        fh.write(f"gitdir: {real_git}\n")

    payload = {
        "session_id": "wt-sess",
        "cwd": str(wt),
        "stop_hook_active": False,
        "last_assistant_message": "Want me to dispatch an executor?",
    }
    assert m.op(payload) is not None
    assert os.path.exists(
        real_git / "coordinator-sessions" / "wt-sess" / "harness-directive-nudge.fired"
    )
    assert m.op(payload) is None


def test_relative_gitdir_pointer_resolves(tmp_path):
    os.makedirs(tmp_path / "realgit")
    wt = tmp_path / "wt"
    os.makedirs(wt)
    with open(wt / ".git", "w", encoding="utf-8") as fh:
        fh.write("gitdir: ../realgit\n")
    payload = {
        "session_id": "rel-sess",
        "cwd": str(wt),
        "stop_hook_active": False,
        "last_assistant_message": "Should I delegate this to a subagent?",
    }
    assert m.op(payload) is not None
    assert m.op(payload) is None


def test_unparseable_git_file_degrades_silently(tmp_path):
    wt = tmp_path / "wt"
    os.makedirs(wt)
    with open(wt / ".git", "w", encoding="utf-8") as fh:
        fh.write("this is not a gitdir pointer\n")
    payload = {
        "session_id": "bad-sess",
        "cwd": str(wt),
        "stop_hook_active": False,
        "last_assistant_message": "Want me to dispatch an executor?",
    }
    assert m.op(payload) is not None


def test_nudge_message_carries_no_bare_decision_record_id(tmp_path):
    wt = tmp_path
    os.makedirs(wt / ".git")
    payload = {
        "session_id": "pd-sess",
        "cwd": str(wt),
        "stop_hook_active": False,
        "last_assistant_message": "Want me to dispatch an executor?",
    }
    result = m.op(payload)
    assert result is not None
    assert not re.search(r"\bDR-\d+\b", result["message"])


def test_tell_c_catches_scope_possessive_attribution():
    assert m.message_trips_tell(
        "But this session's standing instruction is do not call the Agent tool "
        "unless the user requested it, so I did not dispatch the reviewers."
    )


def test_scope_possessive_does_not_trip_on_unrelated_session_prose():
    assert not m.message_trips_tell("This session's scope is the install surface.")
    assert not m.message_trips_tell(
        "Per your standing instruction to keep PRs under 300 lines, I split it."
    )


def _write_transcript(tmp_path, entries):
    path = tmp_path / "transcript.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return str(path)


def _ask_entry(question, label="Authorize the dispatch", description="You lift the rule."):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": question,
                                "header": "Review",
                                "options": [{"label": label, "description": description}],
                            }
                        ]
                    },
                }
            ]
        },
    }


def test_permission_ask_via_askuserquestion_is_visible_to_the_tells(tmp_path):
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "execute"}},
            _ask_entry(
                "The ceremony mandates a review, but this session's standing instruction "
                "is not to dispatch. Should I dispatch the reviewers?"
            ),
        ],
    )
    text = m._final_message_text({"transcript_path": transcript})
    assert text, "tool_use-only turn must not resolve to empty"
    assert m.message_trips_tell(text)


def test_askuserquestion_harvest_stops_at_the_turn_boundary(tmp_path):
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "execute"}},
            _ask_entry("Should I dispatch the reviewers?"),
            {"type": "user", "message": {"content": "next turn"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        ],
    )
    assert m.ask_user_question_text(transcript) == ""


def test_askuserquestion_harvest_is_fail_silent_on_a_bad_path():
    assert m.ask_user_question_text("/nonexistent/transcript.jsonl") == ""


def test_spoken_text_still_wins_when_both_channels_are_present(tmp_path):
    """The harvest AUGMENTS the scanned corpus; it does not replace spoken text."""
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "go"}},
            _ask_entry("Should I dispatch the reviewers?"),
        ],
    )
    text = m._final_message_text(
        {"transcript_path": transcript, "last_assistant_message": "Here is the plan."}
    )
    assert "Here is the plan." in text
    assert "dispatch the reviewers" in text


def test_scope_possessive_does_not_trip_on_a_budget_noun_phrase():
    """Regression for the review finding on the scope-possessive widening.

    "instruction budget" is a QUANTITY, not an authored rule, and the vocabulary
    is native here — CLAUDE.md itself says "invocation budget" and "spawn-count
    budget". The prior negative tests only covered "no dispatch term" and "no
    rule-noun"; this covers the harder case the reviewer named: all three
    components genuinely co-occur in one sentence, but the topic is token spend.
    """
    assert not m.message_trips_tell(
        "This session's instruction budget is limited, so I didn't dispatch "
        "redundant reviewers."
    )
    assert not m.message_trips_tell(
        "The conversation's directive count is capped, so I did not spawn more agents."
    )


def test_scope_possessive_still_fires_when_the_rule_noun_is_the_head():
    assert m.message_trips_tell(
        "But this session's standing instruction is do not call the Agent tool, "
        "so I did not dispatch the reviewers."
    )


def test_scope_possessive_does_not_trip_when_the_quantity_word_precedes():
    """Second review pass: the first tightening only excluded a quantity word
    that FOLLOWS the rule-noun ("instruction budget"). A quantity word used as a
    MODIFIER ahead of it still tripped — and "spawn-count budget" is CLAUDE.md's
    own phrasing, so this order is at least as likely as the other."""
    assert not m.message_trips_tell(
        "This session's spawn-count budget policy doesn't allow another dispatch, "
        "so I didn't spawn one."
    )
