r"""coordinator_core.bash_guards.tests.test_no_machine_absolute_path_in_guard_messages
-- behavioral ratchet against the "thislaptop leak" defect class in
RENDERED guard/deny/advisory/nudge messages.

WHY BEHAVIORAL, NOT STATIC (the defect this ratchet exists for)

`coordinator/bin/check-machine-path-leak.py` structurally parses two
TRACKED files (`settings.json`, `working-repos.yaml`) for committed
absolute-path leaf VALUES. It explicitly does not grep raw source text
(see its own docstring's Negative-spec). That scope is why it never saw
the defect this module targets: `bash_guards._helpers.
_resolve_override_keys_doc_display()` used to render an absolute,
in-process-resolved path (e.g. ``/Users/<operator>/X/project-makima/docs/
reference/guard-override-keys.md``) into every hard-deny guard's
``permissionDecisionReason`` -- a string that exists ONLY at runtime, on
the machine that renders it. There is no absolute path anywhere in
SOURCE to find; the leak is entirely in rendered OUTPUT. A static
scanner has no view of that surface by construction, and existing
guard-message tests assert on affordances/substrings present either way
(portable or leaking) -- neither category could have caught this.
(Confirmed live during this dispatch: `_resolve_override_keys_doc_display`
has since been fixed at its one SSOT to the portable
``"project-makima docs/reference/guard-override-keys.md"`` form -- this
ratchet is the mechanical guarantee that fix (and every future guard
message) STAYS portable, not a report that the bug is still present.)

RELATIONSHIP TO `coordinator_core.ops.check_posix_exec_assumptions`

That module is the fleet's existing, broader Windows/portability ratchet
(twelve tiered violation classes; Tier D's `unresolved_cross_path` is the
class closest to this one's concern -- a tracked `.py` file containing a
hardcoded cross-machine path LITERAL). It could not have caught this
defect EITHER, and not by an oversight this module's own author could
patch as a new class: `scan()` walks `_tracked_files(root)` and matches
`_CROSS_PATH_PATTERNS` against `ast.Constant` STRING-LITERAL nodes only
(see its own `# -- unresolved_cross_path / unclassified: literal string
content --` branch). The actual defect was `Path(existing) / OVERRIDE_KEYS_DOC`
-- a `pathlib` join over a variable resolved by ANOTHER function call at
runtime, never a string literal anywhere in source. No AST-literal scan,
however broadened, can see a value that does not exist until the
resolver executes. That is the precise, structural reason this class
needs a BEHAVIORAL probe (this module) rather than a new static class:
`check_posix_exec_assumptions.scan()` is deliberately execution-free (it
`ast.parse()`s tracked files and never imports/runs them -- true of
every one of its twelve classes), and this module's whole mechanism is
the opposite: import real guard modules, fire them through real
PreToolUse payloads, and inspect the RENDERED string a resolver actually
produced. Folding that into `scan()` would mean executing arbitrary
project code inside what is otherwise a safe, dependency-free per-file
walk over every tracked file in a repo -- a different trust and
execution model, not a new `hits[cls]` bucket in the same loop. This
module is therefore a companion ratchet, not a duplicate: it reuses
`check_posix_exec_assumptions._CROSS_PATH_PATTERNS` as the shared
predicate SSOT (see `_ABS_PATH_PATTERNS` below) rather than inventing an
incompatible second definition of "looks like a cross-machine path,"
per that module's own Tier D note to "reconcile with (not duplicate)
`check-machine-path-leak.py`" -- the same discipline applied here to a
second static sibling.

Scope note on the PM's wider "Windows-hostile constructs generally"
framing: `path_separator` (Tier C, `.replace("/", "\\")`, `.split("\\")`,
literal `\\` concatenation) and `posix_mode_bits` (Tier C, `os.chmod`/
`st_mode` exec-bit tests) are ALREADY covered, statically and blocking,
by `check_posix_exec_assumptions`'s own AST scan over SOURCE -- this
module does not re-detect either pattern in RENDERED text, both because
that would duplicate a class that already blocks the build and because a
bare `/` or `\\` appearing somewhere in free-form guard prose (a doc
path, a URL, a shell example) has no reliable path-shaped anchor the way
an absolute-path literal does, so a text-scan for those two classes
would be a high-false-positive heuristic this module deliberately does
not ship. This module's own added surface is exactly the one gap named
above: an absolute machine/user/drive-rooted path that exists only once
RENDERED, which no static class (Tier A-D, `implicit_encoding`, or
`check-machine-path-leak.py`) can observe by construction.

COVERAGE MECHANISM

This module fires every row already registered in `guard_message_corpus`
-- the C1-C10 message-size-discipline corpus, which itself carries a
self-enforcing closer (`test_ac2_every_reachable_guard_has_a_corpus_row`)
proving every guard reachable from `bash_guards.dispatch._build_guard_chain`
(all three bands: CONFINEMENT_DENY, ADVISORY_REWRITE,
PLATFORM_CONDITIONED_DENY) and every guard `write_guards.engine.
discover_guard_names()` reports has at least one corpus row. Riding that
closer means THIS ratchet's coverage tracks the live registered guard
chain automatically -- a guard added tomorrow with no corpus row already
fails that other test, so by the time a row exists here, this module sees
it for free. No hand-listed roster of guard modules is maintained in this
file (the two-tables antipattern this codebase rejects).

NOT covered (named per this module's own instructions, not implied total
coverage):
  - The 17 `@register_op`-decorated async MCP-handler `coordinator_core/
    hooks/` modules `guard_message_corpus.py`'s own C10 section names
    (`agent_completion_log`, `context_pressure_precompact`, ... -- see
    that module's C10 comment block for the full list). `HookRow.fire`
    is a zero-arg SYNC callable; none of those 17 fit that shape without
    new async capture-harness infrastructure this dispatch does not build.
  - Any guard/hook message text reachable only through a path this
    corpus's `setup`/`payload_factory` fixtures do not exercise (e.g. an
    untested branch inside one guard's own conditional message assembly).
    The corpus's own `expected_speaker` firing+control-row discipline
    bounds this gap but does not eliminate it.
  - `coordinator/bin/*.py` CLI tooling and any DoE-side (coordinator-claude
    repo) shim prose -- out of this repo's tree.

THE PREDICATE

Platform-INDEPENDENT by construction: plain regex over rendered text,
never `os.path`/`pathlib` (which would only recognize the host's OWN
path grammar). Matches POSIX (`/Users/...`, `/home/...`, `/var/...`,
`/tmp/...`) and Windows (`C:\...`, `C:/...`, UNC `\\server\share\...`)
forms unconditionally, so a Windows-shaped leak fails this suite even
when it runs on macOS/Linux, and vice versa.

THE EXEMPTION MECHANISM

A narrow, named, individually-justified allowlist (`_EXEMPT_SPANS`
builder + `_is_corpus_fixture_echo`), not a blanket opt-out. As of
2026-08-11 (PM ruling, "a guard's block message must STOP carrying its
own unlock recipe"), `guard_unlock_sentinel.annotate_deny` no longer
renders the sentinel path at all -- the exemption that used to cover it
(structurally matched via `_SENTINEL_PREFIX`) is gone, not merely
unused, so `annotate_deny`'s output is now covered by this ratchet with
NO exception, same as any other guard message:

  1. ``grant-cli-invocation`` -- `block_unauthorized_claude_md_write.
     _grant_cli_invocation()` legitimately resolves an absolute,
     in-process root into a command the SAME operator pastes into their
     OWN shell immediately after reading it (that module's own docstring
     names this as the deliberate exception to the portable-pointer rule
     `_resolve_override_keys_doc_display` now follows). Exempted by
     calling the function fresh and stripping its EXACT literal output,
     not a wildcard pattern -- so a change to what it renders is measured
     against its own current behavior, never silently widened.
  2. ``corpus-fixture-echo`` -- this suite's own firing corpus builds
     REAL scratch git repos/files (see `guard_message_corpus.py`'s
     `fire_row`/`fire_write_guard_row`/`fire_hook_row`, all rooted under
     `tempfile.TemporaryDirectory()`) specifically so a guard has a real
     target to deny. A guard correctly naming that real path back to the
     agent (e.g. "you tried to write <path>") is doing its job, not
     leaking a doc/root pointer -- so any absolute-path match that is
     either (a) a literal substring of the `CorpusRow.input` text actually
     fired, or (b) rooted under `tempfile.gettempdir()` (this process's
     own temp root, covering setup-synthesized scratch paths `row.input`
     never mentions) is exempted. This is the SAME class as exemption 1,
     generalized to the corpus's own fixtures rather than one specific
     literal.
  3. ``write-guard-corpus-content-literal`` -- two `WRITE_GUARD_ROWS`
     fixtures (`guard_message_corpus._wg_concrete_path_citations_fire`,
     `_wg_settings_json_write_fire`) deliberately write FILE CONTENT
     containing a synthetic Windows-shaped path string, specifically to
     prove `guard_concrete_path_citations`/`guard_settings_json_write`
     detect and quote it back -- both fixture literals are already marked
     ``abs-path-ok: synthetic fixture, not a real path`` at their own
     definition in that corpus module. `WriteGuardRow`'s capture seam
     (`fire_write_guard_row`) does not return the payload it fired (no
     `row_input` equivalent reaches this module, unlike `CorpusRow.input`
     for the bash rows above -- see exemption 2), so these two exact
     literals are named here instead of diffed structurally. Kept to
     exactly the two strings the corpus already documents as synthetic,
     not widened into a pattern.
  4. ``resolved-interpreter-invocation`` -- the BX-16 auto-rewrite rows
     (`check_find_exec_rewrite`/`check_grep_via_bash_rewrite`/
     `check_multiprobe_banner_rewrite`/`check_head_tail_plumbing_rewrite`)
     all prefix their emitted ``updatedInput.command`` with
     ``_bt_python3_invocation()``'s resolved interpreter path -- on a
     python.org Windows install with no ``python3`` on PATH, that is an
     absolute, machine-resolved ``python.exe`` path. Same rationale class
     as exemption 1 (a value "meant to be pasted into a shell has to be
     real" -- here, a value the HARNESS re-executes verbatim, which is an
     even harder requirement than an operator pasting it by hand): this
     is not the doc-pointer leak class the ratchet targets, it is the
     necessarily-real command the auto-rewrite exists to produce. Matched
     structurally by calling ``_bt_python3_invocation()`` fresh and
     stripping its exact literal output, same discipline as exemption 1,
     so a change to what it resolves is measured against its own current
     behavior, never a hand-copied path fragment.

Spec backlink: PM dispatch, 2026-08-05, "the 'why didn't our tests catch
it' half" (companion to the peer executor's per-site `_helpers.py` fix).
"""

