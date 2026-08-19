"""coordinator_core.bash_guards.block_subagent_destructive_action -- Python
engine-ification of DoE's retired
``coordinator/hooks/scripts/block-subagent-destructive-action.sh`` PreToolUse
hook (deleted 2026-07-16, DoE ``2f8b8450``; fold-candidate #3 of 5, W3a
recipe section (b)).

This is a faithful engine-ification, not a redesign: it ports the reference
hook's TWO-LAYER CLASSIFIER, deny conditions, ordering, and deny-reason text
byte-for-byte. Any behavior gap noticed while porting is a finding to surface,
not a fix to silently apply here.

Origin incident (2026-07-13): a review-integrator subagent, detecting its own
mis-scoped commit on a shared work/* branch, "repaired history" with
git stash + git rebase -i + git stash pop. The dropped commit held a SIBLING
agent's entire feature. This guard EM-LOCKS the destructive-action class
(git history-rewrite/working-tree-clobber, recursive rm, recursive
chmod/chown) when the caller is a subagent, leaving the EM main-loop
unaffected.

TWO-LAYER CLASSIFIER:
  Layer 1 -- dangerous-surface detection (v1 scope: git + rm + chmod/chown -R
    ONLY -- docker/kill/DB-client surfaces are OUT of v1, deliberately not
    implemented here). A command touching none of these surfaces allows
    immediately, BEFORE identity resolution, so the overwhelming majority of
    subagent Bash (ls/grep/python/cat) pays no identity-resolution cost.
  Layer 2 -- within a detected dangerous surface, DEFAULT-DENY with a
    safe-forward allowlist. A novel destructive git verb is denied because it
    is NOT on the allowlist, not because it matched a blocklist entry.

IDENTITY AXIS -- DUAL OR-resolver (mirrors the
block_reviewer_bash_outside_allowlist sibling guard's resolver, per the Staff Engineer
Finding 0):
top-level ``agent_type`` (PRIMARY, present at Bash-call time for
UNNAMED/foreground dispatch) OR'd with the back-pointer-chain
``subagent_type`` (SECONDARY, covers NAMED/teammate dispatch). Unlike
fold-candidate #2's set-membership confined-agent check, this guard fires on
ANY resolved subagent type -- it is not an allowlist of confined types, it is
"any resolved subagent + destructive surface -> deny-by-default". AMBIGUOUS
on the SECONDARY leg overrides a populated PRIMARY leg unconditionally.

OVERRIDE-WITHHOLDING (deliberate, per reference hook comment block): this
guard NEVER consults any ``COORDINATOR_OVERRIDE_*``/``COORDINATOR_ALLOW_*``
env var in subagent context -- there is NO subagent-reachable escape hatch
(contrast ``block_subagent_plan_body_bash_write.py``, which DOES honor
``COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY``). The Python port MUST NOT add
one. A sanctioned destructive op routes back through the EM main-loop, which
is never blocked by this guard (no agent_id -> allow).

FAIL POSTURE -- two independent rules from two different sibling families:
  - Fail OPEN (allow) on every unevaluable input (missing/malformed
    agent_id, unreadable back-pointer, empty resolved type) -- borrowed from
    the destructive-git guard family, which fails open on infra/parse
    failure so a broken guard never bricks a subagent's benign Bash.
  - Fail CLOSED (deny) ONLY on the AMBIGUOUS identity-collision sentinel --
    borrowed from the identity-resolution sibling family, which denies
    unconditionally when two dispatches collided on one canonical id.

Anti-scope (do NOT extend without a spec update):
  - Do NOT default-deny all subagent Bash -- Layer 1 must gate identity
    resolution; benign Bash exits before any identity-resolution cost.
  - Do NOT add a subagent-reachable override env var.
  - Do NOT widen Layer 1 to docker/kill/DB-client surfaces (deferred, out of
    v1 scope).

FAIL-OPEN OBSERVABILITY (2026-07-29 addition, PM-authorized, observability
only -- see "FAIL-OPEN OBSERVABILITY" comment block above `_log_fail_open`
for the full rationale): root-caused live by
DoE-claude state/audits/2026-07-29-destructive-git-guard-inconsistency.md --
a `git rm --cached` denied in one repo and silently allowed the identical
shape in another, and `check()` returning `None` on ANY of the three
fail-open branches above wrote nothing anywhere, so the incident could not
be reproduced after the fact. Each of the three fail-open branches now
appends an identity-resolution-tuple record to a settings-home-rooted,
guard-specific log (`_fail_open_log_path`) whenever Layer 1 has ALREADY
flagged the command as touching a dangerous surface -- ordinary benign
subagent Bash still exits at the Layer-1 gate and never reaches this code.
This is a PURE ADDITION: no allow/deny verdict changes on any branch,
confirmed by the existing deny/allow regression suite staying green plus
new tests pinning each fail-open branch's log record and confirming a
fail-open on a NON-flagged command logs nothing.

Ported from the retired DoE bash guard ``block-subagent-destructive-action.sh``
  (deleted 2026-07-16, DoE ``2f8b8450``).
Spec backlink: DoE-claude:pln-subagent-destructive-action-em-88a860
Recipe: scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md section (b).3

INDIRECTION-WRAPPER HARDENING (2026-07-21 addition, NOT part of the original
bash port -- named exception to the anti-scope note above, authorized by
``cross-repo/inbox/2026-07-20-claude-central-em-subagent-destructive-guard-
indirection-bypass.md``): the surface probes above classify on LITERAL
top-level verb tokens (``git``/``rm``/``chmod``/``chown``), so a subagent can
route a denied destructive command through one level of interpreter/xargs/env
indirection -- ``bash <file>``, ``sh -c '...'``, ``env ... <cmd>``, ``xargs``
-- and the wrapper form itself contains none of those tokens at a matching
word-boundary, so Layer 1 never fires. This happened for real (2026-07-20
near-miss, no harm) and is fixed here by adding a SECOND wrapper-shape probe
(``_WRAPPER_PROBE_RE``) alongside the git/rm/chmod probes, and a
subagent-scoped ``_evaluate_wrapper_indirection`` pass in Layer 2:

  - ``<interp> -c '<payload>'`` and ``env ... <cmd>`` shapes are RELIABLY
    parseable (the payload text is present verbatim in the command string)
    -- these are UNWRAPPED via ``shlex`` and the payload is recursively run
    back through the SAME git/rm/chmod matchers used above (reuse, not a
    parallel classifier).
  - ``<interp> <file>`` (no ``-c``) and ``xargs`` shapes are NOT reliably
    parseable (the script's content, or the command xargs will build from
    stdin, is not in the command text at all) -- these are DENIED OUTRIGHT
    for subagents, per the memo's explicit sanction ("Denying ... on those
    specific shapes when the caller is a subagent"). This is a deliberate
    over-block (a legitimate ``bash run-tests.sh`` is denied too) in
    exchange for closing the accidental-bypass case without pretending to
    solve the adversarial one -- see the memo's own "not asking for an
    airtight parser" framing.
  - ``shlex.split`` failure (malformed/unterminated quoting) on a segment
    that already matched the cheap wrapper probe FAILS CLOSED (denies) --
    the opposite fail-direction from the rest of this guard's infra-failure
    posture, because an unparseable wrapper-shaped segment is itself the
    suspect signal, not an infra hiccup.

This hardening only ever engages AFTER the existing subagent-identity
resolution gate (EM main-loop calls remain unaffected) and only widens what
Layer 2 denies -- it never narrows the original ported deny ladder.

UNSCOPED-STASH GAP CLOSE (2026-07-26, this change): confirmed real, not a
deliberate carve-out -- the safe-forward allowlist gated bare `git stash`
and `git stash push` (with no pathspec) through to allow on BOTH the
anchored path (`subcmd == "stash": return None`, unconditional) and the
legacy path (`_STASH_WORD_RE.search(seg): return None`, unconditional).
Only `pop`/`apply` (`_STASH_POP_APPLY_RE`) and `drop`/`clear`
(`_STASH_DROP_CLEAR_RE`) were ever denied; the write-side verb itself was
open. This is the single most dangerous stash shape for a shared working
tree: it sweeps EVERY other live session's uncommitted changes into a
stash owned by one subagent, which may then die (crash, quota exhaustion)
holding it -- exactly what happened on 2026-07-26 (an executor stashed
mid-work, hit a quota limit, and died with the tree reverted and its own
work parked; it survived only because that particular stash happened to be
narrow). Fix, mirroring this file's existing verb-level option-gating idiom
(checkout/switch/branch's `remaining`-scoped flag checks): `git stash` /
`git stash push` (including the IMPLICIT-push flag-only form, e.g.
`git stash -u`) is now denied UNLESS a `--`-delimited pathspec follows,
scoping the stash to specific paths. `list`/`show` (read-only) and the
other named subcommands (`branch`/`create`/`store`/`save`, already unaffected
by this rule -- not the sweep-everything shape) are unchanged. `pop`/
`apply`/`drop`/`clear` are unaffected (already denied, unconditionally, by
the pre-existing checks above).

STASH-POP/DROP DENY-MESSAGE ALTERNATIVE (2026-07-29, this change): executor
doctrine's "prove the failure is pre-existing" recipe used to read
`git stash push -u -- <own paths>` / `git checkout $MB` / `git stash pop` --
the last step is unconditionally denied here (`_POP_APPLY_ACTION_RE`, no
override), so a subagent following the doctrine as written could never
finish it. Confirmed live: a subagent that got stuck this way recovered
by hand via `git show stash@{0}:<path>` + `cp`, and separately, ten
orphaned stash entries were found on this branch, most named
"executor ... probe/WIP" -- the same recipe, abandoned mid-recipe, over
and over. The fix is NOT a narrow-pop allowance: `pop`/`apply` always act
on a STACK POSITION (`stash@{0}` by default), which on a shared branch
with concurrent subagent dispatches has no reliable way to verify "this is
the entry I pushed, not a sibling's" -- there is no git-level identity
check available, only stack order, and stack order is exactly what a
second concurrent scoped-push perturbs. Instead, the doctrine recipe
itself is replaced (this repo's paired doctrine change; guard-side change
is message-only) with a per-file `git show <ref>:<path>` swap that never
touches the stash stack or does a whole-tree checkout at all: save the
edit with plain `cp`, overwrite with the pre-edit content via `git show`
(already on `_SAFE_GIT_SUBCOMMANDS`), run the test, restore with `cp`,
clean up with a bare (non-recursive, non-force) `rm` -- none of which this
guard's existing deny ladder touches, so no guard change was needed to
make the new recipe work. What DID change here is the `pop`/`apply` and
`drop`/`clear` deny messages: they used to fall through to the generic
catch-all reason with no forward path at all; they now name the no-stash
recipe explicitly (design-as-offers: "did you mean X", not a bare "no").
Deny-side behavior (pop/apply/drop/clear still hard-denied, unconditionally,
for subagents) is UNCHANGED -- see `test_git_stash_pop_still_denies_as_pop_
apply`/`test_git_stash_drop_still_denies_as_drop_clear` et al., still green.

SAFE-FORWARD OPTION-SURFACE HARDENING (2026-07-25, THIS change, P0 security
fix): the ANCHORED classifier's safe-forward allowlist
(``_evaluate_git_segment_anchored``) had the SAME defect
``block_reviewer_bash_outside_allowlist`` already fixed same-day in its own
Tier A (that module's Divergence 5): several branches resolved the real
argv SUBCOMMAND correctly, then granted (or denied) based on the subcommand
NAME alone, with NO inspection of the options that followed it. Confirmed
live against real git on this machine, all reaching the safe-forward
allowlist uninspected pre-fix:
  - ``git show --output=/tmp/pwn.py HEAD`` / ``git log --output=<path>`` --
    arbitrary file write (``show``/``log``/``diff``/... sit in
    ``_SAFE_GIT_SUBCOMMANDS``, which returned ``None`` unconditionally).
  - ``git checkout -f`` -- discards ALL uncommitted work; the pathspec/
    dashdash checkout deny only covered a strictly smaller clobber.
  - ``git branch -M main`` -- force-renames over an existing branch,
    destroying its ref (``-D``/``-d --force`` were gated; ``-M`` was not).
  - ``git merge -m "we prefer --ff-only merges" feat`` -- ``_FF_ONLY_RE``
    matched the flag NAME inside the quoted commit-message operand, so
    prose granted the allow for a real non-fast-forward merge.
  - ``git config alias.lg "log --get"`` -- ``_CONFIG_GET_RE`` matched
    ``--get`` inside the alias VALUE operand, so a config WRITE took the
    read-only allow path.
Fix, in two parts:
  1. ``find_git_diff_family_write_flag`` (shared with
     ``block_reviewer_bash_outside_allowlist`` via ``_helpers`` -- see that
     module's docstring item 3) now gates every ``_SAFE_GIT_SUBCOMMANDS``
     member on ``remaining`` (the argv slice AFTER the subcommand, already
     resolved by ``_real_git_subcommand`` for a different purpose) before
     returning the allow.
  2. ``_FF_ONLY_RE``/``_CONFIG_GET_RE`` free-text searches over ``seg`` are
     REPLACED, on the anchored path only, with an exact-token membership
     test against ``remaining`` (``"--ff-only" in remaining`` /
     ``"--get" in remaining``) -- NOT the brief-suggested
     ``dispatch_checks._seg_excluding_freetext_operands`` reuse (that
     helper only strips ``-m``/``--message``-flagged operands, which fixes
     the ``merge -m`` shape but not ``config``'s POSITIONAL value operand,
     which is preceded by no flag at all); the exact-token check against
     the argv this function already computed handles BOTH shapes uniformly
     with no new cross-module dependency, since shlex already collapses a
     quoted multi-word operand into ONE token that can never equal the
     literal flag token.
  3. New verb-level option gates, using the SAME ``remaining``-only
     scoping (never the whole-``seg`` text) so a pre-subcommand GLOBAL
     option sharing a letter (e.g. ``git -C <path> branch x``'s global
     ``-C``) cannot false-trip a subcommand-local check: ``checkout``/
     ``switch`` deny ``-f``/``--force``/``--discard-changes`` (discards
     uncommitted work) and ``-B``/``-C`` (force-creates/resets an existing
     branch, confirmed via ``git checkout -h``/``git switch -h``);
     ``branch`` additionally denies ``-M``/``-C`` (force move/copy even if
     the target exists, confirmed via ``git branch -h``) and ``-m``/``-c``
     combined with ``-f``/``--force`` (same effect); ``tag`` additionally
     denies ``-f``/``--force`` (replaces an existing tag ref, confirmed via
     ``git tag -h``). ``restore`` and ``stash`` were reviewed against their
     own ``-h`` output and found to have no analogous ungated write vector
     beyond what the existing staged/worktree and pop/apply/drop/clear
     gates already cover -- no change made there.
The LEGACY free-text classifier (``_evaluate_git_segment_legacy``) is
intentionally UNCHANGED -- per its own docstring, it is the "fall back to
today's behavior" path used only when ``shlex`` cannot tokenize a segment,
and none of the five confirmed exploit commands above hit that path (all
five tokenize cleanly).

COMMAND-POSITION GIT-TOKEN FIX (2026-07-28, this change): confirmed live --
a subagent's plain ``grep -n "git " file | head -50`` (the literal token
``git `` appearing only inside a double-quoted grep PATTERN, never as an
invoked command) was denied as "unrecognized git verb (default-deny)". Root
cause: `_git_subcommand_and_remaining_for_segment` searched ALL shlex tokens
in a segment for one whose basename equals ``git`` -- not just the
COMMAND-POSITION token -- so a "git" mention anywhere in the argv (a grep
pattern operand, a quoted regex alternation, later in the token list) was
as good as an invocation to the search. When no EXACT ``git`` token existed
(as here -- the quoted pattern tokenizes to one token, ``git ``, that is
never exactly equal to ``git``), the function returned ``parse_ok=False``,
which conflated "genuinely unparseable segment" with "confirmed this
segment does not invoke git" and routed BOTH to the free-text LEGACY
classifier. Legacy's own default-deny then fires on nothing more than the
segment containing the free-text word "git" (via the caller's ``\\bgit\\b``
gate in `_evaluate_git_surface`) -- so any subagent Bash call that merely
MENTIONS "git" as data (a grep/rg pattern, a quoted string, a filename like
`git-crypt`) and does not otherwise match one of legacy's enumerated verb
keywords fell through to its default-deny. This is the SAME "guard's
pattern matching something that merely resembles its target" class as the
guard/queue precedent this fix's commit message cites (see there for the
2026-06-30 drive-letter false-positive and the 2026-07-28 read-side-
matching-semantics lesson) -- the guard here was tested extensively on the
DENY side (five P0 exploit fixes, all with regression tests) and never
against a command that merely CONTAINS the word "git" without invoking it.

Fix, two parts:
  1. `_strip_leading_subshell_and_env` (new) peels a leading subshell-open
     `(` (both `( git ...` spaced and `(git ...` glued) and a leading `env`
     prefix off a segment's tokens, exposing the true COMMAND-POSITION head
     -- matching this module's own definition of "command position" (start
     of command, or immediately after a pipe/`&&`/`||`/`;`/subshell-open/
     `env`; pipe/`;`/`&`/`&&`/`||` are already segment boundaries by the
     time a segment reaches this code, handled upstream by
     `_evaluate_git_surface`'s own `;&|` split).
  2. `_git_subcommand_and_remaining_for_segment` now checks ONLY that
     command-position head (not every token in the segment) against `git`.
     When the head does not normalize to `git`, it returns the
     `_NOT_A_GIT_INVOCATION` sentinel with `parse_ok=True` -- a THIRD state
     distinct from both "bare `git` with no subcommand" (`subcmd is None`,
     unchanged default-deny behavior) and "genuinely unparseable/ambiguous"
     (`parse_ok=False`, unchanged legacy-fallback behavior).
     `_evaluate_git_segment` returns `None` (allow) outright on this
     sentinel, WITHOUT ever reaching the legacy classifier.

Deny-side is unaffected: every confirmed P0 exploit and worktree/remote/
stash fix above invokes `git` (or `env`/subshell-wrapped `git`) as its
actual command-position token, so all still resolve `base == "git"` and
route through the SAME anchored deny ladder as before. A genuine
subshell-wrapped destructive command (`(git push --force)`) is now caught
MORE precisely -- via the anchored path's specific "git push --force"
message -- rather than falling through to the legacy fallback's generic
match, which is a strict improvement, not a narrowing: `_strip_leading_
subshell_and_env` peels the subshell open so `git` still resolves to the
command-position head, and `_real_git_subcommand`/`_evaluate_git_segment_
anchored` classify the remainder exactly as they already do for an
unwrapped `git push --force`.

INDIRECTION-DENY MESSAGE HONESTY + HEREDOC/INLINE-`-c` OVER-BLOCK ASYMMETRY
(2026-08-06, this change -- B2/B3, state/audits/2026-08-06-architecture-
survey-analyst-friction-digest.md, ranked #2 and #3):

  B2. The indirection-deny message's "Safe forward paths:" block never
  named the one wrapper shape that actually works for a multi-statement
  payload -- writing it to a file under the session scratchpad and
  invoking `python3 <path>` (a bare python3-file invocation; see the
  "python/python3 without `-c`" comment above `_C_FLAG_INTERPRETERS`'s
  bare-file branch -- this shape is allowed unconditionally, content
  unexamined, by design). An analyst found this only by trial and error.
  Its first bullet instead read as forbidding that very form ("not via
  `bash <script>` / `sh -c` indirection", unqualified). Verified against
  this module's own classifier (not asserted): `python3 /tmp/x.py` ->
  `check()` returns `None`; `bash /tmp/x.sh` -> denied, "bash <file>
  (interpreter-invoked script -- indirection wrapper, script content
  unexamined)" (`_SHELL_FILE_INTERPRETERS` branch). The message now names
  `python3 <path>` explicitly as the sanctioned route and explicitly
  states `bash <path>`/`sh <path>`/`zsh <path>` do NOT work -- naming a
  path the guard would then block is the defect this fixes, not a
  hypothetical. The opening line's blanket "indirection is blocked for
  subagents" is also replaced (same fix): it overclaimed enforcement this
  guard does not perform, given the python3 carve-out.

  B3. The `python3 -c '<payload>'` over-block (the module's own prior
  language: "a deliberate over-block, not a false positive to work
  around") is bypassable by heredoc for the IDENTICAL logical payload:
  `python3 <<'EOF' ... EOF` reaches the classifier as a BARE python3
  invocation (no `-c` token -- heredoc redirection is not an argv flag),
  which is the SAME always-allowed shape B2 above now points subagents
  toward, and `_strip_heredoc_bodies` (see below) removes the heredoc BODY
  before either path is classified, so the destructive text inside is
  never a classification candidate in the heredoc form while the
  byte-identical text typed inline after `-c` is. Verified live: a
  `python3 -c "...subprocess.run(['git','push','--force',...])..."` denies
  (recurses into `_unwrap_and_classify`); the same call body fed via
  `python3 <<'EOF' ... EOF` returns `None` (allowed).

  This is recorded here as a KNOWN, NAMED divergence, not silently
  patched -- this codebase's "explicit named divergence so it cannot
  later be mistaken for a defect" idiom (see
  `search/tests/test_answer_differential.py::
  test_wc_count_agrees_but_padding_deliberately_diverges`). The finding's
  acceptance criterion explicitly permits either remedy -- a read-only
  allowlist inspecting the inline `-c` payload for read-only-ness, or
  ceasing to claim an enforcement this guard does not perform -- and the
  second is taken deliberately: parsing arbitrary Python source for
  read-only-ness is out of scope for a compensating text classifier, and
  blocking heredoc-fed interpreters outright would break the very
  scratch-script route B2 sanctions (`python3 <path>` is itself commonly
  authored via a `cat > path <<'EOF' ... EOF` write). The deny message's
  "deliberate over-block" claim is therefore narrowed to what is true: the
  INLINE `-c` form is refused because its payload is opaque to this text
  classifier, never that indirection at large is prevented -- a
  bare-file/heredoc-fed interpreter this guard can honestly decline to
  inspect (by design, not oversight) is the sanctioned route, and the
  message now names it as such instead of implying blanket coverage.

  Deny-side is unaffected by both: no allow->deny or deny->allow verdict
  changed on any existing test; this is a message-text and docstring
  change only, per the dispatching brief's explicit scope.

shell-doc-ok: the recipes quoted above are real shell commands a subagent
runs, and the deny-message section exists to name the exact spelling this
guard intercepts -- re-rendering them in prose would leave the guard's
behavior undocumented.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    _skip_wrapper_own_argv,
    exceeds_tokenizable_ceiling as _exceeds_tokenizable_ceiling,
    normalize_executable_basename as _normalize_executable_basename,
    segments_from_tokens_with_pipe_flag as _segments_from_tokens,
    tokenize_full_command as _tokenize_full_command,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    _strip_ps_quotes,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
    strip_powershell_prose_noise,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._helpers import (
    resolve_git_root,
    _read_backpointer_subagent_type,
    find_git_diff_family_write_flag,
    emit_kind_resolution_failure_signal,
)
from coordinator_core._settings_home import settings_home
from coordinator_core.write_guards.block_subagent_plan_body_write import (
    _resolve_subagent_identity,
)


# W3 FIX 1 (2026-07-15, security parity break): the shared _helpers.resolve_effective_types() re-exports
# subagent_sandbox.engine._canonical_agent_id, whose named-teammate leg returns the RAW
# `a<name>-<16hex>` agent_id instead of the bash-canonical `<name>@session-<short>` form.
# For NAMED-TEAMMATE dispatches that keys the wrong back-pointer dir, subagent_type
# resolves empty, and this guard FAILS OPEN where legacy bash DENIES. This module imports
# the ALREADY-CORRECT canonical resolver (write_guards.block_subagent_plan_body_write._resolve_subagent_identity)
# instead, per the same workaround that guard already uses -- see its own docstring.
CLASS = "hard-deny"

# Generator-provenance declaration (generator_provenance.py). This module's
# only real write is _log_fail_open, appending to
# settings_home()/state/destructive-guard-fail-open.log -- settings-home
# rooted, outside the tracked repo tree.
GENERATES = []
# Widened 2026-08-19 (C7, docs/plans/2026-08-19-the-held-guard-cohort-
# becomes-dialect-safe.md AC2/AC12) -- LAST of this cohort's flips,
# deliberately ordered after C3 (this module's own `_evaluate_powershell_
# destructive`, AC10) landed and its classifier tests + the AC5 anchored
# differential were re-run green immediately before this change. A
# dispatched subagent choosing the PowerShell tool instead of Bash
# previously bypassed this guard entirely (see
# docs/reference/guard-tool-name-membership.md section 3, now superseded by
# this flip). The prior held-Bash-only rationale was that the `_evaluate_
# legacy` free-text fallback (`if tokens is None: return _evaluate_legacy
# (cmd)`) was only verified closed for POSIX shapes, not PowerShell's
# here-string/backtick-escape shapes -- `_evaluate_powershell_destructive`
# (C3) is the dedicated PowerShell-dialect classifier that resolves that gap
# ahead of this widen, so `tokens is None` no longer routes a PowerShell
# payload through the POSIX-only legacy fallback.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 40

# ---------------------------------------------------------------------------
# Layer 1 -- dangerous-surface probes.
# re.MULTILINE so ^/$ anchor per-line, mirroring `grep -qE ... <<< "$VAR"`'s
# per-line evaluation of a possibly-multi-line herestring.
#
# BOUNDARY-CLASS WIDEN (2026-07-21 review, Findings 1 & 2): a real shell
# treats a bare word identically whether or not it is quoted (`'bash' x`
# runs `bash x`) and treats `$(`/a backtick/`(` immediately preceding a word
# as a command-substitution/subshell boundary (`$(git push --force)` runs
# `git push --force` for its side effect regardless of what the captured
# stdout is subsequently used for). The original class -- `[;&|\s]` only --
# missed both, so a quoted OR substitution-wrapped verb slipped past every
# Layer-1 surface probe (and the paired Layer-2 `_RM_DENY_RE`/recursive-flag
# probes) with zero identity-resolution cost. `_BOUNDARY_PRE`/`_BOUNDARY_POST`
# are shared by ALL boundary-anchored probes below -- git/rm/chmod/
# recursive-flag/rm-deny/wrapper -- not just the new wrapper probe, since
# they all share the exact same defect. This is a normalize-the-boundary-
# class fix, not a per-probe patch: fixing it once here fixes it everywhere
# the shared constants are used.
# ---------------------------------------------------------------------------
_QUOTE_OPEN_CHARS = "'\"`("
_QUOTE_CLOSE_CHARS = "'\"`)"
_BOUNDARY_PRE = r"(?:^|[;&|\s" + re.escape(_QUOTE_OPEN_CHARS) + r"])"
_BOUNDARY_POST = r"(?:[;&|\s" + re.escape(_QUOTE_CLOSE_CHARS) + r"]|$)"

_GIT_SURFACE_RE = re.compile(_BOUNDARY_PRE + r"git" + _BOUNDARY_POST, re.MULTILINE)
_RM_SURFACE_RE = re.compile(_BOUNDARY_PRE + r"rm" + _BOUNDARY_POST, re.MULTILINE)
_CHMOD_CHOWN_RE = re.compile(
    _BOUNDARY_PRE + r"(?:chmod|chown)" + _BOUNDARY_POST, re.MULTILINE
)
_RECURSIVE_FLAG_RE = re.compile(
    _BOUNDARY_PRE + r"-[a-zA-Z]*[Rr][a-zA-Z]*" + _BOUNDARY_POST + r"|--recursive",
    re.MULTILINE,
)

# rm layer-2 deny (reference hook line 456) -- evaluated against the FULL
# CMD_NORM, NOT per-segment (deliberate divergence from the git surface).
_RM_DENY_RE = re.compile(
    _BOUNDARY_PRE + r"rm(?:\s+-[a-zA-Z]*[rRfF][a-zA-Z]*|\s+--recursive|\s+--force)",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Indirection-wrapper probe (2026-07-21 addition -- see module docstring
# "INDIRECTION-WRAPPER HARDENING"). Cheap, boundary-anchored, same style as
# the git/rm/chmod probes above: catches bash/sh/zsh/python[3]/env/xargs
# invoked as a bare word, so Layer 1 can gate identity-resolution cost on
# EITHER a destructive-verb surface OR a wrapper surface. The
# `python3?(?:\.\d+)*` branch also catches versioned interpreter binaries
# (`python3.11`, `python3.12.1`, ...) at the raw-text gate (Finding 4,
# 2026-07-21 review) -- see `_normalize_interpreter_basename` below for the
# matching normalization on the tokenized path.
# ---------------------------------------------------------------------------
_WRAPPER_PROBE_RE = re.compile(
    _BOUNDARY_PRE + r"(?:bash|sh|zsh|python3?(?:\.\d+)*|env|xargs)" + _BOUNDARY_POST,
    re.MULTILINE,
)

#: Interpreters whose `-c <string>` shape this guard reliably unwraps and
#: recurse-matches (memo's explicit examples: `sh -c '...'`, `python -c
#: '...'`).
_C_FLAG_INTERPRETERS = frozenset({"bash", "sh", "zsh", "python", "python3"})

#: Interpreters whose BARE `<file>` argv shape (no `-c`) is denied outright
#: -- memo's enumerated `bash|sh|zsh <file>` bullet ONLY. Deliberately does
#: NOT include python/python3: `python3 -m pytest`, `python3 script.py
#: --flag` etc. are common, entirely legitimate subagent invocations with no
#: memo-cited bypass shape behind them; over-blocking those was a real
#: regression caught while smoke-testing this guard against its own repo's
#: pytest invocation (2026-07-21). Only shell interpreters get the
#: outright-deny-on-bare-file treatment; python's ONLY denied shape is `-c`.
_SHELL_FILE_INTERPRETERS = frozenset({"bash", "sh", "zsh"})

#: Depth cap against pathological/adversarial nesting (`env env env ...`).
#: A legitimate subagent command never nests this deep; this is a fail-closed
#: backstop, not a claim of completeness.
_MAX_INDIRECTION_DEPTH = 4

#: Bundled-or-standalone `-c` short flag, e.g. `-c`, `-ic`, `-ci` (Finding 3,
#: 2026-07-21 review): Python's CLI parser accepts bundled short flags, so
#: `python3 -ic '<payload>'` behaves as `python3 -i -c '<payload>'` -- an
#: exact `tokens[1] == "-c"` check misses this entirely.
_BUNDLED_C_FLAG_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")

#: Versioned python interpreter basename (`python3.11`, `python3.12.1`, ...)
#: normalized to its unversioned form (`python3`) for the
#: `_C_FLAG_INTERPRETERS`/`_SHELL_FILE_INTERPRETERS` membership checks below
#: -- Finding 4, 2026-07-21 review.
_PYTHON_VERSION_SUFFIX_RE = re.compile(r"^(python3?)(?:\.\d+)+$")

#: `.`/`source` builtins execute a script IN-PROCESS -- same blast radius as
#: `bash <file>` (2026-07-21 security-audit finding #5).
_SOURCE_VERBS = frozenset({".", "source"})

# ---------------------------------------------------------------------------
# MACHINE-LOCAL REGISTRY WRITE DENY (2026-08-03, this change, DR-125
# implementation -- docs/decisions/DR-125-subagent-bash-confinement-two-
# classes.md, DoE-claude). Confirmed live (code-reviewer Finding 3,
# narrow-subagent-commit-confinement-two-classes.md chunk C2 review): once
# `coordinator:executor` left `_helpers._CONFINED_FINDINGS_AGENTS`,
# `machine-local set/array-append/array-set/migrate-publish-mirrors`
# reachability depended SOLELY on that allowlist-confinement guard -- no
# guard in `bash_guards/` or `write_guards/` named `machine-local` at all.
# `machine-local set` writes the `repos.*`/`publish.*` TOML registry that
# OTHER trusted tooling (cross-repo-memo path resolution,
# concrete-path-citation remediation text) reads as ground truth for
# absolute-path resolution -- poisoning it redirects those consumers at an
# attacker-chosen path. DR-125 Ruling 1 already scopes the confined
# perimeter to MACHINE-DEGRADING actions and commits; corrupting a registry
# other tooling trusts for path resolution is machine-degrading, so this is
# implementing that ruling, not a new PM decision.
#
# Keyed on COMMAND SHAPE for EVERY resolved subagent type (not on
# `_CONFINED_FINDINGS_AGENTS` membership) -- deliberately stronger than the
# pre-C2 state, where the executor was denied only incidentally via
# allowlist confinement, and consistent with how this module already
# handles force-push/stash/reset: the harm lives in the command, not in
# who ran it.
#
# Subcommand list verified against the real CLI
# (`<settings-home>/bin/_machine_local.py`'s `main()` subparser dispatch
# table), not guessed from a partial list: `get`/`has`/`keys`/`path`/`dir`
# are READ-ONLY (how a subagent legitimately resolves a sibling-repo path)
# and MUST stay reachable; `set`/`array-append`/`array-set`/
# `migrate-publish-mirrors` are the only members that mutate the on-disk
# registry.
#
# Reuses this module's existing segmentation/normalization machinery --
# `_evaluate_tokenized`'s per-segment `norm_head` resolution already
# tolerates a path-prefixed spelling (`/usr/local/bin/machine-local`) and a
# Windows `.cmd` twin via `_normalize_executable_basename`, `-c`/`env`
# interpreter-wrapping via that same function's existing recursive-unwrap
# branches, and `;`/`&&`/`||`/`|` segmentation via `_segments_from_tokens`
# -- no new matcher/segmenter is authored here. An unparseable command
# already fails CLOSED via the existing `tokens is None` branch (denies as
# a wrapper-indirection surface for any resolved subagent), so no separate
# fail-closed path is needed for this addition either.
# ---------------------------------------------------------------------------
_MACHINE_LOCAL_WRITE_SUBCOMMANDS = frozenset(
    {"set", "array-append", "array-set", "migrate-publish-mirrors"}
)

#: An `argv[0]`-position token that is an unexpanded shell variable reference
#: or command substitution (`$V`, `${V}`, `$(...)`) -- 2026-07-21 security-
#: audit finding #3. The guard does NOT attempt to statically resolve the
#: referenced value (that would require executing the shell); it denies
#: outright instead.
_ARGV0_UNRESOLVED_RE = re.compile(r"^\$")

# ---------------------------------------------------------------------------
# SETTINGS-HOME BIN NARROW EXEMPTION (2026-07-27 addition, authorized by PM
# ruling -- this is a doctrine/guard-inconsistency fix, not a security
# loosening). `coordinator/CLAUDE.md` mandates every skill/command/agent
# fence invoke a settings-home CLI as literally
# ``"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<cli>"``
# -- an unresolved-`$VAR`-in-argv0 shape that `_ARGV0_UNRESOLVED_RE` above
# denies outright, so a subagent that follows the doctrine it was handed gets
# blocked by this guard. Mirrors the sibling guard
# `block_reviewer_bash_outside_allowlist._first_token_is_allowlisted_binary`'s
# reasoning: the BINARY NAME is what matters for identity, and here it is
# statically known (the trailing `/bin/<name>` path segment) even though the
# directory prefix is an unresolved env var -- so this is narrowing, not
# defeating, the "don't statically resolve an unresolved value" rule.
#
# Narrow by construction (each clause closes one bypass a broader match
# would open):
#   - the var name must be EXACTLY `COORDINATOR_SETTINGS_HOME` (any other
#     variable, e.g. `$FOO/bin/x`, is NOT exempt and still denies);
#   - a `:-` default is permitted ONLY with the documented literal default
#     `$HOME/.coordinator-claude-settings` / `${HOME}/.coordinator-claude-settings`
#     -- `${COORDINATOR_SETTINGS_HOME:-/tmp/evil}/bin/x` is an
#     attacker-choosable command via the `:-` fallback and is NOT exempt;
#   - the tail after the resolved prefix must be exactly one `/bin/<name>`
#     path segment with no further `/` -- blocks path traversal
#     (`/bin/../../evil`) and blocks embedding a second, unresolved
#     expansion in the tail;
#   - `<name>` is restricted to `[A-Za-z0-9_.-]+` -- no shell metacharacters,
#     no command substitution, no further `$`/backtick.
# Any command-substitution shape (`$(...)`, backticks) anywhere in argv0
# still fails to match this pattern (it requires the literal `$VAR`/`${VAR}`
# token forms only) and falls through to the existing unresolved-argv0 deny.
_SETTINGS_HOME_DEFAULT_RE = r"\$(?:HOME|\{HOME\})/\.coordinator-claude-settings"
_SETTINGS_HOME_BIN_EXEMPT_RE = re.compile(
    r"^\$(?:"
    r"COORDINATOR_SETTINGS_HOME"
    r"|\{COORDINATOR_SETTINGS_HOME\}"
    r"|\{COORDINATOR_SETTINGS_HOME:-" + _SETTINGS_HOME_DEFAULT_RE + r"\}"
    r")/bin/[A-Za-z0-9_.-]+$"
)


def _is_settings_home_bin_argv0(argv0: str) -> bool:
    """Return ``True`` iff ``argv0`` is the doctrine-mandated settings-home
    CLI invocation form (bare `$COORDINATOR_SETTINGS_HOME`,
    `${COORDINATOR_SETTINGS_HOME}`, or `${COORDINATOR_SETTINGS_HOME:-...}`
    with the documented literal default) followed by exactly one
    `/bin/<name>` segment. See the module comment above
    `_SETTINGS_HOME_BIN_EXEMPT_RE` for why this narrow shape is safe to
    exempt from the unresolved-argv0 deny.
    """
    return bool(_SETTINGS_HOME_BIN_EXEMPT_RE.match(argv0))


def _normalize_interpreter_basename(name: str) -> str:
    """Strip a trailing dotted-version suffix from a python interpreter
    basename (`python3.11` -> `python3`, `python3.12.1` -> `python3`) so
    versioned interpreter binaries are recognized identically to the bare
    `python3` form. Non-python names, and bare `python`/`python3` with no
    suffix, pass through unchanged.
    """
    m = _PYTHON_VERSION_SUFFIX_RE.match(name)
    return m.group(1) if m else name


# ---------------------------------------------------------------------------
# INVOCATION-HEAD NORMALIZATION (2026-07-25 fix -- closes a gap found during
# the 2026-07-21 verb-anchoring review; case-folding added 2026-07-28,
# Finding 1). `_normalize_executable_basename` is EXACT-basename
# normalization, never substring matching: `gitk`, `git-foo`, and `mygit`
# normalize to themselves unchanged and never equal `git` -- only a
# basename that IS `git`/`git.exe`/`GIT.EXE`/... (modulo suffix/case)
# normalizes to `git`. This is the same "identify the actual operation,
# never resolve uncertainty to ALLOW" theme as the 2026-07-21
# boundary-anchor widen.
#
# 2026-07-29: moved to `_command_tokenizer.normalize_executable_basename`
# (imported above, re-exported under this module's prior private name) --
# this was previously a duplicate-maintained copy alongside
# `block_subagent_commit.py`'s own, which had silently drifted (missing the
# case-fold step). See `_command_tokenizer.py`'s module docstring for the
# consolidation rationale and `tests/test_shared_command_tokenizer_contract.py`
# for the contract that now pins this.
# ---------------------------------------------------------------------------


#: Matches an argv0-shaped raw token at a genuine invocation-HEAD position
#: only -- the very start of the command, or immediately following a
#: `;`/`&`/`|`/newline segment separator (each segment's own argv0) -- so a
#: Windows backslash path can be identified BEFORE any shlex tokenization
#: runs. UNLIKE the shared `_BOUNDARY_PRE` used by the Layer-1 surface
#: probes above, this deliberately does NOT treat plain whitespace or a bare
#: quote char as a boundary on its own (2026-07-25 review fix, P2(a)): doing
#: so let this pre-pass rewrite ANY word in the command whose basename
#: normalized to `git` -- including a mention several arguments deep, e.g.
#: the commit-message path in `git commit -m "see C:\notes\git.exe for
#: details"` -- which is scope creep against this function's own
#: docstring/name ("Rewrite a Windows path token", singular, positional).
#: The optional leading whitespace/quote-open char after the separator is
#: still consumed so a quoted or indented argv0 (`  "C:\...git.exe" push`)
#: is still recognized -- only the POSITION that can start a match narrowed,
#: not the shape of what it matches there.
_ARGV0_HEAD_BOUNDARY_PRE = (
    r"(?:\A|[;&|\n])\s*(?:[" + re.escape(_QUOTE_OPEN_CHARS) + r"])?"
)
_RAW_HEAD_TOKEN_RE = re.compile(r"(" + _ARGV0_HEAD_BOUNDARY_PRE + r")([^\s;&|]+)")


#: Basenames whose Windows backslash-path argv0 form must be rewritten to
#: forward-slash BEFORE shlex tokenization runs (see
#: `_normalize_windows_wrapper_argv0` docstring). `git` was the original
#: (2026-07-25) member; widened here (A2 fix) to every basename
#: `_WRAPPER_PROBE_RE` itself recognizes as an indirection-wrapper
#: interpreter -- `C:\Windows\System32\bash.exe -c '<payload>'` and
#: `C:\Python311\python.exe -c '<payload>'` are the memo's own explicit
#: bypass examples, and both die at the SAME shlex-eats-backslashes root
#: cause `git.exe` did. `python3` stands in for the whole
#: `_PYTHON_VERSION_SUFFIX_RE` family (`python3.11`, ...) via
#: `_normalize_interpreter_basename` in the membership check below.
_WINDOWS_ARGV0_NORMALIZE_BASENAMES = frozenset(
    {"git", "bash", "sh", "zsh", "python", "python3", "env", "xargs"}
)


def _normalize_windows_wrapper_argv0(cmd: str) -> str:
    """Rewrite a `\\`-separated Windows path token, AT ARGV0 POSITION ONLY
    (the head of the command or of a `;`/`&`/`|`/newline-separated segment),
    whose basename normalizes to a member of
    `_WINDOWS_ARGV0_NORMALIZE_BASENAMES` (`C:\\path\\to\\git.exe`,
    `C:\\Windows\\System32\\bash.exe`, `C:\\Python311\\python.exe`, ...)
    into its forward-slash equivalent, BEFORE any shlex tokenization runs.

    POSIX shlex -- which this module RELIES ON elsewhere to unescape a
    backslash-obfuscated verb (`r\\m` -> `rm`, Finding #2, 2026-07-21) --
    treats a bare backslash as an escape character and silently drops it,
    which would otherwise mangle `C:\\path\\to\\bash.exe` into
    `C:pathtobash.exe` before `_normalize_executable_basename` ever runs,
    destroying the very backslash this pass needs to see AND collapsing the
    basename so no separator survives to split on -- the exact A2 gap: the
    Layer-1 `_WRAPPER_PROBE_RE`/tokenized-pass basename check never fires,
    so a Windows-spelled `bash.exe -c`/`python.exe -c` indirection sails
    through undetected. Disabling shlex's escape processing globally to
    preserve backslashes is NOT the fix here -- that would silently re-open
    the Finding #2 bypass class (`r\\m` would stay literal `r\\m` instead of
    unescaping to `rm`). Scoped instead to a raw, pre-tokenization ARGV0
    token whose basename normalizes to EXACTLY one of the recognized
    interpreter/git basenames: an unrelated backslash elsewhere in the
    command (a real backslash-obfuscated `rm`, ordinary prose, a commit
    message mentioning a git.exe path) is untouched -- both because
    normalizing requires the token's basename to actually BE a recognized
    name (modulo suffix/case/version), which `r\\m` and prose never are, AND
    because `_ARGV0_HEAD_BOUNDARY_PRE` never treats a mid-segment word as a
    candidate match position at all (2026-07-25 review fix, P2(a)).
    """

    def _rewrite(m: "re.Match[str]") -> str:
        boundary, token = m.group(1), m.group(2)
        if "\\" in token:
            base = _normalize_interpreter_basename(_normalize_executable_basename(token))
            if base in _WINDOWS_ARGV0_NORMALIZE_BASENAMES:
                token = token.replace("\\", "/")
        return boundary + token

    return _RAW_HEAD_TOKEN_RE.sub(_rewrite, cmd)


# ---------------------------------------------------------------------------
# SPACED-WINDOWS-PATH ARGV0 HOLE (2026-07-28, this change).
# `_RAW_HEAD_TOKEN_RE` above stops its raw-token capture at the FIRST
# WHITESPACE (`[^\s;&|]+`), so an argv0 head whose Windows path contains a
# space -- `C:\Program Files\Git\bin\git.exe`, git-for-Windows' DEFAULT
# install location, not an exotic edge case -- is only ever PARTIALLY
# captured (`C:\Program`), never recognized as a member of
# `_WINDOWS_ARGV0_NORMALIZE_BASENAMES`, and left completely unrewritten.
# Downstream `shlex.split` then does one of two things, NEITHER of which
# lands "git"/"bash"/... at argv0 position:
#   - backslash-separated (`C:\Program Files\Git\bin\git.exe`): POSIX shlex
#     eats each backslash as an escape character, mangling the path into
#     TWO garbage tokens (`C:Program`, `FilesGitbingit.exe`) with no
#     recognizable basename;
#   - forward-slash-separated (`C:/Program Files/Git/bin/git.exe`): no
#     escape-eating, but the UNQUOTED embedded space is still a real shlex
#     word-boundary, so the path splits into `C:/Program` (argv0) and
#     `Files/Git/bin/git.exe` (argument) regardless -- this module's
#     `_evaluate_tokenized`/`_git_subcommand_and_remaining_for_segment`
#     check ONLY `seg_tokens[0]`/`working[0]` (an argv0-POSITION identity
#     check, not a position-independent scan), so the correctly-suffixed
#     second fragment is never inspected as a candidate executable at all.
# So THIS GUARD evaluates the mangled tokens and sees no `git` at argv0,
# hence does not itself deny.
#
# RECONCILED 2026-07-29 (code-reviewer Finding 1, cross-guard contradiction
# with `block_subagent_commit.py`'s sibling analysis of the identical
# shape) -- correction to the framing above, not to the fix: this was
# originally labelled a "P0 security fix" on the premise that the guard's
# own silent-allow verdict means the underlying `git rebase`/`git push`
# ACTUALLY EXECUTES on a real Windows box -- i.e. that some invocation path
# resolves via raw `CreateProcess` successive-space-delimited-prefix
# resolution once this guard fails to deny. That premise does not hold for
# this project's actual harness: the Bash tool's execution shell on
# Windows is Git Bash / MSYS bash (see `docs/wiki/bash-on-windows-
# gotchas.md`, DoE-claude) -- a genuine POSIX shell whose own `exec` does
# real word-splitting/backslash-escape processing BEFORE resolving an
# executable, and hands `CreateProcess` an already-resolved, already-quoted
# application name (never a raw, un-split command line with
# `lpApplicationName=NULL`, which is the specific precondition the
# successive-prefix hazard requires). So the SAME unquoted spaced path that
# mangles into garbage tokens for THIS GUARD'S classifier also mangles into
# garbage tokens for the REAL invoking shell -- the underlying `git`
# invocation itself fails to execute, independent of whether this guard
# denies it. There is no confirmed live exploit chain behind this fix on
# this harness's actual execution model; treat it as defense-in-depth
# classifier hardening (fail-closed on an ambiguous/malformed argv0 shape,
# and correctness for a security-relevant guard) rather than a fix for a
# demonstrated bypass. The fix itself is unchanged and still worth keeping
# for that reason -- ported into `block_subagent_commit.py` on the same
# reconciled basis (see that module's 2026-07-29-part-3 docstring entry).
#
# A REFERENCE FIX for exactly this argv0-head-path shape already exists in
# `dispatch_checks._normalize_windows_git_path_head`/
# `_WINDOWS_GIT_PATH_HEAD_RE` (committed `6bb7a8c4`) -- it captures the
# WHOLE head-position path INCLUDING embedded-space components via a
# non-greedy `(?:[^\\/\r\n]+?[\\/])*` component walk (space is not excluded
# from that character class, only the path separators and newlines are), so
# "Program Files" survives as part of ONE captured path instead of being a
# tokenizer boundary. NOT imported here: `dispatch_checks.py` already
# imports `_normalize_executable_basename` FROM this module (see this
# module's own W3-FIX-1 comment block near the top and this file's import
# block) -- a reverse import would be a direct two-module circular import,
# confirmed by inspection, not assumed. Mirrored instead, and widened from
# `git`-only to every basename `_WINDOWS_ARGV0_NORMALIZE_BASENAMES`
# recognizes (the reference function only ever needed `git`, since its
# caller `_command_really_invokes` is git-specific; this module's argv0
# hardening already covers bash/sh/zsh/python[3]/env/xargs for the
# no-space case via `_normalize_windows_wrapper_argv0` above, so the same
# widening applies here for consistency).
#
# Mirroring alone (bare backslash->forward-slash conversion, matching the
# reference function's own `_rewrite` byte-for-byte) is NOT sufficient for
# THIS module, though it is sufficient for the reference's own caller.
# `dispatch_checks._command_really_invokes` deliberately tolerates the
# residual unquoted-space split after conversion (its own docstring: "that
# is harmless here... the second fragment's OWN basename is still `git.exe`")
# because IT scans every token in the split for a recognized basename,
# position-independent. THIS module's argv0-head checks are POSITIONAL
# (`seg_tokens[0]`/`working[0]` only, by design -- see `_evaluate_tokenized`
# module comment "IDENTIFIES ... at each segment's argv[0] TOKEN position,
# never a raw-substring match"), so a bare separator-only rewrite would
# still leave the corrected path split into TWO tokens by the embedded
# space and still miss it at argv0. The rewrite below additionally
# single-quotes an UNQUOTED path that contains whitespace, so the whole
# space-containing path lands as ONE shlex token at argv0 -- an ALREADY
# double/single-quoted path (`"C:\Program Files\...\git.exe" push`) is left
# with its existing quoting, since POSIX shlex already keeps a quoted
# multi-word span as one token (confirmed: the pre-existing
# `test_windows_git_exe_quoted_double_with_space_denies` test passes today,
# unaffected by this whole gap, precisely because the caller already
# quoted it).
# ---------------------------------------------------------------------------
#: `[\\/]{1,2}` (not a single `[\\/]`) -- Review: code-reviewer, Finding 4
#: (P2, 2026-07-28): a UNC path (`\\server\share\Git\bin\git.exe`) opens
#: with TWO leading backslashes, not one. The original single-separator
#: anchor could consume only the first, and the component-repetition group
#: (`[^\\/\r\n]+?[\\/]`, which requires >=1 non-separator char before its
#: own separator) cannot consume a SECOND separator immediately following
#: the first with zero characters between them -- so the whole match failed
#: at the anchor position and a spaced UNC path fell through unrewritten to
#: the older pass, which itself does not handle an embedded space either
#: (see module comment above `_normalize_windows_argv0_head_path_with_spaces`
#: for why a bare separator-only rewrite is insufficient for spaced paths).
#: Widening the anchor to accept ONE OR TWO leading separators closes this
#: without opening anything new: a drive-letter or single-rooted path still
#: has exactly one separator present to match (the `{1,2}` quantifier is
#: greedy but there is nothing more to consume), so `C:\Program Files\...`
#: and `/usr/bin/git` are unaffected; only a genuine two-separator UNC/
#: double-slash head now also matches.
_WINDOWS_ARGV0_HEAD_PATH_RE = re.compile(
    r"(?P<sep>\A|[;&|\n])(?P<ws>\s*)(?P<q>[\"']?)"
    r"(?P<path>(?:[A-Za-z]:)?[\\/]{1,2}(?:[^\\/\r\n]+?[\\/])*"
    r"(?:git|bash|sh|zsh|python3?(?:\.\d+)*|env|xargs)(?:\.exe)?)"
    r"(?P=q)(?=[\s;&|]|\Z)",
    re.IGNORECASE,
)


def _normalize_windows_argv0_head_path_with_spaces(cmd: str) -> str:
    """Rewrite a Windows absolute (drive-letter- or root-rooted) argv0-head
    path -- `\\`- or `/`-separated, possibly containing embedded-space
    components (`C:\\Program Files\\Git\\bin\\git.exe`), whose basename
    normalizes to a member of `_WINDOWS_ARGV0_NORMALIZE_BASENAMES` -- into
    its forward-slash equivalent, additionally wrapping it in single quotes
    when it was unquoted AND contains whitespace, so the whole path lands
    as ONE `shlex` token at argv0 position instead of splitting on the
    embedded space. See the module comment above `_WINDOWS_ARGV0_HEAD_PATH_RE`
    for why quoting (not just separator conversion) is required here.

    Runs BEFORE `_normalize_windows_wrapper_argv0` (see `check()`): once a
    matched path is quoted here, it no longer contains a bare `\\` outside
    the quotes, so the older pass's `if "\\" in token` gate finds nothing
    left to do for it and is unaffected -- this is a strict widening, not a
    replacement, of the older pass's coverage. A spaced UNC path
    (`\\\\server\\share\\Git\\bin\\git.exe`) is now covered too (Finding 4,
    2026-07-28 fix -- see `_WINDOWS_ARGV0_HEAD_PATH_RE`'s own comment for
    why the anchor needed widening from one leading separator to one-or-two).
    A bare RELATIVE path with no leading separator at all (`Git\\bin\\git.exe`,
    no drive letter, no root) is NOT covered -- this regex is deliberately
    rooted-path-anchored only, matching this function's own name/docstring
    ("absolute (drive-letter- or root-rooted)"); it still falls through
    unchanged to the older pass exactly as before.
    """

    def _rewrite(m: "re.Match[str]") -> str:
        sep, ws, q, path = m.group("sep"), m.group("ws"), m.group("q"), m.group("path")
        normalized = path.replace("\\", "/")
        if not q and re.search(r"\s", normalized):
            quoted = normalized.replace("'", "'\\''")
            return sep + ws + "'" + quoted + "'"
        return sep + ws + q + normalized + q

    return _WINDOWS_ARGV0_HEAD_PATH_RE.sub(_rewrite, cmd)


#: Exact `-n`/`--noexec` flag tokens (bug fix, 2026-07-21: state/bug-backlog/
#: 2026-07-21-subagent-guard-false-positive-bash-n-syn-5ef6ef52e2f9.yaml).
#: `-n`/`--noexec` puts bash/sh/zsh into READ-BUT-DO-NOT-EXECUTE mode -- the
#: shell parses the target for syntax errors and exits WITHOUT running a
#: single line of it, so `bash -n <script>` is safe regardless of what the
#: script contains. Matched as an EXACT standalone token only (never a
#: bundled short flag, never a substring of a filename): `bash weird-n-
#: name.sh` tokenizes to one `weird-n-name.sh` token that is never equal to
#: `-n`, so a filename cannot collide with this allowlist.
_NOEXEC_FLAG_TOKENS = frozenset({"-n", "--noexec"})


def _has_noexec_flag_before_script(interpreter_args: List[str]) -> bool:
    """True if `-n`/`--noexec` appears among the shell's OWN option tokens,
    before the script-path argument, in `interpreter_args` (the tokens AFTER
    the interpreter name -- e.g. `["-n", "foo.sh"]` for `bash -n foo.sh`).

    Only scans up to the first non-option token (or a literal `--`
    end-of-options marker): bash stops parsing its OWN options at the first
    argument that doesn't start with `-`, and everything from that point on
    becomes `$1`, `$2`, ... INSIDE the invoked script, not a flag governing
    whether bash executes it. `bash foo.sh -n` therefore does NOT match --
    `-n` there is a positional argument fed TO the script (which still runs
    normally); scanning past the script token would turn this allowlist
    into a trivial bypass (`bash malicious.sh -n` slipping through denial).
    """
    for tok in interpreter_args:
        if tok == "--":
            break
        if tok in _NOEXEC_FLAG_TOKENS:
            return True
        if tok.startswith("-"):
            continue
        break
    return False


def _strip_env_prefix(tokens: List[str]) -> List[str]:
    """Strip a leading ``env`` invocation down to the real command tokens.

    Approximate on purpose (does not consume a `-u NAME`/`-C DIR`-style
    flag's operand as part of the flag) -- this guard is a compensating
    control, not an airtight ``env(1)`` parser (memo: "I am not asking you
    to make the parser airtight"). Returns ``tokens`` unchanged if the first
    token is not literally ``env``; returns ``[]`` if nothing follows the
    stripped prefix.
    """
    if not tokens or tokens[0] != "env":
        return tokens
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.startswith("-"):
            i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        break
    return tokens[i:]


def _unwrap_and_classify(payload: str, depth: int) -> Optional[str]:
    """Apply the SAME git/rm/chmod-R matchers Layer 1/2 use to an unwrapped
    indirection payload (a `-c` string, or an `env`-stripped remainder),
    then recurse into any further indirection shape nested inside it.
    Returns a deny_kind label, or ``None`` if the payload is clean.

    ``strict=False`` on the `_evaluate_git_surface` call below (COMMAND-
    POSITION GIT-TOKEN FIX, 2026-07-28): this payload is NOT guaranteed to be
    real shell syntax -- it is the unwrapped body of an interpreter `-c`
    string, most often actual PYTHON SOURCE (`python -c 'subprocess.run(
    ["git", "push", "--force"])'` is the memo's own bypass example), so a
    shlex-derived "command position" is not a meaningful concept here and
    must NOT be used to allow-outright on a non-git-shaped head token.
    Confirmed live: `subprocess.run(["git", "push", "--force"])` shlex-
    tokenizes (as if it were shell text) to a glued, non-command-shaped
    token list (`['subprocess.run([git,', 'push,', '--force])']`) that
    strict mode would read as "confirmed not a git invocation" and allow --
    exactly the payload the indirection-wrapper hardening exists to catch.
    Non-strict mode preserves the ORIGINAL (pre-fix) full-token-scan
    behavior for this recursive payload-scan context only: a genuine shell
    command reaching `_evaluate_git_surface` from `check()`'s top-level
    `cmd_norm` still gets the strict, command-position-aware fix.
    """
    if _GIT_WORD_RE.search(payload):
        verdict = _evaluate_git_surface(payload, strict=False)
        if verdict is not None:
            return verdict
    if _RM_SURFACE_RE.search(payload) and _RM_DENY_RE.search(payload):
        return "rm -r/-f (recursive or force)"
    if _CHMOD_CHOWN_RE.search(payload) and _RECURSIVE_FLAG_RE.search(payload):
        return "chmod/chown -R (recursive)"
    return _evaluate_wrapper_indirection(payload, depth)


def _evaluate_wrapper_indirection(cmd_text: str, depth: int = 0) -> Optional[str]:
    """Subagent-scoped indirection-wrapper pass (2026-07-21 addition).

    Per-segment (quote-aware `;`/`&`/`|` split via `_tokenize_full_command`/
    `_segments_from_tokens`, same as the tokenized authoritative pass --
    2026-07-26 fix, see inline comment below): classifies the wrapper shape.
    Returns a deny_kind label describing the FIRST denying segment, or
    ``None`` if no segment denies.
    """
    if depth > _MAX_INDIRECTION_DEPTH:
        return "indirection nesting too deep (fails closed)"

    # Quote-aware segmentation (2026-07-26 fix): a naive `re.split(r"[;&|\n]+",
    # cmd_text)` split on the raw text -- as this function did until now --
    # breaks a `;`/`&`/`|` that is INSIDE a quoted argument value (e.g.
    # `--title "some title; with a semicolon"`) into two bogus segments, one
    # of which is a dangling unterminated-quote fragment. That fragment still
    # matched `_WRAPPER_PROBE_RE` (it retained the `python3`/`bash`/etc.
    # token) and then failed `shlex.split`, tripping the fail-closed
    # "unparseable indirection wrapper" branch on a perfectly ordinary quoted
    # value -- a false positive, not a real indirection attempt. Reuse the
    # same punctuation-aware tokenizer the authoritative tokenized pass uses
    # (`_tokenize_full_command`/`_segments_from_tokens`): it treats an
    # unquoted `;`/`&`/`|` as a real separator but keeps a quoted one attached
    # to its word, so a genuine command-chaining `;` outside quotes still
    # yields a separate segment (and still denies below) while one embedded
    # in a quoted value does not.
    all_tokens = _tokenize_full_command(cmd_text)
    if all_tokens is None:
        # Whole command is unparseable (unterminated quote / trailing
        # backslash) -- fail CLOSED, same posture as before, just evaluated
        # once over the full command instead of per naively-split segment.
        return "unparseable indirection wrapper (fails closed)"

    for tokens, _pipe_before in _segments_from_tokens(all_tokens):
        if not tokens:
            continue
        seg = " ".join(shlex.quote(t) for t in tokens)
        if not _WRAPPER_PROBE_RE.search(seg):
            continue

        was_env_wrapped = tokens[0] == "env"
        tokens = _strip_env_prefix(tokens)
        if not tokens:
            continue

        head_base = _normalize_executable_basename(tokens[0])
        head_base = _normalize_interpreter_basename(head_base)

        if head_base == "xargs":
            return "xargs <cmd> (command assembled from stdin -- indirection wrapper)"

        if head_base in _C_FLAG_INTERPRETERS:
            # Bug fix (2026-07-21): a parse-only `-n`/`--noexec` syntax check
            # executes NOTHING, so it is safe regardless of the target
            # file's content -- check this BEFORE the `-c`/bare-file
            # classification below so `bash -n foo.sh` allows outright
            # instead of falling into the `<file> (interpreter-invoked
            # script)` outright-deny shape.
            if head_base in _SHELL_FILE_INTERPRETERS and _has_noexec_flag_before_script(
                tokens[1:]
            ):
                continue

            # Finding 3 (2026-07-21 review): scan ALL of tokens[1:] for a
            # bundled-or-standalone `-c` flag (`-c`, `-ic`, `-ci`, ...), not
            # just an exact `tokens[1] == "-c"` -- Python's CLI parser
            # accepts bundled short flags, so `python3 -ic '<payload>'`
            # behaves as `python3 -i -c '<payload>'` and an exact-token
            # check misses it entirely.
            c_flag_positions = [
                i for i in range(1, len(tokens)) if _BUNDLED_C_FLAG_RE.match(tokens[i])
            ]
            if c_flag_positions:
                idx = c_flag_positions[0]
                if idx + 1 < len(tokens):
                    inline_payload = tokens[idx + 1]
                else:
                    # `-c` present but no distinguishable operand token --
                    # position not reliably determined, so route the whole
                    # remaining segment through the classifier rather than
                    # silently allowing (over-block-over-miss posture, same
                    # as the outright-deny shapes below).
                    inline_payload = " ".join(shlex.quote(t) for t in tokens[idx + 1 :]) or seg
                verdict = _unwrap_and_classify(inline_payload, depth + 1)
                if verdict is not None:
                    return f"{head_base} -c '<inline>' -> {verdict}"
                continue
            if head_base in _SHELL_FILE_INTERPRETERS and len(tokens) >= 2:
                return (
                    f"{head_base} <file> (interpreter-invoked script -- "
                    "indirection wrapper, script content unexamined)"
                )
            # python/python3 without `-c` (e.g. `python3 -m pytest`,
            # `python3 script.py --flag`) is NOT an enumerated bypass shape
            # -- allow, do not fall through to recursion (recursing on
            # UNCHANGED text would just re-match this same branch forever,
            # eventually hitting the depth cap and denying anyway -- the
            # opposite of the intended "not an enumerated shape" outcome).
            continue

        # `head_base` is neither `xargs` nor a `-c`/file-shaped interpreter.
        # Only recurse if `env`-stripping actually made progress (the
        # remaining tokens differ from the original segment) -- e.g.
        # `env FOO=1 <nested-wrapper>` chains resolving back to a direct
        # destructive-verb check. A bare, non-wrapper command that merely
        # matched the cheap `_WRAPPER_PROBE_RE` via an unrelated substring
        # is NOT re-recursed on unchanged text (same infinite-recursion
        # hazard as the python fallthrough above).
        if was_env_wrapped:
            remainder = " ".join(shlex.quote(t) for t in tokens)
            verdict = _unwrap_and_classify(remainder, depth + 1)
            if verdict is not None:
                return verdict

    return None


# ---------------------------------------------------------------------------
# Layer 2 -- per-segment git checks.
# ---------------------------------------------------------------------------
_CHECKOUT_KEYWORD_RE = re.compile(r"\bcheckout\b")
_CHECKOUT_DASHDASH_RE = re.compile(r"\bcheckout\b.*--(?:\s|$)")
_CHECKOUT_PATHSPEC_RE = re.compile(r"\bcheckout\b\s+[A-Za-z0-9_.-]*/[A-Za-z0-9_./-]*")

# Anchored to an actual `git restore` SUBCOMMAND invocation (`git`, then
# optional bare short/long global flags, then `restore` as the next word) --
# NOT the bare word `restore` appearing anywhere in the segment's free text.
# Fix for a live false positive: `git commit -m "restore the carve-out"`
# (the word "restore" inside a quoted commit-message operand, nowhere near
# an actual subcommand position) was denied as "git restore (working tree)"
# by this legacy free-text classifier. `block_subagent_commit.py`'s own
# module docstring documents this collision under "Known pre-existing
# false-positive NOT inherited here" -- this is that fix, applied at the
# source. Accepted narrow gap (module's existing accepted-tradeoff style,
# see e.g. the cross-segment-dataflow note above): a global flag that
# consumes a separate value token before the subcommand (`git -C <dir>
# restore ...`) is no longer matched here -- this legacy path is reached
# only for a genuinely unparseable segment or a `strict=False` indirection-
# payload scan (see `_git_subcommand_and_remaining_for_segment`), where a
# real `-C <dir>` shape is vanishingly rare; a real shell segment with a
# clean `-C` invocation shlex-tokenizes fine and is already handled by the
# anchored classifier (`_evaluate_git_segment_anchored`'s `subcmd ==
# "restore"` branch), never reaching this fallback at all.
_RESTORE_KEYWORD_RE = re.compile(r"\bgit\b(?:\s+--?[\w-]+)*\s+restore\b")
_RESTORE_WORKTREE_RE = re.compile(r"(?:-W|--worktree)(?:\s|$)")
_RESTORE_STAGED_RE = re.compile(r"(?:--staged|(?:^|\s)-S(?:\s|$))")

_RESET_KEYWORD_RE = re.compile(r"(?:^|\s)reset(?:\s|$)")
_RESET_STRIP_PREFIX_RE = re.compile(r".*(?:^|\s)reset(?:\s|$)")
_RESET_REF_RE = re.compile(r"(?:HEAD|@\{|[0-9a-f]{7,40}|/)")
_DASHDASH_SEP_RE = re.compile(r"--(?:\s|$)")

_STASH_POP_APPLY_RE = re.compile(r"\bstash\b.*\b(?:pop|apply)\b")
#: `stash` subcommands OTHER than the sweep-everything push/bare shape --
#: read-only (`list`/`show`) or a different write shape entirely
#: (`branch`/`create`/`store`/`save`) not targeted by the 2026-07-26
#: unscoped-stash-gap-close fix (module docstring "UNSCOPED-STASH GAP
#: CLOSE"). `pop`/`apply`/`drop`/`clear` are deliberately NOT here -- they
#: are matched (and returned on) by their own dedicated regexes above/below.
#: `save` is deliberately ABSENT (2026-07-28): `git stash save [<msg>]` is the
#: pre-2.16 deprecated spelling of `git stash push [-m <msg>]` with identical
#: working-tree effect, so listing it here as a non-sweep "other action"
#: exempted a real unscoped sweep from the rule below.
_STASH_OTHER_ACTION_WORD_RE = re.compile(
    r"\bstash\b\s+(?:list|show|branch|create|store)\b"
)
_REBASE_RE = re.compile(r"\brebase\b")
_HARD_FLAG_RE = re.compile(r"--hard")
_RESET_WORD_RE = re.compile(r"\breset\b")
_COMMIT_WORD_RE = re.compile(r"\bcommit\b")
_AMEND_FLAG_RE = re.compile(r"--amend")
_PUSH_WORD_RE = re.compile(r"\bpush\b")
_PUSH_FORCE_RE = re.compile(
    r"(?:--force(?:[^\-=]|$)|(?:^|\s)-[a-zA-Z]*f[a-zA-Z]*(?:\s|$)|(?:^|[\s\"'])\+[^\s]+)"
)
_BRANCH_WORD_RE = re.compile(r"\bbranch\b")
_BRANCH_DASH_D_UPPER_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*D[a-zA-Z]*(?:\s|$)")
_DASH_D_OR_DELETE_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*d[a-zA-Z]*(?:\s|$)|--delete")
_DASH_F_OR_FORCE_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*f[a-zA-Z]*(?:\s|$)|--force")
_STASH_DROP_CLEAR_RE = re.compile(r"\bstash\b.*\b(?:drop|clear)\b")
_TAG_WORD_RE = re.compile(r"\btag\b")
_REFLOG_WORD_RE = re.compile(r"\breflog\b")
_REFLOG_ACTION_RE = re.compile(r"\b(?:delete|expire)\b")
_FILTER_BRANCH_REPO_RE = re.compile(r"\bfilter-branch\b|\bfilter-repo\b")
_CLEAN_WORD_RE = re.compile(r"\bclean\b")
_CLEAN_FD_RE = re.compile(
    r"(?:^|\s)-[a-zA-Z]*f[a-zA-Z]*d[a-zA-Z]*(?:\s|$)|(?:^|\s)-[a-zA-Z]*d[a-zA-Z]*f[a-zA-Z]*(?:\s|$)"
)

#: LEGACY-PATH `worktree`/`remote` second-level classification (2026-07-25
#: fix, P2(b) -- supersedes the prior minimal patch of the same date).
#: `worktree`/`remote` used to sit in `_SAFE_VERB_RE` below as BARE VERBS
#: with no second-level-subcommand check at all, so ANY form -- mutating or
#: not -- free-text-matched the bare word and fell through to the
#: safe-forward allowlist uninspected. A first pass added
#: `_WORKTREE_MUTATE_RE`/`_REMOTE_MUTATE_RE` to deny the KNOWN-mutating
#: forms, but left an unenumerated `git worktree <novel-verb>` falling
#: through to `_SAFE_VERB_RE` and allowing -- i.e. still allow-by-default on
#: anything not on the closed mutating list, which is exactly the
#: enumeration-plus-ambiguity-routing combination that let the `--namespace`
#: P0 reach production (a not-yet-enumerated future git worktree/remote verb
#: is unreachable via the anchored path's default-deny, but IS reachable via
#: legacy through an unenumerated global flag forcing ambiguous routing).
#:
#: INVERTED here per the same "unknown means deny, not allow" fix already
#: applied to the anchored classifier -- but WITHOUT argv-position
#: extraction (deliberately not restructuring legacy into a tokenizer): a
#: lightweight `_NEXT_WORD_AFTER_RE` capture of whatever plain word
#: immediately follows the literal `worktree`/`remote` token (still free
#: text, just a single extra regex step, not shlex/argv walking) is checked
#: against the small READ-ONLY sets below; anything else -- an unrecognized
#: word, OR one of the already-known mutating words -- denies. No
#: following word at all (bare `git worktree`/`git remote`) allows, matching
#: the anchored path's bare-invocation behavior. `_WORKTREE_MUTATE_RE`/
#: `_REMOTE_MUTATE_RE` are kept ONLY for their more specific deny-reason
#: text on the already-known forms; the next-word fallback below is what
#: actually closes the default-deny gap.
_WORKTREE_MUTATE_RE = re.compile(
    r"\bworktree\b.*\b(?:add|remove|prune|move|repair|unlock|lock)\b"
)
_REMOTE_MUTATE_RE = re.compile(
    r"\bremote\b.*\b(?:add|remove|rm|rename|set-url|set-head|set-branches|prune|update)\b"
)
_WORKTREE_WORD_RE = re.compile(r"\bworktree\b")
_REMOTE_WORD_RE = re.compile(r"\bremote\b")
#: The single plain word immediately following (whitespace-separated) the
#: position this is `.match()`-ed at -- used anchored at `worktree`'s/
#: `remote`'s own match-end so it captures the very next token, not just
#: any later word in the segment.
_NEXT_WORD_AFTER_RE = re.compile(r"\s+(\S+)")
_LEGACY_WORKTREE_READONLY = frozenset({"list"})
_LEGACY_REMOTE_READONLY = frozenset({"-v", "show", "get-url"})

_SAFE_VERB_RE = re.compile(
    r"\b(?:add|commit|status|log|diff|show|fetch|cherry-pick|rev-parse|rev-list|"
    r"ls-files|describe)\b"
)
_PULL_WORD_RE = re.compile(r"\bpull\b")
_MERGE_WORD_RE = re.compile(r"\bmerge\b")
_FF_ONLY_RE = re.compile(r"--ff-only")
_SWITCH_WORD_RE = re.compile(r"\bcheckout\b|\bswitch\b")
_STASH_WORD_RE = re.compile(r"\bstash\b")
_CONFIG_WORD_RE = re.compile(r"\bconfig\b")
_CONFIG_GET_RE = re.compile(r"--get\b")

_GIT_WORD_RE = re.compile(r"\bgit\b")

#: 2026-07-25 P0 fix (this dispatch, "SAFE-FORWARD OPTION-SURFACE HARDENING"
#: in the module docstring) -- new verb-level option gates for the
#: ANCHORED path only, matched against ``remaining_text`` (the argv slice
#: after the subcommand, joined -- NEVER ``seg``, so a pre-subcommand
#: GLOBAL option sharing a letter, e.g. ``git -C <path> branch x``, cannot
#: false-trip a subcommand-local check here).
#: checkout/switch: `-f`/`--force` throws away uncommitted local
#: modifications (confirmed via `git checkout -h`/`git switch -h`) --
#: reuses the already-defined `_DASH_F_OR_FORCE_RE` (branch's generic
#: bundled-`-f`/`--force` detector) rather than a third copy.
_SWITCH_DISCARD_CHANGES_RE = re.compile(r"--discard-changes\b")
#: checkout `-B` / switch `-C` create-OR-RESET an existing branch even if
#: it already exists ("create/reset and checkout/switch a branch" per
#: `git checkout -h`/`git switch -h`) -- same silent-ref-overwrite shape as
#: `git branch -M`/`-C` below.
_CHECKOUT_DASH_B_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*B[a-zA-Z]*(?:\s|$)")
_SWITCH_DASH_C_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*C[a-zA-Z]*(?:\s|$)")
#: `git branch -M`/`-C` (uppercase) force-move/force-copy EVEN IF the target
#: branch already exists (confirmed via `git branch -h`: "-M  move/rename a
#: branch, even if target exists"; "-C  copy a branch, even if target
#: exists"), silently destroying the pre-existing ref.
_BRANCH_FORCE_MOVE_OR_COPY_UPPER_RE = re.compile(
    r"(?:^|\s)-[a-zA-Z]*[MC][a-zA-Z]*(?:\s|$)"
)
#: `-m`/`--move`/`-c`/`--copy` combined with `-f`/`--force` achieves the
#: same effect as `-M`/`-C` respectively (confirmed via `git branch -h`:
#: "-f, --force  force creation, move/rename, deletion").
_BRANCH_MOVE_OR_COPY_RE = re.compile(
    r"(?:^|\s)-[a-zA-Z]*[mc][a-zA-Z]*(?:\s|$)|--move\b|--copy\b"
)


# ---------------------------------------------------------------------------
# SUBCOMMAND-ANCHORED CLASSIFICATION (2026-07-25 false-positive fix).
# Root cause: every `_..._WORD_RE`/`_..._KEYWORD_RE` regex above (checkout,
# restore, reset, stash, rebase, commit, push, branch, tag, reflog,
# filter-branch, clean, pull, merge, switch, config) is a raw `\bword\b`
# search over the WHOLE segment text -- a hyphen is a word-boundary
# character, so it cannot distinguish the real invoked SUBCOMMAND from the
# same word appearing as an operand, a quoted argument, or a hyphenated
# sibling subcommand. Two confirmed false positives, both DENIED as
# "git merge (not --ff-only)" on disk before this fix:
#   - `git grep -n "some-hyphenated-token"` (the pattern argument merely
#     CONTAINS the word "merge")
#   - `git merge-base --is-ancestor A B` (the hyphen is a word boundary, so
#     `\bmerge\b` matches inside `merge-base`)
# `_git_subcommand_for_segment` below determines the REAL subcommand -- the
# first non-flag token following `git`, skipping git's own global options
# -- via `shlex` tokenization, and `_evaluate_git_segment_anchored` gates
# each verb-identity check on THAT token instead of a free-text regex
# search. The flag/argument-detection regexes (`--ff-only`, `--hard`,
# `-D`, ...) are UNCHANGED -- only the "is this segment invoking verb X"
# gate moved from free-text to argv position; scanning for a flag anywhere
# in a segment already known to invoke the right subcommand carries none of
# the mention-vs-invocation ambiguity a bare verb-word search does.
#
# FAIL-CLOSED on shlex failure: an unparseable segment (unbalanced quoting)
# falls back to `_evaluate_git_segment_legacy` -- the ORIGINAL free-text
# classifier, unchanged -- rather than being newly allowed. The legacy
# classifier is intentionally left untouched (including its own
# `_SAFE_VERB_RE`, which does NOT get the new read-only-verb additions
# below) so "fall back to today's behavior" means exactly that.
# ---------------------------------------------------------------------------

#: git global options that consume a following token as their argument
#: (space-separated form only -- `-C <path>`, `-c <key>=<value>`).
_GIT_GLOBAL_OPT_WITH_ARG = frozenset({"-C", "-c"})

#: git global options KNOWN to take no argument at all -- a closed,
#: explicitly-enumerated allowlist. Deliberately small: this is NOT an
#: attempt to enumerate git's full global-option surface (see the
#: unrecognized-flag fail-over below for why that enumeration is the wrong
#: shape of fix).
_GIT_GLOBAL_OPT_NO_ARG = frozenset(
    {"--no-pager", "-P", "--bare", "--literal-pathspecs", "--paginate", "--no-optional-locks"}
)

#: `--git-dir`/`--work-tree` accept EITHER `--foo=value` (one token, handled
#: by the generic `"=" in tok` self-contained-value branch below) or
#: `--foo value` (two tokens) -- the two-token form must be explicitly
#: enumerated here or a space-separated form misresolves the next token as
#: the subcommand.
_GIT_GLOBAL_OPT_SPACE_FORM = frozenset({"--git-dir", "--work-tree"})


#: Sentinel returned by `_git_subcommand_and_remaining_for_segment` in the
#: `subcmd` slot when the segment's COMMAND-POSITION head (after peeling any
#: leading subshell-open `(` tokens and an `env`-prefix, see
#: `_strip_leading_subshell_and_env`) resolves to something other than
#: `git` -- i.e. the word "git" may still appear elsewhere in the segment
#: (a quoted regex alternation, a grep pattern operand, a filename like
#: `git-crypt`), but this segment does not INVOKE git. Distinct from
#: `subcmd is None` (bare `git`/global-flags-only invocation, e.g. `git -C x`
#: with no further subcommand) -- that case still routes through
#: `_evaluate_git_segment_anchored`'s default-deny ladder unchanged. Distinct
#: also from `parse_ok is False` (shlex genuinely could not tokenize the
#: segment, or an unrecognized global flag made the real subcommand
#: unresolvable) -- that case still fails over to the free-text legacy
#: classifier. See module docstring "COMMAND-POSITION GIT-TOKEN FIX".
_NOT_A_GIT_INVOCATION = object()

#: `parse_ok=False` sentinel distinguishing the INLINE-INTERPRETER
#: CARVE-OUT branch (a `-c`-flagged interpreter head with a bundled `-c`
#: flag -- `python3 -c '...'`, `bash -c '...'`, ...) from a genuinely
#: unparseable REAL shell segment. Both route to the legacy free-text
#: classifier (fail-closed on the specific destructive-verb patterns is
#: unchanged for both), but `_evaluate_git_segment` uses this sentinel to
#: suppress ONLY legacy's terminal "unrecognized git verb (default-deny)"
#: catchall for this branch -- see `_evaluate_git_segment`'s "INDIRECTION-
#: PAYLOAD CATCHALL FIX" docstring entry: an interpreter `-c` payload is
#: not real shell syntax, so the bare free-text presence of "git" here
#: carries no invocation meaning, the same reasoning already applied to
#: `_unwrap_and_classify`'s `strict=False` recursive scan.
_INTERPRETER_C_PAYLOAD_AMBIGUOUS = object()

#: Passthrough wrapper binaries that run their remaining argv unchanged --
#: see `_strip_leading_subshell_and_env`'s BX-13 fix comment. Same set
#: `dispatch_checks.py`'s `_BYPASS_PREFIX` already tolerates.
#: Widened (2026-07-29, code-reviewer Finding 3): `setsid`, `strace`, `doas`,
#: and `busybox` were unrecognized passthrough wrappers -- `setsid git
#: worktree add ../wt-1 x` landed the resolved command-position head on
#: `setsid` (never `git`), which is the SAME "unrecognized wrapper binary
#: hides the real command" defect this set exists to close, just via a
#: different binary name. This is still an ENUMERATED allowlist, not a
#: structural fix -- see the finding's own discussion of why a
#: command-position-anchored classifier could instead fall back to the
#: legacy free-text scanner (deny-capable, not allow-outright) on an
#: UNRECOGNIZED head rather than only on a genuine parse failure; that
#: broader change is out of scope for this pass and is called out
#: separately, not silently deferred.
_PASSTHROUGH_WRAPPERS = frozenset(
    {
        "sudo", "command", "time", "exec", "nice", "nohup", "ionice", "timeout",
        "stdbuf", "which", "type", "setsid", "strace", "doas", "busybox",
    }
)

#: BX-14 fix (2026-07-29, confirmed live via the real dispatcher): the peel
#: below tolerated the wrapper BINARY token but never the wrapper's OWN
#: argument(s) -- `timeout 30 git rebase -i HEAD~3`, `ionice -c2 git stash`,
#: `stdbuf -oL git worktree add ...` all landed the resolved head on
#: `30`/`-c2`/`-oL` (never `git`), so the wrapped command still ran for real
#: while this guard (and every guard/module importing this shared peel,
#: including `block_worktree_creation.py`) allowed. Same flag-set
#: `dispatch_checks.py`'s `_BYPASS_WRAPPER_ARG_FLAGS` uses for the identical
#: gap in `check_no_verify` -- own-module copy (no-cross-module-coupling
#: convention).
#: `_skip_wrapper_own_argv` itself now lives in `_command_tokenizer.py`
#: (2026-07-30, M8 consolidation) -- imported above rather than
#: hand-maintained here; this was the ORIGINAL of the five hand-maintained
#: copies (see that module's own docstring for the full history).


def _strip_leading_subshell_and_env(tokens: List[str]) -> List[str]:
    """Peel leading subshell-open `(` markers and a leading `env` prefix off
    ``tokens`` so the remaining head token is the true COMMAND-POSITION
    executable name -- the start of the command, or immediately following a
    subshell open or an `env` invocation (module docstring "COMMAND-POSITION
    GIT-TOKEN FIX"; pipe/`;`/`&`/`&&`/`||` boundaries are already segment
    boundaries by the time a segment reaches this function, handled by
    `_evaluate_git_surface`'s own `;&|` split, so they need no handling
    here). Handles BOTH the spaced subshell form (`( git push`, `(` its own
    token) and the glued form (`(git push`, no space) -- `shlex.split` with
    default `wordchars` treats `(` as an ordinary character, so a glued
    subshell open stays attached to the following word as one token and must
    be peeled off explicitly rather than relying on basename normalization
    (which only strips path separators and a `.exe` suffix, never a leading
    paren). Nested opens (`((git ...`) are peeled in a loop. Returns the
    tokens unchanged (module-empty list unaffected) if neither a leading
    subshell-open nor an `env` prefix is present.
    """
    working = list(tokens)
    while working:
        head = working[0]
        if head == "(":
            working = working[1:]
            continue
        if head.startswith("(") and len(head) > 1:
            working = [head[1:]] + working[1:]
            continue
        # BRACE-GROUPING FIX (2026-07-29, code-reviewer Finding 1, confirmed
        # live): `{ git push --force; }` was never peeled here -- only a
        # subshell-open `(` was. Bash's `{ ... }` grouping REQUIRES a space
        # after `{` (it is a reserved word, not an operator like `(`), so
        # `shlex.split` always yields it as its OWN token (`{cmd` glued is
        # not valid shell syntax and shlex leaves it as one opaque token that
        # never equals `{`, so there is no glued form to additionally peel,
        # unlike the subshell-open case above). Peeling it here exposes the
        # true command-position head exactly as the `(` peel does -- e.g.
        # `{ git push --force; }` splits (by the caller's own `;`/`&`/`|`
        # segmenter) into a `{ git push --force` segment and a bare `}`
        # segment; peeling `{` off the first lands `git` at command
        # position, restoring the anchored classifier's deny ladder for
        # every destructive verb this guard exists to block.
        if head == "{":
            working = working[1:]
            continue
        # BX-13 fix (2026-07-29, confirmed live): a BARE `VAR=value`
        # assignment with no `env` keyword (`FOO=1 git rebase -i HEAD~3`)
        # was never peeled here -- only a literal `env` invocation was
        # (`_strip_env_prefix` below returns tokens unchanged unless
        # `tokens[0] == "env"`) -- so the resolved command-position head was
        # `FOO=1`, never `git`, and the segment fell through as "not a git
        # invocation" while the wrapped command still ran for real.
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            working = working[1:]
            continue
        # BX-13 fix (2026-07-29, confirmed live): a no-op passthrough
        # wrapper (`nice git rebase -i HEAD~3`) was never peeled either --
        # same root cause as the bare-assignment case immediately above,
        # different token shape.
        head_base = _normalize_executable_basename(head)
        if head_base in _PASSTHROUGH_WRAPPERS:
            working = working[1:]
            i = _skip_wrapper_own_argv(working, 0, head_base)
            working = working[i:]
            continue
        break
    return _strip_env_prefix(working)


def _real_git_subcommand(
    argv_after_git: List[str],
) -> "tuple[Optional[str], bool, List[str]]":
    """Return ``(subcommand_or_None, ambiguous, remaining_after_subcommand)``
    -- the real git SUBCOMMAND, the first non-flag token in
    ``argv_after_git`` (the tokens following the literal ``git`` token),
    after skipping git's own global options, plus the tokens that follow
    the subcommand itself (needed by callers that classify on a SECOND-level
    subcommand, e.g. ``worktree``/``remote`` -- see
    `_evaluate_git_segment_anchored`). ``subcommand_or_None`` is ``None`` if
    no such token exists (bare ``git``, or a global-flags-only invocation),
    in which case ``remaining_after_subcommand`` is ``[]``; ``ambiguous`` is
    ``True`` when an unrecognized flag token's argument-taking shape is
    unknown.

    LOAD-BEARING INVARIANT: an unrecognized global option means we do not
    know whether it consumes the following token as its value or not -- and
    not knowing must NEVER produce an ALLOW. Guessing "no argument" for an
    unrecognized `--xxx` token was the exact P0 bypass this function existed
    to close (`git --namespace status push --force` resolved `"status"`,
    the value of `--namespace`, as the subcommand, and never inspected
    `push --force`). Rather than hand-enumerate git's full global-option
    surface (a maintenance burden that reopens this class every time git
    adds one), any token whose consumption shape we don't explicitly know
    sets ``ambiguous=True`` so the caller fails this segment over to the
    legacy free-text classifier -- ambiguity resolves to the MORE
    conservative classifier, never to a guess.
    """
    i = 0
    n = len(argv_after_git)
    while i < n:
        tok = argv_after_git[i]
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        if tok in _GIT_GLOBAL_OPT_NO_ARG:
            i += 1
            continue
        if tok in _GIT_GLOBAL_OPT_SPACE_FORM:
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            # Self-contained `--foo=value` -- carries its own value in one
            # token, so consuming exactly this token is unambiguous
            # regardless of whether we recognize the flag name.
            i += 1
            continue
        if tok.startswith("-"):
            # Unrecognized `-`/`--`-prefixed token with argument shape
            # UNCERTAIN (could be a bare no-arg flag, or a flag that
            # consumes the next token as its value -- e.g. `--namespace`,
            # `--super-prefix`, `--config-env`, `--exec-path`, or any future
            # git global option not enumerated above). Do not guess --
            # fail this segment over to the legacy classifier.
            return None, True, []
        return tok, False, argv_after_git[i + 1 :]
    return None, False, []


def _git_subcommand_and_remaining_for_segment(
    seg: str, strict: bool = True,
) -> "tuple[Optional[str], bool, List[str]]":
    """Tokenize ``seg`` (a segment already known to contain a free-text
    ``\\bgit\\b`` match -- see `_evaluate_git_surface`) with ``shlex`` and
    resolve the real subcommand. Returns ``(subcommand_or_None, parse_ok,
    remaining_after_subcommand)``.

    ``strict`` (module docstring "COMMAND-POSITION GIT-TOKEN FIX") selects
    between two resolution strategies:

    - ``strict=True`` (default -- used for a REAL shell command segment,
      i.e. anything reachable from `check()`'s top-level ``cmd_norm``):
      resolves the subcommand from the segment's COMMAND-POSITION head only
      (after `_strip_leading_subshell_and_env`). If that head does NOT
      normalize to ``git``, returns the `_NOT_A_GIT_INVOCATION` sentinel
      with ``parse_ok=True`` -- a THIRD state, distinct from both "bare
      ``git`` with no subcommand" (``subcmd is None``) and "genuinely
      unparseable/ambiguous" (``parse_ok=False``). The caller must allow
      this segment outright on the sentinel, NOT fall over to the legacy
      free-text classifier -- routing it to legacy is exactly the
      false-positive class this fix closes (legacy's own default-deny fires
      on the mere free-text presence of "git" anywhere in the segment,
      regardless of whether git is actually invoked -- confirmed live via a
      plain ``grep -n "git " file`` denial).
    - ``strict=False`` (used ONLY by `_unwrap_and_classify`'s recursive
      payload scan): preserves the ORIGINAL, pre-fix behavior -- scans EVERY
      token in the segment for one whose basename equals ``git``, anywhere
      in the argv, not just command position. An unwrapped interpreter `-c`
      payload is not guaranteed to be real shell syntax (most often it is
      actual PYTHON SOURCE, e.g. `subprocess.run(["git", "push",
      "--force"])`), so shlex-derived "command position" is not a
      meaningful concept there, and the strict shortcut above would
      incorrectly allow-outright a payload whose glued, non-command-shaped
      tokenization (`['subprocess.run([git,', 'push,', '--force])']`) never
      resolves the head to exactly ``git`` -- exactly the indirection
      payload this recursive scan exists to catch. Never returns
      `_NOT_A_GIT_INVOCATION`; returns ``parse_ok=False`` (legacy fallback)
      whenever no token matches, identical to this function's pre-fix
      contract.

    In BOTH modes: ``parse_ok`` is ``False`` when ``shlex`` cannot tokenize
    the segment (unbalanced quoting) OR ``_real_git_subcommand`` reports an
    unrecognized-global-option ambiguity; the caller MUST fall back to the
    legacy free-text classifier in every such case (fail CLOSED -- an
    unparseable OR ambiguous segment never becomes newly allowed).
    ``remaining_after_subcommand`` is ``[]`` whenever ``parse_ok`` is
    ``False``. When the resolved head DOES normalize to ``git``,
    ``subcommand_or_None`` is a real string or ``None`` (bare ``git``/
    global-flags-only invocation, routed through `_evaluate_git_segment_
    anchored`'s default-deny ladder unchanged) in both modes.

    This is the FULL (3-tuple) form used internally by
    `_evaluate_git_segment`; `_git_subcommand_for_segment` below is the
    original 2-tuple public form kept unchanged for existing callers/tests
    that don't need `remaining_after_subcommand`.
    """
    if _exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- `parse_ok=False` routes the caller to the legacy free-text
        # classifier (default-deny), the same branch an unterminated quote
        # already reaches.
        return None, False, []
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return None, False, []

    if not strict:
        for idx, tok in enumerate(tokens):
            base = _normalize_executable_basename(tok)
            if base == "git":
                subcmd, ambiguous, remaining = _real_git_subcommand(tokens[idx + 1 :])
                if ambiguous:
                    return None, False, []
                return subcmd, True, remaining
        return None, False, []

    working = _strip_leading_subshell_and_env(tokens)
    if not working:
        # Nothing left in command position (e.g. `env` with no trailing
        # command, or an empty/whitespace-only segment) -- not a git
        # invocation, but not a parse failure either.
        return _NOT_A_GIT_INVOCATION, True, []
    base = _normalize_executable_basename(working[0])
    if base != "git":
        # INLINE-INTERPRETER CARVE-OUT (2026-07-28, part of the
        # "COMMAND-POSITION GIT-TOKEN FIX"): a command-position head that IS
        # a `-c`-flagged interpreter (`python`/`python3`/`bash`/`sh`/`zsh`)
        # is NOT eligible for the confident allow-outright shortcut, even
        # though its OWN argv0 is not `git`. Confirmed live: `python -c
        # 'subprocess.run(["git", "push", "--force"])'` shlex-tokenizes its
        # inline payload into a glued, non-command-shaped token list that
        # neither this function's strict path NOR the dedicated
        # indirection-unwrap machinery (`_evaluate_wrapper_indirection`'s
        # `_WRAPPER_PROBE_RE`, gapped on a `/`-preceded interpreter name;
        # `_evaluate_tokenized`'s nested recursion, which never reuses the
        # git-specific classifier) currently resolves to a real `git`
        # command-position token -- the ONLY thing that still catches this
        # live exploit is the LEGACY free-text fallback matching `push`/
        # `--force` inside the quoted payload text. Shortcutting to allow
        # here would silently reopen that hole. Fails this segment over to
        # legacy (`parse_ok=False`) instead of allowing outright -- the
        # SAME conservative "ambiguous means legacy, never a guess" posture
        # `_real_git_subcommand` already uses for an unrecognized global
        # flag, applied to a distinct but analogous ambiguity.
        interp_base = _normalize_interpreter_basename(base)
        if interp_base in _C_FLAG_INTERPRETERS and any(
            _BUNDLED_C_FLAG_RE.match(tok) for tok in working[1:]
        ):
            return _INTERPRETER_C_PAYLOAD_AMBIGUOUS, False, []
        return _NOT_A_GIT_INVOCATION, True, []
    subcmd, ambiguous, remaining = _real_git_subcommand(working[1:])
    if ambiguous:
        return None, False, []
    return subcmd, True, remaining


def _git_subcommand_for_segment(seg: str) -> "tuple[Optional[str], bool]":
    """Original 2-tuple public form of
    `_git_subcommand_and_remaining_for_segment` -- ``(subcommand_or_None,
    parse_ok)``, dropping `remaining_after_subcommand`. Kept as its own
    function (not just documented as "ignore the third value") so existing
    direct callers/tests keep their exact original call shape. Always
    strict (real shell command semantics) -- there is no legacy caller of
    this 2-tuple form that needs the non-strict recursive-payload variant.
    """
    subcmd, parse_ok, _remaining = _git_subcommand_and_remaining_for_segment(seg)
    return subcmd, parse_ok


#: Read-only git verbs safe to allow outright -- ANCHORED path only (the
#: legacy `_SAFE_VERB_RE` fallback deliberately keeps its original list, see
#: module comment above). `merge-base` and `grep` are the two verbs behind
#: the confirmed false positives; `ls-remote`, `blame`, `shortlog`,
#: `cat-file`, `for-each-ref`, `name-rev`, `check-ignore` are added
#: alongside them -- each reads repo state and cannot mutate it.
#:
#: `symbolic-ref` is deliberately NOT in this set: `git symbolic-ref HEAD
#: refs/heads/evil` repoints HEAD, and `git symbolic-ref --delete <ref>`
#: deletes a symbolic ref -- both mutate refs (confirmed empirically
#: 2026-07-25; git itself happens to refuse `--delete HEAD` specifically,
#: but the verb mutates in general, e.g. `refs/remotes/origin/HEAD`). It
#: was added here unflagged in a prior pass and is being removed, not
#: special-cased on `--delete`/argument-count -- that would reintroduce the
#: free-text-flag-sniffing disease this anchored classifier exists to cure.
#: The read-only use case (current branch name) is already covered by the
#: safe-listed `rev-parse`: `git rev-parse --abbrev-ref HEAD`.
#:
#: `worktree`/`remote` are deliberately NOT in this set either (2026-07-25
#: fix): both were blanket-safe-listed here as BARE VERBS with no
#: inspection of their SECOND-level subcommand, so `git worktree remove`,
#: `git worktree prune`, `git remote remove origin`, `git remote set-url`,
#: etc. all passed uninspected. They are now classified on the second-level
#: token below (`_WORKTREE_MUTATING_SUBCOMMANDS`/`_REMOTE_MUTATING_SUBCOMMANDS`)
#: instead of being blanket-allowed here.
_SAFE_GIT_SUBCOMMANDS = frozenset(
    {
        "add",
        "commit",
        "status",
        "log",
        "diff",
        "show",
        "fetch",
        "cherry-pick",
        "rev-parse",
        "rev-list",
        "ls-files",
        "describe",
        "merge-base",
        "grep",
        "ls-remote",
        "blame",
        "shortlog",
        "cat-file",
        "for-each-ref",
        "name-rev",
        "check-ignore",
    }
)

#: `git worktree` SECOND-level subcommand classification (2026-07-25 fix).
#: `list` is the only read-only second-level subcommand; a bare `git
#: worktree` (no second-level token) also lists and is read-only. Every
#: other second-level subcommand mutates the working-tree/worktree-registry
#: state and is denied; an UNRECOGNIZED second-level token is denied too
#: (default-deny, consistent with the top-level rule) -- see
#: `_evaluate_git_segment_anchored`.
_WORKTREE_MUTATING_SUBCOMMANDS = frozenset(
    {"add", "remove", "prune", "move", "repair", "unlock", "lock"}
)

#: `git remote` SECOND-level subcommand classification (2026-07-25 fix).
#: `-v`/`show`/`get-url` are read-only; a bare `git remote` (no second-level
#: token) also lists and is read-only. Every enumerated mutating form is
#: denied; an UNRECOGNIZED second-level token is denied too (default-deny)
#: -- see `_evaluate_git_segment_anchored`.
_REMOTE_READONLY_SUBCOMMANDS = frozenset({"-v", "show", "get-url"})
_REMOTE_MUTATING_SUBCOMMANDS = frozenset(
    {
        "add",
        "remove",
        "rm",
        "rename",
        "set-url",
        "set-head",
        "set-branches",
        "prune",
        "update",
    }
)


def _evaluate_git_segment_anchored(
    seg: str, subcmd: Optional[str], remaining: Optional[List[str]] = None
) -> Optional[str]:
    """Subcommand-anchored counterpart of `_evaluate_git_segment_legacy` --
    identical deny ladder, identical messages, identical flag/argument
    regexes; the only change is that each "is this segment invoking verb X"
    gate is `subcmd == "X"` (the real argv subcommand) instead of a
    free-text `\\bX\\b` search over the whole segment.

    `remaining` -- the argv tokens following `subcmd` itself (from
    `_git_subcommand_for_segment`) -- was originally used ONLY by the
    `worktree`/`remote` second-level classification below; the 2026-07-25
    P0 fix (module docstring "SAFE-FORWARD OPTION-SURFACE HARDENING")
    additionally uses it for checkout/switch/branch/tag's new option gates
    and for merge/pull/config's exact-token checks, all scoped to
    `remaining_text` (never `seg`) so a pre-subcommand GLOBAL option cannot
    false-trip a subcommand-local check.
    """
    remaining = remaining or []
    remaining_text = " ".join(remaining)

    if subcmd == "worktree":
        second = remaining[0] if remaining else None
        if second is None or second == "list":
            return None
        if second in _WORKTREE_MUTATING_SUBCOMMANDS:
            return f"git worktree {second}"
        return "unrecognized git worktree subcommand (default-deny)"

    if subcmd == "remote":
        second = remaining[0] if remaining else None
        if second is None or second in _REMOTE_READONLY_SUBCOMMANDS:
            return None
        if second in _REMOTE_MUTATING_SUBCOMMANDS:
            return f"git remote {second}"
        return "unrecognized git remote subcommand (default-deny)"

    if subcmd == "checkout":
        if _CHECKOUT_DASHDASH_RE.search(seg) or _CHECKOUT_PATHSPEC_RE.search(seg):
            return "git checkout <pathspec>"
        if _DASH_F_OR_FORCE_RE.search(remaining_text):
            return "git checkout -f/--force (discards uncommitted changes)"
        if _CHECKOUT_DASH_B_RE.search(remaining_text):
            return "git checkout -B (force-resets an existing branch)"

    if subcmd == "switch":
        if _DASH_F_OR_FORCE_RE.search(remaining_text) or _SWITCH_DISCARD_CHANGES_RE.search(
            remaining_text
        ):
            return "git switch -f/--force/--discard-changes (discards uncommitted changes)"
        if _SWITCH_DASH_C_RE.search(remaining_text):
            return "git switch -C (force-resets an existing branch)"

    if subcmd == "restore":
        if _RESTORE_WORKTREE_RE.search(seg) or not _RESTORE_STAGED_RE.search(seg):
            return "git restore (working tree)"

    if subcmd == "reset":
        reset_after = _RESET_STRIP_PREFIX_RE.sub(" ", seg, count=1)
        if _RESET_REF_RE.search(reset_after) and not _DASHDASH_SEP_RE.search(reset_after):
            return "git reset <prior-ref>"

    if subcmd == "stash" and _POP_APPLY_ACTION_RE.search(seg):
        return "git stash pop/apply"

    if subcmd == "stash":
        # 2026-07-26 fix (module docstring "UNSCOPED-STASH GAP CLOSE"): bare
        # `git stash`, the IMPLICIT-push flag-only form (`git stash -u`), and
        # explicit `git stash push` all sweep every uncommitted change on the
        # shared tree unless scoped with a `--`-delimited pathspec.
        # `pop`/`apply` already returned above; `list`/`show`/`branch`/
        # `create`/`store` are a different subcommand entirely (not this
        # rule's concern) and fall through unchanged.
        #
        # 2026-07-28: `save` was grouped with `create`/`store` above and is
        # now handled as a sweep shape instead -- `git stash save [<msg>]` is
        # the pre-2.16 deprecated spelling of `git stash push [-m <msg>]`,
        # identical working-tree effect, so excluding it left a live bypass
        # of this very rule. Found by review of the EM-path sibling fix
        # (dispatch_checks.check_destructive_git_revert), which had inherited
        # the same misclassification from here; both are corrected together
        # so the subagent-path and EM-path guards do not diverge.
        stash_head = remaining[0] if remaining else None
        is_push_or_bare = (
            stash_head is None
            or stash_head in ("push", "save")
            or stash_head.startswith("-")
        )
        if is_push_or_bare and "--" not in remaining:
            return "git stash (unscoped)"

    if subcmd == "rebase":
        return "git rebase"
    if subcmd == "reset" and _HARD_FLAG_RE.search(seg):
        return "git reset --hard"
    if subcmd == "commit" and _AMEND_FLAG_RE.search(seg):
        return "git commit --amend"
    if subcmd == "push":
        if _PUSH_FORCE_RE.search(seg):
            return "git push --force"
    if subcmd == "branch":
        if _BRANCH_DASH_D_UPPER_RE.search(seg):
            return "git branch -D"
        if _DASH_D_OR_DELETE_RE.search(seg) and _DASH_F_OR_FORCE_RE.search(seg):
            return "git branch -d --force"
        if _BRANCH_FORCE_MOVE_OR_COPY_UPPER_RE.search(remaining_text):
            return "git branch -M/-C (force-move/force-copy over an existing branch)"
        if _BRANCH_MOVE_OR_COPY_RE.search(remaining_text) and _DASH_F_OR_FORCE_RE.search(
            remaining_text
        ):
            return "git branch -m/-c --force (force-move/force-copy over an existing branch)"
    if subcmd == "stash" and _DROP_CLEAR_ACTION_RE.search(seg):
        return "git stash drop/clear"
    if subcmd == "tag" and _DASH_D_OR_DELETE_RE.search(seg):
        return "git tag -d"
    if subcmd == "tag" and _DASH_F_OR_FORCE_RE.search(remaining_text):
        return "git tag -f/--force (replaces an existing tag ref)"
    if subcmd == "reflog" and _REFLOG_ACTION_RE.search(seg):
        return "git reflog delete/expire"
    if subcmd in ("filter-branch", "filter-repo"):
        return "git filter-branch/filter-repo"
    if subcmd == "clean" and _CLEAN_FD_RE.search(seg):
        return "git clean -fd(x)"

    # --- SAFE-FORWARD ALLOWLIST -- reached only if none of the above
    # matched. Any git subcommand not enumerated here is DEFAULT-DENIED:
    # the load-bearing "novel verb denied for not being allowlisted"
    # property, now keyed on the real argv subcommand rather than a
    # free-text match.
    #
    # 2026-07-25 P0 fix: `_SAFE_GIT_SUBCOMMANDS` used to return `None`
    # (allow) unconditionally here -- confirmed live: `git show
    # --output=<path> HEAD` / `git log --output=<path>` both write an
    # arbitrary file. `find_git_diff_family_write_flag` (shared with
    # `block_reviewer_bash_outside_allowlist` via `_helpers`) now gates
    # every member on `remaining` before allowing.
    if subcmd in _SAFE_GIT_SUBCOMMANDS:
        bad_option = find_git_diff_family_write_flag(remaining)
        if bad_option is not None:
            return (
                f"git {subcmd} option {bad_option!r} is a write/exec vector -- "
                "--output/-o writes to an arbitrary caller-chosen file "
                "(confirmed: `git show --output=<path>` and `git log "
                "--output=<path>` both create the target file) and "
                "--ext-diff enables an external diff driver"
            )
        return None
    if subcmd == "pull":
        # 2026-07-25 P0 fix: `_FF_ONLY_RE.search(seg)` searched the WHOLE
        # segment text, so `--ff-only` mentioned inside a quoted `-m`
        # message operand granted the allow for a real non-fast-forward
        # pull. Exact-token membership against `remaining` (already
        # shlex-tokenized by `_real_git_subcommand`) fixes this: a quoted
        # multi-word operand is ONE token that can never equal the literal
        # `--ff-only` flag token.
        if "--ff-only" in remaining:
            return None
        return "git pull (not --ff-only)"
    if subcmd == "merge":
        # Same fix as `pull` above -- confirmed live exploit: `git merge -m
        # "we prefer --ff-only merges" feat` used to allow via the
        # free-text search matching the flag NAME inside the quoted commit
        # message.
        if "--ff-only" in remaining:
            return None
        return "git merge (not --ff-only)"
    if subcmd == "push":
        # Forcing forms already denied above; anything else on push is safe.
        return None
    if subcmd in ("checkout", "switch"):
        return None
    if subcmd == "branch":
        return None
    if subcmd == "stash":
        return None
    if subcmd == "restore":
        return None
    if subcmd == "tag":
        return None
    if subcmd == "config":
        # 2026-07-25 P0 fix: `_CONFIG_GET_RE.search(seg)` searched the
        # WHOLE segment text, so `--get` mentioned inside a quoted config
        # VALUE operand granted the allow for a real config WRITE.
        # Confirmed live exploit: `git config alias.lg "log --get"`. Same
        # exact-token fix as pull/merge above -- `config`'s value operand
        # is POSITIONAL (preceded by no flag at all), so a
        # freetext-operand-flag-stripping approach (e.g.
        # `dispatch_checks._seg_excluding_freetext_operands`, which only
        # strips `-m`/`--message`-flagged operands) would NOT have covered
        # this shape; the exact-token check against the already-tokenized
        # `remaining` argv handles both uniformly.
        if "--get" in remaining:
            return None
        return "git config (not --get)"
    if subcmd == "mv":
        # Classified explicitly rather than falling through to the
        # default-deny below, SOLELY to earn a named forward path in
        # `_deny_message` — the verdict is unchanged (still denied).
        # 2026-07-25 DoE memo asked for a per-dispatch `git mv` carve-out
        # after an executor authorized by its brief to move one archived
        # lesson file fell back to filesystem `mv`. Declined, and this is
        # the reasoning: `git mv A B` is exactly `mv A B` plus `git add A B`,
        # and the `git add` half is precisely what the EM-only staging lock
        # exists to withhold — on a shared tree, a subagent writing the
        # index is what lets a peer's bare `git commit` absorb work nobody
        # reviewed. Nothing is lost by declining: git detects renames by
        # content similarity at commit time, so the EM's own `git add` of
        # both paths records the rename identically. The executor's `mv`
        # fallback was the INTENDED path, not a workaround — it only read
        # as a gap because this guard denied it as an "unrecognized verb"
        # without ever naming the alternative.
        return "git mv (index-mutating rename)"

    return "unrecognized git verb (default-deny)"


#: Action-only counterparts of `_STASH_POP_APPLY_RE`/`_STASH_DROP_CLEAR_RE`
#: for the anchored path -- subcommand identity is already confirmed via
#: argv position, so only the action word itself needs a text search.
_POP_APPLY_ACTION_RE = re.compile(r"\b(?:pop|apply)\b")
_DROP_CLEAR_ACTION_RE = re.compile(r"\b(?:drop|clear)\b")


# ---------------------------------------------------------------------------
# TOKENIZED AUTHORITATIVE PASS (2026-07-21 security-audit hardening,
# findings #1-#6). Structural root cause: the git/rm/chmod/chown
# verb-identification above (`_GIT_SURFACE_RE`/`_RM_SURFACE_RE`/
# `_CHMOD_CHOWN_RE`/`_evaluate_git_segment`'s raw-text entry) matches a raw
# substring over the command TEXT -- any trick that stops the destructive
# verb's literal characters from appearing CONTIGUOUS defeats it (`r''m`,
# `ch"m"od`, `r\m`). This pass tokenizes with `shlex` (which correctly
# reconstructs the intended word from all three obfuscations) and matches the
# resulting argv[0] TOKEN against the verb/surface lists instead -- this is
# now the AUTHORITATIVE identification path; the raw-text probes above are
# kept as a cheap pre-filter/defense-in-depth (OR'd in at the call site,
# `check()`) but are never the SOLE gate.
#
# This pass is purely ADDITIVE alongside every raw-regex path above and the
# existing `_evaluate_wrapper_indirection`/`_unwrap_and_classify` machinery
# (both left untouched) -- it only WIDENS what Layer 2 denies, exactly like
# the 2026-07-21 boundary-anchor widen it sits beside.
#
# 2026-07-29: `_tokenize_full_command`/`_segments_from_tokens` moved to
# `_command_tokenizer.py` (imported above, re-exported under this module's
# prior private names) -- see that module's docstring for why.
# ---------------------------------------------------------------------------


class _TokenSurfaces:
    """Accumulator for `_evaluate_tokenized`'s single-pass classification."""

    __slots__ = (
        "is_git",
        "is_rm",
        "is_chmod_chown_r",
        "is_wrapper",
        "is_machine_local",
        "deny_kind",
    )

    def __init__(self) -> None:
        self.is_git = False
        self.is_rm = False
        self.is_chmod_chown_r = False
        self.is_wrapper = False
        self.is_machine_local = False
        self.deny_kind: Optional[str] = None


def _evaluate_tokenized(cmd_text: str, depth: int = 0) -> _TokenSurfaces:
    """Tokenized authoritative pass (2026-07-21 audit response, findings
    #1-#6). Identifies the git/rm/chmod/chown/wrapper/eval/source verb at
    each segment's ``argv[0]`` TOKEN position (never a raw-substring match),
    then reuses the EXISTING sub-verb regexes (``_evaluate_git_segment``,
    ``_RM_DENY_RE``, ``_RECURSIVE_FLAG_RE``) against a normalized,
    quote-stripped reconstruction of the segment's tokens -- reuse, not a
    parallel classifier.
    """
    result = _TokenSurfaces()

    if depth > _MAX_INDIRECTION_DEPTH:
        result.is_wrapper = True
        result.deny_kind = "indirection nesting too deep (fails closed)"
        return result

    tokens = _tokenize_full_command(cmd_text)
    if tokens is None:
        result.is_wrapper = True
        result.deny_kind = "unparseable command (fails closed)"
        return result

    for seg_tokens, pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue

        argv0 = seg_tokens[0]
        if _ARGV0_UNRESOLVED_RE.match(argv0) and not _is_settings_home_bin_argv0(argv0):
            # Finding #3: unexpanded $VAR/${VAR}/$(...) in argv[0] position --
            # deny outright, do NOT attempt to statically resolve the
            # referenced value. EXEMPTION: the doctrine-mandated settings-home
            # CLI form (see `_SETTINGS_HOME_BIN_EXEMPT_RE` above) is narrowly
            # excluded -- its binary name is statically known even though the
            # directory prefix is an unresolved env var.
            result.is_wrapper = True
            if result.deny_kind is None:
                result.deny_kind = (
                    "unresolved $VAR/${VAR}/$(...) in command position -- "
                    "referenced value not statically resolvable"
                )
            continue

        was_env_wrapped = argv0 == "env"
        working = _strip_env_prefix(seg_tokens)
        if not working:
            continue
        if was_env_wrapped and _ARGV0_UNRESOLVED_RE.match(working[0]) and not _is_settings_home_bin_argv0(working[0]):
            result.is_wrapper = True
            if result.deny_kind is None:
                result.deny_kind = (
                    "unresolved $VAR/${VAR}/$(...) in command position -- "
                    "referenced value not statically resolvable"
                )
            continue

        head_base = _normalize_executable_basename(working[0])
        norm_head = _normalize_interpreter_basename(head_base)
        # Reconstruct with shlex.quote per token, not a bare join -- a bare
        # join is lossy (a raw quote char in an already-unquoted token, e.g.
        # a commit message with an apostrophe, can make the downstream
        # re-shlex.split() spuriously fail and misroute the call to the
        # legacy free-text fallback, which can then false-positive-deny on
        # a legacy-recognized destructive word inside the message text).
        # shlex.quote makes the round-trip lossless.
        seg_norm_text = " ".join(shlex.quote(tok) for tok in working)

        if norm_head == "git":
            result.is_git = True
            verdict = _evaluate_git_segment(seg_norm_text)
            if verdict is not None and result.deny_kind is None:
                result.deny_kind = verdict
            continue

        if norm_head == "rm":
            result.is_rm = True
            if _RM_DENY_RE.search(seg_norm_text) and result.deny_kind is None:
                result.deny_kind = "rm -r/-f (recursive or force)"
            continue

        if norm_head in ("chmod", "chown"):
            if _RECURSIVE_FLAG_RE.search(seg_norm_text):
                result.is_chmod_chown_r = True
                if result.deny_kind is None:
                    result.deny_kind = "chmod/chown -R (recursive)"
            continue

        if norm_head == "machine-local":
            # See module comment above `_MACHINE_LOCAL_WRITE_SUBCOMMANDS`
            # (MACHINE-LOCAL REGISTRY WRITE DENY). The subcommand is
            # `working[1]` -- the CLI's own subparser has no top-level
            # option that precedes it (see `_machine_local.py main()`), so
            # this is an exact-token check, not a free-text search.
            result.is_machine_local = True
            if len(working) >= 2 and working[1] in _MACHINE_LOCAL_WRITE_SUBCOMMANDS:
                if result.deny_kind is None:
                    result.deny_kind = f"machine-local {working[1]} (registry write)"
            continue

        if norm_head == "xargs":
            result.is_wrapper = True
            if result.deny_kind is None:
                result.deny_kind = (
                    "xargs <cmd> (command assembled from stdin -- indirection wrapper)"
                )
            continue

        if norm_head in _SOURCE_VERBS:
            # Finding #5.
            result.is_wrapper = True
            if result.deny_kind is None:
                result.deny_kind = (
                    f"{norm_head} <file> (script sourced in-process -- "
                    "indirection wrapper, script content unexamined)"
                )
            continue

        if norm_head == "eval":
            # Finding #6.
            result.is_wrapper = True
            if len(working) < 2:
                ev_verdict: Optional[str] = "eval with no operand (fails closed)"
            else:
                operand_text = " ".join(working[1:])
                if operand_text.startswith("$"):
                    ev_verdict = (
                        "eval $VAR/$(...) (unresolved operand -- indirection wrapper)"
                    )
                else:
                    nested = _evaluate_tokenized(operand_text, depth + 1)
                    ev_verdict = (
                        f"eval '<inline>' -> {nested.deny_kind}"
                        if nested.deny_kind
                        else None
                    )
            if ev_verdict is not None and result.deny_kind is None:
                result.deny_kind = ev_verdict
            continue

        if norm_head in _SHELL_FILE_INTERPRETERS and pipe_before:
            # Finding #4: bare interpreter fed via stdin pipe -- deny
            # regardless of token count, since piped content is never
            # present in the command text.
            result.is_wrapper = True
            if result.deny_kind is None:
                result.deny_kind = (
                    f"{norm_head} (bare interpreter fed via stdin pipe -- "
                    "indirection wrapper, piped content unexamined)"
                )
            continue

        if norm_head in _C_FLAG_INTERPRETERS:
            result.is_wrapper = True
            # Bug fix (2026-07-21): a parse-only `-n`/`--noexec` syntax
            # check executes NOTHING, so it is safe regardless of the
            # target file's content -- checked BEFORE the `-c`/bare-file
            # classification below so `bash -n foo.sh` allows outright
            # (mirrors the identical fix in `_evaluate_wrapper_indirection`
            # above; kept as two call sites, not refactored into one, since
            # this tokenized pass is deliberately additive/parallel to the
            # raw-text pass per this section's module comment).
            if norm_head in _SHELL_FILE_INTERPRETERS and _has_noexec_flag_before_script(
                working[1:]
            ):
                continue
            c_flag_positions = [
                i for i in range(1, len(working)) if _BUNDLED_C_FLAG_RE.match(working[i])
            ]
            if c_flag_positions:
                idx = c_flag_positions[0]
                if idx + 1 < len(working):
                    inline_payload = working[idx + 1]
                else:
                    inline_payload = " ".join(working[idx + 1:]) or seg_norm_text
                nested = _evaluate_tokenized(inline_payload, depth + 1)
                if nested.deny_kind is not None and result.deny_kind is None:
                    result.deny_kind = f"{norm_head} -c '<inline>' -> {nested.deny_kind}"
                continue
            if norm_head in _SHELL_FILE_INTERPRETERS and len(working) >= 2:
                if result.deny_kind is None:
                    result.deny_kind = (
                        f"{norm_head} <file> (interpreter-invoked script -- "
                        "indirection wrapper, script content unexamined)"
                    )
                continue
            continue

        if was_env_wrapped:
            result.is_wrapper = True
            nested = _evaluate_tokenized(seg_norm_text, depth + 1)
            if nested.deny_kind is not None and result.deny_kind is None:
                result.deny_kind = nested.deny_kind
            continue

    return result


#: Heredoc operator + delimiter word (`<<EOF`, `<< EOF`, `<<-EOF`, `<<'EOF'`,
#: `<<"EOF"`, `<<\EOF`). A heredoc BODY is stdin DATA, never shell command
#: tokens, so the destructive-verb probes must not scan it -- prose containing
#: the bare word `git`/`rm`/`chmod` inside a benign `cat > file <<EOF … EOF`
#: write is data, not a git command (2026-07-23 false-positive: a
#: code-reviewer's findings prose mentioning "git" was denied as an
#: "unrecognized git verb"). Herestrings (`<<<`) have no body and are
#: intentionally NOT matched.
#: The optional leading `\` (`\\?`) recognizes the POSIX backslash-escaped
#: delimiter spelling (`<<\EOF`), equivalent in effect to `<<'EOF'` (disables
#: parameter/backtick expansion inside the body) -- a common way to write a
#: non-expanding heredoc that this regex previously missed entirely (no quote
#: character follows `<<`, so the old `(['\"]?)` group matched zero-width and
#: the next required char class `[A-Za-z_]` failed against the literal `\`).
#: Missing this meant such a heredoc's body was never stripped and was
#: rescanned as live command text -- the exact false-deny class this module's
#: heredoc-stripping was introduced to close, just for one untested spelling.
_HEREDOC_OP_RE = re.compile(r"<<-?\s*\\?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredoc_bodies(cmd: str) -> str:
    """Remove heredoc BODY lines (stdin data) from ``cmd``, keeping the
    command line that introduces each heredoc and dropping everything from the
    next line through the closing delimiter line (inclusive).

    A heredoc feeds literal data to a command's stdin; its content is never
    executed as shell commands, so the destructive-verb classifiers must not
    treat verbs appearing inside it as commands. Anti-bypass: this does NOT
    reduce protection against an interpreter fed BY a heredoc (``bash <<EOF …
    EOF``) -- after the body is stripped, the residual ``bash <<'EOF'`` still
    matches the wrapper probe and is denied as an interpreter-invoked-script
    indirection wrapper. Multiple heredocs queued on one line are consumed in
    order. Closing-delimiter match is line-stripped equality, which covers both
    ``<<`` and tab-stripping ``<<-`` for this compensating control's purposes.
    """
    lines = cmd.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        out.append(line)
        i += 1
        for delim in [m.group(2) for m in _HEREDOC_OP_RE.finditer(line)]:
            while i < n and lines[i].strip() != delim:
                i += 1
            if i < n:  # consume the closing delimiter line too
                i += 1
    return "\n".join(out)


def _extract_command(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("command") or ""


def _evaluate_git_segment(seg: str, strict: bool = True) -> Optional[str]:
    """Evaluate ONE git-bearing command segment. Tries the subcommand-
    ANCHORED classifier first (`_git_subcommand_for_segment` +
    `_evaluate_git_segment_anchored` -- see the "SUBCOMMAND-ANCHORED
    CLASSIFICATION" module comment above); falls back to
    `_evaluate_git_segment_legacy`'s free-text classifier ONLY when
    `shlex` cannot tokenize the segment (fail CLOSED on parse failure --
    an unparseable segment must never become newly allowed).

    COMMAND-POSITION GIT-TOKEN FIX: in ``strict`` mode (default -- see
    `_git_subcommand_and_remaining_for_segment`'s ``strict`` parameter),
    when that function DEFINITIVELY determines this segment does not invoke
    git in command position (`_NOT_A_GIT_INVOCATION` sentinel -- e.g. "git"
    appears only inside a quoted regex alternation, as a grep pattern
    operand, or as part of a filename like `git-crypt`), this returns
    ``None`` (allow) directly WITHOUT falling over to the legacy free-text
    classifier. Routing a confirmed-non-invocation to legacy was the root
    cause of a live false positive: legacy's own default-deny fires on the
    mere free-text presence of the word "git" in the segment, regardless of
    whether git is actually the invoked command. ``strict=False`` (passed
    only from `_unwrap_and_classify`'s recursive indirection-payload scan,
    where the "segment" may be non-shell text like python source) disables
    this shortcut and preserves the original full-token-scan behavior.

    INDIRECTION-PAYLOAD CATCHALL FIX (2026-08-15, this change -- AC5/AC6 of
    docs/plans/2026-08-15-the-close-s-three-deferred-defects-becom.md C3):
    confirmed live -- a read-only `python3 -c "print(open('notes.txt')
    .read().count('git'))"` probe (the reported incident: a review-
    integrator's file-read probe, no git subprocess attempted or
    attemptable) was DENIED. Root cause: `strict=False`'s own token scan
    (in `_git_subcommand_and_remaining_for_segment`) never finds a token
    whose basename equals exactly `git` here (`shlex.split` glues the
    payload into one non-`git`-shaped token, since Python source is not
    shell syntax), so it returns `parse_ok=False` -- indistinguishable, at
    this call site, from a GENUINELY unparseable payload -- and both route
    to `_evaluate_git_segment_legacy`, whose free-text scan has NO "found
    nothing destructive-shaped" allow branch: it falls all the way to its
    own terminal "unrecognized git verb (default-deny)" line whenever the
    segment merely CONTAINS the word "git" (the `\\bgit\\b` gate in
    `_unwrap_and_classify`) without matching any of the specific
    destructive-verb patterns above it (push --force, rebase, reset --hard,
    stash pop/apply, ...). That terminal catchall is correct default-deny
    posture for a REAL shell segment (`strict=True`'s only path here is a
    genuinely unparseable shell command, where failing closed on an
    unrecognized verb is exactly the intended "novel verb" protection) --
    it is NOT correct for arbitrary python source that merely mentions
    "git" as data, since there `\\bgit\\b` presence carries no invocation
    meaning at all.

    Fix: `strict` (already known here, no new state needed) is threaded
    into `_evaluate_git_segment_legacy` as `default_deny_on_unmatched`.
    `strict=True` keeps the terminal catchall (unchanged fail-closed
    behavior for a genuinely unparseable REAL shell segment). `strict=False`
    (the `-c`-payload recursive scan) suppresses ONLY that terminal
    catchall -- every specific destructive-verb pattern above it in
    `_evaluate_git_segment_legacy` (push --force, rebase, reset --hard,
    commit --amend, branch -D, stash pop/apply/drop/clear, stash unscoped,
    tag -d, reflog delete/expire, filter-branch/filter-repo, clean -fd,
    worktree <mutate>, remote <mutate>, checkout <pathspec>, restore
    <worktree>) is UNCHANGED and still fires on free-text co-occurrence,
    since none of those checks depend on this catchall -- see
    `test_indirection_python_inline_c_destructive_payload_denies` (still
    green: `subprocess.run(["git", "push", "--force"])` is caught by the
    push+--force free-text pattern, not the catchall this narrows).
    """
    subcmd, parse_ok, remaining = _git_subcommand_and_remaining_for_segment(
        seg, strict=strict
    )
    if not parse_ok:
        # Suppress legacy's terminal catchall for either (a) the recursive
        # `-c`-payload scan (`strict=False`), or (b) the INLINE-INTERPRETER
        # CARVE-OUT sentinel (a `-c`-flagged interpreter head even in
        # `strict=True` mode -- `_INTERPRETER_C_PAYLOAD_AMBIGUOUS`) -- both
        # are non-shell-syntax payload text, not a real shell segment. A
        # genuinely unparseable REAL shell segment (`subcmd is None` here)
        # keeps the catchall unchanged.
        default_deny_on_unmatched = strict and subcmd is not _INTERPRETER_C_PAYLOAD_AMBIGUOUS
        return _evaluate_git_segment_legacy(
            seg, default_deny_on_unmatched=default_deny_on_unmatched
        )
    if subcmd is _NOT_A_GIT_INVOCATION:
        return None
    return _evaluate_git_segment_anchored(seg, subcmd, remaining)


def _evaluate_git_segment_legacy(
    seg: str, default_deny_on_unmatched: bool = True
) -> Optional[str]:
    """Evaluate ONE git-bearing command segment (reference hook 290-447).

    Returns the ``_DENY_KIND`` label if this segment denies, else ``None``
    (segment allowed -- caller continues to the next segment). Each check
    below is an INDEPENDENT ``if`` (not elif) mirroring the bash structure:
    a matched keyword that does not itself satisfy the deny sub-condition
    falls through to the next independent check, exactly as the bash
    predecessor's sequential (non-exclusive) `if` blocks do.

    Kept byte-for-byte as the ORIGINAL free-text classifier -- this is the
    "today's behavior" fallback the module comment above requires when a
    segment cannot be shlex-tokenized. Do not add the new read-only verbs
    (`merge-base`, `grep`, ...) here; that widening is anchored-path-only.

    ``default_deny_on_unmatched`` (2026-08-15, see `_evaluate_git_segment`'s
    "INDIRECTION-PAYLOAD CATCHALL FIX" docstring entry for the full
    rationale): governs ONLY the terminal fallback line at the very end of
    this function. ``True`` (default -- the `strict=True`/genuinely-
    unparseable-real-shell-segment caller) preserves the original
    "unrecognized git verb (default-deny)" catchall unchanged. ``False``
    (the `strict=False`/`-c`-payload indirection-scan caller) returns
    ``None`` (allow) instead of that catchall -- every specific
    destructive-verb check ABOVE the catchall is unaffected either way.
    """
    # --- WORKING-TREE-CLOBBER DENY SET (checked before the general
    # verb-allowlist walk) ---------------------------------------------
    if _CHECKOUT_KEYWORD_RE.search(seg):
        if _CHECKOUT_DASHDASH_RE.search(seg) or _CHECKOUT_PATHSPEC_RE.search(seg):
            return "git checkout <pathspec>"

    if _RESTORE_KEYWORD_RE.search(seg):
        if _RESTORE_WORKTREE_RE.search(seg) or not _RESTORE_STAGED_RE.search(seg):
            return "git restore (working tree)"

    if _RESET_KEYWORD_RE.search(seg):
        reset_after = _RESET_STRIP_PREFIX_RE.sub(" ", seg, count=1)
        if _RESET_REF_RE.search(reset_after) and not _DASHDASH_SEP_RE.search(reset_after):
            return "git reset <prior-ref>"

    if _STASH_POP_APPLY_RE.search(seg):
        return "git stash pop/apply"

    # 2026-07-26 fix (module docstring "UNSCOPED-STASH GAP CLOSE"): bare
    # `git stash`, the IMPLICIT-push flag-only form (`git stash -u`), and
    # explicit `git stash push` all sweep every uncommitted change on the
    # shared tree unless scoped with a `--`-delimited pathspec. `pop`/
    # `apply` already returned above; `drop`/`clear` return later (own
    # check, unaffected); `list`/`show`/`branch`/`create`/`store`/`save`
    # (`_STASH_OTHER_ACTION_WORD_RE`) are a different subcommand entirely
    # and fall through unchanged.
    if (
        _STASH_WORD_RE.search(seg)
        and not _STASH_OTHER_ACTION_WORD_RE.search(seg)
        and not _STASH_DROP_CLEAR_RE.search(seg)
        and not _DASHDASH_SEP_RE.search(seg)
    ):
        return "git stash (unscoped)"

    # --- Incident-class verbs -- unconditional deny ---------------------
    if _REBASE_RE.search(seg):
        return "git rebase"
    if _HARD_FLAG_RE.search(seg) and _RESET_WORD_RE.search(seg):
        return "git reset --hard"
    if _COMMIT_WORD_RE.search(seg) and _AMEND_FLAG_RE.search(seg):
        return "git commit --amend"
    if _PUSH_WORD_RE.search(seg):
        if _PUSH_FORCE_RE.search(seg):
            return "git push --force"
    if _BRANCH_WORD_RE.search(seg):
        if _BRANCH_DASH_D_UPPER_RE.search(seg):
            return "git branch -D"
        if _DASH_D_OR_DELETE_RE.search(seg) and _DASH_F_OR_FORCE_RE.search(seg):
            return "git branch -d --force"
    if _STASH_DROP_CLEAR_RE.search(seg):
        return "git stash drop/clear"
    if _TAG_WORD_RE.search(seg) and _DASH_D_OR_DELETE_RE.search(seg):
        return "git tag -d"
    if _REFLOG_WORD_RE.search(seg) and _REFLOG_ACTION_RE.search(seg):
        return "git reflog delete/expire"
    if _FILTER_BRANCH_REPO_RE.search(seg):
        return "git filter-branch/filter-repo"
    if _CLEAN_WORD_RE.search(seg) and _CLEAN_FD_RE.search(seg):
        return "git clean -fd(x)"
    if _WORKTREE_MUTATE_RE.search(seg):
        return "git worktree <mutate>"
    if _REMOTE_MUTATE_RE.search(seg):
        return "git remote <mutate>"

    # P2(b) fix (2026-07-25): fail CLOSED on an unrecognized worktree/remote
    # second-level form instead of falling through to `_SAFE_VERB_RE`'s
    # generic bare-verb match (worktree/remote are no longer in that set --
    # see its comment above). These ALWAYS return -- allow or deny -- so a
    # worktree/remote segment can never reach the generic allowlist below
    # uninspected.
    worktree_match = _WORKTREE_WORD_RE.search(seg)
    if worktree_match is not None:
        next_word = _NEXT_WORD_AFTER_RE.match(seg, worktree_match.end())
        if next_word is None or next_word.group(1) in _LEGACY_WORKTREE_READONLY:
            return None
        return "unrecognized git worktree subcommand (default-deny)"

    remote_match = _REMOTE_WORD_RE.search(seg)
    if remote_match is not None:
        next_word = _NEXT_WORD_AFTER_RE.match(seg, remote_match.end())
        if next_word is None or next_word.group(1) in _LEGACY_REMOTE_READONLY:
            return None
        return "unrecognized git remote subcommand (default-deny)"

    # --- SAFE-FORWARD ALLOWLIST -- reached only if none of the above
    # matched. Any git verb not enumerated here is DEFAULT-DENIED: the
    # load-bearing "novel verb denied for not being allowlisted" property.
    if _SAFE_VERB_RE.search(seg):
        return None
    if _PULL_WORD_RE.search(seg):
        if _FF_ONLY_RE.search(seg):
            return None
        return "git pull (not --ff-only)"
    if _MERGE_WORD_RE.search(seg):
        if _FF_ONLY_RE.search(seg):
            return None
        return "git merge (not --ff-only)"
    if _PUSH_WORD_RE.search(seg):
        # Forcing forms already denied above; anything else on push is safe.
        return None
    if _SWITCH_WORD_RE.search(seg):
        return None
    if _BRANCH_WORD_RE.search(seg):
        return None
    if _STASH_WORD_RE.search(seg):
        return None
    if _RESTORE_KEYWORD_RE.search(seg):
        return None
    if _TAG_WORD_RE.search(seg):
        return None
    if _CONFIG_WORD_RE.search(seg):
        if _CONFIG_GET_RE.search(seg):
            return None
        return "git config (not --get)"

    if not default_deny_on_unmatched:
        # strict=False caller (`-c`-payload indirection scan): the segment
        # mentions "git" as free text but matched none of the specific
        # destructive-verb patterns above -- no argv-shaped git invocation
        # was ever confirmed here (see `_evaluate_git_segment`'s docstring
        # entry). Allow rather than default-deny on bare word presence.
        return None

    return "unrecognized git verb (default-deny)"


def _evaluate_git_surface(cmd_norm: str, strict: bool = True) -> Optional[str]:
    """Split ``cmd_norm`` into command segments, per-segment evaluation,
    first-match-wins (reference hook 279-449). ``strict`` is threaded to
    `_evaluate_git_segment` unchanged -- see that function and
    `_git_subcommand_and_remaining_for_segment` for the strict/non-strict
    contract; `_unwrap_and_classify` is the sole ``strict=False`` caller.

    QUOTE-AWARE SPLIT FIX (2026-07-28, part of the "COMMAND-POSITION
    GIT-TOKEN FIX" module docstring entry): confirmed live -- a subagent's
    `grep -E "(status|git|push)" somefile.md` (a regex ALTERNATION inside a
    double-quoted grep pattern, containing literal `|` characters that are
    shell-quoted data, not command separators) was denied. Root cause: this
    function used to split on a naive `re.split(r"[;&|\\n]+", cmd_norm)` --
    raw TEXT split, blind to quoting -- so the `|` characters INSIDE the
    quoted pattern were treated as real pipe separators, shattering the
    single command into bogus segments, one of which was the bare word
    `git` on its own. That fake segment then hit the anchored classifier's
    default-deny for an "invocation" that never happened.

    Reuses the SAME quote-aware tokenizer the TOKENIZED AUTHORITATIVE PASS
    already relies on (`_tokenize_full_command`/`_segments_from_tokens`,
    also used by `_evaluate_wrapper_indirection`) instead of a parallel
    naive split: a quoted `;`/`&`/`|` stays attached to its word, an
    unquoted one still yields a genuine segment boundary. Each token segment
    is reconstructed via `shlex.quote` per token (lossless round-trip, same
    technique already used in `_evaluate_tokenized`/`_evaluate_wrapper_
    indirection`) before being handed to `_GIT_WORD_RE`/`_evaluate_git_
    segment`, which re-tokenizes it identically.

    Falls back to the original raw-text split ONLY when the whole command
    is unparseable by `_tokenize_full_command` (unterminated quote/trailing
    backslash) -- the same fail-CLOSED posture as every other unparseable-
    command path in this module (never silently allow on a parse failure).
    """
    tokens = _tokenize_full_command(cmd_norm)
    if tokens is None:
        segments = re.split(r"[;&|\n]+", cmd_norm)
        for seg in segments:
            if not seg.strip():
                continue
            if not _GIT_WORD_RE.search(seg):
                continue
            verdict = _evaluate_git_segment(seg, strict=strict)
            if verdict is not None:
                return verdict
        return None

    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        seg = " ".join(shlex.quote(tok) for tok in seg_tokens)
        if not _GIT_WORD_RE.search(seg):
            continue
        verdict = _evaluate_git_segment(seg, strict=strict)
        if verdict is not None:
            return verdict
    return None


def _git_mv_corrected_command(cmd: str) -> Optional[str]:
    """(2026-07-29, duty-of-care promotion) Reconstruct the concrete, safe
    ``mv`` invocation for a denied ``git mv`` command, using the caller's
    OWN real source/dest paths -- rather than making the caller re-derive
    the generic ``mv A B`` template shown in ``_build_reason`` by hand.

    Reuses ``_git_subcommand_and_remaining_for_segment`` (the same
    shlex-based tokenizer the git-verb classifier itself is built on, not a
    hand-rolled parser) to pull the already-tokenized argv AFTER the ``mv``
    subcommand, drops any ``-``-prefixed flags (git mv's own flags --
    ``-f``/``-k``/``-n``/``-v`` -- have no bearing on a plain filesystem
    ``mv``, which always overwrites/never-warns by default; a flag-bearing
    invocation is rare enough that showing the flag-free positional form is
    still the right default), and re-quotes each remaining path operand via
    ``shlex.quote`` so a space or shell-special character in a real path
    round-trips safely.

    ``--`` END-OF-OPTIONS is honored (2026-07-30, M13/M19 review fix): a
    bare ``--`` token -- git's (and every POSIX getopt-style tool's) own
    end-of-options marker -- stops the flag filter; every token AFTER it is
    kept as a real operand regardless of a leading ``-``, mirroring how
    ``git mv -- -weird-file.txt dest`` actually behaves. Before the
    scan reaches ``--`` (or if the segment has none), a ``-``-prefixed
    token is still treated as a flag, the same posture as before this fix
    -- the defect this closes was dropping a REAL hyphen-led path operand
    that never had a ``--`` to disambiguate it, not tightening the ordinary
    flag-filtering case.

    Returns ``None`` -- never a guessed-at command -- when the segment
    doesn't parse cleanly as a git invocation, isn't actually ``mv``, or
    has fewer than two path operands (a malformed/incomplete ``git mv`` the
    real git CLI would itself reject); the caller falls back to the
    generic template in that case rather than asserting a fabricated fix.
    """
    subcmd, parse_ok, remaining = _git_subcommand_and_remaining_for_segment(cmd)
    if not parse_ok or subcmd != "mv" or not remaining:
        return None
    operands: List[str] = []
    past_end_of_options = False
    for tok in remaining:
        if not past_end_of_options and tok == "--":
            past_end_of_options = True
            continue
        if not past_end_of_options and tok.startswith("-") and tok != "-":
            continue
        operands.append(tok)
    if len(operands) < 2:
        return None
    return "mv " + " ".join(shlex.quote(tok) for tok in operands)


def _is_opaque_indirection(wrapper_verdict: str) -> bool:
    """True when `wrapper_verdict` names a wrapper shape whose PAYLOAD this
    guard genuinely never saw (a script file's content, xargs-assembled
    stdin, an unparseable/too-deep wrapper, or an unresolved `$VAR`/`$(...)`
    reference) -- the over-block C18d/census flips to advisory. False for a
    `-c`/`eval`-recursed shape where `_unwrap_and_classify`/
    `_evaluate_tokenized` actually unwrapped a REAL destructive git/rm/
    chmod-chown-R/machine-local match inside the payload -- those embed a
    `' -> '` arrow to the resolved deny_kind (e.g. "bash -c '<inline>' ->
    rm -r/-f (recursive or force)") and stay hard-denied: the destructive
    core the guard actually examined and confirmed, not an over-block.
    """
    return " -> " not in wrapper_verdict


def _build_indirection_advisory_reason(wrapper_shape: str) -> str:
    """Short (Axis-A, <220-byte-prose-cap) advisory for an OPAQUE
    indirection wrapper (see `_is_opaque_indirection`) -- the guard cannot
    see the payload, so it no longer blocks it outright; it only flags the
    shape. No override note: this guard has none, by design (module
    docstring "OVERRIDE-WITHHOLDING")."""
    return (
        "ADVISORY: not blocked. This is an indirection wrapper whose "
        "payload is opaque to this guard's text classifier -- it may be "
        "entirely benign.\n\n"
        f"  Shape: {wrapper_shape}\n\n"
        "Genuinely destructive? Surface it to the EM instead of running it."
    )


def _build_reason(
    deny_kind: str, agent_id: str, effective_type: str, cmd_safe: str, cmd: str = ""
) -> str:
    if deny_kind == "ambiguous-identity":
        return (
            "BLOCKED: ambiguous agent identity (canonical-id collision) — failing closed.\n\n"
            f"  Canonical id: {agent_id}\n"
            "  Recorded type: AMBIGUOUS (collision sentinel in dispatched-agents.txt)\n"
            f"  Command:       {cmd_safe}\n\n"
            "Two dispatches sharing the same canonical id arrived with DIFFERENT\n"
            "subagent_types. This guard denies destructive-surface Bash unconditionally\n"
            "for any AMBIGUOUS-resolved identity — surface this to the EM."
        )
    if deny_kind.startswith("indirection: "):
        wrapped = deny_kind[len("indirection: "):]
        return (
            "BLOCKED: this wrapper's payload is opaque to this guard's text\n"
            "classifier, not indirection at large — `bash <script>` / `sh -c '...'`\n"
            "/ `xargs` / `env ... <cmd>` are refused because the guard cannot see\n"
            "what they will run.\n\n"
            f"  Subagent: {agent_id} ({effective_type})\n"
            f"  Wrapper:  {wrapped}\n"
            f"  Command:  {cmd_safe}\n\n"
            "This guard classifies destructive git/rm/chmod-chown-R operations by\n"
            "literal command text (docs/plans/2026-07-13-subagent-destructive-action-\n"
            "em-lock.md). Wrapping a command through an interpreter/xargs/env\n"
            "indirection hides that text from the guard even when the wrapped\n"
            "command is entirely benign — this is a deliberate over-block, not a\n"
            "false positive to work around.\n\n"
            "Safe forward paths — for the WORK, never for the blocked operation:\n"
            "  - Multi-statement payload (the reason you reached for `-c`/a script)?\n"
            "    Write it to a file under your session scratchpad and invoke it as\n"
            "    `python3 <path>` — a bare python3-file invocation is NOT an\n"
            "    indirection wrapper to this guard (content unexamined, by design,\n"
            "    same as any other file you write and then read yourself).\n"
            "    `bash <path>` / `sh <path>` / `zsh <path>` do NOT work — those are\n"
            "    refused outright, with no equivalent carve-out.\n"
            "    This route exists for benign multi-statement work (batched probes,\n"
            "    a computation too long for one line). Putting the destructive\n"
            "    operation you were just blocked from into that file is the\n"
            "    fight-the-hook evasion named at the bottom of this message, not a\n"
            "    use of this carve-out — the guard cannot see it, which is exactly\n"
            "    why the honesty of not doing it is on you.\n"
            "  - Running a SHELL script instead? Read it and run the command directly\n"
            "    in Bash calls, one at a time — not via `bash <script>` / `sh -c`\n"
            "    indirection.\n"
            "  - Building a command from stdin? Compute the argument list yourself\n"
            "    and run the target command directly instead of piping into xargs.\n"
            "  - Needed the wrapper for a legitimately destructive op? Surface it to\n"
            "    the EM — the main-loop is never blocked by this guard.\n\n"
            "There is NO subagent-honored override for this guard — evading it via a\n"
            "further layer of indirection is doctrine-forbidden fight-the-hook\n"
            "behavior, not a workaround (coordinator/CLAUDE.md § Self-Improvement Loop)."
        )
    if deny_kind == "git mv (index-mutating rename)":
        corrected = _git_mv_corrected_command(cmd)
        if corrected is not None:
            safe_path_step = (
                f"  - Run it now, with your own paths already substituted in:\n"
                f"      {corrected}\n"
                "    Then name both paths in your report back to the EM."
            )
        else:
            safe_path_step = (
                "  - Move the file with plain `mv A B`, then name both paths in your\n"
                "    report back to the EM."
            )
        return (
            "BLOCKED: `git mv` stages the rename, and staging is EM-only.\n\n"
            f"  Subagent: {agent_id} ({effective_type})\n"
            f"  Denied:   {deny_kind}\n"
            f"  Command:  {cmd_safe}\n\n"
            "`git mv A B` is `mv A B` plus `git add A B`. Only the second half is\n"
            "withheld from you: on a shared tree, a subagent writing the index is\n"
            "what lets a peer's next bare `git commit` absorb work nobody reviewed.\n\n"
            "Safe forward path — this is the INTENDED route, not a workaround:\n"
            f"{safe_path_step}\n"
            "    Git detects renames by content similarity at commit time, so the\n"
            "    EM's own `git add A B` records the rename identically to `git mv`.\n"
            "    You lose nothing by using `mv`.\n\n"
            "A per-dispatch carve-out for `git mv` was considered and declined\n"
            "2026-07-28 for this reason — a brief cannot grant it, because there is\n"
            "nothing to grant."
        )
    if deny_kind == "git stash pop/apply":
        return (
            "BLOCKED: `git stash pop`/`git stash apply` — this is a STACK-POSITION\n"
            "operation (it always acts on `stash@{0}` unless you name a different\n"
            "index), so on a shared tree with concurrent sessions it can restore a\n"
            "SIBLING session's stash instead of your own, and a conflicted pop can\n"
            "clobber uncommitted work that landed after you pushed. There is no\n"
            "scoped/pathspec form of pop/apply that would make this safe to allow.\n\n"
            f"  Subagent: {agent_id} ({effective_type})\n"
            f"  Denied:   {deny_kind}\n"
            f"  Command:  {cmd_safe}\n\n"
            "You almost certainly don't need pop/apply at all — the common reason\n"
            "to reach for it is proving a test failure is pre-existing (see executor\n"
            "doctrine § Pre-Existing-Failure Verification), and that recipe no longer\n"
            "uses stash:\n"
            "  - Save your edit:      `cp <path> <path>.your-wip.bak` (plain cp)\n"
            "  - Get the old content: `git show <ref>:<path> > <path>` (`show` is\n"
            "    already on the safe-forward allowlist — no stash needed at all)\n"
            "  - Run the test, then restore: `cp <path>.your-wip.bak <path>`\n"
            "  - Clean up: `rm <path>.your-wip.bak` (bare rm, no -r/-f, unaffected\n"
            "    by this guard)\n"
            "This never touches the stash stack, so there is nothing to pop and\n"
            "nothing that can collide with a sibling session's work.\n\n"
            "Already have a stash entry you need restored (e.g. from before this\n"
            "guard existed)? Read it out instead of popping it: `git show\n"
            "stash@{N}:<path> > <path>` for each path the stash holds (`show`\n"
            "again, not `pop`) — then surface the now-orphaned stash entry to the\n"
            "EM for disposal (drop/clear are also EM-only, for the same reason).\n\n"
            "There is NO subagent-honored override for this guard — the lock keys on\n"
            "resolved caller-context, never on an env var, by design."
        )
    if deny_kind == "git stash drop/clear":
        return (
            "BLOCKED: `git stash drop`/`git stash clear` — permanently discards a\n"
            "stash entry, and on a shared tree you cannot tell whether the entry at\n"
            "that stack position is one you own or a sibling session's un-recovered\n"
            "work; dropping the wrong one is unrecoverable via normal git commands.\n\n"
            f"  Subagent: {agent_id} ({effective_type})\n"
            f"  Denied:   {deny_kind}\n"
            f"  Command:  {cmd_safe}\n\n"
            "Safe forward paths:\n"
            "  - Recovered a stash's content already (via `git show stash@{N}:<path>`)\n"
            "    and just want the now-useless entry gone? Surface it to the EM —\n"
            "    the main-loop is never blocked by this guard and can drop it after\n"
            "    confirming no sibling session still needs it.\n"
            "  - Never needed the stash in the first place? Use the no-stash\n"
            "    pre-existing-failure recipe instead (see the `stash pop/apply`\n"
            "    deny message, or executor doctrine § Pre-Existing-Failure\n"
            "    Verification) so no entry is created to clean up later.\n\n"
            "There is NO subagent-honored override for this guard — the lock keys on\n"
            "resolved caller-context, never on an env var, by design."
        )
    if deny_kind == "git stash (unscoped)":
        return (
            "BLOCKED: unscoped `git stash`/`git stash push` — this sweeps EVERY\n"
            "uncommitted change on the shared tree, including sibling sessions'\n"
            "in-flight work, into a stash owned by this subagent alone.\n\n"
            f"  Subagent: {agent_id} ({effective_type})\n"
            f"  Denied:   {deny_kind}\n"
            f"  Command:  {cmd_safe}\n\n"
            "Safe forward paths:\n"
            "  - Need a clean baseline before editing YOUR OWN files? Scope it:\n"
            "    `git stash push -- <your own touched paths>` (a `--`-delimited\n"
            "    pathspec is required, and must name only files you authored).\n"
            "  - Just need a file's pre-edit content? Skip stash entirely:\n"
            "    `git show HEAD:<path>` into your scratchpad instead.\n"
            "  - Need a full-tree stash for a legitimate reason? Surface it to\n"
            "    the EM — the main-loop is never blocked by this guard.\n\n"
            "This is the caller-context lock realizing docs/plans/2026-07-13-subagent-\n"
            "destructive-action-em-lock.md: a subagent cannot reason about the shared\n"
            "branch's blast radius. Confirmed live (2026-07-26): an executor stashed\n"
            "unscoped mid-work, hit a quota limit, and died holding the stash — every\n"
            "other live session's uncommitted work was reverted on disk until the\n"
            "stash was found and popped. It survived only because that stash\n"
            "happened to be narrow.\n\n"
            "There is NO subagent-honored override for this guard — the lock keys on\n"
            "resolved caller-context, never on an env var, by design."
        )
    if deny_kind.startswith("machine-local "):
        return (
            "BLOCKED: machine-local write subcommands are EM-locked (DR-125) -- "
            "they mutate a registry other tooling trusts for path resolution.\n\n"
            f"  Subagent: {agent_id} ({effective_type})\n"
            f"  Denied:   {deny_kind}\n"
            f"  Command:  {cmd_safe}\n\n"
            "Use instead:\n"
            "    machine-local get/has/keys/path/dir\n\n"
            "Need to write a key? Surface it to the EM -- the main-loop is never\n"
            "blocked by this guard. There is NO subagent-honored override -- but note\n"
            "this is a speed bump at the shell surface, not a capability boundary: a\n"
            "subagent that can run Python can still write the registry directly,\n"
            "unseen by this guard. Fix forward, or surface a genuine need to the EM."
        )
    if deny_kind == "rm -r/-f (recursive or force)":
        return (
            "BLOCKED: subagents may not run destructive rm operations — EM-locked.\n\n"
            f"  Subagent: {agent_id} ({effective_type})\n"
            f"  Denied:   {deny_kind}\n"
            f"  Command:  {cmd_safe}\n\n"
            "This is the caller-context lock realizing docs/plans/2026-07-13-subagent-\n"
            "destructive-action-em-lock.md: a subagent cannot reason about the shared\n"
            "branch's blast radius.\n\n"
            "`rm -r`/`rm -f` (recursive or force) is denied OUTRIGHT for subagents —\n"
            "including against your own session's scratchpad. There is no sandbox-\n"
            "scoped, path-based, or any other exception this guard will honor.\n\n"
            "Safe forward paths:\n"
            "  - Cleaning up your OWN scratchpad? You almost certainly do not need\n"
            "    this command. Unflagged `rm <file>` and `rmdir <dir>` are permitted\n"
            "    and are enough for throwaway files you created. Do NOT escalate\n"
            "    scratch tidiness to the EM: the scratchpad is outside every repo,\n"
            "    nothing reads it after you exit, and an interrupt to buy neatness\n"
            "    there costs the one context this lock exists to protect. Leaving it\n"
            "    is a fine outcome.\n"
            "  - Need history rewrite, a shared-tree revert, or anything else\n"
            "    destructive to a REPO? Surface it to the EM — the main-loop is never\n"
            "    blocked by this guard.\n\n"
            "There is NO subagent-honored override for this guard — the lock keys on\n"
            "resolved caller-context, never on an env var, by design -- but note this\n"
            "is a speed bump at the shell surface, not a capability boundary: a\n"
            "subagent that can run Python can still remove files directly, unseen by\n"
            "this guard. Fix forward, or surface a genuine need to the EM."
        )
    return (
        "BLOCKED: destructive git/rm/chmod-chown -R is blocked at the shell surface\n"
        "for subagents.\n\n"
        f"  Denied: {deny_kind}\n"
        f"  Cmd:    {cmd_safe}\n\n"
        "This is a deliberate speed bump on shell-invoked git, not a capability\n"
        "boundary -- no shell-token matcher can constrain an interpreter, and a\n"
        "subagent that can run Python (or any other interpreter) can still reach\n"
        "git directly, unseen by this guard. It exists to make the destructive\n"
        "path cost conscious effort instead of a reflexive shell command, not to\n"
        "claim the action is unreachable.\n\n"
        "Fix forward, or surface it to the EM to run -- the main-loop is never\n"
        "blocked by this guard. This guard has no subagent-reachable override\n"
        "flag; routing around it via another interpreter is not defeating a\n"
        "real gate, so treat a genuine need for this exact operation as a\n"
        "signal to ask, not a puzzle to solve."
    )


# ---------------------------------------------------------------------------
# FAIL-OPEN OBSERVABILITY (2026-07-29 addition, pure addition -- see module
# docstring FAIL-POSTURE section for the three fail-open branches this
# instruments). Root cause: `check()` returning `None` (allow) writes
# nothing anywhere, so a Layer-1-flagged destructive command that then
# fails open at the identity gate leaves zero trace -- confirmed exploitable
# via ordinary, non-adversarial multi-repo dispatch (a subagent's Bash call
# lands in a repo other than the one it was dispatched from, so the
# per-repo back-pointer the SECONDARY leg reads was never written there).
# See DoE-claude state/audits/2026-07-29-destructive-git-guard-inconsistency.md.
#
# This section is OBSERVABILITY ONLY -- it never changes an allow/deny
# verdict. It only ever runs on a command Layer 1 has ALREADY flagged as
# touching a dangerous surface (git/rm/chmod-chown-R/wrapper-indirection),
# preserving the module's cost-gating design intent: an ordinary subagent
# Bash call (ls/grep/python/cat) never reaches this code, and it costs
# nothing extra beyond the log write itself -- no additional
# `resolve_git_root` subprocess spawn is introduced on the EM main-loop
# path (branch `no-agent-id-key` logs with git_root left unresolved rather
# than paying a spawn to fill it in, since that branch fires for the
# EM's own top-level Bash calls too and this module structurally never
# adds identity-resolution cost there).
# ---------------------------------------------------------------------------
_FAIL_OPEN_LOG_RELPATH = ("state", "destructive-guard-fail-open.log")
_NOT_ATTEMPTED = "<not-attempted>"


def _fail_open_log_path() -> Path:
    """Return the settings-home-rooted fail-open log path.

    Deliberately settings-home-rooted (machine-scoped), NOT
    `<repo_root>/state/...` like the sibling `detached_spawn`
    housekeeping-failures log: a fail-open event here is about THIS
    GUARD's own identity resolution, not about any one target repo --
    `git_root` is frequently `None` on the very branches this logs (the
    no-agent_id-key branch never even attempts to resolve one), and
    subagent traffic routinely spans many repos in one session. A single
    fixed location, independent of which repo the flagged command
    happened to target, is the only shape that can't itself go missing
    because the "wrong" repo lacked a `state/` directory. Kept as a
    SEPARATE log from `detached_spawn`'s `housekeeping-failures.log`
    (not folded into it) -- that log's grammar/verbs (`SPAWN FAILED`/
    `CHILD FAILED`) and its read-then-clear-on-orientation-regen
    lifecycle are specific to detached-CLI spawn bookkeeping; this is a
    security-guard record that should accumulate durably and not be
    silently truncated by an unrelated orientation-cache regen cycle.
    """
    return settings_home() / Path(*_FAIL_OPEN_LOG_RELPATH)


def _log_fail_open(
    branch: str,
    payload: Dict[str, Any],
    cmd: str,
    *,
    raw_agent_id: str = "",
    agent_id: str = "",
    git_root: Optional[str] = _NOT_ATTEMPTED,  # type: ignore[assignment]
    agent_type_present: bool = False,
    agent_type: str = "",
    subagent_type_present: bool = False,
    subagent_type: str = "",
) -> None:
    """Best-effort append a fail-open identity-resolution record to the
    settings-home fail-open log (see `_fail_open_log_path`).

    Records the full identity-resolution tuple the audit asked for: raw
    `agent_id` as received (vs. canonicalized), `session_id`, payload
    `cwd`, resolved `git_root` (or the `_NOT_ATTEMPTED` sentinel when this
    branch never tried to resolve one), the PRIMARY (`agent_type`) and
    SECONDARY (`subagent_type`) identity legs -- each distinguishing
    "absent from the payload"/"never computed" from "computed and empty"
    via its own `*_present` flag -- which of the three fail-open branches
    fired, and a safely-truncated command.

    NEVER raises (PreToolUse-hook contract, matching every sibling
    best-effort writer in this codebase, e.g.
    `coordinator_core.ops.ceremony.detached_spawn._log_spawn_failure`): a
    broken observability path must never become the thing that blocks a
    tool call. Any failure -- settings-home resolution, directory
    creation, the write itself -- is swallowed silently.
    """
    try:
        log_path = _fail_open_log_path()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        session_id = payload.get("session_id") or ""
        cwd = payload.get("cwd")
        fields = {
            "branch": branch,
            "raw_agent_id": raw_agent_id if raw_agent_id else "<absent>",
            "agent_id": agent_id if agent_id else "<unresolved>",
            "session_id": session_id if session_id else "<absent>",
            "cwd": cwd if cwd else "<absent>",
            "git_root": git_root if git_root is not None else "<none>",
            "agent_type": agent_type if agent_type_present else "<absent>",
            "subagent_type": subagent_type if subagent_type_present else "<not-computed>",
            "cmd": _sanitize_command(cmd),
        }
        parts = " ".join(f"{k}={v!r}" for k, v in fields.items())
        line = f"[{timestamp}] FAIL-OPEN {parts}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 -- observability must never raise into check()
        pass


def _sanitize_command(cmd: str) -> str:
    """Strip tab/CR/LF/FF/VT to a space,
    strip ASCII control chars 0x00-0x1F (``tr -d '\\000-\\037'`` -- 0x7F DEL
    is deliberately NOT stripped, matching the bash range), truncate to 200
    chars with a trailing ``...`` marker.
    """
    cmd_safe = re.sub(r"[\t\r\n\f\v]", " ", cmd)
    cmd_safe = "".join(ch for ch in cmd_safe if ord(ch) >= 0x20)
    if len(cmd_safe) > 200:
        cmd_safe = cmd_safe[:200] + "..."
    if not cmd_safe:
        cmd_safe = "(empty/unparseable)"
    return cmd_safe


# ---------------------------------------------------------------------------
# POWERSHELL-DIALECT DESTRUCTIVE-VERB CLASSIFIER (2026-08-07, this change --
# C4d of docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-
# silent.md; see docs/reference/guard-dialect-coverage.md row 16, "the
# expensive one"). A SEPARATE classifier from the Bash Layer 1/2 machinery
# above -- not a widening of it -- because the ~25 raw-text regexes above are
# anchored on literal Bash-shaped punctuation/quoting and, per the spike
# verdict record (docs/research/spike-verdicts/2026-08-07-powershell-guard-
# detection-and-tokenizer-mechanism.md, "shlex(posix=True) on PowerShell is
# actively wrong"), feeding PowerShell text through this module's `shlex`-
# based tokenizer (`_evaluate_tokenized`/`_tokenize_full_command`) mangles
# Windows paths and can raise outright. This classifier instead tokenizes via
# `_dialect.resolve_segments_for_dialect` (tree-sitter-pwsh), which already
# records SILENT on any parse failure/grammar gap (see `_dialect.py`'s own
# `_powershell_tokens` docstring) -- this module adds no second SILENT path
# for that case, it only adds verb/flag recognition on top of a tokenizer
# that already declines to rule when it cannot.
#
# `rm` IS included in `_PS_REMOVE_VERBS` below, despite C3 finding 1
# (guard-dialect-coverage.md "Scope and method") naming it as an
# already-covered alias collision: that finding's "already covered" claim
# rests on the Bash-leg raw-text `_RM_SURFACE_RE`/`_RM_DENY_RE` pair running
# unconditionally -- true when `tool_name == "Bash"`, but this classifier is
# reached ONLY for `Dialect.POWERSHELL` (a SEPARATE branch off `check()`,
# never falling through to the Bash-leg regexes at all -- see module comment
# above `check()`), so a real `tool_name == "PowerShell"` dispatch carrying
# `rm -Recurse -Force <path>` would otherwise see NEITHER classifier. Adding
# `rm` here is therefore genuinely new coverage for the PowerShell leg, not
# a duplicate of the Bash leg's (unreached, for this dialect) coverage --
# confirmed live via `test_powershell_rm_alias_denies_via_existing_dialect_
# neutral_probe`'s own name update / test comment. `mv`/`cp` are NOT added:
# neither Move-Item nor Copy-Item is in this guard's v1 destructive-surface
# scope (git/rm/chmod-chown-R only, per the module docstring's own Anti-scope
# note), so their PowerShell aliases carry no more coverage obligation here
# than the bare `mv`/`cp` binaries do on the Bash leg. This classifier covers
# the verbs the spike measured returning bare `None`: `Remove-Item`/`ri`/
# `rd`/`del`/`rm` (recursive-or-force delete), `icacls` (permission
# modification, always dangerous), and `Stop-Process` (process termination,
# always dangerous) -- see the dispatching brief's "Required work" list.
#
# FLAG-SHAPE: PowerShell has no clustered short flags (`rm -rf` cannot
# execute there at all -- confirmed live, "A parameter cannot be found that
# matches parameter name 'rf'") and parameter names PREFIX-MATCH, so a
# literal `-rf`/`-Recurse`/`-Force` matcher has zero recall. `_ps_has_flag`
# below matches a PREFIX SET (`-r*`, `-fo*`) case-insensitively, plus the
# legacy cmd.exe-alias slash flags (`/s`, `/f`) `rd`/`del` accept when
# PowerShell resolves them via its own cmd-compatibility shims.
_PS_REMOVE_VERBS = frozenset({"remove-item", "ri", "rd", "del", "erase", "rm"})
_PS_ICACLS_VERBS = frozenset({"icacls"})
_PS_STOP_PROCESS_VERBS = frozenset({"stop-process"})


def _ps_has_flag_prefix(tokens: List[str], dash_prefixes: tuple, slash_exact: frozenset) -> bool:
    """True if any of ``tokens`` is a `-`-prefixed flag whose lowercased text
    starts with one of ``dash_prefixes`` (PowerShell prefix-matching, e.g.
    `-r` matches `-Recurse`/`-r`; `-fo` matches `-Force`/`-fo`), OR an exact
    lowercased match in ``slash_exact`` (the legacy cmd.exe-alias slash-flag
    spelling `rd`/`del` accept, e.g. `/s`, `/f`).
    """
    for tok in tokens:
        low = tok.lower()
        if low.startswith("-") and any(low.startswith(p) for p in dash_prefixes):
            return True
        if low in slash_exact:
            return True
    return False


def _ps_has_positional_target(tokens: List[str]) -> bool:
    """True if ``tokens`` (the argv slice after a destructive verb) contains
    at least one token that is not itself a flag (`-`/`/`-prefixed) -- i.e. a
    genuine path/target operand appears in the token stream at all.

    Used to distinguish an ordinary direct invocation (`Remove-Item -Force
    <path>`, target present) from the OBJECT-PIPELINE BLIND SPOT (see
    `_evaluate_powershell_destructive`'s own docstring): `Get-ChildItem
    <path> | Remove-Item -Force` carries NO target token in this segment at
    all -- the target lives entirely in PowerShell's object pipeline, which
    no tokenizer can see.
    """
    return any(not (tok.startswith("-") or tok.startswith("/")) for tok in tokens)


def _ps_normalize_verb_token(tok: str) -> str:
    """Normalize a single PowerShell head-position token to the plain verb
    spelling it resolves to at runtime, so `_PS_REMOVE_VERBS`/
    `_PS_ICACLS_VERBS`/`_PS_STOP_PROCESS_VERBS` membership checks aren't
    defeated by quoting (`'Remove-Item'`) or a no-op backtick escape of an
    ordinary character (`` Rem`ove-Item `` -- PowerShell's own escape rule
    only affects the following character, so a backtick before a plain
    letter is simply removed, never a real transformation). Order matters:
    quotes are stripped first (matching `_dialect._strip_ps_quotes`'s own
    "verbatim quoted span" contract), THEN backticks are removed from the
    unquoted result.
    """
    return _strip_ps_quotes(tok).replace("`", "")


def _ps_resolve_head_verb(tokens: List[str]) -> tuple:
    """Resolve the destructive-verb candidate and the remaining argument
    tokens from a PowerShell segment's token stream, unwinding the two
    call-operator forms this classifier must see through in addition to a
    direct invocation:

      - `&('Remove-Item') ...` / `&("Remove-Item") ...` -- the call
        operator applied to a parenthesized, quoted verb literal. Segmented
        as `['(', "'Remove-Item'", ')', ...]` (the leading `&` is already
        consumed as a statement-boundary token by segmentation).
      - `&(Get-Command Remove-Item) ...` -- the call operator applied to a
        `Get-Command` lookup. Segmented as
        `['(', 'Get-Command', 'Remove-Item', ')', ...]`.

    Returns `(verb_lower, rest_tokens)`; `verb_lower` is `""` if no verb
    candidate could be resolved (caller's membership checks then simply
    miss, same as today's behavior for an unrecognized head).
    """
    if not tokens:
        return "", []

    if tokens[0] == "(":
        try:
            close = tokens.index(")")
        except ValueError:
            return "", []
        inner = tokens[1:close]
        rest = tokens[close + 1 :]
        if not inner:
            return "", rest
        if inner[0].lower() == "get-command" and len(inner) >= 2:
            verb_tok = inner[1]
        else:
            verb_tok = inner[0]
        return _ps_normalize_verb_token(verb_tok).lower(), rest

    verb_tok = tokens[0]
    return _ps_normalize_verb_token(verb_tok).lower(), tokens[1:]


def _evaluate_legacy_powershell_verbs(text):
    """Parse-failure fallback for the non-git PowerShell verb classifier
    (AC3 / Conventions (a)) -- the twin of `_evaluate_legacy_powershell_git`.

    Found by the slice-B partitioned review and confirmed by execution:
    `Remove-Item -Recurse -Force ./state` DENIES, and the same command with
    `&> out.txt` appended -- a shape `_dialect.py`'s own docstring records as
    confirmed ``has_error=True`` -- tokenized to ``None`` and therefore
    ALLOWED. Widening ``MATCHERS`` to ``COMMAND_TOOL_NAMES`` is what made that
    path reachable for real PowerShell-tool traffic, so the fallback has to
    exist for the widen to be safe.

    Strips here-string bodies and quoted spans via C2's
    `strip_powershell_prose_noise` before scanning, so hazard-documenting
    prose naming ``Remove-Item -Recurse -Force`` does not read as an issued
    command, and reuses this module's own verb/flag predicates rather than
    inventing a second classifier.
    """
    stripped = strip_powershell_prose_noise(text)
    for raw_segment in _PS_LEGACY_SEPARATOR_RE.split(stripped):
        seg = raw_segment.strip()
        if not seg:
            continue
        tokens = [_ps_normalize_verb_token(tok) for tok in seg.split()]
        if not tokens:
            continue
        head, rest = _ps_resolve_head_verb(tokens)
        if not head:
            continue
        if head in _PS_REMOVE_VERBS:
            if _ps_has_flag_prefix(rest, ("-r", "-fo"), frozenset({"/s", "/f"})):
                return "PowerShell remove-item (recursive or force delete)"
        elif head in _PS_ICACLS_VERBS:
            return "PowerShell icacls (permission modification)"
        elif head in _PS_STOP_PROCESS_VERBS:
            return "PowerShell stop-process (process termination)"
    return None


def _evaluate_powershell_destructive(cmd_norm: str) -> Optional[str]:
    """Return a deny_kind label for the first PowerShell-dialect destructive
    verb found in ``cmd_norm``, or ``None`` if none is found (either
    genuinely clean, or the tokenizer itself declined to rule -- SILENT
    already recorded by `_dialect.resolve_segments_for_dialect` in that
    case, nothing further to do here).

    OBJECT-PIPELINE BLIND SPOT (negative spec, required by the dispatching
    brief): `Get-ChildItem <path> | Remove-Item -Force` (or any `... |
    Remove-Item`/`Stop-Process`/... segment whose target arrives entirely via
    a preceding pipeline stage) carries no path/target token in THIS
    segment's own token stream -- there is nothing here to match, by
    construction, not by a gap in this classifier's flag/verb tables. This is
    NOT fixable by tokenization, verb recognition, or flag-shape matching
    (docs/reference/guard-dialect-coverage.md, "Object-pipeline defeats").
    Rather than return a bare clean (indistinguishable from "inspected and
    cleared") for that shape, this function records SILENT via
    `_verdict.record_silent` and continues scanning -- the command's overall
    verdict may still be a genuine allow (if no other segment denies), but
    the pipeline-fed segment itself is recorded as "declined to rule", never
    "cleared". A test pins this behavior via `_verdict.collecting()`.
    """
    segments = resolve_segments_for_dialect(
        cmd_norm, Dialect.POWERSHELL, guard_name="block_subagent_destructive_action"
    )
    if segments is None:
        return _evaluate_legacy_powershell_verbs(cmd_norm)

    for tokens, pipe_before in segments:
        if not tokens:
            continue
        head, rest = _ps_resolve_head_verb(tokens)
        if not head:
            continue

        if head in _PS_REMOVE_VERBS:
            if not _ps_has_flag_prefix(rest, ("-r", "-fo"), frozenset({"/s", "/f"})):
                continue
            if pipe_before and not _ps_has_positional_target(rest):
                record_silent(
                    "block_subagent_destructive_action",
                    "PowerShell object-pipeline target for %r -- no path token "
                    "in this segment's stream, declined to rule (see "
                    "_evaluate_powershell_destructive docstring, OBJECT-"
                    "PIPELINE BLIND SPOT)" % head,
                )
                continue
            return f"PowerShell {head} (recursive or force delete)"

        if head in _PS_ICACLS_VERBS:
            return f"PowerShell {head} (permission modification)"

        if head in _PS_STOP_PROCESS_VERBS:
            return f"PowerShell {head} (process termination)"

    return None


# ---------------------------------------------------------------------------
# POWERSHELL GIT-DESTRUCTIVE PARITY PORT (2026-08-19, C3 of docs/plans/
# 2026-08-19-the-held-guard-cohort-becomes-dialect-safe.md). Prior to this
# change, `_evaluate_powershell_destructive` above covered only
# `_PS_REMOVE_VERBS`/`_PS_ICACLS_VERBS`/`_PS_STOP_PROCESS_VERBS` -- the Bash
# leg's git deny ladder (`_evaluate_git_segment_anchored`, hard reset, force
# push, checkout/switch discarding worktree state, branch -D, clean -fdx,
# unscoped stash, filter-branch/filter-repo, worktree/remote/reflog mutation,
# pull/merge not --ff-only, config not --get, etc. -- the full enumerated
# ladder at lines ~1735-1948) sits on a `shlex`-tokenized path PowerShell
# text never reaches (`check()` routes `Dialect.POWERSHELL` to
# `_check_powershell`, never through the Bash-leg `_evaluate_git_surface`
# call at the bottom of `check()`), so a real `tool_name == "PowerShell"`
# dispatch carrying `git push --force` / `git reset --hard` / `git clean
# -fdx` / ... saw NEITHER classifier and allowed silently.
#
# THIS IS A PORT, NOT A REIMPLEMENTATION: `git` is an external executable on
# both dialects -- PowerShell passes its own argv through to `git.exe`
# UNINTERPRETED (no cmdlet-style parameter-name prefix-matching applies to
# an external command's own flags, unlike `_PS_REMOVE_VERBS`'s cmdlet-prefix
# concern above), so git's subcommand/flag GRAMMAR is byte-identical
# regardless of which shell invoked it. The existing anchored deny ladder
# (`_evaluate_git_segment_anchored`) and its subcommand resolver
# (`_real_git_subcommand`, itself already dialect-agnostic -- it operates on
# a `List[str]`, never on shell-shaped text) are therefore REUSED VERBATIM
# here, not duplicated: this function's only job is to get from
# tree-sitter-pwsh's token stream to the same `(seg_text, subcmd,
# remaining)` shape the Bash leg already builds via `shlex`, then hand off
# to the identical classifier -- true byte-for-byte parity, not a
# lookalike second implementation that could drift.
#
# MATCHERS IS NOT WIDENED HERE (AC12, deliberately deferred to a later
# chunk in the same plan): this function is reachable ONLY via
# `_check_powershell`, itself reachable only when `dialect_from_tool_name`
# already resolved `Dialect.POWERSHELL` -- i.e. only when `MATCHERS`
# already admits the calling tool name for SOME other reason. This chunk
# makes the guard correct once reached; it does not change reachability.
# ---------------------------------------------------------------------------


_PS_LEGACY_SEPARATOR_RE = re.compile(r"[;\n]|&&|\|\||\|")


def _evaluate_legacy_powershell_git(text):
    """PowerShell-shaped free-text fallback for the `tokens is None` route
    (AC3 / Conventions (a)), parallel in shape and posture to
    `block_stash_destruction._evaluate_legacy_powershell`.

    NEVER routes to `_evaluate_git_segment_legacy` (bash-shaped free text,
    the spurious-deny source this plan exists to kill). It strips
    here-string bodies and quoted spans via
    `_dialect.strip_powershell_prose_noise` (C2), then re-runs the SAME
    anchored ladder the parsed path uses (`_real_git_subcommand` +
    `_evaluate_git_segment_anchored`) over the residue, so a
    hazard-documenting prose string quoting `git reset --hard` does not
    read as an issued command while a real invocation still denies.

    Load-bearing, not defensive: `--` is not valid PowerShell, so every
    `git checkout -- <path>` form fails to tokenize and arrives here.
    Returning ``None`` on this route -- the state this function replaces --
    let those forms ALLOW under PowerShell while denying under Bash, the
    exact covered-but-permissive guard AC12's ordering exists to prevent.
    """
    stripped = strip_powershell_prose_noise(text)
    for raw_segment in _PS_LEGACY_SEPARATOR_RE.split(stripped):
        seg = raw_segment.strip()
        if not seg:
            continue
        tokens = [_ps_normalize_verb_token(tok) for tok in seg.split()]
        if not tokens:
            continue
        if _normalize_executable_basename(tokens[0]) != "git":
            continue
        subcmd, ambiguous, remaining = _real_git_subcommand(tokens[1:])
        if ambiguous:
            return (
                "git (unrecognized global option -- ambiguous resolution, "
                "denied per _real_git_subcommand's never-guess contract)"
            )
        verdict = _evaluate_git_segment_anchored(" ".join(tokens), subcmd, remaining)
        if verdict is not None:
            return verdict
    return None


def _evaluate_powershell_git_destructive(cmd_norm: str) -> Optional[str]:
    """Return a deny_kind label for the first destructive git invocation
    found in ``cmd_norm``, tokenized via the PowerShell dialect resolver,
    or ``None`` if none is found (clean, or the tokenizer declined to rule
    -- SILENT already recorded by `resolve_segments_for_dialect` in that
    case).

    Per-segment: each token is quote/backtick-stripped
    (`_ps_normalize_verb_token`, the SAME normalization
    `_evaluate_powershell_destructive` already applies to its own head
    token -- PowerShell's tokenizer emits a quoted leaf's source span
    VERBATIM, unlike `shlex`) before the segment's head token is checked
    against `git`/`git.exe`/... via `_normalize_executable_basename` (the
    same basename normalizer the Bash leg uses, already case/suffix
    tolerant). A non-git segment is skipped, not denied -- this function
    only classifies segments that actually invoke git, exactly like the
    Bash leg's own `_GIT_SURFACE_RE` gate.

    A resolved git invocation is handed to `_real_git_subcommand` (already
    dialect-agnostic -- see module comment above) to resolve the real
    subcommand past git's own global options, then to
    `_evaluate_git_segment_anchored` -- the IDENTICAL function the Bash leg
    calls -- against a space-joined reconstruction of the cleaned tokens
    (used only for that function's own flag-regex text searches, e.g.
    `_HARD_FLAG_RE`/`_PUSH_FORCE_RE`; exact-token checks like
    `"--ff-only" in remaining` are unaffected by reconstruction since they
    compare against the already-tokenized ``remaining`` list, not the
    joined text).

    An AMBIGUOUS unrecognized-global-option resolution (`_real_git_
    subcommand`'s own load-bearing "never guess, never allow" contract)
    denies outright here rather than falling back to a legacy free-text
    classifier: the Bash leg's `_evaluate_git_segment_legacy` fallback is a
    Bash-shaped free-text scanner with no PowerShell-dialect counterpart in
    this chunk's scope, and the Bash leg's own posture for this case is
    "ambiguity never resolves to allow" -- denying preserves that posture
    without inventing a second legacy classifier this chunk was not asked
    to build.
    """
    segments = resolve_segments_for_dialect(
        cmd_norm, Dialect.POWERSHELL, guard_name="block_subagent_destructive_action"
    )
    if segments is None:
        return _evaluate_legacy_powershell_git(cmd_norm)

    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        clean = [_ps_normalize_verb_token(tok) for tok in tokens]
        if _normalize_executable_basename(clean[0]) != "git":
            continue
        subcmd, ambiguous, remaining = _real_git_subcommand(clean[1:])
        if ambiguous:
            return (
                "git (unrecognized global option -- ambiguous resolution, "
                "denied per _real_git_subcommand's never-guess contract)"
            )
        seg_text = " ".join(clean)
        verdict = _evaluate_git_segment_anchored(seg_text, subcmd, remaining)
        if verdict is not None:
            return verdict

    return None


def _check_powershell(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """PowerShell-dialect leg of `check()` -- identity resolution mirrors the
    Bash leg's DUAL OR-resolver (module docstring "IDENTITY AXIS") exactly,
    reusing the same helpers. SURFACE classification is split across two
    functions: `_evaluate_powershell_git_destructive` (git deny ladder,
    ported at parity from the Bash leg's `_evaluate_git_segment_anchored`
    -- see that function's own module comment) and
    `_evaluate_powershell_destructive` (the non-git
    Remove-Item/icacls/Stop-Process verb classifier). Kept as a fully
    separate function (not interleaved into the Bash body) so the Bash
    leg's own AC4 behavior-preservation requirement carries zero risk from
    this addition.
    """
    cmd = _extract_command(payload)
    if not cmd:
        return None
    cmd_norm = cmd.replace("\r", "")

    deny_kind = _evaluate_powershell_git_destructive(cmd_norm)
    if deny_kind is None:
        deny_kind = _evaluate_powershell_destructive(cmd_norm)
    if deny_kind is None:
        return None

    if "agent_id" not in payload:
        _log_fail_open(
            "no-agent-id-key",
            payload,
            cmd,
            agent_type_present="agent_type" in payload,
            agent_type=payload.get("agent_type") or "",
        )
        return None

    git_root = resolve_git_root(payload.get("cwd"))
    raw_agent_id = payload.get("agent_id") or ""
    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id) if raw_agent_id else ""
    agent_type = payload.get("agent_type") or ""
    subagent_type = ""
    subagent_type_computed = False
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(git_root, agent_id)
        subagent_type_computed = True

    effective_type = agent_type or subagent_type or ""
    if subagent_type == "AMBIGUOUS":
        effective_type = "AMBIGUOUS"

    kind_unresolved = not effective_type
    if kind_unresolved:
        branch = (
            "kind-unresolved-unparseable-agent-id"
            if not agent_id
            else "kind-unresolved-empty-effective-type"
        )
        _log_fail_open(
            branch,
            payload,
            cmd,
            raw_agent_id=raw_agent_id,
            agent_id=agent_id,
            git_root=git_root,
            agent_type_present="agent_type" in payload,
            agent_type=agent_type,
            subagent_type_present=subagent_type_computed,
            subagent_type=subagent_type,
        )
        # No resolved kind at all -- same posture as the Bash leg: this is a
        # known subagent (raw_agent_id present) whose KIND could not be
        # determined. The PowerShell leg's verb table has no allow-forward
        # path (every recognized verb is always-dangerous or flag-gated
        # already), so an unresolved kind still denies below -- there is no
        # equivalent of the Bash leg's Layer-2 allowlist to fall through to.

    if effective_type == "AMBIGUOUS":
        deny_kind = "ambiguous-identity"

    cmd_safe = _sanitize_command(cmd)
    reason = _build_reason(deny_kind, agent_id or raw_agent_id, effective_type, cmd_safe, cmd)
    final_verdict = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    if kind_unresolved:
        emit_kind_resolution_failure_signal(
            "block_subagent_destructive_action", agent_id, git_root, final_verdict
        )
    return final_verdict


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is None:
        return None
    if dialect is Dialect.POWERSHELL:
        return _check_powershell(payload)

    cmd = _extract_command(payload)
    if not cmd:
        return None

    # CRLF normalize (Windows/Git-Bash jq.exe text-mode quirk -- redundant
    # per-check strip, per recipe section (c) "double-strip discipline").
    cmd_norm = cmd.replace("\r", "")

    # Heredoc bodies are stdin DATA, not shell commands -- strip them before
    # classification so benign prose containing a destructive verb word (a
    # code-reviewer's findings mentioning "git", a doc heredoc saying "rm")
    # is not mis-denied. Anti-bypass: an interpreter FED by a heredoc
    # (`bash <<EOF … EOF`) still denies -- the residual `bash <<'EOF'` matches
    # the wrapper probe. `cmd_safe` for the deny reason still uses the
    # original `cmd`, so the operator sees the real command text.
    cmd_norm = _strip_heredoc_bodies(cmd_norm)

    # 2026-07-28 fix (SPACED-WINDOWS-PATH ARGV0 HOLE, see the module comment
    # above `_WINDOWS_ARGV0_HEAD_PATH_RE`): quote-and-normalize an UNQUOTED
    # Windows argv0-head path that contains an embedded-space component
    # (`C:\Program Files\Git\bin\git.exe`) BEFORE the no-space-only pass
    # below, so the whole path lands as one `shlex` token instead of
    # splitting on the space.
    cmd_norm = _normalize_windows_argv0_head_path_with_spaces(cmd_norm)

    # 2026-07-25 fix, widened 2026-07-28 (A2) -- see
    # `_normalize_windows_wrapper_argv0` docstring: rewrite a Windows
    # backslash-path invocation head (`C:\path\to\git.exe`,
    # `C:\Windows\System32\bash.exe`, `C:\Python311\python.exe`) to its
    # forward-slash equivalent BEFORE any shlex tokenization runs, so it
    # survives shlex's escape-processing intact and is recognized downstream
    # the same as a bare `git`/`bash`/`python3`/... head.
    cmd_norm = _normalize_windows_wrapper_argv0(cmd_norm)

    # ------------------------------------------------------------------
    # LAYER 1 -- dangerous-surface detection, BEFORE identity resolution.
    # ------------------------------------------------------------------
    is_git_surface = bool(_GIT_SURFACE_RE.search(cmd_norm))
    is_rm_surface = bool(_RM_SURFACE_RE.search(cmd_norm))
    is_chmod_chown_r_surface = False
    if _CHMOD_CHOWN_RE.search(cmd_norm) and _RECURSIVE_FLAG_RE.search(cmd_norm):
        is_chmod_chown_r_surface = True
    # 2026-07-21 addition (see module docstring "INDIRECTION-WRAPPER
    # HARDENING"): a fourth Layer-1 probe, cheap and boundary-anchored like
    # the three above, so identity resolution also gates on wrapper shapes
    # that hide a destructive verb from the literal-token probes.
    is_wrapper_surface = bool(_WRAPPER_PROBE_RE.search(cmd_norm))

    # 2026-07-21 security-audit hardening (findings #1-#6): the tokenized
    # authoritative pass runs unconditionally -- the raw-text probes above
    # can never be the SOLE gate, since a quote-obfuscated verb (`r''m`,
    # `ch"m"od`, `r\m`) or an unresolved-indirection/eval/source/bare-pipe-
    # interpreter shape may match NONE of them.
    tok_surfaces = _evaluate_tokenized(cmd_norm)
    is_git_surface = is_git_surface or tok_surfaces.is_git
    is_rm_surface = is_rm_surface or tok_surfaces.is_rm
    is_chmod_chown_r_surface = is_chmod_chown_r_surface or tok_surfaces.is_chmod_chown_r
    is_wrapper_surface = is_wrapper_surface or tok_surfaces.is_wrapper
    # MACHINE-LOCAL REGISTRY WRITE DENY (see module comment above
    # `_MACHINE_LOCAL_WRITE_SUBCOMMANDS`) -- tokenized-only surface, no raw-
    # text pre-probe: `_evaluate_tokenized` already runs unconditionally
    # above, so a separate cheap regex would gate nothing further here.
    is_machine_local_surface = tok_surfaces.is_machine_local

    if not (
        is_git_surface
        or is_rm_surface
        or is_chmod_chown_r_surface
        or is_wrapper_surface
        or is_machine_local_surface
    ):
        return None

    # No agent_id at all -> top-level EM Bash call -> allow, before paying
    # any identity-resolution cost (reference hook line 160). Logged
    # WITHOUT resolving git_root (see `_log_fail_open` docstring) so this
    # branch -- which fires on ordinary EM main-loop traffic, not just
    # subagent traffic -- never pays a `resolve_git_root` subprocess spawn
    # it didn't already pay before this observability addition.
    if "agent_id" not in payload:
        _log_fail_open(
            "no-agent-id-key",
            payload,
            cmd,
            agent_type_present="agent_type" in payload,
            agent_type=payload.get("agent_type") or "",
        )
        return None

    # ------------------------------------------------------------------
    # IDENTITY AXIS -- DUAL OR-resolver via the shared engine helper.
    # ------------------------------------------------------------------
    git_root = resolve_git_root(payload.get("cwd"))
    raw_agent_id = payload.get("agent_id") or ""
    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id) if raw_agent_id else ""
    agent_type = payload.get("agent_type") or ""
    subagent_type = ""
    subagent_type_computed = False
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(git_root, agent_id)
        subagent_type_computed = True

    # Empty AGENT_ID (raw_agent_id present but unparseable to either accepted
    # id shape) no longer fail-open-allows (fixed 2026-07-30): raw_agent_id
    # presence already established this IS a subagent above -- an id that
    # fails to canonicalize is a second lookup-miss on the SAME "what kind"
    # question, not a fresh answer to "is this a subagent". It no longer
    # short-circuits to allow here; the PRIMARY (`agent_type`) leg may still
    # resolve a kind below even when canonicalization failed, so whether
    # this is actually a kind-resolution failure is decided once, after
    # `effective_type` is computed, not here.
    effective_type = agent_type or subagent_type or ""
    # AMBIGUOUS on the SECONDARY leg overrides a populated PRIMARY leg
    # unconditionally (guards against a stale/spoofed PRIMARY masking a
    # real collision).
    if subagent_type == "AMBIGUOUS":
        effective_type = "AMBIGUOUS"

    # No resolved type via EITHER resolver -- same fix: this is still a known
    # subagent (raw_agent_id present), just one whose KIND we could not
    # determine (empty git_root, or a missing/unreadable/malformed
    # backpointer chain). Falls through into LAYER 2's default-deny rather
    # than returning allow; Layer 2's classification does not depend on
    # `effective_type` except for the AMBIGUOUS override above, which cannot
    # fire here since `subagent_type` was never computed without a resolved
    # `agent_id` + `git_root`.
    kind_unresolved = not effective_type
    if kind_unresolved:
        branch = (
            "kind-unresolved-unparseable-agent-id"
            if not agent_id
            else "kind-unresolved-empty-effective-type"
        )
        _log_fail_open(
            branch,
            payload,
            cmd,
            raw_agent_id=raw_agent_id,
            agent_id=agent_id,
            git_root=git_root,
            agent_type_present="agent_type" in payload,
            agent_type=agent_type,
            subagent_type_present=subagent_type_computed,
            subagent_type=subagent_type,
        )
        # The signal itself is emitted below, at EITHER of this function's
        # two reachable exits (Layer 2 can still allow OR deny on an
        # unresolved kind -- its classification does not gate on
        # effective_type except the AMBIGUOUS override above), with the
        # exact verdict this call is about to return.

    # ------------------------------------------------------------------
    # LAYER 2 -- default-deny with safe-forward allowlist.
    # ------------------------------------------------------------------
    deny = False
    deny_kind = ""

    if effective_type == "AMBIGUOUS":
        deny = True
        deny_kind = "ambiguous-identity"
    else:
        if is_git_surface:
            verdict = _evaluate_git_surface(cmd_norm)
            if verdict is not None:
                deny = True
                deny_kind = verdict

        # Tokenized authoritative fallback (findings #1/#2): catches a
        # quote-obfuscated git verb the raw-text evaluator above missed.
        if not deny and tok_surfaces.is_git and tok_surfaces.deny_kind:
            deny = True
            deny_kind = tok_surfaces.deny_kind

        if not deny and is_rm_surface:
            if _RM_DENY_RE.search(cmd_norm):
                deny = True
                deny_kind = "rm -r/-f (recursive or force)"

        if not deny and tok_surfaces.is_rm and tok_surfaces.deny_kind:
            deny = True
            deny_kind = tok_surfaces.deny_kind

        if not deny and is_chmod_chown_r_surface:
            deny = True
            deny_kind = "chmod/chown -R (recursive)"

        if not deny and is_wrapper_surface:
            wrapper_verdict = _evaluate_wrapper_indirection(cmd_norm)
            if wrapper_verdict is not None:
                deny = True
                deny_kind = f"indirection: {wrapper_verdict}"

        # Tokenized authoritative fallback (findings #3-#6): unresolved
        # variable/command-substitution indirection, eval, source/`.`, and a
        # bare interpreter fed via stdin pipe -- shapes the raw-text wrapper
        # probe/evaluator above cannot see at all.
        if not deny and tok_surfaces.is_wrapper and tok_surfaces.deny_kind:
            deny = True
            deny_kind = f"indirection: {tok_surfaces.deny_kind}"

        if not deny and tok_surfaces.is_machine_local and tok_surfaces.deny_kind:
            deny = True
            deny_kind = tok_surfaces.deny_kind

    if not deny:
        if kind_unresolved:
            emit_kind_resolution_failure_signal(
                "block_subagent_destructive_action", agent_id, git_root, None
            )
        return None

    cmd_safe = _sanitize_command(cmd)

    # RESHAPE (2026-08-06, docs/plans/2026-08-06-apply-guard-class-census.md
    # C18d): the destructive core (a git/rm/chmod-chown-R/machine-local
    # match this guard actually SAW, directly or resolved via a `-c`/`eval`
    # recursion -- see `_is_opaque_indirection`) stays a hard deny,
    # unchanged. An OPAQUE indirection-wrapper shape -- the guard never saw
    # the payload at all -- no longer denies outright; it advises instead.
    # `GuardEntry` registration (band CONFINEMENT_DENY, fail_closed=True) is
    # UNCHANGED -- this branch returns an allow+additionalContext envelope
    # from the same registered `check()`, the same shape
    # `block_dev_repo_sentinel_removal`'s prior single-function design used
    # (see `dispatch.py`'s own comment on that guard, cited verbatim in this
    # module's dispatching brief) before that guard was split into two
    # registrations -- here it stays one, per the brief's explicit
    # instruction not to touch this guard's `dispatch.py` entry.
    if deny_kind.startswith("indirection: "):
        wrapper_shape = deny_kind[len("indirection: "):]
        if _is_opaque_indirection(wrapper_shape):
            advisory_verdict = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": _build_indirection_advisory_reason(wrapper_shape),
                }
            }
            if kind_unresolved:
                emit_kind_resolution_failure_signal(
                    "block_subagent_destructive_action", agent_id, git_root, advisory_verdict
                )
            return advisory_verdict

    reason = _build_reason(deny_kind, agent_id or raw_agent_id, effective_type, cmd_safe, cmd)

    final_verdict = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    if kind_unresolved:
        emit_kind_resolution_failure_signal(
            "block_subagent_destructive_action", agent_id, git_root, final_verdict
        )
    return final_verdict
