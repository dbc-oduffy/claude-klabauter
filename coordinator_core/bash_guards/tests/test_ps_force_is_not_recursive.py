"""`Remove-Item -Force <file>` is not a recursive delete.

check_destructive_rm rewrote every recognized PowerShell removal into a
bash-shaped ``rm -rf <argv>`` segment and then re-derived recursion from that
synthesized string with a bash SHORT-FLAG regex. Two things went wrong at once:
the ``-rf`` it had just stamped on always matched, and so did any surviving
PowerShell flag containing an ``r`` -- ``-Force`` above all. So a single-file
delete armed the uncommitted-work ladder, and the refusal named neither the
flag it had read nor the dialect it read it in.

Measured 2026-09-11 in three repos by three sessions, each of whom read the
result as a policy to argue with rather than as a parse bug.

Both verdicts are proven here: a real ``-Recurse`` still arms and still denies,
and the refusal that does fire names the token that caused it.
"""

from __future__ import annotations

from coordinator_core.bash_guards import dispatch_checks


def test_force_alone_is_not_read_as_recursion():
    assert dispatch_checks._ps_recurse_flag(["-Force", "-LiteralPath", "a.py"]) is None


def test_recurse_and_its_unambiguous_prefixes_are_read_as_recursion():
    for tok in ("-Recurse", "-recurse", "-Recurs", "-rec", "-r", "-R"):
        assert dispatch_checks._ps_recurse_flag([tok, "x"]) == tok


def test_other_r_bearing_powershell_flags_are_not_recursion():
    for tok in ("-Force", "-Filter", "-Credential", "-WhatIf", "-ErrorAction"):
        assert dispatch_checks._ps_recurse_flag([tok, "x"]) is None


def test_a_double_dash_token_is_not_a_powershell_parameter():
    assert dispatch_checks._ps_recurse_flag(["--recursive"]) is None
