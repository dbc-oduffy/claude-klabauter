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

import os
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


# For NAMED-TEAMMATE dispatches that keys the wrong back-pointer dir, subagent_type
# the ALREADY-CORRECT canonical resolver (write_guards.block_subagent_plan_body_write._resolve_subagent_identity)
CLASS = "hard-deny"

GENERATES = []
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 40

# re.MULTILINE so ^/$ anchor per-line, mirroring `grep -qE ... <<< "$VAR"`'s
# BOUNDARY-CLASS WIDEN (2026-07-21 review, Findings 1 & 2): a real shell
# Layer-1 surface probe (and the paired Layer-2 `_RM_DENY_RE`/recursive-flag
# probes) with zero identity-resolution cost. `_BOUNDARY_PRE`/`_BOUNDARY_POST`
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

# CMD_NORM, NOT per-segment (deliberate divergence from the git surface).
_RM_DENY_RE = re.compile(
    _BOUNDARY_PRE + r"rm(?:\s+-[a-zA-Z]*[rRfF][a-zA-Z]*|\s+--recursive|\s+--force)",
    re.MULTILINE,
)

# "INDIRECTION-WRAPPER HARDENING"). Cheap, boundary-anchored, same style as
_WRAPPER_PROBE_RE = re.compile(
    _BOUNDARY_PRE + r"(?:bash|sh|zsh|python3?(?:\.\d+)*|env|xargs)" + _BOUNDARY_POST,
    re.MULTILINE,
)

_C_FLAG_INTERPRETERS = frozenset({"bash", "sh", "zsh", "python", "python3"})

_SHELL_FILE_INTERPRETERS = frozenset({"bash", "sh", "zsh"})

_MAX_INDIRECTION_DEPTH = 4

_BUNDLED_C_FLAG_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")

#: `_C_FLAG_INTERPRETERS`/`_SHELL_FILE_INTERPRETERS` membership checks below
_PYTHON_VERSION_SUFFIX_RE = re.compile(r"^(python3?)(?:\.\d+)+$")

#: `.`/`source` builtins execute a script IN-PROCESS -- same blast radius as
_SOURCE_VERBS = frozenset({".", "source"})

# MACHINE-LOCAL REGISTRY WRITE DENY (2026-08-03, this change, DR-125
# `coordinator:executor` left `_helpers._CONFINED_FINDINGS_AGENTS`,
# perimeter to MACHINE-DEGRADING actions and commits; corrupting a registry
# `_CONFINED_FINDINGS_AGENTS` membership) -- deliberately stronger than the
# are READ-ONLY (how a subagent legitimately resolves a sibling-repo path)
_MACHINE_LOCAL_WRITE_SUBCOMMANDS = frozenset(
    {"set", "array-append", "array-set", "migrate-publish-mirrors"}
)

_ARGV0_UNRESOLVED_RE = re.compile(r"^\$")

# SETTINGS-HOME BIN NARROW EXEMPTION (2026-07-27 addition, authorized by PM
# ``"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<cli>"``
# -- an unresolved-`$VAR`-in-argv0 shape that `_ARGV0_UNRESOLVED_RE` above
#   - the var name must be EXACTLY `COORDINATOR_SETTINGS_HOME` (any other
#     -- `${COORDINATOR_SETTINGS_HOME:-/tmp/evil}/bin/x` is an
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
    m = _PYTHON_VERSION_SUFFIX_RE.match(name)
    return m.group(1) if m else name


# INVOCATION-HEAD NORMALIZATION (2026-07-25 fix -- closes a gap found during


#: runs. UNLIKE the shared `_BOUNDARY_PRE` used by the Layer-1 surface
#: is still recognized -- only the POSITION that can start a match narrowed,
_ARGV0_HEAD_BOUNDARY_PRE = (
    r"(?:\A|[;&|\n])\s*(?:[" + re.escape(_QUOTE_OPEN_CHARS) + r"])?"
)
_RAW_HEAD_TOKEN_RE = re.compile(r"(" + _ARGV0_HEAD_BOUNDARY_PRE + r")([^\s;&|]+)")


#: `_WRAPPER_PROBE_RE` itself recognizes as an indirection-wrapper
#: `_PYTHON_VERSION_SUFFIX_RE` family (`python3.11`, ...) via
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


