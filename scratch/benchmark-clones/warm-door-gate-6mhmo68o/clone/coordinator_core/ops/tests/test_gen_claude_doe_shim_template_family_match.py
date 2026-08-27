"""
coordinator_core/ops/tests/test_gen_claude_doe_shim_template_family_match.py

Regression guard: the shim template and the resolved shell family must agree.

Why (found 2026-08-14 while hardening the launch chain): the generator copies
template bytes VERBATIM but names its destination from the shell family
(`_shim_filename`). The two inputs arrive from different places — `--template`
from the caller, the family from `--shell` or `_default_shell_family()` — and
nothing reconciled them.

`coordinator_core/install/maximalist.py` Step 3.5a.2 hardcoded the `.sh`
template and passed no `--shell`. On native Windows the default family is
"powershell", so install-maximalist rendered 62 lines of bash into
`claude-doe-shim.ps1`. Dot-sourcing that defines no `claude()`, so every
session launches without the coordinator plugin — while the profile's sentinel
block is present and correct, so the disabled-block detector and every other
downstream check report a healthy install. Silent, and worse than a loud break.

Scope note: detection is deliberately narrow. Only an explicit opposite-family
extension counts; arbitrary template names (test fixtures use them) are never
second-guessed.

Spec backlink: docs/reference/interactive-launch-chain.md § 2.
"""
from __future__ import annotations

import pytest

from coordinator_core.ops.gen_claude_doe_shim import _template_family_mismatch

pytestmark = [pytest.mark.cadence]


def test_bash_template_with_powershell_family_is_rejected():
    msg = _template_family_mismatch("/x/templates/shell/claude-doe-shim.sh.tmpl", "powershell")

    assert msg is not None
    assert "claude-doe-shim.ps1" in msg
    assert "--shell bash" in msg


def test_powershell_template_with_bash_family_is_rejected():
    msg = _template_family_mismatch("/x/templates/shell/claude-doe-shim.ps1.tmpl", "bash")

    assert msg is not None
    assert "claude-doe-shim.sh" in msg


@pytest.mark.parametrize(
    "tmpl,family",
    [
        ("/x/templates/shell/claude-doe-shim.sh.tmpl", "bash"),
        ("/x/templates/shell/claude-doe-shim.ps1.tmpl", "powershell"),
    ],
)
def test_matching_pairs_pass(tmpl, family):
    assert _template_family_mismatch(tmpl, family) is None


@pytest.mark.parametrize(
    "tmpl",
    [
        "/tmp/pytest-of-x/test_foo0/tmpl.txt",
        "/tmp/whatever",
        "/x/custom-shim-template",
    ],
)
@pytest.mark.parametrize("family", ["bash", "powershell"])
def test_neutral_template_names_are_not_second_guessed(tmpl, family):
    """Test fixtures pass arbitrary paths; the guard must not reject them."""
    assert _template_family_mismatch(tmpl, family) is None


def test_maximalist_selects_its_template_by_shell_family():
    """The call site that had the defect must derive, never hardcode.

    Source-level: the live consequence only reproduces on native Windows, and
    this must fail on a POSIX CI run too.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[3] / "coordinator_core" / "install" / "maximalist.py"
    ).read_text(encoding="utf-8")

    # Anchor on the CODE markers, not the bare step number — the module's
    # summary docstring lists the same numbers earlier in the file.
    start = src.index("-- Step 3.5a.2 -- gen-claude-doe-shim --")
    step = src[start : src.index("-- Step 3.5b --", start)]

    assert "claude-doe-shim.ps1.tmpl" in step, (
        "maximalist Step 3.5a.2 never names the powershell template — it is hardcoded to bash again"
    )
    assert '"--shell"' in step, (
        "maximalist Step 3.5a.2 must pass --shell explicitly so template and family cannot drift"
    )
