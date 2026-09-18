"""coordinator_core.hooks.support.message_envelope — the single
message-construction seam a Category-A (locally-authored, speaking) hook
routes through.

Ported from DoE-claude `coordinator/hooks/scripts/_message_envelope.py`
(40 consumers there) per
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C3, behavior
-preserving except for wiki-citation resolution (see `resolve_wiki_citation`
below) — everything else in this module is unchanged from the source.

Purpose: the PM named a concrete failure mode -- guard prose growing without
bound because doctrine alone never enforced a limit, and a static/lint check
over source text cannot see prose assembled at emit time from helper
templates. This module gives every Category-A hook one place to compose its
message so a runtime harness can measure it correctly, and gives every
future hook author the 280-char ceiling and the fenced-alternative exemption
"by construction" rather than by remembering a rule written in a wiki.

NEVER used by a Category-B (DR-118 pointer/relay) hook -- see
`docs/decisions/DR-118-doe-resident-transport-seam-is-a-pointer.md`. A
Category-B shim's prose is authored on the sibling doctrine plane and
relayed verbatim; routing it through this envelope would hand a
doctrine-plane-resident transport seam a message policy DR-118 forbids it
from holding. Category-A hooks own their own prose and are exactly what
this module is for.

Two responsibilities, deliberately kept in one small module rather than
split:

  1. A PURE composing callable (`compose`) -- separable from any
     stdin-reading `main()` and from exit-code plumbing.
  2. An IMPURE `emit` that actually writes a composed `Message` to one of
     the three real hook channels in use (Stop stderr+exit-2, PreToolUse
     `additionalContext`, PreToolUse `permissionDecisionReason`), UNLESS
     `COORDINATOR_HOOK_MESSAGE_MEASURE=1` is set, in which case it writes a
     structured `{"prose": ..., "alternative": ..., "anchor": ...}` record
     instead of the flattened channel text.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: The cap, defined once. Every future exception-manifest / gate-test module
#: imports this constant rather than redeclaring it.
#:
#: Kept at 280 CHARS, deliberately NOT converged with this engine's own
#: separate guard-message cap conventions elsewhere in `coordinator_core` —
#: 280 was derived against the Category-A hook population this cap governs;
#: a shared number with an unrelated corpus would be true of neither by
#: construction. Converging the two on sight is a number-matching move, not
#: a corpus-derived one.
CEILING = 280

#: Environment variable that switches `emit()` from writing a hook's real
#: channel output to writing a structured measurement record instead.
MEASURE_ENV_VAR = "COORDINATOR_HOOK_MESSAGE_MEASURE"

#: The three channel shapes in use across Category-A hooks. `emit()`
#: accepts exactly one of these.
CHANNEL_STOP = "stop"
CHANNEL_ADDITIONAL_CONTEXT = "additional_context"
CHANNEL_DENY = "deny"

_CHANNELS = frozenset({CHANNEL_STOP, CHANNEL_ADDITIONAL_CONTEXT, CHANNEL_DENY})

#: A bounded line count for the fenced alternative block. Not specified
#: numerically by the original spec; picked generous enough for a real
#: copy-pasteable command/diff (a few lines) while still ruling out a
#: converted hook smuggling paragraphs of prose into the exempt slot under
#: cover of a fence.
ALTERNATIVE_MAX_LINES = 10


@dataclass(frozen=True)
class Message:
    """The result of `compose()` -- what `emit()` writes to a real channel
    or a measurement record. `prose` is the only field the 280-char ceiling
    counts; `alternative` and `anchor` are structurally separate and EXEMPT
    from the count (see `validate_alternative_shape`)."""

    prose: str
    alternative: Optional[str] = None
    anchor: Optional[str] = None


# --------------------------------------------------------------------------
# Alternative-block shape validation -- the structural exemption.
# --------------------------------------------------------------------------

#: First non-blank-line "looks like a command or path" proxy. Deliberately
#: cheap, not a real shell parser -- see `_looks_like_command_or_path`'s own
#: docstring for what it does and does not catch. A token carrying a literal
#: `$` (a `${VAR}`/`$VAR` shell expansion) is checked against the WIDER
#: `_SHELL_VAR_TOKEN_RE` instead. A Windows drive-letter prefix -- a single
#: letter immediately followed by `:` and a path separator -- is admitted as
#: an optional leading segment, mirroring the identical narrow carve-out
#: `_PROSE_PUNCT_RE` already applies to the SAME shape.
_COMMAND_TOKEN_RE = re.compile(r"^(?:[A-Za-z]:[\\/])?[A-Za-z0-9_./\\-]+$")
_SHELL_VAR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./\\${}:=,@%+~-]+$")
_SENTENCE_END_RE = re.compile(r"[.!?]\s*$")
#: A comma/semicolon/em-dash, or a colon that is not part of a
#: drive-letter-style path prefix (a single letter immediately followed by
#: `:` and a path separator) -- punctuation shapes common in natural
#: -language prose and rare in a single command or path invocation.
_PROSE_PUNCT_RE = re.compile(r"[,;—]|(?<![A-Za-z]):(?![\\/])")
#: Curated content-word list -- two or more hits among the line's tokens is
#: treated as prose.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "that",
        "this",
        "instead",
        "name",
        "one",
        "dispatch",
        "applies",
        "reviewer",
        "or",
        "not",
        "amending",
        "for",
    }
)
#: Closed-class English grammar words -- ANY single hit among the line's
#: tokens is treated as prose, unlike `_STOPWORDS`'s >=2 threshold.
#: Deliberately excludes everyday CLI-subcommand-shaped verbs (`add`,
#: `remove`, `use`, `fix`, ...) -- those are NOT closed-class and appear in
#: genuine commands (`git add`).
_FUNCTION_WORDS = frozenset(
    {
        "the", "a", "an",
        "in", "on", "at", "by", "with", "from", "into", "onto", "of", "to",
        "as", "before", "after", "under", "over", "about", "between",
        "during", "without", "within", "than", "per", "via", "against",
        "and", "but", "nor", "yet", "because", "if", "unless", "while",
        "though", "although", "then",
        "it", "its", "these", "those", "which", "who", "whom", "whose",
        "was", "were", "be", "been", "being", "has", "have", "had",
        "do", "does", "did", "will", "would", "can", "could", "should",
        "must", "may", "might",
    }
)


def _first_non_blank_line(text: str) -> Optional[str]:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _looks_like_command_or_path(line: str) -> bool:
    """Cheap runnability proxy: does `line` look like a copy-pasteable
    command or path invocation rather than a prose sentence?

    Deliberately NOT a shell parser. It rejects a line that ends in
    sentence-terminating punctuation, carries prose-shaped punctuation
    (comma/semicolon/em-dash/non-drive-letter colon), whose first token is
    not a plausible executable/path shape, that carries two or more of the
    curated content-word `_STOPWORDS`, OR that carries even ONE closed-class
    `_FUNCTION_WORDS` grammar word among its tokens."""
    if not line:
        return False
    if _SENTENCE_END_RE.search(line):
        return False
    if _PROSE_PUNCT_RE.search(line):
        return False
    tokens = line.split()
    if not tokens:
        return False
    first_token = tokens[0].strip("\"'")
    if not first_token:
        return False
    token_shaped = bool(_COMMAND_TOKEN_RE.match(first_token))
    if not token_shaped and "$" in first_token:
        token_shaped = bool(_SHELL_VAR_TOKEN_RE.match(first_token))
    if not token_shaped:
        return False
    normalized_tokens = [t.strip(".,;:'\"").lower() for t in tokens]
    if any(t in _FUNCTION_WORDS for t in normalized_tokens):
        return False
    stopword_hits = sum(1 for t in normalized_tokens if t in _STOPWORDS)
    if stopword_hits >= 2:
        return False
    return True


def validate_alternative_shape(
    alternative: Optional[str], *, max_lines: int = ALTERNATIVE_MAX_LINES
) -> "tuple[bool, Optional[str]]":
    """The alternative-block structural validator, importable directly so a
    gate test can drive it with synthesized data rather than live disk
    state.

    Returns `(True, None)` when `alternative` is `None` (no block supplied
    -- always valid) or a valid runnable block. Returns `(False, reason)`
    otherwise. Enforces, in order:

      - non-empty text;
      - no embedded triple-backtick fence -- callers pass RAW block text,
        this module owns the fencing at render time (`render()`);
      - at most `max_lines` lines;
      - the first non-blank line parses as a command or path invocation
        (`_looks_like_command_or_path`).
    """
    if alternative is None:
        return True, None
    if not isinstance(alternative, str) or not alternative.strip():
        return False, "alternative must be non-empty text"
    if "```" in alternative:
        return False, "alternative must not itself contain a fenced block"
    lines = alternative.splitlines()
    if len(lines) > max_lines:
        return False, f"alternative exceeds the {max_lines}-line bound"
    first = _first_non_blank_line(alternative)
    if first is None:
        return False, "alternative has no non-blank line"
    if not _looks_like_command_or_path(first):
        return (
            False,
            "alternative's first non-blank line does not parse as a "
            "command or path invocation",
        )
    return True, None


# --------------------------------------------------------------------------
# The pure composer.
# --------------------------------------------------------------------------


def compose(
    prose: str, alternative: Optional[str] = None, anchor: Optional[str] = None
) -> Message:
    """Build a `Message` from a hook's diagnosis (`prose`, the ONLY field
    the 280-char ceiling counts), an optional fenced runnable `alternative`
    (structurally separate, exempt from the count -- see
    `validate_alternative_shape`), and an optional wiki `anchor` naming
    where the relocated explanation lives.

    Pure: no I/O, no environment read, no process interaction. Raises
    `ValueError` on a shape violation (empty prose, an invalid alternative
    block, or an empty-string anchor) rather than composing a malformed
    `Message`."""
    if not isinstance(prose, str) or not prose.strip():
        raise ValueError("message_envelope.compose: prose must be non-empty text")
    if alternative is not None:
        ok, reason = validate_alternative_shape(alternative)
        if not ok:
            raise ValueError(f"message_envelope.compose: invalid alternative block ({reason})")
    if anchor is not None and (not isinstance(anchor, str) or not anchor.strip()):
        raise ValueError(
            "message_envelope.compose: anchor must be non-empty text when provided"
        )
    return Message(prose=prose.strip(), alternative=alternative, anchor=anchor)


# --------------------------------------------------------------------------
# Wiki-citation resolution.
#
# ADAPTATION FROM THE PORTED SOURCE, not a straight port: DoE's
# `_message_envelope.resolve_wiki_citation` resolved a `docs/wiki/<page>.md`
# citation to an absolute path anchored at `_coordinator_dir()`, computed as
# `Path(__file__).resolve().parent.parent.parent` -- correct there because
# that module lived inside the doctrine-plane `coordinator/hooks/scripts/`
# tree, three levels under the doctrine root that actually holds
# `docs/wiki/`. This module now lives inside the ENGINE
# (`coordinator_core/hooks/support/`), whose own `__file__`-relative
# ancestor is this claude-klabauter checkout's root, not the doctrine-plane root that
# holds `docs/wiki/` -- porting the old computation verbatim would silently
# resolve every citation into the WRONG repo's tree.
#
# This repo's own already-landed hooks (e.g.
# `coordinator_core/hooks/nudge_harness_directive_dispatch.py`) already
# settle this: they emit a doctrine-plane wiki citation as a literal,
# unresolved `coordinator/docs/wiki/<page>.md` string rather than attempting
# runtime absolute-path resolution, consistent with
# `docs/reference/boundary-and-data-planes.md`'s planes split (doctrine
# content is coordinator-claude's, not claude-klabauter's, to resolve). This module
# follows that established convention: `resolve_wiki_citation` rewrites a
# citation to an absolute path ONLY when `CLAUDE_PLUGIN_ROOT` names a real
# doctrine root at call time (the one reliable, harness-supplied anchor for
# "where is the doctrine plane running from" -- unlike a `__file__`-relative
# guess, it cannot point at the wrong repo), and leaves the citation
# untouched otherwise -- never resolves into this engine's own tree.
# --------------------------------------------------------------------------

#: `docs/wiki/`, optionally `coordinator/`-prefixed -- the two forms
#: observed across the ported `_WIKI_ANCHOR` constants and the hand-rolled
#: "Reference:" citations already landed in `coordinator_core/hooks/`.
#:
#: The page part spans SUBDIRECTORIES, not just a flat page name. Each
#: interior segment must itself match the same conservative character class
#: and the final one must end `.md`, so a directory-only target
#: (`docs/wiki/`, `docs/wiki/coordinator-tripwires/`) still does not match
#: and is emitted verbatim.
_WIKI_CITATION_RE = re.compile(
    r"(?:coordinator/)?docs/wiki/((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.md)"
)


def _doctrine_root() -> Optional[Path]:
    """The doctrine-plane root this process is actually running under, or
    `None` when it cannot be determined. `CLAUDE_PLUGIN_ROOT` is the
    harness-supplied anchor for the coordinator-claude plugin tree a hook
    process runs from; unlike a `__file__`-relative computation (correct
    only when this module's own file lives inside that tree, which it no
    longer does), it names the right root regardless of where this engine
    checkout sits relative to the doctrine plane. Returns `None` when the
    env var is absent or does not resolve to a real directory -- callers
    treat that as "cannot resolve, leave the citation as-is", never as a
    reason to fall back to a guess."""
    raw = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not raw:
        return None
    try:
        root = Path(raw)
        return root if root.is_dir() else None
    except (OSError, ValueError):
        return None


def _render_resolved(path: "Path") -> str:
    """Render an already-resolved citation path for a reader.

    A resolved path is not automatically fit to send. On an installed
    layout the plugin lives under the operator's home, so the absolute form
    carries their account name into text whose audience is a dispatched
    subagent -- an identity token the repo-relative literal never held.

    Collapsed to `~/` when the path is under the home directory, native
    absolute otherwise -- a root outside home (a system-wide install) has no
    identity to hide and stays as it is.

    THE SEPARATOR IS LOAD-BEARING, NOT COSMETIC. The remainder is emitted
    POSIX-style even on Windows because `~\\...` does not expand for the
    reader, while `~/` does.

    Fails open to the absolute form: a home directory that cannot be
    determined is a rendering question, never a reason to drop a citation.
    """
    try:
        relative = path.relative_to(Path.home())
    except (ValueError, RuntimeError, OSError):
        return str(path)
    return f"~/{relative.as_posix()}" if relative.parts else "~"


def resolve_wiki_citation(text: str) -> str:
    """Rewrite every `docs/wiki/<page>.md` citation embedded in `text`
    (optionally `coordinator/`-prefixed, per `_WIKI_CITATION_RE`) into an
    absolute path anchored at `_doctrine_root()`, preserving whatever
    precedes and follows the matched substring (a `#slug` fragment, a
    ` § SECTION` locator, surrounding prose) untouched. When the doctrine
    root cannot be resolved (see `_doctrine_root`), or `text` carries no
    such citation, `text` is returned unchanged -- this module never
    resolves a citation into its OWN (engine) tree.

    Guards against double-mangling an already-resolved or foreign-anchored
    citation: a match is only substituted when it starts at the beginning
    of `text` or immediately follows whitespace/an opening delimiter."""

    root = _doctrine_root()
    if root is None:
        return text

    def _sub(match: "re.Match[str]") -> str:
        start = match.start()
        if start > 0 and text[start - 1] not in " \t\n(['\"`":
            return match.group(0)
        return _render_resolved(root / "docs" / "wiki" / match.group(1))

    return _WIKI_CITATION_RE.sub(_sub, text)


# --------------------------------------------------------------------------
# Rendering (pure*) and emission (impure) -- attaches to the existing hook
# seam, does not create a parallel one. (*`render()` reads the environment
# via `resolve_wiki_citation()` -- no filesystem/network/process I/O.)
# --------------------------------------------------------------------------


def render(message: Message) -> str:
    """Flatten `message` to the text a real (non-measurement) channel
    carries: the prose, then the alternative re-fenced in triple backticks
    (if present), then a trailing pointer at the wiki anchor (if present).
    The anchor is resolved via `resolve_wiki_citation()` (see above) so the
    emitted pointer resolves for the reader wherever the doctrine plane is
    running from, and is left as the literal citation text when it cannot
    be."""
    parts = [message.prose]
    if message.alternative:
        parts.append("")
        parts.append("```\n" + message.alternative.rstrip("\n") + "\n```")
    if message.anchor:
        parts.append("")
        parts.append(f"See {resolve_wiki_citation(message.anchor)}.")
    return "\n".join(parts)


def measurement_enabled() -> bool:
    return os.environ.get(MEASURE_ENV_VAR) == "1"


def _measurement_record(message: Message) -> str:
    return json.dumps(
        {"prose": message.prose, "alternative": message.alternative, "anchor": message.anchor},
        separators=(",", ":"),
    )


def _write_measurement_record(message: Message) -> None:
    """Write the structured measurement record for `message` to fd 3, or a
    documented fallback when fd 3 is not open (the common case outside a
    harness -- fd 3 is not a channel any process is guaranteed to inherit).

    Windows-safe: this never assumes POSIX fd semantics hold. The `os.write`
    call is wrapped so a closed/unavailable fd 3 (any `OSError`, including
    the Windows "bad file descriptor" shape) degrades to the fallback
    instead of crashing the hook. The fallback is stdout: under measurement
    mode this module never also writes the flattened channel output (see
    `emit`), so stdout is free for the harness to read the SAME structured
    line from instead."""
    line = _measurement_record(message)
    try:
        os.write(3, (line + "\n").encode("utf-8"))
        return
    except Exception:
        pass
    sys.stdout.write(line + "\n")


def emit(message: Message, channel: str) -> Optional[int]:
    """Impure: the one emission seam. Writes `message` to `channel` exactly
    as hooks do today -- UNLESS `COORDINATOR_HOOK_MESSAGE_MEASURE=1` is set,
    in which case it writes the structured measurement record instead of
    the flattened channel output, and returns `None` without touching the
    real channel at all.

    `channel` is one of `CHANNEL_STOP`, `CHANNEL_ADDITIONAL_CONTEXT`,
    `CHANNEL_DENY`:

      - `CHANNEL_STOP`: writes `render(message)` to stderr, returns `2`
        (the exit code a Stop-family hook's `main()` should `sys.exit()`
        with).
      - `CHANNEL_ADDITIONAL_CONTEXT`: writes a
        `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "additionalContext": <text>}}` envelope to stdout, returns `0`.
      - `CHANNEL_DENY`: writes a `{"hookSpecificOutput": {"hookEventName":
        "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": <text>}}` envelope to stdout, returns
        `0`.

    Raises `ValueError` for an unrecognised `channel`."""
    if channel not in _CHANNELS:
        raise ValueError(f"message_envelope.emit: unknown channel {channel!r}")

    if measurement_enabled():
        _write_measurement_record(message)
        return None

    text = render(message)

    if channel == CHANNEL_STOP:
        # .buffer.write bypasses Python's Windows text-mode newline
        # translation (stderr in text mode would silently turn every LF
        # into CRLF, breaking byte-fidelity with siblings that write the
        # same way).
        sys.stderr.buffer.write(text.encode("utf-8"))
        return 2

    if channel == CHANNEL_ADDITIONAL_CONTEXT:
        envelope = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": text,
            }
        }
        sys.stdout.write(json.dumps(envelope, separators=(",", ":")))
        return 0

    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": text,
        }
    }
    sys.stdout.write(json.dumps(envelope, separators=(",", ":")))
    return 0