# SPACED-WINDOWS-PATH ARGV0 HOLE (2026-07-28, this change).
# `_RAW_HEAD_TOKEN_RE` above stops its raw-token capture at the FIRST
# WHITESPACE (`[^\s;&|]+`), so an argv0 head whose Windows path contains a
# install location, not an exotic edge case -- is only ever PARTIALLY
# `_WINDOWS_ARGV0_NORMALIZE_BASENAMES`, and left completely unrewritten.
#     escape-eating, but the UNQUOTED embedded space is still a real shlex
#     check ONLY `seg_tokens[0]`/`working[0]` (an argv0-POSITION identity
# RECONCILED 2026-07-29 (code-reviewer Finding 1, cross-guard contradiction
# ACTUALLY EXECUTES on a real Windows box -- i.e. that some invocation path
# A REFERENCE FIX for exactly this argv0-head-path shape already exists in
# `_WINDOWS_GIT_PATH_HEAD_RE` (committed `6bb7a8c4`) -- it captures the
# WHOLE head-position path INCLUDING embedded-space components via a
# module's own W3-FIX-1 comment block near the top and this file's import
# `git`-only to every basename `_WINDOWS_ARGV0_NORMALIZE_BASENAMES`
# position-independent. THIS module's argv0-head checks are POSITIONAL
# module comment "IDENTIFIES ... at each segment's argv[0] TOKEN position,
# single-quotes an UNQUOTED path that contains whitespace, so the whole
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


#: `-n`/`--noexec` puts bash/sh/zsh into READ-BUT-DO-NOT-EXECUTE mode -- the
_NOEXEC_FLAG_TOKENS = frozenset({"-n", "--noexec"})

#: A long option's ATTACHED-value form (`--rcfile=/tmp/x`, `--init-file=x`),
_LONG_OPT_WITH_VALUE_RE = re.compile(r"^--[A-Za-z][A-Za-z0-9-]*=")


def _has_noexec_flag_before_script(interpreter_args: List[str]) -> bool:
    for tok in interpreter_args:
        if tok == "--":
            break
        if tok in _NOEXEC_FLAG_TOKENS:
            return True
        if tok.startswith("-"):
            continue
        break
    return False


def _has_script_operand(interpreter_args: List[str]) -> bool:
    """True if `interpreter_args` (the tokens AFTER a `bash`/`sh`/`zsh` name)
    contains a non-option OPERAND -- i.e. the argv actually names a script for
    the shell to run.

    An argv whose every remaining token is one of the shell's OWN option
    tokens names no script at all: `bash --version`, `bash --help`, `sh -l`
    print or set something and exit, and denying them as
    "interpreter-invoked script" describes an invocation that does not exist
    (bug fix, 2026-08-22: an option-only argv reached the bare-`<file>` deny
    branch purely on token COUNT).

    Option scanning matches `_has_noexec_flag_before_script`, with two
    deliberate edges:

    - A literal `--` ends option parsing, so a following token is an operand
      even though it starts with `-`: `bash -- --version` runs a script
      NAMED `--version` and stays denied.
    - A lone `-` is not an option; bash reads the script from stdin, whose
      content this guard cannot examine, so it counts as an operand and
      stays denied.

    An option that TAKES a value (`bash --rcfile <file>`, `bash -O <name>`)
    has its value counted as the operand. That is deliberately unchanged
    from the pre-fix token-count behaviour -- this predicate only ever
    withholds the deny from an argv with NO operand at all, so it can never
    open a shape the count check already denied. `-c` never reaches here:
    the `_BUNDLED_C_FLAG_RE` branch above claims it first, in every caller.

    ATTACHED-FORM VALUE FIX (2026-08-22, code-review finding on this same
    fix's landing): the space-separated case above (`--rcfile <file>`)
    already returns True on the value token, but the attached `=` form
    (`--rcfile=/tmp/evil.rc`) is a SINGLE token starting with `-`, so it fell
    into the "pure option, skip" branch and was never seen as carrying a
    script-bearing value at all -- an argv made entirely of such tokens
    (`bash --rcfile=/tmp/evil.rc --norc`) read as option-only and was
    ALLOWED, letting `--rcfile=`/`--init-file=` source an unexamined file.
    Fixed by treating ANY `--name=value` long option as operand-bearing,
    not by naming just `--rcfile`/`--init-file`: an allowlist of "known
    script-sourcing options" fails open the moment bash grows one this
    guard's author didn't enumerate, exactly the failure mode that produced
    this bug. Any option that takes an attached value is presumed capable of
    carrying one until proven otherwise -- consistent with the existing
    space-separated behaviour, which already denies on ANY value token
    without checking which option preceded it.
    """
    for idx, tok in enumerate(interpreter_args):
        if tok == "--":
            return idx + 1 < len(interpreter_args)
        if _LONG_OPT_WITH_VALUE_RE.match(tok):
            return True
        if tok.startswith("-") and tok != "-":
            continue
        return True
    return False


