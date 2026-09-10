"""Check 14 -- undeclared-staged-deletion.

Guards the P0 recorded at ``state/bug-backlog/2026-08-31-four-bug-blitz-
commits-deleted-five-file-6216c89502b9.yaml``: four commits from one bug-blitz
run each landed a pure deletion under a subject describing a fix that was
nowhere in its own diff, and nothing anywhere reported a problem because the
working tree kept functioning against a copy git did not have.

MEASUREMENT THIS PREDICATE RESTS ON, and it should be re-run by anyone holding
the full history. Over the 699 commits reachable from the branch this check was
built on, 11 carry a staged ``D``, and the full-message verb scan below fires on
0 of them -- i.e. zero false positives, with each of the 11 declaring its removal
somewhere in its message. A SUBJECT-ONLY scan (what the bug row originally
proposed) fires on 8 of the 11, all 8 legitimate. The four accident commits
themselves (``8730aeb007``, ``06a05dae48``, ``f6500605ca``, ``b3e96db623``) and
the three rides-along victims the row's REFUTED block names (``d721e7b3e1``,
``e3f53f3d6c``, ``9822a595ff``) are NOT reachable from that branch, so the
true-positive side is reasoned from their recorded subjects rather than measured.
Re-measure with::

    git log -n 1500 --format=%H | while read s; do
      git show --format= --name-status -M "$s" | grep -q '^D\\t' || continue
      git log -1 --format=%B "$s" | grep -Eiq \\
        '\\b(delet|remov|retir|drop|gravestone|prun|purg|kill|untrack|discard)' \\
        || echo "FIRES $s"
    done

Note the row's OWN first-proposed predicate (``insertions == 0 AND
deletions > 0``) is deliberately NOT what is implemented -- the row's later
REFUTED block measured it wrong and named two counter-examples where the
deletion rode along inside a commit carrying insertions.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.commit_tripwires import (
    _commit_message_from_tokens,
    _staged_deletions,
    check_undeclared_staged_deletion,
)


def _tokens(*args: str):
    return ["git", "commit", *args]


# ---------------------------------------------------------------------------
# The incident shape itself.
# ---------------------------------------------------------------------------


def test_fires_on_the_recorded_incident_shape():
    """8730aeb007's real subject, against its real deletion."""
    detail = check_undeclared_staged_deletion(
        _tokens(
            "-m",
            "bug-blitz: git.maintenance and handoff.repair_deployment_state "
            "reach OP_CLASSIFICATION",
        ),
        ["D\tcoordinator_core/authz/classification.py"],
    )
    assert detail is not None
    assert "UNDECLARED STAGED DELETION" in detail
    assert "coordinator_core/authz/classification.py" in detail
    assert "removes 1 tracked file(s)" in detail


def test_fires_when_the_deletion_rides_along_with_insertions():
    """The REFUTED block's correction: an ``insertions == 0`` predicate misses
    this, so the implementation must not carry one. The status list here holds
    modifications alongside the deletion, exactly as ``d721e7b3e1`` did."""
    detail = check_undeclared_staged_deletion(
        _tokens("-m", "week-changelog: [backfill] daily block 2026-08-18"),
        [
            "M\tstate/week-changelog/2026-08-18.md",
            "A\tstate/week-changelog/2026-08-19.md",
            "D\tbin/tests/test_claude_klabauter_doctor_eol_rider_host_suspension.py",
        ],
    )
    assert detail is not None
    assert "test_claude_klabauter_doctor_eol_rider_host_suspension.py" in detail


def test_counts_and_truncates_a_large_deletion_set():
    dels = ["D\tstate/handoffs/h%02d.md" % i for i in range(13)]
    detail = check_undeclared_staged_deletion(
        _tokens("-m", "sizing: the percolate rebuild is its own L"), dels
    )
    assert detail is not None
    assert "removes 13 tracked file(s)" in detail
    assert "... and 3 more" in detail


# ---------------------------------------------------------------------------
# Declared removals stay silent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "delete op_latency's unread _IS_WINDOWS",
        "C3: retire nine empty supersede scaffolds",
        "Untrack engine-provenance-counts.jsonl so its gitignore entry can work",
        "memo(outbox): discard the staged draft, hand-delivered instead",
        "quarantine the environment-answered default suite-wide",
        "gravestone the falsifier-compare op",
        "drop the dead branch",
        "K-046: kill ceremony.wsc_tail",
        "prune stale sidecars",
        "revert 4f38c3f163",
    ],
)
def test_silent_when_the_message_declares_the_removal(message):
    assert (
        check_undeclared_staged_deletion(
            _tokens("-m", message), ["D\tsome/path.py"]
        )
        is None
    )


