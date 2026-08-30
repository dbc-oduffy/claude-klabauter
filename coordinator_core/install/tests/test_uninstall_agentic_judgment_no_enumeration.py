"""
coordinator_core.ops.tests.test_uninstall_agentic_judgment_no_enumeration — AC6:
the uninstall-agentic-judgment wiki page must never enumerate a concrete write
surface.

Purpose: chunk C7's mechanical half. The page
(`docs/wiki/uninstall-agentic-judgment.md`) carries uninstall's *judgment* prose
deliberately — how to weigh a surface, when to stop, what to tell the operator —
but the *facts* (which keys, which paths, which markers) live exactly once, in the
write-surface declarations (`write_surface_discovery`) and the install receipt. If
the page ever lists a real key/path/marker, it becomes a third hand-maintained copy
of those facts and rots the way the copy that motivated this whole plan did
(Anti-scope: "Do not let the wiki page enumerate surfaces").

Design, per the dispatch brief — KIND-KEYED, not literal-set membership:

  A literal-set test (collect every literal string the declarations happen to carry
  today, assert none appear) is false-green on every SHAPE-declaring writer: a
  `ShapedClause` entry contributes a TEMPLATE with a placeholder
  (e.g. ``"repos.<derived-key>"``), never a concrete literal, so a page that
  enumerated ``repos.some-real-repo`` would stay green under a literal-set scan —
  precisely the writers whose enumeration risk is highest, since their surface is
  computed rather than fixed in source.

  Instead this test derives one RECOGNIZER REGEX per declared entry, keyed off the
  entry's declared `kind`:

    - key-shaped kinds (`git-config-key`, `machine-local-key`, `os-env-var`,
      `structured-file-key`) recognize the entry's declared `key`.
    - path-shaped kinds (`file-path`, `line-membership`) recognize the entry's
      declared `path`.
    - marker-delimited kinds (`rc-block`, `hook-gate-region`) recognize the
      entry's `path` AND its `begin_marker`/`end_marker` (skipping `None` and the
      permanent `absent-on-legacy-installs` tri-state sentinel, which is a status
      value, not a surface fact).

  A STATIC entry's value is matched as a literal (word-bounded, case-sensitive).
  A SHAPED entry's value contains `<...>` template placeholders (per
  `write_surface.py`'s `ShapedClause.entry_template` docstring) — those become a
  wildcard segment in the recognizer, so the regex matches ANY concrete instance
  of the shape, not only today's literal placeholder spelling. This is what makes
  the test fail closed on the shaped half of the protocol instead of silently
  passing it through.

Allowlist: the page's oh-my-zsh cautionary case names ``~/.ssh/config`` and
``.zshrc`` on purpose — a different tool's failure mode, not this install's own
surface. Every allowlist entry below carries an inline reason; an unexplained
entry is exactly how this test would quietly stop working.

Spec backlink: pln-writer-declared-write-surface-49d3bd,
chunk C7, AC6.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.install.write_surface import ABSENT_ON_LEGACY_INSTALLS
from coordinator_core.install.write_surface_discovery import (
    _flatten_declaration,
    discover_declarations,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PAGE_PATH = REPO_ROOT / "docs" / "wiki" / "uninstall-agentic-judgment.md"


def _declared_entries() -> list[dict]:
    """Every declared entry from every discovered writer, flattened.

    Replaces the `write_surface.emit_manifest` op this test used to dispatch
    through: the op was gravestoned for having no consumer, and the shape
    this test needs was never the op — it was the declarations underneath.
    Discovery failures are ignored here on purpose: a writer this scan cannot
    import contributes no surface values, so it cannot make the page scan
    pass for the wrong reason. `test_declared_entries_span_multiple_kinds`
    below is what fails closed if discovery collapses wholesale."""
    declarations, _failures = discover_declarations(REPO_ROOT)
    return [e for decl in declarations.values() for e in _flatten_declaration(decl)]

# Kind -> which entry field(s) carry a scannable surface fact for that kind.
_KEY_KINDS = {"git-config-key", "machine-local-key", "os-env-var", "structured-file-key"}
_PATH_KINDS = {"file-path", "line-membership"}
_MARKER_KINDS = {"rc-block", "hook-gate-region"}

# Deliberately EMPTY, and the history is the point. This once held
# `~/.ssh/config` and `.zshrc`, exempted on the reasoning that they named a
# different tool's failure mode (ohmyzsh#13156) rather than any surface of
# ours. That reasoning was true when written and false within the day: both
# paths became genuinely declared surfaces once `setup-github-auth-1password`
# and `shell_rc_guard` declared theirs, and `test_allowlist_entries_carry_a_
# reason_and_are_not_declared_values` caught the collision.
#
# An allowlist entry is a standing claim about the world, not a local
# annotation — it keeps asserting itself long after whoever wrote it stopped
# checking. Prefer rewriting the page to need no exemption, as was done here,
# over adding one. If an entry is genuinely unavoidable, its reason must say
# what would make it expire.
_ALLOWLIST: dict[str, str] = {}


# `key`/`path` are the two fields the write_surface.py module docstring documents
# as carrying template placeholders on a SHAPED entry (e.g. `key="repos.<derived-
# key>"`) — `<...>` there means "any concrete instance of this shape". A marker
# (`begin_marker`/`end_marker`) is never templated even on a shaped entry (see the
# declarations: a `rc-block`'s markers are fixed literal comment text regardless of
# form) and MUST be treated as a literal, not scanned for placeholder syntax —
# some marker text legitimately contains its own angle brackets (e.g.
# `"<!-- coordinator:posture:start -->"`), which would be misread as a
# placeholder span if run through the same splitter.
_TEMPLATED_FIELDS = {"key", "path"}


def _placeholder_pattern(value: str) -> str | None:
    """Turn a declared key/path value into a recognizer regex. A SHAPED entry's
    value contains `<...>` template placeholders (e.g. "repos.<derived-key>") —
    those become a wildcard so the recognizer matches ANY concrete instance of
    the shape, not only today's placeholder spelling. A STATIC entry's value has
    no such placeholder and round-trips to a literal, word-bounded match.

    Returns None when the value is ENTIRELY a placeholder with no literal
    skeleton at all (e.g. "<declared-path>") — such a value carries no
    specific text to recognize, and wildcarding the whole thing would just match
    any word in the page, which is a false-positive generator, not a check.
    """
    parts = re.split(r"(<[^>]+>)", value)
    out: list[str] = []
    literal_chars = 0
    for part in parts:
        if part.startswith("<") and part.endswith(">"):
            out.append(r"\S+")
        elif part:
            out.append(re.escape(part))
            literal_chars += len(part)
    if literal_chars < 3:
        return None
    body = "".join(out)
    return rf"(?<![\w.-]){body}(?![\w.-])"


def _literal_pattern(value: str) -> str:
    return rf"(?<![\w.-]){re.escape(value)}(?![\w.-])"


def _candidate_values(entry: dict) -> list[tuple[str, bool]]:
    """Returns (value, templated) pairs — `templated=True` routes through the
    placeholder-aware recognizer, `False` through the literal one."""
    values: list[tuple[str, bool]] = []
    kind = entry.get("kind")
    if kind in _KEY_KINDS and entry.get("key"):
        values.append((entry["key"], True))
    if kind in _PATH_KINDS and entry.get("path"):
        values.append((entry["path"], True))
    if kind in _MARKER_KINDS:
        if entry.get("path"):
            values.append((entry["path"], True))
        for marker_field in ("begin_marker", "end_marker"):
            marker = entry.get(marker_field)
            if marker and marker != ABSENT_ON_LEGACY_INSTALLS:
                values.append((marker, False))
    return values


def _recognizers() -> dict[str, re.Pattern[str]]:
    recognizers: dict[str, re.Pattern[str]] = {}
    for entry in _declared_entries():
        for value, templated in _candidate_values(entry):
            if value in _ALLOWLIST or value in recognizers:
                continue
            pattern_src = _placeholder_pattern(value) if templated else _literal_pattern(value)
            if pattern_src is None:
                continue
            recognizers[value] = re.compile(pattern_src)
    assert recognizers, "expected at least one recognizer derived from the declarations"
    return recognizers


def test_declared_entries_span_multiple_kinds():
    # Sanity precondition for the rest of this module: if the declared set is ever
    # empty or collapses to one kind, the scan below would trivially pass for
    # the wrong reason (nothing to recognize), not because the page is clean.
    entries = _declared_entries()
    kinds = {e.get("kind") for e in entries if e.get("kind")}
    assert len(entries) >= 20
    assert _KEY_KINDS & kinds
    assert _PATH_KINDS | _MARKER_KINDS & kinds


def test_page_enumerates_no_declared_write_surface():
    assert PAGE_PATH.exists(), f"expected the judgment page at {PAGE_PATH}"
    text = PAGE_PATH.read_text(encoding="utf-8")

    hits: list[str] = []
    for value, pattern in _recognizers().items():
        if pattern.search(text):
            hits.append(value)

    assert not hits, (
        "docs/wiki/uninstall-agentic-judgment.md enumerates declared write-surface "
        f"value(s) it must not name: {hits!r}. Point at the declaration/receipt by "
        "name instead of pasting the value."
    )


def test_allowlist_entries_carry_a_reason_and_are_not_declared_values():
    declared_values = {
        value
        for entry in _declared_entries()
        for value, _templated in _candidate_values(entry)
    }
    for value, reason in _ALLOWLIST.items():
        assert reason.strip(), f"allowlist entry {value!r} has no reason"
        assert value not in declared_values, (
            f"allowlisted value {value!r} collides with an actual declared surface "
            "value — remove it from the allowlist, it must be scanned for real"
        )


def test_recognizer_scan_actually_fails_closed_on_an_injected_surface(tmp_path):
    # Prove the scan bites: build a throwaway copy of the page with one real
    # declared surface spliced in, and confirm the same scan goes RED against
    # it. A green enumeration test that has never been seen to fail is worth
    # very little (dispatch brief's own verification requirement).
    injected_entry = next(
        e for e in _declared_entries() if e.get("kind") == "git-config-key" and e.get("key")
    )
    injected_value = injected_entry["key"]

    text = PAGE_PATH.read_text(encoding="utf-8")
    poisoned = text + f"\n\nDebug note: this writer touches `{injected_value}` directly.\n"

    recognizers = _recognizers()
    pattern = recognizers[injected_value]
    assert pattern.search(poisoned), "expected the injected literal surface to be caught"
    assert not pattern.search(text), "the real page should not already contain this literal"