def _strip_env_prefix(tokens: List[str]) -> List[str]:
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
    if depth > _MAX_INDIRECTION_DEPTH:
        return "indirection nesting too deep (fails closed)"

    # matched `_WRAPPER_PROBE_RE` (it retained the `python3`/`bash`/etc.
    all_tokens = _tokenize_full_command(cmd_text)
    if all_tokens is None:
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
            if head_base in _SHELL_FILE_INTERPRETERS and _has_noexec_flag_before_script(
                tokens[1:]
            ):
                continue

            c_flag_positions = [
                i for i in range(1, len(tokens)) if _BUNDLED_C_FLAG_RE.match(tokens[i])
            ]
            if c_flag_positions:
                idx = c_flag_positions[0]
                if idx + 1 < len(tokens):
                    inline_payload = tokens[idx + 1]
                else:
                    inline_payload = " ".join(shlex.quote(t) for t in tokens[idx + 1 :]) or seg
                verdict = _unwrap_and_classify(inline_payload, depth + 1)
                if verdict is not None:
                    return f"{head_base} -c '<inline>' -> {verdict}"
                continue
            if head_base in _SHELL_FILE_INTERPRETERS and _has_script_operand(
                tokens[1:]
            ):
                return (
                    f"{head_base} <file> (interpreter-invoked script -- "
                    "indirection wrapper, script content unexamined)"
                )
            # UNCHANGED text would just re-match this same branch forever,
            continue

        # matched the cheap `_WRAPPER_PROBE_RE` via an unrelated substring
        if was_env_wrapped:
            remainder = " ".join(shlex.quote(t) for t in tokens)
            verdict = _unwrap_and_classify(remainder, depth + 1)
            if verdict is not None:
                return verdict

    return None


_CHECKOUT_KEYWORD_RE = re.compile(r"\bcheckout\b")
_CHECKOUT_DASHDASH_RE = re.compile(r"\bcheckout\b.*--(?:\s|$)")
_CHECKOUT_PATHSPEC_RE = re.compile(r"\bcheckout\b\s+[A-Za-z0-9_.-]*/[A-Za-z0-9_./-]*")

# Anchored to an actual `git restore` SUBCOMMAND invocation (`git`, then
_RESTORE_KEYWORD_RE = re.compile(r"\bgit\b(?:\s+--?[\w-]+)*\s+restore\b")
_RESTORE_WORKTREE_RE = re.compile(r"(?:-W|--worktree)(?:\s|$)")
_RESTORE_STAGED_RE = re.compile(r"(?:--staged|(?:^|\s)-S(?:\s|$))")

_RESET_KEYWORD_RE = re.compile(r"(?:^|\s)reset(?:\s|$)")
_RESET_STRIP_PREFIX_RE = re.compile(r".*(?:^|\s)reset(?:\s|$)")
_RESET_REF_RE = re.compile(r"(?:HEAD|@\{|[0-9a-f]{7,40}|/)")
_DASHDASH_SEP_RE = re.compile(r"--(?:\s|$)")

_STASH_POP_APPLY_RE = re.compile(r"\bstash\b.*\b(?:pop|apply)\b")
#: unscoped-stash-gap-close fix (module docstring "UNSCOPED-STASH GAP
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


