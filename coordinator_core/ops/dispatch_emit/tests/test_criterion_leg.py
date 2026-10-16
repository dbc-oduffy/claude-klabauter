"""criterion_leg: the criterion typecheck classifier and the executor routing clause."""

import pytest

from coordinator_core.ops.dispatch_emit import gate_owed
from coordinator_core.ops.dispatch_emit.criterion_leg import (
    CRITERION_TYPECHECK_CLAUSE,
    CriterionTypecheck,
    criterion_typecheck,
)


@pytest.mark.parametrize("s", [None, "", "   ", "all tests pass and the build is green"])
def test_no_typecheck_returns_none(s):
    assert criterion_typecheck(s) is None


def test_backticked_command_returned():
    r = criterion_typecheck("`npx tsc --noEmit -p .` exits 0")
    assert r == CriterionTypecheck(commands=("npx tsc --noEmit -p .",))


def test_bare_word_has_no_commands():
    assert criterion_typecheck("pnpm typecheck passes").commands == ()


def test_two_commands_in_order_deduped():
    r = criterion_typecheck(
        "`mypy src` and `npx tsc --noEmit` and again `mypy src` plus `pytest -q` pass"
    )
    assert r.commands == ("mypy src", "npx tsc --noEmit")


@pytest.mark.parametrize(
    "s",
    [
        "tsconfig.json is valid",
        "the typescript sources compile",
        "type checking of inputs is strict",
        "`tsconfig.json` is updated",
    ],
)
def test_near_misses_return_none(s):
    assert criterion_typecheck(s) is None


@pytest.mark.parametrize("w", ["vue-tsc", "type-check", "type check", "Pyright", "TYPECHECK"])
def test_vocabulary_matches(w):
    assert criterion_typecheck(f"{w} is clean") is not None


def test_prose_only_mention_is_accepted_false_positive():
    s = (
        "An exit criterion naming a typecheck is placed on the verify/test leg and not handed "
        "to an executor row, so no executor row halts PARTIAL on it and dependents start."
    )
    assert criterion_typecheck(s).commands == ()


def test_clause_carries_guard_denied_line_the_runner_matches():
    assert "gate-blocker: guard-denied:" in CRITERION_TYPECHECK_CLAUSE
    assert gate_owed.GATE_OWED_RE.search(CRITERION_TYPECHECK_CLAUSE)