from __future__ import annotations

import re
import tempfile
from typing import Any, Dict, List, Tuple

from coordinator_core.bash_guards._helpers import (
    operator_override_note,
    resolve_override_keys_doc_display,
)
from coordinator_core.bash_guards.tests.guard_message_corpus import (
    ADVISORY_REWRITE_ROWS,
    CONFINEMENT_ROWS,
    HOOK_ROWS,
    PLATFORM_CONDITIONED_ROWS,
    WRITE_GUARD_ROWS,
    fire_hook_row,
    fire_row,
    fire_write_guard_row,
)
from coordinator_core.session.guard_unlock_sentinel import annotate_deny
from coordinator_core.write_guards.block_unauthorized_claude_md_write import (
    _grant_cli_invocation,
)
from coordinator_core.ops.check_posix_exec_assumptions import (
    _CROSS_PATH_PATTERNS as _TIER_D_CROSS_PATH_PATTERNS,
)
from coordinator_core.bash_guards.dispatch_checks import _bt_python3_invocation

# ---------------------------------------------------------------------------
# The predicate -- platform-independent, regex-only (see module docstring).
#
# SSOT reuse, not a second definition (module docstring's "RELATIONSHIP TO
# check_posix_exec_assumptions"): `_TIER_D_CROSS_PATH_PATTERNS` already
# encodes this project's one definition of "looks like a cross-machine
# path" for `/Users/<name>/`, `/home/<name>/`, drive-letter, and UNC forms
# -- including the deliberate single-letter-URL-scheme false-positive fix
# recorded on that constant's own definition (`s://` must not match). Those
# patterns are `^`-anchored (correct for matching a WHOLE ast.Constant
# string); this module instead scans free-form rendered PROSE, where the
# offending path sits mid-string (e.g. "-- full list: /Users/..."), so each
# pattern's anchor is stripped and recompiled unanchored below rather than
# hand-copying new regex bodies that could drift from the shared source.
def _unanchor(pattern: re.Pattern) -> re.Pattern:
    body = pattern.pattern.lstrip("^")
    if body == r"\\\\":
        # Review: code-reviewer (Finding 3) -- stripping the `^` anchor off
        # the UNC pattern leaves just "two literal backslashes", which then
        # matches ANY doubled-backslash artifact mid-string, not only a
        # genuine `\\server\share`-shaped path. Require at least one
        # non-backslash segment plus a following backslash so the
        # recompiled predicate still needs a real UNC-shaped tail.
        return re.compile(body + r"[^\s\\]+\\[^\s\"'`]*")
    return re.compile(body + r"[^\s\"'`]*")