def _seg_forcing_form_scan_text(seg: str) -> str:
    """Narrow `seg` to the argv of the `git` it invokes, for the LEGACY
    path's `_PUSH_FORCE_RE` only -- or return `seg` whole when that cannot
    be established.

    The anchored path (`_evaluate_git_segment_anchored`) already has this
    right for every other option gate: its docstring records that the
    2026-07-25 hardening scoped checkout/switch/branch/tag/merge/pull/
    config to `remaining_text` "never `seg`" so a pre-subcommand token
    cannot false-trip a subcommand-local check. Push kept scanning `seg`,
    and the legacy path -- which is where a WRAPPED invocation lands,
    because the anchored callers all require `tokens[0]` to normalize to
    `git` -- never had the scoping at all.

    The reachable consequence, reproduced 2026-09-01 against this module:

        /usr/bin/time -f "%e" git push   ->  "git push --force"

    `time` takes `-f FORMAT`. The push is a plain no-argument push. Same
    defect as CHECK 2's in `dispatch_checks` (see `_seg_forcing_form_scan_
    text` there and `TestCheck2WrapperOwnFlagsAreNotGitsFlags`); reported
    together by example-retrieval-repo-em in `cross-repo/inbox/2026-09-01-example-retrieval-repo-
    em-push-cadence-cap-below-noop-floor.md`.

    Deliberately NOT imported from `dispatch_checks` -- these two modules
    keep their force-form regexes separately for the same reason
    `_real_git_subcommand` is not shared: different token-source contracts,
    and a guard that fails closed on its own terms beats one coupled to a
    sibling's parsing assumptions.

    Cannot open a bypass: `--force` and `+refspec` are push ARGUMENTS, so a
    genuine forcing push carries them after the `git` token and stays in
    the scanned text. Fails CLOSED to the whole segment on a heredoc
    marker, an untokenizable segment, or no `git` token at all -- so the
    free-text fallback this legacy path exists to provide keeps firing on
    exactly the segments it always did.
    """
    if "<<" in seg:
        return seg
    if _exceeds_tokenizable_ceiling(seg):
        return seg
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return seg
    for idx, tok in enumerate(tokens):
        if _normalize_executable_basename(tok) == "git":
            return " ".join(tokens[idx:])
    return seg
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
#: `worktree`/`remote` used to sit in `_SAFE_VERB_RE` below as BARE VERBS
#: `_WORKTREE_MUTATE_RE`/`_REMOTE_MUTATE_RE` to deny the KNOWN-mutating
#: through to `_SAFE_VERB_RE` and allowing -- i.e. still allow-by-default on
#: INVERTED here per the same "unknown means deny, not allow" fix already
#: lightweight `_NEXT_WORD_AFTER_RE` capture of whatever plain word
#: against the small READ-ONLY sets below; anything else -- an unrecognized
#: the anchored path's bare-invocation behavior. `_WORKTREE_MUTATE_RE`/
#: `_REMOTE_MUTATE_RE` are kept ONLY for their more specific deny-reason
_WORKTREE_MUTATE_RE = re.compile(
    r"\bworktree\b.*\b(?:add|remove|prune|move|repair|unlock|lock)\b"
)
_REMOTE_MUTATE_RE = re.compile(
    r"\bremote\b.*\b(?:add|remove|rm|rename|set-url|set-head|set-branches|prune|update)\b"
)
_WORKTREE_WORD_RE = re.compile(r"\bworktree\b")
_REMOTE_WORD_RE = re.compile(r"\bremote\b")
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
#: ANCHORED path only, matched against ``remaining_text`` (the argv slice
#: reuses the already-defined `_DASH_F_OR_FORCE_RE` (branch's generic
_SWITCH_DISCARD_CHANGES_RE = re.compile(r"--discard-changes\b")
#: checkout `-B` / switch `-C` create-OR-RESET an existing branch even if
_CHECKOUT_DASH_B_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*B[a-zA-Z]*(?:\s|$)")
_SWITCH_DASH_C_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*C[a-zA-Z]*(?:\s|$)")
_BRANCH_FORCE_MOVE_OR_COPY_UPPER_RE = re.compile(
    r"(?:^|\s)-[a-zA-Z]*[MC][a-zA-Z]*(?:\s|$)"
)
_BRANCH_MOVE_OR_COPY_RE = re.compile(
    r"(?:^|\s)-[a-zA-Z]*[mc][a-zA-Z]*(?:\s|$)|--move\b|--copy\b"
)


# SUBCOMMAND-ANCHORED CLASSIFICATION (2026-07-25 false-positive fix).
# Root cause: every `_..._WORD_RE`/`_..._KEYWORD_RE` regex above (checkout,
# character, so it cannot distinguish the real invoked SUBCOMMAND from the
#     CONTAINS the word "merge")
# `-D`, ...) are UNCHANGED -- only the "is this segment invoking verb X"
# FAIL-CLOSED on shlex failure: an unparseable segment (unbalanced quoting)
# falls back to `_evaluate_git_segment_legacy` -- the ORIGINAL free-text
# `_SAFE_VERB_RE`, which does NOT get the new read-only-verb additions

_GIT_GLOBAL_OPT_WITH_ARG = frozenset({"-C", "-c"})

_GIT_GLOBAL_OPT_NO_ARG = frozenset(
    {"--no-pager", "-P", "--bare", "--literal-pathspecs", "--paginate", "--no-optional-locks"}
)

_GIT_GLOBAL_OPT_SPACE_FORM = frozenset({"--git-dir", "--work-tree"})


#: `subcmd` slot when the segment's COMMAND-POSITION head (after peeling any
#: classifier. See module docstring "COMMAND-POSITION GIT-TOKEN FIX".
_NOT_A_GIT_INVOCATION = object()

