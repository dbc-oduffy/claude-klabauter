"""coordinator_core.bash_guards.guard_offer_invoke_params_stdin --
``check_offer_invoke_params_stdin``: rewrite a ``coordinator_core.invoke``
call that carries its params payload as an inline shell-quoted argv token
into the quoting-immune ``--params-file -`` heredoc form.

The shape this closes, observed live 2026-07-29 on a real
``ceremony.scoped_git_commit`` call: an op payload embedding free text (a
commit message, a memo body) is handed to the shell as a single-quoted argv
token, and the text contains an apostrophe --

    ... invoke ceremony.scoped_git_commit '{"message": "C1's half (build)"}'

Bash ends the quoted span AT the apostrophe. Everything after it is
unquoted shell text, so the ``(`` two words later is a syntax error and the
command dies in ``eval`` before the interpreter it targets is ever spawned
(``syntax error near unexpected token '('``). With no bracket in the tail
it is worse than a syntax error: the payload silently re-tokenizes and the
op runs with a mangled message.

Neither failure is diagnosable from the op's side -- the process never
receives the payload -- and neither is preventable by the caller being more
careful, which is why this is a guard and not a doctrine line. The fix is a
transport whose correctness does not depend on the payload's own bytes: a
quoted heredoc into ``--params-file -`` performs no interpolation and has
no quote sensitivity, so no payload byte can change how the shell parses
the command (also ARG_MAX-immune, and needs no temp file to clean up).

Rung-A safety (why this auto-rewrites rather than only offering): the
rewrite is applied ONLY when the extracted payload span parses as a JSON
object AND that span cross-checks as a single shell token (see
``_span_is_single_shell_token``). The parse alone is NOT the proof -- a
concrete counterexample exists where two distinct single-quoted argv tokens,
separated by other command text, get merged by the ``'{`` / ``}'`` span
bracketing into one document that still parses as JSON while being neither
of the two actual tokens. The shlex cross-check closes that gap for the case
where it can run: where ``cmd`` is cleanly shell-tokenizable, "the extracted
span equals exactly one token" IS the proof the parse alone cannot supply.
A span that FAILS that check is denied rather than rewritten or ignored --
the shell's own reading disagrees with the payload as written, so the op
would receive something other than what was typed. That covers the merge
counterexample and, just as importantly, the even-apostrophe case:
``'{"m":"isn't"}'`` tokenizes to the single token ``{"m":"isnt"}`` -- valid
JSON, both apostrophes silently gone -- which is precisely the quiet
corruption this guard exists to stop, and which silence would have let run.

Where ``cmd`` is NOT shell-tokenizable (``shlex.split`` raises -- the
payload carries an odd number of quote characters, which is the guard's own
primary target shape, e.g. an apostrophe inside a message field), no
cross-check is available and the JSON parse remains the only evidence. That
is a known, accepted residual gap: the span is the strongest available
reading of a command that is already broken shell syntax, not a proof.

That latitude is granted to THAT shape only, and not to the other reason no
tokens exist. A command past the shared tokenizer ceiling
(``_command_tokenizer.exceeds_tokenizable_ceiling`` -- the DoS bound, since
``shlex`` is quadratic in the longest token) is denied, not rewritten: the
guard cannot claim to understand a command it declined to read, and a
payload that large is already 8x past ``_ARGV_PAYLOAD_HAZARD_BYTES``.
Collapsing the two causes onto one ``None`` is what previously let padding
buy an ALLOW here, so the cross-check reports four NAMED outcomes and the
caller branches on every one of them (see ``_CROSS_CHECK_CONFIRMED`` and
its siblings).

Where neither check succeeds the guard stays silent rather than guessing; where
the rewrite cannot be placed (multi-line command -- a heredoc body must
follow the line carrying its ``<<`` operator) it denies with the shape
spelled out, because the alternative is letting a command run that is
already known to corrupt its own payload.

``host_is_windows`` is deliberately not threaded to this guard (unlike the
three ``PLATFORM_CONDITIONED_DENY`` guards): the rewrite target is a
``<<'DELIM'`` heredoc, which is bash-native syntax with no platform branch --
identical on Linux/macOS bash and on Windows git-bash/msys bash, the same
interpreter the Bash tool execs through everywhere. Relatedly,
``_ARGV_PAYLOAD_HAZARD_BYTES`` is one conservative threshold applied on
every platform BY CHOICE, not a Windows number applied without considering
whether it should be platform-gated: it is calibrated to the tighter
Windows/msys ARG_MAX ceiling and left as-is on macOS/Linux too, because a
single conservative ceiling everywhere is simpler than platform-branching a
threshold whose only cost is an occasional unnecessary (but harmless)
rewrite.

Spec backlink: coordinator_core/invoke/__main__.py --params-file "-" branch
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Dict, Optional, Tuple

from coordinator_core.bash_guards.dispatch_checks import (
    _allow_rewrite,
    _crlf_strip,
    _deny,
    _override,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._command_tokenizer import (
    exceeds_tokenizable_ceiling,
)

#: Both spellings of the entrypoint: `-m coordinator_core.invoke` (the
#: module form every caller uses) and a path form, should one appear.
_INVOKE_RE = re.compile(r"coordinator_core[./]invoke(?:\b|$)")

#: M17 (2026-07-30): named once so the three deny/allow messages below route
#: through `operator_override_note` (the SSOT builder) instead of each
#: hand-writing "To bypass: export COORDINATOR_ALLOW_INVOKE_ARGV_PARAMS=1." --
#: that clause named an action unreachable from inside a live session.
_OVERRIDE_ENV_VAR = "COORDINATOR_ALLOW_INVOKE_ARGV_PARAMS"

#: Payload size past which an argv token is a portability hazard on its own,
#: apostrophe or not -- Windows/msys ARG_MAX is ~32KB and the failure there
#: is a truncation, not an error. Well under that, because the payload is
#: only part of the command line.
_ARGV_PAYLOAD_HAZARD_BYTES = 8000

_HEREDOC_DELIM = "CCJSON"


def _extract_inline_payload(cmd: str) -> Optional[Tuple[int, int, str, str]]:
    """Locate a shell-quoted JSON-object argv token in ``cmd``.

    Returns ``(open_quote_idx, close_quote_idx, quote_char, payload_text)``,
    or ``None`` when no span parses as a JSON object.

    Deliberately does NOT tokenize here: the whole point of this guard is a
    command the shell itself may not be able to tokenize, so a tokenizer
    embedded in this function would bail on exactly the inputs that matter.
    The span is bounded by the literal ``<quote>{`` opener and the LAST
    ``}<quote>`` closer. The JSON parse below is NECESSARY but not
    SUFFICIENT proof the span is right -- a merged two-token span can still
    parse as valid JSON (see the module docstring's rung-A argument). The
    caller (``check_offer_invoke_params_stdin``) runs a separate
    ``_span_is_single_shell_token`` cross-check against this function's
    result where ``cmd`` is shell-tokenizable at all.

    Only the single-quoted form is recognized. That is not an oversight: a
    JSON object's own key quotes have to be backslash-escaped to survive a
    double-quoted shell span, so the raw span for a double-quoted payload
    never parses as JSON and the rung-A proof is unavailable by
    construction. Silence is the correct verdict there -- an unescaping
    heuristic would be exactly the guess this function refuses to make.
    """
    quote = "'"
    open_idx = cmd.find(quote + "{")
    if open_idx < 0:
        return None
    brace_idx = cmd.rfind("}" + quote)
    if brace_idx <= open_idx:
        return None
    payload = cmd[open_idx + 1:brace_idx + 1]
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return (open_idx, brace_idx + 1, quote, payload)


#: The four outcomes of `_span_is_single_shell_token`, named rather than
#: encoded as `Optional[bool]` (M-16, 2026-08-05). The old tri-state put the
#: two "no verdict from the cross-check" outcomes on `None`, which the sole
#: caller distinguished from `True` only by NOT mentioning it -- an implicit
#: fall-through into `_allow_rewrite`. Four named states force every caller
#: branch to be written down, and separate the two causes that must resolve
#: in OPPOSITE directions (see `CONTRACT` in each constant's comment).
#:
#: Cross-check confirms the span IS exactly one shell token -> safe to rewrite.
_CROSS_CHECK_CONFIRMED = "confirmed"
#: Cross-check RAN and disagreed: `cmd` tokenizes cleanly, but no single token
#: equals `payload`. The span swallowed shell text between two real tokens, or
#: an even number of apostrophes vanished during tokenization. CONTRACT: DENY.
_CROSS_CHECK_CONTRADICTED = "contradicted"
#: `cmd` is not shell-tokenizable at all (`shlex.split` raises -- an odd number
#: of quote characters). This is the guard's OWN PRIMARY TARGET SHAPE, not an
#: anomaly: the apostrophe-in-a-message payload that motivated the guard lands
#: here every time. CONTRACT: proceed to the rewrite on the JSON parse alone --
#: a known, accepted residual gap, argued in the module docstring. Denying here
#: would deny the exact commands the guard exists to repair.
_CROSS_CHECK_UNAVAILABLE = "unavailable"
#: `cmd` is past the shared tokenizer ceiling, so `shlex` was never run (it is
#: quadratic in the longest token; see `_command_tokenizer.
#: _MAX_TOKENIZABLE_COMMAND_CHARS`). CONTRACT: DENY -- unlike
#: `_CROSS_CHECK_UNAVAILABLE`, the command is not a shape this guard can claim
#: to understand, and a 64 KB+ inline params payload is already past
#: `_ARGV_PAYLOAD_HAZARD_BYTES` by 8x. Refusing the parse must not buy an
#: ALLOW from the guard the padding defeats.
_CROSS_CHECK_TOO_LARGE = "too-large"


def _span_is_single_shell_token(cmd: str, payload: str) -> str:
    """Cross-check ``payload`` (the DEQUOTED payload text -- what
    ``_extract_inline_payload`` returns, with the enclosing single-quotes
    already stripped) against ``cmd``'s own shell tokenization.

    ``shlex.split`` returns each single-quoted argv word with its quotes
    removed and no escape processing inside the quotes, so a legitimate
    single-quoted JSON token dequotes to exactly ``payload`` -- comparing
    against the quoted span (with its quotes still attached) would never
    match and would make this check useless.

    Returns one of the four ``_CROSS_CHECK_*`` constants above; each carries
    its own caller contract and none of them may be treated as a weaker or
    stronger form of another. In particular ``_CROSS_CHECK_CONTRADICTED``
    (a cross-check RAN and the span does not match reality) and
    ``_CROSS_CHECK_UNAVAILABLE`` (no cross-check could run, the parse is the
    last word) are NOT the same claim, and ``_CROSS_CHECK_TOO_LARGE``
    resolves like the former rather than the latter even though it, too,
    produced no tokens.

    NEGATIVE SPEC: this function never decides the verdict. It reports what
    the shell's own reading of ``cmd`` supports, and nothing else.
    """
    if exceeds_tokenizable_ceiling(cmd):
        return _CROSS_CHECK_TOO_LARGE
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return _CROSS_CHECK_UNAVAILABLE
    return _CROSS_CHECK_CONFIRMED if payload in tokens else _CROSS_CHECK_CONTRADICTED


def _payload_hazard(quote: str, payload: str) -> Optional[str]:
    """Name the reason this payload cannot survive argv transport, or
    ``None`` if it can. A payload that IS shell-safe is left alone -- the
    argv form stays a perfectly good transport for small machine-generated
    params, and rewriting those would be noise."""
    if len(payload) > _ARGV_PAYLOAD_HAZARD_BYTES:
        return (
            "the payload is %d bytes, near the ~32KB argv ceiling on "
            "Windows/msys where the overflow truncates rather than errors"
            % len(payload)
        )
    if "'" in payload:
        return "an apostrophe ends the quoted span -- the rest becomes unquoted shell text"
    return None


def check_offer_invoke_params_stdin(
    cmd: str,
    session_id: str = "",
    hook_payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not cmd:
        return None
    original_cmd = _crlf_strip(cmd)
    if not _INVOKE_RE.search(original_cmd):
        return None
    if "--params-file" in original_cmd:
        return None
    if _override(_OVERRIDE_ENV_VAR):
        return None

    found = _extract_inline_payload(original_cmd)
    if found is None:
        return None
    open_idx, close_idx, quote, payload = found

    hazard = _payload_hazard(quote, payload)
    if hazard is None:
        return None

    # This loop is provably unreachable today, not merely well-defended:
    # `_extract_inline_payload` requires `json.loads(payload)` to succeed
    # (strict mode) before any rewrite happens, and strict `json.loads`
    # rejects a raw (unescaped) newline inside a JSON string -- so `payload`
    # can never contain an actual `\n` byte, the heredoc body is always
    # exactly one line, and the delimiter can never appear at line-start in
    # the transported bytes. The loop costs nothing and is kept anyway
    # because it covers a hypothetical future transport (pretty-printed
    # params, say) that no longer holds that property.
    delim = _HEREDOC_DELIM
    n = 0
    while re.search(r"(?m)^%s$" % re.escape(delim), payload):
        n += 1
        delim = "%s_%d" % (_HEREDOC_DELIM, n)

    offer_shape = (
        "  python3 -m coordinator_core.invoke <op> --params-file - "
        "--repo <root> <<'%s'\n  <json payload, verbatim>\n  %s" % (delim, delim)
    )

    _offer_note = operator_override_note(_OVERRIDE_ENV_VAR, payload=hook_payload, git_root=git_root)
    _offer_note_trailer = ("\n\n" + _offer_note) if _offer_note else ""

    # Cross-check the extracted span against the command's own shell
    # tokenization, where that is possible. Every one of the four outcomes is
    # branched on BY NAME below -- an outcome that resolves by falling out of
    # this block is a bug (it is the bug this shape replaced: `None` used to
    # reach `_allow_rewrite` by not being mentioned).
    cross_check = _span_is_single_shell_token(original_cmd, payload)

    # CONTRADICTED: a cross-check RAN and disagreed -- the command tokenizes
    # cleanly, but no single token equals the payload as written. Two shapes
    # produce that, and both are denied rather than rewritten or ignored. The
    # merge counterexample -- two distinct quoted tokens whose span brackets
    # into one still-parseable document -- is not safe to rewrite, because the
    # span is not the payload. And an EVEN number of apostrophes is not safe
    # to ignore: `'{"m":"isn't"}'` tokenizes to the single token
    # `{"m":"isnt"}`, valid JSON with both apostrophes silently gone, which is
    # the quiet corruption this guard exists to stop.
    if cross_check == _CROSS_CHECK_CONTRADICTED:
        return _deny(
            (
                "Denied: %s. Tokens merged or vanished -- the op would not "
                "get what you typed.\n\n"
                "Use instead:\n%s"
                % (hazard, offer_shape)
            )
            + _offer_note_trailer
        )

    # TOO_LARGE: the command is past the shared tokenizer ceiling, so no
    # cross-check could be run without re-opening the quadratic hang. Denying
    # is the fail-closed direction and the only self-consistent one: a payload
    # that large is already many times `_ARGV_PAYLOAD_HAZARD_BYTES`, so
    # padding a command past the ceiling must not buy a silent rewrite (or,
    # since a rewrite short-circuits the guard chain, skip the bands behind
    # it). Cannot fire below the ceiling by construction.
    if cross_check == _CROSS_CHECK_TOO_LARGE:
        return _deny(
            (
                "Denied: %s, and the command is too large to shell-tokenize, "
                "so its payload boundaries cannot be verified.\n\n"
                "Use instead:\n%s"
                % (hazard, offer_shape)
            )
            + _offer_note_trailer
        )

    # UNAVAILABLE: `cmd` is not shell-tokenizable at all (an odd number of
    # quote characters -- the guard's PRIMARY TARGET SHAPE), so there is
    # nothing to cross-check and the JSON parse remains the only evidence.
    # Deliberately falls through to the rewrite, exactly as CONFIRMED does:
    # this is the live 2026-07-29 apostrophe shape, and denying it would deny
    # ordinary work the guard exists to repair. Stated here rather than
    # implied by omission.
    #
    # Anything else is unreachable today and DENIES rather than falling
    # through: a future fifth outcome must not inherit the rewrite by being
    # unmentioned, which is precisely how the old `None` became a fail-open.
    if cross_check not in (_CROSS_CHECK_CONFIRMED, _CROSS_CHECK_UNAVAILABLE):
        return _deny(
            (
                "Denied: %s, and the payload's boundaries could not be "
                "verified.\n\nUse instead:\n%s" % (hazard, offer_shape)
            )
            + _offer_note_trailer
        )

    if "\n" in original_cmd:
        # A heredoc body must follow the line carrying its `<<` operator, so
        # the operator can only be spliced into a single-line command. Not
        # worth parsing multi-line command structure to place it: name the
        # shape and let the caller place it.
        return _deny(
            (
                "Denied: %s. Not rewritten -- command spans multiple "
                "lines; a heredoc body must follow its own '<<' line.\n\n"
                "Use instead:\n%s"
                % (hazard, offer_shape)
            )
            + _offer_note_trailer
        )

    rewritten = "%s--params-file - <<'%s'%s\n%s\n%s" % (
        original_cmd[:open_idx],
        delim,
        original_cmd[close_idx + 1:],
        payload,
        delim,
    )
    return _allow_rewrite(
        rewritten,
        (
            "Auto-rewritten to '--params-file -' heredoc: %s."
            % hazard
        )
        + _offer_note_trailer,
    )
