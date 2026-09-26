"""Tests for 3.1: the commit-subject worked example composes THIS wave's
own ids and titles into a literal subject, rather than the generic
``<what changed>`` placeholder.

Spec: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md (IBMDT-C4).
Source ask: cross-repo memo archive/2026-09-11-doe-claude-em-dispatch-emit-
commit-wave-halts-and-multi-plan-waves.md § 1 -- "have the emitter compose
the literal subject. It already holds every chunk id and title, e.g.
`C3, C4, C7: <title C3>; <title C4>; <title C7>`, with the rule that
dropping a non-DONE id drops its title too."
"""

from coordinator_core.ops.dispatch_emit.emit import _commit_agent_call


def test_three_chunk_wave_commit_brief_contains_the_literal_subject():
    call = _commit_agent_call(
        ["a.py", "b.py", "c.py"],
        "Commit wave 1",
        0,
        ["C3", "C4", "C7"],
        chunk_titles=[
            "Emitter composes the literal subject",
            "Batches write-capable waves at <=5",
            "Denies a subagent's heredoc-fed interpreter",
        ],
    )
    assert (
        "C3, C4, C7: Emitter composes the literal subject; Batches "
        "write-capable waves at <=5; Denies a subagent's heredoc-fed "
        "interpreter" in call
    )
    assert "<what changed>" not in call


def test_absent_titles_keep_the_prior_placeholder_shape():
    """No parallel title list -- every pre-C4 caller/test -- degrades to the
    pre-existing generic worked example, byte-identical to before this
    chunk."""
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "C1: <what changed>" in call


def test_mismatched_title_and_id_counts_degrade_to_the_placeholder():
    """A title list of the wrong length is never zipped against ids
    positionally -- that would silently mispair an id with the wrong
    row's title. Degrades to the placeholder instead."""
    call = _commit_agent_call(
        ["a.py"],
        "Commit wave 1",
        0,
        ["C1", "C2"],
        chunk_titles=["Only one title"],
    )
    assert "C1, C2: <what changed>" in call
    assert "Only one title" not in call


def test_dropping_a_non_done_id_drops_its_title_too():
    call = _commit_agent_call(
        ["a.py", "b.py"],
        "Commit wave 1",
        0,
        ["C1", "C2"],
        chunk_titles=["First fix", "Second fix"],
    )
    assert "its paths AND its title" in call
    assert "mismatched id/title pairing" in call