#: `parse_ok=False` sentinel distinguishing the INLINE-INTERPRETER
#: CARVE-OUT branch (a `-c`-flagged interpreter head with a bundled `-c`
#: catchall for this branch -- see `_evaluate_git_segment`'s "INDIRECTION-
#: PAYLOAD CATCHALL FIX" docstring entry: an interpreter `-c` payload is
_INTERPRETER_C_PAYLOAD_AMBIGUOUS = object()

#: `dispatch_checks.py`'s `_BYPASS_PREFIX` already tolerates.
#: different binary name. This is still an ENUMERATED allowlist, not a
#: UNRECOGNIZED head rather than only on a genuine parse failure; that
_PASSTHROUGH_WRAPPERS = frozenset(
    {
        "sudo", "command", "time", "exec", "nice", "nohup", "ionice", "timeout",
        "stdbuf", "which", "type", "setsid", "strace", "doas", "busybox",
    }
)

#: `dispatch_checks.py`'s `_BYPASS_WRAPPER_ARG_FLAGS` uses for the identical
#: hand-maintained here; this was the ORIGINAL of the five hand-maintained


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
        # subshell-open `(` was. Bash's `{ ... }` grouping REQUIRES a space
        if head == "{":
            working = working[1:]
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            working = working[1:]
            continue
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
            i += 1
            continue
        if tok.startswith("-"):
            # UNCERTAIN (could be a bare no-arg flag, or a flag that
            return None, True, []
        return tok, False, argv_after_git[i + 1 :]
    return None, False, []


#: Matches a shell OR PowerShell redirection OPERATOR at the start of a
_REDIRECTION_OP_RE = re.compile(r"^(?:\d*(?:>>?|&>>?|<{1,3}|<>)|\*>>?)")


def _strip_leading_redirection_tokens(tokens: "List[str]") -> "List[str]":
    """Drop a leading run of shell-redirection tokens from `tokens`.

    2026-08-22 fix (UNSCOPED-STASH GAP, REOPENED): `shlex` has no concept of
    shell redirection -- `git stash 2>&1` tokenizes to `["git", "stash",
    "2>&1"]`, an ordinary-looking 3rd token, not two tokens split apart by a
    stream-redirect operator the way a real shell would see it. Any caller
    that reads `remaining[0]` as "the first real argument after the
    subcommand" (the `stash` branch of `_evaluate_git_segment_anchored`, and
    `block_subagent_stash_creation`'s mirror of it) was silently handed
    `"2>&1"` as that argument -- not `None` (bare `git stash`, denied) and
    not a real flag/subcommand, so the stash branch's `is_push_or_bare` test
    failed and the command fell through to the safe-forward allowlist. A
    redirection is never a real git argument; this treats one exactly like
    the absence of an argument, matching what the shell itself would see.

    2026-08-23 fix (same gap, two more character classes): the original
    regex only matched bash's OUTPUT-direction operators. `<`-family input
    redirection (`git stash </dev/null`) and PowerShell's `*>`/`*>>`
    all-streams redirect are exactly as invisible to `shlex`/the PowerShell
    tokenizer as `>` was, and displace `remaining[0]` the identical way.
    Widened rather than special-cased so every `remaining[0]` reader (this
    module's stash branch, both `block_subagent_stash_creation` legs, and
    `block_stash_destruction`'s two sites) gets the fix from one shared
    helper -- see `docs/reference` note on not hand-copying this check.

    Handles both a self-contained token (operator and target glued together,
    e.g. `"2>&1"`, `">/dev/null"`, `"</dev/null"`) and a bare operator
    followed by its target as a SEPARATE token (e.g. `[">", "/dev/null"]`,
    `["2>", "/dev/null"]`, `["<<<", "x"]`) -- the latter only occurs when the
    shell command had whitespace between the operator and its target, which
    the tokenizer still splits into two tokens. Consumes a run of more than
    one redirection (`2>&1 >/tmp/log`), not just one, since a caller may
    chain several before any real argument.
    """
    i = 0
    n = len(tokens)
    while i < n:
        match = _REDIRECTION_OP_RE.match(tokens[i])
        if not match:
            break
        if match.end() < len(tokens[i]):
            i += 1
        else:
            i += 1
            if i < n:
                i += 1
    return tokens[i:]


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
        return _NOT_A_GIT_INVOCATION, True, []
    base = _normalize_executable_basename(working[0])
    if base != "git":
        # INLINE-INTERPRETER CARVE-OUT (2026-07-28, part of the
        # "COMMAND-POSITION GIT-TOKEN FIX"): a command-position head that IS
        # `_WRAPPER_PROBE_RE`, gapped on a `/`-preceded interpreter name;
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
    subcmd, parse_ok, _remaining = _git_subcommand_and_remaining_for_segment(seg)
    return subcmd, parse_ok


