"""An emitted commit prompt must name the chunk-ids its subject has to register.

Spec backlink:
    docs/plans/2026-08-19-the-held-guard-cohort-becomes-dialect-safe.md
    (execution residual, 2026-08-19) — measured, not hypothesised: run
    ``wf_03c57a1b-c23`` executed all nine chunks and every commit agent carried
    the correct ``Deliverable-Id:`` trailer, yet ``close-out-and-stamp``
    reported ``open_chunk_ids: [C1, C2, C4, C5, C6, C7, C8, C9]`` and stamped
    the plan ``partial``.

    ``close-out-and-stamp`` joins a commit to a plan chunk on TWO legs: the
    ``Deliverable-Id:`` trailer AND a subject registering the chunk-id. The
    emitted prompt said "Commit wave 2's work", so the agents wrote
    wave-scoped subjects ("wave 2: update bash_guards dialect detection",
    "guard(wave4): ..."). Leg 2 never matched. The operator had to re-derive
    the chunk-to-commit mapping by hand from the log and write eight
    ``disposition_ref`` entries into the spine — for a run in which nothing
    actually failed.

    This is the failure signature the execute-plan skill names explicitly:
    "``missing_chunk_ids`` at exit 0 over a range that provably holds every
    chunk SHA — means missing trailers, not a range problem."

Negative-spec: the commit prompt must never be emitted naming only the wave
index and pathspec when the wave's rows are available to the call site.
"""

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
    assert "partial" in call.lower()


def test_commit_prompt_still_names_wave_and_pathspec():
    """The chunk-id requirement is additive — it must not displace the
    pathspec, which is what the commit agent refuses without."""
    call = _commit_agent_call(["a.py", "b.py"], "Commit wave 2", 1, ["C2"])
    assert "a.py" in call
    assert "b.py" in call
    assert "wave 2" in call


def test_absent_chunk_ids_degrade_without_emitting_an_empty_requirement():
    """Back-compat: the parameter is optional, and omitting it must not emit a
    dangling 'MUST register the chunk id(s): ' with nothing after it."""
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
    # The example must be the full list, never a bare leading single id.
    assert f"{joined}: <what changed>" in call
    assert "C4: <what changed>" not in call


def test_commit_prompt_carries_returning_executor_provenance():
    """git-commit-agent.md § Pathspec provenance refuses a pathspec sourced
    from a plan chunk's `writes:` declaration or an EM tree survey -- only a
    returning executor's own touched-files set is acceptable. The emitted
    prompt must say both: the pathspec is the wave's declared scope, AND
    the executor report(s) below are the returning executor(s)' own
    touched-files set, so the committer can verify one against the other."""
    call = _commit_agent_call(
        ["a.py", "b.py"], "Commit wave 1", 0, ["C1"], "wave1Results"
    )
    lowered = call.lower()
    assert "provenance" in lowered
    assert "writes" in lowered
    assert "touched-files" in lowered
    assert "executor" in lowered


def test_commit_prompt_references_the_waves_captured_results_var():
    """The prompt must actually splice the wave's captured executor return
    value in, not just talk about provenance in the abstract -- otherwise
    the committer still has nothing but the pathspec to trust."""
    call = _commit_agent_call(
        ["a.py"], "Commit wave 2", 1, ["C2"], "wave2Results"
    )
    assert "JSON.stringify(wave2Results" in call


def test_commit_prompt_without_results_var_keeps_the_prior_shape():
    """Back-compat: a caller not threading a results var (or a fixture that
    predates this chunk) must still emit a valid single-quoted prompt, not a
    dangling template-literal reference to an unbound variable."""
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "JSON.stringify" not in call
    assert "provenance" not in call.lower()


def test_commit_prompt_escapes_a_backtick_in_the_pathspec():
    """The static prompt text (built from Python-known data such as the
    pathspec) is spliced into a JS template literal once a results_var is
    present -- a literal backtick in that data must not be able to close
    the template literal early."""
    call = _commit_agent_call(
        ["weird`path.py"], "Commit wave 1", 0, ["C1"], "wave1Results"
    )
    assert "weird\\`path.py" in call
    # No unescaped backtick may appear between the opening and the
    # `${JSON.stringify` interpolation -- every backtick in that span must
    # be immediately preceded by a backslash.
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
    """A literal `${` in Python-known static text must not open a second,
    unintended interpolation inside the template literal."""
    call = _commit_agent_call(
        ["${evil}.py"], "Commit wave 1", 0, ["C1"], "wave1Results"
    )
    assert "\\${evil}.py" in call


def test_commit_prompt_dynamic_report_content_never_needs_python_side_escaping():
    """The executor report itself is a RUNTIME JS value (the return of
    `await agent(...)`/`await parallel(...)` in the emitted script) --
    never a string this Python module has in hand at emit time. Safety
    against a report containing a backtick/`${...}`/quote therefore comes
    from routing it through `JSON.stringify` at runtime (template-literal
    interpolation splices in the resulting VALUE, never re-parsing it as
    source), not from any Python-side string transform on report content
    this module never sees."""
    call = _commit_agent_call(
        ["a.py"], "Commit wave 3", 2, ["C3"], "wave3Results"
    )
    assert "JSON.stringify(wave3Results, null, 2)" in call


def test_preflight_prompt_does_not_treat_a_clean_path_as_a_refusal():
    """Slice-D review P2b: the preflight-wording fix shipped untested.

    Before it, preflight asked whether each path was "claimable and
    committable"; agents answered with `git status --porcelain`, which is
    empty before any chunk has run, so every plan reported BLOCKED with every
    path named as refused. A regression restoring that reading would other-
    wise pass every test in the repo.
    """
    call = _preflight_agent_call(["a.py", "b.py"], "Preflight")
    lowered = call.lower()
    assert "expected" in lowered
    assert "not a refusal" in lowered
    # BLOCKED must still be reachable for a genuine refusal cause.
    assert "blocked" in lowered
    for cause in ("claim", "ignore", "guard"):
        assert cause in lowered
