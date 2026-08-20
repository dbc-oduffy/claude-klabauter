"""Every `archive-stamp-cli` verb a pickup brief emits must be a verb that
CLI actually accepts.

A directive's `cli` + `args` pair is a claim about the named CLI's argv
contract, and both ways of working a directive are supported: dispatched
through `pickup_assemble.apply`, or run verbatim by hand off the brief.
`build_gate_recheck_directive` emitted the short `gate-recheck`, which
`_dispatch_archive_stamp_cli` accepted internally while the CLI itself
rejected it with `unknown subcommand` (exit 2) — so the brief was correct
for one consumer and wrong for the other, and only the by-hand path broke
(cross-repo/inbox/2026-08-20-doe-claude-em-cmd-forwarder-eats-json-and-two-
smaller-seams.md, item 2).

Negative-spec:
    - Does NOT spawn `archive-stamp-cli`. The accepted verbs are read out of
      its source text, so this test stays on the fast tier and outside the
      per-item git-spawn amplification budget.
    - Does NOT assert the CLI's verb set itself, nor that every CLI verb is
      reachable from a directive. Only the one direction that can strand an
      operator: an emitted verb the CLI will refuse.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core import pickup_assemble as pa

REPO_ROOT = Path(__file__).resolve().parents[3]
ARCHIVE_STAMP_CLI = REPO_ROOT / "coordinator" / "bin" / "archive-stamp-cli.py"


def _cli_accepted_verbs() -> set[str]:
    src = ARCHIVE_STAMP_CLI.read_text(encoding="utf-8")
    return set(re.findall(r'subcmd == "([a-z0-9-]+)"', src))


def test_cli_verb_extraction_is_not_vacuous():
    """The regex above is the whole basis of this test — an extraction that
    silently returned nothing would make every assertion below pass."""
    verbs = _cli_accepted_verbs()
    assert len(verbs) > 5
    assert "claim-handoff" in verbs


def test_gate_recheck_directive_emits_a_verb_the_cli_accepts():
    directive = pa.build_gate_recheck_directive("state/handoffs/h1.md")

    assert directive["cli"] == "archive-stamp-cli"
    assert directive["args"][0] == "gate-recheck-handoff"
    assert directive["args"][0] in _cli_accepted_verbs()


def test_short_gate_recheck_verb_is_not_a_cli_subcommand():
    """Pins WHY the long form is emitted: the short spelling the directive
    used to carry is not a subcommand `archive-stamp-cli` recognises. If the
    CLI ever grows it as an alias, this test is the place that says so."""
    assert "gate-recheck" not in _cli_accepted_verbs()