#: Read-only git verbs safe to allow outright -- ANCHORED path only (the
#: legacy `_SAFE_VERB_RE` fallback deliberately keeps its original list, see
#: token below (`_WORKTREE_MUTATING_SUBCOMMANDS`/`_REMOTE_MUTATING_SUBCOMMANDS`)
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

#: state and is denied; an UNRECOGNIZED second-level token is denied too
_WORKTREE_MUTATING_SUBCOMMANDS = frozenset(
    {"add", "remove", "prune", "move", "repair", "unlock", "lock"}
)

#: denied; an UNRECOGNIZED second-level token is denied too (default-deny)
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
        # 2026-08-22 fix (UNSCOPED-STASH GAP, REOPENED): strip a leading
        stash_remaining = _strip_leading_redirection_tokens(remaining)
        stash_head = stash_remaining[0] if stash_remaining else None
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
    # 2026-07-25 P0 fix: `_SAFE_GIT_SUBCOMMANDS` used to return `None`
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
        if "--ff-only" in remaining:
            return None
        return "git pull (not --ff-only)"
    if subcmd == "merge":
        if "--ff-only" in remaining:
            return None
        return "git merge (not --ff-only)"
    if subcmd == "push":
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
        # is POSITIONAL (preceded by no flag at all), so a
        if "--get" in remaining:
            return None
        return "git config (not --get)"
    if subcmd == "mv":
        # fallback was the INTENDED path, not a workaround — it only read
        return "git mv (index-mutating rename)"

    return "unrecognized git verb (default-deny)"


#: Action-only counterparts of `_STASH_POP_APPLY_RE`/`_STASH_DROP_CLEAR_RE`
_POP_APPLY_ACTION_RE = re.compile(r"\b(?:pop|apply)\b")
_DROP_CLEAR_ACTION_RE = re.compile(r"\b(?:drop|clear)\b")


# TOKENIZED AUTHORITATIVE PASS (2026-07-21 security-audit hardening,
# verb-identification above (`_GIT_SURFACE_RE`/`_RM_SURFACE_RE`/
# `_CHMOD_CHOWN_RE`/`_evaluate_git_segment`'s raw-text entry) matches a raw
# verb's literal characters from appearing CONTIGUOUS defeats it (`r''m`,
# now the AUTHORITATIVE identification path; the raw-text probes above are
# This pass is purely ADDITIVE alongside every raw-regex path above and the


class _TokenSurfaces:

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
            # referenced value. EXEMPTION: the doctrine-mandated settings-home
            # CLI form (see `_SETTINGS_HOME_BIN_EXEMPT_RE` above) is narrowly
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
            result.is_wrapper = True
            if result.deny_kind is None:
                result.deny_kind = (
                    f"{norm_head} <file> (script sourced in-process -- "
                    "indirection wrapper, script content unexamined)"
                )
            continue

        if norm_head == "eval":
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
            result.is_wrapper = True
            if result.deny_kind is None:
                result.deny_kind = (
                    f"{norm_head} (bare interpreter fed via stdin pipe -- "
                    "indirection wrapper, piped content unexamined)"
                )
            continue

        if norm_head in _C_FLAG_INTERPRETERS:
            result.is_wrapper = True
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
            if norm_head in _SHELL_FILE_INTERPRETERS and _has_script_operand(
                working[1:]
            ):
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


_HEREDOC_OP_RE = re.compile(r"<<-?\s*\\?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredoc_bodies(cmd: str) -> str:
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
            if i < n:
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
        # `-c`-payload scan (`strict=False`), or (b) the INLINE-INTERPRETER
        # CARVE-OUT sentinel (a `-c`-flagged interpreter head even in
        # `strict=True` mode -- `_INTERPRETER_C_PAYLOAD_AMBIGUOUS`) -- both
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
    # (`_STASH_OTHER_ACTION_WORD_RE`) are a different subcommand entirely
    if (
        _STASH_WORD_RE.search(seg)
        and not _STASH_OTHER_ACTION_WORD_RE.search(seg)
        and not _STASH_DROP_CLEAR_RE.search(seg)
        and not _DASHDASH_SEP_RE.search(seg)
    ):
        return "git stash (unscoped)"

    if _REBASE_RE.search(seg):
        return "git rebase"
    if _HARD_FLAG_RE.search(seg) and _RESET_WORD_RE.search(seg):
        return "git reset --hard"
    if _COMMIT_WORD_RE.search(seg) and _AMEND_FLAG_RE.search(seg):
        return "git commit --amend"
    if _PUSH_WORD_RE.search(seg):
        if _PUSH_FORCE_RE.search(_seg_forcing_form_scan_text(seg)):
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

    # second-level form instead of falling through to `_SAFE_VERB_RE`'s
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
            "    the EM — this guard does not block the main loop.\n\n"
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
            "    this guard does not block the main loop, which can drop it after\n"
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
            "    the EM — this guard does not block the main loop.\n\n"
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
        "BLOCKED: destructive git/rm/chmod-chown -R at the shell surface --\n"
        "not a capability boundary; an interpreter still reaches git.\n\n"
        f"  Denied:   {deny_kind}\n"
        f"  Command:  {cmd_safe}\n\n"
        "Fix forward, or surface it to the EM. No subagent-reachable\n"
        "override exists."
    )


