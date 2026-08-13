"""A9 -- ``_CLAIM_CONFLICT_REMEDY``/``_UNANSWERABLE_CLAIM_REMEDY`` name only
remedies that exist.

A string-literal snapshot test pins today's wording and says nothing about
whether the remedy it names actually parses -- exactly the shape of test
that would NOT have caught the shipped regression this test is fixing:
``_UNANSWERABLE_CLAIM_REMEDY`` told an operator to run
``session-claim-cli clear-claim-if-dead <path>``, a bare-path shorthand
``session-claim-cli``'s own ``_dispatch`` never accepted -- it exits 2
(usage error) on that invocation. See ``scoped_git_commit.py``'s two
``_..._REMEDY`` constants and their block comment for the fuller story.

This test instead PARSES: it extracts every backtick-quoted
``session-claim-cli <subcommand> <args...>`` invocation out of the two
remedy constants (their live values, not a hand-copied excerpt), and
cross-checks each one against ``session-claim-cli``'s own ``_dispatch``
function -- parsed via ``ast``, not re-implemented by hand -- to confirm
the named subcommand exists and the named arity would not hit that
function's usage-error path.

Metavariables (``<path>``, ``<root>``, ...) are positional-argument slots,
not resolvable arguments: this test counts them as one argument each and
never tries to substitute a real value for them.

SECOND REMEDY CLASS (2026-08-11, B6 of docs/plans/2026-08-11-kill-on-
staleness-and-a-way-past-the-gate.md): ``_CLAIM_CONFLICT_REMEDY`` now also
names DR-260's operator-granted unlock as an escape, via a backtick-quoted
``guard-unlock <guard_name>`` marker -- deliberately NOT a
``session-claim-cli`` invocation, because DR-260 has no minting CLI, by
design (the operator hand-writes the sentinel file; see
``guard_unlock_sentinel``'s own module docstring and the mechanism-gate
spike record's Finding 2). A remedy naming a CLI command that does not
exist was exactly the regression this file's own module docstring
describes -- the fix here is the same discipline applied to the new class:
never assert this marker is invisible to the check (an exemption), assert
instead that the guard name it names is the SAME string
``scoped_git_commit._check_claim_conflicts`` actually calls
``guard_unlock_sentinel.consume()`` with (``_CLAIM_CONFLICT_GUARD_NAME``) --
so a rename on one side without the other is caught here, the identical
shape of regression the CLI-invocation checks above already guard against.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit as sgc

#: coordinator_core/ops/ceremony/tests/<this file> -> repo root is four
#: parents up (tests -> ceremony -> ops -> coordinator_core -> repo root).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CLI_REPO_PATH = "coordinator/bin/session-claim-cli"

#: Backtick-quoted invocations beginning with the CLI's own name -- this is
#: how both remedy constants mark up the commands they tell an operator to
#: run (see their block comment); anything named WITHOUT this markup (e.g.
#: `_CLAIM_CONFLICT_REMEDY`'s parenthetical mention of
#: `list-claims-by-session` with no args) is prose, not an invocation, and
#: is deliberately not matched here.
_INVOCATION_RE = re.compile(r"`(session-claim-cli [^`]+)`")

#: The file-shaped remedy class (B6): a backtick-quoted ``guard-unlock
#: <guard_name>`` marker naming DR-260's operator-granted sentinel unlock --
#: never a CLI invocation, so it is matched by its OWN regex rather than
#: `_INVOCATION_RE` above, and cross-checked below against the actual guard
#: name the wiring uses rather than the CLI's `_dispatch` arities.
_UNLOCK_MARKER_RE = re.compile(r"`guard-unlock ([^` ]+)`")


def _extract_unlock_markers(text: str) -> List[str]:
    """Return every guard name named by a `` `guard-unlock <guard_name>` ``
    marker in *text*, in order."""
    return _UNLOCK_MARKER_RE.findall(text)


def _extract_invocations(text: str) -> Iterator[Tuple[str, str, List[str]]]:
    """Yield ``(full_invocation_text, subcommand, args)`` for every
    backtick-quoted ``session-claim-cli ...`` invocation in *text*.

    Every whitespace-separated token after the subcommand counts as one
    positional arg, metavariables (``<path>``) included -- they occupy a
    positional slot even though this test never resolves them to a real
    value.
    """
    for match in _INVOCATION_RE.finditer(text):
        tokens = match.group(1).split()
        assert tokens[0] == "session-claim-cli"
        if len(tokens) < 2:
            continue
        subcmd, args = tokens[1], tokens[2:]
        yield match.group(1), subcmd, args


def _min_arity_from_block(body: List[ast.stmt]) -> int:
    """Return the minimum ``len(rest)`` a dispatch arm accepts before
    falling through to its own ``_usage(...)`` call, inferred from the
    arm's own guard shape -- never a hand-maintained arity table that could
    itself drift from the CLI's actual code, which is exactly the failure
    mode this test exists to close.

    Recognizes the two guard shapes ``_dispatch`` actually uses: a bare
    ``if not rest:`` (arity >= 1) and ``if len(rest) < N:`` (arity >= N).
    A shorthand arm shaped ``if len(rest) == 1: ... elif len(rest) >= 2:
    ...`` (as ``clear-claim-if-dead``/``release-artifact`` grow a bare-path
    form) also reads as arity >= 1 -- the ``== 1`` branch is itself proof
    a single arg is accepted. An arm with none of these guards (no
    required-arg check at all) reads as arity >= 0.
    """
    for stmt in body:
        if not isinstance(stmt, ast.If):
            continue
        test = stmt.test
        if (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == "rest"
        ):
            return 1
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.left, ast.Call)
            and isinstance(test.left.func, ast.Name)
            and test.left.func.id == "len"
            and len(test.left.args) == 1
            and isinstance(test.left.args[0], ast.Name)
            and test.left.args[0].id == "rest"
            and isinstance(test.comparators[0], ast.Constant)
            and isinstance(test.comparators[0].value, int)
        ):
            op = test.ops[0]
            value = test.comparators[0].value
            if isinstance(op, ast.Lt):
                return value
            if isinstance(op, ast.Eq) and value == 1:
                return 1
    return 0


def _subcommand_arities(source: str) -> Dict[str, int]:
    """Parse ``session-claim-cli``'s ``_dispatch`` function and return
    ``{subcommand: minimum_required_arg_count}`` for every
    ``if subcmd == "...":`` arm it actually contains.

    A subcommand absent from the returned mapping is one ``_dispatch``
    does not accept at all -- any invocation naming it would hit the
    trailing "unknown subcommand" branch, not any particular arm's own
    usage error.
    """
    tree = ast.parse(source, filename=_CLI_REPO_PATH)
    dispatch_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
        ),
        None,
    )
    assert dispatch_fn is not None, (
        "session-claim-cli: could not find a `_dispatch` function -- this "
        "test's own assumption about the CLI's dispatch shape no longer "
        "holds, update it rather than the remedy constants"
    )
    arities: Dict[str, int] = {}
    for node in dispatch_fn.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "subcmd"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and isinstance(test.comparators[0].value, str)
        ):
            continue
        arities[test.comparators[0].value] = _min_arity_from_block(node.body)
    return arities


def _committed_cli_source() -> str:
    """Read ``session-claim-cli`` as it stands at ``HEAD`` -- never the
    worktree copy.

    A refusal constant ships in a commit; what it must parse against is
    what THAT commit's CLI actually accepts, not whatever a live peer
    session currently has uncommitted in the shared worktree (see this
    module's docstring -- the bare-path shorthand this test's regression
    is about lived exactly there, uncommitted, when the shipped refusal
    text was already broken). Reading via ``git show HEAD:`` rather than
    the on-disk file keeps this test honest about that distinction and
    immune to a concurrent uncommitted edit on either side.
    """
    result = subprocess.run(
        ["git", "show", f"HEAD:{_CLI_REPO_PATH}"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout


@pytest.fixture(scope="module")
def cli_arities() -> Dict[str, int]:
    source = _committed_cli_source()
    arities = _subcommand_arities(source)
    assert arities, (
        "session-claim-cli: parsed zero `if subcmd == ...:` arms out of "
        "_dispatch -- either the file moved, or this test's ast walk no "
        "longer matches its shape"
    )
    return arities


@pytest.mark.parametrize(
    "remedy_name", ["_CLAIM_CONFLICT_REMEDY", "_UNANSWERABLE_CLAIM_REMEDY"]
)
def test_remedy_invocations_have_at_least_one_command(remedy_name: str) -> None:
    text = getattr(sgc, remedy_name)
    invocations = list(_extract_invocations(text))
    assert invocations, (
        f"{remedy_name}: no backtick-quoted `session-claim-cli ...` "
        "invocation found -- if this remedy genuinely names no command, "
        "this test has nothing to check and should be told so explicitly"
    )


@pytest.mark.parametrize(
    "remedy_name", ["_CLAIM_CONFLICT_REMEDY", "_UNANSWERABLE_CLAIM_REMEDY"]
)
def test_remedy_invocations_parse_against_cli_dispatch(
    remedy_name: str, cli_arities: Dict[str, int]
) -> None:
    text = getattr(sgc, remedy_name)
    for full, subcmd, args in _extract_invocations(text):
        assert subcmd in cli_arities, (
            f"{remedy_name} names subcommand {subcmd!r} via `{full}`, but "
            f"session-claim-cli's _dispatch accepts no such subcommand "
            f"(known: {sorted(cli_arities)}) -- this invocation exits 2 "
            "on 'unknown subcommand'"
        )
        min_arity = cli_arities[subcmd]
        assert len(args) >= min_arity, (
            f"{remedy_name} invokes `{full}` with {len(args)} arg(s) "
            f"({args!r}), but session-claim-cli's {subcmd!r} arm requires "
            f"at least {min_arity} -- this invocation exits 2 on the "
            "usage-error path"
        )


def test_claim_conflict_remedy_names_no_guard_unlock() -> None:
    """C5 (docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-
    keys.md): `_CLAIM_CONFLICT_REMEDY` no longer discusses DR-260's
    operator-granted unlock at all -- a wall does not discuss its own
    doors. This asserts that absence positively, on both axes: no
    `` `guard-unlock <name>` `` marker, and the wiring's own guard name
    (`_CLAIM_CONFLICT_GUARD_NAME`) is not a substring of the rendered text
    either -- a bare re-source of the deleted marker check would otherwise
    go silently vacuous the moment the marker disappeared."""
    assert not _extract_unlock_markers(sgc._CLAIM_CONFLICT_REMEDY), (
        "_CLAIM_CONFLICT_REMEDY: found a `guard-unlock <name>` marker -- "
        "the remedy text must not name this escape at all (see C5)"
    )
    assert sgc._CLAIM_CONFLICT_GUARD_NAME not in sgc._CLAIM_CONFLICT_REMEDY, (
        f"_CLAIM_CONFLICT_REMEDY still names the guard "
        f"{sgc._CLAIM_CONFLICT_GUARD_NAME!r} -- the constant stays as the "
        "wiring's SSOT, but the rendered remedy text must not mention it"
    )


def test_unanswerable_remedy_names_no_guard_unlock_marker() -> None:
    """The DR-260 break-past only ever clears a live-claimant CONFLICT
    (see `_check_claim_conflicts`'s own docstring/B5 spec) -- it has no
    bearing on an UNANSWERABLE claim-index read, a different failure class
    entirely. `_UNANSWERABLE_CLAIM_REMEDY` must not claim an escape this
    mechanism does not actually grant."""
    assert not _extract_unlock_markers(sgc._UNANSWERABLE_CLAIM_REMEDY)
