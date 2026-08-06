"""Tests for coordinator_core.hooks.nudge_harness_directive_dispatch.

Covers the three things that can go wrong with a Stop-hook nudge: it fires when
it should, it stays silent when it should (precision matters more than recall
here — a false positive blocks the PM's end-of-turn), and it cannot wedge the
session in a loop.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from coordinator_core.hooks import nudge_harness_directive_dispatch as m


def _transcript(tmp_path, *assistant_texts):
    """Write a minimal transcript jsonl whose assistant turns are `assistant_texts`."""
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
    """A tmp dir that looks like a git repo, so the sentinel has somewhere to live."""
    os.makedirs(tmp_path / ".git")
    return tmp_path


# ---------------------------------------------------------------------------
# Tell detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I did not delegate because my instructions say do not call the AgentTool.",
        "I held off since the guidance is to avoid it unless the user requested it.",
        "Want me to dispatch a reviewer for this?",
        "Should I delegate the verification to a subagent?",
        "Would you like me to fan-out this across parallel agents?",
        # F2: a message that cites the suppressor-adjacent doctrine (DR-108)
        # AND trips a real tell (asking permission) must still trip — DR-108
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
        # Verbatim-shaped 2026-08-02 failure: possessive attribution + a
        # dispatch term + a restriction cue, all in one sentence.
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


@pytest.mark.parametrize(
    "text",
    [
        "Dispatched two reviewers; both came back clean.",
        "Fixed the precedence bug in resolve_root(); tests green.",
        "Should I ship this to main, or hold for the weekly gate?",
        "",
        # F3: a substring of "AgentTool" inside an unrelated identifier must
        # not trip Tell A now that it has word boundaries.
        "I renamed AgentToolkit to clarify its scope.",
        # F4: the bare "not to use workflows or deep-research" alternative was
        # dropped — this benign tool-choice statement must now stay silent.
        "My system prompt says not to use workflows or deep-research, so I did this inline.",
        # Tell C negative: a real, unrelated PM instruction — possessive
        # attribution present, but no dispatch term in the same sentence.
        "Per your standing instruction to keep PRs under 300 lines, I split "
        "this into two.",
        # Tell C negative: possessive attribution + dispatch term, but no
        # restriction cue — this reports something the PM actually asked
        # for, not a misattributed restriction.
        "As you instructed, I dispatched two reviewers already.",
        # Tell C negative: possessive attribution about an unrelated rule,
        # sharing the sentence with an unrelated dispatch mention elsewhere
        # in the turn must not bleed across sentences.
        "Your rule about commit messages is clear, so I followed it. "
        "Also dispatched a reviewer for the diff.",
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
        # Genuine PM gate: merge-to-main requires the literal keyword and
        # must never be flagged.
        "Shall I merge to main?",
        "Ready to merge?",
        "/merging-to-main?",
        # Outward-facing action: pushing to a shared remote / opening a PR
        # is a correct ask-before-external-action, never this tell.
        "Should I push this to the remote?",
        "Want me to open a PR for this?",
        "Should I push and open a PR now?",
        # Reporting an already-completed commit is not an ask.
        "Committed as abc1234.",
        "Staged and committed the changes.",
        # Scoping questions about commit CONTENTS, not permission to commit.
        "Which files should I include in this commit?",
        "This lessons file looks out of scope — should I leave it out of the commit?",
        # A sentence combining a commit verb with an outward/gated cue must
        # be treated as the correct outward/gated ask, not this tell.
        "Should I commit and then push this to the remote?",
    ],
)
def test_tell_d_commit_permission_ask_does_not_trip(text):
    assert m.message_trips_tell(text) is False


def test_meta_discussion_suppressed():
    """A session ABOUT this mechanism must not nudge itself."""
    text = "The AgentTool line comes from tengu_heron_brook; see harness-directive-conflicts.md."
    assert m.message_trips_tell(text) is False


# ---------------------------------------------------------------------------
# op() gating
# ---------------------------------------------------------------------------


def test_fires_on_tell(repo):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    result = m.op(_payload(repo, t))
    assert result is not None
    assert "Dispatch/commit of your work is EM remit" in result["message"]


def test_fires_on_tell_c_misattribution(repo):
    """A3: the op fires end-to-end on the misattribution tell, not only via
    message_trips_tell in isolation."""
    t = _transcript(
        repo,
        "I'm holding that dispatch on your standing don't-call-the-Agent-tool "
        "instruction.",
    )
    result = m.op(_payload(repo, t))
    assert result is not None
    assert "Dispatch/commit of your work is EM remit" in result["message"]


def test_fires_on_tell_d_commit_permission(repo):
    """The op fires end-to-end on the commit-permission tell, not only via
    message_trips_tell in isolation."""
    t = _transcript(repo, "Should I stage and commit these changes now?")
    result = m.op(_payload(repo, t))
    assert result is not None
    assert "Dispatch/commit of your work is EM remit" in result["message"]


def test_fires_at_most_once_per_session(repo):
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    assert m.op(_payload(repo, t)) is not None
    assert m.op(_payload(repo, t)) is None


def test_stop_hook_active_never_fires(repo):
    """Loop guard: a Stop caused by a hook block must not re-arm the block."""
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
    """F9: op() must not raise when handed a non-dict payload."""
    assert m.op(["not", "a", "dict"]) is None
    assert m.op(None) is None
    assert m.op("also not a dict") is None


def test_missing_session_id_warns_and_may_repeat(repo):
    """F7: no session_id -> PID-scoped sentinel; the nudge says so."""
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    payload = _payload(repo, t)
    del payload["session_id"]
    result = m.op(payload)
    assert result is not None
    assert "invocation-scoped" in result["message"]


def test_worktree_style_git_file_resolves_sentinel_root(tmp_path):
    """F5: a `.git` FILE (worktree/submodule) must be treated as the repo root,
    not skipped in favor of an ancestor `.git` directory."""
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
    # root-resolution half of F5), and the gitdir: pointer is then followed to
    # a real directory so the sentinel can actually be written — without that
    # second half, fire-once silently degraded to fire-every-time here.
    assert sentinel.startswith(str(real_git))
    assert m.op(payload) is not None
    assert m.op(payload) is None


def test_ordinary_final_turn_is_silent(repo):
    t = _transcript(repo, "Want me to dispatch a reviewer?", "Dispatched; verdict OK.")
    assert m.op(_payload(repo, t)) is None


# ---------------------------------------------------------------------------
# Dispatch-evidence suppression — a non-empty dispatched-agents.txt (written by
# track_dispatched_agents.py) falsifies the "EM has not dispatched" premise.
# ---------------------------------------------------------------------------


def _git_init(repo):
    """Initialise a minimal committed git repo at repo — needed so git_common_dir
    (a real `git rev-parse` subprocess call) resolves instead of raising."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _write_dispatched_agents(repo, session_id, content):
    d = repo / ".git" / "coordinator-sessions" / session_id
    os.makedirs(d, exist_ok=True)
    (d / "dispatched-agents.txt").write_text(content, encoding="utf-8")


