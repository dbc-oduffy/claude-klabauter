
from coordinator_core.ops.dispatch_emit.emit import (
    _commit_agent_call,
    _preflight_agent_call,
)


def test_commit_prompt_names_every_chunk_id_in_the_wave():
    call = _commit_agent_call(
        ["a.py", "b.py"], "Commit wave 4", 3, ["C4", "C5", "C6", "C7"]
    )
    for chunk_id in ("C4", "C5", "C6", "C7"):
        assert chunk_id in call


def test_commit_prompt_states_the_subject_requirement():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "subject" in call.lower()
    assert "resumed agent" in call.lower()


def test_commit_prompt_still_names_wave_and_pathspec():
    call = _commit_agent_call(["a.py", "b.py"], "Commit wave 2", 1, ["C2"])
    assert "a.py" in call
    assert "b.py" in call
    assert "wave 2" in call


def test_absent_chunk_ids_degrade_without_emitting_an_empty_requirement():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0)
    assert "MUST register" not in call

def test_multi_chunk_example_shows_every_id_not_just_the_first():
    """Slice-D review P1, caught at workstream-complete.

    The requirement sentence comma-joined all four ids while the worked
    example showed only ``C4:`` -- so a commit agent for a 4-chunk wave had a
    plausible reading that only the first id belonged in the subject, silently
    leaving C5/C6/C7 unregistered and reproducing the very `partial` stamp
    this prompt exists to prevent. Asserting each id appears SOMEWHERE is not
    enough (the requirement sentence already contains them all) -- the
    example itself has to demonstrate the multi-id format.
    """
    ids = ["C4", "C5", "C6", "C7"]
    call = _commit_agent_call(["a.py"], "Commit wave 4", 3, ids)
    joined = ", ".join(ids)
    assert joined in call
    assert f"{joined}: <what changed>" in call
    assert "C4: <what changed>" not in call


def test_commit_prompt_carries_returning_executor_provenance():
    call = _commit_agent_call(
        ["a.py", "b.py"], "Commit wave 1", 0, ["C1"], "wave1Results"
    )
    lowered = call.lower()
    assert "provenance" in lowered
    assert "writes" in lowered
    assert "touched-files" in lowered
    assert "executor" in lowered


def test_commit_prompt_references_the_waves_captured_results_var():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 2", 1, ["C2"], "wave2Results"
    )
    assert "JSON.stringify(wave2Results" in call


def test_commit_prompt_without_results_var_keeps_the_prior_shape():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "JSON.stringify" not in call
    assert "provenance" not in call.lower()


def test_commit_prompt_escapes_a_backtick_in_the_pathspec():
    call = _commit_agent_call(
        ["weird`path.py"], "Commit wave 1", 0, ["C1"], "wave1Results"
    )
    assert "weird\\`path.py" in call
    start = call.index("await agent(`") + len("await agent(")
    end = call.index("${JSON.stringify")
    body = call[start:end]
    i = 0
    while True:
        idx = body.find("`", i)
        if idx == -1:
            break
        if idx == 0:
            i = idx + 1
            continue
        assert body[idx - 1] == "\\", f"unescaped backtick at {idx}: {body!r}"
        i = idx + 1


def test_commit_prompt_escapes_a_dollar_brace_in_the_pathspec():
    call = _commit_agent_call(
        ["${evil}.py"], "Commit wave 1", 0, ["C1"], "wave1Results"
    )
    assert "\\${evil}.py" in call


def test_commit_prompt_dynamic_report_content_never_needs_python_side_escaping():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 3", 2, ["C3"], "wave3Results"
    )
    assert "JSON.stringify(wave3Results, null, 2)" in call


def test_preflight_prompt_does_not_treat_a_clean_path_as_a_refusal():
    call = _preflight_agent_call(["a.py", "b.py"], "Preflight")
    lowered = call.lower()
    assert "expected" in lowered
    assert "not a refusal" in lowered
    assert "blocked" in lowered
    for cause in ("claim", "ignore", "guard"):
        assert cause in lowered


def test_commit_prompt_tells_the_agent_to_read_no_delta():
    """The dispatched committer is the reader that cannot see the warning.

    `coordinator-safe-commit` prints `commit_v2`'s warnings to stderr, but a
    workflow commit agent calls `commit_paths` IN-PROCESS and never crosses
    that surface -- the emitted brief is the only thing standing between it
    and the fact. It commits unattended and reports one line the orchestrating
    EM takes as delivery, so a declared path that contributed nothing is
    invisible unless the brief names the field. DoE-claude's `874cf35dd`
    (five paths declared, four landed, the fifth the point of the commit)
    happened on this path, not at a terminal.
    """
    call = _commit_agent_call(["plan.md", "a.py"], "Commit wave 1", 0, ["C1"])
    assert "no_delta" in call
    assert "contributed nothing" in call


def test_commit_prompt_scopes_no_delta_to_paths_not_deleted_paths():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], "wave1Results"
    )
    assert "PhantomDeletionDeclared" in call
    assert "deleted_paths" in call


def test_no_delta_is_reported_above_the_success_token_not_instead_of_it():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert call.index("no_delta") < call.index("COMMIT-LANDED")