# FAIL-OPEN OBSERVABILITY (2026-07-29 addition, pure addition -- see module
# docstring FAIL-POSTURE section for the three fail-open branches this
# per-repo back-pointer the SECONDARY leg reads was never written there).
# This section is OBSERVABILITY ONLY -- it never changes an allow/deny
_FAIL_OPEN_LOG_RELPATH = ("state", "destructive-guard-fail-open.log")
_NOT_ATTEMPTED = "<not-attempted>"

_FAIL_OPEN_LOG_MAX_BYTES = 5 * 1024 * 1024
_FAIL_OPEN_LOG_MAX_GENERATIONS = 3

_SYNTHETIC_CORPUS_SESSION_PREFIX = "guard-message-corpus-"


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


def _rotate_fail_open_log_if_oversized(log_path: Path) -> None:
    try:
        if log_path.stat().st_size < _FAIL_OPEN_LOG_MAX_BYTES:
            return
    except OSError:
        return
    for gen in range(_FAIL_OPEN_LOG_MAX_GENERATIONS, 0, -1):
        if gen == _FAIL_OPEN_LOG_MAX_GENERATIONS:
            oldest = log_path.with_name(f"{log_path.name}.{gen}")
            try:
                oldest.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        src = log_path.with_name(f"{log_path.name}.{gen}")
        dst = log_path.with_name(f"{log_path.name}.{gen + 1}")
        try:
            if src.exists():
                os.replace(src, dst)
        except OSError:
            pass
    try:
        os.replace(log_path, log_path.with_name(f"{log_path.name}.1"))
    except OSError:
        pass


