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

NEGATIVE SPEC: this suite does not assert that a NOT-REACHABLE or OUT-OF-CLASS site's foreign
token *survives* — those two verdicts do not carry the "must remain visible to the reader"
obligation SUBJECT does, so token-survival is pinned only for the `_SUBJECT_SITES` table above.
It also does not assert on the 3 CHANGED sites (`warm/skew.py`, `forwarder_drift.py`'s skip-line,
`cc_invoke.py`'s split announcement) — those are pinned by their own dedicated suites landed in C3
(`coordinator_core/warm/tests/test_skew_publish_lag_suppression.py`,
`coordinator_core/plugin_health/tests/test_forwarder_drift_skip_line_rewrite.py`,
`coordinator/bin/tests/test_cc_invoke_foreign_identity.py`).

# Review: overengineering-reviewer (findings 1/2) — the `# foreign-identity:` declaration
# vocabulary in the falsifier had exactly one reader, which retires with the plan
# (`promotion_reason` states it is not promoted), so 29 in-source declarations — 15 of them
# NOT-REACHABLE, changing no rendered byte — would go unread the moment the plan closes. This
# suite is the durable reader: it survives the plan (ships in the suite, not plan-scoped) and
# already reads source text, so it is extended below to assert every `# foreign-identity:`
# declaration is well-formed (a real verdict, followed by a non-empty reason) and that the
# declared count matches the known-good tally, instead of adding a new B7-style source-scanning
# lint mechanism (not available: B7 lints a rendered message STRING fed by a runtime trigger
# corpus, per `coordinator_core/message_register/_rules.py :: run_rule("B7", text)` — it has no
# source hit site and never sees comments). The 15 NOT-REACHABLE sites are kept, not deleted: they
# are verified classifications recorded nowhere else for the `coordinator/bin` half (the
# disposition-probe audit is `coordinator_core`-scoped), and this extension is what answers
# "asserted by nothing" without destroying that information.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.cadence]


def _load_falsifier_module():
    """Import the falsifier by file path -- its filename is not a valid module
    name (dots, hyphens), so it cannot be imported normally."""
    path = _REPO_ROOT / "docs" / "plans" / (
        "2026-08-30-the-engine-stops-naming-its-own-repo.falsifier.py"
    )
    spec = importlib.util.spec_from_file_location("_engine_stops_naming_falsifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


# ---------------------------------------------------------------------------
# Review: overengineering-reviewer (findings 1/2) -- durable reader for the
# `# foreign-identity:` declaration vocabulary. Source-text based, no new
# module, no new file: this suite scans the same two trees the falsifier's
# corroboration leg scans, and pins (a) every declaration is well-formed and
# (b) the declared count matches the known-good tally -- so a declaration
# regressing to a malformed comment, or one silently vanishing, breaks the
# suite instead of going unread once the plan-scoped falsifier retires.
# ---------------------------------------------------------------------------

_DECLARATION_RE = re.compile(
    r"#\s*foreign-identity:\s*(SUBJECT|NOT-REACHABLE|OUT-OF-CLASS)\s*[—-]\s*(\S.*)$"
)
_KNOWN_VERDICTS = ("SUBJECT", "NOT-REACHABLE", "OUT-OF-CLASS")
_EXPECTED_DECLARATION_COUNT = 28


def _all_declaration_lines() -> list[tuple[str, int, str]]:
    """Return (rel_path, lineno, raw_line) for every `# foreign-identity:` comment
    under coordinator_core/ and coordinator/bin/, excluding this suite's own file
    and any tests/ directory (declarations live at production call sites only)."""
    hits: list[tuple[str, int, str]] = []
    for sub in ("coordinator_core", "coordinator/bin"):
        base = _REPO_ROOT / sub
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            parts = path.parts
            if "tests" in parts or ".pytest_cache" in parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            rel = path.relative_to(_REPO_ROOT).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "foreign-identity:" in line and line.strip().startswith("#"):
                    hits.append((rel, lineno, line))
    return hits


def test_every_foreign_identity_declaration_is_well_formed():
    hits = _all_declaration_lines()
    assert hits, "expected at least one # foreign-identity: declaration in the tree"
    malformed = []
    for rel, lineno, line in hits:
        match = _DECLARATION_RE.search(line)
        if not match:
            malformed.append(f"{rel}:{lineno}: {line.strip()!r}")
            continue
        verdict, reason = match.group(1), match.group(2)
        if verdict not in _KNOWN_VERDICTS or not reason.strip():
            malformed.append(f"{rel}:{lineno}: {line.strip()!r}")
    assert not malformed, (
        "malformed # foreign-identity: declaration(s) -- each must be "
        "'# foreign-identity: SUBJECT|NOT-REACHABLE|OUT-OF-CLASS — <non-empty reason>':\n"
        + "\n".join(malformed)
    )


# Review: code-reviewer (finding 1) -- `declaration_for` must not credit a
# declaration above one class method to a sibling method's site; a false
# "declared" verdict is the one direction this instrument must never fail in.
def test_declaration_for_does_not_cross_a_class_method_boundary():
    module = _load_falsifier_module()
    source = '''\
class Example:
    def first(self):
        print("no declaration here")

    # foreign-identity: SUBJECT — deliberate, belongs to second() only
    def second(self):
        print("declared here")
'''
    lines = source.splitlines()
    first_hit_lineno = next(
        i + 1 for i, ln in enumerate(lines) if "no declaration here" in ln
    )
    second_hit_lineno = next(
        i + 1 for i, ln in enumerate(lines) if "declared here" in ln
    )
    assert module.declaration_for(lines, first_hit_lineno) is None, (
        "first()'s undeclared print must not be credited with second()'s "
        "declaration -- declaration_for crossed the method boundary"
    )
    assert module.declaration_for(lines, second_hit_lineno) == "SUBJECT"


def test_foreign_identity_declaration_count_matches_known_tally():
    hits = _all_declaration_lines()
    assert len(hits) == _EXPECTED_DECLARATION_COUNT, (
        f"expected {_EXPECTED_DECLARATION_COUNT} # foreign-identity: declarations "
        f"(the falsifier's own corroboration-leg tally at close-out), found {len(hits)}. "
        "A change in this count is either a new emission site needing its own "
        "declaration, or a declaration that silently vanished -- update "
        "_EXPECTED_DECLARATION_COUNT only after confirming which, in the "
        "disposition-probe audit or a fresh classification."
    )
