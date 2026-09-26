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
from typing import Mapping, Optional

CEILING = 280

MEASURE_ENV_VAR = "COORDINATOR_HOOK_MESSAGE_MEASURE"

CHANNEL_STOP = "stop"
CHANNEL_ADDITIONAL_CONTEXT = "additional_context"
CHANNEL_DENY = "deny"

_CHANNELS = frozenset({CHANNEL_STOP, CHANNEL_ADDITIONAL_CONTEXT, CHANNEL_DENY})

ALTERNATIVE_MAX_LINES = 10


@dataclass(frozen=True)
class Message:

    prose: str
    alternative: Optional[str] = None
    anchor: Optional[str] = None


#: `_SHELL_VAR_TOKEN_RE` instead. A Windows drive-letter prefix -- a single
#: `_PROSE_PUNCT_RE` already applies to the SAME shape.
_COMMAND_TOKEN_RE = re.compile(r"^(?:[A-Za-z]:[\\/])?[A-Za-z0-9_./\\-]+$")
_SHELL_VAR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./\\${}:=,@%+~-]+$")
_SENTENCE_END_RE = re.compile(r"[.!?]\s*$")
_PROSE_PUNCT_RE = re.compile(r"[,;—]|(?<![A-Za-z]):(?![\\/])")
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
#: tokens is treated as prose, unlike `_STOPWORDS`'s >=2 threshold.
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


def compose(
    prose: str, alternative: Optional[str] = None, anchor: Optional[str] = None
) -> Message:
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


# ADAPTATION FROM THE PORTED SOURCE, not a straight port: DoE's
# citation to an absolute path ONLY when `CLAUDE_PLUGIN_ROOT` names a real

#: observed across the ported `_WIKI_ANCHOR` constants and the hand-rolled
#: The page part spans SUBDIRECTORIES, not just a flat page name. Each
_WIKI_CITATION_RE = re.compile(
    r"(?:coordinator/)?docs/wiki/((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.md)"
)


def _doctrine_root(env: "Optional[Mapping[str, str]]" = None) -> Optional[Path]:
    """The doctrine-plane root this process is actually running under, or
    `None` when it cannot be determined. `CLAUDE_PLUGIN_ROOT` is the
    harness-supplied anchor for the coordinator-claude plugin tree a hook
    process runs from; unlike a `__file__`-relative computation (correct
    only when this module's own file lives inside that tree, which it no
    longer does), it names the right root regardless of where this engine
    checkout sits relative to the doctrine plane. Returns `None` when the
    var is absent or does not resolve to a real directory -- callers treat
    that as "cannot resolve, leave the citation as-is", never as a reason
    to fall back to a guess.

    `env` is the session-scoped `params["env"]` mapping a PreToolUse op
    receives (see `nudge_multiwave_workflow.py`'s established convention).
    This resident engine serves ~50 concurrent sessions; its own process
    `os.environ` belongs to none of them, so `env` is read exclusively when
    the caller supplies one. `env=None` (a caller that cannot thread a
    session env through, e.g. a Stop-channel path with no `params`) falls
    back to `os.environ` for backward compatibility -- not a silent
    ambient read reintroduced by default, but the documented degradation
    for callers with no session payload to read from."""
    raw = (env or {}).get("CLAUDE_PLUGIN_ROOT") if env is not None else os.environ.get(
        "CLAUDE_PLUGIN_ROOT"
    )
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


def resolve_wiki_citation(
    text: str, *, env: "Optional[Mapping[str, str]]" = None
) -> str:
    """Rewrite every `docs/wiki/<page>.md` citation embedded in `text`
    (optionally `coordinator/`-prefixed, per `_WIKI_CITATION_RE`) into an
    absolute path anchored at `_doctrine_root(env)`, preserving whatever
    precedes and follows the matched substring (a `#slug` fragment, a
    ` § SECTION` locator, surrounding prose) untouched. When the doctrine
    root cannot be resolved (see `_doctrine_root`), or `text` carries no
    such citation, `text` is returned unchanged -- this module never
    resolves a citation into its OWN (engine) tree.

    `env` is forwarded to `_doctrine_root` -- the session-scoped
    `params["env"]` mapping, never this process's own `os.environ`, per
    the established convention (see `_doctrine_root`'s docstring).

    Guards against double-mangling an already-resolved or foreign-anchored
    citation: a match is only substituted when it starts at the beginning
    of `text` or immediately follows whitespace/an opening delimiter."""

    root = _doctrine_root(env)
    if root is None:
        return text

    def _sub(match: "re.Match[str]") -> str:
        start = match.start()
        if start > 0 and text[start - 1] not in " \t\n(['\"`":
            return match.group(0)
        return _render_resolved(root / "docs" / "wiki" / match.group(1))

    return _WIKI_CITATION_RE.sub(_sub, text)


def render(message: Message, *, env: "Optional[Mapping[str, str]]" = None) -> str:
    parts = [message.prose]
    if message.alternative:
        parts.append("")
        parts.append("```\n" + message.alternative.rstrip("\n") + "\n```")
    if message.anchor:
        parts.append("")
        parts.append(f"See {resolve_wiki_citation(message.anchor, env=env)}.")
    return "\n".join(parts)


def measurement_enabled() -> bool:
    return os.environ.get(MEASURE_ENV_VAR) == "1"


def _measurement_record(message: Message) -> str:
    return json.dumps(
        {"prose": message.prose, "alternative": message.alternative, "anchor": message.anchor},
        separators=(",", ":"),
    )


def _write_measurement_record(message: Message) -> None:
    sys.stdout.write(_measurement_record(message) + "\n")


def emit(
    message: Message, channel: str, *, env: "Optional[Mapping[str, str]]" = None
) -> Optional[int]:
    """Impure: the one emission seam. Writes `message` to `channel` exactly
    as hooks do today -- UNLESS `COORDINATOR_HOOK_MESSAGE_MEASURE=1` is set,
    in which case it writes the structured measurement record instead of
    the flattened channel output, and returns `None` without touching the
    real channel at all.

    `env` is the session-scoped `params["env"]` mapping, forwarded to
    `render()` for wiki-citation resolution (see `_doctrine_root`'s
    docstring) -- never this process's own `os.environ`.

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

    text = render(message, env=env)

    if channel == CHANNEL_STOP:
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
