"""AC3 regression pin: the non-git PowerShell verb classifier must not fail
OPEN on the parse-failure route.

Spec backlink: docs/plans/2026-08-19-the-held-guard-cohort-becomes-dialect-safe.md
(AC3 -- `tokens is None` routes to C2's PowerShell-shaped scanner and still
denies on a hit; AC12 -- the `MATCHERS` widen is only safe once that holds).

Found by the slice-B partitioned code review at workstream-complete and
confirmed by execution before the fix: `Remove-Item -Recurse -Force ./state`
DENIED, while the same command with `&> out.txt` appended -- a shape
`_dialect.py`'s own docstring records as confirmed ``has_error=True`` --
tokenized to ``None`` and ALLOWED. `_evaluate_powershell_destructive` had the
same `if segments is None: return None` shape its git sibling had, and only
the git one was fixed.

Why it mattered here specifically: `_check_powershell` was unreachable in
production while `MATCHERS` was `("Bash",)`. Widening it to
`COMMAND_TOOL_NAMES` is what turned a dead fail-open path into a live silent
allow for real PowerShell-tool traffic -- so the widen and this fallback are
the same safety obligation, not two independent improvements.

Negative-spec: do NOT relax either parse-failure route to `return None`, and
do NOT route them into the bash-shaped `_evaluate_legacy`.
"""

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard

#: (parseable spelling, the same command made untokenizable). `&> out.txt` is
#: the documented `has_error=True` form, so the second column genuinely takes
#: the parse-failure route rather than merely looking like it should.
_PAIRS = [
    ("Remove-Item -Recurse -Force ./state", "Remove-Item -Recurse -Force ./state &> out.txt"),
    ("Stop-Process -Force -Name claude", "Stop-Process -Force -Name claude &> out.txt"),
    ("icacls ./state /grant everyone:F", "icacls ./state /grant everyone:F &> out.txt"),
]

#: Hazard-documenting prose must not deny on either route -- the false-positive
#: class this cohort exists to kill. Fixing fail-open by denying everything
#: would trade one defect for a worse one.
_PROSE = [
    'Write-Output "never run Remove-Item -Recurse -Force here"',
    "echo 'Stop-Process -Force is destructive' &> out.txt",
    'Write-Output "icacls /grant is a permission change" &> out.txt',
]


@pytest.mark.parametrize("parseable,untokenizable", _PAIRS)
def test_untokenizable_spelling_denies_exactly_as_the_parseable_one_does(
    parseable, untokenizable
):
    anchored = guard._evaluate_powershell_destructive(parseable)
    assert anchored is not None, (
        f"anchor failed: {parseable!r} must deny on the tokenized path"
    )
    assert guard._evaluate_powershell_destructive(untokenizable) == anchored


@pytest.mark.parametrize("command", _PROSE)
def test_hazard_documenting_prose_still_allows(command):
    assert guard._evaluate_powershell_destructive(command) is None


def test_verb_fallback_is_reachable_and_rules_directly():
    assert (
        guard._evaluate_legacy_powershell_verbs("Remove-Item -Recurse -Force ./x")
        is not None
    )
    assert guard._evaluate_legacy_powershell_verbs("Get-Item ./x") is None