def test_verb_is_found_in_the_body_not_only_the_subject():
    """b9a1aefc9e's real shape: the subject says ``repair the frontmatter``
    and only the body says ``deleted``. A subject-only scan false-positives
    here, which is why the scan reads the whole message."""
    assert (
        check_undeclared_staged_deletion(
            _tokens(
                "-m",
                "workweek-complete Step 3: repair the frontmatter the schema "
                "has slots for",
                "-m",
                'Junk stubs plan1.md ("world") and p.md ("problem") deleted.',
            ),
            ["D\tdocs/plans/plan1.md", "D\tdocs/problems/p.md"],
        )
        is None
    )


# ---------------------------------------------------------------------------
# Renames are not deletions -- the archive path must never fire.
# ---------------------------------------------------------------------------


def test_git_mv_archive_closure_does_not_fire():
    """Every queue closure in this codebase is a ``git mv`` to ``archive/``.
    Under ``-M`` those report ``R``, and a guard that fired on them would be
    switched off within a day."""
    status = [
        "R100\tstate/bug-backlog/2026-07-06-a.yaml\tarchive/bug-backlog/2026-09/2026-07-06-a.yaml",
        "R096\tstate/bug-backlog/2026-07-08-b.yaml\tarchive/bug-backlog/2026-09/2026-07-08-b.yaml",
    ]
    assert _staged_deletions(status) == []
    assert (
        check_undeclared_staged_deletion(
            _tokens("-m", "bug-blitz: close two OBE rows"), status
        )
        is None
    )


def test_staged_deletions_ignores_every_non_D_status():
    status = [
        "M\ta.py",
        "A\tb.py",
        "R100\told.py\tnew.py",
        "C075\tsrc.py\tcopy.py",
        "T\tlink.py",
        "D\tgone.py",
        "",
    ]
    assert _staged_deletions(status) == ["gone.py"]


# ---------------------------------------------------------------------------
# Fail-open cases. These are the ones that decide the advisory-vs-deny call.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (),                                   # editor-composed
        ("-F", "/tmp/msg.txt"),               # message from a file
        ("--file", "/tmp/msg.txt"),
        ("-C", "HEAD~1"),                     # reused message
        ("--reuse-message", "HEAD~1"),
        ("--fixup", "abc123"),
        ("--squash", "abc123"),
        ("-t", "/tmp/tmpl"),
    ],
)
def test_fails_open_when_the_message_is_not_on_the_command_line(args):
    assert _commit_message_from_tokens(list(args)) is None
    assert (
        check_undeclared_staged_deletion(
            _tokens(*args), ["D\tcoordinator_core/authz/classification.py"]
        )
        is None
    )


def test_fails_open_when_the_commit_segment_was_not_resolved():
    assert (
        check_undeclared_staged_deletion(None, ["D\tgone.py"]) is None
    )


def test_silent_when_nothing_is_deleted():
    assert (
        check_undeclared_staged_deletion(
            _tokens("-m", "add a thing"), ["M\ta.py", "A\tb.py"]
        )
        is None
    )


# ---------------------------------------------------------------------------
# Message extraction.
# ---------------------------------------------------------------------------


def test_message_extraction_forms():
    assert _commit_message_from_tokens(["-m", "subject"]) == "subject"
    assert _commit_message_from_tokens(["--message=subject"]) == "subject"
    assert _commit_message_from_tokens(["-msubject"]) == "subject"
    assert (
        _commit_message_from_tokens(["-m", "subject", "-m", "body"])
        == "subject\n\nbody"
    )


def test_pathspec_after_double_dash_is_never_read_as_a_message():
    """``git commit -m fix -- delete_me.py`` must still fire: the word
    ``delete`` is in a PATH, not in the message."""
    detail = check_undeclared_staged_deletion(
        _tokens("-m", "fix the thing", "--", "delete_me.py"),
        ["D\tdelete_me.py"],
    )
    assert detail is not None


def test_option_values_are_not_read_as_a_message():
    """``--author "Removed Person"`` must not count as declaring a removal."""
    assert (
        _commit_message_from_tokens(["--author", "Removed Person", "-m", "fix"])
        == "fix"
    )
    assert (
        check_undeclared_staged_deletion(
            _tokens("--author", "Removed Person", "-m", "fix"), ["D\tgone.py"]
        )
        is not None
    )