def test_suppressed_when_dispatched_agents_file_present_and_nonempty(tmp_path):
    repo = tmp_path / "repo"
    os.makedirs(repo)
    _git_init(repo)
    session_id = "sess-dispatched"
    _write_dispatched_agents(repo, session_id, "abcdef012345\tsonnet\texecutor\t1234567890\n")
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    result = m.op(_payload(repo, t, session_id=session_id))
    assert result is None


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


def test_suppression_reads_git_common_dir_not_worktree_local(tmp_path):
    """A worktree session's dispatch evidence lives under the MAIN repo's
    .git, not the worktree-local one — the check must follow git_common_dir,
    not this module's worktree-local `_resolve_git_dir` walk."""
    main_repo = tmp_path / "main"
    os.makedirs(main_repo)
    _git_init(main_repo)

    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt)],
        cwd=main_repo,
        check=True,
    )

    session_id = "sess-worktree-dispatch"
    # Evidence lives under the MAIN repo's .git/coordinator-sessions/, as
    # track_dispatched_agents.py (keyed off git_common_dir) actually writes it.
    _write_dispatched_agents(main_repo, session_id, "abcdef012345\tsonnet\texecutor\t1234567890\n")

    t = _transcript(wt, "Want me to dispatch an executor for this?")
    result = m.op(_payload(wt, t, session_id=session_id))
    assert result is None


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def test_last_assistant_text_skips_tool_only_turns(repo):
    """The final SPOKEN turn is the target, not a trailing tool_use-only entry."""
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
    """No .git to hang a sentinel on: still fire (repeat beats silence)."""
    t = _transcript(tmp_path, "Want me to dispatch an executor?")
    assert m.op(_payload(tmp_path, t)) is not None
    assert m.op(_payload(tmp_path, t)) is not None


def test_last_assistant_text_non_dict_message_does_not_raise(repo):
    """F1: a truthy non-dict `message` (e.g. a bare string) must not raise."""
    path = repo / "t.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "message": "not-a-dict"}) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"content": "plain"}}) + "\n")
    assert m.last_assistant_text(str(path)) == "plain"


def test_last_assistant_text_handles_large_transcript(repo):
    """F8: a >512KB transcript must still locate the final spoken turn via the
    binary-mode tail seek, discarding the partial line it lands inside."""
    path = repo / "big.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 200}})
    with open(path, "w", encoding="utf-8") as fh:
        # Pad well past the 512KB tail window.
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


# ---------------------------------------------------------------------------
# Sentinel atomicity
# ---------------------------------------------------------------------------


def test_claim_fire_is_atomic(tmp_path):
    """F6: two concurrent claims for the same sentinel must not both win."""
    sentinel = str(tmp_path / "sub" / "harness-directive-nudge.fired")
    assert m._claim_fire(sentinel) is True
    assert m._claim_fire(sentinel) is False


# ---------------------------------------------------------------------------
# Harness-supplied last_assistant_message (primary path) vs transcript fallback
# ---------------------------------------------------------------------------


def test_last_assistant_message_is_preferred_over_transcript(repo):
    """The harness hands us the text; the transcript must not be touched at all."""
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
    """A distinct session per case — otherwise fire-once masks the second call."""
    t = _transcript(repo, "Want me to dispatch an executor for this?")
    payload = _payload(
        repo, t, session_id=f"sess-{abs(hash(str(bad_field)))}", last_assistant_message=bad_field
    )
    assert m.op(payload) is not None


def test_no_transcript_and_no_field_is_silent(repo):
    payload = {"session_id": "s", "cwd": str(repo), "stop_hook_active": False}
    assert m.op(payload) is None


# ---------------------------------------------------------------------------
# Worktree / submodule: `.git` is a FILE holding a gitdir: pointer
# ---------------------------------------------------------------------------


def test_worktree_gitdir_pointer_allows_sentinel_write(tmp_path):
    """Fire-once must hold in a worktree — nothing can be created under a FILE."""
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
    # The sentinel landed in the REAL git dir, so the second call is suppressed.
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
    # No resolvable sentinel home -> no-sentinel degradation: fires, may repeat.
    assert m.op(payload) is not None


def test_nudge_message_carries_no_bare_decision_record_id(tmp_path):
    """The message ships to OSS installs, where a bare DR id names an unopenable doc."""
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
