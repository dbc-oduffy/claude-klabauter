"""Every PowerShell loop grammar that fans out must classify, not just `foreach`.

Purpose: under `Dialect.POWERSHELL` the classifier recognised `foreach ($x in
$y)` and the `ForEach-Object`/`%` pipeline alias, and nothing else. A C-style
`for`, a `while`, and a `do { } while|until` all returned NO SHAPES, while each
runs its brace block once per iteration -- the per-item fan-out `FOR_LOOP`
exists to name.

The module's own docstring is what makes that a defect rather than a
preference. It discharges the `WHILE_READ_LOOP` absence with a stated reason and
then says outright that an absence with no stated reason reads as an oversight
to the next author. These three had none.

The distinction that made it easy to miss, and the one this file exists to stop
being re-collapsed: PowerShell having no `while read` IDIOM does not mean
PowerShell has no `while` LOOP. It has one, and it fans out. `WHILE_READ_LOOP`
stays correctly absent from the POWERSHELL table entry; that is a separate fact
from this one.

WHY `FOR_LOOP` AND NOT A NEW SHAPE. `SHAPE_PRECEDENCE` is ordered and a new
member would have to be seated with an argument rather than appended. No new
seat is needed: `foreach` is not a `for` either and has classified `FOR_LOOP`
since this detector existed, so the shape already means "iteration that fans
out" rather than "the POSIX `for` keyword". The consumer leg
(`guard_plumbing_and_loops`) is advisory-only on every platform by its own
negative-spec, so widening what classifies here widens what is ADVISED, never
what is denied.

Provenance: raised by doe-claude-aa on 2026-09-01 as an explicit non-finding --
they saw the C-style `for` from their own tree and could not tell intent from
omission. They were right that they couldn't: no intent was recorded, and
`while`/`do-while` were missing too, which their probe did not reach.

Non-spawning by construction: pure classification, no subprocess.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards._dialect import Dialect
from coordinator_core.bash_guards._shape_classifier import Shape, classify_command


def _primary(cmd: str, dialect: Dialect):
    result = classify_command(cmd, dialect=dialect)
    return result.primary.shape if result.primary else None


@pytest.mark.parametrize(
    "cmd",
    [
        "for ($i=0; $i -lt 10; $i++) { git log -1 }",
        "while ($true) { git log -1 }",
        "do { git log -1 } while ($i -lt 3)",
        "do { git log -1 } until ($done)",
    ],
)
def test_pwsh_loop_grammars_classify_as_for_loop(cmd):
    assert _primary(cmd, Dialect.POWERSHELL) is Shape.FOR_LOOP


def test_the_foreach_statement_still_classifies():
    assert (
        _primary("foreach ($f in $files) { git log -1 $f }", Dialect.POWERSHELL)
        is Shape.FOR_LOOP
    )


def test_the_pipeline_alias_still_wins_its_own_shape():
    assert (
        _primary("Get-ChildItem X: | % { git log -1 $_ }", Dialect.POWERSHELL)
        is Shape.PIPELINE_FOREACH_OBJECT
    )


def test_a_plain_command_classifies_as_no_loop():
    assert _primary("git log -1 HEAD", Dialect.POWERSHELL) is None


def test_foreach_without_an_in_clause_is_not_the_statement_form():
    assert _primary("foreach ($x) { git log -1 }", Dialect.POWERSHELL) is None


def test_a_loop_keyword_without_a_brace_block_does_not_classify():
    """A brace block is required. A bare keyword with no body is not a loop,
    and a command merely CONTAINING the word is certainly not."""
    assert _primary("for ($i=0; $i -lt 3; $i++)", Dialect.POWERSHELL) is None
    assert _primary("format-volume", Dialect.POWERSHELL) is None


def test_a_loop_keyword_without_a_paren_does_not_classify():
    assert _primary("while { git log -1 }", Dialect.POWERSHELL) is None


def test_do_without_a_trailing_condition_does_not_classify():
    assert _primary("do { git log -1 }", Dialect.POWERSHELL) is None


def test_the_posix_for_loop_still_classifies_under_bash():
    assert (
        _primary("for f in *.py; do git log -1 $f; done", Dialect.BASH)
        is Shape.FOR_LOOP
    )


def test_pwsh_grammar_does_not_classify_under_bash():
    assert _primary("while ($true) { git log -1 }", Dialect.BASH) is None
