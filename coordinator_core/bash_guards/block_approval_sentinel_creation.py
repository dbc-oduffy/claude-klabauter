"""coordinator_core.bash_guards.block_approval_sentinel_creation -- PreToolUse
(Bash) hard-deny guard over Bash-level doctrine-approval-sentinel creation --
EM included.

WHAT THIS GUARD ACTUALLY BUYS, AND WHAT IT DOES NOT (read this before relying
on it). This is a LEXICAL classifier over command TEXT -- it never executes,
evaluates, or simulates the shell. Three rounds of forge-closure dispatches
(2026-07-28, first 2026-07-30 round, second 2026-07-30 round) have each
closed the shapes they were shown and left a neighbour uncaught; that is the
nature of a lexical approach, not a bug in any one round, and this guard's
own text says so rather than implying a boundary it cannot hold. What it
DOES buy: it stops an agent that reflexively creates the sentinel -- via
`touch`, a redirect, `mkdir`, `cp` from a stashed copy, `python3 -c`, or any
of the other enumerated shapes -- INCLUDING an agent that has just read a
deny message naming one of those shapes and would otherwise try it again the
same way. That is the realistic failure mode this guard exists for, and
closing it is worth having. What it does NOT buy: it is not a barrier
against an agent that deliberately constructs an evasion -- dynamic string
assembly, a glob near-miss, a runtime-computed basename -- see "KNOWN OPEN
GAP" below for the specific shapes known to defeat it. The boundary's real
strength does not live here alone: the sibling DoE-side read gate requires
the sentinel to be a REGULAR FILE (a forged directory is dead regardless of
what reaches the filesystem, see "KNOWN OPEN GAP"), the Write/Edit tool-call
surface is separately guarded by `_sentinel_write_guard` (a different code
path, not a text classifier over Bash strings), and the actual creation of a
genuine approval is an act only the human PM performs out-of-band -- those
hold regardless of whatever this classifier misses.

WHY THIS EXISTS. A sibling DoE-side hook denies edits to always-loaded
doctrine surfaces (global CLAUDE.md and friends) unless a repo-root sentinel
file named `.coordinator-doctrine-edit-approved` exists and is under 30
minutes old. The PM creates that sentinel themselves, by hand, as the
approval act. If ANY agent could create the sentinel via Bash -- `touch`,
a redirect, `cp` from a stashed copy, `python3 -c` -- the approval boundary
would be significantly weakened: an EM (or any dispatched subagent) could
reflexively forge its own approval and edit doctrine unsupervised. This
guard closes every shape of that it can see lexically -- not every
conceivable shape, see "KNOWN OPEN GAP" -- and denies CREATION only;
removing the sentinel is always safe (it re-locks the boundary, it never
grants anything) and stays allowed.

NOT IDENTITY-GATED -- fires for every caller, EM included, same posture as
`check_raw_pid_liveness.py` and `block_worktree_creation.py`: the anti-
pattern (self-forging PM approval) is wrong regardless of who types it. An
EM-only gate would defeat the guard's own purpose, since the EM is exactly
who this sentinel exists to constrain.

NO OVERRIDE -- DELIBERATE, by design, no exceptions. Precedent:
`block_subagent_destructive_action.py`'s own "OVERRIDE-WITHHOLDING,
deliberate" section (module docstring lines 41-45) -- a subagent (or an EM)
can set its own process env, so any `COORDINATOR_OVERRIDE_*` escape hatch
here would be reachable by exactly the caller class this guard exists to
constrain, and would make this a bypass of a bypass-prevention guard. There
is no legitimate reason for an agent to ever create this specific file --
the PM's own act of creating it IS the approval, so a programmatic path to
create it is never sanctioned, not even conditionally. Do not add one.

REGISTRATION ORDERING -- MUST run BEFORE `offer-git-c` in
`coordinator_core.bash_guards.dispatch`. That check rewrites `cd <dir> &&
git <sub>`-shaped commands into `git -C <dir> <sub>` and returns
allow-with-updatedInput, which SHORT-CIRCUITS every later guard in the
chain -- so a guard registered after it never sees `cd /tmp && touch
.coordinator-doctrine-edit-approved`. This exact bug was found and fixed
for `block_worktree_creation.py` (see that module's own docstring "CRITICAL
-- chain ordering" precedent and its registration comment in `dispatch.py`,
which sits just above `offer-git-c`); this guard is registered immediately
adjacent to it for the identical reason.

DETECTION SURFACE. Reuses the shared shell-shape tokenizer/segmenter from
`block_subagent_destructive_action.py` (`_tokenize_full_command`,
`_segments_from_tokens`, `_normalize_executable_basename`) rather than
inventing a second, weaker parser -- so leading env-var assignments,
`&&`/`;`/`|` chaining, and POSIX quoting (including adjacent-quote
concatenation, e.g. `'.coordinator'-'doctrine-edit-approved'`, which shlex
merges into one token) are all handled the same way the sibling guards
already handle them. Within each shell segment, this guard denies when it
finds either:

  1. A shell redirection (`>`, `>>`, optionally fd-prefixed like `2>`, in
     either bare-operator ("> file") or attached ("`>file`") form) whose
     target's basename is the sentinel filename.
  2. An invocation of a command capable of creating/overwriting a named
     file by argument -- `touch`, `cp`, `mv`, `install`, `ln`, `tee` -- where
     any argument's basename is the sentinel filename (source OR
     destination position; default-deny posture, see below).
  3. `sed -i` (any `-i`-prefixed in-place flag spelling) where an argument's
     basename is the sentinel filename.
  4. `python`/`python2`/`python3`(`.NN`) invoked with `-c <code>` (bare or
     attached, `-ccode`) where the code payload contains the sentinel
     filename as a substring -- covers `open(..., 'w')`-style writes without
     needing a full Python-source parser.

DEFAULT POSTURE ON AMBIGUITY IS DENY, DELIBERATELY ASYMMETRIC. This guard
protects an approval boundary, not a convenience default: a false negative
here (a creation slipping through) is a structural failure of the boundary
the sibling DoE hook depends on, while a false positive costs only a
rephrase. Consequently rule 2 above denies on ANY argument position
(source or destination) rather than trying to disambiguate which one is
being written -- e.g. `cp .coordinator-doctrine-edit-approved /tmp/x` reads
the sentinel and is technically harmless, but is denied anyway rather than
risk under-covering the destination case with a positional heuristic. Same
reasoning for rule 4: any substring mention of the filename inside a
`-c` payload denies, without inspecting whether the payload's write mode is
genuinely `'w'`.

ALLOWED, UNCONDITIONALLY: reads (`cat`, `ls`, `stat`) and removal (`rm`,
`test -f ... && rm ...`) of the sentinel. Removing an approval is always
safe -- it re-locks the boundary rather than unlocking it -- so this guard
never inspects `rm`/`test`/`cat`/`ls`/`stat` invocations at all; only the
command classes enumerated above are even considered.

KNOWN OPEN GAP -- DYNAMIC STRING CONSTRUCTION AND GLOB-SHAPED NEAR-MISSES
(documented, not solved; 2026-07-30 round-two and round-three forge-closure
dispatches). This guard, like its shared-engine base class
(`_sentinel_creation_guard.py`, see that module's own "KNOWN OPEN GAP"
entries for brace expansion and pre-existing symlink indirection), is a pure
LEXICAL classifier over the command TEXT -- it never executes, evaluates, or
simulates the shell. A payload that never spells the sentinel basename as a
contiguous substring in the command text defeats it, including after the
2026-07-30 variable-taint fix (and its round-three transitive-closure
extension) above:

  - A Python `chr()`/string-concatenation construction fed to `python -c`
    (e.g. building the basename byte-by-byte before calling
    `open(..., 'w')`).
  - A shell glob that merely resembles the basename without matching it
    exactly (e.g. `.coordinator-doctrine-edit-approv?d`, a single-character
    wildcard the shell expands at runtime but which this guard sees only as
    literal glob syntax, never as the resolved name).
  - A `$(...)`/backtick command substitution that COMPUTES the basename at
    runtime (as opposed to a plain variable assignment, which the taint fix
    above does track).
  - VARIABLE-ASSEMBLED BASENAMES (round-three finding, confirmed live):
    `S="prefix-"; S2="${S}suffix"; mkdir $S2` where NEITHER `S` nor `S2` is
    ever assigned a value that contains the sentinel basename as a
    contiguous substring -- the basename is only complete once bash
    concatenates the two half-strings at runtime. This is different IN KIND
    from the chained-dereference shapes the round-three taint fix DOES
    close (`B=$A; mkdir $B`, `C="${B}"`, any-length chain of those): those
    all propagate a value that already contains the full basename as a
    substring somewhere in the assignment chain, which is exactly what
    substring-search-based taint can follow. String assembly instead builds
    the substring itself out of fragments that never individually contain
    it -- closing that would mean symbolically evaluating shell string
    concatenation, a fundamentally different (and much heavier) mechanism
    than tracking which variable NAMES carry a tainted VALUE. Not attempted
    here; deliberately left in this same documented-gap class rather than
    chased into a partial, likely-inconsistent fix.

None of the above is closed here, and none can be closed by a smarter regex
alone -- doing so would require actually interpreting the shell (or the
embedded language), which is a different guard shape entirely, not a rule
addition to this one. What DOES still hold regardless: the Write/Edit
tool-call surface stays covered by the separate `_sentinel_write_guard` leg
(a different code path, not a text classifier over Bash strings), and the
sibling DoE-side read gate requires the sentinel to be a REGULAR FILE, so a
forged DIRECTORY at the sentinel's path -- which this guard's `mkdir` rule
(part of the 2026-07-30 first-round fix) already denies outright regardless
of this gap -- is closed on the read side too, independent of whatever a
future lexical bypass might slip past the create side.

Spec: doctrine-approval sentinel un-creatable-by-agent guard (DoE-claude
dispatch, 2026-07-28; round-two variable-taint closure, 2026-07-30) --
companion to the sibling DoE-side hook that reads this sentinel to gate
always-loaded-doctrine edits.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import re

from coordinator_core.bash_guards._sentinel_creation_guard import (
    REASON_INDIRECTION,
    SentinelCreationDetector,
    _REDIR_PREFIX_RE,
)
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _strip_heredoc_bodies,
    _tokenize_full_command,
)
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
#: Widened 2026-08-07 (C4e) from `["Bash"]` -- this guard now also declares
#: a verdict (or SILENT) for PowerShell, per the plan's per-guard-dialect-
#: declaration ruling (Doubt-check (2)). See `check()`'s own dialect gate.
#: A direct reference to the shared universe (C2 declaration-form
#: conversion) -- never a copy or re-wrap -- since this guard covers the
#: full command tool-name universe.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: The exact basename this guard protects. Never relaxed to a substring/
#: prefix match -- an unrelated file that merely CONTAINS this string in a
#: longer name (e.g. `.coordinator-doctrine-edit-approved.bak`) is a
#: DIFFERENT file and is not the approval sentinel the sibling DoE hook
#: reads; matching it too would be scope creep past what this guard is
#: chartered to protect.
_TARGET_BASENAME = ".coordinator-doctrine-edit-approved"

#: A `VAR=value` assignment token (bare, or the `VAR=value` half of an
#: `export VAR=value` pair -- `export` itself never matches this and is
#: simply skipped over, since the assignment token that follows it is
#: tokenized separately and matches on its own).
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

#: A `$VAR` or `${VAR}` dereference, anywhere inside a token (e.g. bare
#: `$S`, quoted `"$S"` -- shlex already strips the quotes so both read
#: identically by the time this guard sees them -- or embedded in a larger
#: token such as `of=$S`).
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


class _ApprovalSentinelDetector(SentinelCreationDetector):
    """Default-deny variant of the shared detector, scoped to THIS guard
    only (2026-07-30 forge-closure fix).

    WHY A SUBCLASS, NOT AN EDIT TO THE SHARED ENGINE. `SentinelCreationDetector`
    (`_sentinel_creation_guard.py`) is shared by three concrete guards --
    this one, `block_worktree_sentinel_creation.py`, and
    `block_disarm_marker_sentinel_creation.py`. The confirmed forge (an
    allowlist of file-creating commands -- `touch`/`cp`/`mv`/`install`/`ln`/
    `tee`/`sed -i`/`python -c`/`dd of=` -- that a live probe walked straight
    around via `mkdir`, `curl -o`, `wget -O`, `rsync`, `git checkout HEAD --`,
    and `unzip -d`, none of which are file-arg commands the base rule set
    even considers) is a defect specific to the approval-sentinel dispatch
    brief. Editing the shared class would silently change behavior for the
    other two sentinels without their own brief, their own review, or their
    own adversarial test pass -- out of scope here. Overriding only
    `_segment_denies` on a subclass keeps every other guard on the shared
    engine's existing (allowlist) posture untouched, while this guard gets
    the inverted (default-deny) posture. The parent's redirect-target check,
    env/wrapper-skip, indirection-unwrap pass (`_evaluate_segment_indirection`
    / `_classify_payload`), heredoc-stripping, and legacy unparseable-command
    fallback are all inherited unchanged -- the indirection pass in
    particular already denies outright for bare-interpreter-invoked scripts
    and heredoc-fed interpreters regardless of content (an existing DENY this
    override must never turn into an ALLOW -- "the inversion is a widening of
    what denies, never a narrowing"), and it recurses into THIS subclass's
    own overridden `_segment_denies` for `-c`-unwrapped payloads, so a nested
    `bash -c "mkdir <sentinel>"` gets the same default-deny treatment as a
    top-level one for free.

    THE INVERSION ITSELF. Rather than enumerating file-creating commands
    (which cannot be completed -- the next omitted tool is the next forge),
    a segment that MENTIONS the sentinel basename anywhere in its tokens
    (bare argument, substring inside a quoted code payload, a `key=value`
    operand such as `dd`'s `of=`) DENIES unless its head command is one of a
    narrow, explicitly-enumerated set of provably harmless operations:

      - `rm` -- removal is always sanctioned; it only re-locks the boundary.
      - Read-only inspection: `cat`, `ls`, `stat`, `test`, `head`, `tail`,
        `wc`, `file`, `grep`.
      - Read-only `git` subcommands: `status`, `diff`, `log`, `show`,
        `ls-files`, `rev-parse`, `describe`, `check-ignore`, `check-attr`.
        Any other `git` subcommand (`checkout`, `restore`, `stash` (`pop`
        included), or anything else) falls through to the mention-based deny
        like any other command, since those subcommands DO write. The set is
        an allowlist that grows one demonstrated entry at a time, NOT
        "read-only git subcommands" as a category -- see
        `_SAFE_GIT_SUBCOMMANDS` for why, and keep the deny message's
        enumeration in sync with it, since a message that gestures at the
        category rather than naming the members reads as broader than the
        code is.

    A shell redirection into the sentinel (rule 1, OVERRIDDEN below --
    `_redirect_target_denies` -- see "VARIABLE TAINT" for why) is checked
    FIRST and denies regardless of the head command's safety -- `cat
    /etc/passwd > <sentinel>` must not read as safe merely because `cat` is
    on the read-only list; the redirect is what writes the file, not `cat`.

    VARIABLE TAINT (2026-07-30, round-two forge-closure fix). The rules
    above are all PER-SEGMENT with no cross-segment data flow, so a variable
    assigned in one segment and dereferenced in the next evaded every one of
    them while working perfectly at runtime (bash resolves the variable
    within the same invocation): `S=<sentinel>; mkdir $S`,
    `S=<sentinel>; touch $S`, `L=<sentinel>; ln -s /tmp/x $L`, and
    `S=<sentinel>; cat /etc/hostname > $S` all created/overwrote the
    sentinel while this guard allowed. Closed by tracking which variable
    NAMES were assigned a value containing the sentinel basename anywhere in
    the command string (`_collect_tainted_vars`, scanning ALL top-level
    tokens once per `evaluate()` call, not per segment -- taint flows
    forward across `;`/`&&`/`||`/`|` segment boundaries within one command
    string, which is exactly the gap), then treating `$VAR`/`${VAR}` for any
    tainted VAR as a mention of the target wherever this detector already
    checks for a mention: `_segment_mentions_target` (the default-deny
    mention scan) and `_redirect_target_denies` (the redirect-target check,
    overridden here for that reason -- the parent class's version only knows
    the literal-basename compare `_is_target`, not variable taint). This is
    NOT "any segment whose head is unsafe AND the basename appears anywhere
    in the text denies" -- that broader rule was considered and rejected: it
    would wrongly deny `S=<sentinel>; cat /etc/hostname > $S` for the wrong
    reason (segment head `cat` IS safe) while still needing the redirect
    check to catch the actual write, and would wrongly ALLOW nothing extra
    over the taint approach while being harder to reason about. Taint is
    scoped to variable NAMES, not blanket text presence.

    TRANSITIVE TAINT (2026-07-30, round-three forge-closure fix). The
    round-two taint above only ever tainted a variable whose OWN assignment
    value directly contained the sentinel basename as a substring -- a
    single hop. A variable assigned from ANOTHER already-tainted variable
    (`B=$A` where `A=<sentinel>`, or the brace/quoted spellings `B="${A}"`/
    `B="$A"`) never itself contained the basename as a substring, so it
    stayed untainted, and a chain of any length (`C=$B; D=$C; mkdir $D`)
    evaded round two entirely while still resolving to the real sentinel at
    runtime. `_collect_tainted_vars` now runs to a FIXED POINT over the
    command's assignment list: each pass taints any variable whose value
    either (a) contains the basename directly, or (b) dereferences a
    variable name already in the tainted set, and passes repeat until a
    pass adds nothing new -- so a chain propagates however many hops it
    takes, in either assignment order (`B=$A; A=<sentinel>` would need a
    second pass to resolve too, which the fixed-point loop provides for
    free, though the confirmed live shapes all assign the root first). This
    is STILL substring-based taint, not string-construction evaluation --
    see the module docstring "KNOWN OPEN GAP" for why a variable-ASSEMBLED
    basename (`S="prefix-"; S2="${S}approved"` where neither value alone
    contains the full basename) is explicitly NOT closed by this and is not
    the same class of gap.

    shell-doc-ok: every quoted span above is a real shell command this
    detector exists to deny, transcribed from a confirmed-live evasion --
    the taint rules are unexplainable without the assignment-and-dereference
    spelling they match.
    """

    #: Commands that can never create/modify the sentinel through their own
    #: normal operation (absent a redirect, which is checked separately and
    #: first -- see class docstring). Removal is always sanctioned; the rest
    #: are pure reads.
    _SAFE_ARGV0 = frozenset(
        {"rm", "cat", "ls", "stat", "test", "head", "tail", "wc", "file", "grep"}
    )

    #: `git` subcommands that only read repo state. Anything else under
    #: `git` (`checkout`, `restore`, `stash`, or an unrecognized subcommand)
    #: is NOT presumed safe and falls through to the mention-based deny.
    #:
    #: `check-ignore` and `check-attr` are pure path QUERIES -- they resolve a
    #: pathname against `.gitignore` / `.gitattributes` and report, touching
    #: no file and no index. Both were missing until 2026-07-31, when a
    #: `git check-ignore .coordinator-doctrine-edit-approved` -- the exact
    #: command that verifies the sentinel is ignored, i.e. that the
    #: checkout-materialises-a-fresh-sentinel hole is closed -- was denied as
    #: a write. That is the guard refusing the very check that confirms its
    #: own boundary holds. `check-attr` is admitted alongside it as the
    #: identical query shape rather than waiting for its own false positive.
    #:
    #: Deliberately NOT widened past demonstrated need: this set is an
    #: enumerate-the-harmless allowlist by construction (see the class
    #: docstring's "THE INVERSION ITSELF"), so it grows one justified entry at
    #: a time. `blame`, `cat-file`, and `ls-tree` are equally read-only and
    #: equally absent -- add them when something actually needs them, with
    #: the same note.
    _SAFE_GIT_SUBCOMMANDS = frozenset(
        {
            "status", "diff", "log", "show", "ls-files", "rev-parse", "describe",
            "check-ignore", "check-attr",
        }
    )

    def _segment_is_safe(self, seg_tokens: "list[str]", argv0_idx: int) -> bool:
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if base in self._SAFE_ARGV0:
            return True
        if base == "git" and argv0_idx + 1 < len(seg_tokens):
            sub = seg_tokens[argv0_idx + 1]
            if sub in self._SAFE_GIT_SUBCOMMANDS:
                return True
        return False

    def __init__(self, target_basename: str) -> None:
        super().__init__(target_basename)
        #: Variable names, tainted for the CURRENT `evaluate()` call only --
        #: recomputed at the top of `evaluate()` from that call's own
        #: command string, never carried over between calls. See class
        #: docstring "VARIABLE TAINT".
        self._tainted_vars: "set[str]" = set()

    def _collect_tainted_vars(self, tokens: "list[str]") -> "set[str]":
        """Scan every token of the (whole, not-yet-segmented) command for a
        `VAR=value` assignment -- bare, or the half of `export VAR=value`
        that actually carries the value -- and return the set of tainted
        variable names, iterated to a FIXED POINT (see class docstring
        "TRANSITIVE TAINT"). Deliberately scans the FULL token stream rather
        than one segment at a time, so taint set in one segment is visible
        to a later segment in the same command string (see class docstring
        "VARIABLE TAINT").

        A variable is tainted if its assignment value either directly
        mentions the sentinel basename, or dereferences a variable name
        already in the tainted set (`B=$A`, `B="${A}"`, `B="$A"` -- shlex
        has already stripped the surrounding quotes by the time this guard
        sees the token, so the three spellings are indistinguishable here).
        Repeated passes let a chain of any length resolve regardless of
        which order its links were assigned in the command string."""
        assignments: "list[tuple[str, str]]" = []
        for tok in tokens:
            m = _ASSIGN_RE.match(tok)
            if m:
                assignments.append((m.group(1), m.group(2)))

        tainted: "set[str]" = set()
        changed = True
        while changed:
            changed = False
            for var, value in assignments:
                if var in tainted:
                    continue
                if self._mention_re.search(value):
                    tainted.add(var)
                    changed = True
                    continue
                if any(
                    vm.group(1) in tainted for vm in _VAR_REF_RE.finditer(value)
                ):
                    tainted.add(var)
                    changed = True
        return tainted

    def _token_dereferences_tainted_var(self, token: str) -> bool:
        return any(
            m.group(1) in self._tainted_vars for m in _VAR_REF_RE.finditer(token)
        )

    def _segment_mentions_target(self, seg_tokens: "list[str]") -> bool:
        for tok in seg_tokens:
            if self._mention_re.search(tok):
                return True
            if self._token_dereferences_tainted_var(tok):
                return True
        return False

    def _redirect_target_denies(self, seg_tokens: "list[str]") -> bool:
        """OVERRIDE (2026-07-30, round-two forge-closure fix): the parent's
        version only ever checks the literal-basename compare (`_is_target`)
        against the redirect's target token, so `cat /etc/hostname > $S`
        (with `S` tainted from an earlier segment) slipped through -- the
        redirect target token is literally `$S`, never the sentinel
        basename itself. Same two-token shapes as the parent (`> file` bare,
        `>file` attached, optionally fd-prefixed/duplicated), plus a
        tainted-variable-dereference check on the resolved candidate.

        shell-doc-ok: the redirect spellings quoted here are the literal
        shell forms this override parses; naming them is the docstring's
        whole content."""
        n = len(seg_tokens)
        for i, tok in enumerate(seg_tokens):
            m = _REDIR_PREFIX_RE.match(tok)
            if not m:
                continue
            remainder = tok[m.end() :]
            if remainder:
                candidate = remainder
            elif i + 1 < n:
                candidate = seg_tokens[i + 1]
            else:
                continue
            if self._is_target(candidate):
                return True
            if self._token_dereferences_tainted_var(candidate):
                return True
        return False

    def evaluate(self, cmd: str):
        """OVERRIDE: recompute `self._tainted_vars` from THIS call's command
        string before delegating to the parent's `evaluate()`, which drives
        `_segment_denies` (and, through it, the two taint-aware overrides
        above) per segment. Falls back to an empty taint set for an
        unparseable command -- the parent's own legacy free-text fallback
        (`_evaluate_legacy`) does not use segments or taint at all, so an
        empty set there costs nothing."""
        cmd_norm = _strip_heredoc_bodies(cmd)
        tokens = _tokenize_full_command(cmd_norm)
        self._tainted_vars = self._collect_tainted_vars(tokens) if tokens else set()
        return super().evaluate(cmd)

    def _segment_denies(self, seg_tokens: "list[str]") -> bool:  # noqa: D401
        """Default-deny override: a redirect into the sentinel always
        denies (rule 1, taint-aware override above); otherwise a segment
        whose head command
        is NOT in the safe set denies as soon as ANY of its tokens mentions
        the sentinel basename -- replacing the parent's enumerated-command
        rules 2-5 (`touch`/`cp`/`mv`/`install`/`ln`/`tee`, `sed -i`,
        `python -c`, `dd of=`), all of which are subsumed by this blanket
        check (each of those commands takes the sentinel as a plain
        argument or an embedded substring, and none of them is in
        `_SAFE_ARGV0`)."""
        if not seg_tokens:
            return False
        if self._redirect_target_denies(seg_tokens):
            return True
        argv0_idx = self._env_skip_index(seg_tokens)
        if argv0_idx >= len(seg_tokens):
            return False
        if self._segment_is_safe(seg_tokens, argv0_idx):
            return False
        return self._segment_mentions_target(seg_tokens)


#: Detection engine for this guard specifically -- see `_ApprovalSentinelDetector`
#: docstring above for why this is a dedicated subclass rather than a shared-
#: engine edit. `block_worktree_sentinel_creation.py` and
#: `block_disarm_marker_sentinel_creation.py` remain on the base
#: `SentinelCreationDetector` (allowlist posture), unchanged by this fix.
_detector = _ApprovalSentinelDetector(_TARGET_BASENAME)


def _evaluate(cmd: str, dialect: Optional[Dialect] = None):
    """`dialect=None`/`Dialect.BASH` preserves the exact pre-C4e call shape
    (`_detector.evaluate(cmd)`, AC4) -- only a genuinely recognized
    non-bash dialect routes through the new dialect-aware entry point (see
    `_sentinel_creation_guard.SentinelCreationDetector.evaluate_for_dialect`)."""
    if dialect is None or dialect is Dialect.BASH:
        return _detector.evaluate(cmd)
    return _detector.evaluate_for_dialect(
        cmd, dialect, guard_name="block_approval_sentinel_creation"
    )


def _deny_reason(cmd: str, reason_kind: str, reason_class: str) -> str:
    # Deliberately does NOT echo `cmd` back into the message and does NOT
    # name the target basename in either branch below -- both would print
    # the exact bypass an eager agent could copy-paste, which reads as
    # sanctioning it rather than blocking it. `cmd` stays accepted for
    # call-site symmetry with the sibling guard, but is intentionally
    # unused here.
    #
    # `reason_class` (2026-07-28 diagnosability fix -- see
    # `_sentinel_creation_guard.py` module docstring "REASON CLASS") splits
    # the ONE fixed message this function used to return into two truthful
    # ones: a REASON_DIRECT deny means a rule positively matched the
    # sentinel, so the "this command would create/modify the sentinel"
    # assertion is actually correct. A REASON_INDIRECTION deny means the
    # opposite -- the payload sits behind an interpreter/env/xargs/heredoc
    # wrapper this guard cannot examine, so it denies BY CONSTRUCTION, not
    # because anything was found. The prior single-message version asserted
    # the DIRECT text on an INDIRECTION deny too, which is how a caller
    # whose script never mentioned the sentinel got told it would create
    # one -- a false assertion that cost a live cross-repo debugging
    # round-trip (see dispatch brief, 2026-07-28).
    del cmd
    if reason_class == REASON_INDIRECTION:
        # `reason_kind` is safe to surface here (it names a shell SHAPE --
        # e.g. "bash <file> (interpreter-invoked script -- indirection
        # wrapper, script content unexamined)" -- never a bypass to
        # copy-paste), but a RECURSIVE indirection verdict can still bottom
        # out one level down in the direct branch's target-naming string
        # (e.g. `bash -c "touch <sentinel>"` unwraps through exactly that).
        # Redact the basename out regardless of which sub-path produced it,
        # rather than trusting the branch alone to guarantee echo-safety.
        safe_shape = reason_kind.replace(_TARGET_BASENAME, "<the sentinel>")
        return (
            "BLOCKED (approval-sentinel guard): this command was denied "
            "because its payload is delivered through an interpreter, "
            "stdin, or command-assembly indirection this guard cannot "
            "examine -- NOT because the payload was found to touch the "
            "approval sentinel.\n\n"
            "Detected shape: %s\n\n"
            "If this command genuinely does not touch the approval "
            "sentinel: run its underlying steps directly (not through an "
            "interpreter/stdin/xargs wrapper) so this guard can see them, "
            "or ask the EM/PM to run it.\n\n"
            "Reading or removing an existing sentinel remains available as a "
            "DIRECT command -- `cat`, `ls`, `stat`, `rm` -- but not through a "
            "wrapper like this one: inside an interpreter payload this guard "
            "cannot tell a read from a write, so it denies either way. "
            "Removal only re-locks the boundary." % safe_shape
        )
    del reason_kind  # REASON_DIRECT: message below is fixed, not shape-derived.
    return (
        "BLOCKED: creates/modifies the PM-approval sentinel for doctrine "
        "edits; agents cannot self-approve. Ask the PM to create it.\n\n"
        "Use instead: `cat`, `ls`, `stat`, `test`, `head`, `tail`, `wc`, "
        "`file`, `grep`, `rm`, `git status`, `git diff`, `git log`, "
        "`git show`, `git ls-files`, `git rev-parse`, `git describe`, "
        "`git check-ignore`, `git check-attr`. Removal re-locks the boundary."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the approval-sentinel-creation-ban gate against a
    PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller including the main-loop EM
    (see module docstring "NOT IDENTITY-GATED").
    """
    # Deliberately no try/except here -- fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards (dispatch.py's guard_chain
    # fail_closed=True entries route an uncaught exception through its
    # crash-deny wrapper); catching and swallowing an unexpected error into
    # a silent allow here would defeat that contract.
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None
    dialect = dialect_from_tool_name(tool_name)

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    # NOTE: deliberately no raw-text `_MENTION_RE` pre-filter gate here
    # (unlike the sibling guards' cheap-probe posture) -- a partially-quoted
    # spelling such as `'.coordinator-doctrine-edit'"-approved"` does NOT
    # contain the sentinel basename as a contiguous raw substring (the quote
    # characters sit between the two halves); only the TOKENIZED form (after
    # shlex merges the adjacent quoted segments) reconstructs it. Gating on
    # the raw substring here would silently defeat exactly the "partially-
    # quoted spellings" coverage this guard is chartered to close. The
    # tokenized pass below is cheap enough that skipping this pre-filter is
    # not a meaningful cost.
    deny, reason_kind, reason_class = _evaluate(cmd, dialect)
    if not deny:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(cmd, reason_kind, reason_class),
        }
    }
