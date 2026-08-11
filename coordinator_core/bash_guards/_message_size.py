"""coordinator_core.bash_guards._message_size -- the byte-measurement core
for guard message-size discipline.

Spec backlink: docs/plans/2026-08-02-guard-message-size-discipline.md,
chunk C2, § Problem's "Correction: the 440-byte cap compounds a ceiling
and inverts the duty-of-care incentive" and "Correction: the speaker
predicate reconstitutes the silent-shim trap one level down".

Given one guard envelope (the return shape `dispatch.GuardEntry.fn()` or
any of the five `_hook_envelope` builders produce), this module answers
four questions: how many prose bytes did the guard actually spend on ITS
OWN advisory content (`prose_bytes`), how many bytes were exempted as a
concrete offered alternative (`exempt_bytes`, e.g. a backtick command or an
indented rewrite block), how many bytes are FOUND DATA the guard is
echoing back rather than authoring (`data_bytes` -- see "FOUND DATA vs.
AUTHORED PROSE" below), and is this cell a "speaker" at all
(`is_speaker`). It does not decide liveness (that is
`_alternative_liveness`'s job, a different question over the same text) and
it does not run on the production dispatch hot path (Anti-scope) -- it is
test-suite-only measurement.

Modeled on `_guard_coverage.py`'s shape (pure measurement functions over a
shipped envelope/command, no new pattern-matching invented here), not on
`_alternative_liveness`'s liveness-verdict logic.

Exempt-span extraction reuses, verbatim, the four SHIPPED extraction
primitives `_alternative_liveness` already ships (`_BACKTICK_RE`,
`_INDENTED_CMD_RE`, `_LABELED_INDENT_BLOCK_RE`, `_cue_windows`) -- this
module authors no new regex for the prose/exempt split. It deliberately
does NOT import `_alternative_liveness.classify_band` (a same-named but
UNRELATED OURS/PEER ownership split -- who currently owns fixing a guard,
not this module's advisory/confinement/platform band) and does NOT import
anything below `_alternative_liveness.py:845` (liveness-verdict logic, out
of scope).

FOUND DATA vs. AUTHORED PROSE (`data_bytes`) -- a distinct accounting
class, alongside `prose_bytes`/`exempt_bytes`/`tail_bytes`, for content a
guard is ECHOING BACK from the world (paths it found, values it read)
rather than SENTENCES ITS AUTHOR WROTE. `check_destructive_git_revert`'s
deny message is the motivating case: it lists every denied path, one
indented line each, and `_is_diagnostic_echo` (correctly) refuses to treat
"what was denied" as an offered alternative -- so those lines land in
`prose_bytes` today, and the message's size tracks the CALLER's path
count/length, which is unbounded (measured: 220 bytes at 1 short path, 269
at 1 long path, 429 at 3 long paths). `data_bytes` is measured and
reported, never silently exempted -- a reader can see a cell is "200 bytes
of prose, 900 of data", never just a smaller prose number with no
explanation.

ABUSE RESISTANCE, stated plainly (this is the load-bearing half of the
mechanism, not a caveat): the data class is STRUCTURAL, derived from the
SHAPE a guard's own message already has, not from any keyword an author
could type into ordinary prose to duck the cap. A span only counts as
`data_bytes` when ALL of these hold simultaneously:

  1. It sits inside a `_LABELED_INDENT_BLOCK_RE` match -- the same
     shipped, already-reused "colon (or closing paren), immediate
     newline, then one or more indented lines with no blank-line gap"
     shape `_exempt_span_bytes` already keys off of. No new positional
     regex is authored for this test; `_data_block_spans` below is a
     structural re-application of an existing shipped primitive, plus a
     small positional helper (`_cue_window_spans`) that mirrors
     `_alternative_liveness._cue_windows`'s own arithmetic over the SAME
     shipped `_CUE_WINDOW_RE`/`_CUE_WINDOW_MAX_CHARS`, only to return
     spans instead of substrings.
  2. EVERY non-blank line inside that indented block IS A PATH ENTRY,
     never merely "contains a path-shaped token" -- `_is_path_entry_line`:
     the line, minus one trailing short/punctuation-free `(...)` reason,
     is a SINGLE whitespace-free token, and that token contains `/`.
     WORD COUNT is the load-bearing half of this check, not slash
     presence: an earlier revision keyed off "line contains a slash",
     which review correctly reopened -- ordinary prose routinely contains
     a slash (`and/or`, `his/her`, a URL, or one an author inserts on
     purpose), and worse, a sentence-with-a-slash could never disqualify
     a block under that rule precisely because slash-containment was also
     what qualified every other line. Word count closes both directions
     at once: a sentence is many space-separated words and fails the
     single-token test regardless of whether a slash appears anywhere in
     it, so an author cannot pad authored prose past the cap by dropping
     a slash into each line (see
     `test_prose_paragraph_with_slashes_in_every_line_stays_prose` and
     `test_smuggled_prose_line_with_slash_disqualifies_whole_block` in
     `tests/test_message_size.py`), and a genuinely non-path sentence
     mixed into an otherwise-genuine list still disqualifies the WHOLE
     block back to prose (an author cannot smuggle one authored sentence
     in and have it ride along uncharged). An ordinary paragraph indented
     under a colon with no path-shaped lines at all stays 100% prose too
     (`test_indented_prose_paragraph_without_path_tokens_stays_prose`).
  3. The span does not overlap a `_cue_windows` window -- an offered
     alternative (a command block after "Use instead:"/"Did you mean" /
     etc.) is `_exempt_span_bytes`'s job, not this class's; the two
     accounting classes must never double-subtract the same bytes.

Residual, named honestly rather than hidden: a denied path with no
directory separator at all (a bare top-level filename) is not a path
entry by this definition and stays charged as prose. That is a
deliberate, narrow miss, not an oversight -- a bare filename is short and
bounded, not the unbounded-growth vector (path count x path length) this
class exists to catch, and dropping the slash requirement entirely (to
catch that case) would let a single made-up one-word "entry" per line
pass as data with no path shape at all -- word count alone disqualifies
sentences, but does not by itself prove a token is a PATH. Slash-shaped
tokens only.

NEGATIVE SPEC -- `MESSAGE_PROSE_CAP_BYTES` is `220`, not `440`. The prior
`2 x 220` derivation double-budgeted a ceiling as though it were a measured
sentence and inverted the duty-of-care incentive (rewarding a guard for
dropping its own escape hatch); see the plan's own correction for the full
argument. Every consumer of this module names `MESSAGE_PROSE_CAP_BYTES`;
none restates the number.

NEGATIVE SPEC 2 -- the speaker predicate is `prose_bytes > 0`, never
"envelope is not None". `dispatch._resolve_suppressed_envelope` manufactures
non-`None`, zero-prose (rewrite-leg-only) envelopes on non-Windows hosts;
an envelope-based predicate would silently admit those into a leg-2
distribution and pre-satisfy the mean-bytes bound arithmetically. This
module owns the predicate because it owns the byte split.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from coordinator_core.bash_guards._alternative_liveness import (
    _BACKTICK_RE,
    _CUE_WINDOW_MAX_CHARS,
    _CUE_WINDOW_RE,
    _cue_windows,
    _INDENTED_CMD_RE,
    _LABELED_INDENT_BLOCK_RE,
)
from coordinator_core._hook_envelope import COORDINATOR_PROVENANCE_MARKER
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards.dispatch import GuardBand

#: The cap on a guard's OWN prose, measured with the mandatory
#: `operator_override_note` tail already subtracted by identity -- NOT
#: 2x220. See module docstring NEGATIVE SPEC above; every AC and every
#: downstream consumer names this constant, none restates the number.
MESSAGE_PROSE_CAP_BYTES = 220

#: The two `hookSpecificOutput` sub-fields that ever carry guard prose,
#: across all six `_hook_envelope` shapes. `updatedInput` (rung A) is
#: deliberately absent -- its `command` is a structured rewrite field, not
#: rendered text, so it is excluded by never being read into the measured
#: string in the first place (algorithm step 3), not by pattern-matching it
#: back out.
_PROSE_FIELDS: Tuple[str, ...] = ("additionalContext", "permissionDecisionReason")

#: A directory-derived proxy band is namespaced `directory:<bucket>` so it
#: can never collide with a real `GuardBand.value` string (all three of
#: which are hyphenated, non-namespaced tokens).
_PROXY_BAND_PREFIX = "directory:"


@dataclass(frozen=True)
class MessageSizeMeasurement:
    """One envelope's byte-measurement result.

    `band` is a plain `str` (either a `GuardBand.value`, e.g.
    `"advisory-rewrite"`, or a directory-derived proxy band, e.g.
    `"directory:write_guards"`) rather than the `GuardBand` enum itself --
    callers with no `GuardBand` at all (write_guards/hooks rows) still need
    a band string to bucket by, and forcing them through a real `GuardBand`
    member would be exactly the "directory bucket masquerading as a
    duty-of-care classification" the plan's own correction forbids.

    `data_bytes` is FOUND DATA (paths/values the guard is echoing back),
    measured and reported but never counted against `prose_bytes`/the cap
    -- see module docstring "FOUND DATA vs. AUTHORED PROSE". Invariant:
    `prose_bytes == max(0, total_bytes - exempt_bytes - tail_bytes -
    data_bytes)`.
    """

    total_bytes: int
    exempt_bytes: int
    tail_bytes: int
    data_bytes: int
    prose_bytes: int
    band: Optional[str]
    is_speaker: bool
    over_cap: bool


def proxy_band(directory: str) -> str:
    """A directory-derived proxy band for a write_guards/hooks row that
    carries no `dispatch.GuardBand` -- named as a directory bucket, never
    folded into `GuardBand` itself (plan's own correction: "the directory-
    derived proxy band... is a directory bucket, not a duty-of-care
    classification")."""
    return _PROXY_BAND_PREFIX + directory


def resolve_band(band: Optional[Union[GuardBand, str]]) -> Optional[str]:
    """Normalize a `GuardBand` member, a pre-resolved proxy-band string, or
    `None` into the plain `str` `MessageSizeMeasurement.band` stores."""
    if band is None:
        return None
    if isinstance(band, GuardBand):
        return band.value
    return band


def _extract_prose_text(envelope: Optional[Dict[str, Any]]) -> str:
    """Pull the rendered prose out of one guard envelope: whichever of
    `additionalContext` / `permissionDecisionReason` is present under
    `hookSpecificOutput`. `updatedInput.command` (rung A) is never read
    here -- algorithm step 3's exemption is "never read into the measured
    string", not a post-hoc filter."""
    if not envelope:
        return ""
    hso = envelope.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return ""
    parts = [hso[field] for field in _PROSE_FIELDS if isinstance(hso.get(field), str) and hso.get(field)]
    return "\n".join(parts)


def _merge_spans(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Union a list of (possibly overlapping) `(start, end)` character
    ranges into their non-overlapping cover -- a backtick span can sit
    inside an indented block (or vice versa), and double-counting the
    overlap would double-subtract those bytes from prose."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


#: Review: coordinator:code-reviewer (Finding 1, guard-message-size-
#: discipline) -- a diagnostic-echo line (what was denied) is not an
#: offered alternative and must never be exempted from the prose cap
#: merely because it sits inside an indented cue-window run. Mirrors
#: `_alternative_liveness`'s own raw-indented-line skip-list
#: (`candidate.startswith(("Subagent:", "Command:", "Denied:", "Reason:"))`,
#: `_alternative_liveness.py`), extended with `"Detected:"` for the same
#: reason the reviewer named: `check_test_suite_invocation.py`'s
#: `Detected: %s` lines describe the triggering input, not an alternative.
_DIAGNOSTIC_LINE_PREFIXES: Tuple[str, ...] = ("Subagent:", "Command:", "Denied:", "Reason:", "Detected:")


def _is_diagnostic_echo(candidate: str) -> bool:
    """True when an indented line is a diagnostic echo (what-was-denied
    prose), never a concrete offered alternative, regardless of where it
    falls relative to a cue window."""
    return candidate.strip().startswith(_DIAGNOSTIC_LINE_PREFIXES)


def _exempt_span_bytes(text: str) -> int:
    """Algorithm steps 1-2: within each `_cue_windows` span, find backtick
    (`_BACKTICK_RE`) and indented/labeled-block (`_INDENTED_CMD_RE`,
    `_LABELED_INDENT_BLOCK_RE`) spans, union their ranges, and sum the
    exempted bytes. Deliberately NOT re-run outside cue windows -- unlike
    `_alternative_liveness`'s own broader backtick scan, this module's
    charter is "prose vs. offered alternative", so exemption stays anchored
    to the same cue-window discipline that module's own `_ALT_CUE_RE`/
    `_CUE_WINDOW_RE` split enforces, to avoid over-exempting a stray
    backtick in ordinary prose.

    `_LABELED_INDENT_BLOCK_RE` exempts only its capture group (the indented
    lines themselves), not the whole match -- the whole match also includes
    the introducing colon/blank-line prefix, which is prose, not an offered
    command.

    Neither regex is content-aware on its own, so a line matching either
    pattern is additionally checked against `_is_diagnostic_echo` before it
    is allowed into the exempt spans -- a `Detected:`/`Command:`/`Denied:`/
    `Reason:`/`Subagent:` line is never a concrete offered alternative no
    matter how it is indented (Finding 1, see `_DIAGNOSTIC_LINE_PREFIXES`).
    A multi-line `_LABELED_INDENT_BLOCK_RE` match is filtered per-line so a
    genuine alternative line is not swept out along with a diagnostic-echo
    line sharing the same labeled block.
    """
    total = 0
    for window in _cue_windows(text):
        spans: List[Tuple[int, int]] = []
        spans.extend(m.span() for m in _BACKTICK_RE.finditer(window))
        spans.extend(
            m.span()
            for m in _INDENTED_CMD_RE.finditer(window)
            if not _is_diagnostic_echo(m.group(1))
        )
        for block_match in _LABELED_INDENT_BLOCK_RE.finditer(window):
            block_start = block_match.start(1)
            block_text = block_match.group(1)
            for line_match in re.finditer(r"[^\n]*\n?", block_text):
                line_text = line_match.group()
                if not line_text or _is_diagnostic_echo(line_text):
                    continue
                spans.append((block_start + line_match.start(), block_start + line_match.end()))
        for start, end in _merge_spans(spans):
            total += len(window[start:end].encode("utf-8"))
    return total


#: A trailing `(...)` parenthetical reason, if present -- e.g.
#: `(load-bearing)`, `(peer-claimed by s1)`, `(load-bearing, peer-claimed
#: by s1)`. Comma is deliberately allowed inside the reason (the real
#: call site joins two reasons with one); terminal sentence punctuation
#: is not (`_is_path_entry_line` below).
_TRAILING_PAREN_RE = re.compile(r"\s\(([^()]*)\)\s*$")

#: Sentence-shaped punctuation a genuine short reason phrase never needs.
#: Its presence in the parenthetical is the signal an author is writing
#: prose inside what looks like a reason, not naming one fact.
_SENTENCE_PUNCTUATION_RE = re.compile(r"[.!?;]")

#: Cap on the parenthetical reason's own length -- a real reason
#: ("load-bearing, peer-claimed by <session-id>") is short; a paragraph
#: hiding inside parentheses is not.
_MAX_REASON_CHARS = 80


def _is_path_entry_line(line: str) -> bool:
    """True when `line` (already stripped of its leading indent) is A PATH
    ENTRY, not a sentence -- the discriminator this class actually needs.

    An earlier revision asked "does this line CONTAIN a path-shaped
    token" (slash presence). That failed both directions: ordinary prose
    routinely contains a slash (`and/or`, `his/her`, a URL, an author who
    inserts one on purpose to duck the cap), and a single non-path line
    could never disqualify a block precisely because slash-containment
    was also what qualified every OTHER line -- there was no line shape
    a sentence-with-a-slash could take that failed the old test.

    The real distinguishing shape: a genuine entry is ONE whitespace-free
    token, optionally followed by a short, punctuation-free parenthetical
    reason. Prose is many space-separated words. Word count -- can this
    line be written as one token plus one short aside -- is the
    discriminator an author cannot satisfy while also writing a sentence;
    slash presence is retained only as a secondary structural check on
    the token itself, never sufficient alone (this is the exact ask from
    the reopened review: "word count is the cheap, robust one; slash
    presence is not").
    """
    stripped = line.strip()
    if not stripped:
        return False

    core = stripped
    paren_match = _TRAILING_PAREN_RE.search(stripped)
    if paren_match:
        core = stripped[: paren_match.start()]
        reason = paren_match.group(1)
        if not reason:
            return False
        if len(reason) > _MAX_REASON_CHARS:
            return False
        if _SENTENCE_PUNCTUATION_RE.search(reason):
            return False

    if not core:
        return False
    if re.search(r"\s", core):
        # More than one token outside the parenthetical -- a sentence
        # (or a sentence with a trailing "(...)"-shaped aside bolted on),
        # never a single path entry.
        return False
    if "/" not in core:
        # Retained as a secondary structural signal on the token itself
        # (a real repo/filesystem path has a separator); never sufficient
        # on its own -- see the word-count check above, which is what
        # actually carries the anti-gaming property.
        return False
    return True


def _cue_window_spans(text: str) -> List[Tuple[int, int]]:
    """Positional twin of `_alternative_liveness._cue_windows`: same
    shipped `_CUE_WINDOW_RE`/`_CUE_WINDOW_MAX_CHARS`, same bounds
    arithmetic, returning `(start, end)` character spans instead of
    substrings. Needed so `_data_block_spans` can exclude any span already
    inside a cue window (an offered alternative is `_exempt_span_bytes`'s
    class, never `data_bytes`'s -- the two must not double-subtract the
    same bytes)."""
    spans: List[Tuple[int, int]] = []
    for m in _CUE_WINDOW_RE.finditer(text):
        start = m.end()
        blank = text.find("\n\n", start)
        end = blank if blank != -1 else len(text)
        end = min(end, start + _CUE_WINDOW_MAX_CHARS)
        spans.append((start, end))
    return spans


def _within_any_span(start: int, end: int, spans: List[Tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def _data_block_spans(text: str) -> List[Tuple[int, int]]:
    """Algorithm for `data_bytes`: every `_LABELED_INDENT_BLOCK_RE` block
    (colon/paren, immediate indent, no blank-line gap -- the SAME shipped
    shape `_exempt_span_bytes` already keys off of) where EVERY non-blank
    line IS a path entry (`_is_path_entry_line`: one whitespace-free
    token, optional short punctuation-free parenthetical reason -- word
    count, not slash presence, is what disqualifies a sentence), and the
    block does not fall inside a cue window (that is `_exempt_span_bytes`'s
    territory). A single line failing `_is_path_entry_line` disqualifies
    the WHOLE block back to prose -- see module docstring point 2; this is
    what stops an author from smuggling one authored sentence into an
    otherwise-genuine data block."""
    cue_spans = _cue_window_spans(text)
    spans: List[Tuple[int, int]] = []
    for block_match in _LABELED_INDENT_BLOCK_RE.finditer(text):
        block_start = block_match.start(1)
        block_text = block_match.group(1)
        line_matches = [m for m in re.finditer(r"[^\n]*\n?", block_text) if m.group().strip()]
        if not line_matches:
            continue
        if not all(_is_path_entry_line(m.group()) for m in line_matches):
            continue
        for m in line_matches:
            abs_start = block_start + m.start()
            abs_end = block_start + m.end()
            if _within_any_span(abs_start, abs_end, cue_spans):
                continue
            spans.append((abs_start, abs_end))
    return spans


def _data_block_bytes(text: str) -> int:
    total = 0
    for start, end in _merge_spans(_data_block_spans(text)):
        total += len(text[start:end].encode("utf-8"))
    return total


#: Sentinel key rendered through `operator_override_note` at import time
#: purely to discover the LITERAL TEXT that immediately precedes the env-var
#: name in each of its two branches (flag-shaped, `reason_placeholder=`-
#: shaped) -- never a hardcoded transcription of that builder's prose. See
#: `_derive_override_env_var_prefixes` for why this must stay derived.
_OVERRIDE_PREFIX_SENTINEL = "ZZZOVERRIDESENTINELZZZ"


def _derive_override_env_var_prefixes() -> List[str]:
    """Recover the literal prefix text `operator_override_note` renders
    immediately before its `env_var` argument, for BOTH branches (default
    flag-shaped, and `reason_placeholder=`-shaped), by rendering the
    builder with a sentinel key and slicing off everything before the
    sentinel.

    NEGATIVE SPEC (2026-08-11 incident): `_OVERRIDE_ENV_VAR_RE` used to be
    an INDEPENDENT, hand-transcribed pattern (`r"\\b([A-Z][A-Z0-9_]{3,})=1\\b"`)
    keyed on a `KEY=1` assignment shape. `operator_override_note`'s
    2026-08-11 reshape (commit ba3bf8a98) removed the assignment form
    entirely (NEGATIVE SPEC 4 on that function -- the key is now named
    BARE), and this independent transcription silently decoupled: it
    recovered nothing, `_tail_bytes` subtracted nothing, and the ~170-byte
    SSOT tail landed back inside every guard's prose budget uncredited (30+
    corpus cells over the 220-byte cap). A pattern built by TRANSCRIBING a
    builder's output shape can always drift out of sync with the builder
    silently, because nothing ties the two together -- this function is the
    fix: it renders the ACTUAL builder and reads the prefix back off ITS
    OWN output, so a future reshape of `operator_override_note`'s rendered
    text automatically changes what this function derives too. Do not
    replace this with a second hand-copied literal, ever -- that is the
    exact defect class this function exists to close.
    """
    prefixes: List[str] = []
    for rendered in (
        operator_override_note(_OVERRIDE_PREFIX_SENTINEL),
        operator_override_note(_OVERRIDE_PREFIX_SENTINEL, reason_placeholder="a caller-supplied reason placeholder"),
    ):
        idx = rendered.index(_OVERRIDE_PREFIX_SENTINEL)
        prefixes.append(rendered[:idx])
    return prefixes


def _derive_override_env_var_common_suffix() -> str:
    """The two branches' prefixes (`_derive_override_env_var_prefixes`)
    diverge only in the parenthetical SHAPE text (``"(flag)"`` vs.
    ``'(reason, e.g. "<placeholder>")'`` -- the latter interpolates the
    CALLER's own `reason_placeholder` text, which `_discoverable_env_vars`
    cannot know in advance for an arbitrary guard message). Everything
    AFTER that parenthetical (``"), unsettable from inside this session --
    read only at hook-process spawn: "``) is identical between the two
    branches regardless of placeholder text, so anchoring on the common
    SUFFIX of the two derived prefixes -- rather than either prefix whole
    -- recovers the env var independent of which placeholder text a given
    call site used. Still fully derived from a live render, never a
    transcription of this literal string.
    """
    prefixes = _derive_override_env_var_prefixes()
    a, b = prefixes[0], prefixes[1]
    length = 0
    while length < min(len(a), len(b)) and a[len(a) - 1 - length] == b[len(b) - 1 - length]:
        length += 1
    return a[len(a) - length :]


_OVERRIDE_ENV_VAR_RE = re.compile(
    re.escape(_derive_override_env_var_common_suffix()) + r"([A-Z][A-Z0-9_]{3,})"
)


def _tail_bytes(
    text: str,
    override_env_var: Optional[str],
    *,
    override_reason_placeholder: Optional[str] = None,
) -> int:
    """Algorithm step 4: locate the mandatory `operator_override_note` tail
    BY IDENTITY and return its byte length if present. `override_env_var`
    must be the exact env var the guard's OWN call site passed to
    `operator_override_note` -- this is identity matching against a known
    SSOT fragment (call the same builder, subtract the exact string it
    returns), not a regex and not a fixed literal, since the doc path
    spliced into the rendered string is itself call-time-resolved
    (`_resolve_override_keys_doc_display`)."""
    candidates = [override_env_var] if override_env_var else _discoverable_env_vars(text)
    for candidate in candidates:
        tail = operator_override_note(candidate, reason_placeholder=override_reason_placeholder)
        if tail and tail in text:
            return len(tail.encode("utf-8"))
    return 0


def _discoverable_env_vars(text: str) -> List[str]:
    """Recover the env-var argument a guard passed to `operator_override_note`
    by reading it back off the rendered message.

    This does NOT weaken the identity guarantee `_tail_bytes` documents: the
    subtraction still only happens when the builder's own returned string
    appears verbatim in the message. What is inferred here is the ARGUMENT,
    not the tail — a wrong guess simply builds a string that does not match
    and subtracts nothing, which is the safe direction.

    The alternative — a hand-maintained env-var column on all 69 corpus rows —
    was rejected deliberately: it rots the moment a guard changes its override
    key, and it fails silently by over-counting prose (the tail lands back in
    the cap) rather than loudly. Recovering the argument from the message keeps
    the SSOT in the guard's own call site, where it belongs.

    NEGATIVE SPEC (2026-08-11) -- `_OVERRIDE_ENV_VAR_RE` MUST stay DERIVED
    from `operator_override_note`'s own render
    (`_derive_override_env_var_prefixes`), never re-transcribed as an
    independent literal pattern. The 2026-08-11 reshape of that builder
    (commit ba3bf8a98, removing the `KEY=1` assignment form) broke this
    recovery precisely because the prior pattern was authored as a
    standalone copy of the builder's output shape, with nothing tying the
    two together -- the builder changed, the copy did not, and recovery
    silently went to zero. Any future reshape of `operator_override_note`'s
    rendered text must not be able to decouple this regex again without
    also changing what it derives from.
    """
    return _OVERRIDE_ENV_VAR_RE.findall(text)


def _provenance_marker_bytes(text: str) -> int:
    """Bytes of the `[coordinator]` provenance marker present in `text`, or 0.

    Exempt for the same reason `_tail_bytes` is: the marker is FIXED, per-firing
    boilerplate identical on every message this suite emits (see
    `coordinator_core._hook_envelope.COORDINATOR_PROVENANCE_MARKER`), not prose
    the guard's author chose and could shorten. Counting a constant against a
    per-guard prose cap would charge all 69 corpus rows for a byte cost no
    individual guard can do anything about, and would push cells that sit just
    under the cap over it without a single word of their own prose changing.

    Counts only ONE occurrence, at the start, matching where `_stamp` puts it —
    the marker is idempotent by construction, so a second occurrence would be
    guard prose that happens to quote the marker, which SHOULD be charged.
    """
    marker = "%s " % COORDINATOR_PROVENANCE_MARKER
    return len(marker.encode("utf-8")) if text.startswith(marker) else 0


def measure_envelope(
    envelope: Optional[Dict[str, Any]],
    *,
    band: Optional[Union[GuardBand, str]] = None,
    override_env_var: Optional[str] = None,
    override_reason_placeholder: Optional[str] = None,
) -> MessageSizeMeasurement:
    """Measure one guard envelope: total / prose / exempt-span / found-data
    byte counts, plus band and the speaker/over-cap predicates.

    `envelope` is `None` for a non-firing guard on this input (the exact
    value `GuardEntry.fn()` returns) -- this degrades cleanly to a
    zero-byte, non-speaker measurement, not a raised error, since a `None`
    cell is itself part of what the corpus needs to see (non-triggering
    cells are required, not optional -- Anti-scope).

    `override_env_var` (and `override_reason_placeholder`) must be the
    exact arguments the guard's OWN call site passed to
    `operator_override_note`, for the identity-match tail subtraction
    (`_tail_bytes`). Passing nothing means "this guard's message carries no
    override tail" -- the tail subtraction is then a no-op, not an error.
    """
    text = _extract_prose_text(envelope)
    total_bytes = len(text.encode("utf-8"))
    exempt_bytes = _exempt_span_bytes(text) + _provenance_marker_bytes(text)
    tail_bytes = _tail_bytes(text, override_env_var, override_reason_placeholder=override_reason_placeholder)
    data_bytes = _data_block_bytes(text)
    prose_bytes = max(0, total_bytes - exempt_bytes - tail_bytes - data_bytes)
    return MessageSizeMeasurement(
        total_bytes=total_bytes,
        exempt_bytes=exempt_bytes,
        tail_bytes=tail_bytes,
        data_bytes=data_bytes,
        prose_bytes=prose_bytes,
        band=resolve_band(band),
        is_speaker=prose_bytes > 0,
        over_cap=prose_bytes > MESSAGE_PROSE_CAP_BYTES,
    )
