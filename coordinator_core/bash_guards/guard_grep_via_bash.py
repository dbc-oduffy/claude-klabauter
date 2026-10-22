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
advisory-only, and only for a genuine GNU-only grep construct (see
``_has_gnu_only_construct``). Everything else this guard
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
A partial-pipe rewrite (grep half to ``python3 -c``, downstream verbatim)
was offered for the two-segment `grep | downstream` shape and DELETED by
M10R: it replaces the grep process with an interpreter process, a spawn
delta of zero on every shape it accepted
(state/audits/2026-09-30-grep-composed-advisory-fork-delta.md). A
composed command now gets only the GNU-only portability advisory.

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
check; `check()` now only calls `_composed_advisory` when it finds one --
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

Spec backlink: coordinator-content-repo:pln-windows-viability-stop-the-spa-b969d9 § BX-6
Consumes: coordinator_core/bash_guards/_shape_classifier.py (BX-2)
Consumes: coordinator_core/bash_guards/dispatch_checks.py's
  ``check_grep_via_bash_rewrite`` and its private helpers (BX-16 shape 2)
"""

from __future__ import annotations

import os
import shlex
from typing import Any, Dict, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _segments_from_tokens_with_pipe_flag,
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _Shape,
    ShapeClassification as _ShapeClassification,
    classify_command as _classify_command,
    tokenize_for_classification as _tokenize_for_classification,
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

_POWERSHELL_GREP_FAMILY_BINARIES = ("Select-String", "sls")


def _extract_command(payload: Dict[str, Any]) -> Optional[str]:
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
        return None, _REASON_CHAINED
    seg_tokens, pipe_before = segments[0]
    if pipe_before or not seg_tokens:
        return None, _REASON_CHAINED
    # `classify_command` only matches GREP_VIA_BASH by finding a grep-family
    # binary in SOME segment; with exactly one segment here, that segment's
    # first token is necessarily the grep-family binary the classifier
    # found -- this is not re-derived, just asserted, to keep the reason
    # taxonomy exhaustive without a third, practically-unreachable branch.
    assert any(_token_matches_binary(seg_tokens[0], b) for b in _GREP_FAMILY_BINARIES)
    parsed = _grep_flags_and_operands(seg_tokens)
    if parsed is None:
        return None, _REASON_UNTRANSLATABLE
    rewrite = _safe_grep_rewrite(parsed)
    if rewrite is None:
        return None, _REASON_UNTRANSLATABLE
    return rewrite, ""


def _safe_grep_rewrite(parsed: Dict[str, Any]) -> Optional[str]:
    """``_grep_python_rewrite`` output iff it builds and its ``python3 -c``
    script compiles; ``None`` (caller keeps the original command) on any error."""
    try:
        rewrite = _grep_python_rewrite(parsed)
        compile(shlex.split(rewrite)[-1], "<grep-rewrite>", "exec")
    except (SyntaxError, ValueError, KeyError, TypeError, IndexError):
        return None
    return rewrite


#: Short flags GNU-only among grep's recognized set (macOS 26.5 `man grep`,
#: 2026-08-01); one rationale per entry, asserted by
#: ``TestGnuOnlyDenylistRationaleDocumented``:
#:   - `P` (`--perl-regexp`): BSD grep has no PCRE engine.
#:   - `z`: NUL-separated records; absent on BSD grep.
#:   - `Z`: semantic collision -- BSD `-Z` forces zgrep-style decompression.
#:   - `T` (`--initial-tab`): absent on BSD grep.
#: The long-option half is ``_GNU_ONLY_LONG_OPTS``; see ``_has_gnu_only_construct``.
_GNU_ONLY_SHORT_FLAGS = frozenset({"P", "z", "Z", "T"})

#: Denylist of long options GNU-only versus BSD/macOS grep. Derived 2026-08-01,
#: macOS 26.5 `man grep`; re-verify against the target platform's `man grep`.
#:   - `--perl-regexp`: long spelling of `-P`.
#:   - `--include-from`, `--exclude-from`: BSD ships only `--include`/`--exclude`.
#:   - `--initial-tab`: long spelling of `-T`.
#:   - `--group-separator`, `--no-group-separator`: context-separator
#:     customization is a GNU extension.
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


def _composed_advisory(
    evidence: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Advisory-only envelope (never a deny, on any platform) for a
    composed or untranslatable grep-via-Bash command carrying a genuine
    GNU-only grep construct (``_has_gnu_only_construct``). Everything else
    is silence -- design-as-offers: where no actionable alternative exists,
    the honest output is silence.

    Does NOT explain why the in-process search answerer is unavailable
    here: the reader cannot re-target it. The message keeps only the
    fallback the reader can take (dispatch a search subagent).

    Appends `operator_override_note(_OVERRIDE_ENV_VAR)`; the gate in
    `tests/test_override_route_inventory.py` keeps it from regressing.
    """
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