_ABS_PATH_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    _unanchor(p) for p in _TIER_D_CROSS_PATH_PATTERNS
) + (
    # Additive over Tier D (brief-named classes that constant does not
    # cover): Tier D deliberately buckets a bare `/tmp/` literal under its
    # OWN `unclassified` catch-all rather than `unresolved_cross_path` (see
    # that module's `if s.startswith("/tmp/"): hits["unclassified"]`), and
    # never covers `/var/` at all. Both are explicitly named in this
    # dispatch's own predicate requirement ("must catch POSIX (`/Users/...`,
    # `/home/...`, `/var/...`, `/tmp/...`)"), so they are added here rather
    # than left uncovered because the sibling module classifies them
    # differently for its own purposes.
    re.compile(r"/var/[^\s\"'`]+"),
    re.compile(r"/tmp/[^\s\"'`]+"),
)


def _find_absolute_paths(text: str) -> List[str]:
    """Every machine-absolute-path SPAN in `text`, POSIX or Windows-shaped,
    independent of the host this test runs on."""
    found: List[str] = []
    for pattern in _ABS_PATH_PATTERNS:
        found.extend(m.group(0) for m in pattern.finditer(text))
    return found


# ---------------------------------------------------------------------------
# Exemption mechanism -- see module docstring's numbered list.
# ---------------------------------------------------------------------------

