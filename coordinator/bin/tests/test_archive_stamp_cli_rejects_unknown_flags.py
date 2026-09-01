"""test_archive_stamp_cli_rejects_unknown_flags.py — a silently-dropped flag is a
partial write reported as a full one.

`archive-stamp-cli` hand-slices `argv` per verb: each branch reads the positionals
it wants (`rest[0]`, `rest[1]`) and scans order-independently for its own flags.
Nothing ever looked at what was left over, so

    archive-stamp-cli repark-handoff <path> --gate-note "why this is parked"

reparked the handoff, discarded the note, and exited 0. The caller is told the
write succeeded; the part they cared about is gone, and nothing on disk records
that it was ever asked for. That is what this refuses — not typos.

THE ACCEPTED SET IS DERIVED FROM `_SUBCOMMAND_USAGE`, never hand-listed here or in
the CLI. That table is already the declared contract, and a second copy would go
stale the first time a verb gained a flag. The parity test below pins that
derivation so the guard cannot quietly decay into a stale allowlist.

Run: pytest coordinator/bin/tests/test_archive_stamp_cli_rejects_unknown_flags.py -q
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def cli():
    """Load the CLI by location. `_reject_unknown_flags` runs before the engine
    import, so no engine resolution is needed to exercise it."""
    if str(_BIN_DIR / "lib") not in sys.path:
        sys.path.insert(0, str(_BIN_DIR / "lib"))
    spec = importlib.util.spec_from_file_location(
        "_archive_stamp_cli_under_test", _BIN_DIR / "archive-stamp-cli.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_reported_case_is_refused(cli, capsys):
    """`repark-handoff --gate-note` — the invocation that lost a note and said OK."""
    rc = cli._reject_unknown_flags("repark-handoff", ["some/path.md", "--gate-note", "why"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--gate-note" in err
    assert "usage: archive-stamp-cli repark-handoff" in err


@pytest.mark.parametrize(
    ("subcmd", "argv"),
    [
        ("repark-handoff", ["p.md"]),
        ("claim-handoff", ["p.md"]),
        ("close-handoff", ["p.md", "--reason", "stale"]),
        ("gate-recheck-handoff", ["p.md", "2026-09-01", "--cleared"]),
        ("ship-handoff", ["p.md", "--sha", "abc1234", "--archive", "--force"]),
        ("chain-archive-handoff", ["p.md", "--exclude", "a.md", "--exclude", "b.md"]),
        ("correct-handoff-body", ["p.md", "--old-string", "a", "--new-string", "b"]),
    ],
)
def test_declared_flags_still_pass(cli, subcmd, argv):
    """Every flag a verb's own usage line declares must survive the guard.

    This is the half that would break real callers, so it carries more cases than
    the refusal half.
    """
    assert cli._reject_unknown_flags(subcmd, argv) is None


def test_a_value_may_look_like_a_flag(cli):
    """The token after a recognized flag is that flag's VALUE, not a flag.

    Without the skip, `--reason --weird` would refuse instead of passing `--weird`
    through as the reason text.
    """
    assert cli._reject_unknown_flags("close-handoff", ["p.md", "--reason", "--weird"]) is None


@pytest.mark.parametrize("subcmd", ["unclaim-handoff", "unconsume-handoff"])
def test_free_text_positional_verbs_stay_exempt(cli, subcmd):
    """`unclaim-handoff <path> [note]` takes free-form prose positionally.

    A note beginning with `--` is indistinguishable from an unknown flag, and both
    verbs' usage lines already document that collision as pre-existing. Refusing
    here would CHANGE documented behaviour rather than restore intended behaviour,
    which is not this guard's job.
    """
    assert cli._reject_unknown_flags(subcmd, ["p.md", "--not-a-real-flag"]) is None


def test_help_is_answered_upstream_not_by_this_guard(cli):
    """Pins the ORDERING that makes a help union unnecessary here.

    The guard originally unioned the help flags into its accepted set. That was
    dead: `main()` early-returns on every help form before reaching the guard, so
    a help flag cannot arrive. Unioning them pinned a branch nothing exercises
    (Kira, 2026-08-31). What actually needs pinning is the ordering itself — if a
    later edit moved the guard ahead of the help early-return, `--help` would
    start printing a usage error instead of usage.
    """
    src = inspect.getsource(cli.main)
    help_return = src.index("_SUBCOMMAND_USAGE[subcmd]")
    guard_call = src.index("_reject_unknown_flags(subcmd, rest)")
    assert help_return < guard_call, (
        "main() must answer help BEFORE _reject_unknown_flags runs; the guard "
        "deliberately does not accept help flags itself"
    )


def test_help_actually_answers_through_main(cli, capsys):
    """The BEHAVIOURAL half, restored after review-integrator escalated that the
    structural test above can pass for the wrong reason.

    The ordering assertion greps two literals out of `main()`'s source and
    compares their positions. A rename raises `ValueError` (loud, fine), but a
    refactor that moves the help check into a helper while leaving both literals
    in their original relative positions passes while proving nothing. This
    exercises the actual control flow instead: `--help` must return 0 and print
    usage, whatever the source looks like.

    Both are kept. The structural one pins the ordering cheaply and names the
    invariant; this one is the check that cannot be satisfied by coincidence.
    """
    rc = cli.main(["repark-handoff", "p.md", "--help"])
    assert rc == 0
    assert "usage: archive-stamp-cli repark-handoff" in capsys.readouterr().out


def test_unknown_subcommand_is_not_this_guard_s_job(cli):
    """A verb absent from `_SUBCOMMAND_USAGE` falls through untouched — the
    existing unknown-subcommand path owns that refusal, and swallowing it here
    would report the wrong error."""
    assert cli._reject_unknown_flags("no-such-verb", ["--anything"]) is None


def test_accepted_set_is_derived_not_hand_listed(cli):
    """Pins the derivation itself.

    If someone replaces the `_SUBCOMMAND_USAGE` scan with a literal set, a verb
    gaining a flag starts being refused despite being documented. Asserting the
    guard tracks the table catches that: `--kind` is declared only in
    `stamp-shipped-in`'s usage string and nowhere in the guard's own source.
    """
    assert "--kind" in cli._SUBCOMMAND_USAGE["stamp-shipped-in"]
    assert cli._reject_unknown_flags("stamp-shipped-in", ["p.md", "--kind", "successor"]) is None
    # ...and the same flag is NOT silently accepted on a verb that never declared it.
    assert cli._reject_unknown_flags("repark-handoff", ["p.md", "--kind", "successor"]) == 2


@pytest.mark.parametrize(
    ("subcmd", "argv"),
    [
        ("action-memo", ["m.md", "--disposition", "actioned"]),
        ("action-memo", ["m.md", "--disposition", "partial", "--note", "why"]),
        ("resolve-memo", ["m.md", "--note", "x"]),
        ("resolve-memo", ["m.md", "--any-engine-flag-at-all"]),
    ],
)
def test_open_flag_tail_verbs_forward_their_tail(cli, subcmd, argv):
    """The regression example-retrieval-repo-df hit on 2026-08-31, the day this guard landed.

    `action-memo` and `resolve-memo` declare `[disposition-flags...]` and forward
    their tail to the engine verbatim, so their flag vocabulary is the ENGINE's.
    `_SUBCOMMAND_USAGE` cannot enumerate it, `_FLAG_RE` therefore found NOTHING,
    and an empty accepted set made the guard refuse every documented disposition
    flag with exit 2 — the entire memo-disposition surface unreachable through
    this CLI. The reporting session had to call `cs_action_memo` directly to
    action a memo at all.

    Note this is the guard's OWN failure direction: it refuses, so an empty
    vocabulary must decline, never refuse-everything.
    """
    assert cli._reject_unknown_flags(subcmd, argv) is None


def test_a_repeatable_literal_flag_is_not_an_open_tail(cli):
    """The near-miss in the fix, pinned so it cannot be widened by accident.

    `chain-archive-handoff`'s usage ends `[--exclude <path>]...` — an ellipsis,
    but on a REPEATABLE LITERAL flag that IS declared and IS enumerable. A
    fix keyed on "usage contains ..." rather than on the placeholder's shape
    would have exempted it and silently dropped a mistyped `--exclude`.
    """
    assert cli._reject_unknown_flags("chain-archive-handoff", ["p.md", "--exclude", "a.md"]) is None
    assert cli._reject_unknown_flags("chain-archive-handoff", ["p.md", "--bogus"]) == 2


def test_a_verb_with_no_flags_still_refuses(cli):
    """Why the fix is not "fail open when the accepted set is empty".

    `claim-handoff <handoff_path>` declares no flags and so also yields an empty
    set — but there an undeclared flag is exactly the silent partial write this
    guard exists for. The discriminator has to be the usage line's SHAPE, not
    the size of the set it produces.
    """
    assert cli._reject_unknown_flags("claim-handoff", ["p.md", "--bogus"]) == 2
    assert cli._reject_unknown_flags("repark-handoff", ["p.md", "--gate-note", "w"]) == 2


def test_every_engine_disposition_flag_survives_the_guard(cli):
    """The tripwire example-retrieval-repo-em asked for, keyed on the ENGINE's own table.

    The parametrised case above pins the guard's SHAPE — an open flag tail
    declines — using invented flag names. This pins the actual contract: every
    flag `coordinator_core.archive_stamp` declares for a memo disposition
    reaches the engine through this CLI. It fails the moment someone
    "documents" these two verbs by enumerating their flags in the usage row,
    which would close the open tail and re-strand whichever flag the
    enumeration missed — the exact regression, arrived at from the other side.
    """
    from coordinator_core.archive_stamp import (
        _DISPOSITION_BOOL_FLAGS,
        _DISPOSITION_FLAGS,
    )

    assert _DISPOSITION_FLAGS, "engine declares no disposition flags — table moved?"
    for subcmd in ("action-memo", "resolve-memo"):
        for flag in _DISPOSITION_FLAGS:
            assert cli._reject_unknown_flags(subcmd, ["m.md", flag, "v"]) is None, flag
        for flag in _DISPOSITION_BOOL_FLAGS:
            assert cli._reject_unknown_flags(subcmd, ["m.md", flag]) is None, flag