def _evaluate_grep_via_bash_match(
    classification: _ShapeClassification, payload: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Shared composed/substitutable-residue evaluation for a command whose
    `classification.primary` is already confirmed `GREP_VIA_BASH`, on EITHER
    dialect (C3, pln-the-shape-classifier-reaches-a-e743e5). `_substitutable
    _rewrite`/`_has_gnu_only_construct` both operate
    on already-tokenized argv, which carries no dialect-specific syntax of
    its own once split -- D2's own finding that GREP_VIA_BASH "works
    unchanged" on the PowerShell leg (the same binary-identity detector
    fires on `grep`/`rg` regardless of which shell spawned it) extends to
    this guard's rewrite/advisory payload too, which is shaped around POSIX
    grep's own short-flag surface -- a surface `grep`/`rg` present the same
    way under either dialect, since the BINARY itself does not change.
    Extracted so the PowerShell leg can reuse it instead of re-deriving a
    parallel copy (AC11).

    Substitutable residue (a full rewrite exists) still returns `None` here
    on EITHER dialect: on Bash, `grep-via-bash-rewrite` (registered earlier
    in `dispatch.py`'s chain) already claims it; on PowerShell, no sibling
    rewrite chain entry is reachable for this shape at all
    (`grep-via-bash-rewrite`'s own `matchers=("Bash",)` -- out of this
    chunk's write set to widen), so this guard stays silent rather than
    invent a rewrite outlet that does not exist to point at.
    """
    rewrite, _reason = _substitutable_rewrite(classification.tokens)
    if rewrite is not None:
        return None
    if not _has_gnu_only_construct(classification.tokens):
        return None
    match = classification.primary
    assert match is not None
    return _composed_advisory(match.evidence, payload=payload)


def _check_powershell(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """PowerShell-dialect leg (row 12, docs/reference/guard-dialect-
    coverage.md): declares SILENT for a Select-String/sls invocation this
    guard can SEE but cannot safely translate (see
    `_POWERSHELL_GREP_FAMILY_BINARIES`'s own comment) -- never a bare clean
    for input this guard actually recognized.

    ALSO (C3, pln-the-shape-classifier-reaches-a-e743e5 § AC4/AC12): carries
    `dialect_from_tool_name`'s result into `classify_command` -- a native
    `grep`/`rg` invocation is the SAME binary-identity match on either
    dialect (D2), so this leg reuses `_evaluate_grep_via_bash_match` (the
    same composed/GNU-only-construct advisory the Bash leg renders) rather
    than only recognizing the Select-String vocabulary and staying silent
    on everything else, which is what this leg did before C3 -- a genuine
    `grep -rn TODO src/` run from a PowerShell prompt used to get no verdict
    from this guard at all.

    Returns a genuine `None` (no SILENT) for any PowerShell command that is
    not grep-family-shaped at all -- that is a true clean, not a decline,
    mirroring how the Bash leg below returns bare `None` for a
    non-GREP_VIA_BASH command.
    """
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    # One heredoc-stripped tokenize serves the decline check and the
    # classification below: a heredoc body is stdin data, so a command that
    # only fails to parse because of its body is judged on its real command
    # text, same as the Bash leg.
    tokens = _tokenize_for_classification(
        cmd, _dialect.Dialect.POWERSHELL, guard_name="guard_grep_via_bash"
    )
    if tokens is None:
        return None

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

    classification = _classify_command(
        cmd, dialect=_dialect.Dialect.POWERSHELL, tokens=tokens
    )
    if not classification.has_shape(_Shape.GREP_VIA_BASH):
        return None
    return _evaluate_grep_via_bash_match(classification, payload)


def check(
    payload: Dict[str, Any], host_is_windows: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
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
    #
    # Substitutable residue (a full rewrite exists) is claimed by
    # `grep-via-bash-rewrite` (dispatch_checks.check_grep_via_bash_rewrite),
    # registered EARLIER in dispatch.py's guard chain -- this guard's own
    # platform-conditioned deny/advise for that set was provably
    # unreachable in production (0 denies on either platform across the
    # full corpus) and was removed 2026-07-30 (H11(a)). Composed residue
    # (a chained/piped command, or a genuine GNU-only construct) has
    # nothing upstream to claim it -- `_evaluate_grep_via_bash_match`
    # (shared with the PowerShell leg, C3) renders that advisory, or stays
    # silent per design-as-offers when no
    # genuine GNU/BSD divergence exists to name (H11(c) evidence: 99.67% of
    # this guard's prior firing set was exactly that silent case).
    return _evaluate_grep_via_bash_match(classification, payload)