_TEMP_ROOT = tempfile.gettempdir()

#: Exemption 5 -- exact literals only, see module docstring. Sourced from
#: `guard_message_corpus.py`'s own `_wg_concrete_path_citations_fire`/
#: `_wg_settings_json_write_fire`, both already marked
#: `abs-path-ok: synthetic fixture, not a real path` at their definition.
_WRITE_GUARD_CORPUS_CONTENT_LITERALS: Tuple[str, ...] = (
    "X:\\some-checkout",  # abs-path-ok: synthetic fixture, not a real path
    "C:\\Users\\someone\\x",
)


def _canon_backslashes(s: str) -> str:
    """Collapse any run of repeated backslash-escaping down to single
    backslashes -- a guard quoting an offending value back into its own
    message (observed live: `guard_settings_json_write` renders it through
    a quoting step that doubles each `\\`) means the SAME literal can
    appear doubled, quadrupled, or plain depending on how many quoting
    layers it passed through; comparing on the collapsed form makes the
    exemption check indifferent to how many layers were applied."""
    while "\\\\" in s:
        s = s.replace("\\\\", "\\")
    return s


def _contains_normalized(haystack: str, needle: str) -> bool:
    """Substring test on the backslash-collapsed form of both sides, so a
    named exemption literal matches regardless of how many quoting layers
    re-escaped it in the rendered message (see `_canon_backslashes`)."""
    if not needle:
        return False
    return _canon_backslashes(needle) in _canon_backslashes(haystack)


def _is_exempt(
    span: str, *, row_input: str, grant_cli_text: str, interpreter_text: str = ""
) -> bool:
    """True if `span` (one `_find_absolute_paths` match) is a named,
    individually-justified legitimate absolute path rather than a leak."""
    # 1. grant-CLI-invocation -- exact literal produced by the function
    #    itself, called fresh per assertion (see caller).
    if grant_cli_text and _contains_normalized(grant_cli_text, span):
        return True
    # 4. Resolved-interpreter-invocation -- exact literal produced by
    #    `_bt_python3_invocation()` itself, called fresh per assertion
    #    (see module docstring item 4 and caller).
    if interpreter_text and _contains_normalized(interpreter_text, span):
        return True
    # 2. Corpus-fixture echo -- either literally present in the fired
    #    input text, or rooted under this process's own temp directory
    #    (covers setup-synthesized scratch repos `row_input` never names).
    if row_input and _contains_normalized(row_input, span):
        return True
    if span.startswith(_TEMP_ROOT):
        return True
    # 3. Write-guard corpus content literals -- see module docstring item 3.
    for literal in _WRITE_GUARD_CORPUS_CONTENT_LITERALS:
        if _contains_normalized(span, literal) or _contains_normalized(literal, span):
            return True
    return False


def _collect_violations(
    guard_name: str,
    row_id: str,
    envelope: Any,
    *,
    row_input: str = "",
    grant_cli_text: str = "",
    interpreter_text: str = "",
) -> List[str]:
    """Recursively walk `envelope` (the exact hookSpecificOutput-shaped
    dict/list/str tree a guard/hook renders), returning one formatted
    violation string per non-exempt absolute-path match."""
    violations: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for span in _find_absolute_paths(node):
                if not _is_exempt(
                    span,
                    row_input=row_input,
                    grant_cli_text=grant_cli_text,
                    interpreter_text=interpreter_text,
                ):
                    violations.append(
                        "%s/%s: leaked %r in message text" % (guard_name, row_id, span)
                    )
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(envelope)
    return violations


