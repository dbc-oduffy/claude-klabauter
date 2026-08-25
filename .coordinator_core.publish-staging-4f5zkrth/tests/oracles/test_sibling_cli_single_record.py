"""Oracles for exemptions whose claim is "this sibling CLI takes ONE record per invocation".

Each test below is the executable form of a sentence that used to sit in `_EXEMPT_SITES` as
prose. The sentence could only ever be checked by a human re-reading it; these fail the moment
the sibling parser gains `nargs='+'` or an append action, which is exactly when the exemption
stops being true and the caller should start batching.

Bound to the sites they discharge by `_ORACLE_CLAIMS` in
`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`. Import-and-introspect only --
no subprocess, so these run on the fast tier beside the gate rather than on a slower cadence.
"""

from __future__ import annotations

import pytest

from coordinator_core.tests.oracles._sibling_cli import (
    accepts_multiple,
    actions_by_dest,
    load_bin_module,
    subparser_of,
)


def _assert_all_scalar(parser, dests: tuple[str, ...], cli: str) -> None:
    actions = actions_by_dest(parser)
    missing = [d for d in dests if d not in actions]
    assert not missing, (
        f"{cli}: expected argument dest(s) {missing} no longer exist -- this oracle is pinned to "
        "a parser that has changed shape, so the exemption it discharges is unverified. Re-read "
        "the CLI rather than deleting the assertion."
    )
    multi = [d for d in dests if accepts_multiple(actions[d])]
    assert not multi, (
        f"{cli}: {multi} now accept MULTIPLE values in one invocation. The exemption claiming "
        "one record per spawn is no longer true -- the caller should batch, and its "
        "`_ORACLE_CLAIMS` entry should be deleted rather than this test relaxed."
    )


def test_percolate_gate_scan_secrets_takes_one_target():
    """`percolate-mirror::_run_gate_legs` spawns `scan-secrets` once per row because `--target`
    selects that row's own ruleset and `--files` is that row's own list. A live 2026-08-18
    incident is on record: feeding one target's scan the whole run's file list raised HIGH-tier
    findings against other rows' sources under the wrong ruleset."""
    parser = load_bin_module("percolate-gate.py")._build_parser()
    _assert_all_scalar(
        subparser_of(parser, "scan-secrets"), ("target", "files"), "percolate-gate scan-secrets"
    )


def test_lesson_promote_takes_one_record():
    """`coordinator-harvest-deferrals::_harvest -> _run_lesson_promote`.

    Asserted over the RECORD-IDENTITY fields, not over every dest. The claim an exemption makes
    is "one record per invocation"; a field that is itself list-valued (several tags on ONE
    lesson) does not touch it. An earlier draft of this oracle tested "no dest accepts
    multiple", which is a different and wrong question -- it would have declared two live
    exemptions dead over fields that are plural WITHIN a single record."""
    parser = load_bin_module("coordinator-lesson-promote.py")._build_parser(("doctrine",))
    _assert_all_scalar(
        parser,
        ("title", "body", "change_kind", "target_wiki"),
        "coordinator-lesson-promote",
    )


def test_queue_append_takes_one_record():
    """`coordinator-harvest-deferrals::_harvest -> _run_queue_append`.

    `--deliverables`, `--specs` and `--dependency-annotations` ARE append actions, and that is
    correct: one queue entry can carry several. What makes this one record per spawn is that
    the entry's own identity fields are scalar -- there is no way to describe a second entry in
    the same invocation."""
    parser = load_bin_module("coordinator-queue-append.py")._build_parser()
    _assert_all_scalar(
        parser, ("schema", "title", "body", "status"), "coordinator-queue-append"
    )


def test_reconcile_completion_commits_takes_one_entry_path(capsys):
    """One oracle, four sites: `workday-complete-reconcile::run_completion_reconcile`,
    `workday-start-reconcile-sweep::run_sweep`, `workweek-complete-close::run_reconcile_sweep`,
    and `merge-release-notes-derive::cmd_reconcile_sweep` all shell to this same CLI once per
    entry.

    This one is not argparse -- the parser is hand-rolled inside `main()` -- so the surface is
    exercised rather than introspected. That is safe HERE and only because the arity refusal
    happens before `entry_path` is touched: no file is opened, nothing is written, and the call
    returns 1. Verified by reading `main()` rather than assumed; an entrypoint whose refusal sat
    after its first side effect would need a different oracle shape, not this one with a
    tmp_path bolted on."""
    main = load_bin_module("reconcile-completion-commits.py").main

    assert main(["entry-one.md", "entry-two.md"]) == 1, (
        "reconcile-completion-commits.py now accepts two entry paths in one invocation. Four "
        "exemptions rest on it taking exactly one -- batch those callers and delete their "
        "`_ORACLE_CLAIMS` entries."
    )
    assert "too many positional args" in capsys.readouterr().err, (
        "the two-path call returned 1 for some reason OTHER than arity. This oracle would then "
        "be passing for the wrong reason, which is worse than failing -- re-read `main()`."
    )


@pytest.mark.parametrize(
    "script, entry",
    [
        ("percolate-gate.py", "_build_parser"),
        ("coordinator-lesson-promote.py", "_build_parser"),
        ("coordinator-queue-append.py", "_build_parser"),
    ],
)
def test_oracle_targets_remain_importable(script: str, entry: str):
    """The oracles above are worth nothing if their target stops being importable -- a parser
    that cannot be loaded would turn every claim above into a skipped test, and a silently
    skipped oracle is the prose exemption again with extra steps.

    Pinned as its own assertion so an import break reads as an import break rather than as the
    CLI having changed shape."""
    module = load_bin_module(script)
    assert callable(getattr(module, entry, None)), (
        f"{script} no longer exposes a callable {entry}() -- the oracles pinned to it cannot "
        "verify anything until this is repaired."
    )
