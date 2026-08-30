"""Pins that every declared SUBJECT-class site from the 2026-08-30 foreign-repo-identity
disposition probe (`state/audits/2026-08-30-foreign-repo-identity-disposition-probe.md`) still
renders its foreign-repo token in source.

Why source text, not an import-and-call: several of these sites live in scripts with
module-level side effects (argv parsing, `sys.exit`, environment reads) unsuited to import in a
unit test, and the exemption this pins is a textual one — "this string still contains the
token" — not a runtime behaviour. Reading the file's text is the whole assertion; no module needs
to exist, only the source line to be there (per C6's own brief).

This is what stops a future tightening pass (a blanket suppression, a lint auto-fix, a
find-and-replace) from quietly eating a message that has to carry a foreign-repo name to remain
actionable — the audit's own sample makes the point: `queue_promote.py`'s
`"machine-local set repos.doe_claude /path/to/DoE-claude"` and `state_root.py`'s `"engine.source_root
<path-to-live-claude-klabauter>"` are the remedy, not noise beside it. Re-run after the disposition
half (C6) to prove no subject-class site regressed.

NEGATIVE SPEC: this suite does not assert anything about NOT-REACHABLE, OUT-OF-CLASS, or
UNCLASSIFIABLE sites (the probe's other three verdicts) — those are left exactly as they are and
carry no exemption obligation. It also does not assert on the 3 CHANGED sites (`warm/skew.py`,
`forwarder_drift.py`'s skip-line, `cc_invoke.py`'s split announcement) — those are pinned by their
own dedicated suites landed in C3
(`coordinator_core/warm/tests/test_skew_publish_lag_suppression.py`,
`coordinator_core/plugin_health/tests/test_forwarder_drift_skip_line_rewrite.py`,
`coordinator/bin/tests/test_cc_invoke_foreign_identity.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.cadence]


def _read(rel_path: str) -> str:
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"expected subject-class site file missing: {rel_path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Each case: (file, at least one literal substring that must survive in the
# file's text). Substrings are the load-bearing foreign-repo token in the
# probe's cited message, not the whole message.
# ---------------------------------------------------------------------------

_SUBJECT_SITES = [
    ("coordinator_core/engine_root.py", "broken or partial claude-klabauter checkout"),
    ("coordinator_core/ops/coordinator_doe_root.py", "machine-local set repos.doe_claude /path/to/DoE-claude"),
    ("coordinator_core/ops/check_rag_state.py", "python3 <claude-klabauter>/scripts/setup.py"),
    ("coordinator_core/ops/generate_repomap.py", "python3 <claude-klabauter>/scripts/setup.py"),
    ("coordinator_core/ops/generate_repomap.py", "Install the coordinator-claude plugin"),
    ("coordinator_core/state_root.py", "engine.source_root <path-to-live-claude-klabauter>"),
    ("coordinator_core/tracker_holder.py", "/path/to/claude-klabauter"),
    ("coordinator_core/trusted_root_guard.py", "python3 <claude-klabauter>/scripts/setup.py"),
    ("coordinator_core/resolve_coordinator_clone.py", 'os.path.join(claude_home, "plugins", "coordinator-claude")'),
    ("coordinator_core/resolve_coordinator_clone.py", "plugin.mirrors.coordinator-claude.live_path"),
    # abs-path-ok: literal marker copied from the cited source file's own message text, not a
    # path this test resolves or joins.
    ("coordinator_core/resolve_coordinator_clone.py", "flat ~/.claude/plugins/coordinator-claude"),
    ("coordinator_core/plugin_health/forwarder_drift.py", "DoE-claude coordinator/ dir"),
    ("coordinator_core/write_guards/nudge_session_display_name_as_identifier.py", '"claude-klabauter"'),
    ("coordinator_core/ops/check_generator_output_staleness.py", 'f"DoE-claude@{sha}"'),
    ("coordinator_core/ops/check_generator_output_staleness.py", 'f"DoE-claude:{pair.artifact}"'),
]


@pytest.mark.parametrize("rel_path,marker", _SUBJECT_SITES)
def test_subject_class_site_still_renders_its_foreign_token(rel_path: str, marker: str):
    text = _read(rel_path)
    assert marker in text, (
        f"{rel_path}: expected subject-class marker {marker!r} is gone — a subject-class site's "
        "whole point is that the reader must see this token to act; if this file's message "
        "genuinely no longer needs it, that is a re-classification, not a silent suppression, "
        "and needs a fresh disposition-probe entry, not a passing test edit"
    )


def test_probe_document_exists_and_records_the_split():
    probe = _read("state/audits/2026-08-30-foreign-repo-identity-disposition-probe.md")
    for expected in ("15 KEEP", "12 NOT-REACHABLE", "UNCLASSIFIABLE"):
        assert expected in probe, f"disposition probe missing expected tally text: {expected!r}"


def test_row_14_ruling_is_recorded_and_left_alone():
    # Row 14 (docs/plans/.../C6.md): _KNOWN_SLUGS is a symmetric 8-repo constant, not a
    # directed foreign-identity emission -- out of scope, KEEP, ruled by the EM against the
    # probe's own reading. Confirm it still contains claude-klabauter (untouched) and is not a
    # single-repo-only tuple (which would mean someone narrowed it under this plan's cover).
    text = _read("coordinator_core/write_guards/nudge_session_display_name_as_identifier.py")
    assert '"claude-klabauter"' in text
    assert '"doe-claude"' in text