# ---------------------------------------------------------------------------
# The ratchet itself -- fires every row in every corpus band, collects EVERY
# violation across the whole run (not stop-on-first), and reports the full
# list. Deliberately one assertion, not per-row parametrization: the point
# of this dispatch's "report the full list" deliverable is a single,
# complete scope-of-the-class finding, not N independent pass/fail cells.
# ---------------------------------------------------------------------------


def test_no_machine_absolute_path_in_any_corpus_guard_message():
    grant_cli_text = _grant_cli_invocation()
    interpreter_text = _bt_python3_invocation()
    violations: List[str] = []

    for row in CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS:
        capture = fire_row(row)
        if capture.envelope is None:
            continue
        violations.extend(
            _collect_violations(
                row.guard,
                row.row_id,
                capture.envelope,
                row_input=row.input,
                grant_cli_text=grant_cli_text,
                interpreter_text=interpreter_text,
            )
        )

    for row in WRITE_GUARD_ROWS:
        if row.unverified_reason is not None:
            continue
        capture = fire_write_guard_row(row)
        if capture.envelope is None:
            continue
        violations.extend(
            _collect_violations(
                row.guard,
                row.row_id,
                capture.envelope,
                grant_cli_text=grant_cli_text,
                interpreter_text=interpreter_text,
            )
        )

    for row in HOOK_ROWS:
        capture = fire_hook_row(row)
        if capture.envelope is None:
            continue
        violations.extend(
            _collect_violations(
                row.guard,
                row.row_id,
                capture.envelope,
                grant_cli_text=grant_cli_text,
                interpreter_text=interpreter_text,
            )
        )

    assert not violations, (
        "machine-absolute path(s) leaked into rendered guard/advisory/hook "
        "message(s) -- see module docstring for the exemption mechanism if "
        "one of these is a false positive, not a real leak:\n  "
        + "\n  ".join(sorted(set(violations)))
    )


# ---------------------------------------------------------------------------
# Direct tests of the confirmed defect's exact seam (`_resolve_override_
# keys_doc_display`/`operator_override_note`, and the `annotate_deny`
# funnel every hard-deny guard passes through) -- independent of the
# corpus, so a regression here is unambiguous (no exemption applies: these
# functions carry no legitimate absolute-path leg at all).
# ---------------------------------------------------------------------------


def test_override_keys_doc_display_is_portable():
    doc_display = resolve_override_keys_doc_display()
    assert not _find_absolute_paths(doc_display), (
        "resolve_override_keys_doc_display() rendered a machine-absolute "
        "path: %r" % doc_display
    )


def test_operator_override_note_is_portable():
    note = operator_override_note(
        "COORDINATOR_ALLOW_SOME_GUARD", payload={"session_id": "sess-c1d-em"}
    )
    assert not _find_absolute_paths(note), (
        "operator_override_note() rendered a machine-absolute path: %r" % note
    )


def test_operator_override_note_reason_shaped_is_portable():
    note = operator_override_note(
        "COORDINATOR_QUEUE_PUNT",
        payload={"session_id": "sess-c1d-em"},
        reason_placeholder="<reason>",
    )
    assert not _find_absolute_paths(note), (
        "operator_override_note(reason_placeholder=...) rendered a "
        "machine-absolute path: %r" % note
    )


def test_annotate_deny_funnel_renders_no_machine_absolute_path_at_all():
    """`annotate_deny` is the SINGLE seam both `write_guards.engine` and
    `bash_guards.dispatch` route every hard-deny through (see that
    function's own docstring) -- exercising it directly, independent of
    any one guard's own message text, pins the funnel itself rather than
    relying only on corpus rows that happen to trigger a hard-deny.

    Superseded (2026-08-11, PM ruling -- "a guard's block message must
    STOP carrying its own unlock recipe"): this used to exempt the
    sentinel path it rendered (`_SENTINEL_PREFIX`-matched). `annotate_deny`
    no longer renders the sentinel path at all, so that exemption is gone
    and this asserts the funnel leaks NO machine-absolute path,
    full stop -- an assertion that would fail on the pre-2026-08-11 text
    (which rendered a real `tempfile.gettempdir()`-rooted sentinel path)
    and passes on the current one, unlike the old form which would have
    passed either way once the sentinel-exemption carve-out existed."""
    envelope: Dict[str, Any] = {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "example guard deny reason",
        }
    }
    annotated = annotate_deny(
        envelope,
        "test-session-abc123",
        "example-guard",
        resolve_override_keys_doc_display(),
    )
    reason = annotated["hookSpecificOutput"]["permissionDecisionReason"]
    violations = _find_absolute_paths(reason)
    assert not violations, (
        "annotate_deny() rendered a machine-absolute path: %r in %r"
        % (violations, reason)
    )