def _is_synthetic_corpus_session(session_id: str) -> bool:
    """True for a `session_id` minted by this package's own
    `guard_message_corpus` test corpus (see `_SYNTHETIC_CORPUS_SESSION_PREFIX`).
    """
    return session_id.startswith(_SYNTHETIC_CORPUS_SESSION_PREFIX)


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

    A `session_id` matching `_is_synthetic_corpus_session` is dropped before
    any rotation check or write -- this package's own test corpus must never
    pollute the production log it is measured against.

    NEVER raises (PreToolUse-hook contract, matching every sibling
    best-effort writer in this codebase, e.g.
    `coordinator_core.ops.ceremony.detached_spawn._log_spawn_failure`): a
    broken observability path must never become the thing that blocks a
    tool call. Any failure -- settings-home resolution, directory
    creation, rotation (`_rotate_fail_open_log_if_oversized`), the write
    itself -- is swallowed silently.
    """
    try:
        session_id = payload.get("session_id") or ""
        if _is_synthetic_corpus_session(session_id):
            return
        log_path = _fail_open_log_path()
        _rotate_fail_open_log_if_oversized(log_path)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        from coordinator_core.session.claimed_write import append_claimed_line  # noqa: PLC0415 -- deferred: guard hot path

        append_claimed_line(log_path, line.encode("utf-8"))
    except Exception:  # noqa: BLE001 -- observability must never raise into check()
        pass


def _sanitize_command(cmd: str) -> str:
    cmd_safe = re.sub(r"[\t\r\n\f\v]", " ", cmd)
    cmd_safe = "".join(ch for ch in cmd_safe if ord(ch) >= 0x20)
    if len(cmd_safe) > 200:
        cmd_safe = cmd_safe[:200] + "..."
    if not cmd_safe:
        cmd_safe = "(empty/unparseable)"
    return cmd_safe


# POWERSHELL-DIALECT DESTRUCTIVE-VERB CLASSIFIER (2026-08-07, this change --
# expensive one"). A SEPARATE classifier from the Bash Layer 1/2 machinery
# `rm` IS included in `_PS_REMOVE_VERBS` below, despite C3 finding 1
# rests on the Bash-leg raw-text `_RM_SURFACE_RE`/`_RM_DENY_RE` pair running
# reached ONLY for `Dialect.POWERSHELL` (a SEPARATE branch off `check()`,
# FLAG-SHAPE: PowerShell has no clustered short flags (`rm -rf` cannot
# matches parameter name 'rf'") and parameter names PREFIX-MATCH, so a
_PS_REMOVE_VERBS = frozenset({"remove-item", "ri", "rd", "del", "erase", "rm"})
_PS_ICACLS_VERBS = frozenset({"icacls"})
_PS_STOP_PROCESS_VERBS = frozenset({"stop-process"})


def _ps_has_flag_prefix(tokens: List[str], dash_prefixes: tuple, slash_exact: frozenset) -> bool:
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


# POWERSHELL GIT-DESTRUCTIVE PARITY PORT (2026-08-19, C3 of docs/plans/
# `_PS_REMOVE_VERBS`/`_PS_ICACLS_VERBS`/`_PS_STOP_PROCESS_VERBS` -- the Bash
# text never reaches (`check()` routes `Dialect.POWERSHELL` to
# THIS IS A PORT, NOT A REIMPLEMENTATION: `git` is an external executable on
# UNINTERPRETED (no cmdlet-style parameter-name prefix-matching applies to
# an external command's own flags, unlike `_PS_REMOVE_VERBS`'s cmdlet-prefix
# a `List[str]`, never on shell-shaped text) are therefore REUSED VERBATIM
# MATCHERS IS NOT WIDENED HERE (AC12, deliberately deferred to a later
# already resolved `Dialect.POWERSHELL` -- i.e. only when `MATCHERS`


_PS_LEGACY_SEPARATOR_RE = re.compile(r"[;\n]|&&|\|\||\|")


def _evaluate_legacy_powershell_git(text):
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

    cmd_norm = cmd.replace("\r", "")

    cmd_norm = _strip_heredoc_bodies(cmd_norm)

    # 2026-07-28 fix (SPACED-WINDOWS-PATH ARGV0 HOLE, see the module comment
    # above `_WINDOWS_ARGV0_HEAD_PATH_RE`): quote-and-normalize an UNQUOTED
    cmd_norm = _normalize_windows_argv0_head_path_with_spaces(cmd_norm)

    cmd_norm = _normalize_windows_wrapper_argv0(cmd_norm)

    is_git_surface = bool(_GIT_SURFACE_RE.search(cmd_norm))
    is_rm_surface = bool(_RM_SURFACE_RE.search(cmd_norm))
    is_chmod_chown_r_surface = False
    if _CHMOD_CHOWN_RE.search(cmd_norm) and _RECURSIVE_FLAG_RE.search(cmd_norm):
        is_chmod_chown_r_surface = True
    # 2026-07-21 addition (see module docstring "INDIRECTION-WRAPPER
    # HARDENING"): a fourth Layer-1 probe, cheap and boundary-anchored like
    is_wrapper_surface = bool(_WRAPPER_PROBE_RE.search(cmd_norm))

    tok_surfaces = _evaluate_tokenized(cmd_norm)
    is_git_surface = is_git_surface or tok_surfaces.is_git
    is_rm_surface = is_rm_surface or tok_surfaces.is_rm
    is_chmod_chown_r_surface = is_chmod_chown_r_surface or tok_surfaces.is_chmod_chown_r
    is_wrapper_surface = is_wrapper_surface or tok_surfaces.is_wrapper
    # MACHINE-LOCAL REGISTRY WRITE DENY (see module comment above
    # `_MACHINE_LOCAL_WRITE_SUBCOMMANDS`) -- tokenized-only surface, no raw-
    is_machine_local_surface = tok_surfaces.is_machine_local

    if not (
        is_git_surface
        or is_rm_surface
        or is_chmod_chown_r_surface
        or is_wrapper_surface
        or is_machine_local_surface
    ):
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

    # IDENTITY AXIS -- DUAL OR-resolver via the shared engine helper.
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
    effective_type = agent_type or subagent_type or ""
    # AMBIGUOUS on the SECONDARY leg overrides a populated PRIMARY leg
    if subagent_type == "AMBIGUOUS":
        effective_type = "AMBIGUOUS"

    # `effective_type` except for the AMBIGUOUS override above, which cannot
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
        # effective_type except the AMBIGUOUS override above), with the

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

    # `GuardEntry` registration (band CONFINEMENT_DENY, fail_closed=True) is
    # UNCHANGED -- this branch returns an allow+additionalContext envelope
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
