"""coordinator_core.bash_guards.guard_grep_via_bash -- PreToolUse(Bash)
advisory-only guard for the GREP_VIA_BASH shape (BX-6,
docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md, the
50.9%-of-forks shape -- the single largest measured fork shape in the
1,389-transcript / 62,487-call fork-tax census).

NARROWED 2026-07-30 (worklist row H11, docs/plans/2026-07-30-os-aware-
guard-advisory-defaults.md) -- this guard used to also deny (Windows) /
advise (macOS) the "substitutable residue" of this shape via
``_platform_verdict.platform_verdict_for_shape``. That branch is gone:
measured against the full corpus it fired on the same 234-command set
`dispatch_checks.check_grep_via_bash_rewrite` (registered EARLIER in
`dispatch.py`'s guard chain, and sharing this module's own classification/
parsing helpers) already claims as an ``ADVISORY_REWRITE`` -- 0 denies on
either platform, provably unreachable in production. What remains is
advisory-only, and only for a narrow slice of composed traffic: a real
partial rewrite (see "PARTIAL PROMOTION" below) or a genuine GNU-only grep
construct (see ``_has_gnu_only_construct``). Everything else this guard
used to advise on (99.67% of its prior firing set, per H11(c)'s own
measurement) is now silence -- design-as-offers (global `~/.claude/
CLAUDE.md`): where no actionable alternative exists, the honest output is
silence, not a ~2.2KB advisory naming none.

THE REWRITE TARGET THIS ROW DENIES TOWARD
------------------------------------------
The deny names ``dispatch_checks.check_grep_via_bash_rewrite`` (BX-16 shape
2) -- a REAL, already-landed auto-rewrite that turns a substitutable
grep-family invocation into a single ``python3 -c`` ``os.walk``/``re``
one-liner. This module does not re-derive that translation; it imports the
SAME parsing/rendering functions the rewrite check itself uses
(``_bt_grep_flags_and_operands`` / ``_bt_grep_python_rewrite`` /
``_GREP_FAMILY_BINARIES_BT``), so "is this command substitutable" and "what
does the rewrite look like" can never drift out of sync with what the
rewrite seam actually emits -- the reachability half of BX-6's own
requirement (deny only toward a target BX-16 can ACTUALLY EMIT) is
structurally guaranteed by sharing the implementation, not re-asserted by a
second hand-written copy that could silently diverge from it.

Explicitly NOT a rewrite target: the ``Grep`` tool (confirmed unreachable
at every layer under this project's control on build 2.1.220 --
permissions, launcher, agent frontmatter with ``Tools: *``, and tool
deferral were all eliminated as live options) and any claude-klabauter-owned op
(out of scope for this plan; the coordinator-owned op transport and its
outlets are deferred to Plan B). The ONLY thing this guard's deny message
ever points at is the Bash-to-Bash ``python3 -c`` rewrite BX-16 already
ships.

THE SUBSTITUTABLE/COMPOSED LINE (BX-6's own decision, not left to
classifier accident)
-------------------------------------------------------------------
A command matching the ``GREP_VIA_BASH`` shape (``_shape_classifier.
classify_command``) is SUBSTITUTABLE RESIDUE -- and only then eligible for
a Windows deny -- iff its entire observable output is reproducible by the
single Bash-to-Bash rewrite above with NO downstream shell processing:

  1. The command is exactly ONE ``;``/``&``/``|``-delimited segment (see
     ``_command_tokenizer.segments_from_tokens_with_pipe_flag``) -- nothing
     is chained after it with ``&&``/``;``, and nothing feeds INTO it via a
     pipe (``pipe_before`` false for that one segment). This alone rules out
     both "grep is not the last stage" (something pipes ITS stdout onward --
     impossible for a lone segment, since there is nothing after it to pipe
     to) and "a captured shell variable used later" / "``;``-joined
     non-grep work" (either shape is >= 2 segments by construction).
  2. The segment's first token is a grep-family binary
     (``_GREP_FAMILY_BINARIES_BT`` -- ``grep``/``egrep``/``fgrep``/``rg``).
  3. Every flag on the invocation is one of the rewrite's own recognized
     SHORT, substitutable flags (``_GREP_SUBSTITUTABLE_SHORT_FLAGS`` --
     recursive/case-insensitive/count/filenames-only/line-numbered/word-
     boundary), via ``_bt_grep_flags_and_operands``. A long option
     (``--foo``) or an unrecognized short flag (context lines, extended
     regex, invert-match, etc.) makes the invocation non-substitutable --
     BX-16 does not attempt to translate it, so this guard must not claim a
     rewrite exists for it either; that residue is advised, not denied,
     exactly like a genuinely composed pipeline (see below).

Anything that fails ANY of the three tests above is COMPOSED: advised on
every platform, never denied anywhere, because BX-16 offers no
Bash-to-Bash equivalent this guard could point a Windows deny at, and
denying toward a target that does not exist is precisely the deny-before-
rewrite hazard this whole plan sequences against.

THE EDGE CASE, DECIDED EXPLICITLY (not left to accident)
---------------------------------------------------------
``grep -l X | xargs grep -l Y`` -- a two-stage AND-filter -- is ruled
COMPOSED, not substitutable residue. Reasoning: this command is TWO
segments (a ``|`` separates ``grep -l X`` from ``xargs grep -l Y``), which
already fails test 1 above on its own terms -- the first grep's stdout
feeds ``xargs``, which itself re-invokes a second grep with an argument
list built from the first's output; no single stage stands alone with
nothing downstream of it. It is also, independently, unreachable: BX-16's
own rewrite (``check_grep_via_bash_rewrite``) explicitly requires exactly
one segment and returns no rewrite for anything else, so even setting the
substitutability rule aside, there is no BX-16-emitted target this guard
could deny toward for the two-stage shape -- denying it anyway would be
exactly the "deny before its rewrite exists" hazard this row's own sequencing
constraint forbids. A correct rewrite for the two-stage case is not
impossible in principle (a single python3 script could walk once and apply
both filters), it is simply not what BX-16 shipped, and this guard consumes
BX-16's rewrite rather than inventing a second, independent one.

OPEN TRADEOFF -- CARRIED FORWARD, NOT RESOLVED HERE (the Staff Engineer's review
finding, restated per this row's own instruction not to silently resolve
it): the fleet's own fork-tax figures -- 39.3% of commands carry a grep-via-
Bash shape, 50.9% of FORKS come from it, ~4.2 forks per grep-bearing
command against a measured p99 of 12 -- suggest per-command fork MASS may
concentrate in exactly the composed bucket this module never denies
(a grep chained with other work, by definition, produces more forks per
command than a lone substitutable ``grep -rn foo .``). If that holds, this
guard denies the CHEAP single-fork case and waves the EXPENSIVE
multi-fork case through with only an advisory -- a real engineering
tradeoff on evidence not yet split substitutable-vs-composed by bucket. Per
plan BX-6, resolving this is NOT a blocker for this row; it should be
checked against BX-1-class fork data (deferred to Plan B) before leaning on
this guard's Windows-deny half as a complete fix for the 50.9% shape.

PARTIAL PROMOTION OF THE COMPOSED BUCKET (worklist Row G2, 2026-07-30)
-----------------------------------------------------------------------
This tradeoff is narrowed, not resolved, by ``_partial_pipe_rewrite``: for
the narrow sub-case of exactly two `|`-joined segments where the first is
a substitutable grep-family invocation, the composed advisory now offers a
REAL alternative (rewrite the grep half only, pipe its stdout verbatim
into the untouched downstream segment) instead of prose alone. It removes
one fork, not the whole chain, and it is offered only as an advisory on
every platform -- never a deny -- because its stdout has not been
differentially validated against real grep for every flag/downstream
combination (see that function's own "KNOWN LIMIT" paragraph). A 3+-segment
pipeline or a `;`/`&&` chain still gets prose only; no mechanism exists for
those shapes.

Override
--------
``COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD=1`` bypasses this guard entirely
(read inline, at call time, per this package's ``_override`` convention --
never hoisted to module scope) -- named distinctly from BX-16's own
``COORDINATOR_ALLOW_GREP_VIA_BASH`` (the rewrite check's escape hatch,
which suppresses the AUTO-REWRITE, not this guard's own advisory) so an
operator can disable either seam independently without ambiguity about
which one they turned off. H11(b) (2026-07-30) briefly opted this guard's
composed advisory out of the shared ``operator_override_note`` call
entirely, on the reasoning that a hand-written paragraph could be dropped
without a substitute -- that left the env var functioning but
undiscoverable from the message text, unlike every other advisory-emitting
guard in this package. Restored (M17, 2026-07-30) on a code-review finding, as the
same one-line ``operator_override_note(_OVERRIDE_ENV_VAR)`` pointer every
sibling guard now appends post the second override-note cut (see that
function's own docstring) -- no reintroduction of the old ~1.1KB inline
boilerplate, just the SSOT pointer.

GNU-ONLY PORTABILITY CHECK (worklist row H11(c), 2026-07-30; DENYLIST
correction C2, 2026-08-01)
--------------------------------------------------------------
The composed-advisory path used to fire on every chained/piped/
untranslatable-flag GREP_VIA_BASH match and frame the fork-count/
portability tradeoff in prose, regardless of whether portability was
actually at stake. Measured against this guard's own firing set: only 65
of 19,434 commands (0.33%) carry a construct that actually behaves
differently on BSD grep (macOS) than GNU grep (Linux) -- `--include`/
`--exclude` (8.36%), `-o` (2.55%), and `-r` (1.07%) all exist on BSD grep
too (semantic divergence, not breakage), and were never a genuine
portability hazard. ``_has_gnu_only_construct`` is that narrow, targeted
check; `check()` now only calls `_composed_advisory` when it finds one of
these OR a real partial rewrite (see "PARTIAL PROMOTION" above) --
everything else is silence, per design-as-offers.

C2 (2026-08-01) corrected the check's SHAPE, not just its threshold: the
original cut classified GNU-only via an ALLOWLIST of portable long options
(`_PORTABLE_LONG_OPTS = {"--include", "--exclude"}`), treating every OTHER
long option as GNU-only by default -- an allowlist makes every unexamined
flag a false positive, and `--exclude-dir` and `--version` (both
BSD-supported) fired under it purely for not being on a two-entry list.
Replaced with an explicit DENYLIST (`_GNU_ONLY_SHORT_FLAGS` /
`_GNU_ONLY_LONG_OPTS`, both in this module) enumerating the genuinely-
divergent constructs, sourced to macOS 26.5's `man grep` (BSD grep mirrors
nearly all GNU long options per its own STANDARDS section, so the
divergent set is small) -- see those constants' own comments for the
derivation date/platform and per-entry rationale.

Negative-spec -- what this module deliberately does NOT do:
  - Does NOT re-implement shape detection. ``_shape_classifier.
    classify_command`` is the sole classifier; this module only consumes
    ``has_shape(Shape.GREP_VIA_BASH)`` and the shared tokenizer's segment
    split.
  - Does NOT re-implement the rewrite. ``_bt_grep_flags_and_operands`` /
    ``_bt_grep_python_rewrite`` are imported from ``dispatch_checks``
    (BX-16's own module) verbatim -- a second hand-rolled translation would
    drift the moment BX-16's flag set or rendering changes.
  - Does NOT read/write ``dispatch.py``'s guard chain. Registration is the
    EM's serial step (see this module's own commit/PR notes for the exact
    line and position).
  - Does NOT deny, on any platform. The one branch that ever denied
    (Windows-only, for substitutable residue) was provably unreachable in
    production -- see the module-docstring note at the top of this file --
    and was removed 2026-07-30 (H11(a)). ``host_is_windows`` stays on
    ``check()``'s signature for call-site compatibility with
    ``dispatch.py``'s existing keyword-forwarding, but this guard no
    longer branches on it.
  - Does NOT fail closed (deny) on an unparseable command
    (``classify_command`` returning ``tokens=None``). An unparseable
    command already fails to match ANY shape by the classifier's own
    contract, so this guard has nothing to act on.

Spec backlink: DoE-claude:pln-windows-viability-stop-the-spa-b969d9 § BX-6
Consumes: coordinator_core/bash_guards/_shape_classifier.py (BX-2)
Consumes: coordinator_core/bash_guards/dispatch_checks.py's
  ``check_grep_via_bash_rewrite`` and its private helpers (BX-16 shape 2)
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _segments_from_tokens_with_pipe_flag,
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _Shape,
    classify_command as _classify_command,
)
from coordinator_core.bash_guards.dispatch_checks import (
    _GREP_FAMILY_BINARIES_BT as _GREP_FAMILY_BINARIES,
    _bt_grep_flags_and_operands as _grep_flags_and_operands,
    _bt_grep_python_rewrite as _grep_python_rewrite,
    _bt_token_matches_binary as _token_matches_binary,
)
from coordinator_core._hook_envelope import allow_advisory
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards import _dialect
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

#: Review: code-reviewer -- Finding 5 (nit): these attributes are
#: vestigial in `bash_guards` -- `dispatch.py` imports `check` explicitly
#: and hardcodes ordering + `fail_closed` in its `guard_chain` literal
#: rather than doing attribute-based discovery (only `write_guards/tests/`,
#: a different guard family, reads `CLASS`). Pre-existing convention
#: copied from that family, not new to this module; kept for readability,
#: but `PRIORITY` in particular governs nothing here and duplicate values
#: across modules (e.g. this file's own `42` vs another module's `41`) do
#: not indicate real ordering -- see `dispatch.py`'s `guard_chain` list for
#: the actual order. `CLASS` itself was deleted (C14f, 2026-08-06): this
#: guard moved to ADVISORY_REWRITE on 2026-07-30 (H11(a)) and nothing in
#: the repo ever read the `"hard-deny"` string, which had gone stale and
#: actively contradicted this module's own "Does NOT deny" negative-spec.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 42

#: This guard's OWN escape hatch -- distinct from BX-16's
#: ``COORDINATOR_ALLOW_GREP_VIA_BASH`` (which suppresses the auto-rewrite
#: check, not this guard). Read inline at call time, never hoisted (this
#: package's established ``_override`` convention -- see
#: ``dispatch_checks._override``'s own docstring for why).
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD"

_SHAPE_NAME = "grep-via-bash"

#: PowerShell has no true alias for the grep family (`Select-String`/`sls`
#: is a different VERB, not an alias collision -- see docs/reference/
#: guard-dialect-coverage.md row 12). This guard's rewrite payload
#: (`_grep_python_rewrite`, imported verbatim from BX-16 above) is shaped
#: for POSIX grep's own SHORT-flag surface, not Select-String's
#: (`-Pattern`, `-Context N,N`, `-SimpleMatch`, `-CaseSensitive`, ...) --
#: the same "second, parallel flag grammar" problem
#: `guard-dialect-coverage.md`'s row 21 (`guard_inprocess_search`) names for
#: an identical rewrite dependency. Recognizing a Select-String/sls
#: invocation here would let this guard SEE the shape without being able to
#: safely translate it, so this module declares SILENT rather than guess a
#: rewrite it cannot honestly offer (row 12's own instruction: "prefer
#: SILENT to a guess").
_POWERSHELL_GREP_FAMILY_BINARIES = ("Select-String", "sls")


def _extract_command(payload: Dict[str, Any]) -> Optional[str]:
    """Return the CRLF-normalized ``command`` string for a Bash PreToolUse
    payload, or ``None`` if this payload is not a non-empty Bash call."""
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    return cmd.replace("\r", "")


#: Why a command classified as GREP_VIA_BASH failed to qualify as
#: substitutable residue -- kept distinct so the composed-advisory message
#: never misdescribes WHICH test failed (AC-7: a message must not claim a
#: command is chained/piped when the real reason is an untranslatable flag,
#: or vice versa).
_REASON_CHAINED = "chained"
_REASON_UNTRANSLATABLE = "untranslatable"


def _substitutable_rewrite(tokens: list) -> Tuple[Optional[str], str]:
    """Classify ``cmd``'s tokenization (already confirmed GREP_VIA_BASH) as
    substitutable residue or composed, per THE SUBSTITUTABLE/COMPOSED LINE
    in this module's docstring.

    Returns ``(rewrite_text, "")`` for substitutable residue, or
    ``(None, reason)`` for a composed command, where ``reason`` is one of
    the ``_REASON_*`` constants above -- so the caller can render an
    advisory that accurately names WHY no rewrite is offered, rather than a
    single generic message that may misdescribe a single-segment,
    untranslatable-flag command as "chained" (or vice versa).
    """
    segments = _segments_from_tokens_with_pipe_flag(tokens)
    if len(segments) != 1:
        return None, _REASON_CHAINED  # `;`/`&`-joined or piped -- composed
    seg_tokens, pipe_before = segments[0]
    if pipe_before or not seg_tokens:
        return None, _REASON_CHAINED  # something pipes INTO this segment
    # `classify_command` only matches GREP_VIA_BASH by finding a grep-family
    # binary in SOME segment; with exactly one segment here, that segment's
    # first token is necessarily the grep-family binary the classifier
    # found -- this is not re-derived, just asserted, to keep the reason
    # taxonomy exhaustive without a third, practically-unreachable branch.
    assert any(_token_matches_binary(seg_tokens[0], b) for b in _GREP_FAMILY_BINARIES)
    parsed = _grep_flags_and_operands(seg_tokens)
    if parsed is None:
        return None, _REASON_UNTRANSLATABLE  # long option / unrecognized short flag
    return _grep_python_rewrite(parsed), ""


#: Short flags that are GENUINELY GNU-only among grep's recognized set --
#: verified against macOS 26.5's `man grep` (BSD grep) on 2026-08-01 (C2,
#: docs/plans/2026-08-01-advisory-firing-shape-predicate.md). Each entry
#: below carries its own one-line rationale (asserted per-entry by
#: ``TestGnuOnlyDenylistRationaleDocumented`` in this guard's test module,
#: so a future addition cannot land undocumented):
#:   - `P` (`--perl-regexp`, PCRE): BSD grep ships no PCRE engine at all --
#:     outright absent, the highest-risk divergence (silently different
#:     match semantics, not a hard error) among this set.
#:   - `z` (NUL-separated records): outright absent on BSD grep -- unknown
#:     option, hard error rather than silent divergence.
#:   - `Z` (`--null`'s single-dash spelling elsewhere in the GNU grep
#:     family): a SEMANTIC COLLISION, not just absence -- BSD `-Z` means
#:     "force zgrep-style decompression," a different behavior under the
#:     same letter, which is a sharper hazard than an unrecognized flag.
#:   - `T` (`--initial-tab`): outright absent on BSD grep.
#: See ``_GNU_ONLY_LONG_OPTS`` immediately below for the long-option half
#: of the same denylist. See ``_has_gnu_only_construct``.
_GNU_ONLY_SHORT_FLAGS = frozenset({"P", "z", "Z", "T"})

#: DENYLIST, not an allowlist, of long options that are genuinely GNU-only
#: on BSD/macOS grep -- replaces the prior ``_PORTABLE_LONG_OPTS``
#: allowlist (C2, docs/plans/2026-08-01-advisory-firing-shape-predicate.md):
#: an allowlist makes every unexamined flag a false positive by default
#: (`--exclude-dir` and `--version`, both BSD-supported, fired under the
#: old shape purely because neither was `--include`/`--exclude`), whereas
#: BSD/macOS grep mirrors nearly all GNU long options per its own man page
#: STANDARDS section -- the genuinely-divergent set is small, so enumerating
#: it both fails safe (an unrecognized-but-actually-portable long option is
#: silent, not a false positive) and is smaller to maintain. Derived
#: 2026-08-01, macOS 26.5 `man grep` (BSD grep); re-verify against the
#: target platform's own `man grep` if grep versions diverge from this
#: baseline. Each entry's rationale (asserted per-entry by
#: ``TestGnuOnlyDenylistRationaleDocumented``):
#:   - `--perl-regexp`: long spelling of `-P` -- see that entry above.
#:   - `--include-from`, `--exclude-from`: absent on BSD grep (BSD only
#:     ships the base `--include`/`--exclude`, not the from-a-file forms).
#:   - `--initial-tab`: long spelling of `-T` -- see that entry above.
#:   - `--group-separator`, `--no-group-separator`: absent on BSD grep
#:     (context-line separator customization is a GNU-only extension).
_GNU_ONLY_LONG_OPTS = frozenset(
    {
        "--perl-regexp",
        "--include-from",
        "--exclude-from",
        "--initial-tab",
        "--group-separator",
        "--no-group-separator",
    }
)


def _has_gnu_only_construct(tokens: list) -> bool:
    """True iff some grep-family segment of ``tokens`` carries a
    genuinely GNU-only construct: a short flag in ``_GNU_ONLY_SHORT_FLAGS``
    (bare, or inside a combined short-flag cluster like `-rnP`) or a long
    option in ``_GNU_ONLY_LONG_OPTS``. Scans every grep-family segment of a
    chained/piped command, not only a lone single-segment one -- the
    GNU/BSD divergence is a property of the grep invocation wherever it
    sits, not of whether the command happens to be a single segment
    (`_substitutable_rewrite`'s `_REASON_CHAINED` short-circuits before
    ever inspecting flags, so this check re-scans independently rather
    than reusing that function's reason).

    DENYLIST, not an allowlist (C2, 2026-08-01) -- see
    ``_GNU_ONLY_LONG_OPTS``'s own comment for why the allowlist shape this
    replaced (``_PORTABLE_LONG_OPTS``) was itself the defect: it classified
    every long option BSD grep also supports (`--exclude-dir`, `--version`,
    among others) as GNU-only by default, an Axis-A false positive naming
    an alternative ("confirm intent") that did not apply to the flag that
    fired on. `--include`/`--exclude`, `--exclude-dir`, `--version`, `-o`,
    and `-r` are all BSD-supported and must NOT trip this check.
    """
    for seg_tokens, _pipe_before in _segments_from_tokens_with_pipe_flag(tokens):
        if not seg_tokens or not any(
            _token_matches_binary(seg_tokens[0], b) for b in _GREP_FAMILY_BINARIES
        ):
            continue
        for tok in seg_tokens[1:]:
            if not tok.startswith("-") or tok == "-":
                continue
            if tok.startswith("--"):
                if tok.split("=", 1)[0] in _GNU_ONLY_LONG_OPTS:
                    return True
                continue
            if any(ch in _GNU_ONLY_SHORT_FLAGS for ch in tok[1:]):
                return True
    return False


#: ALLOWLIST, not a blocklist, for a downstream token `_partial_pipe_rewrite`
#: is willing to re-quote via `shlex.quote`. This is the second round of
#: additions to what used to be a blocklist here (glob/variable/backtick/
#: tilde characters); a blocklist reproduces the same failure every time a
#: new construct is found (worklist Row G2 review, 2026-07-30 -- a bare
#: redirection operator like `>` and brace expansion like `out.{txt,bak}`
#: both slipped the prior blocklist, since neither contains `$*?`[` and
#: neither starts with `~`). An allowlist inverts the burden: a token is
#: only safe to re-quote if it is the kind of ordinary argv word
#: `shlex.quote` leaves syntactically inert -- word characters, and the
#: small set of punctuation (`@%+=:,./-`) that is common in flag values
#: and paths and carries no shell meaning of its own. Anything else --
#: `>`/`>>`/`<`/`<<`/`<<<` and their numbered/duplicating forms
#: (`2>`, `&>`, `>&`, `N>`, `N>>`), `|&`, brace expansion (`{`/`}`), glob
#: characters (`*`/`?`/`[`), variable/command substitution (`$`, `` ` ``),
#: a leading `~`, or anything else not on the allowlist -- declines rather
#: than risk re-quoting a token whose shell meaning `shlex.quote` would
#: change. Declining is always the safe outcome (see `_partial_pipe_rewrite`
#: docstring): a token this predicate does not recognize is treated as
#: unsafe, not as "probably fine."
_DOWNSTREAM_SAFE_TOKEN_RE = re.compile(r"^[\w@%+=:,./-]+$")


def _downstream_token_needs_expansion(tok: str) -> bool:
    """True if `tok` is NOT on the allowlist of ordinary argv words
    (see `_DOWNSTREAM_SAFE_TOKEN_RE`) -- i.e. re-quoting it via
    `shlex.quote` is not provably safe, so `_partial_pipe_rewrite` must
    decline rather than offer a rewrite for it. Kept as a "needs
    expansion"-named predicate (rather than renamed to "is unsafe") since
    its caller's control flow already reads that way; the allowlist
    inversion is internal to this function."""
    return not _DOWNSTREAM_SAFE_TOKEN_RE.match(tok)


def _partial_pipe_rewrite(tokens: list) -> Optional[str]:
    """For the narrow shape of exactly two `|`-joined segments where the
    FIRST is a substitutable grep-family invocation (per
    ``_grep_flags_and_operands``, the same gate the single-segment
    substitutable-residue path already uses) and nothing pipes INTO that
    first segment, build a real, runnable replacement: rewrite ONLY the
    grep segment to its python3 form (``_grep_python_rewrite`` -- the exact
    function the full single-segment rewrite already calls, never
    re-derived here), then pipe its stdout into the downstream segment's
    OWN tokens, re-quoted verbatim via ``shlex.quote`` -- the downstream
    stage is never re-interpreted or re-implemented, only re-quoted from
    the tokens the classifier already split out.

    This removes exactly ONE fork (the `grep` process) out of however many
    the full pipeline spawns -- not the whole pipeline -- so it is a
    strictly smaller win than the single-segment substitutable-residue
    rewrite `check()`'s final branch offers. It is nonetheless a REAL
    alternative for the single largest bucket of otherwise-untouched
    grep-via-Bash traffic (state/audits/2026-07-29-guard-module-ladder-
    worklist.md Row G2, DoE-claude repo) -- unlike that bucket's prior
    prose-only advisory, this names something the caller can actually run.

    Returns ``None`` for anything outside this narrow shape: not exactly
    two segments, the second not `|`-fed, the first `|`-fed itself
    (something feeds INTO the grep segment too -- a 3+-stage pipeline,
    out of scope), or the first segment's flags/pattern outside BX-16's
    substitutable set (an untranslatable flag or regex dialect makes
    ``_grep_flags_and_operands`` return ``None``, and this function
    inherits that same refusal rather than guessing). A `;`/`&&`-joined
    chain is >= 2 segments joined by a non-`|` separator and also returns
    ``None`` here -- this function never widens what counts as
    "chained"/"composed" beyond this one narrow, provably-safe case.

    Also returns ``None`` (2026-07-30, M13/M19 review fix, widened to an
    allowlist the same day after a second review round) when any
    DOWNSTREAM token is not on ``_DOWNSTREAM_SAFE_TOKEN_RE``'s allowlist of
    ordinary argv words -- i.e. re-quoting it via ``shlex.quote`` is not
    provably safe. This covers shell expansion (`$VAR`/`$(...)`, a glob
    like `*.py`, a leading `~`, a backtick substitution) AND, since the
    allowlist inverts the burden instead of naming constructs one at a
    time, redirection operators (`>`, `>>`, `<`, `<<`, `<<<`, `2>`, `&>`,
    `>&`, numbered forms, `|&`) and brace expansion (`out.{txt,bak}`) --
    neither of which the first round's blocklist caught, since neither
    contains a blocklisted character. The re-quoting step below
    reconstructs the downstream segment from its ALREADY-tokenized argv,
    which has already lost whether a token was originally bare or quoted --
    re-deriving the segment's exact original raw text to recover that
    distinction is not cheap, so rather than risk silently offering a
    command whose downstream half no longer means what the caller's own
    token meant, this narrows applicability instead and declines the shape
    entirely, same posture as every other refusal in this list.

    KNOWN LIMIT, stated rather than hidden (worklist Row G2's own "what
    makes this hard" section): the rewritten grep segment's stdout has not
    been differentially validated byte-for-byte against real grep, across
    every recognized flag combination, for every downstream consumer
    (trailing-newline handling on the last line, `-c`'s exact count
    format, `-l`'s path ordering/formatting) -- this is offered as a real,
    syntactically valid, one-fewer-fork alternative, never as a
    guaranteed-identical replacement. That is why this stays an advisory
    on every platform, same as the rest of the composed bucket -- see
    `check()` and this module's own docstring "THE SUBSTITUTABLE/COMPOSED
    LINE" -- never a deny anywhere; this function does not change that.
    """
    segments = _segments_from_tokens_with_pipe_flag(tokens)
    if len(segments) != 2:
        return None
    (up_tokens, up_pipe_before), (down_tokens, down_pipe_before) = segments
    if up_pipe_before or not down_pipe_before:
        return None
    if not up_tokens or not down_tokens:
        return None
    if not any(_token_matches_binary(up_tokens[0], b) for b in _GREP_FAMILY_BINARIES):
        return None
    if any(_downstream_token_needs_expansion(tok) for tok in down_tokens):
        return None
    parsed = _grep_flags_and_operands(up_tokens)
    if parsed is None:
        return None
    grep_rewrite = _grep_python_rewrite(parsed)
    downstream_cmd = " ".join(shlex.quote(tok) for tok in down_tokens)
    return "%s | %s" % (grep_rewrite, downstream_cmd)


def _composed_advisory(
    evidence: str,
    partial_rewrite: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Advisory-only envelope (never a deny, on any platform) for the
    narrow slice of composed grep-via-Bash traffic this guard still has
    something actionable to say about -- see `check()`'s gate just above
    its call site. Only two things reach this function (worklist row
    H11(c), 2026-07-30): a real partial rewrite (`_partial_pipe_rewrite`)
    or a genuine GNU-only grep construct (`_has_gnu_only_construct`).
    Everything else that used to reach this function (99.67% of this
    guard's prior firing set) is now silence -- design-as-offers (global
    `~/.claude/CLAUDE.md`): where no actionable alternative exists, the
    honest output is silence, not a ~2.2KB advisory naming none.

    Leads with the alternative, not the violation: when a partial
    rewrite exists it opens the message; the portability-only case (no
    rewrite, just a real GNU/BSD divergence) states the divergence
    plainly instead of first explaining what ISN'T offered.

    Does NOT explain why the in-process search answerer is unavailable
    here. That answerer is registered ahead of this guard in
    `dispatch.py`'s chain and has already evaluated and declined this
    exact invocation by the time this message is built -- true, and
    unusable by the reader, who cannot re-target the answerer or change
    the registration order. The chain mechanics belong here; the message
    keeps only the fallback the reader can actually take (dispatch a
    search subagent). Cut 2026-07-30 under the message-prose trim that
    also collapsed the override note to a pointer.

    Appends `operator_override_note(_OVERRIDE_ENV_VAR)` -- restored on a
    code-review finding (M17, 2026-07-30), after H11(b) had opted this guard's advisory out
    of the shared helper entirely, leaving `_OVERRIDE_ENV_VAR` functional
    but unmentioned. Every sibling advisory-emitting guard in this package
    names its own override route this same way; see
    `tests/test_override_route_inventory.py` for the gate that now keeps
    this guard (or any future one) from silently regressing again.
    """
    if partial_rewrite:
        # A real, runnable alternative -- see `_partial_pipe_rewrite`'s own
        # "KNOWN LIMIT" paragraph for the honest caveat on stdout parity
        # this offer does NOT claim to guarantee. Embedded VERBATIM, not
        # re-indented or re-wrapped: `expected_grep_rewrite in ctx`
        # (this guard's own test suite) pins the exact bytes
        # `_grep_python_rewrite` emits, and re-indenting a multi-line
        # `python3 -c` script by a constant offset breaks its top-level
        # statements (module-level code must start at column 0) --
        # unrunnable is worse than over-budget. Not exemptable either:
        # `_BACKTICK_RE` (`` `([^`\n]+)` ``) never matches across a
        # newline, so this guard's message-size floor is pinned by this
        # rewrite's own length, not by wrapper prose -- see this guard's
        # C8 execution report for the measured floor.
        lede = "%s one-fewer-fork replacement instead: %s" % (
            _SHAPE_NAME,
            partial_rewrite,
        )
    else:
        lede = "%s `%s` carries a GNU-only grep construct -- diverges on BSD grep (macOS)." % (
            _SHAPE_NAME,
            evidence,
        )
    return allow_advisory(
        "PreToolUse",
        "BASH-SPAWN ADVISORY: %s Else, Explore or general-purpose "
        "subagent. %s"
        % (lede, operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)),
    )


def _check_powershell(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """PowerShell-dialect leg (row 12, docs/reference/guard-dialect-
    coverage.md): declares SILENT for a Select-String/sls invocation this
    guard can SEE but cannot safely translate (see
    `_POWERSHELL_GREP_FAMILY_BINARIES`'s own comment) -- never a bare clean
    for input this guard actually recognized. Returns a genuine `None` (no
    SILENT) for any PowerShell command that is not grep-family-shaped at
    all -- that is a true clean, not a decline, mirroring how the Bash leg
    below returns bare `None` for a non-GREP_VIA_BASH command.
    """
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    tokens = _dialect.tokenize_command(
        cmd, _dialect.Dialect.POWERSHELL, guard_name="guard_grep_via_bash"
    )
    if tokens is None:
        return None  # SILENT already recorded by `_dialect` for the parse failure

    for seg_tokens, _pipe_before in _segments_from_tokens_with_pipe_flag(tokens):
        if seg_tokens and any(
            _token_matches_binary(seg_tokens[0], b)
            for b in _POWERSHELL_GREP_FAMILY_BINARIES
        ):
            record_silent(
                "guard_grep_via_bash",
                "PowerShell Select-String/sls invocation recognized, but "
                "this guard's rewrite payload (_bt_grep_python_rewrite) is "
                "shaped for POSIX grep's own short-flag surface and cannot "
                "translate Select-String's flag surface cleanly -- see "
                "docs/reference/guard-dialect-coverage.md row 12.",
            )
            return None
    return None


def check(
    payload: Dict[str, Any], host_is_windows: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    """Evaluate the grep-via-Bash guard against a PreToolUse(Bash) payload.

    Returns ``None`` for the overwhelming majority of matches (no match at
    all; substitutable residue, which `grep-via-bash-rewrite` -- registered
    earlier in `dispatch.py`'s guard chain -- already claims; or a composed
    command with no real alternative to name), or an advisory-only envelope
    (every platform, never a deny) for the narrow slice this guard still
    has something actionable to say about -- see `_composed_advisory`.

    ``host_is_windows`` stays on this signature for call-site compatibility
    with `dispatch.py`'s existing keyword-forwarding; this guard no longer
    branches on it (H11(a), 2026-07-30 -- see the module docstring).

    PowerShell dialect (`payload["tool_name"] == "PowerShell"`) routes to
    `_check_powershell` -- see that function's own docstring (row 12, C4c).
    """
    if _dialect.dialect_from_tool_name(payload.get("tool_name") or "") is _dialect.Dialect.POWERSHELL:
        return _check_powershell(payload)

    cmd = _extract_command(payload)
    if not cmd:
        return None
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    classification = _classify_command(cmd)
    if classification.tokens is None:
        return None
    if not classification.has_shape(_Shape.GREP_VIA_BASH):
        return None
    # GREP_VIA_BASH is the highest-precedence shape in
    # `_shape_classifier.SHAPE_PRECEDENCE` -- `has_shape` true here means
    # `classification.primary.shape` is always GREP_VIA_BASH too, so this
    # guard's message never misdescribes a command whose primary match is
    # actually some other shape (AC-7).
    match = classification.primary
    assert match is not None  # has_shape already confirmed this

    rewrite, reason = _substitutable_rewrite(classification.tokens)
    if rewrite is not None:
        # Substitutable residue -- `grep-via-bash-rewrite` (dispatch_checks.
        # check_grep_via_bash_rewrite), registered EARLIER in dispatch.py's
        # guard chain and sharing this module's own classification/parsing
        # helpers, already claims every command in this set as an
        # ADVISORY_REWRITE. This guard's own platform-conditioned deny/
        # advise for the same set was provably unreachable in production
        # (0 denies on either platform across the full corpus) and was
        # removed 2026-07-30 (H11(a)) -- nothing left to add here.
        return None

    partial_rewrite = (
        _partial_pipe_rewrite(classification.tokens)
        if reason == _REASON_CHAINED
        else None
    )
    if partial_rewrite is None and not _has_gnu_only_construct(classification.tokens):
        # Neither a real rewrite nor a genuine GNU/BSD divergence to name
        # -- per design-as-offers, the honest output where no actionable
        # alternative exists is silence, not a ~2.2KB advisory naming none
        # (H11(c) evidence: 99.67% of this guard's prior firing set was
        # exactly this case).
        return None
    return _composed_advisory(match.evidence, partial_rewrite, payload=payload)
