"""coordinator_core.bash_guards.dispatch_checks -- the 11 checks folded into
DoE's retired ``coordinator/hooks/scripts/preuse-bash-dispatch.sh`` (deleted
2026-07-16, DoE ``2f8b8450``) (hard/soft/content/ advisory phases), ported
per the W3a/W3b naked-Python hook migration recipe
(scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md
Sec(c)).

These are DISTINCT from the 5-fold subagent-identity cohort already ported in
this package (block_illegal_filename.py, block_reviewer_bash_outside_allowlist.py,
block_subagent_destructive_action.py, block_subagent_plan_body_bash_write.py,
nudge_subagent_scoped_commit.py -- CLASS/MATCHERS/PRIORITY/check(payload)
discovery-module shape). The 11 checks here instead mirror their bash
predecessors' own ``check_<name>(cmd, session_id[, cwd])`` sourceable-function
contract 1:1 -- ``dispatch.py`` (the naked-Python dispatcher) calls them
directly in the exact bash dispatcher's phase order; there is no
discovery/registration layer for this cohort (recipe Sec(c) names the phase
order as load-bearing structure the port must preserve exactly, not something
a generic discovery loop should re-derive).

Each ``check_*`` function:
  - takes the RAW (not yet CRLF-stripped) command string as ``cmd`` and the
    ``session_id`` (some also take ``cwd``); strips its OWN CR at entry
    (mirrors the bash "redundant-but-safe insurance" double-strip discipline
    the recipe calls out -- Sec(c) "Parse-once contract").
  - returns ``None`` on ALLOW (prints nothing in bash) or the nested
    ``{"hookSpecificOutput": {...}}`` envelope dict on DENY/rewrite/advisory
    (mirrors bash Form-A JSON on stdout).
  - never raises for an ordinary "nothing to do" input (empty command, no
    matching verb) -- returns ``None``. A genuine internal crash indicates a
    programming bug in this module, not user input; dispatch.py's F1 harness
    (per-check try/except, hard-guards fail-closed / others fail-open) is the
    outer safety net, matching the bash dispatcher's `rc != 0` -> crash-deny
    semantics for the hard chain.

Ported from these retired DoE bash guards (DoE coordinator/hooks/scripts/,
all deleted 2026-07-16, DoE ``2f8b8450``, except ``validate-commit.sh``
deleted 2026-07-20, DoE ``e91827a7``):
  block-no-verify.sh, block-destructive-git-orphan.sh, block-destructive-rm.sh,
  block-destructive-git-clean.sh, block-destructive-git-revert.sh,
  block-blanket-git-add.sh, block-runaway-find.sh, offer-git-c-over-cd.sh,
  nudge-probe-spray.sh, validate-commit.sh
  (nudge-windows-console-popup.sh retired -- DR-054, 2026-07-15)
Spec backlink: scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md Sec(c)

KNOWN PORTING GAPS (surfaced, not silently patched -- see the W3b dispatch
task's own instruction to report rather than "improve" a guard):
  - ``check_validate_commit`` ports Check 7 (CLAUDE.md budget, deny-capable),
    Check 5 (scoped-staging) and Check 8 (frontmatter-mutation subject
    discipline), and delegates Checks 9-11. Check 8 ports ONLY its warn-only
    default-mode behavior (non-strict) -- the bash original also carries a
    DORMANT strict-mode deny branch (``COORDINATOR_FRONTMATTER_STRICT=1``,
    env-gated and off by default) that this port deliberately does NOT carry
    -- porting a dormant deny path that has never fired in production is out
    of scope for a warn-only-parity pass. Check 8's frontmatter-path prefix
    also skips the bash's ``coordinator-state-root.sh``-derived
    ``_vc_handoffs_prefix`` seam (no Python ``coordinator_state_root`` port
    exists yet -- see the same gap noted in ``coordinator_core/ops/
    review_trail_write.py`` and ``coordinator_core/ops/cruft_sweep.py``) and
    hardcodes the bash's own post-migration fallback value
    ``state/handoffs`` (the state-root-outside-git-root case, now the common
    case per DR-047).

CLOSED PORTING GAPS:
  - (2026-07-31, incident + code-reviewer findings 1/2/3/4/5/6) The 2026-07-31
    `~/.claude` destruction (see `check_destructive_rm`'s own root-deny
    comment and `test_check_destructive_rm_repo_root.py`) turned up four
    further gaps in the same function beyond the root-resolution/identity
    fix itself: a bare repository target bypassed the guard entirely (no
    `--show-toplevel` output and no dirty-state fallback for a bare repo --
    now probed via `--is-bare-repository`); the root-deny and dirty-work
    branches spawned an identical `git -C <dir> rev-parse --show-toplevel`
    twice per directory target (now computed once and threaded through);
    the incident's own literal command (`rm -rf ~/.Claude`) was STILL
    allowed post-fix because the target-token loop never tilde-expanded
    `tgt` before its `os.path.exists` gate, and unconditionally skipped any
    token containing `$` -- including `$HOME`-relative targets, the same
    incident class under a different spelling (now: `~` expanded via
    `os.path.expanduser`, and `$HOME`/`${HOME}` narrowly resolved via
    `_expand_home_var`; other `$VAR`/glob tokens remain a known, unchanged
    bound, see the token-loop comment and finding 8); and the deny message
    overclaimed full history loss for a linked `git worktree add` target
    (whose store lives in the main repo), now branched by
    `is_linked_worktree`.
  - (2026-07-24, g4-M1) Check 5's ``COORDINATOR_SCOPE_STRICT=1`` dormant
    deny branch is now ported: a staged file outside this session's
    ``compute_scope()`` result DENIES (instead of warns) when the env var is
    set, matching the bash's Phase-5 strict-mode intent. Warn-only remains
    the default (env var unset). Reuses ``compute_scope()`` -- no new
    declared-scope carrier was introduced.
  - (2026-07-16, Q24) ``_rm_peer_claim_of``'s liveness source used the
    30-minute ``touched.txt`` mtime backstop UNCONDITIONALLY -- now prefers
    the canonical ``coordinator_core.session.liveness.live_session_ids`` set
    per-sid (a sid with a ``meta.json`` is in canonical's scan scope; a sid
    with no ``meta.json`` degrades to the mtime backstop), matching the bash
    ``_rm_peer_claim_of`` / ``_gr_peer_claim_of`` decision tree. See the
    function's own docstring for the exact tree.
  - (2026-07-16, bug-backlog 2026-07-16-dispatch-checks-py-peer-claim-guard-
    omit-811b3291dd8a.yaml, code-reviewer Finding 3) ``_rm_peer_claim_of`` did
    NOT thread the caller's own session id, so it could match the current
    session's own ``touched.txt`` claim on a target it itself just created
    and DENY (not-overridable) the session's own scratch as "claimed by LIVE
    peer session <own-sid>" -- now resolves ``cur_sid`` via
    ``coordinator_core.session.core.resolve_session_id(cwd=root)`` and skips
    it in the per-sid scan (bash's ``cur_sid`` / ``cs_resolve_session_id``
    self-exclusion, now ported). See the function's own docstring.
  - (2026-07-20, Windows de-bash campaign) ``check_validate_commit``'s Checks
    9-11 (schema-version-bump tripwire, bin/sh polyglot shebang tripwire,
    machine-path-leak guard) no longer delegate to ``bin/*.sh`` via
    subprocess-by-filename -- ported in-process to
    ``coordinator_core.bash_guards.commit_tripwires``. The prior filename-based
    lookup (``_find_bin_script``/``_delegate_bin_check``, both removed) was
    confirmed dead on this machine at port time: the machine-path-leak script
    had already been renamed ``.sh``->``.py`` DoE-side, and this install's
    ``~/.claude`` plugin mirror had an empty ``bin/`` besides. See
    ``commit_tripwires.py``'s own module docstring for the full writeup.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.bash_guards import commit_tripwires
from coordinator_core.bash_guards._helpers import operator_override_note

if TYPE_CHECKING:
    from coordinator_core.session.scope import OwnerFact

# Generator-provenance declaration (generator_provenance.py). Every real
# write in this module (override_log, the probe-spray ring/times/cooldown
# files, scope-warnings.log) resolves under <git_root>/.git/coordinator-
# sessions/<session_id>/ or a session_dir -- untracked guard bookkeeping,
# never a tracked repo artifact.
GENERATES = []
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _normalize_windows_argv0_head_path_with_spaces,
)
from coordinator_core.bash_guards._command_tokenizer import (
    _extract_command_substitutions as _bt_extract_command_substitutions,
    _skip_wrapper_own_argv,
    exceeds_tokenizable_ceiling as _bt_exceeds_tokenizable_ceiling,
    segments_from_tokens_simple as _bt_segments_from_tokens_simple,
    segments_from_tokens_with_pipe_flag as _bt_segments_from_tokens_with_pipe_flag,
    token_matches_binary as _bt_token_matches_binary,
    tokenize_full_command as _bt_tokenize_full_command,
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _BT_Shape,
    classify_command as _bt_classify_command,
)

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Review: code-reviewer (Finding 3) -- `coordinator_core.session.{liveness,
# core,scope}` are deliberately NOT imported at module scope. A module-level
# ImportError in any of those (unrelated future edits) would break THIS
# module's own import, taking down `dispatch.py`'s whole dispatcher process --
# including every hard-deny check (block-destructive-rm et al.), not just the
# two checks that actually need session-state. Each import is instead done
# lazily, at the single call site that needs it, wrapped so an ImportError
# degrades to that check's OWN pre-existing safe fallback -- never to "allow"
# for a destructive guard. See `_rm_peer_claim_of` and `check_validate_commit`
# Check 5 for the two call sites.


# ---------------------------------------------------------------------------
# Shared low-level helpers (mirror the bash `_xxx_strip_q` / `_xxx_deny` /
# segment-split / heredoc-strip idioms repeated near-verbatim across the
# sibling .sh files per their own header comments).
# ---------------------------------------------------------------------------

def _crlf_strip(s: str) -> str:
    return s.replace("\r", "") if s else s


def _join_backslash_newlines(cmd: str) -> str:
    """Join `\\<NL>` line-continuations into a single space (mirrors
    `CMD="${CMD//\\$NL/ }"`)."""
    return cmd.replace("\\\n", " ")


#: BX-13 (2026-07-29, confirmed live via the real dispatcher): several
#: checks in this module scan raw/segmented command TEXT with a pattern
#: anchored at a `;`/`&`/`|`/string-start boundary, so a quoted
#: `sh -c '<payload>'` (or `bash -c`/`env sh -c`/etc.) argument -- which the
#: invoking shell WILL execute -- was never unwrapped and re-scanned; only
#: the OUTER `sh -c '...'` text was seen, and a `git`/`rm`/`find` token
#: inside the quoted payload never sat at a recognized boundary. Own-module
#: copy of the identical fix shipped in `block_subagent_commit.py`'s
#: `_wrapped_shell_c_payloads` (not shared via cross-module import -- this
#: file's own established no-cross-module-coupling precedent for small
#: tokenizer-adjacent helpers).
_SHELL_C_WRAPPER_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_MAX_SHELL_C_UNWRAP_DEPTH = 4

#: Bundled-or-standalone `-c` short flag, e.g. `-c`, `-ic`, `-ci` (Finding,
#: 2026-07-29 EM-run second-pass integration): a shell's CLI parser accepts
#: bundled short flags, so `sh -ic '<payload>'` behaves as `sh -i -c
#: '<payload>'` -- an exact `"-c" in rest` check (below) missed this
#: entirely, so `sh -ic "git commit --no-verify"` was never unwrapped and
#: re-scanned by ANY of this module's checks that rely on
#: `_shell_c_unwrap_payloads` (`check_no_verify`, `check_destructive_rm`,
#: `check_destructive_git_revert`, `check_blanket_git_add`,
#: `check_runaway_find`). Own-module copy of `block_subagent_commit.py` /
#: `block_subagent_destructive_action.py`'s identically-named constant (no-
#: cross-module-coupling convention already established in this file).
_BUNDLED_C_FLAG_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")


def _shell_c_unwrap_single_segment(tokens: List[str]) -> Optional[str]:
    """The single-segment scan `_shell_c_unwrap_payloads` used to run
    against the WHOLE command text (pre-cd-prefix-bypass-fix): find a
    wrapper interpreter (optionally `env`-/assignment-prefixed) at the head
    of `tokens` and return its `-c` payload, or `None` if `tokens` is not a
    wrapper invocation. `tokens` is one caller-supplied segment's token
    list -- this function does no splitting or tokenizing of its own."""
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    if i < len(tokens) and os.path.basename(tokens[i].replace("\\", "/")) == "env":
        i += 1
        while i < len(tokens) and (
            tokens[i] in ("-i", "--ignore-environment")
            or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
        ):
            i += 1
    if i >= len(tokens):
        return None
    head = os.path.basename(tokens[i].replace("\\", "/"))
    head = re.sub(r"\.exe$", "", head, flags=re.IGNORECASE)
    if head not in _SHELL_C_WRAPPER_INTERPRETERS:
        return None
    rest = tokens[i + 1 :]
    c_flag_positions = [j for j in range(len(rest)) if _BUNDLED_C_FLAG_RE.match(rest[j])]
    if not c_flag_positions:
        return None
    idx = c_flag_positions[0]
    if idx + 1 >= len(rest):
        return None
    return rest[idx + 1]


def _shell_c_unwrap_payloads(cmd: str, depth: int = 0) -> List[str]:
    """Return the nested command-text payload(s) of any `sh -c`/`bash -c`/
    `zsh -c`/`dash -c`/`ksh -c` invocation in `cmd` (optionally `env`-/
    assignment-prefixed), so a caller can re-run ITS OWN matcher against the
    ACTUAL executed text too. Fails safe: returns `[]` (nothing extra to
    scan, never a crash or a false deny) on unparseable input or nesting
    past `_MAX_SHELL_C_UNWRAP_DEPTH`.

    Bug fix (2026-08-17, cd-prefix unwrap gap): the wrapper was previously
    looked for ONLY at token position 0 of the whole `cmd` text, on the
    premise that multi-segment chaining ahead of the wrapper was "already
    caught by each caller's own segment loop before this helper is ever
    consulted" -- that premise was false. None of the six call sites
    (`check_no_verify`, `check_destructive_git_orphan`, `check_destructive_
    rm`, `_check_destructive_git_revert_full`, `check_blanket_git_add`,
    `check_runaway_find`) segments `cmd` before calling this helper; each
    hands it the raw command text once. `cd /tmp && sh -c '<payload>'`
    therefore tokenized to `['cd', '/tmp', '&&', 'sh', '-c', '<payload>']`,
    and the position-0 check saw `cd`, never `sh` -- a live bypass of every
    one of those six checks, confirmed via direct probe.

    Now segments `cmd` itself via the same quote-aware tokenizer/segmenter
    every other guard in this package shares (`tokenize_full_command` +
    `segments_from_tokens_simple`, `_command_tokenizer.py`) and looks for
    the wrapper at the head of EACH segment, not just the first. This is
    strictly additive: a bare `sh -c '...'` (no leading segment) still
    matches on its own now-single segment exactly as before, and a quoted
    `;`/`&`/`|` (`echo "a && b"`) is never treated as a segment boundary,
    because the shared tokenizer already respects quoting -- unlike this
    function's PRIOR `shlex.split(cmd, posix=True)` call, which had no
    `punctuation_chars` set and so never split on separators at all
    (relying entirely on position 0 instead). A payload found in ANY
    segment is unwrapped and recursively re-scanned, same as before.

    Bug fix (2026-08-17, command-substitution unwrap gap): `echo $(sh -c
    '<payload>')` and the backtick equivalent still bypassed every caller --
    the shared tokenizer has no `$(...)`-aware grouping (a deliberate scope
    boundary, not fixed here: it is load-bearing for 5+ other guard
    modules), so a wrapper hidden inside a command substitution was never a
    real segment head and never reached `_shell_c_unwrap_single_segment`.
    Fixed WITHOUT touching the shared tokenizer's grouping: the existing
    segment loop below still runs on `cmd` completely unmodified (see its
    own comment for why -- a wrapper's `-c` payload can legitimately CONTAIN
    a `$(...)` a downstream caller still needs verbatim), and a SEPARATE
    pass runs this package's own `_extract_command_substitutions`
    (`_command_tokenizer.py`, already used by `resolve_command_positions`
    for the identical reason) over that same `cmd` to pull out every
    unquoted top-level `$(...)`/backtick span, quote-aware -- `echo
    '$(...)'` (single-quoted, literal data) yields no substitutions at all,
    while `echo "$(...)"` (double-quoted) still yields one, matching real
    shell semantics where only single quotes suppress substitution. Each
    extracted inner command text is recursed into via this SAME function
    (one extra `depth`), so a wrapper nested inside a substitution is
    unwrapped exactly like a wrapper nested inside another wrapper's payload
    already was -- same `_MAX_SHELL_C_UNWRAP_DEPTH` bound, and nested
    `$(...)` spans (paren-balanced by `_extract_command_substitutions`)
    unwrap one level per recursion rather than all at once. Each `subs`
    entry is a strictly-smaller substring of `cmd`, but NOT necessarily
    disjoint from the segment loop's own view of `cmd`: a double-quoted
    wrapper `-c` payload that itself contains a `$(...)` gets that inner
    substitution discovered TWICE -- once when the segment loop's own
    `_shell_c_unwrap_single_segment` recurses into the whole `-c` payload
    text (which still contains the `$(...)` verbatim, per the comment
    below), and again here when the substitution walk finds the same
    `$(...)` independently in `cmd`. This is harmless (bounded by
    `_MAX_SHELL_C_UNWRAP_DEPTH`, and it makes this function OVER-scan a
    payload, never under-scan one) but it is real double-work, not a
    partition -- see the fuller note further down where this was found and
    the comment corrected (n04, this file).
    """
    if depth > _MAX_SHELL_C_UNWRAP_DEPTH:
        return []
    if _bt_exceeds_tokenizable_ceiling(cmd):
        # DoS bound inherited from `_command_tokenizer`. NOT redundant with
        # `tokenize_full_command`'s own internal ceiling check below now
        # that `_extract_command_substitutions` also runs over `cmd` before
        # this function returns: that helper is a manual scan with no
        # ceiling of its own, so this explicit gate is what stops it (and
        # not just the tokenizer call) from ever seeing an over-ceiling `cmd`.
        return []
    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return []
    payloads: List[str] = []
    for seg_tokens in _bt_segments_from_tokens_simple(tokens):
        payload = _shell_c_unwrap_single_segment(seg_tokens)
        if payload is None:
            continue
        payloads.append(payload)
        payloads.extend(_shell_c_unwrap_payloads(payload, depth + 1))
    # Command substitutions: scanned on the UNTOUCHED `cmd`, never on a
    # neutralized copy fed to the segment loop above. An earlier version of
    # this fix tokenized a neutralized `cmd` (each `$(...)`/backtick span
    # collapsed to one space) for BOTH the segment loop and the substitution
    # walk, on the theory that this kept the two scans a disjoint partition
    # of `cmd`. That neutralization is wrong for THIS loop specifically: a
    # wrapper's own `-c` payload can legitimately CONTAIN a `$(...)` a
    # downstream caller still needs to see verbatim (`/bin/sh -c "git reset
    # --hard $(echo HEAD~3)"` -- `check_destructive_git_orphan`'s own CHECK 1
    # denies a subshell-resolved reset target on sight, but only if the
    # unwrapped payload it re-scans still HAS the `$(...)` in it; neutralizing
    # it first silently downgraded the target to the "no bare token found"
    # default of `HEAD`, turning a hard deny into a false allow). So the
    # segment loop above always sees `cmd` verbatim, preserving every
    # wrapper payload's real text unmodified.
    #
    # The substitution walk below is a SEPARATE, ADDITIONAL pass over that
    # same `cmd` text, not a second consumer of the segment loop's already-
    # tokenized `tokens` -- needed because `tokenize_full_command` has no
    # `$(...)`-aware grouping (a shared-tokenizer scope boundary, not fixed
    # here), so a BARE, unquoted `$(sh -c '...')` glues `$(` onto the
    # wrapper's own name (`'$(sh'` as one token) and the segment loop above
    # can never see `sh` at a segment head to begin with -- confirmed via
    # `tokenize_full_command("echo $(sh -c '...')")` returning a single
    # `['echo', '$(sh', '-c', "...)"]` segment, head `echo`, no wrapper
    # match. `_extract_command_substitutions` is quote-aware exactly like
    # the segment loop's own tokenizer (single-quoted `$(...)` is literal
    # data and yields no substitution; double-quoted and bare `$(...)` both
    # do, matching real shell semantics), so each extracted inner command
    # text is recursed into via this SAME function at `depth + 1`. This
    # scans `cmd`'s bytes a second time at THIS level (once for segments,
    # once for substitutions) -- a bounded constant-factor duplication of
    # the CURRENT level's work, not a per-level multiplier. Each `subs`
    # entry is a strictly-smaller substring of `cmd`, but NOT provably
    # disjoint from what the segment loop above already recursed into: a
    # double-quoted wrapper `-c` payload containing its own `$(...)` is
    # recursed into once by the segment loop (the whole payload, `$(...)`
    # and all -- see the comment above on why it is left verbatim) and
    # again here (the `$(...)` alone, found independently by this
    # substitution walk). Confirmed live (2026-08, n04): harmless -- still
    # bounded by `_MAX_SHELL_C_UNWRAP_DEPTH`, and the duplication makes this
    # function OVER-scan such a payload, never under-scan one -- but it is
    # real overlapping work, not a clean partition, so total work across the
    # bounded recursion is linear in `cmd`'s length times the depth bound
    # with a bounded constant-factor overhead, never exponential.
    _, subs = _bt_extract_command_substitutions(cmd)
    for sub in subs:
        payloads.extend(_shell_c_unwrap_payloads(sub, depth + 1))
    return payloads


def _strip_q(t: str) -> str:
    """Strip ONE layer of surrounding single/double quotes (mirrors
    `_rm_strip_q`/`_orphan_strip_q`/`_find_strip_q`)."""
    if len(t) >= 2:
        if t[0] == '"' and t[-1] == '"':
            return t[1:-1]
        if t[0] == "'" and t[-1] == "'":
            return t[1:-1]
    # bash impl strips independently from each end (asymmetric quote pairs
    # tolerated) -- replicate that rather than requiring symmetric quotes.
    if t.endswith('"'):
        t = t[:-1]
    if t.startswith('"'):
        t = t[1:]
    if t.endswith("'"):
        t = t[:-1]
    if t.startswith("'"):
        t = t[1:]
    return t


def _split_segments(cmd: str) -> List[str]:
    """Split on runs of `;`, `&`, `|` (mirrors `sed -E 's/[;&|]+/\\n/g'`)."""
    return re.split(r"[;&|]+", cmd)


def _strip_ws_quoted_spans(s: str) -> str:
    """Port of `_orphan_strip_ws_quoted_spans` -- delete whitespace-containing
    quoted spans, single pass, own-quote pairing (no open/close ambiguity)."""
    out: List[str] = []
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        if c in ("\"", "'"):
            q = c
            j = i + 1
            span_start = j
            found = False
            while j < n:
                if s[j] == q:
                    found = True
                    break
                j += 1
            if found:
                span = s[span_start:j]
                if not re.search(r"[ \t\n]", span):
                    out.append(q + span + q)
                i = j + 1
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


_HEREDOC_INTRO_RE = re.compile(r"<<-?('[^']*'|\"[^\"]*\"|[A-Za-z0-9_.+-]+)")


def _strip_heredoc_bodies(cmd: str) -> str:
    """Port of the awk heredoc-body-stripping state machine shared verbatim
    across block-destructive-rm.sh / -git-clean.sh / -git-revert.sh. Fail-safe:
    on an unterminated heredoc, returns ``cmd`` UNCHANGED (mirrors the bash
    `[[ rc -eq 0 ]] && CMD="$STRIPPED"` discard-on-failure)."""
    lines = cmd.split("\n")
    out_lines: List[str] = []
    in_hd = False
    hd_word = ""
    hd_strip = False
    for line in lines:
        if not in_hd:
            m = _HEREDOC_INTRO_RE.search(line)
            if m:
                seg = m.group(0)
                hd_strip = len(seg) > 2 and seg[2] == "-"
                w = seg[3:] if hd_strip else seg[2:]
                if w and w[0] in ("'", '"'):
                    w = w[1:-1]
                hd_word = w
                in_hd = True
            out_lines.append(line)
        else:
            check = line
            if hd_strip:
                check = re.sub(r"^\t+", "", check)
            if check == hd_word:
                in_hd = False
            # suppressed (heredoc body line) -- not appended
    if in_hd:
        # Unterminated heredoc -- fail-safe, discard the strip.
        return cmd
    return "\n".join(out_lines)


_HEREDOC_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "eval"})

# Interpreters that CAN spawn a subprocess (and so could shell out to a real
# `git push --force`/`git branch -D`) but usually don't -- a heredoc body fed
# to one of these is only prose-stripped if it shows NO sign of doing so (see
# `_heredoc_body_has_spawn_indicator`). Found via the 2026-07-2x coordinator
# probe (probe_guard.py cases D/E): a `python3 - <<'PY'` / `node <<'JS'` body
# that calls `subprocess.run([...])` / `execSync(...)` was being blanket-
# stripped as prose alongside the genuinely-prose case (a python heredoc that
# only WRITES a doc discussing force-pushing) -- both were "not a shell
# interpreter" under the original binary classification. This repo's own
# bash-to-Python porting campaign makes "Python heredoc that shells out to
# git" a growing shape, not a curiosity -- it needs its own middle tier.
_HEREDOC_SCRIPTABLE_INTERPRETERS = frozenset({
    "python", "python3", "python2", "perl", "ruby", "node", "nodejs", "php",
})

# Conservative substring scan (not a single monolithic regex) over a
# scriptable-interpreter heredoc body -- any hit means the body might spawn a
# subprocess, so it stays VISIBLE to CHECK 2/3 rather than being stripped as
# prose. Matched case-insensitively against the whole body. Covers the
# process-spawning constructs of the languages in
# `_HEREDOC_SCRIPTABLE_INTERPRETERS`: Python (subprocess, os.system,
# os.popen, commands.), Perl/Ruby (system(, exec(, backtick command
# substitution, qx(/qx{/qx/, %x(/%x{, Open3), Node (child_process, exec(,
# execSync, spawn(, spawnSync), PHP (system(, exec(, shell_exec(,
# proc_open(, backticks). A body with none of these tokens is prose (writes
# a file, prints text, etc.) and is stripped exactly like a non-scriptable
# prose heredoc.
_SPAWN_INDICATOR_TOKENS = (
    "subprocess", "os.system", "os.popen", "commands.", "shell_exec(",
    "proc_open(", "system(", "exec(", "execsync", "spawnsync", "spawn(",
    "child_process", "qx(", "qx{", "qx/", "open3", "%x(", "%x{",
)

#: Backtick is a spawn indicator in exactly the languages where it MEANS
#: command substitution. Perl/Ruby/PHP execute `` `cmd` ``; Python and Node
#: do not -- a backtick there is a syntax error, never a subprocess. Keeping
#: it unconditional made a markdown code span inside a Python heredoc's
#: string literal (`` `git reset --hard` `` written as documentation) read as
#: a spawn, holding the whole body visible to CHECK 1/2/3, which then denied
#: the prose. Negative spec: this narrows the indicator by INTERPRETER, never
#: by body content -- every Perl/Ruby/PHP backtick deny is retained.
#: Reported by DoE-claude, cross-repo memo
#: `2026-08-19-doe-claude-em-check1-hazard-prose-false-positive-reproduced-and-bounded.md`.
_BACKTICK_SUBSTITUTION_INTERPRETERS = frozenset({"perl", "ruby", "php"})


def _heredoc_body_has_spawn_indicator(
    body_lines: List[str], interpreter: Optional[str] = None
) -> bool:
    """True if `body_lines` (a scriptable-interpreter heredoc body) contains
    any token from `_SPAWN_INDICATOR_TOKENS`, case-insensitively. A single
    substring scan over the joined body is intentionally coarse (a
    conservative token scan, not a language parser) -- false positives (a
    body that merely MENTIONS "subprocess" in a comment/string) only cost a
    body staying visible to CHECK 2/3's existing regex scan, which is
    itself tolerant of prose (see CHECK 1's oracle-verification precedent);
    they can never cause a false ALLOW.

    `interpreter` is the resolved command word of the introducing line (see
    `_heredoc_intro_command_word`). A backtick counts as a spawn indicator
    only for `_BACKTICK_SUBSTITUTION_INTERPRETERS`; an unknown/absent
    interpreter fails CLOSED and keeps counting it."""
    text = "\n".join(body_lines).lower()
    if any(tok in text for tok in _SPAWN_INDICATOR_TOKENS):
        return True
    if interpreter is None or interpreter not in _HEREDOC_SCRIPTABLE_INTERPRETERS:
        return "`" in text
    return interpreter in _BACKTICK_SUBSTITUTION_INTERPRETERS and "`" in text


def _line_has_shell_in_command_position(line: str) -> bool:
    """True if ANY segment of `line` (split on `;`/`&`/`|`, mirroring
    `_split_segments`) has a shell interpreter (bash/sh/zsh/dash/ksh, or
    `eval`) as its leading command word (after skipping wrapper/env
    tokens). Covers a shell invoked ANYWHERE on the heredoc-introducing
    line, not just as the line's own first word -- `cat <<'EOF' | bash`,
    `sudo bash <<'EOF'`, `foo && bash <<'EOF'` all hit this. (2026-07-2x
    coordinator probe case G: a heredoc piped into `bash` after a `cat`
    prefix is not prose -- the pipe target executes it.)

    Fails closed PER SEGMENT: an unparseable segment, or one with no
    resolvable command word, counts as a hit for THAT segment (so the
    overall scan returns True) -- an ambiguous line is never classified as
    definitively non-shell.
    """
    for seg in _split_segments(line):
        seg = seg.strip()
        if not seg:
            continue
        if _bt_exceeds_tokenizable_ceiling(seg):
            # DoS bound inherited from `_command_tokenizer`, not a local
            # tuning knob -- same fail-closed `True` an unparseable segment
            # already takes.
            return True
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            return True
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in _RM_WRAPPER_WORDS:
                i += 1
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
                i += 1
                continue
            break
        if i >= len(tokens):
            return True
        cmd_tok = os.path.basename(tokens[i])
        if cmd_tok in _HEREDOC_SHELL_INTERPRETERS:
            return True
    return False


def _classify_heredoc_intro(prefix: str, full_line: str = "") -> str:
    """Classifies a heredoc by what its introducing line (the text UP TO the
    ``<<`` operator) invokes. Returns one of:

      - ``"shell"`` -- a shell interpreter (bash/sh/zsh/dash/ksh, or `eval`)
        that will itself EXECUTE the heredoc body as further commands (e.g.
        ``bash <<'EOF' ... EOF``). ALWAYS visible to CHECK 2/3 -- a real
        forcing push/branch-delete written inside it actually runs.
      - ``"scriptable"`` -- an interpreter that CAN spawn a subprocess but
        usually doesn't (python/python3/python2/perl/ruby/node/nodejs/php).
        Visible to CHECK 2/3 only if the body itself contains a spawn
        indicator (see `_heredoc_body_has_spawn_indicator`); otherwise
        treated as prose and stripped.
      - ``"prose"`` -- a RECOGNIZABLE command word that is neither a shell
        nor a scriptable interpreter (e.g. `git`, `cat`, `tee`) -- the
        heredoc body is data for that command (a commit message, a file's
        contents), not something that gets interpreted/executed. Always
        treated as prose and stripped -- this is the `git commit -F -
        <<'MSG'` / `cat > file <<'EOF'` case.
      - ``"unknown"`` -- an unparseable prefix, or a prefix with NO
        resolvable command word (e.g. bare `<<'EOF'`, or only wrapper/env
        tokens) -- genuinely ambiguous, not a recognized non-executing
        command. Fails CLOSED to ALWAYS visible, identical to `"shell"` --
        this can only widen what stays visible, never narrow it into a
        false ALLOW.
    """
    if _line_has_shell_in_command_position(full_line if full_line else prefix):
        return "shell"
    cmd_tok = _heredoc_intro_command_word(prefix)
    if cmd_tok is None:
        return "unknown"
    if cmd_tok in _HEREDOC_SHELL_INTERPRETERS:
        return "shell"
    if cmd_tok in _HEREDOC_SCRIPTABLE_INTERPRETERS:
        return "scriptable"
    return "prose"


def _heredoc_intro_command_word(prefix: str) -> Optional[str]:
    """Resolve the command word a heredoc-introducing `prefix` invokes (its
    basename, wrapper words and `VAR=` assignments skipped), or `None` when
    that cannot be determined -- an unparseable prefix, one over the
    tokenizable ceiling, or one with no command word at all.

    Extracted from `_classify_heredoc_intro` so the resolved interpreter is
    available to `_heredoc_body_has_spawn_indicator`'s backtick arm as well
    as to the classification itself. `None` is the fail-CLOSED value at both
    call sites: `"unknown"` classification, and backtick-counts-as-spawn."""
    if _bt_exceeds_tokenizable_ceiling(prefix):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- `None` is the documented always-visible fail-closed
        # answer an unparseable prefix already lands on.
        return None
    try:
        tokens = shlex.split(prefix, posix=True)
    except ValueError:
        return None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _RM_WRAPPER_WORDS:
            i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    return os.path.basename(tokens[i])


_QUOTE_SENTINEL_SQ = ""
_QUOTE_SENTINEL_DQ = ""


def _protect_line_quotes(line: str) -> str:
    """Replace `'`/`"` with Private-Use-Area sentinel chars so a downstream
    whole-string pass (`_strip_ws_quoted_spans`) cannot quote-pair across
    this line and delete a multi-word quoted span it contains (e.g. a
    Python/Node source string literal like `'git push origin main
    --force'`). Reversed by `_restore_protected_quotes` immediately after
    that pass runs. Only applied to heredoc-body lines CHECK 2/3 must keep
    visible (see `_strip_heredoc_bodies_for_prose_scan`) -- never to prose
    lines, which are discarded outright, and never to any other check in
    this file (`_strip_ws_quoted_spans` has exactly one call site)."""
    return line.replace("'", _QUOTE_SENTINEL_SQ).replace('"', _QUOTE_SENTINEL_DQ)


def _restore_protected_quotes(s: str) -> str:
    return s.replace(_QUOTE_SENTINEL_SQ, "'").replace(_QUOTE_SENTINEL_DQ, '"')


def _strip_shell_comments_for_prose_scan(cmd: str) -> str:
    """Drop shell `#` comments from `cmd` before
    `check_destructive_git_orphan` pattern-matches it, so prose in a comment
    can never trip a check that is supposed to match what the command DOES.
    Same contract as `_seg_excluding_freetext_operands`, one layer out:
    a comment is text the shell never executes, so removing it can only
    NARROW what CHECK 1/2/3 see, never widen it into a false ALLOW.

    Negative spec: this is NOT a heredoc fix. The reported case
    (`python -c "print(1)"  # the doc mentions `` `git reset --hard` ``)
    carries no heredoc at all -- CHECK 1 builds `after` as everything
    following the first `reset` token, comments included, so a markdown code
    span in a trailing comment read as a command substitution.
    `_seg_resolved_git_subcommand`'s docstring already names the mirror-image
    hazard (`git push origin main --force # git stash push` suppressing
    CHECK 2 via a comment); stripping comments strengthens that walk rather
    than competing with it.

    A `#` opens a comment only at word start (line start or after
    whitespace) and outside quoting -- `${x#y}`, `foo#bar` and a `#` inside
    a quoted span are all left alone. Quote state is tracked per line and
    over the Private-Use-Area sentinels `_protect_line_quotes` substitutes,
    since kept heredoc-body lines reach this function still protected.

    Fails closed per line, never open: a line whose quoting does not close
    (unterminated quote, or one over the tokenizable ceiling) is returned
    UNCHANGED, so the caller's raw regex scan runs exactly as before --
    still over-blocking, never under-blocking.
    """
    single = ("'", _QUOTE_SENTINEL_SQ)
    double = ('"', _QUOTE_SENTINEL_DQ)
    out: List[str] = []
    for line in cmd.split("\n"):
        if "#" not in line:
            out.append(line)
            continue
        if _bt_exceeds_tokenizable_ceiling(line):
            # DoS bound inherited from `_command_tokenizer` -- same
            # unchanged-line fail-closed branch an unterminated quote takes.
            out.append(line)
            continue
        in_sq = in_dq = False
        cut = None
        prev_ws = True
        for idx, ch in enumerate(line):
            if ch == "\\" and not in_sq:
                prev_ws = False
                continue
            if ch in single and not in_dq:
                in_sq = not in_sq
            elif ch in double and not in_sq:
                in_dq = not in_dq
            elif ch == "#" and not in_sq and not in_dq and prev_ws:
                cut = idx
                break
            prev_ws = ch.isspace()
        if in_sq or in_dq:
            out.append(line)
            continue
        out.append(line if cut is None else line[:cut].rstrip())
    return "\n".join(out)


def _strip_heredoc_bodies_for_prose_scan(cmd: str) -> str:
    """Variant of `_strip_heredoc_bodies` used ONLY by
    `check_destructive_git_orphan` (CHECK 2's force-push scan and CHECK 3's
    force-delete-branch scan, which share its "match on what the command
    DOES, never what free text SAYS" contract -- see
    `_seg_excluding_freetext_operands`). Does NOT change the unconditional
    `_strip_heredoc_bodies` behavior already relied on by
    check_destructive_rm / check_destructive_git_clean /
    check_destructive_git_revert.

    A heredoc body is prose (a commit message written via `git commit -F -
    <<'MSG'`, a file authored via `python3 - <<'PY'` that merely WRITES
    text, etc.) and gets stripped UNLESS `_classify_heredoc_intro` says its
    introducing line either (a) invokes a shell interpreter that will
    itself EXECUTE that body as further commands (``"shell"``/``"unknown"``
    -- an unrecognized command word fails closed the same way), or (b)
    invokes a scriptable interpreter (python/perl/ruby/node/php) AND the
    body itself contains a spawn indicator (``"scriptable"`` +
    `_heredoc_body_has_spawn_indicator` -- e.g. a `python3 - <<'PY'` body
    that calls `subprocess.run(['git', 'push', ..., '--force'])`). A
    recognized non-executing consumer (``"prose"`` -- `git`, `cat`, `tee`,
    etc.) is always stripped. Either way a KEPT heredoc's body is left
    UNSTRIPPED (visible to the caller's existing regex scan) whenever there
    is genuine reason to think it might execute a real destructive command.

    Kept body lines are additionally quote-PROTECTED (see
    `_protect_line_quotes`) before being returned: the caller
    (`check_destructive_git_orphan`) runs `_strip_ws_quoted_spans` right
    after this function, and that scanner deletes any whitespace-containing
    quoted span wholesale -- which would otherwise erase a Python/Node
    source-string literal like `'git push origin main --force'` before
    CHECK 2 ever sees it (this was an actual regression: probe case E, a
    `node <<'JS'` body calling `execSync('git push ... --force')`, ALLOWed
    because its one multi-word quoted arg was deleted). The caller restores
    real quotes via `_restore_protected_quotes` after `_strip_ws_quoted_spans`
    runs, so downstream segment-splitting / `_seg_excluding_freetext_operands`
    see ordinary quotes again.

    Fails closed exactly like `_strip_heredoc_bodies`: an unterminated
    heredoc returns `cmd` UNCHANGED (the pre-fix raw scan runs as before --
    still over-blocking, never under-blocking). This can only NARROW what
    CHECK 2/3 see (by removing genuine non-executing prose), never widen it.
    """
    lines = cmd.split("\n")
    out_lines: List[str] = []
    in_hd = False
    hd_word = ""
    hd_strip = False
    hd_class = "unknown"
    hd_interp: Optional[str] = None
    pending_body_lines: List[str] = []
    for line in lines:
        if not in_hd:
            m = _HEREDOC_INTRO_RE.search(line)
            if m:
                seg = m.group(0)
                hd_strip = len(seg) > 2 and seg[2] == "-"
                w = seg[3:] if hd_strip else seg[2:]
                if w and w[0] in ("'", '"'):
                    w = w[1:-1]
                hd_word = w
                in_hd = True
                hd_class = _classify_heredoc_intro(line[: m.start()], line)
                hd_interp = _heredoc_intro_command_word(line[: m.start()])
                pending_body_lines = []
            out_lines.append(line)
        else:
            check = line
            if hd_strip:
                check = re.sub(r"^\t+", "", check)
            if check == hd_word:
                in_hd = False
                if hd_class == "scriptable":
                    if _heredoc_body_has_spawn_indicator(pending_body_lines, hd_interp):
                        out_lines.extend(_protect_line_quotes(bl) for bl in pending_body_lines)
                        out_lines.append(line)
                    # else: prose -- buffered body lines AND terminator both
                    # suppressed, matching `_strip_heredoc_bodies`'s own
                    # terminator handling.
                elif hd_class == "prose":
                    pass  # buffered body lines AND terminator suppressed
                else:
                    # "shell" or "unknown" -- always visible; body lines
                    # were already appended immediately below (protected)
                    # as they were read, so only the terminator remains.
                    out_lines.append(line)
                pending_body_lines = []
            elif hd_class == "scriptable":
                # Buffer RAW (unprotected) -- decision (strip vs. keep) is
                # deferred until the terminator is found and the WHOLE body
                # can be scanned for a spawn indicator.
                pending_body_lines.append(line)
            elif hd_class == "prose":
                pass  # suppressed immediately -- decision doesn't depend on content
            else:
                # "shell" or "unknown" -- kept, protected immediately.
                out_lines.append(_protect_line_quotes(line))
    if in_hd:
        return cmd
    return "\n".join(out_lines)


#: BX-14 fix (2026-07-29, confirmed live via the real dispatcher): a
#: heredoc-fed shell (`bash <<'EOF' ... git commit --no-verify ... EOF`) was
#: never unwrapped by `check_no_verify` -- the function only flattens real
#: newlines to spaces (`flat`, below) and then segment-splits on `;`/`&`/`|`,
#: so the heredoc's own intro/terminator lines collapse into the SAME
#: segment as the surrounding `bash <<'EOF' ... EOF` text, with `bash` (not
#: `git`) sitting at command position -- the body text is invisible to this
#: guard even though it is executed for real once the terminator is read.
#: This is the mirror-image of `_shell_c_unwrap_payloads` (which already
#: unwraps `sh -c '...'`/`bash -c "..."` for this exact guard, added the
#: same day) for the heredoc-fed-shell shape instead of the `-c` shape --
#: reusing the EXISTING heredoc-classification machinery
#: (`_HEREDOC_INTRO_RE`, `_classify_heredoc_intro`,
#: `_heredoc_body_has_spawn_indicator`) that `_strip_heredoc_bodies_for_
#: prose_scan` already relies on, rather than inventing new heredoc parsing.
#: That function DROPS a non-executing heredoc body from a scan; this one is
#: the opposite direction -- it RETURNS an executing heredoc's body FOR the
#: caller to re-scan, exactly as `_shell_c_unwrap_payloads` returns a
#: `sh -c` payload for the caller to re-scan.
#:
#: Runs on `cmd` (real newlines intact), never on `flat` (newlines already
#: collapsed to spaces) -- the heredoc intro/terminator boundary detection
#: needs the actual line structure `flat` has already destroyed.
_MAX_HEREDOC_UNWRAP_DEPTH = 4


def _heredoc_shell_payloads(cmd: str, depth: int = 0) -> List[str]:
    """Return the body-text payload(s) of any heredoc in `cmd` whose
    introducing line will itself EXECUTE that body as further shell/script
    commands: a `"shell"`/`"unknown"`-classified intro (`bash <<'EOF' ...`,
    fails closed identically to `_classify_heredoc_intro`'s own posture), or
    a `"scriptable"`-classified intro (`python3 - <<'PY' ...`) whose body
    contains a spawn indicator (`_heredoc_body_has_spawn_indicator` --
    `subprocess.run([...])` etc.). A `"prose"` heredoc (`git commit -F -
    <<'MSG'`, `cat > file <<'EOF'`) or a spawn-indicator-free scriptable one
    yields nothing -- that body is data, not executed shell text.

    Fails safe: an unterminated heredoc, or nesting past
    `_MAX_HEREDOC_UNWRAP_DEPTH`, yields `[]` (nothing extra to scan, never a
    crash, never a false deny) -- the caller's own scan of the introducing
    line is unaffected either way, and this can only ADD payloads to scan,
    never remove visibility the caller already had.
    """
    if depth > _MAX_HEREDOC_UNWRAP_DEPTH:
        return []
    lines = cmd.split("\n")
    payloads: List[str] = []
    in_hd = False
    hd_word = ""
    hd_strip = False
    hd_class = "unknown"
    hd_interp: Optional[str] = None
    pending_body_lines: List[str] = []
    for line in lines:
        if not in_hd:
            m = _HEREDOC_INTRO_RE.search(line)
            if m:
                seg = m.group(0)
                hd_strip = len(seg) > 2 and seg[2] == "-"
                w = seg[3:] if hd_strip else seg[2:]
                if w and w[0] in ("'", '"'):
                    w = w[1:-1]
                hd_word = w
                in_hd = True
                hd_class = _classify_heredoc_intro(line[: m.start()], line)
                hd_interp = _heredoc_intro_command_word(line[: m.start()])
                pending_body_lines = []
        else:
            check = line
            if hd_strip:
                check = re.sub(r"^\t+", "", check)
            if check == hd_word:
                in_hd = False
                if hd_class in ("shell", "unknown"):
                    payloads.append("\n".join(pending_body_lines))
                elif hd_class == "scriptable" and _heredoc_body_has_spawn_indicator(
                    pending_body_lines, hd_interp
                ):
                    payloads.append("\n".join(pending_body_lines))
                pending_body_lines = []
            else:
                pending_body_lines.append(line)
    if in_hd:
        return []
    nested: List[str] = []
    for payload in payloads:
        nested.extend(_heredoc_shell_payloads(payload, depth + 1))
    return payloads + nested


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _advisory(msg: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": msg,
        }
    }


def _allow_rewrite(new_cmd: str, ctx: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": new_cmd},
        }
    }
    if ctx:
        out["hookSpecificOutput"]["additionalContext"] = ctx
    return out


#: Wall-clock a single dispatch may spend inside `_run_git` subprocesses
#: before further probes are declined unspawned. Derived from the harness's
#: own PreToolUse hook window (15 000 ms, read off a `hook_cancelled`
#: attachment in a live transcript -- the harness owns that number, this
#: repo does not), minus the rest of one invocation: interpreter boot, the
#: `coordinator_core` import, and every non-probing guard's in-process work.
#: Measured idle end-to-end for the whole hook, real payload, warm index:
#: ~0.55 s, of which ~0.95 s of git time is the tail under contention -- so
#: this budget is not felt on an unloaded box and only binds when the machine
#: is at its documented 50-70-concurrent-LLM norm (`docs/wiki/machine-load-
#: norm.md`).
#:
#: WHY A CEILING AT ALL. Per-probe `timeout=2.0` bounds ONE spawn; nothing
#: bounded their SUM. A commit-shaped command spawns six git processes on
#: this path, so the engine's own worst case (12 s of git, plus boot and
#: import) sat ABOVE the harness window -- and a hook that overruns its
#: window is cancelled outright, which loses every guard's verdict, not just
#: the slow one's. That is the delivery failure recorded in
#: `state/bug-backlog/2026-08-15-bare-commit-deny-never-reached-the-
#: operator.yaml`: no deny, no advisory, nothing printed at all.
_GIT_PROBE_BUDGET_SECONDS = 6.0

#: Deadline (a `time.monotonic()` stamp) for the dispatch currently in
#: flight, or `None` when no budget is armed. INERT BY DEFAULT and armed at
#: exactly one seam (`dispatch.evaluate_payload_json`), which is the only
#: caller running inside the harness's cancellable window: a check invoked
#: directly -- every test in this package, `_alternative_liveness`'s own
#: liveness harness -- sees `None` and behaves byte-identically to before
#: this budget existed. Do not arm it from a check; a check does not know
#: whether it is one guard in a chain or the whole call.
#:
#: `contextvars.ContextVar` rather than a bare module global (C8,
#: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md):
#: under a warm engine two dispatches can be interleaved, and a bare global
#: lets a second dispatch's `_arm_git_probe_deadline` silently resurrect a
#: first dispatch's already-exhausted budget (or vice versa) -- the
#: characterization test this fix flips,
#: `coordinator_core/warm/tests/test_process_global_characterization.py`
#: Site 6. Each dispatch runs in its own Task/thread Context, so arm/disarm
#: in one dispatch is invisible to a concurrently-interleaved one.
_git_probe_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "_dispatch_checks_git_probe_deadline", default=None
)

#: Returned instead of spawning once the budget is spent. Deliberately 127
#: (`_run_git`'s existing unresolvable-`git` code) and NOT -1: -1 is the
#: timeout sentinel four oracle branches in this file treat as fail-CLOSED
#: (`check_destructive_git_clean`, and the revert/reset/stash oracles), and
#: a budget-exhausted probe is a guard-process resource condition, not
#: evidence the command is unsafe -- denying on it would block real work on
#: a loaded machine, the same argument `_bt_c7_index_holds_foreign_paths`
#: already makes for its own fail-open posture. Every `rc != 0` call site in
#: this file therefore degrades to its own already-specified fail-open
#: default, which is strictly better than what the overrun produced: a
#: cancelled hook delivers NO guard's verdict, including the many that need
#: no probe at all.
_GIT_PROBE_BUDGET_SPENT_RC = 127


def _arm_git_probe_deadline(budget: Optional[float] = None) -> None:
    """Open a probe budget for one dispatch. Idempotent per dispatch: a
    second arm replaces the deadline rather than extending it.

    `None` reads `_GIT_PROBE_BUDGET_SECONDS` AT CALL TIME rather than
    binding it as a default argument at import: a default would freeze the
    module constant into this signature, making it unreachable to the one
    thing that needs to vary it -- a test shrinking the budget so it can
    exercise exhaustion in milliseconds instead of seconds."""
    if budget is None:
        budget = _GIT_PROBE_BUDGET_SECONDS
    _git_probe_deadline.set(time.monotonic() + budget)


def _disarm_git_probe_deadline() -> None:
    """Close the current dispatch's probe budget, restoring the unbudgeted
    default. Callers arm/disarm in a `try`/`finally` so an exception on the
    guard chain cannot leave a stale deadline armed for the next call in a
    long-lived process (a test run; anything embedding this engine)."""
    _git_probe_deadline.set(None)


def _git_probe_budget_spent() -> bool:
    """True iff a budget is armed AND already spent. `None` (unarmed) is
    never "spent" -- see `_git_probe_deadline`'s inert-by-default note."""
    deadline = _git_probe_deadline.get()
    return deadline is not None and time.monotonic() >= deadline


def _run_git(args: List[str], cwd: Optional[str] = None, timeout: float = 2.0,
             extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """Run `git <args>`, optionally `-C <cwd>`-scoped via subprocess `cwd=`.
    Returns (returncode, stdout). rc == -1 signals a timeout (mirrors bash's
    `rc=124` convention used by the oracle-timeout deny branches).

    Declines to spawn at all, returning `_GIT_PROBE_BUDGET_SPENT_RC`, once
    the dispatch's probe budget is spent (see `_git_probe_deadline`). The
    per-call `timeout` is deliberately NOT clamped to the remaining budget:
    a clamped timeout would push borderline probes onto the rc == -1 path,
    which four oracles in this file treat as fail-CLOSED, manufacturing
    denies out of machine load. Overshoot is therefore bounded by one call's
    own `timeout`, not by the budget."""
    if _git_probe_budget_spent():
        print(
            "bash_guards.dispatch_checks: git probe budget (%.1fs) spent; "
            "declining `git %s` unspawned -- this dispatch's remaining "
            "probes fall back to their own fail-open defaults."
            % (_GIT_PROBE_BUDGET_SECONDS, " ".join(args[:3])),
            file=sys.stderr,
        )
        return _GIT_PROBE_BUDGET_SPENT_RC, ""
    env = None
    if extra_env:
        env = dict(os.environ)
        env.update(extra_env)
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=timeout,
            env=env,
            creationflags=_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        return -1, ""
    except OSError:
        return 127, ""
    return result.returncode, result.stdout


def _batch_show_index_blobs(paths: List[str], cwd: Optional[str]) -> Dict[str, Optional[str]]:
    """Batched replacement for a per-file `git show :<path>` index-blob read
    (used by `check_validate_commit`'s CLAUDE.md-budget check). ONE
    `git cat-file --batch` feed (stdin, byte mode) resolves every path's
    staged content instead of one `git show` spawn per file -- the same
    amplification shape `cat_file_batch`/`cat_file_batch_objects`
    (`coordinator_core.ops.ceremony.git_native`) close elsewhere, reimplemented
    locally because THIS module's `_run_git` runs `subprocess.run(...,
    encoding="utf-8", errors="replace")` -- text mode -- while `cat-file
    --batch`'s per-record `size` field is a BYTE count; decoding first would
    desync every slice after the first multi-byte character or CRLF. Also
    honors this module's git-probe budget (`_git_probe_budget_spent`), which
    the ceremony helper has no reason to know about.

    Reconciliation: every requested path is bound to an explicit slot by
    walking `paths` in order -- resolved -> blob text (utf-8, errors=
    "replace", matching `_run_git`'s own decode policy), missing/truncated/
    malformed -> None. Absence from git's output is never read as "resolved".

    Returns `{}` for empty `paths`, spawning no subprocess.
    """
    if not paths:
        return {}
    if _git_probe_budget_spent():
        print(
            "bash_guards.dispatch_checks: git probe budget (%.1fs) spent; "
            "declining `git cat-file --batch` unspawned -- claudemd-budget "
            "reads for this dispatch fall back to skipped (fail-open)."
            % _GIT_PROBE_BUDGET_SECONDS,
            file=sys.stderr,
        )
        return {p: None for p in paths}
    stdin_bytes = ("\n".join(":%s" % p for p in paths) + "\n").encode("utf-8")
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch"],
            input=stdin_bytes,
            capture_output=True,
            cwd=cwd,
            timeout=2.0,
            creationflags=_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        return {p: None for p in paths}
    except OSError:
        return {p: None for p in paths}
    if proc.returncode != 0:
        # A non-zero exit can follow partial stdout writes; treating that
        # stdout as trustworthy would let the byte-offset parser slice a
        # short/garbled record and hand back a WRONG (truncated) blob
        # instead of a missing one. Fail the whole batch closed, same as
        # TimeoutExpired/OSError above -- an indeterminate read must never
        # present as a successfully-read blob on this commit-hot-path guard.
        return {p: None for p in paths}
    stdout = proc.stdout
    results: Dict[str, Optional[str]] = {}
    pos = 0
    for p in paths:
        nl = stdout.find(b"\n", pos)
        if nl == -1:
            # stdout ran out relative to the requested set -- every
            # remaining path is unresolved; never guess at a partial record.
            results[p] = None
            continue
        header = stdout[pos:nl]
        pos = nl + 1
        if header.endswith(b" missing"):
            results[p] = None
            continue
        parts = header.split(b" ")
        if len(parts) != 3:
            results[p] = None
            continue
        _sha, _type, size_field = parts
        try:
            size = int(size_field)
        except ValueError:
            results[p] = None
            continue
        content = stdout[pos:pos + size]
        pos += size
        if stdout[pos:pos + 1] == b"\n":
            pos += 1
        results[p] = content.decode("utf-8", errors="replace")
    return results


def _override(name: str) -> bool:
    """Inline-per-call env read -- NEVER hoist to module scope (F2, recipe
    Sec(e) "module-level namespace collisions"). Every call site in this file
    calls `_override("COORDINATOR_ALLOW_X")` fresh, matching bash `${VAR:-0}`
    at each individual guard's own call site."""
    return os.environ.get(name, "0") == "1"


# ---------------------------------------------------------------------------
# 1. check_no_verify -- block-no-verify.sh
# ---------------------------------------------------------------------------

#: BX-13 fix (2026-07-29, confirmed live via the real dispatcher, not a
#: theoretical hole): `git` used to have to sit DIRECTLY after the segment
#: boundary (`^|&&|;|\|`) with nothing but whitespace in between, so a
#: leading environment-variable assignment, an `env` prefix, or a passthrough
#: wrapper (`nice`/`time`/`command`/`exec`/`nohup`/`ionice`/`timeout`/
#: `stdbuf`/`which`/`type`) -- `FOO=1 git commit --no-verify -m x`,
#: `nice git commit --no-verify -m x` -- was never recognized as a git
#: invocation at all and sailed through this guard while still executing
#: `--no-verify` for real. This tolerates the same wrapper-prefix shape this
#: file's own `_GC_CLEAN_CMD_RE` (destructive-git-clean) already tolerates
#: ahead of its own `git` anchor, reused here rather than re-derived.
#: BX-14 fix (2026-07-29, confirmed live via the real dispatcher): the walk
#: above tolerated the WRAPPER BINARY token itself but never the wrapper's
#: OWN argument(s) -- `timeout 30 git commit --no-verify`, `ionice -c2 git
#: commit --no-verify`, `stdbuf -oL git commit --no-verify` all landed on
#: `30`/`-c2`/`-oL` as the "next token", which is neither a wrapper word nor
#: `git`, so the walk broke there and the segment fell through to the raw
#: `_BYPASS_RE` scan below -- which has the identical gap (a bare
#: wrapper-word-then-space is required immediately before `git`, with no
#: allowance for an intervening argument token). `_WRAPPER_FLAG_GROUP` below
#: tolerates zero or more of the wrapper's own `-flag` tokens, both the
#: attached-value form (`-c2`/`-oL`, a single token) and the SEPARATE-token
#: form (`-c 2`) via a negative lookahead (`(?!git\b)`) on the optional value
#: slot -- a bare flag can never swallow the literal `git` token that must
#: follow as its "value"; the regex engine backtracks off the optional value
#: group rather than consuming past the command boundary.
#: Catastrophic-backtracking fix (2026-08-05, dispatch-latency-bound corpus):
#: `_WRAPPER_FLAG_GROUP`'s star and `_BYPASS_PREFIX`'s outer star (below) both
#: sit ABOVE an alternation whose branches can match overlapping text -- the
#: `env\s+(?:\S+=\S*\s+)*` branch and the bare-assignment branch
#: (`[A-Za-z_][A-Za-z0-9_]*=\S*\s+`) both recognize the same
#: assignment-shaped token, so a long run of `FOO=1 BAR=2 ...` with no
#: trailing `git` gives the engine exponentially many ways to partition that
#: run across outer-star iterations before it can conclude no match exists --
#: the textbook nested-quantifier ReDoS shape. Wrapped in `(?>...)` (atomic
#: group, Python 3.11+ -- this package's floor, see repo CLAUDE.md) rather
#: than rewritten to remove the overlap: atomic grouping commits to the
#: first successful partition and never reconsiders it, which is safe here
#: specifically BECAUSE every alternative below ends at a whitespace
#: boundary and NONE of them can match a literal `git` token -- there is no
#: legitimate input for which some OTHER partition of the prefix would let
#: the trailing `git` match succeed where the first-found partition's
#: trailing `git` match fails. Losing the ability to backtrack into the
#: prefix therefore cannot lose a match; it only forecloses the exponential
#: re-exploration of partitions that were never going to matter.
_WRAPPER_FLAG_GROUP = r"(?>(?:-\S+(?:\s+(?!git\b)\S+)?\s+)*)"
_BYPASS_PREFIX = (
    # BRACE-GROUPING FIX (2026-07-29, EM-run second-pass integration,
    # confirmed live): `{ ` is bash's brace-list opener (a reserved word
    # requiring a following space, not an operator like `(`), so
    # `{ git add -A; }` / `{ git commit --no-verify; }` reach this prefix
    # with a leading `{ ` this group did not previously tolerate -- any
    # caller anchoring on `_BYPASS_PREFIX` (this regex fallback and
    # `check_blanket_git_add`'s matcher) silently missed the brace-wrapped
    # form. Added as one more zero-or-more alternative, same as every other
    # wrapper here.
    # PAREN-GROUPING FIX (2026-07-29, EM-run confinement-corpus pass,
    # confirmed live): `( git add -A )` / `( git commit --no-verify )` were
    # never tolerated either -- `(` is whitespace-separated in the tested
    # shape and, like `{`, the shared tokenizer (`shlex.shlex(...,
    # punctuation_chars=";&|")`, `_command_tokenizer.py`) does not treat it
    # specially, so it always falls out as its own token via ordinary
    # whitespace-splitting. Same bypass class, same fix shape as the brace
    # alternative directly above -- this regex fallback and
    # `check_blanket_git_add`'s matcher both anchor on `_BYPASS_PREFIX`.
    r"(?>(?:\{\s+|"
    r"\(\s+|"
    r"(?:sudo|command|time|exec|nohup|which|type)\s+|"
    r"nice\s+" + _WRAPPER_FLAG_GROUP + r"|"
    r"timeout\s+" + _WRAPPER_FLAG_GROUP + r"\d+(?:\.\d+)?[smhd]?\s+|"
    r"ionice\s+" + _WRAPPER_FLAG_GROUP + r"|"
    r"stdbuf\s+" + _WRAPPER_FLAG_GROUP + r"|"
    r"env\s+(?>(?:\S+=\S*\s+)*)|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)"
)
#: Catastrophic-backtracking fix, second cause (2026-08-05, same corpus):
#: the previous single-regex `_BYPASS_RE` put an unbounded `.*` between
#: `git\s+` and the literal flag alternation -- `git\s+(.*\s)?(flag-alts)`
#: -- so on a segment with a `git` head but no bypass flag, the engine tries
#: every possible split of `.*` before concluding no match, and `re.search`
#: repeats that whole O(n) probe at every segment-boundary candidate
#: (`^`/`&&`/`||`/`;`/`|`) in the text, compounding to superlinear cost.
#: Restructured into two BOUNDED regexes run in sequence rather than one
#: combined pattern: `_BYPASS_HEAD_RE` locates the leftmost git-invocation
#: head (anchor + prefix + `git\s+`, no `.*`, atomic prefix as above --
#: linear), then `_BYPASS_MARKER_RE` -- itself free of unbounded
#: quantifiers, just three literal alternatives each with bounded `\s+`/
#: `\s*` -- searches ONLY the text from that head's end onward for a
#: marker preceded by whitespace. Checking the LEFTMOST head only is
#: sufficient, not merely convenient: for any later head H2, the region
#: after H2 is a subset of the region after the leftmost head H1 (H2 occurs
#: later in the string), so "a marker exists after H1" is implied by "a
#: marker exists after H2" -- if the marker search from H1 fails, it would
#: fail from every later head too, and if it succeeds, the leftmost head
#: alone already witnesses the match `_BYPASS_RE.search` is asking for.
#: `_BypassRe.search` below reproduces the original combined pattern's
#: match-or-None contract so every existing call site (`bool(_BYPASS_RE.
#: search(...))`) is unchanged.
#:
#: Semantics preserved exactly (this is a deliberately OVER-inclusive
#: fail-closed fallback, reached only when the command could not be
#: tokenized -- over-matching is the safe direction here, under-matching is
#: not): a match still means "a git invocation head exists at a segment
#: boundary, and one of the three bypass markers appears at or after that
#: head, preceded by whitespace". The literal `in`-check pre-filter is a
#: speed short-circuit for the (overwhelmingly common) case where none of
#: the three markers appear anywhere in the text at all -- it is NOT relied
#: on for correctness when a marker IS present (an embedded `--no-verify`
#: still gets the full bounded head+marker scan, which stays linear
#: regardless of payload size).
_BYPASS_HEAD_RE = re.compile(r"(^|&&|\|\||;|\|)\s*" + _BYPASS_PREFIX + r"git\s+")
_BYPASS_MARKER_RE = re.compile(
    r"(?:^|\s)(?:-c\s+commit\.gpgsign\s*=\s*false|--no-verify|--no-gpg-sign)"
)
_BYPASS_MARKER_LITERALS = ("--no-verify", "--no-gpg-sign", "commit.gpgsign")


class _BypassRe:
    """Drop-in replacement for the former single compiled `_BYPASS_RE`
    pattern -- exposes the same `.search(text) -> Optional[Match]` surface
    every call site already uses via `bool(_BYPASS_RE.search(...))`, backed
    by the two-regex bounded scan documented above instead of one
    catastrophically-backtracking pattern.
    """

    @staticmethod
    def search(text: str) -> Optional[re.Match[str]]:
        if not any(marker in text for marker in _BYPASS_MARKER_LITERALS):
            return None
        head = _BYPASS_HEAD_RE.search(text)
        if head is None:
            return None
        # `head` ends with the trailing `\s+` of `git\s+`, so `head.end() -
        # 1` is guaranteed to be one of those whitespace characters -- back
        # the marker scan up by one so a marker sitting IMMEDIATELY after
        # `git ` (no additional separator text) still satisfies `_BYPASS_
        # MARKER_RE`'s own `(?:^|\s)` boundary. `re.search`'s `^` anchors to
        # absolute string position 0 only, never to a `pos=` start offset,
        # so starting exactly at `head.end()` would silently require a
        # SECOND whitespace character before the marker that the original
        # combined pattern never required.
        return _BYPASS_MARKER_RE.search(text, head.end() - 1)


_BYPASS_RE = _BypassRe()

#: Quote-blindness fix (2026-07-29, this-package sixth instance): `_BYPASS_RE`
#: above scans RAW segment TEXT, with no regard for whether the matched flag
#: string sits in ARGUMENT position or inside a quoted operand (a `-m`/`-F`
#: commit-message body, a heredoc, a filename). Reproduced live: a commit
#: whose MESSAGE PROSE merely *describes* `--no-verify`/`--no-gpg-sign`/`-c
#: commit.gpgsign=false` (e.g. this very fix's own commit message) tripped
#: the guard exactly as hard as a commit that actually PASSES one of those
#: flags. `_seg_has_git_bypass_flag` below replaces the raw-text scan with a
#: tokenize-first, then-inspect-argv-position check: it shlex-splits `seg`
#: ONCE and drops the -m/--message operand TOKEN (same free-text-operand
#: flags CHECK 2's `_seg_excluding_freetext_operands` already excludes for
#: the identical prose-vs-argv problem) directly from the token LIST --
#: deliberately NOT by reusing that helper's string-rejoin-then-re-split
#: round trip, which would silently drop the quoting around any OTHER
#: operand still in the segment (a `-c "commit.gpgsign = false"` operand's
#: internal space would re-split into three tokens on the second parse,
#: turning a genuine single-token bypass into three that no longer match).
#: Only an actual `--no-verify`/`--no-gpg-sign` token, or an actual `-c`
#: token followed by a `commit.gpgsign=false` operand, counts.
_BYPASS_WRAPPER_WORDS = frozenset({
    "sudo", "command", "time", "exec", "nice", "nohup",
    "ionice", "timeout", "stdbuf",
})
_BYPASS_FLAG_TOKENS = frozenset({"--no-verify", "--no-gpg-sign"})
_BYPASS_GPGSIGN_FALSE_RE = re.compile(r"^commit\.gpgsign\s*=\s*false$")

#: DELIBERATE VERDICT CHANGE (M5P Piece 1, 2026-07-29, named on purpose --
#: this is not a plumbing change): `which`/`type` used to sit inside
#: `_BYPASS_WRAPPER_WORDS` above, so `_seg_has_git_bypass_flag`'s tokenized
#: walk peeled through them exactly like an execs-its-argv wrapper --
#: `which git commit --no-verify` was DENIED as if it had actually run
#: `git commit --no-verify`. It never does: `which`/`type` REPORT on a
#: named command without ever executing it (`_command_tokenizer.
#: WrapperSemanticClass.INSPECTS_WITHOUT_EXECING` already classifies both
#: this way, correctly -- this file previously disagreed with its own
#: package's shared classification). A segment whose command-position head
#: positively resolves to one of these two words can never be a real
#: `--no-verify`/`--no-gpg-sign`/`-c commit.gpgsign=false` bypass, no matter
#: what its remaining argv looks like, so `_seg_has_git_bypass_flag` now
#: returns a confirmed `False` for it directly rather than falling through
#: to the ambiguous-case raw-text scan (see that function's own use of this
#: set, below). Scope is `check_no_verify` only -- the shared `_BYPASS_
#: PREFIX`/`_BYPASS_RE` regex (also relied on by `check_blanket_git_add`)
#: is deliberately UNCHANGED, so this narrowing does not reach that guard.
_INSPECTS_WITHOUT_EXECING_WORDS = frozenset({"which", "type"})

#: BX-14 (2026-07-29): wrapper binaries that take their OWN argument(s)
#: before the wrapped command -- `timeout 30 <cmd>`, `nice -n 10 <cmd>`,
#: `ionice -c 2 <cmd>` / `ionice -c2 <cmd>`, `stdbuf -oL <cmd>`. Both the
#: separate-token (`-c 2`) and attached (`-c2`) flag-value forms are
#: consumed here -- this token-list walk can distinguish them safely,
#: unlike the regex fallback (`_BYPASS_PREFIX`) above, which only tolerates
#: the attached form without risking swallowing the real command token.
#: `_skip_wrapper_own_argv` itself (walking `base`'s own flag/argument
#: tokens so `_seg_has_git_bypass_flag`'s walk lands on the NEXT wrapper or
#: the real command) now lives in `_command_tokenizer.py` (2026-07-30, M8
#: consolidation) -- imported above rather than hand-maintained here. That
#: shared copy is a strict superset of this file's former one (it also
#: recognizes `nice`'s bare-numeric niceness operand, which this file's copy
#: never did), so consolidating can only widen wrapper-argv recognition
#: here, never narrow it -- a segment this file's narrower walk could not
#: fully peel already fell back to the raw `_BYPASS_RE` scan, which still
#: over-blocks rather than under-blocking.


def _seg_has_git_bypass_flag(seg: str, pretokenized: Optional[List[str]] = None) -> bool:
    """Positionally scan `seg`'s ACTUAL git argv tokens (after dropping the
    free-text operand TOKEN of -m/--message, done on the token list itself
    -- see module comment above for why NOT via `_seg_excluding_freetext_
    operands`'s string round trip) for a genuine --no-verify /
    --no-gpg-sign / `-c commit.gpgsign=false` bypass flag.

    Fails CLOSED on anything it cannot positively tokenize: unparseable argv
    (shlex ValueError) or a segment with no confirmed command-position `git`
    after skipping wrapper/env/assignment prefix tokens both fall back to
    the pre-existing raw-text `_BYPASS_RE` scan on THIS segment -- ambiguity
    still over-blocks, it never under-blocks. Only a segment that
    POSITIVELY resolves to a git invocation with NO bypass token in its own
    argv is allowed to clear this check.

    `pretokenized`, when given, is this segment's own already-shlex-split
    token list (quote-aware segment boundaries already resolved by the
    caller -- see `check_no_verify`'s Finding-1 fix comment). Passing it
    skips the redundant re-`shlex.split(seg, ...)` below entirely, since a
    segment carved by the caller's quote-aware tokenizer can never itself
    raise `ValueError` here the way a naive re-split-then-parse segment
    could. `seg` (the segment's flat text, reconstructed by the caller for
    this purpose only) is still used for the two `_BYPASS_RE` raw-scan
    fallback branches below, which need literal text, not a token list.
    """
    if pretokenized is not None:
        raw_tokens = pretokenized
    elif _bt_exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- the over-inclusive raw `_BYPASS_RE` scan an unparseable
        # segment already falls back to (linear, so safe at any length).
        return bool(_BYPASS_RE.search(seg))
    else:
        try:
            raw_tokens = shlex.split(seg, posix=True)
        except ValueError:
            return bool(_BYPASS_RE.search(seg))

    tokens: List[str] = []
    skip_next = False
    for tok in raw_tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _FREETEXT_OPERAND_FLAGS:
            skip_next = True
            continue
        if any(tok.startswith(flag + "=") for flag in _FREETEXT_OPERAND_FLAGS):
            continue
        tokens.append(tok)

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # BRACE-GROUPING FIX (2026-07-29, EM-run second-pass integration,
        # confirmed live): `{ git commit --no-verify -m x; }` was never
        # peeled here -- the leading `{` token left the "first effective
        # token" as `{` itself, never `git`, so the walk below fell through
        # to `_BYPASS_RE.search(seg)`, whose prefix pattern also has no `{`
        # tolerance -- a real bypass silently allowed. Bash requires a space
        # after `{` (a reserved word, not an operator like `(`), so
        # `shlex.split`/the shared tokenizer always yields it as its own
        # token. Mirrors `block_subagent_commit.py`'s
        # `_first_effective_token` brace-peel fix (same day, same class).
        # PAREN-GROUPING FIX (2026-07-29, EM-run confinement-corpus pass,
        # confirmed live): `( git commit --no-verify -m x )` has the exact
        # same shape -- `(`, whitespace-separated, falls out as its own
        # token from the shared tokenizer for the same reason `{` does, and
        # was never peeled here either. Same fix, same reasoning.
        if tok in ("{", "("):
            i += 1
            continue
        base = _normalize_executable_basename(tok)
        if base in _BYPASS_WRAPPER_WORDS:
            i += 1
            i = _skip_wrapper_own_argv(tokens, i, base)
            continue
        if base == "env":
            i += 1
            while i < n and (
                tokens[i] in ("-i", "--ignore-environment")
                or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
            ):
                i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$", tok):
            i += 1
            continue
        break
    if i < n and _normalize_executable_basename(tokens[i]) in _INSPECTS_WITHOUT_EXECING_WORDS:
        # M5P Piece 1: a positively-resolved `which`/`type` head never
        # executes anything past it -- this is a CONFIRMED False, not an
        # ambiguous case, so it must not fall through to the raw-text scan
        # immediately below (that regex's shared `_BYPASS_PREFIX` still
        # tolerates `which`/`type`, unchanged, for `check_blanket_git_add`'s
        # sake -- see `_INSPECTS_WITHOUT_EXECING_WORDS`'s own docstring).
        return False
    if i >= n or _normalize_executable_basename(tokens[i]) != "git":
        # No confirmed command-position `git` in this segment's own argv --
        # fall back to the raw scan rather than guessing this is prose.
        return bool(_BYPASS_RE.search(seg))

    i += 1
    while i < n:
        tok = tokens[i]
        if tok in _BYPASS_FLAG_TOKENS:
            return True
        if tok == "-c" and i + 1 < n and _BYPASS_GPGSIGN_FALSE_RE.match(tokens[i + 1]):
            return True
        i += 1
    return False


def check_no_verify(
    cmd: str,
    session_id: str = "",
    *,
    resolved: Optional[List[Any]] = None,
    hook_payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Deny a git invocation carrying a hook-bypass flag.

    ``resolved`` (M5, 2026-07-30) is the dispatcher's shared
    ``_command_tokenizer.resolve_command_positions(cmd)`` result -- one
    ``ResolvedCommand`` per top-level segment of ``cmd`` -- threaded in so
    this guard can consume that one shared segmentation walk instead of
    running its own `_bt_tokenize_full_command` + `_bt_segments_from_
    tokens_simple` pass (AC-8). Consumes each segment's ``raw_tokens``
    (the tokens BEFORE the resolver's own wide `EXECS_ITS_ARGV_WRAPPERS`
    peel) rather than its peeled ``tokens`` -- `_seg_has_git_bypass_flag`
    still applies THIS module's own narrower `_BYPASS_WRAPPER_WORDS`
    vocabulary as a post-filter, so this migration cannot widen (or
    narrow) what the guard matches; it only removes the redundant
    tokenize. `resolved is None` (every caller predating this parameter)
    takes the original self-contained path below, unchanged.
    """
    if _override("COORDINATOR_OVERRIDE_NO_VERIFY"):
        return None
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    flat = re.sub(r"\\n", " ", cmd.replace("\r", "").replace("\n", " "))

    _nv_note = operator_override_note(
        "COORDINATOR_OVERRIDE_NO_VERIFY", payload=hook_payload, git_root=git_root
    )

    def _nv_bypass_deny() -> Dict[str, Any]:
        base = (
            "BLOCKED: git bypass flag detected. The coordinator doctrine "
            "prohibits --no-verify, --no-gpg-sign, and -c commit.gpgsign=false."
        )
        if _nv_note:
            base += " If the PM has explicitly authorized bypassing hooks: %s" % _nv_note
        return _deny(base)

    if re.search(r"\bgit\b", flat):
        if resolved:
            # Consume the shared resolver's segmentation via `raw_tokens`
            # instead of re-tokenizing `flat` from scratch (see docstring).
            # A segment whose `raw_tokens` is empty (the resolver's
            # unparseable/depth-cap fallback shape) falls through to this
            # function's own self-contained tokenize path below rather than
            # being silently skipped -- `used_resolved` stays False for that
            # segment set and the whole `resolved` list is abandoned in
            # favor of the always-safe fallback.
            used_resolved = True
            for rc in resolved:
                seg_tokens = list(getattr(rc, "raw_tokens", None) or [])
                if not seg_tokens:
                    used_resolved = False
                    break
                seg_text = " ".join(seg_tokens)
                if _seg_has_git_bypass_flag(seg_text, pretokenized=seg_tokens):
                    return _nv_bypass_deny()
            if used_resolved:
                return _no_verify_rescan_shell_c_and_heredoc(cmd, flat, session_id, hook_payload, git_root)

        # Finding 1 fix (2026-07-29, this-package seventh instance of the
        # split-before-tokenize class): `_split_segments` below is a raw
        # `re.split(r"[;&|]+", cmd)` -- QUOTE-UNAWARE. A quoted `;`/`&`/`|`
        # inside a git commit-message operand (`git commit -m "release;
        # ship it" --no-verify`) would get torn in half by that split, with
        # "git" landing in one fragment and the real `--no-verify` flag
        # landing in the other -- neither fragment alone carries both, so
        # the bypass silently sailed through undetected. Segmenting via the
        # SAME quote-aware tokenizer already shared by every other guard in
        # this package (`_bt_tokenize_full_command` + `_bt_segments_from_
        # tokens_simple`, `_command_tokenizer.py`) instead of a second,
        # bespoke, quote-blind splitter closes this: an unquoted `;`/`&`/`|`
        # still yields a clean segment boundary, but one sitting inside a
        # quoted operand no longer does, so one indivisible git invocation
        # can never be torn into two innocent-looking halves.
        bt_tokens = _bt_tokenize_full_command(flat)
        if bt_tokens is None:
            # Unparseable (unterminated quote / trailing backslash) -- fail
            # CLOSED via the original whole-string raw scan, over-inclusive
            # by construction (never under-blocks a genuine bypass, may
            # over-block ambiguous prose -- the safe direction for an
            # unparseable command).
            if _BYPASS_RE.search(flat):
                return _nv_bypass_deny()
        else:
            for seg_tokens in _bt_segments_from_tokens_simple(bt_tokens):
                if not seg_tokens:
                    continue
                seg_text = " ".join(seg_tokens)
                if _seg_has_git_bypass_flag(seg_text, pretokenized=seg_tokens):
                    return _nv_bypass_deny()

    return _no_verify_rescan_shell_c_and_heredoc(cmd, flat, session_id, hook_payload, git_root)


def _no_verify_rescan_shell_c_and_heredoc(
    cmd: str,
    flat: str,
    session_id: str,
    hook_payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-13/BX-14 rescan legs, shared by every `check_no_verify` return path
    above (the `resolved`-consuming early return included) so migrating the
    segment walk onto `raw_tokens` can never accidentally drop either rescan.
    The recursive `check_no_verify` calls here deliberately do NOT thread
    `resolved` back in: an unwrapped `sh -c` payload or heredoc body is not a
    segment of the outer `cmd` the caller's `resolved` was computed against,
    so re-running the full self-contained (`resolved=None`) path against
    each unwrapped payload is correct, not a missed optimization. They DO
    thread `hook_payload`/`git_root` back in (2026-08-13, audience-gated
    `operator_override_note` migration) -- the unwrapped text is a different
    COMMAND, not a different CALLER, so the hook envelope this rescan is
    evaluating on behalf of is unchanged.
    """
    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too.
    for payload in _shell_c_unwrap_payloads(flat):
        result = check_no_verify(payload, session_id, hook_payload=hook_payload, git_root=git_root)
        if result is not None:
            return result

    # BX-14: a heredoc fed to a shell interpreter (`bash <<'EOF' ... EOF`) is
    # executed, not inert text -- unwrap and re-scan it too. Runs on `cmd`
    # (real newlines), never `flat` -- see `_heredoc_shell_payloads` docstring.
    for payload in _heredoc_shell_payloads(cmd):
        result = check_no_verify(payload, session_id, hook_payload=hook_payload, git_root=git_root)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# 2. check_destructive_git_orphan -- block-destructive-git-orphan.sh
# F0: PER-SEGMENT `git -C <dir>` resolution -- never a single dispatcher-level
# git root. See module docstring / recipe Sec(e).
# ---------------------------------------------------------------------------

def _orphan_override_hint(payload: Optional[Dict[str, Any]], git_root: Optional[str]) -> str:
    """Per-call replacement for the former module-level ``_ORPHAN_OVERRIDE_HINT``
    constant (2026-08-13, audience-gated ``operator_override_note`` migration;
    tasks/guard-messages-keys/DECISIONS.md D1/D2). A module-level constant
    built from ``operator_override_note(...)`` was computed once at IMPORT
    time, before any request-scoped ``payload``/``git_root`` existed to
    resolve an audience against -- the same no-payload-at-composition-time
    defect this whole workstream exists to remove, one level up from the
    per-firing string it wraps. Callers pass their own in-scope ``payload``/
    ``git_root``; each of the four call sites below applies the SPLICE
    CONTRACT documented on ``operator_override_note`` itself (gate the
    trailing sentence on ``bool(...)`` rather than always appending it) --
    an empty return here means the caller must render NO trailing pointer
    sentence at all, not a bare "If this is genuinely intended: ".
    """
    note = operator_override_note("COORDINATOR_ALLOW_ORPHAN", payload=payload, git_root=git_root)
    if not note:
        return ""
    return "If this is genuinely intended: %s" % note


def _new_git_memo() -> Callable[[List[str], Optional[str]], Tuple[int, str]]:
    """Factory for a per-call `_run_git` memo, keyed on the exact
    `(cwd, args)` pair actually run.

    F0-b (2026-08-05, spawn-count defect): shared by every guard function
    with a per-segment/per-target loop that re-resolves an identical `git`
    query multiple times -- first found in `check_destructive_git_orphan`
    CHECK 1 (`rev-parse --verify`/`rev-list --count` spawned once per
    segment of a chained `git reset --hard`, ~2 spawns/segment, ~1,778
    spawns / 9.9s at 16 KiB) and, same session, `check_destructive_rm`'s
    scratch-allowlist/dirty-work probes (`git -C <parent-dir> rev-parse
    --show-toplevel`/`--git-dir`, spawned once per rm TARGET with
    byte-identical argv whenever two targets share a parent directory --
    ~1 extra spawn per target, linear: `rm a.py b.py c.py` in one dir
    measured 5 spawns before this fix, 4 after).

    Repo state cannot change mid-dispatch (a single synchronous hook
    invocation, no concurrent mutator), so exact-key reuse within ONE call
    into the owning guard function is sound: identical args+cwd are
    guaranteed to return the identical `(rc, stdout)` pair, so this can
    never turn a failed resolution into a cached success or vice versa --
    it is exact-key reuse, not a heuristic.

    Returns a closure over a FRESH dict on every call -- deliberately NOT a
    module-level cache. Each caller (e.g. `check_destructive_git_orphan`,
    `check_destructive_rm`) must call this once per invocation of ITSELF and
    keep the returned closure local to that call, never hoisted to module
    scope: the keys are attacker-influenced (ref names, filesystem paths
    from the command under evaluation), so a shared cache would have
    unbounded growth and would leak resolved verdicts across unrelated
    dispatches / test runs sharing one interpreter.
    """
    memo: Dict[Tuple[Optional[str], Tuple[str, ...]], Tuple[int, str]] = {}

    def _memo_run_git(args: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
        key = (cwd, tuple(args))
        cached = memo.get(key)
        if cached is None:
            cached = _run_git(args, cwd=cwd)
            memo[key] = cached
        return cached

    return _memo_run_git


def _extract_git_c_dir(seg: str) -> str:
    m = re.search(r"git\s+-C\s+(\S+)", seg)
    if not m:
        return ""
    return _strip_q(m.group(1))


# Flags whose operand is free-form prose, never an actual command token --
# CHECK 2 must not let the operand's WORDING influence its verdict (a commit
# message that *describes* a force-push is not a force-push). See
# cross-repo/inbox/2026-07-20-claude-central-em-orphan-guard-check2-prose-
# false-positive.md.
_FREETEXT_OPERAND_FLAGS = ("-m", "--message")


def _seg_excluding_freetext_operands(seg: str) -> str:
    """Tokenize `seg` with shlex and drop the operand tokens of free-text
    flags (-m / --message) before CHECK 2 pattern-matches it, so a commit
    message that merely *discusses* force-pushing (prose) can never trip the
    same regex that detects an ACTUAL forcing `git push`/`-f`/`+refspec`
    token. Only the flag's OWN token is ever stripped -- everything else
    (subcommand, remote, branch, other flags) passes through unchanged, so
    this can only narrow what CHECK 2 sees, never widen it.

    Fails closed, never open: if `shlex.split` raises (malformed/unterminated
    quoting -- can't be parsed as a real argv), returns `seg` UNCHANGED, so
    the caller's existing raw-segment regex scan runs exactly as it did
    before this fix (still over-blocking, never under-blocking).
    """
    if _bt_exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- returns `seg` UNCHANGED, the same over-blocking branch an
        # unterminated quote already reaches.
        return seg
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return seg
    kept: List[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _FREETEXT_OPERAND_FLAGS:
            skip_next = True
            continue
        if any(tok.startswith(flag + "=") for flag in _FREETEXT_OPERAND_FLAGS):
            continue
        kept.append(tok)
    return " ".join(kept)


def _seg_resolved_git_subcommand(seg: str) -> Optional[str]:
    """Positionally resolve the git SUBCOMMAND `seg` invokes -- the first
    non-flag token after a command-position `git`, walking past git's own
    global options (`_GIT_GLOBAL_OPT_WITH_ARG` / `_GIT_GLOBAL_OPT_NO_ARG_
    SIMPLE`, defined below and shared with `_command_really_invokes`) --
    or `None` if that cannot be determined with confidence.

    CHECK 2 originally excluded `git stash push` via a substring regex
    (`\\bstash\\s+push\\b`), which a coordinator review caught as a bypass
    of a bypass-prevention guard: `git push origin main --force -- stash
    push` and `git push origin main --force # git stash push` both contain
    that substring while genuinely invoking `push`, so the exclusion
    suppressed CHECK 2 on a real forcing push. This positional walk fixes
    that by construction -- it returns the FIRST non-flag token after
    `git`, so a trailing pathspec or comment mentioning `stash push` can
    never influence which subcommand actually got resolved.

    Same design as `block_subagent_destructive_action._real_git_
    subcommand` (its own module docstring's "COMMAND-POSITION GIT-TOKEN
    FIX", ~line 1132 there) -- not reused directly (different module,
    different token-source contract: that function walks tokens already
    past a confirmed `git` token, this one also confirms command-position
    `git` itself via `_normalize_executable_basename`) but the same
    fail-CLOSED discipline: an unresolvable case (unparseable `seg`, no
    command-position `git`, or an unrecognized global flag whose argument
    shape is unknown) returns `None`, NEVER guesses. `None` means "cannot
    positively confirm a subcommand" -- callers must treat that as
    "keep the pre-existing, more conservative behavior", never as
    confirmation of any particular subcommand (including `push`).

    A `<<` heredoc marker anywhere in `seg` also forces `None`, even when
    `shlex.split` parses cleanly: a heredoc body is NOT part of the
    invoking command's own argv in real shell semantics (the shell reads
    it as raw input until the terminator line, never as further tokens on
    the command line), so treating it as ordinary follow-on argv would be
    confidently wrong, not merely uncertain. Concretely, `TestCheck2Heredoc
    BodyProseExclusion.test_unterminated_heredoc_with_force_push_in_body_
    fails_closed` feeds an UNTERMINATED `git commit -F - <<'MSG'` whose
    body is `git push origin main --force` -- `shlex.split` happily parses
    the whole thing as one argv (the quoted `'MSG'` closes immediately, so
    nothing raises) and resolves the OUTER command's subcommand as
    `commit`, which is positionally accurate but would silence CHECK 2 on
    a segment the existing fail-closed contract requires to keep denying
    (the caller upstream already decided this malformed heredoc could not
    be safely stripped, and left the raw text, `--force` included, for the
    regex fallback to catch).
    """
    if "<<" in seg:
        return None
    if _bt_exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- `None` routes the caller to its raw `\bpush\b` word match,
        # the same over-inclusive fallback an unparseable segment takes.
        return None
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return None
    if not tokens or _normalize_executable_basename(tokens[0]) != "git":
        return None
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
            if tok in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                i += 1
                continue
            # Unrecognized flag -- consumption shape unknown, do not guess.
            return None
        return tok
    return None


#: ALLOWLIST (deliberately, not a denylist) of ordinary POSIX-ish utility
#: heads that are positionally CONFIRMED to never themselves launch `git` --
#: none of these execs argv it's handed, spawns a subprocess, or interprets
#: further shell/script text, so once one of these sits at command position,
#: nothing later in the segment (a path, a pattern, a message) can still
#: turn into a `git push --force`. Covers the P2/P3 shapes exactly: `test -f
#: .../push-failures.log`, `rm -f push.log`, `grep -f patterns-push.txt`.
#: An unrecognized head (anything NOT in this set, including a wrapper,
#: shell, or scripting interpreter, or plain unfamiliarity) stays AMBIGUOUS
#: -- see `_seg_confirmed_not_git_invocation`'s own docstring for why this
#: must be an allowlist, not "everything except a known-dangerous set": a
#: scripted spawn like `subprocess.run(['git', 'push', ..., '--force'])`
#: (kept visible by `_strip_heredoc_bodies_for_prose_scan`'s spawn-indicator
#: check) has a head token that is neither `git` nor any recognizable
#: wrapper/interpreter word either -- a denylist would wrongly call that
#: "confirmed safe" and skip the fallback scan that is this shape's ONLY
#: remaining detection path.
_CHECK2_SAFE_NONSPAWNING_HEADS = frozenset({
    "test", "[", "[[", "true", "false", "echo", "printf",
    "cat", "ls", "stat", "wc", "head", "tail", "grep", "rm", "cp", "mv",
    "touch", "mkdir", "rmdir", "basename", "dirname", "pwd", "du", "df",
    "chmod", "chown", "ln", "readlink", "realpath",
})


def _seg_confirmed_not_git_invocation(seg: str) -> bool:
    """True only when `seg` positionally resolves to a command whose HEAD is
    a member of `_CHECK2_SAFE_NONSPAWNING_HEADS` -- a plain utility that is
    definitely not `git` and definitely cannot itself spawn `git` further
    down (unlike a shell, `env`, `xargs`, or a scripting interpreter, all of
    which are deliberately left OUT of that allowlist and so stay ambiguous).

    P2/P3 fix (2026-07-30): CHECK 2's forcing-push scan previously treated
    EVERY segment where `_seg_resolved_git_subcommand` returned `None` the
    same way -- falling back to a raw `\\bpush\\b` word scan over the whole
    segment text, then a bare `-f` scan. That fallback exists for a
    genuinely ambiguous case (a heredoc body, or a scripted spawn like
    `subprocess.run(['git', 'push', ..., '--force'])`, where positional
    resolution legitimately cannot be trusted and CHECK 2 must still see the
    raw text). But `_seg_resolved_git_subcommand` also returns `None` for a
    SECOND, entirely different reason: a segment that parses cleanly and
    positively resolves to a command that is simply not `git` at all --
    `test -f /Users/x/.git/push-failures.log` parses with `tokens[0] ==
    "test"`, a CONFIRMED non-git, non-spawning command, not an unresolvable
    one. Collapsing "confirmed safe" into the same bucket as "cannot tell"
    let a read-only `test -f` against a path that merely CONTAINS the
    substrings "git" and "push" (plus a `-f` flag with unrelated semantics)
    get denied as though it were a forcing push. Same root cause misfires on
    `rm -f push.log` / `grep -f patterns-push.txt`.

    This function narrows the fallback to genuine ambiguity only: a segment
    that (a) fails to shlex-parse, (b) contains a heredoc marker, or (c)
    positionally resolves to any head NOT in the allowlist above (a shell,
    `env`, `xargs`, a scripting interpreter, an unrecognized wrapper, or
    truly anything else this function doesn't positively vouch for) remains
    eligible for the raw `\\bpush\\b` fallback scan. Only a segment whose
    head is a KNOWN, enumerated, non-spawning utility is definitively not a
    push candidate -- no further text scan runs, so a `push`/`-f` substring
    anywhere in ITS operands can no longer matter. Fails closed as always:
    any parse failure, empty tokens, or unrecognized head returns `False`
    (stays eligible for the existing raw scan), never `True`.
    """
    if "<<" in seg:
        return False
    if _bt_exceeds_tokenizable_ceiling(seg):
        # DoS bound inherited from `_command_tokenizer`, not a local tuning
        # knob -- `False` keeps the segment eligible for the raw scan, the
        # same fail-closed answer a parse failure already gives.
        return False
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    head = _normalize_executable_basename(tokens[0])
    return head in _CHECK2_SAFE_NONSPAWNING_HEADS


def check_destructive_git_orphan(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """F0-b (2026-08-05, spawn-count defect): CHECK 1's `rev-parse --verify
    <target>^{commit}` and `rev-list --count <target>..HEAD` probes ran once
    PER SEGMENT even though `target`/`git_cwd` are almost always identical
    across a chained command (`git reset --hard X && git reset --hard X`),
    answering a question this call already answered -- linear-in-segment-
    count subprocess spawns (measured ~2 spawns per segment at HEAD, ~1,778
    spawns / 9.9s on a 16 KiB chained-reset command). Fixed via `_new_git_
    memo`'s per-call, exact-(cwd, args)-keyed memo -- see that factory's
    docstring for why this is sound and why it is deliberately NOT
    module-level. Only wraps calls that are provably safe to reuse
    (repo/ref queries whose result depends solely on their own args + cwd,
    not on which segment reached them) -- see call sites below for which
    legs were left unmemoized and why.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)
    if _override("COORDINATOR_ALLOW_ORPHAN"):
        return None

    # BX-13 (2026-08-17, confirmed live bypass -- guard-bypass-triage
    # Finding 1): captured BEFORE the heredoc/quoted-span stripping below,
    # which deletes a whitespace-containing `sh -c '...'`/`bash -c "..."`
    # payload WHOLESALE as "inert prose" -- the exact mechanism that
    # correctly allows `echo "reviewing git commit conventions"` -- so
    # `sh -c 'git reset --hard $(echo HEAD~3)'` never reached the `\bgit\b`
    # gate this function used to have here at all. Rescanned via
    # `_shell_c_unwrap_payloads` (this module's own copy of
    # `block_subagent_commit.py`'s `_wrapped_shell_c_payloads` -- see that
    # constant's docstring above) AFTER the segment loop below, near the
    # bottom of this function, same shape `check_no_verify`/
    # `check_destructive_rm` already use for the identical bug class.
    raw_cmd = cmd
    # Prose-heredoc-body stripping runs BEFORE `_strip_ws_quoted_spans` --
    # that scanner's whitespace-containing-span rule can otherwise mangle a
    # multi-line heredoc body containing an unpaired quote char, corrupting
    # the terminator line this function still needs to find. See
    # `_strip_heredoc_bodies_for_prose_scan`'s own docstring for the
    # shell-fed-vs-prose discriminator (CHECK 2/3 fix, cross-repo memo
    # 2026-07-2x heredoc-body-prose-false-positive).
    cmd = _strip_heredoc_bodies_for_prose_scan(cmd)
    # Comments run AFTER heredoc-body stripping (so a `#` inside a body the
    # pass above discarded is never consulted) and BEFORE
    # `_strip_ws_quoted_spans` (which would otherwise delete the quoted
    # spans this scanner needs to track quote state across). See
    # `_strip_shell_comments_for_prose_scan` for why this cannot widen.
    cmd = _strip_shell_comments_for_prose_scan(cmd)
    cmd = _strip_ws_quoted_spans(cmd)
    # Restore quote chars sentinel-protected by `_strip_heredoc_bodies_for_
    # prose_scan` for KEPT heredoc-body lines, now that the whitespace-quoted-
    # span scanner above has run (and so can no longer delete a multi-word
    # source-string literal it would otherwise mistake for shell free text).
    cmd = _restore_protected_quotes(cmd)

    # No top-level `\bgit\b` fast-path return here (deliberately removed,
    # BX-13 fix): the per-segment gate at the top of the loop below already
    # skips every git-free segment, and a hard early return here is exactly
    # what let the `sh -c` bypass above short-circuit past the rescan at the
    # bottom of this function before it could ever run.

    # Per-call memo, keyed on the exact (cwd, args) pair actually run --
    # see `_new_git_memo` docstring for why this is sound and why it is
    # deliberately NOT module-level.
    _memo_run_git = _new_git_memo()
    _orphan_hint = _orphan_override_hint(payload, git_root)

    for seg in _split_segments(cmd):
        if not seg.strip():
            continue
        if not re.search(r"\bgit\b", seg):
            continue

        c_dir = _extract_git_c_dir(seg)
        git_cwd = _orphan_c_cwd(c_dir)

        # CHECK 1 -- git reset --hard <target>
        if re.search(r"\breset\b", seg) and re.search(r"--hard", seg):
            after = re.sub(r".*(^|\s)reset(\s|$)", " ", seg, count=1)
            # A lone `$(`/backtick in `after` is not proof of a subshell-
            # resolved target: prose that NAMES the hazard (a markdown code
            # span, a comment, a string literal assembled elsewhere) can
            # leave exactly one delimiter in this slice -- the other half of
            # its pair sits before `reset`, outside `after` entirely. A real
            # target opens AND closes its subshell inside `after`, so require
            # the matched pair rather than bare presence (DR-144).
            #
            # The pair is matched across NEWLINES, not just within one line:
            # `git reset --hard $(\n  git rev-parse origin/main\n)` is ordinary
            # shell, and a single-line-only rule dropped it from CHECK 1
            # entirely (it survived only via the dirty-tree arm, which does not
            # fire on a clean tree). Prose cannot exploit the widening because
            # the two strippers upstream of here remove the shapes that carry
            # it -- `_strip_shell_comments_for_prose_scan` for comments, and
            # `_heredoc_body_has_spawn_indicator`'s interpreter-gated backtick
            # arm for Python/Node bodies -- so what reaches `after` with
            # delimiters still in it is shell, where they mean what they say.
            if re.search(r"\$\(.*\)|`.*`", after, re.DOTALL):
                return _deny(
                    "BLOCKED: 'git reset --hard' with a subshell-resolved "
                    "target ($(...) or backticks) cannot be verified safe — "
                    "the hook will not execute the subshell to learn what it "
                    "points at.\n\n"
                    "Resolve the ref to a literal first and re-check what it "
                    "would drop:\n  git rev-list --count <resolved-ref>..HEAD"
                    + ("\n\n" + _orphan_hint if _orphan_hint else "")
                )
            if not re.search(r"(^|\s)--(\s|$)", after):
                bare = [
                    _strip_q(tok) for tok in after.split() if not tok.startswith("-")
                ]
                if len(bare) <= 1:
                    target = bare[0] if bare else "HEAD"
                    # F0-b: memoized -- (target, git_cwd) is usually identical
                    # across every segment of a chained `git reset --hard`, and
                    # repo state cannot change mid-dispatch, so re-resolving
                    # the same ref/count on segment 2..N is pure repeated
                    # work. See `_new_git_memo` docstring.
                    rc, _ = _memo_run_git(["rev-parse", "--verify", "%s^{commit}" % target], git_cwd)
                    if rc == 0:
                        rc2, out = _memo_run_git(["rev-list", "--count", "%s..HEAD" % target], git_cwd)
                        n_str = out.strip() if rc2 == 0 else "0"
                        try:
                            n = int(n_str)
                        except ValueError:
                            n = 0
                        if n > 0:
                            # Routed through the same per-call memo as the
                            # rev-parse/rev-list probes above (2026-08-07,
                            # spawn-storm sweep D6) -- this call is a
                            # single-shot in practice (the function returns
                            # on this branch immediately below), so the
                            # memo buys no spawn-count reduction here, but
                            # nothing about repo state can change mid-
                            # dispatch either, so there is no freshness
                            # reason to keep it as a bare `_run_git` call
                            # sitting beside two memoized siblings.
                            _, branch_out = _memo_run_git(["rev-parse", "--abbrev-ref", "HEAD"], git_cwd)
                            cur_branch = branch_out.strip() or "HEAD"
                            _, subj_out = _run_git(
                                ["log", "--format=  - %h %s", "%s..HEAD" % target], cwd=git_cwd
                            )
                            subjects = "\n".join(subj_out.splitlines()[:5])
                            more = "\n  ... and %d more" % (n - 5) if n > 5 else ""
                            _reset_trailer = (
                                "\n\nIf those %d commits are genuinely disposable (or "
                                "provably safe on another ref), %s" % (n, _orphan_hint)
                                if _orphan_hint
                                else ""
                            )
                            return _deny(
                                (
                                    "BLOCKED: 'git reset --hard %s' would drop %d "
                                    "commit(s) from branch '%s'.\n\n"
                                    "These commits are reachable from HEAD but NOT from "
                                    "%s, so the reset orphans them:\n%s%s\n\n"
                                    "This is the 2026-05-28 near-miss shape: a hard reset to "
                                    "a ref that is BEHIND your current work. Before overriding, "
                                    "re-derive the TRUE state yourself (do not trust a "
                                    "remembered count):\n"
                                    "  git rev-list --count %s..HEAD   # commits you "
                                    "would lose; must be 0 to be safe\n"
                                    "  git branch -a --contains HEAD          # other refs "
                                    "that already hold this work"
                                    % (target, n, cur_branch, target, subjects, more, target)
                                )
                                + _reset_trailer
                            )

        # CHECK 2 -- force push
        # Evaluated on the tokenized segment with -m/--message operands
        # removed -- see `_seg_excluding_freetext_operands` docstring. This
        # must match on what the command DOES, never on what a commit
        # message SAYS.
        seg_check2 = _seg_excluding_freetext_operands(seg)
        # MENTION-vs-INVOCATION gate. `\bpush\b` also matches `push` as
        # `git stash push`'s own subcommand token -- a local, non-remote
        # operation with no forcing semantics at all -- so the free-text
        # word match alone denies `git stash push -f -- <paths>` and
        # `git stash push -m "x" -- +path` as if they were a forced remote
        # push.
        #
        # A prior fix here used a `\bstash\s+push\b` substring exclusion --
        # a coordinator review found that's a bypass of a bypass-prevention
        # guard: `git push origin main --force -- stash push` and `git push
        # origin main --force # git stash push` both genuinely invoke
        # `push` yet contain that substring, so the exclusion silently
        # suppressed CHECK 2 on a real forcing push. Fixed by resolving the
        # segment's subcommand POSITIONALLY instead (`_seg_resolved_git_
        # subcommand`, first non-flag token after `git`) -- a substring can
        # be planted anywhere in a segment; a subcommand's ARGV POSITION
        # cannot.
        #
        # `resolved is None` (unparseable segment, no command-position
        # `git`, or an unrecognized global flag) deliberately falls back to
        # the plain `\bpush\b` word match rather than skipping CHECK 2 --
        # this is what keeps `TestCheck2ScriptableHeredocSpawnDetection`
        # failing CLOSED: a heredoc body's mangled shlex tokens (`subprocess
        # .run(['git', 'push', ...])`) never resolve a clean `git` command
        # position, so `resolved` is `None` there and CHECK 2 still examines
        # the segment via the original word-match path.
        _resolved_subcommand = _seg_resolved_git_subcommand(seg_check2)
        if _resolved_subcommand is not None:
            _is_push_candidate = _resolved_subcommand == "push"
        elif _seg_confirmed_not_git_invocation(seg_check2):
            # Positionally confirmed non-git, non-spawning head (`test -f
            # .../push-failures.log`, `rm -f push.log`, `grep -f
            # patterns-push.txt`) -- P2/P3 fix, see
            # `_seg_confirmed_not_git_invocation` docstring. Never a push
            # candidate; the raw `\bpush\b`/`-f` fallback below must not run
            # against this segment's free-text operands.
            _is_push_candidate = False
        else:
            _is_push_candidate = bool(re.search(r"\bpush\b", seg_check2))
        if _is_push_candidate:
            if re.search(
                r"(--force([^-=]|$)|(^|\s)-[a-zA-Z]*f[a-zA-Z]*(\s|$)|(^|[\s\"'])\+\S+)",
                seg_check2,
            ):
                return _deny(
                    "BLOCKED: this 'git push' uses a forcing form (--force / -f / "
                    "+refspec) that rewrites remote history and can drop commits "
                    "existing only on the remote (a concurrent push you have not "
                    "fetched).\n\n"
                    "Use --force-with-lease instead. It refuses the push if the "
                    "remote moved since your last fetch — exactly the protection "
                    "plain --force discards:\n"
                    "  git push <remote> <branch> --force-with-lease"
                    + ("\n\n" + _orphan_hint if _orphan_hint else "")
                )

        # CHECK 3 -- force-delete branch
        if re.search(r"\bbranch\b", seg):
            force_delete = False
            if re.search(r"(^|\s)-[a-zA-Z]*D[a-zA-Z]*(\s|$)", seg):
                force_delete = True
            if (
                re.search(r"((^|\s)-[a-zA-Z]*d[a-zA-Z]*(\s|$)|--delete)", seg)
                and re.search(r"((^|\s)-[a-zA-Z]*f[a-zA-Z]*(\s|$)|--force)", seg)
            ):
                force_delete = True
            if force_delete:
                # F0-b: memoized -- `git_cwd` is the only input (a fixed
                # "is this a repo" probe), so it is loop-invariant across
                # every force-delete segment sharing the same -C/cwd, unlike
                # the two calls below whose result varies with `br` (branch
                # name) per segment.
                rc, _ = _memo_run_git(["rev-parse", "--git-dir"], git_cwd)
                if rc == 0:
                    after = re.sub(r".*(^|\s)branch(\s|$)", " ", seg, count=1)
                    # F0-c: a single batched membership set, NOT the
                    # exact-key memo above -- `br` varies per token, so the
                    # (cwd, args)-keyed memo cannot collapse a per-token
                    # `rev-parse --verify`. See `_local_branch_names`
                    # docstring for the equivalence argument and the fail-
                    # direction preserved on lookup failure.
                    _branch_names = _local_branch_names(_memo_run_git, git_cwd)
                    for tok in after.split():
                        if tok.startswith("-"):
                            continue
                        br = _strip_q(tok)
                        if not _branch_names or br not in _branch_names:
                            continue
                        rc3, out3 = _memo_run_git(
                            ["branch", "-a", "--contains", "refs/heads/%s" % br, "--format=%(refname)"],
                            git_cwd,
                        )
                        others = [
                            line for line in out3.splitlines()
                            if line != "refs/heads/%s" % br and not re.match(r"refs/remotes/[^/]*/HEAD", line)
                        ]
                        if not others:
                            _branch_trailer = (
                                "\n\nIf '%s' is truly disposable, %s" % (br, _orphan_hint)
                                if _orphan_hint
                                else ""
                            )
                            return _deny(
                                (
                                    "BLOCKED: force-deleting branch '%s' would orphan "
                                    "its commits — they live on NO other ref (no local "
                                    "branch, no remote).\n\n"
                                    "'%s' is not contained in any other branch or "
                                    "remote. The lowercase, non-forced 'git branch -d' "
                                    "refuses exactly this case; the force form ('-D', or "
                                    "'-d --force') overrides that safety.\n\n"
                                    "To preserve the work first:\n"
                                    "  git checkout <target> && git merge %s"
                                    % (br, br, br)
                                )
                                + _branch_trailer
                            )

    # BX-13 rescan (see comment near `raw_cmd` above): unwrap any
    # `sh -c`/`bash -c`/etc. payload from the RAW, pre-stripping command text
    # and re-run this SAME function against it, so a wrapped `git reset
    # --hard $(...)` (or force-push, or force-delete-branch) gets the full
    # CHECK 1-3 treatment rather than a second, independently-drifting
    # implementation. Bounded recursion: `_shell_c_unwrap_payloads` itself
    # caps nesting depth and fails safe (`[]`) on unparseable input, so this
    # can only ADD coverage, never loop or crash.
    for _shell_payload in _shell_c_unwrap_payloads(raw_cmd):
        _result = check_destructive_git_orphan(_shell_payload, session_id, payload, git_root)
        if _result is not None:
            return _result

    return None


def _local_branch_names(
    memo_run_git: Callable[[List[str], Optional[str]], Tuple[int, str]], git_cwd: Optional[str]
) -> Optional[set]:
    """Single-spawn membership set for "does refs/heads/<name> exist",
    replacing a `rev-parse --verify refs/heads/<name>` spawned once PER
    BRANCH TOKEN (F0-c, 2026-08-05: `git branch -D <N tokens>` spawned N
    `rev-parse --verify` calls -- exactly 1 per token, ~1,780 ms at 400
    tokens). Unlike the reset/rm legs (F0-b), the per-call args here
    genuinely VARY per token (a different branch name each time), so the
    exact-(cwd, args)-keyed `_new_git_memo` cannot collapse repeats of THIS
    call the way it does for the reset/rm legs -- the fix instead replaces
    N distinct existence probes with a single enumeration.

    `git for-each-ref --format=%(refname:short) refs/heads/` lists every
    local branch in ONE spawn, ref-backend agnostic (loose or packed) and
    correct for slash-named branches (`feature/x` prints as `feature/x`,
    not `refs/heads/feature/x`) -- the same domain `rev-parse --verify
    refs/heads/<name>` was checking membership of, so a Python `in` test
    against the returned set is an equivalent existence check, not an
    approximation.

    Returns `None` on a for-each-ref failure so the caller treats EVERY
    candidate branch the way a failed `rev-parse --verify` would have
    (skip, never deny) -- preserving the "a nonexistent/unresolvable ref
    causes `continue`, not a deny" fail direction this guard has always
    had. The call itself is routed through the shared per-call
    `memo_run_git` (args are constant per `git_cwd`, so repeated per-
    segment calls within one dispatch already collapse to a single real
    spawn via that memo, same as any other loop-invariant call in this
    function).
    """
    rc, out = memo_run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], git_cwd)
    if rc != 0:
        return None
    return set(out.splitlines())


def _orphan_c_cwd(c_dir: str) -> Optional[str]:
    """F0: per-segment `git -C <dir>` becomes subprocess `cwd=<dir>` (git -C
    is directory-scoped identically to running the git binary FROM that
    directory for the purposes of this dispatcher's checks). Empty c_dir ->
    None (bare cwd-relative `git rev-parse`, mirrors the bash `GOPT=()` case)."""
    return c_dir or None


# ---------------------------------------------------------------------------
# 3. check_destructive_rm -- block-destructive-rm.sh
# F0: per-target repo resolution (git -C "$(dirname TGT_ABS)"), never a
# single dispatcher-level root.
# ---------------------------------------------------------------------------

_RM_CMD_RE = re.compile(
    r"^\s*((sudo|command|time|exec|nice|nohup)\s+|env\s+(\S+=\S*\s+)*|"
    r"[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(/\S*/)?rm(\s|$)"
)
_RM_WRAPPER_WORDS = {
    "sudo", "command", "time", "exec", "nice", "nohup", "env",
    "ionice", "timeout", "stdbuf", "which", "type",
}


def _rm_is_rm_segment(seg: str) -> bool:
    prev = None
    s = seg
    while s != prev:
        prev = s
        s = s.lstrip()
        if s.startswith("$("):
            s = s[2:]
        elif s.startswith("("):
            s = s[1:]
        elif s.startswith("{"):
            s = s[1:]
        elif s.startswith("!"):
            s = s[1:]
        elif s.startswith("`"):
            s = s[1:]
        elif s.startswith("\\"):
            s = s[1:]
    probe = s.replace("'", "").replace('"', "")
    # Bundled `-c` (2026-07-29, EM-run second-pass integration, confirmed
    # live): `sh -ic '...'` behaves as `sh -i -c '...'`, so an exact `-c`
    # token requirement missed it -- `sh -ic "rm -rf ..."` / `sh -ic "git
    # clean -fdx"` / `sh -ic "git <revert-verb>..."` all silently sailed
    # through as prose. Widened to `-[a-zA-Z]*c[a-zA-Z]*` (same tolerance as
    # `_BUNDLED_C_FLAG_RE` elsewhere in this file/package).
    if re.match(r"^(bash|sh|zsh|dash)\s+(\S+\s+)*-[a-zA-Z]*c[a-zA-Z]*(\s|$)|^eval(\s|$)", probe):
        return bool(re.search(r"\brm\b", probe))
    firstword = s.split(None, 1)[0] if s.split(None, 1) else ""
    if firstword in _RM_WRAPPER_WORDS:
        return bool(re.search(r"(^|[\s({`])(/\S*/)?rm([\s)}`]|$)", s))
    return bool(_RM_CMD_RE.search(probe))


def _abs_path(p: str, base: Optional[str] = None) -> str:
    """Resolve p to an absolute path, forward-slash-normalized.

    Windows pathlib.Path.resolve() renders backslashes; the legacy bash
    checks this ports (block-destructive-rm.sh and siblings) run under
    Git-Bash / git own path plumbing, which emits forward slashes
    unconditionally (git itself always prints forward-slash-separated
    paths, even for Windows targets). A raw backslash rendering here is a
    PARITY BREAK against the golden corpus (deny-message paths and the
    git -C / status / stash command lines it prints), not an intentional
    platform difference -- normalize before returning so every caller
    (including the verbatim-quoted git -C "<path>" lines in the deny
    message) matches legacy byte-for-byte.
    """
    try:
        resolved = str(Path(p if os.path.isabs(p) else os.path.join(base or os.getcwd(), p)).resolve())
    except OSError:
        # Resolution failure (e.g. broken symlink loop, permission denied on an
        # ancestor dir) -- fall back to the raw input unresolved/unnormalized;
        # callers treat this the same as any other unresolved-but-present path.
        return p
    return resolved.replace("\\", "/")


_HOME_VAR_RE = re.compile(r"\$(?:HOME\b|\{HOME\})")


def _expand_home_var(tok: str) -> Optional[str]:
    """Expand a ``$HOME``/``${HOME}`` reference in ``tok`` using this
    process's own ``HOME`` env var.

    Narrow and deliberate, not a general shell-variable expander: this hook
    runs out-of-band from the invoking shell and has no access to its
    environment or expansion rules, so expanding an ARBITRARY `$VAR` would
    be guessing, not resolving. `$HOME`/`${HOME}` is the one exception --
    it is well-known, essentially always matches the current process's own
    HOME, and skipping a recursive-rm target solely because it spells the
    home directory as ``$HOME/.claude`` instead of ``~/.claude`` is exactly
    the 2026-07-31 incident class (a target silently bypassing every check
    below because the token filter gave up on it).

    Returns ``None`` (never raises) when the token still contains an
    unresolved ``$`` afterward -- an unknown variable, or ``HOME`` unset --
    so the caller can fall back to skipping the token, same as before this
    fix, rather than acting on a partially-resolved path.
    """
    if "$" not in tok:
        return tok
    home = os.environ.get("HOME")
    if not home:
        return None
    # negative-spec: the replacement MUST stay a callable. Passing `home` as a
    # plain string makes re.sub parse it as a replacement TEMPLATE, in which
    # backslash sequences are escapes -- so a Windows HOME of `C:\Users\<name>`
    # raises `PatternError: bad escape \U`, breaking this function's own
    # "never raises" contract three lines up in the docstring. Observed live on
    # win32: every `rm` naming `$HOME` crashed the destructive-rm guard, which
    # fails CLOSED, so the guard denied the command having never inspected it.
    # A callable replacement is returned verbatim with no escape processing.
    expanded = _HOME_VAR_RE.sub(lambda _m: home, tok)
    if "$" in expanded:
        return None
    return expanded


def _is_same_dir(a: str, b: str) -> bool:
    """True when a and b name the SAME directory on disk.

    Identity is st_dev/st_ino via os.path.samefile, not a string compare,
    because the two are not equivalent on the filesystems this runs on:
      - case-insensitive APFS/HFS+ (macOS default) and NTFS make
        `~/.Claude` and `~/.claude` one directory, while os.path.normcase
        is an identity function off Windows and reports them different;
      - symlinks and bind-style mounts alias two spellings onto one inode.
    Both aliasing modes matter to a caller deciding whether a path about to
    be irreversibly deleted is a repository root.

    Returns False (never raises) when either side is missing or unstattable
    -- callers use this to ARM a deny, so an unresolvable path must not
    assert an identity it cannot prove. NOTE this is not an equivalent
    backstop for every caller: for `check_destructive_rm`'s root-deny branch
    on a CLEAN repo root, the only other leg left once this returns False is
    the dirty-work check a few lines down, which denies nothing when `git
    status --porcelain` is empty -- an unstattable path on an otherwise-clean
    root therefore gets no protection at all here, not a softer one.

    Also note: because `os.path.samefile` follows symlinks (as does the
    `_abs_path` resolution callers run first), a target that is a SYMLINK
    pointing at a repo root is reported identical to the root itself, even
    though `rm -rf <symlink>` (no trailing slash forcing traversal) would
    only unlink the symlink on POSIX, not delete the store. Accepted
    over-blocking (safe direction, not a regression) -- not narrowed here.
    """
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def check_destructive_rm(cmd: str, session_id: str = "") -> Optional[Dict[str, Any]]:
    """F0-b (2026-08-05, spawn-count defect): the per-TARGET loop below
    resolves `git -C <parent-dir> rev-parse --show-toplevel` (scratch-
    allowlist probe) and, for a file target, `git -C <parent-dir> rev-parse
    --show-toplevel` again (dirty-work root probe) once PER TARGET, with
    byte-identical argv whenever two targets share a parent directory --
    the overwhelmingly common case for a plain `rm a.py b.py c.py`. ~1 extra
    spawn per target, linear (measured: 1 target -> 4 spawns, 3 targets ->
    5, 10 targets -> 12, 30 targets -> 32; 15 targets across 3 different
    dirs -> 17, confirming the memo below still resolves each distinct dir
    independently). Fixed via `_new_git_memo`'s per-call, exact-(cwd, args)-
    keyed memo -- see that factory's docstring for why this is sound and
    why it is deliberately NOT module-level.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)
    cmd = _strip_heredoc_bodies(cmd)

    if not re.search(r"\brm\b", cmd):
        return None

    rm_override = _override("COORDINATOR_ALLOW_RM")

    # Per-call memo, keyed on the exact (cwd, args) pair actually run -- see
    # `_new_git_memo` docstring for why this is sound and why it is
    # deliberately NOT module-level.
    _memo_run_git = _new_git_memo()

    cur_repo = None
    rc, out = _run_git(["rev-parse", "--show-toplevel"])
    if rc == 0:
        cur_repo = out.strip()

    for seg in _split_segments(cmd):
        if not seg.strip():
            continue
        if not _rm_is_rm_segment(seg):
            continue
        # Skip `git ... rm` (staged removal, git-recoverable).
        if re.search(
            r"\bgit(\s+(-C\s+\S+|-c\s+\S+|--(git-dir|work-tree|namespace)(=\S+|\s+\S+)|"
            r"--exec-path(=\S+|\s+\S+)?|-p|--paginate|--no-pager|--bare|"
            r"--no-replace-objects|--literal-pathspecs|--glob-pathspecs|"
            r"--noglob-pathspecs|--icase-pathspecs|--no-optional-locks))*\s+rm\b",
            seg,
        ):
            continue

        after = re.sub(r".*(^|\s)rm(\s|$)", " ", seg, count=1)
        recursive = bool(re.search(r"(^|\s)-[a-zA-Z]*[rR][a-zA-Z]*(\s|$)|--recursive", after))

        if recursive and re.search(r"\$\(|`", after) and not rm_override:
            return _deny(
                "BLOCKED: 'rm' with a recursive flag and a subshell-resolved "
                "target ($(...) or backticks) cannot be verified safe — the "
                "hook will not run the subshell to learn what it would "
                "delete.\n\n"
                "Resolve the target to a literal path first and re-check what "
                "lives there:\n"
                "  git status --porcelain -- <resolved-path>   # uncommitted/"
                "untracked work that rm would destroy"
            )

        targets: List[Tuple[str, str]] = []
        for tok in after.split():
            if tok == "--" or tok.startswith("-"):
                continue
            raw_tok = tok
            if "$" in tok:
                expanded = _expand_home_var(tok)
                if expanded is None or any(c in expanded for c in ("*", "?", "[")):
                    # 2026-07-31: a bare `$`/`${...}` reference the hook
                    # cannot resolve in-process still gets skipped here --
                    # this is finding 8, an inherited/known bound (no shell
                    # environment access, no expansion rules) unchanged by
                    # this fix. Only the narrow, well-known $HOME/${HOME}
                    # case below is resolved, because that is exactly the
                    # incident class (a recursive rm on a $HOME-relative
                    # target silently bypassing every check here).
                    continue
                tok = expanded
            elif any(c in tok for c in ("*", "?", "[")):
                continue
            t = _strip_q(tok)
            if t.endswith(")"):
                t = t[:-1]
            if t.endswith("}"):
                t = t[:-1]
            # Review: code-reviewer (Finding 1) -- `t_raw` is the AS-TYPED
            # token (captured BEFORE any `$HOME`/`${HOME}` expansion above),
            # kept separate from `t` (which is already expanded for the
            # $HOME leg). For a plain or `~`-spelled token the two are
            # identical (expansion never touched `tok` in that case). Both
            # are threaded through so deny MESSAGES can show the raw, as-
            # typed spelling for EVERY target shape, not just tilde-spelled
            # ones.
            t_raw = _strip_q(raw_tok)
            if t_raw.endswith(")"):
                t_raw = t_raw[:-1]
            if t_raw.endswith("}"):
                t_raw = t_raw[:-1]
            targets.append((t_raw, t))

        for tgt, tgt_expanded in targets:
            # 2026-07-31: `os.path.exists`/`os.path.isdir` do NOT expand a
            # leading `~` themselves -- `rm -rf ~/.Claude` was tokenized
            # correctly but then silently `continue`d here because
            # `os.path.exists("~/.claude")` is False, before ANY guard leg
            # below got a chance to run. Expand a leading `~` the same way
            # guard_offer_git_c.py:_offer_normalize_path already does, and
            # use the expanded form for every filesystem probe below; `tgt`
            # itself stays the raw, as-typed string so deny MESSAGES still
            # show the user what they wrote. `tgt_expanded` is `tgt` for a
            # plain or `~`-spelled token, and the already-`$HOME`-expanded
            # path for a `$HOME`/`${HOME}`-spelled token (Finding 1 fix --
            # this used to overwrite `tgt` itself for that spelling, so
            # `tgt` was already absolute by message-build time despite the
            # comment above claiming otherwise for every spelling).
            tgt_resolved = os.path.expanduser(tgt) if tgt.startswith("~") else tgt_expanded
            if not tgt or not os.path.exists(tgt_resolved):
                continue
            tgt_abs = _abs_path(tgt_resolved)

            norm = tgt_abs.replace("\\", "/")
            if (
                "/coordinator-sessions/" in norm and "-claims/" in norm.split("/coordinator-sessions/", 1)[-1]
            ) or re.search(r"/coordinator-sessions/[^/]*-claims$", norm):
                if rm_override:
                    continue
                return _deny(
                    "BLOCKED: '%s' is a coordinator claim lock dir — "
                    "regenerable session scratch, NOT git history. Do not rm "
                    "it by hand.\n\n"
                    "Did you mean: cs_clear_claim_if_dead <class> <basename>\n\n"
                    "  cs_clear_claim_if_dead calls cs_claim_holder_live first "
                    "and refuses to clear a LIVE peer's claim. The manual-rm "
                    "path is exactly how a live peer gets stomped (see "
                    "CLAIM-CLEAR-LIVENESS in coordinator-tripwires.md).\n\n"
                    "  cs_clear_claim_if_dead is the canonical safe path for "
                    "clearing a stale claim.\n"
                    "  (Available in lib/coordinator-session.sh — source it "
                    "first if not already loaded.)" % tgt
                )

            # A `*.lock` file under the git store holds no committed data --
            # git writes a new index to `index.lock` and renames it onto
            # `index`; unlink the lock mid-flight and that rename FAILS and
            # the git command errors out loudly, leaving the on-disk index
            # untouched and still consistent. A second git that acquires a
            # fresh lock reads that same consistent index. The realistic
            # worst case is a failed git command, not a damaged repository
            # -- and the index is regenerable from HEAD plus the working
            # tree regardless. Hard blocks in this guard are reserved for
            # irreversible harm; this is not irreversible, so the prior
            # deny was a wall around the wrong path rather than a cheaper
            # right one -- an EM must be able to clear a git lock without
            # human escalation. See cross-repo/inbox/
            # 2026-08-12-example-retrieval-repo-em-git-index-lock-reaper.md. This leg
            # must fire FIRST and narrowly (it does not replace the branch
            # below, which still denies every non-lock git-store path).
            #
            # NEGATIVE SPEC -- do NOT turn this into an advisory that names
            # the reaper, however helpful that reads. `dispatch.py`'s guard
            # loop returns on the FIRST non-None envelope, so returning
            # anything here short-circuits every guard registered after this
            # one for the same command. A silent `continue` is the required
            # shape; the allow IS the message.
            is_git_store_target = norm.endswith("/.git") or "/.git/" in norm or os.path.basename(tgt_abs) == ".git"
            # Review: code-reviewer (dispatch d6708a9c, findings 1-2) -- the
            # allow is scoped to a lock FILE only; a `.lock`-suffixed
            # directory has no place in the rename-onto-index safety
            # argument above and must fall through to the general git-store
            # deny below.
            if is_git_store_target and os.path.basename(tgt_abs).endswith(".lock") and os.path.isfile(tgt_abs):
                continue

            # Review: code-reviewer (Finding 4) -- a target whose basename is
            # literally `.git` is caught HERE and denied with the generic
            # git-store message below, before it ever reaches the bare-repo
            # probe further down (`tgt_is_bare` / `--is-bare-repository`).
            # A `.git`-named bare repo is therefore denied via a different,
            # less specific message than the root-deny branch's bare-repo
            # wording -- the bare-repo leg is not exercised for every
            # bare-repo shape.
            if is_git_store_target:
                if rm_override:
                    continue
                return _deny(
                    "BLOCKED: 'rm' would delete part of the git store at "
                    "'%s'. This corrupts/destroys repository history "
                    "irreversibly — no checkout, reflog, or stash recovers it." % tgt
                )

            # A repo ROOT is the same irreversibility class as the `.git`
            # target above -- deleting it takes the store with it -- but it
            # reaches this code as an ordinary directory, so the check above
            # never sees it. It is denied on IDENTITY (the target resolves to
            # a toplevel, OR the target IS a bare repo -- a bare repo has no
            # separate worktree to distinguish it from its own store), not on
            # dirty-state: by the time the store is gone there is no status
            # to consult, and every clean/dirty distinction downstream is
            # moot.
            #
            # Resolution is by st_dev/st_ino (`_is_same_dir`), never by string
            # compare, because a case-insensitive filesystem makes
            # `~/.Claude` and `~/.claude` the SAME directory while
            # `os.path.normcase` (a no-op off Windows) reports them distinct.
            # That exact aliasing deleted a live `~/.claude` on 2026-07-31
            # via a probe-cleanup `rm -rf` whose author believed it named a
            # separate case-variant directory.
            #
            # `tgt_top`/`tgt_is_bare` are computed ONCE here (a single
            # `rev-parse --show-toplevel` plus, only when that comes back
            # empty, a single `--is-bare-repository` probe) and reused by the
            # dirty-work branch below instead of re-invoking `_run_git` with
            # the same `-C <dir> rev-parse --show-toplevel` args a second
            # time on this hot path.
            tgt_is_dir = os.path.isdir(tgt_resolved)
            tgt_top = ""
            tgt_is_bare = False
            if tgt_is_dir:
                # F0-b: memoized -- safe to reuse if the same dir target
                # repeats (e.g. `rm -rf dirA dirA`); see `_new_git_memo`.
                rc_top, out_top = _memo_run_git(["-C", tgt_abs, "rev-parse", "--show-toplevel"])
                tgt_top = out_top.strip() if rc_top == 0 else ""
                if not tgt_top:
                    # `rev-parse --show-toplevel` fails inside a BARE repo
                    # (no working tree to name) -- without this probe a
                    # bare repo target would fall straight through both this
                    # branch and the dirty-work branch below (which also has
                    # no dirty-state fallback for a bare repo) undenied.
                    #
                    # Review: code-reviewer (Finding 3) -- `--show-toplevel`
                    # also fails for the OVERWHELMINGLY common case of "a
                    # plain, non-repo scratch directory" -- not just for a
                    # bare repo. Spawning a second git process to rule out
                    # "is this a bare repo" on EVERY such target doubled
                    # subprocess cost on the single most frequent shape of
                    # this check. A real (bare or non-bare) git directory
                    # always has a `HEAD` file and an `objects/` subdir at
                    # its root -- cheap `os.path` checks that can rule out
                    # the common non-repo case without a spawn. This can
                    # only narrow when the spawn happens, never skip a
                    # genuine bare repo: a bare repo's toplevel dir IS its
                    # git dir, so it always has both.
                    looks_like_git_dir = os.path.isfile(
                        os.path.join(tgt_abs, "HEAD")
                    ) and os.path.isdir(os.path.join(tgt_abs, "objects"))
                    if looks_like_git_dir:
                        # F0-b: memoized, same rationale as `tgt_top` above.
                        rc_bare, out_bare = _memo_run_git(
                            ["-C", tgt_abs, "rev-parse", "--is-bare-repository"]
                        )
                        tgt_is_bare = rc_bare == 0 and out_bare.strip() == "true"
                if tgt_is_bare or (tgt_top and _is_same_dir(tgt_top, tgt_abs)):
                    if rm_override:
                        continue
                    store_desc = tgt_top if tgt_top else tgt_abs
                    # A LINKED `git worktree add` worktree's actual .git
                    # store lives in the MAIN repo (`.git/worktrees/<name>`)
                    # -- deleting the worktree directory loses its local
                    # uncommitted changes, not the repo's full history.
                    # Branch the claim so it's true for both target shapes.
                    is_linked_worktree = False
                    if tgt_top and not tgt_is_bare:
                        # F0-b: memoized, same rationale as `tgt_top` above.
                        rc_gd, out_gd = _memo_run_git(["-C", tgt_abs, "rev-parse", "--git-dir"])
                        if rc_gd == 0 and "/worktrees/" in out_gd.strip().replace("\\", "/"):
                            is_linked_worktree = True
                    if is_linked_worktree:
                        loss_desc = (
                            "removes this worktree's local uncommitted "
                            "changes and its link to the repository's git "
                            "metadata -- commits, branches, stashes, and "
                            "reflog entries live in the MAIN repo's .git "
                            "store, not here, but this worktree's own "
                            "uncommitted work"
                        )
                    else:
                        loss_desc = (
                            "deletes the .git store along with the "
                            "worktree — every commit, branch, stash, and "
                            "reflog entry that exists only here"
                        )
                    return _deny(
                        "BLOCKED: '%s' is the ROOT of a git repository (%s). "
                        "'rm' on it %s "
                        "is gone irreversibly, "
                        "plus all gitignored content (settings, caches, "
                        "credentials, session state) that no commit ever "
                        "held.\n\n"
                        "This deny is on identity, not on dirty-state: a "
                        "clean worktree does NOT make deleting the store "
                        "safe.\n\n"
                        "If you are removing a CLONE that is fully pushed, "
                        "confirm every ref is on a remote first:\n"
                        '  git -C "%s" log --branches --not --remotes '
                        "--oneline    # empty == nothing unpushed\n"
                        '  git -C "%s" stash list                          '
                        "  # stashes are never pushed\n"
                        '  git -C "%s" status --porcelain --ignored        '
                        "  # untracked + gitignored content\n\n"
                        "Beware case-insensitive filesystems: a path that "
                        "LOOKS like a case-variant probe directory can be the "
                        "real one. Verify with:\n"
                        '  python3 -c "import os;print(os.stat(%r).st_ino)"'
                        % (tgt, store_desc, loss_desc, tgt_abs, tgt_abs, tgt_abs, tgt_abs)
                    )

            # Scratch allowlist.
            scratch_bn = os.path.basename(tgt_abs)
            scratch_par_abs = os.path.dirname(tgt_abs)
            # F0-b: memoized -- `scratch_par_abs` is the TARGET's parent dir,
            # byte-identical argv across every target sharing that parent
            # (the common `rm a.py b.py c.py` shape: 1 extra spawn per
            # target, linear, before this fix). Also collapses with the
            # file-target `probe_dir` call further below, which computes the
            # exact same `-C <parent-dir> rev-parse --show-toplevel` for a
            # non-directory target -- a pre-existing double-spawn on THIS
            # target alone that the shared memo now also absorbs for free.
            rc_s, out_s = _memo_run_git(["-C", scratch_par_abs, "rev-parse", "--show-toplevel"])
            scratch_repo = out_s.strip() if rc_s == 0 else None
            if (
                scratch_repo
                and cur_repo
                and os.path.normcase(scratch_repo) == os.path.normcase(cur_repo)
                and not os.path.islink(tgt_resolved.rstrip("/"))
                and os.path.isdir(os.path.join(scratch_repo, "tasks"))
                and os.path.normcase(scratch_par_abs) == os.path.normcase(os.path.join(scratch_repo, "tasks"))
                and re.match(r"^[a-zA-Z0-9_-]+-scratch$", scratch_bn)
            ):
                continue

            if tgt_is_dir or recursive:
                # Resolve the owning repo from the TARGET when the target is
                # itself a directory, and only fall back to its parent for a
                # file target (where `-C <file>` is not a valid git cwd).
                #
                # Asking the parent unconditionally -- as this did until
                # 2026-07-31 -- silently skipped (`continue`) every target
                # whose parent happened to sit outside a repo, which is the
                # normal shape of a checkout: `~/.claude`, `~/X/DoE-claude`,
                # and `~/X/claude-klabauter` were all ALLOWED because `~` and
                # `~/X` are not repos, while their SUBdirectories were
                # correctly denied. The dirty-work protection existed and
                # simply never ran on the paths with the most to lose.
                #
                # For a directory target, `tgt_top` was already resolved by
                # the root-deny branch above via this exact `-C <dir>
                # rev-parse --show-toplevel` call -- reuse it rather than
                # spawning git a second time with identical args. A bare
                # repo target never reaches here (denied above), so no
                # bare-repo handling is needed on this leg.
                if tgt_is_dir:
                    root = tgt_top
                else:
                    probe_dir = os.path.dirname(tgt_abs)
                    # F0-b: memoized -- `probe_dir` is `dirname(tgt_abs)`,
                    # identical to `scratch_par_abs` above for THIS target
                    # (same computation) and to every other file target
                    # sharing this parent dir; routed through the same memo
                    # rather than spawning a second/duplicate `git` call.
                    rc_r, out_r = _memo_run_git(["-C", probe_dir, "rev-parse", "--show-toplevel"])
                    root = out_r.strip() if rc_r == 0 else ""
                if not root:
                    continue
                # NOT memoized: the pathspec argument is `tgt_abs`, which by
                # definition differs across targets (two rm targets naming
                # the same path is a degenerate case, not the shape this
                # audit is scoped to) -- genuinely loop-VARIANT, unlike the
                # calls above.
                rc_st, out_st = _run_git(
                    ["-C", root, "--no-optional-locks", "status", "--porcelain", "--", tgt_abs]
                )
                status = "\n".join(out_st.splitlines()[:9])
                if status:
                    peer_sid = _rm_peer_claim_of(tgt_abs, root)
                    if peer_sid:
                        return _deny(
                            "BLOCKED (not overridable): '%s' holds "
                            "untracked/uncommitted work claimed by LIVE peer "
                            "session %s in this shared worktree. "
                            "Untracked files are byte-indistinguishable from "
                            "your own and git CANNOT recover them. If you "
                            "believe it is stale, confirm with the peer or "
                            "wait for their handoff — do not delete a live "
                            "peer's uncommitted work." % (tgt, peer_sid)
                        )
                    if rm_override:
                        continue
                    disp = "\n".join(status.splitlines()[:8])
                    more = "\n  ... and more (first 8 shown)" if len(status.splitlines()) > 8 else ""
                    return _deny(
                        "BLOCKED: 'rm' on '%s' would destroy uncommitted/"
                        "untracked work that git CANNOT recover (untracked "
                        "files and unstaged edits live in no commit, no "
                        "stash, no reflog):\n%s%s\n\n"
                        "Before overriding, re-derive what would actually be "
                        "lost (do not trust a remembered or narrated state):\n"
                        '  git -C "%s" status --porcelain -- "%s"\n\n'
                        "To preserve the work first, stash it (includes "
                        "untracked; restore later with stash pop):\n"
                        '  git -C "%s" stash push -u -- "%s"   '
                        '# -u includes untracked; restore later: git -C '
                        '"%s" stash pop\n\n'
                        "Reserve irreversible deletion for genuinely "
                        "disposable, self-authored, uncontested paths."
                        % (tgt, disp, more, root, tgt_abs, root, tgt_abs, root)
                    )

    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too.
    for payload in _shell_c_unwrap_payloads(cmd):
        result = check_destructive_rm(payload, session_id)
        if result is not None:
            return result
    return None


def _touch_event_path_no_import(line: str) -> str:
    """Local, import-free copy of `scope.parse_touch_event`'s path-field
    extraction only (verb/timestamp are irrelevant to the CLAIMED-membership
    fallback that calls this -- every survivor is treated as claimed either
    way). Exists so `_rm_peer_claim_of`'s outermost fallback (``scope``
    itself unimportable) needs no import of the very module that failed to
    import -- see Review: code-reviewer Finding 1 on the caller.
    """
    stripped = line.rstrip("\n")
    parts = stripped.split(None, 2)
    if len(parts) == 1:
        return parts[0]
    if len(parts) != 3:
        return stripped
    verb, ts_str, path = parts
    if verb not in ("T", "R"):
        return stripped
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return stripped
    if ts.tzinfo is None:
        return stripped
    return path


def _rm_peer_claim_of(tgt_abs: str, root: str) -> str:
    """Port of bash's `_rm_peer_claim_of` / `_gr_peer_claim_of` (identical
    trees in both source hooks; this one function serves both
    check_destructive_rm and check_destructive_git_revert, mirroring the
    bash "_gr_ names copied from _rm_ so this hook is self-contained"
    duplication as a single shared implementation instead).

    Liveness order (Q24 -- CLOSED, see module docstring "CLOSED PORTING
    GAPS"): for each candidate peer session dir under
    ``<root>/.git/coordinator-sessions/*``, canonical liveness
    (``coordinator_core.session.liveness.live_session_ids``) is consulted
    FIRST, per-sid:
      - If the sid has a ``meta.json`` file, it is within canonical's scan
        scope ("covered"). A covered sid is treated as a live peer-claim iff
        it is a member of ``live_session_ids(cwd=root)`` -- regardless of its
        touched.txt mtime (a live-but-idle peer, e.g. mtime > 30 minutes
        stale, is STILL treated as contested). A covered sid absent from the
        live-set is dead -> not contested, and is skipped entirely (no mtime
        fallback for a sid canonical has already ruled on).
      - If the sid has NO ``meta.json``, it is outside canonical's scan scope
        (not "known dead" -- simply unseen), and degrades to the 30-minute
        ``touched.txt`` mtime backstop exactly as before.
    A ``live_session_ids`` call that raises (fail-safe, mirrors the bash
    subshell-isolated failure handling) degrades every sid to the mtime
    backstop, matching the bash "empty live-set -> everything degrades"
    fail-safe posture.

    Self-identity exclusion (2026-07-16 -- CLOSED, bug-backlog
    2026-07-16-dispatch-checks-py-peer-claim-guard-omit-811b3291dd8a.yaml):
    ``cur_sid`` is resolved via the canonical
    ``coordinator_core.session.core.resolve_session_id(cwd=root)`` -- the
    same 4-tier chain bash's ``cs_resolve_session_id`` walks -- and a sid
    equal to ``cur_sid`` is skipped in the per-sid scan below (a session
    never contests its own claim). Mirrors the bash
    ``[[ -n "$cur_sid" && "$sid" == "$cur_sid" ]] && continue`` guard
    exactly: the skip only fires when ``cur_sid`` is non-empty, so an
    unresolvable/ambiguous identity (``resolve_session_id`` returns ``""``
    on Tier-4 ambiguity -- see its own docstring) degrades to "exclude
    nothing" rather than "exclude everything" -- a genuine live peer stays
    contested, never a false-ALLOW.
    """
    sess_dir = os.path.join(root, ".git", "coordinator-sessions")
    if not os.path.isdir(sess_dir):
        return ""
    try:
        tgt_rel = os.path.relpath(tgt_abs, root).replace(os.sep, "/")
    except ValueError:
        return ""
    if not tgt_rel:
        return ""

    # Review: code-reviewer (Finding 3) -- lazy, per-call import. An
    # ImportError here (or any other exception) degrades identically to the
    # pre-existing "unresolvable identity" path: cur_sid = "" -> the
    # self-exclusion guard below simply does not fire (never widens to
    # "exclude everything"), so a genuine live peer stays contested.
    try:
        from coordinator_core.session.core import resolve_session_id
        cur_sid = resolve_session_id(cwd=root)
    except Exception:
        cur_sid = ""

    # Review: code-reviewer (Finding 3) -- lazy, per-call import. An
    # ImportError here degrades identically to a raising `live_session_ids`
    # call: live_ok=False forces every sid through the mtime backstop below
    # (fail-CLOSED for the destructive guard -- never "canonical says dead,
    # allow").
    live_ok = True
    try:
        from coordinator_core.session import liveness
        live_sids = liveness.live_session_ids(cwd=root)
    except Exception:
        live_sids = frozenset()
        live_ok = False

    # Review: code-reviewer (Finding 1, Finding 4) -- hoisted above the
    # per-sid loop (matching the liveness/resolve_session_id imports' actual
    # posture, fixing Finding 4's inaccurate "same posture" comment that used
    # to sit on a per-sid import below). Hoisting also separates the two
    # failure modes Finding 1 flagged as conflated: an ImportError on `scope`
    # itself is caught HERE, once, and degrades every sid to the raw-line
    # fallback below (parse_touch_event unavailable -> can't parse, so fall
    # back to treating every survivor as claimed); a per-sid *projection*
    # failure (`_collect_peer_path_mtimes`/`project_peer_claims` raising on
    # a particular touched.txt) is caught separately per-sid, where
    # `parse_touch_event` -- already imported and known-good here -- is used
    # for the fallback instead of a raw-line match, so the fallback stays
    # fail-CLOSED even once event lines exist (see the per-sid try/except
    # below).
    try:
        from coordinator_core.session.scope import (
            _collect_peer_path_mtimes,
            parse_touch_event,
            project_peer_claims,
        )
        scope_ok = True
    except Exception:
        scope_ok = False

    # Review: code-reviewer (Finding 4) -- hoisted once per call rather than
    # per-sid (bash re-reads epoch per sid); skew is sub-millisecond across
    # the loop, far below the 30-minute backstop window -- intentional.
    now = time.time()
    try:
        entries = os.listdir(sess_dir)
    except OSError:
        return ""
    for sid in entries:
        if sid in (".archive", ".agents"):
            continue
        if cur_sid and sid == cur_sid:
            continue
        sid_dir = os.path.join(sess_dir, sid)
        if not os.path.isdir(sid_dir):
            continue

        meta_path = os.path.join(sid_dir, "meta.json")
        # Review: code-reviewer (Finding 1) -- a raising live_session_ids call
        # must degrade EVERY sid (covered or not) to the mtime backstop, per
        # this function's own docstring; gating solely on meta.json presence
        # silently reopened a false-allow for covered sids when the try/except
        # above caught an exception.
        canon_covers = live_ok and os.path.isfile(meta_path)
        if canon_covers:
            if sid not in live_sids:
                continue  # canonical says dead -> not contested
            # canonical says live -> contested regardless of mtime; fall
            # through to the touched.txt path-match below (no mtime check).
        else:
            # Degradation backstop: sid unseen by canonical liveness.
            touched = os.path.join(sid_dir, "touched.txt")
            if not os.path.isfile(touched):
                continue
            try:
                mtime = os.path.getmtime(touched)
            except OSError:
                # Same touched.txt race: deleted/rotated out between the
                # isfile check and here -- nothing to stamp a liveness
                # window on, so degrade to unclaimed for this sid.
                continue
            if now - mtime > 1800:
                continue

        touched = os.path.join(sid_dir, "touched.txt")
        if not os.path.isfile(touched):
            continue
        try:
            with open(touched, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            # touched.txt vanished/rotated between the isfile check above
            # and this open (race with the peer session finishing) -- no
            # claim survives to contest, so this sid degrades to unclaimed
            # rather than blocking on a file that no longer exists.
            continue

        # Review: repointed through scope.py's event-log-aware peer
        # projection (P3) rather than a bare-line membership test -- a
        # legacy bare-path line still projects CLAIMED (P1's fail-safe), so
        # this is behaviour-identical on today's corpus (no writer emits
        # T/R events yet); it stops silently under-matching once a writer
        # does. This function is fail-CLOSED for the destructive guard, so
        # a per-sid projection failure here (or `scope` being unimportable
        # at all, per `scope_ok` above) must still contest every survivor --
        # never degrade to "skip this sid" (fail-open). The fallback parses
        # the PATH FIELD out of each raw line via `parse_touch_event` (Review:
        # code-reviewer Finding 1) rather than matching on the raw
        # verb+timestamp+path line itself: once any line carries a verb
        # prefix, `tgt_rel` (a bare repo-relative path) never equals or
        # prefix-matches that raw string, so the old raw-line fallback
        # silently stopped matching real claims -- the fail-OPEN regression
        # this fix closes. `parse_touch_event`'s own fail-safe treats any
        # line it cannot parse as CLAIMED ('T', None, <line>), matching the
        # "unknown time = CLAIMED" posture used throughout `scope.py`.
        nonblank_lines = [ln for ln in lines if ln]
        peer_claimed_paths: set = set()
        if scope_ok:
            try:
                peer_path_mtimes = _collect_peer_path_mtimes(nonblank_lines, root)
                peer_claimed_paths = set(
                    project_peer_claims(nonblank_lines, peer_path_mtimes)
                )
            except Exception:
                peer_claimed_paths = {
                    parse_touch_event(ln)[2] for ln in nonblank_lines
                }
        else:
            # `scope` itself failed to import (see `scope_ok` above) --
            # `parse_touch_event` is not available here, so this uses the
            # local, import-free path-field parser instead, keeping this
            # outermost fallback distinguishable from (and independent of)
            # the per-sid projection-failure fallback above, which DOES have
            # a good `parse_touch_event` to call.
            peer_claimed_paths = {
                _touch_event_path_no_import(ln) for ln in nonblank_lines
            }

        for opath in peer_claimed_paths:
            if opath.startswith(tgt_rel + "/") or tgt_rel.startswith(opath + "/") or opath == tgt_rel:
                return sid
    return ""


# ---------------------------------------------------------------------------
# 4. check_destructive_git_clean -- block-destructive-git-clean.sh
# ---------------------------------------------------------------------------

_GC_LOADBEARING_RE = re.compile(
    r"(^|/)state(/|$)|(^|/)docs/plans(/|$)|(^|/)docs/decisions(/|$)|"
    r"(^|/)docs/wiki(/|$)|(^|/)docs/research(/|$)|"
    r"(^|/)cross-repo/(inbox|archive|outbox)(/|$)|-handoff.*\.md$|"
    r"(^|/)completion(/|$)|review-trail/.*\.json$"
)
_GC_LOADBEARING_PREFIXES = (
    "state", "docs/plans", "docs/decisions", "docs/wiki", "docs/research",
    "cross-repo/inbox", "cross-repo/archive", "cross-repo/outbox",
)
_GC_CLEAN_CMD_RE = re.compile(
    r"^\s*((sudo|command|time|exec|nice|nohup|ionice|timeout|stdbuf|which|type)\s+|"
    r"env\s+(\S+=\S*\s+)*|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(/\S*/)?git(\s+(-C\s+\S+|"
    r"-c\s+\S+|--(git-dir|work-tree|namespace)(=\S+|\s+\S+)|--exec-path(=\S+|\s+\S+)?|"
    r"-p|--paginate|--no-pager|--bare|--no-replace-objects|--literal-pathspecs|"
    r"--glob-pathspecs|--noglob-pathspecs|--icase-pathspecs|--no-optional-locks))*"
    r"\s+clean\b"
)

def _gc_is_clean_segment(seg: str) -> bool:
    prev = None
    s = seg
    while s != prev:
        prev = s
        s = s.lstrip()
        if s.startswith("$("):
            s = s[2:]
        elif s.startswith("("):
            s = s[1:]
        elif s.startswith("{"):
            s = s[1:]
        elif s.startswith("!"):
            s = s[1:]
        elif s.startswith("`"):
            s = s[1:]
        elif s.startswith("\\"):
            s = s[1:]
    probe = s.replace("'", "").replace('"', "")
    # Bundled `-c` (2026-07-29, EM-run second-pass integration, confirmed
    # live): `sh -ic '...'` behaves as `sh -i -c '...'`, so an exact `-c`
    # token requirement missed it -- `sh -ic "rm -rf ..."` / `sh -ic "git
    # clean -fdx"` / `sh -ic "git <revert-verb>..."` all silently sailed
    # through as prose. Widened to `-[a-zA-Z]*c[a-zA-Z]*` (same tolerance as
    # `_BUNDLED_C_FLAG_RE` elsewhere in this file/package).
    if re.match(r"^(bash|sh|zsh|dash)\s+(\S+\s+)*-[a-zA-Z]*c[a-zA-Z]*(\s|$)|^eval(\s|$)", probe):
        return bool(re.search(r"\bgit\b.*\bclean\b", probe))
    firstword = s.split(None, 1)[0] if s.split(None, 1) else ""
    if firstword in _RM_WRAPPER_WORDS:
        return bool(re.search(r"\bgit\b.*\bclean\b", s))
    return bool(_GC_CLEAN_CMD_RE.search(probe))


def _is_loadbearing(path: str, seam_state_root: str = "") -> bool:
    p = path.rstrip("/")
    if not p:
        return False
    if _GC_LOADBEARING_RE.search(p):
        return True
    for prefix in _GC_LOADBEARING_PREFIXES:
        if prefix == p or p.startswith(prefix + "/"):
            return True
    if seam_state_root:
        if p == seam_state_root or p.startswith(seam_state_root + "/"):
            return True
        if seam_state_root.startswith(p + "/"):
            return True
    return False


def check_destructive_git_clean(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)
    cmd = _strip_heredoc_bodies(cmd)

    if not re.search(r"\bgit\b", cmd) or not re.search(r"\bclean\b", cmd):
        return None
    if _override("COORDINATOR_OVERRIDE_GIT_CLEAN"):
        return None

    # W5/C5 (2026-08-19, spawn-count defect): identical to the F0-b shape
    # `_new_git_memo` was built for -- a chained command repeating the exact
    # same `git clean` invocation (`git clean -nd dirA && git clean -nd
    # dirA`) re-ran the oracle once per segment with byte-identical argv.
    # `extra_env` is a fixed `{"LC_ALL": "C"}` on every call in this loop, so
    # it is not part of the memo key -- folding it in would be a no-op that
    # only widens the key for no reason.
    _clean_oracle_memo: Dict[Tuple[str, ...], Tuple[int, str]] = {}

    def _memo_run_clean_oracle(args: List[str]) -> Tuple[int, str]:
        key = tuple(args)
        cached = _clean_oracle_memo.get(key)
        if cached is None:
            cached = _run_git(args, extra_env={"LC_ALL": "C"})
            _clean_oracle_memo[key] = cached
        return cached

    for seg in _split_segments(cmd):
        if not seg.strip():
            continue
        if not _gc_is_clean_segment(seg):
            continue

        c_dir = _extract_git_c_dir(seg)
        after_clean = re.sub(r".*(^|\s)clean(\s|$)", " ", seg, count=1)

        if re.search(r"(^|\s)-[a-zA-Z]*n[a-zA-Z]*(\s|$)|(^|\s)--dry-run(\s|$)", after_clean):
            continue

        oracle_args = ["-c", "color.ui=never"]
        if c_dir:
            oracle_args += ["-C", c_dir]
        oracle_args += ["clean", "-nd"]

        if re.search(r"(^|\s)-[a-zA-Z]*X[a-zA-Z]*(\s|$)", after_clean):
            oracle_args.append("-X")
        elif re.search(r"(^|\s)-[a-zA-Z]*x[a-zA-Z]*(\s|$)", after_clean):
            oracle_args.append("-x")

        for m in re.finditer(r"(-e\s+(\S+)|--exclude=(\S+))", after_clean):
            pat = m.group(2) or m.group(3)
            if pat:
                oracle_args += ["-e", pat]

        pathspecs: List[str] = []
        if re.search(r"(^|\s)--(\s|$)", after_clean):
            tail = re.sub(r".*\s--\s*", "", after_clean, count=1)
            pathspecs = tail.split()

        rc, out = _memo_run_clean_oracle(oracle_args + (["--", *pathspecs] if pathspecs else []))
        if rc == -1:
            _clean_note = operator_override_note(
                "COORDINATOR_OVERRIDE_GIT_CLEAN", payload=payload, git_root=git_root
            )
            return _deny(
                "BLOCKED: git clean safety check timed out (2s) — cannot "
                "confirm no load-bearing files would be lost.\n\n"
                "Use instead:\n"
                "  git clean -nd [your flags]"
                + ("\n\nOr: " + _clean_note if _clean_note else "")
            )
        if rc != 0:
            continue
        if not out.strip():
            continue

        loadbearing: List[str] = []
        for line in out.splitlines():
            if not line.startswith("Would remove "):
                continue
            p = line[len("Would remove "):].rstrip("/")
            if _is_loadbearing(p):
                loadbearing.append(p)

        if not loadbearing:
            continue

        shown_count = len(loadbearing)
        shown = "\n".join("  %s" % p for p in loadbearing[:8])
        more = "\n  ... and %d more (first 8 shown)" % (shown_count - 8) if shown_count > 8 else ""
        _clean_note2 = operator_override_note(
            "COORDINATOR_OVERRIDE_GIT_CLEAN", payload=payload, git_root=git_root
        )
        return _deny(
            (
                "BLOCKED: git clean would delete %d untracked load-bearing "
                "file(s), unrecoverable (no commit/stash/reflog):\n%s%s\n\n"
                "Use instead:\n"
                '  git stash push -u -m "wip" -- <paths>'
                % (shown_count, shown, more)
            )
            + ("\n\nOr: " + _clean_note2 if _clean_note2 else "")
        )

    return None


# ---------------------------------------------------------------------------
# 5. check_destructive_git_revert -- block-destructive-git-revert.sh
# ---------------------------------------------------------------------------

_GR_BASE_RE = (
    r"^\s*((sudo|command|time|exec|nice|nohup|ionice|timeout|stdbuf|which|type)\s+|"
    r"env\s+(\S+=\S*\s+)*|[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(/\S*/)?git(\s+(-C\s+\S+|"
    r"-c\s+\S+|--(git-dir|work-tree|namespace)(=\S+|\s+\S+)|--exec-path(=\S+|\s+\S+)?|"
    r"-p|--paginate|--no-pager|--bare|--no-replace-objects|--literal-pathspecs|"
    r"--glob-pathspecs|--noglob-pathspecs|--icase-pathspecs|--no-optional-locks))*\s+"
)


#: git global options taking a SPACE-SEPARATED value, which must be consumed
#: with their operand when walking argv to the real subcommand. Kept in step
#: with the same options `_GR_BASE_RE` above already enumerates.
_GIT_GLOBAL_OPT_WITH_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)

#: git global options KNOWN to take no operand at all. Closed and small on
#: purpose -- anything absent resolves as "unknown shape" and fails closed.
_GIT_GLOBAL_OPT_NO_ARG_SIMPLE = frozenset(
    {
        "-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects",
        "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
        "--icase-pathspecs", "--no-optional-locks",
    }
)


#: CONSOLIDATED (2026-07-28, Finding 5, nit -- three independently-authored
#: Windows-path-head normalizers, one weaker/undermaintained): this module
#: used to carry its OWN git-only argv0-head-path regex/rewrite, duplicating
#: `block_subagent_destructive_action._normalize_windows_argv0_head_path_
#: with_spaces` (a documented, deliberate divergence -- see that function's
#: own history) rather than reusing it. Confirmed by inspection, NOT
#: assumed, that reuse is safe in THIS direction: this module already
#: imports `_normalize_executable_basename` from `block_subagent_
#: destructive_action` (see the top-of-file import block), and that module
#: imports nothing from this one -- the dependency is one-way, so importing
#: one more name from it introduces no cycle. The two normalizers had
#: already diverged exactly as this finding warned they would: the shared
#: one is git+bash+sh+zsh+python[3]+env+xargs-aware and covers spaced UNC
#: paths (Finding 4 fix, same review), while this module's own copy was
#: git-only and had NOT yet received the UNC-anchor widening -- reusing it
#: here closes that gap for `_command_really_invokes` too, for free.
#:
#: The shared function ADDITIONALLY single-quotes an unquoted spaced path so
#: it lands as ONE `shlex` token (this module's own copy only rewrote
#: separators, leaving a spaced path split across tokens) -- strictly safer
#: for `_command_really_invokes` below, which previously had to document
#: "tolerating" that split because it scans every token position-
#: independent; quoting removes the need to tolerate anything.
def _normalize_windows_git_path_head(cmd: str) -> str:
    """Thin alias over the shared normalizer -- kept as its own name (rather
    than inlining the import at every call site) so this module's own
    `_command_really_invokes` docstring, which explicitly discusses this
    step, keeps a stable name to reference.
    """
    return _normalize_windows_argv0_head_path_with_spaces(cmd)


#: Same argv0-HEAD-position anchor as `_WINDOWS_GIT_PATH_HEAD_RE`, but the
#: leading path component is OPTIONAL and the trailing `git(?:\.exe)?` is
#: the entire match target -- collapses ANY head-position git invocation
#: (bare `git.exe`/`GIT.EXE`, a no-space POSIX path `/usr/bin/git.exe`, or a
#: spaced Windows path `C:\Program Files\Git\bin\git.exe`) down to the bare
#: literal `git`, case-folded. Review: code-reviewer -- Finding 3 (P2,
#: 2026-07-28): `check_destructive_git_revert`'s own verb-resolution
#: (`_gr_is_revert_segment`/`_GR_BASE_RE`) requires a LITERAL `git` token
#: immediately followed by known flags/whitespace -- `_command_really_
#: invokes`'s Windows-exe corroboration (added same diff) is only ever
#: reached from INSIDE the `stash` verb branch, which `_gr_is_revert_segment`
#: never enters for `git.exe`/`GIT.EXE`/a Windows-path git.exe spelling in
#: the first place, since `_GR_BASE_RE` has no `.exe`-suffix or spaced-path
#: awareness at all. Confirmed live (2026-07-28, this fix): pre-fix,
#: `check_destructive_git_revert("git.exe -C <repo> stash")` returned
#: `None` (silent allow) against a repo with real uncommitted peer work --
#: `_command_really_invokes`'s own passing unit tests never exercised the
#: real entrypoint, exactly the "tests pass on an unwired guard" failure
#: mode this session exists to eliminate. Fix: normalize the whole `cmd` to
#: its canonical bare-`git` spelling BEFORE segment-splitting/verb
#: resolution runs, so `_GR_BASE_RE`'s literal `git` match (and every
#: downstream `checkout`/`restore`/`reset`/`stash` verb walk) sees the same
#: text it already handles for a POSIX `git` invocation.
_GIT_EXE_HEAD_TO_BARE_RE = re.compile(
    r"(?P<sep>\A|[;&|\n])(?P<ws>\s*)(?P<q>[\"']?)"
    r"(?:(?:[A-Za-z]:)?[\\/](?:[^\\/\r\n]+?[\\/])*)?git(?:\.exe)?"
    r"(?P=q)(?=[\s;&|]|\Z)",
    re.IGNORECASE,
)


def _normalize_git_exe_head_to_bare(cmd: str) -> str:
    """Collapse a head-position git invocation -- however spelled (bare
    `git.exe`, case-varied `GIT.EXE`/`Git.exe`, a no-space POSIX path, or a
    spaced Windows path) -- down to the canonical bare `git` token, quotes
    and any path prefix dropped. See `_GIT_EXE_HEAD_TO_BARE_RE` for the
    exact-basename anchoring discipline (never substring matching:
    `gitk.exe`, `git-foo.exe`, and `legit.exe` are all left untouched --
    the literal boundary lookahead immediately after `git`/`git.exe` fails
    to match anything else attached to that basename).
    """

    def _rewrite(m: "re.Match[str]") -> str:
        return m.group("sep") + m.group("ws") + "git"

    return _GIT_EXE_HEAD_TO_BARE_RE.sub(_rewrite, cmd)


def _command_really_invokes(cmd: str, subcommand: str) -> bool:
    """True iff ``cmd`` invokes ``git <subcommand>`` at ARGV POSITION anywhere
    in the FULL command text -- the subcommand is the first non-flag token
    following a standalone ``git`` token, not merely a word appearing in the
    text.

    Deliberately evaluated over the whole command rather than per segment.
    `_split_segments` is not quote-aware, so a `|`/`;`/`&` inside a quoted
    operand -- `grep -i "... \\|git <verb>"` -- manufactures fragments
    (`git <verb>"`) that the free-text classifiers read as real invocations
    AND that `shlex` then refuses to tokenize (unterminated quote), so a
    per-segment corroboration would fail closed on precisely the fragments it
    exists to dismiss. `shlex` over the intact command keeps a quoted operand
    as ONE token that can never equal ``git``, which is what makes the
    mention case distinguishable at all.

    Fails CLOSED (returns True -- "treat as an invocation, keep checking")
    when the full command cannot be tokenized: this corroboration step exists
    to remove false positives, never to open a bypass.

    A Windows argv0 path (`C:\\Program Files\\Git\\bin\\git.exe`) is rewritten
    to its forward-slash equivalent via `_normalize_windows_git_path_head`
    BEFORE `shlex.split` runs: POSIX `shlex` treats a bare backslash as an
    escape character and silently eats it, which would otherwise mangle
    `C:\\Program Files\\Git\\bin\\git.exe` into two garbage tokens
    (`C:Program`, `FilesGitbingit.exe` -- the backslashes vanish AND the
    unescaped space still splits the path) with no fragment whose basename
    resolves to `git`. Converting to forward slashes first still leaves the
    unescaped-space split (`C:/Program Files/Git/bin/git.exe` ->
    `['C:/Program', 'Files/Git/bin/git.exe']`), but that is harmless here:
    the second fragment's OWN basename is still `git.exe`, which
    `_normalize_executable_basename` resolves correctly regardless of the
    unrelated garbage token ahead of it.
    """
    cmd = _normalize_windows_git_path_head(cmd)
    # Punctuation-adjacent-token fix (2026-07-29, EM-run second-pass
    # integration, confirmed live): a raw `shlex.split` does NOT separate
    # `;`/`&`/`|` from an immediately-preceding word with no whitespace
    # between them (`git stash;echo done` -> tokens `['git', 'stash;echo',
    # 'done']`... in fact even `{ git -C <dir> stash; }`, produced by this
    # very file's own `"{ %s; }" % cmd` test-shape template, tokenizes
    # `stash;` as ONE token) -- `tok == "stash"` then never matches, this
    # corroboration falls through to the next (nonexistent) `git` candidate,
    # and the sweep-shape `is_sweep_shape and not _command_really_invokes`
    # gate above silently skips a genuine unscoped `git stash`. Switched to
    # `_bt_tokenize_full_command` (this package's shared tokenizer,
    # `punctuation_chars=";&|"`), which always yields `;`/`&`/`|` as their
    # OWN separate tokens regardless of adjacent whitespace, while still
    # respecting POSIX quoting exactly as `shlex.split` did.
    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return True
    n = len(tokens)
    for idx, tok in enumerate(tokens):
        # Windows-shaped executable tokens (`git.exe`, `C:\Program
        # Files\Git\bin\git.exe`) must normalize to the same identity as the
        # POSIX `git` token, or the corroboration this function exists to
        # perform fails OPEN on Windows -- see `_normalize_executable_
        # basename`'s own docstring for why this is exact-basename
        # normalization, never substring matching (`gitk`/`git-foo`/`mygit`
        # are unaffected).
        if _normalize_executable_basename(tok) != "git":
            continue
        # EVERY `git` occurrence is examined, not just the first. A compound
        # command routinely carries an unrelated git invocation ahead of the
        # one under test (`git add -A && git stash`, `git status; git stash
        # -u`); returning on the first occurrence's resolved subcommand would
        # let that earlier invocation mask the real one and wave the sweep
        # through. A non-matching occurrence therefore falls through to the
        # next candidate rather than deciding for the whole command.
        i = idx + 1
        while i < n:
            t = tokens[i]
            if t in _GIT_GLOBAL_OPT_WITH_ARG:
                # Consumes the FOLLOWING token as its value -- not advancing
                # past it resolves the VALUE as the subcommand (`git -C <dir>
                # stash` reading <dir> as the verb).
                i += 2
                continue
            if t.startswith("--") and "=" in t:
                i += 1
                continue
            if t.startswith("-"):
                if t in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                    i += 1
                    continue
                # Unrecognized flag: whether it consumes the next token is
                # unknown, so THIS occurrence's subcommand is unresolvable.
                # Fail CLOSED immediately rather than continuing to a later
                # occurrence -- "might be a real invocation, cannot tell" must
                # not be swept under some other occurrence's clean resolution.
                return True
            if t == subcommand:
                return True
            break
    return False


def _gr_is_revert_segment(seg: str) -> str:
    """Returns matched verb ("checkout"/"restore"/"reset"/"stash") or ""."""
    prev = None
    s = seg
    while s != prev:
        prev = s
        s = s.lstrip()
        if s.startswith("$("):
            s = s[2:]
        elif s.startswith("("):
            s = s[1:]
        elif s.startswith("{"):
            s = s[1:]
        elif s.startswith("!"):
            s = s[1:]
        elif s.startswith("`"):
            s = s[1:]
        elif s.startswith("\\"):
            s = s[1:]
    probe = s.replace("'", "").replace('"', "")

    # Bundled `-c` (2026-07-29, EM-run second-pass integration, confirmed
    # live): `sh -ic '...'` behaves as `sh -i -c '...'`, so an exact `-c`
    # token requirement missed it -- `sh -ic "rm -rf ..."` / `sh -ic "git
    # clean -fdx"` / `sh -ic "git <revert-verb>..."` all silently sailed
    # through as prose. Widened to `-[a-zA-Z]*c[a-zA-Z]*` (same tolerance as
    # `_BUNDLED_C_FLAG_RE` elsewhere in this file/package).
    if re.match(r"^(bash|sh|zsh|dash)\s+(\S+\s+)*-[a-zA-Z]*c[a-zA-Z]*(\s|$)|^eval(\s|$)", probe):
        for verb in ("checkout", "restore", "reset", "stash"):
            if re.search(r"\bgit\b.*\b" + verb + r"\b", probe):
                return verb
        return ""

    firstword = s.split(None, 1)[0] if s.split(None, 1) else ""
    if firstword in _RM_WRAPPER_WORDS:
        for verb in ("checkout", "restore", "reset", "stash"):
            if re.search(r"\bgit\b.*\b" + verb + r"\b", s):
                return verb
        return ""

    for verb in ("checkout", "restore", "reset", "stash"):
        if re.search(_GR_BASE_RE + verb + r"\b", probe):
            return verb
    return ""


def _check_destructive_git_revert_full(
    cmd: str,
    session_id: str = "",
    hook_payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Returns ``(deny_or_none, advisory_or_none)``.

    Split 2026-08-05 (Review: staff-eng, Finding 0 -- CONFINEMENT_DENY
    chain-shadowing): the advisory floor below must NOT return from the
    same function `dispatch.py` registers as a hard-deny at chain position
    5 -- `evaluate_payload_json` returns on the first non-None envelope, so
    an advisory returned there would short-circuit every hard-deny guard
    registered after it (`block-stash-destruction`,
    `block-subagent-stash-creation`, `block-subagent-destructive-action`,
    `block-subagent-commit`, ...), which `GuardBand`'s own docstring
    forbids for CONFINEMENT_DENY ("every entry in this band emits either a
    deny or silent None"). `check_destructive_git_revert` (the registered
    hard-deny) and `check_destructive_git_revert_advisory` (a separate,
    later-registered ADVISORY_REWRITE leg) are both thin wrappers over this
    function.

    Deliberately NOT `functools.lru_cache`d at this level -- module-scope
    memoization would leak stale `_run_git` mock results across unrelated
    test cases that happen to share a `cmd` string in the same interpreter.
    Instead `dispatch.py`'s `_build_guard_chain` memoizes this function's
    result for the lifetime of a single dispatch call (both legs share one
    call, one process-per-invocation), so a dispatch pass that evaluates
    both the hard-deny and advisory legs never re-spawns the underlying
    `git status`/`git rev-parse` calls -- this guard sits on the PreToolUse
    hot path under an end-to-end invocation budget.
    """
    if not cmd:
        return None, None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)
    cmd = _strip_heredoc_bodies(cmd)
    # Review: code-reviewer -- Finding 3 (P2, 2026-07-28): normalize a
    # head-position Windows-exe/case-varied git spelling to the bare `git`
    # token BEFORE verb resolution -- see `_normalize_git_exe_head_to_bare`
    # docstring for why this was a live entrypoint-level gap distinct from
    # (and not closed by) the `_command_really_invokes` corroboration below.
    cmd = _normalize_git_exe_head_to_bare(cmd)

    if not re.search(r"\bgit\b", cmd):
        return None, None
    if not re.search(r"\bcheckout\b|\brestore\b|\breset\b|\bstash\b", cmd):
        return None, None
    if _override("COORDINATOR_OVERRIDE_GIT_REVERT"):
        return None, None

    # Per-call replacement for the former module-level `_GR_OVERRIDE_HINT`
    # constant (2026-08-13, audience-gated `operator_override_note`
    # migration) -- computed once per call, from THIS call's `hook_payload`/
    # `git_root`, not at import time. `_gr_note` feeds the five direct
    # `operator_override_note` call sites below (all now redundant with this
    # one resolution) and `_gr_hint` mirrors the old constant's own
    # sentence-prefixed shape for its three former call sites, empty when
    # `_gr_note` is empty (see `operator_override_note`'s SPLICE CONTRACT).
    _gr_note = operator_override_note(
        "COORDINATOR_OVERRIDE_GIT_REVERT", payload=hook_payload, git_root=git_root
    )
    _gr_hint = (
        "If this revert is genuinely intended:\n\n" + _gr_note if _gr_note else ""
    )

    # Advisory floor (2026-08-05): `affected` non-empty but `deny_paths`
    # empty (no load-bearing/peer-claimed path in it) previously fell
    # through `if not deny_paths: continue` with ZERO signal -- see
    # cross-repo/inbox/2026-08-05-doe-claude-em-unscoped-stash-has-no-
    # main-loop-guard.md. `pending_advisory` accumulates the MOST
    # destructive such non-blocking envelope seen so far (Review:
    # staff-eng, Finding 11 -- `len(affected)` compared across segments,
    # not first-match-wins; the max-selection costs no extra bytes since
    # only one candidate is ever rendered) while the loop keeps scanning
    # every remaining segment; a deny found on ANY later segment (or in
    # the `_shell_c_unwrap_payloads` rescan below) still wins outright and
    # returns immediately, per this function's own deny-precedence
    # contract. `_pending_advisory_affected` tracks the `len(affected)`
    # the current `pending_advisory` was built from, so a later segment
    # with a larger sweep can replace an earlier, smaller one.
    pending_advisory: Optional[Dict[str, Any]] = None
    _pending_advisory_affected = -1

    # Latency defect (2026-08-05, dispatch-latency-bound corpus): `cmd` is
    # loop-invariant across every segment, but `_command_really_invokes`
    # tokenizes the WHOLE command on every call -- calling it from inside
    # the segment loop below made this function O(segments x len(cmd))
    # instead of O(len(cmd)). Resolved lazily, per verb, keyed by dict
    # membership (a legitimate `_command_really_invokes` result of `False`
    # for a verb is a real value in the dict, distinct from that verb's key
    # never having been computed at all -- `dict.__contains__`, not a
    # sentinel, is what tells the two apart here) rather than hoisted
    # eagerly above the loop: most commands never reach a matching-verb
    # segment at all, and an eager hoist would charge every one of THOSE
    # benign commands a tokenize pass they never needed. A fresh local per
    # call to this function is also correct across the `_shell_c_unwrap_
    # payloads` recursion a few lines below -- that recursion re-enters via
    # `check_destructive_git_revert`, a distinct call with its own stack
    # frame and its own `cmd`, so it gets its own independent memo rather
    # than reusing this one's. Widened 2026-08-05 (Review: staff-eng,
    # Finding 3) from a `stash`-only local to a per-verb cache covering all
    # four -- see the MENTION-vs-INVOCATION gate immediately below the verb
    # resolution for why every verb needs it, not stash alone.
    _verb_invocation_confirmed: Dict[str, bool] = {}

    # F0-b-adjacent spawn-count defect (2026-08-07, spawn-storm sweep D6):
    # the `git status --porcelain` oracle below ran once PER SEGMENT even
    # though `git_cwd` is almost always identical across a chained command
    # (`git checkout . && git reset --hard` hits the checkout branch then
    # the reset branch, both against the same working tree) -- the tree
    # cannot mutate mid-dispatch (a single synchronous hook invocation, no
    # concurrent mutator), so re-running the same-cwd status probe on
    # segment 2..N is pure repeated work, the same shape `_new_git_memo`
    # closes for the reset/orphan checks. NOT routed through that shared
    # `_memo_run_git` closure here: its memo drops `extra_env`, and this
    # oracle depends on `LC_ALL=C` for locale-independent porcelain output
    # -- reusing it verbatim would silently vary this check's behavior
    # under a non-C locale. A small local cache keyed on `git_cwd` alone
    # (the args are invariant across every call site below) preserves that
    # env pin while still collapsing the repeat.
    _status_porcelain_cache: Dict[Optional[str], Tuple[int, str]] = {}

    def _memo_status_porcelain(cwd: Optional[str]) -> Tuple[int, str]:
        if cwd not in _status_porcelain_cache:
            _status_porcelain_cache[cwd] = _run_git(
                ["--no-optional-locks", "status", "--porcelain"],
                cwd=cwd,
                extra_env={"LC_ALL": "C"},
            )
        return _status_porcelain_cache[cwd]

    # Sibling memo for the `rev-parse --show-toplevel` probe a few hundred
    # lines below, same shape as `_status_porcelain_cache` immediately
    # above: `git_cwd` is loop-invariant across every segment sharing one
    # `-C <dir>` (or none), and the repo root cannot change mid-dispatch, so
    # re-resolving it on segment 2..N of a chained revert command is pure
    # repeated work. Unlike the porcelain probe this call carries no
    # `extra_env`, so it is safe to route through `_run_git`'s plain
    # (cwd,)-only shape rather than needing its own env-pinned spawn.
    _toplevel_cache: Dict[Optional[str], Tuple[int, str]] = {}

    def _memo_show_toplevel(cwd: Optional[str]) -> Tuple[int, str]:
        if cwd not in _toplevel_cache:
            _toplevel_cache[cwd] = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
        return _toplevel_cache[cwd]

    for seg in _split_segments(cmd):
        if not seg.strip():
            continue
        verb = _gr_is_revert_segment(seg)
        if not verb:
            continue

        # MENTION-vs-INVOCATION gate (Review: staff-eng, Finding 3 --
        # widened 2026-08-05 from the stash branch alone to all four verbs).
        # `_split_segments` splits on `;`/`&`/`|` WITHOUT quote awareness, so
        # a `|` inside a quoted operand -- e.g. `grep -i "reset --hard\|git
        # checkout ."` -- manufactures a bogus segment (`git checkout ."`)
        # that `_gr_is_revert_segment`'s free-text classifier above reads as
        # a real invocation. Before the advisory floor (2026-08-05) a bogus
        # segment on checkout/restore/reset was inert whenever `_run_git`
        # found nothing dirty, but a dirty tree now turns that same bogus
        # segment into a live advisory -- the identical class of latent bug
        # Finding 3 names for stash, now reachable on the other three verbs
        # too. Corroborate the free-text verdict against argv position --
        # the same anchored-subcommand discipline `block_subagent_
        # destructive_action` adopted for its own mention-vs-invocation
        # false positives -- and skip the segment when it does not hold up.
        if verb not in _verb_invocation_confirmed:
            _verb_invocation_confirmed[verb] = _command_really_invokes(cmd, verb)
        if not _verb_invocation_confirmed[verb]:
            continue

        c_dir = _extract_git_c_dir(seg)
        git_cwd = _orphan_c_cwd(c_dir)
        after = re.sub(r".*(^|\s)" + verb + r"(\s|$)", " ", seg, count=1)

        affected: List[str] = []
        rc = 0
        out = ""

        if verb in ("checkout", "restore"):
            toks = after.split()
            dotspec = False
            seen_dashdash = False
            for tk in toks:
                if tk == "--":
                    seen_dashdash = True
                    continue
                if not tk.startswith("-") or seen_dashdash:
                    if tk.rstrip("/") == ".":
                        dotspec = True
            if dotspec:
                has_staged = bool(re.search(r"(^|\s)(-[a-zA-Z]*S[a-zA-Z]*|--staged)(\s|$)", after))
                has_worktree = bool(re.search(r"(^|\s)(-[a-zA-Z]*W[a-zA-Z]*|--worktree)(\s|$)", after))
                if verb == "restore" and has_staged and not has_worktree:
                    continue
                rc, out = _memo_status_porcelain(git_cwd)
                if rc == -1:
                    return _deny(
                        "BLOCKED: 'git %s .' oracle (git status) timed "
                        "out (2 s) — cannot verify the revert is safe.\n\n"
                        "Safe path first — verify what would be discarded, "
                        "then preserve it:\n"
                        "  git status --porcelain\n"
                        '  git stash push -u -m "before-revert" -- <paths>   '
                        "# make it recoverable"
                        % (verb,)
                        + ("\n\nAfter preserving the files, re-run. Or:\n%s" % _gr_hint if _gr_hint else "")
                    ), None
                if rc != 0:
                    continue
                for line in out.splitlines():
                    if not line:
                        continue
                    cols = line[:2]
                    path = line[3:]
                    if " -> " in path:
                        path = path.rsplit(" -> ", 1)[-1]
                    if cols == "??":
                        continue
                    if cols[1:2] != " ":
                        affected.append(path)

        elif verb == "reset":
            if re.search(r"(^|\s)--hard(\s|$)", after):
                rc, out = _memo_status_porcelain(git_cwd)
                if rc == -1:
                    return _deny(
                        "BLOCKED: 'git reset --hard' oracle (git status) "
                        "timed out (2 s) — cannot verify the reset is "
                        "safe.\n\n"
                        "Safe path first — verify what would be discarded, "
                        "then preserve it:\n"
                        "  git status --porcelain\n"
                        '  git stash push -u -m "before-reset" -- <paths>   '
                        "# make it recoverable"
                        + ("\n\nAfter preserving the files, re-run. Or:\n" + _gr_hint if _gr_hint else "")
                    ), None
                if rc != 0:
                    continue
                for line in out.splitlines():
                    if not line:
                        continue
                    cols = line[:2]
                    path = line[3:]
                    if " -> " in path:
                        path = path.rsplit(" -> ", 1)[-1]
                    if cols == "??":
                        continue
                    if cols != "  ":
                        affected.append(path)

        elif verb == "stash":
            # `-u`/`-a` widen a stash to ALSO take untracked files; they do not
            # make it a sweep. A bare `git stash` / `git stash push` already
            # sweeps every tracked modification in the tree -- including every
            # peer session's in-flight edits -- so the write-shape, not the
            # untracked flag, is what this check gates on. Mirrors the
            # subagent-side rule already shipped in
            # `block_subagent_destructive_action` (2026-07-26 "UNSCOPED-STASH
            # GAP CLOSE"): the sweep shapes are bare `stash`, explicit `push`,
            # and the implicit-push flag-only form (`git stash -u`), and a
            # `--`-delimited pathspec is what scopes it back to the caller's
            # own paths.
            has_u = bool(re.search(
                r"(^|\s)(-[a-zA-Z]*u[a-zA-Z]*|--include-untracked|-[a-zA-Z]*a[a-zA-Z]*|--all)(\s|$)",
                after,
            ))
            has_dashdash = bool(re.search(r"(^|\s)--(\s|$)", after))
            after_toks = after.split()
            first_tok = after_toks[0] if after_toks else ""
            # Non-sweep subcommands: read-only (`list`/`show`), a different
            # write shape entirely (`branch`/`create`/`store` -- these write a
            # stash OBJECT without touching the working tree), or already
            # covered by their own dedicated checks (`pop`/`apply`/`drop`/
            # `clear`).
            #
            # `save` is deliberately NOT excluded: `git stash save [<msg>]` is
            # the pre-2.16 deprecated spelling of `git stash push [-m <msg>]`,
            # with identical working-tree effect -- it sweeps every tracked
            # modification exactly as the bare form does. Grouping it with
            # `create`/`store` reads plausible and is wrong.
            is_sweep_shape = first_tok not in (
                "pop", "apply", "drop", "list", "show", "clear",
                "branch", "create", "store",
            )
            # The mention-vs-invocation gate for this segment's verb
            # ("stash") already ran, unconditionally, right after `verb`
            # was resolved above (Review: staff-eng, Finding 3 -- hoisted
            # 2026-08-05 from a sweep-shape-only, stash-only local check to
            # a per-verb gate covering every segment of every verb before
            # any verb-specific branch runs) -- confirmed live 2026-07-28
            # (a `grep -i "stash -u\|git stash"` bogus segment) and by the
            # rows this dispatch adds to `test_check_destructive_git_revert_
            # stash.py` for reset/checkout/restore over the same fixture,
            # so this branch never needs its own local corroboration.
            if is_sweep_shape and not has_dashdash:
                rc, out = _memo_status_porcelain(git_cwd)
                if rc == -1:
                    return _deny(
                        "BLOCKED: 'git stash' oracle (git status) timed "
                        "out (2 s) — cannot verify the sweep is safe.\n\n"
                        "Safe path first — scope the stash to your own "
                        "paths:\n"
                        '  git stash push -u -m "before-stash" -- <paths>'
                        + ("\n\nOr: " + _gr_hint if _gr_hint else "")
                    ), None
                if rc != 0:
                    continue
                for line in out.splitlines():
                    if not line:
                        continue
                    cols = line[:2]
                    path = line[3:]
                    if " -> " in path:
                        path = path.rsplit(" -> ", 1)[-1]
                    if cols == "??":
                        # Untracked -- swept only by the `-u`/`-a` widening.
                        if has_u:
                            affected.append(path)
                        continue
                    # Tracked with any staged or unstaged modification: taken
                    # by EVERY stash write shape, bare `git stash` included.
                    # Collecting only `??` here (the pre-2026-07-28 behavior)
                    # meant a stash sweeping a peer's tracked in-flight edits
                    # found nothing to report and allowed silently.
                    if cols != "  ":
                        affected.append(path)

        if not affected:
            continue

        rc_root, out_root = _memo_show_toplevel(git_cwd)
        repo_root = out_root.strip() if rc_root == 0 else ""

        deny_paths: List[Tuple[str, str]] = []
        for ap in affected:
            lb = _is_loadbearing(ap)
            peer_sid = _rm_peer_claim_of(ap if os.path.isabs(ap) else os.path.join(repo_root, ap), repo_root) if repo_root else ""
            if lb and peer_sid:
                deny_paths.append((ap, "load-bearing, peer-claimed by %s" % peer_sid))
            elif lb:
                deny_paths.append((ap, "load-bearing"))
            elif peer_sid:
                deny_paths.append((ap, "peer-claimed by %s" % peer_sid))

        verb_label = {"reset": "git reset --hard", "stash": "git stash (unscoped)"}.get(verb, "git %s" % verb)
        if verb in ("checkout", "restore"):
            safe_path = (
                "Did you mean to scope this to your own paths?\n"
                "  git checkout -- <your-paths>\n"
                "  git restore -- <your-paths>\n\n"
                "Preserve first (recoverable stash of everything, including "
                "untracked):\n"
                '  git stash push -u -m "before-revert" -- <paths>'
            )
        elif verb == "reset":
            safe_path = (
                "Preserve it first instead of discarding it -- stash, "
                "then reset:\n"
                '  git stash push -u -m "before-reset"\n'
                "  git reset --hard"
            )
        else:
            safe_path = (
                "Scope the stash to your own paths instead of sweeping every "
                "session's uncommitted work:\n"
                '  git stash push -u -m "before-stash" -- <your-paths>'
            )

        if not deny_paths:
            # Nothing in `affected` is load-bearing or peer-claimed -- no
            # deny warranted, but the caller still gets a non-blocking
            # nudge rather than silence. `safe_path` is reused verbatim
            # (never re-authored) per this floor's own message-size
            # discipline; checkout/restore's two-paragraph form is trimmed
            # to its first paragraph only -- the cue-exempt "Did you mean"
            # offer -- because the full two-paragraph form plus the lead
            # sentence clears MESSAGE_PROSE_CAP_BYTES (measured: 258 > 220)
            # where the trimmed form measures 116. `reset`/`stash` keep
            # their full `safe_path` -- both already clear cap intact.
            #
            # The lead sentence is VERB-CONDITIONED because the hazard is
            # not the same one. A stash IS recoverable (`pop`/`apply` are
            # the EM's own restore path -- see `block_stash_destruction.
            # py`'s "WHY DROP/CLEAR AND NOT POP/APPLY"); its hazard is that
            # it sweeps every session's work in a shared tree, not that the
            # content is gone. `reset --hard`/`checkout .` genuinely leave
            # no recovery path -- EXCEPT for a staged entry: its blob is
            # already in the object database and survives (recoverable via
            # `git fsck --lost-found`), unlike an unstaged/untracked one.
            # `reset --hard`'s own `affected` collection (`cols != "  "`)
            # includes staged entries, so its harm text carries the same
            # qualifier the hard-deny message for this verb below now also
            # states (Review: staff-eng, Finding 4; Review: code-reviewer,
            # Finding 1) -- an advisory that overstates is one the next
            # reader discounts.
            if pending_advisory is None or len(affected) > _pending_advisory_affected:
                offer = safe_path.split("\n\n", 1)[0] if verb in ("checkout", "restore") else safe_path
                if verb == "stash":
                    # Finding 12: this exact branch has already confirmed
                    # `repo_root`/`_rm_peer_claim_of` found no peer claim on
                    # any affected path -- "including any peer session's" is
                    # not what was checked here; "unclaimed" is.
                    harm = "sweep %d uncommitted file(s), including any unclaimed peer work"
                elif verb == "reset":
                    harm = (
                        "discard %d uncommitted file(s) (staged ones stay "
                        "recoverable via `git fsck --lost-found`)"
                    )
                else:
                    harm = "discard %d uncommitted file(s) git cannot recover"
                pending_advisory = _advisory(
                    (
                        "ADVISORY: %s would %s.\n\n%s"
                        % (verb_label, harm % len(affected), offer)
                    )
                    + ("\n\nOr: %s" % _gr_note if _gr_note else "")
                )
                _pending_advisory_affected = len(affected)
            continue

        shown_count = len(deny_paths)
        # 3, not the former 8 -- a trim, deliberately not a drop.
        #
        # Every listed path is charged against `MESSAGE_PROSE_CAP_BYTES`:
        # `_message_size._is_diagnostic_echo` excludes what-was-denied prose
        # from exemption, so this message's size tracks the CALLER's path
        # lengths, which are unbounded. Measured on one file: 219 bytes for
        # `state/x.json`, 251 for a realistically-named artifact; twelve
        # files ran 721. No wording change can hold the cap, because the
        # wording was never what breached it.
        #
        # Replacing the list with a bounded reason histogram DOES hold it
        # (measured: 208 worst case at any file count or path length) and is
        # the wrong trade -- 21 tests pin that a denied path is named, and
        # for a peer-claimed path that name is the entire decision-relevant
        # content. The operator can get paths from `git status`; they cannot
        # get "this one belongs to session X" from anywhere else.
        #
        # So: trim to bound the common case, keep the contract, and leave
        # the residual breach visible rather than buying the cap with the
        # message's reason for existing. Closing it properly means the cap
        # counting found-data differently from authored prose, which is
        # `_message_size`'s contract to change, not this call site's.
        _SHOWN_PATH_LIMIT = 3
        shown = "".join("  %s (%s)\n" % (p, r) for p, r in deny_paths[:_SHOWN_PATH_LIMIT])
        # Terse by necessity, not by taste: this trailer is guard-authored
        # prose, so it sits outside the found-data block `_message_size`
        # now discounts (a blank line separates them by construction, and
        # it is not a path entry, so folding it in would disqualify the
        # whole block). The long form spent ~32 bytes of the prose budget
        # restating a limit the reader can see; `+N more` spends 9.
        more = "\n  +%d more" % (shown_count - _SHOWN_PATH_LIMIT) if shown_count > _SHOWN_PATH_LIMIT else ""

        if verb == "reset":
            # `reset`'s own `affected`/`deny_paths` collection (`cols != "  "`)
            # includes staged entries, whose blob survives in the object
            # database (recoverable via `git fsck --lost-found`) unlike an
            # unstaged/untracked one -- this deny text carries the same
            # qualifier the advisory harm text for this verb states above,
            # so the two paths agree on the same hazard class instead of the
            # deny overstating what the advisory just corrected.
            # Offer and override-route are built here rather than reusing the
            # shared `safe_path`/`_GR_OVERRIDE_HINT` pair: this branch is the
            # one under the prose cap by the narrowest margin, and both shared
            # strings spend their bytes on preamble (`_GR_OVERRIDE_HINT` opens
            # with a sentence before naming the variable, where
            # `operator_override_note` is measured tail-exempt).
            deny_reason = (
                "BLOCKED: '%s' discards %d uncommitted file(s). Staged blobs "
                "survive in `git fsck --lost-found`; unstaged and untracked "
                "edits do not:\n%s%s\n\n"
                # "instead" is load-bearing, not phrasing: `_alternative_
                # liveness._CUE_WINDOW_RE` only exempts an indented command
                # block that falls inside a cue window, so dropping the cue
                # word makes the two commands below count as prose and pushes
                # this message back over `MESSAGE_PROSE_CAP_BYTES`.
                "Preserve it first instead:\n"
                '  git stash push -u -m "before-reset"\n'
                "  git reset --hard"
                % (verb_label, shown_count, shown, more)
            ) + ("\n\n%s" % _gr_note if _gr_note else "")
        elif verb == "stash":
            # Verb-conditioned for the same reason the advisory harm text
            # above is: a stash is RECOVERABLE (`pop`/`apply`), so the shared
            # "git CANNOT recover" lead was false on this path -- the same
            # overstatement already corrected one branch up, which the deny
            # had gone on making.
            deny_reason = (
                "BLOCKED: '%s' sweeps %d uncommitted file(s), including work "
                "no session has claimed:\n%s%s\n\n"
                # See the reset branch on why the cue word is load-bearing.
                "Scope it to your own paths instead:\n"
                '  git stash push -u -m "before-stash" -- <your-paths>'
                % (verb_label, shown_count, shown, more)
            ) + ("\n\n%s" % _gr_note if _gr_note else "")
        else:
            deny_reason = (
                "BLOCKED: '%s' discards %d uncommitted file(s) git cannot "
                "recover — no commit, stash, or reflog holds them:\n%s%s\n\n"
                # Both offers need their own cue word: an indented block is
                # exempt only inside a cue window, and the window closes at
                # the next blank line, so the second paragraph does not
                # inherit the first's "Did you mean".
                "Did you mean to scope it?\n"
                "  git checkout -- <your-paths>\n"
                "  git restore -- <your-paths>\n\n"
                "Or preserve everything first instead:\n"
                '  git stash push -u -m "before-revert" -- <paths>'
                % (verb_label, shown_count, shown, more)
            ) + ("\n\n%s" % _gr_note if _gr_note else "")

        return _deny(deny_reason), None

    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too. The rescan
    # can itself return either envelope kind (it calls this same function) --
    # a deny still wins outright and returns immediately; an advisory only
    # fills `pending_advisory` if a segment above did not already produce
    # one, preserving deny-over-advisory precedence across both loops.
    #
    # Review: code-reviewer, Finding 2 -- this is first-found-wins, NOT the
    # size-ranked "most destructive advisory wins" rule the segment loop
    # above applies within a single scan pass (`len(affected)` compared
    # across segments). That ranking is deliberately scoped to one scan
    # pass only: threading `len(affected)` out of the recursive call here
    # would require widening `_check_destructive_git_revert_full`'s 2-tuple
    # return contract, which three call sites depend on (this recursive
    # call, and the two guard entry points below) -- including
    # `dispatch.py`'s per-dispatch-call memoisation closure and the
    # call-graph resolver in `test_override_route_inventory.py`, which
    # specifically pattern-matches this function's `[0]`/`[1]` subscript
    # shape. Not forced here; escalated instead (see review-integrator
    # report). Deny precedence is unaffected either way -- a deny from the
    # rescan still returns immediately, unconditionally, above.
    for payload in _shell_c_unwrap_payloads(cmd):
        deny_result, advisory_result = _check_destructive_git_revert_full(
            payload, session_id, hook_payload, git_root
        )
        if deny_result is not None:
            return deny_result, None
        if pending_advisory is None:
            pending_advisory = advisory_result
    return None, pending_advisory


def check_destructive_git_revert(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Hard-deny leg only -- registered in `dispatch.py`'s CONFINEMENT_DENY
    band. Never returns the advisory half; see
    `check_destructive_git_revert_advisory` and
    `_check_destructive_git_revert_full`'s docstring for why the two are
    split (Review: staff-eng, Finding 0)."""
    deny, _advisory = _check_destructive_git_revert_full(cmd, session_id, payload, git_root)
    return deny


def check_destructive_git_revert_advisory(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Advisory-only leg -- registered in `dispatch.py`'s ADVISORY_REWRITE
    band, after every CONFINEMENT_DENY hard-deny guard and before
    `offer-git-c`'s rewrite, so it can never shadow a hard deny.
    `dispatch.py`'s `_build_guard_chain` memoizes `_check_destructive_git_
    revert_full`'s result per dispatch call, so calling both this and
    `check_destructive_git_revert` in the same pass never re-spawns the
    underlying `git status`/`git rev-parse` calls."""
    _deny, advisory = _check_destructive_git_revert_full(cmd, session_id, payload, git_root)
    return advisory


# ---------------------------------------------------------------------------
# 6. check_blanket_git_add -- block-blanket-git-add.sh
# Hazard-discriminated guard (re-scoped 2026-07-31, correcting an over-broad
# 2026-07-31 widening, ``2d6bca4e``): the guard used to be a pure cwd-guard,
# firing ONLY inside the ~/.claude meta-repo (the origin incident's own
# repo -- 4 cross-contamination commits on work/machine-a/2026-06-14,
# 2026-06-15). That commit removed the scope gate ENTIRELY, on the claim
# that the 2026-06-15 plan
# (DoE-claude docs/plans/2026-06-15-harden-safe-commit-against-sibling-add-all.md
# § E1) recorded the meta-repo scope as an incidental mechanism detail with
# no stated reason the hazard is meta-repo-specific -- that claim was false
# (the plan states directly: "the cwd guard is what makes it a no-op in
# consumer sessions, not the hook's absence") and a code review caught it:
# firing in EVERY repo denies an ordinary `git add -A` in every OSS-consumer
# install this engine ships into, none of which have the concurrent-multi-EM
# traffic that motivated the guard, and none of which have a per-repo
# opt-out an operator would know to reach for.
#
# The corrected shape is neither the original narrow gate (missed
# claude-klabauter, a shared tree this DoE-claude EM commits into directly and
# daily, carrying the identical cross-contamination hazard) nor the
# now-corrected blanket widening (catches every OSS consumer) -- it fires
# only where the concurrent-session hazard is actually possible:
# ``_is_hazard_repo`` below DENIES inside the ~/.claude meta-repo (the
# original scope, unchanged) OR inside any repo this MACHINE's fleet
# registry (``repos.*`` in the machine-local settings-home registry, see
# ``coordinator_core.machine_resolver``) knows about -- an OSS consumer
# install has no such registry entry, so the guard stays inert there. FAILS
# OPEN by construction: any error resolving the discriminator (missing HOME,
# unreadable settings home, malformed registry) degrades to "not a hazard
# repo" (ALLOW), never to a deny -- see ``_is_hazard_repo``'s own docstring.
# ---------------------------------------------------------------------------

def _meta_repo_root() -> Optional[str]:
    """Resolve the ~/.claude meta-repo root path (the guard's original,
    unwidened scope), or ``None`` if it cannot be resolved (e.g. no ``HOME``
    in the environment) -- callers treat ``None`` as "this half of the
    discriminator did not match," never as an error to propagate."""
    try:
        return os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude"))
    except OSError:
        return None


def _hazard_registry_repo_roots() -> List[str]:
    """Every non-empty ``repos.*`` value in the machine-local fleet
    registry (``registry.local.toml`` merged over ``registry.toml``, same
    two-file precedence as ``machine_resolver.registry_get`` -- see that
    module for the registry-directory resolution ladder, including its
    ``MACHINE_LOCAL_REGISTRY_DIR`` test-isolation override). These are the
    fleet sibling repos THIS machine knows about -- an OSS-consumer install
    has no such registry, so this returns ``[]`` there, not a guess.

    Fails open: any exception reading/parsing the registry (missing
    settings home, malformed TOML, an unreadable file) returns ``[]``
    rather than raising -- an unresolvable registry can only narrow what
    counts as a hazard repo, never turn an unrelated command into a deny.
    """
    try:
        from coordinator_core import machine_resolver

        reg_dir = machine_resolver.registry_dir()
        roots: List[str] = []
        for fname in ("registry.local.toml", "registry.toml"):
            flat = machine_resolver.load_flat_registry_file(reg_dir / fname)
            for key, val in flat.items():
                if key.startswith("repos.") and val:
                    roots.append(str(val))
        return roots
    except Exception:  # noqa: BLE001 -- fail-open by design, see docstring
        return []


def _paths_match(a: str, b: str) -> bool:
    """``os.path.samefile`` when both paths exist on disk (matches real
    inodes across symlinks/mount points), falling back to a normalized
    string comparison when either side cannot be ``stat``-ed (a registry
    entry pointing at a path that no longer exists on this machine, etc.).
    Mirrors the pre-widening samefile-with-string-fallback shape this guard
    already used for its ~/.claude comparison."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _is_hazard_repo(git_root: str) -> bool:
    """The hazard discriminator: True iff ``git_root`` is a repo where a
    blanket ``git add -A`` can plausibly sweep a CONCURRENT session's
    in-flight edits into this commit -- the ~/.claude meta-repo (the origin
    incident's own repo), or any repo this machine's fleet registry
    (``repos.*``) tracks (claude-klabauter, DoE-claude, and every other
    sibling repo on this machine -- see ``_hazard_registry_repo_roots``).
    An arbitrary OSS-consumer repo has neither property, so this returns
    False there and the guard stays a no-op -- restoring the load-bearing
    percolation property the 2026-07-31 all-repo widening (``2d6bca4e``)
    destroyed (see this section's own header comment for the incident).

    FAILS OPEN: wrapped in a blanket ``except Exception`` -- ANY failure
    classifying ``git_root`` (a broken ``HOME``, an unreadable settings
    home, a registry read raising something ``_hazard_registry_repo_roots``
    itself did not anticipate) returns False, i.e. "not a hazard repo," so
    ``check_blanket_git_add`` ALLOWS rather than denies. A guard that denies
    when it cannot classify is exactly the defect this discriminator
    replaces -- unlike the pre-2026-07-31 gate's IMPLICIT fail-open (a
    ``samefile`` ``OSError`` fell through to a plain string-equality
    fallback, never to a deny), this is now an explicit, load-bearing
    contract of the function.
    """
    try:
        meta = _meta_repo_root()
        if meta is not None and _paths_match(git_root, meta):
            return True
        for root in _hazard_registry_repo_roots():
            if _paths_match(git_root, root):
                return True
        return False
    except Exception:  # noqa: BLE001 -- fail-open by design, see docstring
        return False


def _awk_quote_aware_split(cmd: str) -> List[str]:
    """Port of the awk quote-state-persists-across-newlines segment splitter
    in block-blanket-git-add.sh. Splits on unquoted `;`, `&`, `|`."""
    segs: List[str] = []
    seg: List[str] = []
    sq = dq = False
    for ch in cmd:
        if ch == "'" and not dq:
            sq = not sq
            seg.append(ch)
            continue
        if ch == '"' and not sq:
            dq = not dq
            seg.append(ch)
            continue
        if not sq and not dq and ch in (";", "&", "|"):
            segs.append("".join(seg))
            seg = []
            continue
        seg.append(ch)
    segs.append("".join(seg))
    return segs


#: git global options that may sit BETWEEN the `git` token and its
#: subcommand (`git -C <dir> add -A`, `git --git-dir=... add -A`, ...) --
#: same vocabulary `_GR_BASE_RE` above already enumerates for the
#: destructive-git-{checkout,restore,reset,stash} guard's identical
#: subcommand-not-literally-adjacent-to-`git` problem. Example-market-data-repo-em
#: scoped-commit-guard-asymmetry finding (2026-08-03 relay): before this,
#: `git -C <path> add -A` never even reached the per-segment matcher below --
#: the gate at this function's own top (`\bgit\s+add\b`) requires "add"
#: immediately after "git", so ANY global option in between silently
#: allowed the identical blanket sweep this guard exists to catch.
#: Duplicated (not re-derived from `_GR_BASE_RE`, which bundles its OWN
#: wrapper-prefix alternation into the same string) so this guard's gate can
#: compose the option group with its own `_BYPASS_PREFIX` wrapper vocabulary
#: instead.
_GIT_ADD_GLOBAL_OPT_RE = (
    r"(?:-C\s+\S+|-c\s+\S+|--(?:git-dir|work-tree|namespace)(?:=\S+|\s+\S+)|"
    r"--exec-path(?:=\S+)?|-p|--paginate|--no-pager|--bare|--no-replace-objects|"
    r"--literal-pathspecs|--glob-pathspecs|--noglob-pathspecs|--icase-pathspecs|"
    r"--no-optional-locks)"
)
_GIT_ADD_GATE_RE = re.compile(r"\bgit(?:\s+" + _GIT_ADD_GLOBAL_OPT_RE + r")*\s+add\b")
_GIT_ADD_INVOCATION_OPTS_RE = re.compile(
    r"\bgit((?:\s+" + _GIT_ADD_GLOBAL_OPT_RE + r")*)\s+add\b"
)
_GIT_ADD_DASH_C_VALUE_RE = re.compile(r"-C\s+(\S+)")


def _bt_blanket_add_dash_c_cwd(cmd: str) -> str:
    """The cwd ``check_blanket_git_add``'s hazard-repo probe should resolve
    the git root against: the LAST ``-C <dir>`` value preceding an ``add``
    subcommand anywhere in ``cmd``, else the guard process's own cwd
    (unchanged behavior for every shape with no ``-C``).

    Real git chains multiple ``-C`` values (``-C a -C b`` == cwd ``a/b``);
    this follows the SAME "last -C wins" simplification
    ``_bt_git_dash_c_value`` already uses for C7's probe, rather than
    re-deriving chained-cwd resolution a second time. Safe because a
    probe-cwd miss fails OPEN here (``_run_git`` returns non-zero, the
    caller returns ``None`` a few lines below), never denies -- under- or
    over-resolving a chained ``-C`` can only narrow this guard's coverage,
    never turn an unrelated command into a false deny.
    """
    dash_c_val: Optional[str] = None
    for opts_blob in _GIT_ADD_INVOCATION_OPTS_RE.findall(cmd):
        found = _GIT_ADD_DASH_C_VALUE_RE.findall(opts_blob)
        if found:
            dash_c_val = found[-1]
    if not dash_c_val:
        return os.getcwd()
    dash_c_val = dash_c_val.strip("'\"")
    if os.path.isabs(dash_c_val):
        return dash_c_val
    return os.path.normpath(os.path.join(os.getcwd(), dash_c_val))


from coordinator_core.bash_guards._override_log_path import _override_log_path


def check_blanket_git_add(
    cmd: str,
    session_id: str = "",
    hook_payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)

    if not _GIT_ADD_GATE_RE.search(cmd):
        return None

    rc, out = _run_git(["rev-parse", "--show-toplevel"], cwd=_bt_blanket_add_dash_c_cwd(cmd))
    git_root = out.strip() if rc == 0 else ""
    if not git_root:
        return None

    if not _is_hazard_repo(git_root):
        return None

    if _override("COORDINATOR_OVERRIDE_BLANKET_ADD") or _override("_COORDINATOR_SAFE_COMMIT_INTERNAL_BLANKET"):
        try:
            override_log = _override_log_path(git_root, session_id)
            if override_log is None:
                raise OSError("could not resolve an overrides.log path")
            with open(override_log, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(
                    "%s | %s | OVERRIDE-BLANKET-GIT-ADD | %s\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), session_id or "no-session", cmd[:120])
                )
        except OSError as exc:
            # Audit-log write failed -- the override itself still proceeds
            # (this guard is soft/fail-open per dispatch.py's guard_chain),
            # but the override is now unrecorded, so surface it.
            print(
                "block-blanket-git-add: failed to write override audit log "
                "%s: %s" % (override_log, exc),
                file=sys.stderr,
            )
        return None

    matched_cmd = ""
    for seg in _awk_quote_aware_split(cmd):
        if not seg.strip():
            continue
        seg_cmd = re.sub(r'"[^"]*"', " ", seg)
        seg_cmd = re.sub(r"'[^']*'", " ", seg_cmd)
        # BX-13 fix (2026-07-29, confirmed live): `env git add -A` / `nice
        # git add -A` were never recognized (only a bare env-assignment
        # prefix was tolerated, not an `env`/passthrough-wrapper token
        # itself), so a caller could sidestep this guard with a no-op
        # wrapper while still executing the exact same blanket `git add`.
        # Reuses `_BYPASS_PREFIX` (check_no_verify, above) rather than
        # re-deriving the same wrapper-prefix shape a third time.
        #
        # `_GIT_ADD_GLOBAL_OPT_RE` composed in (2026-08-03,
        # example-market-data-repo-em scoped-commit-guard-asymmetry finding):
        # `git -C <dir> add -A` previously never matched here either -- same
        # gap as this function's own top-of-function short-circuit, see
        # `_GIT_ADD_GLOBAL_OPT_RE`'s own docstring.
        if not re.match(
            r"^\s*" + _BYPASS_PREFIX + r"git(?:\s+" + _GIT_ADD_GLOBAL_OPT_RE + r")*\s+add\b",
            seg_cmd,
        ):
            continue

        after = re.sub(
            r".*(^|\s)git(?:\s+" + _GIT_ADD_GLOBAL_OPT_RE + r")*\s+add\s*",
            " ",
            seg,
            count=1,
        )
        after = after.replace('"', "").replace("'", "").replace("\\", "")

        drtoks = after.split()
        past_dd = False
        dryrun_exempt = False
        for drtok in drtoks:
            if drtok == "--":
                past_dd = True
                continue
            if past_dd:
                continue
            if drtok in ("--dry-run", "-n"):
                dryrun_exempt = True
                break
        if dryrun_exempt:
            continue

        past_dd = False
        should_deny = False
        deny_reason = ""
        for tok in after.split():
            if tok == "--":
                past_dd = True
                continue
            if past_dd:
                continue
            if tok in ("--all", "--update"):
                should_deny = True
                deny_reason = tok
                break
            if tok in (".", "./"):
                should_deny = True
                deny_reason = tok
                break
            # `:/` and `:/.` are git's own "magic pathspec" for the top of
            # the working tree -- the identical blast radius as `.`/`-A`,
            # just spelled differently (example-market-data-repo-em
            # scoped-commit-guard-asymmetry finding, 2026-08-03 relay).
            if tok in (":/", ":/."):
                should_deny = True
                deny_reason = tok
                break
            if tok.startswith("-") and not tok.startswith("--"):
                if "/" not in tok and not tok.startswith("./"):
                    flag_chars = tok[1:]
                    if "A" in flag_chars or "u" in flag_chars or "U" in flag_chars:
                        should_deny = True
                        deny_reason = tok
                        break
            # An absolute pathspec that resolves to the repo root itself is
            # `.` written a different way -- same finding as `:/` above. A
            # DEEPER absolute path (a genuinely scoped subdirectory/file) is
            # deliberately NOT matched here, same "root, not a subtree"
            # asymmetry this file's own `_find_is_root_anchor` documents for
            # `check_runaway_find`.
            elif (tok.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", tok)) and git_root:
                norm_tok = os.path.normpath(tok.rstrip("/\\")) or tok
                if _paths_match(norm_tok, git_root):
                    should_deny = True
                    deny_reason = tok
                    break

        if should_deny:
            full_seg_trimmed = seg.lstrip()[:120]
            matched_cmd = "%s [matched flag: %s]" % (full_seg_trimmed, deny_reason)
            break

    if not matched_cmd:
        # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted
        # argument is executed, not inert text -- unwrap and re-scan it too.
        for payload in _shell_c_unwrap_payloads(cmd):
            result = check_blanket_git_add(payload, session_id, hook_payload)
            if result is not None:
                return result
        return None

    _add_note = operator_override_note(
        "COORDINATOR_OVERRIDE_BLANKET_ADD", payload=hook_payload, git_root=git_root
    )
    reason = (
        "BLOCKED: blanket `git add` sweeps in sibling sessions' edits "
        "(SC-DR-014). Matched: %s\n\n"
        "Use instead:\n"
        "  git add -- path/to/file\n"
        "  scoped-git-commit -m <subject> -- path/to/file"
        % (matched_cmd,)
    ) + ("\n\nOr: %s" % _add_note if _add_note else "")
    return _deny(reason)


# ---------------------------------------------------------------------------
# 7. check_runaway_find -- block-runaway-find.sh
# ---------------------------------------------------------------------------

def _find_is_root_anchor(tok: str) -> bool:
    t = _strip_q(tok)
    if re.match(r"^/+$", t):
        return True
    if re.match(r"^[A-Za-z]:[/\\]*$", t):
        return True
    s = t
    while s.endswith("/") and len(s) > 1:
        s = s[:-1]
    if re.match(r"^/[A-Za-z]$", s):
        return True
    if re.match(r"^/mnt/[A-Za-z]$", s):
        return True
    if re.match(r"^/cygdrive/[A-Za-z]$", s):
        return True
    # Bare home-directory anchor -- `find ~ -name x` / `find ~/ -name x`
    # (the trailing-slash strip loop above already reduces `~/` to `~`) is a
    # whole-home-directory scan, the identical runaway shape this guard
    # exists to catch for `/`. A DEEPER anchor (`~/subdir`) is deliberately
    # NOT matched here -- only the bare home token is "the whole tree", same
    # asymmetry as `/mnt/<X>` (root of one drive) vs `/mnt/<X>/subdir` (not
    # caught).
    #
    # NOTE: `$HOME` is deliberately NOT handled here, and never will be --
    # `check_runaway_find`'s own caller-side loop already bails (never
    # guesses) on ANY token containing `$` before this function is even
    # reached (the `if any(c in stripped for c in ("$", "*", "?", "[",
    # "`")): break` line), because `$HOME` is a caller-environment variable
    # this guard cannot resolve without expanding it itself -- and `$HOME`
    # can be legitimately overridden to something that is NOT the real home
    # directory. Adding a literal `"$HOME"` string match here would be dead
    # code (unreachable past that earlier bail) AND would contradict this
    # file's own established never-guess-at-expansion precedent for
    # `$`-prefixed tokens.
    if s == "~":
        return True
    return False


_FIND_WRAPPER_WORDS = {"sudo", "command", "time", "env", "nice", "nohup", "exec", "timeout", "stdbuf"}

_ENV_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
_WRAPPER_WORD_RE = re.compile(r"(" + "|".join(_FIND_WRAPPER_WORDS) + r")\s+")

#: `nice`'s own niceness argument, sitting between `nice` and the real
#: command it wraps -- the bare-numeric form (`nice -19 <cmd>`), the
#: separate-token `-n`/`--adjustment` form (`nice -n 19 <cmd>`), and the
#: attached-value short-flag form (`nice -n19 <cmd>`). Mirrors
#: `dispatch_checks.py`'s own `_BYPASS_WRAPPER_ARG_FLAGS`/`_skip_wrapper_
#: own_argv` and `_sentinel_creation_guard.py`'s `_NICE_BARE_NUMERIC_RE` --
#: same argument shape, same wrapper, ported to this function's index-based
#: (not token-list-based) walk. Consumed as a WHOLE match including its
#: trailing whitespace, symmetric with how `_WRAPPER_WORD_RE` itself
#: consumes the wrapper word's own trailing whitespace.
_NICE_NICENESS_ARG_RE = re.compile(r"(?:-n\s+\d+|--adjustment\s+\d+|-n\d+|-\d+)\s+")


def _skip_leading_env_and_wrappers_idx(buf: str, i: int) -> int:
    """Advance index `i` into `buf`, past leading whitespace, environment-
    variable assignment tokens (`FOO=bar`), and wrapper words
    (`_FIND_WRAPPER_WORDS`), repeating until nothing more strips. Index-based
    so callers that need a position into the ORIGINAL buffer (not a copied
    suffix) -- e.g. `_offer_awk_parse`'s quote-aware scan -- can use it
    directly. `_strip_leading_env_and_wrappers` is the string-returning
    wrapper for callers that only need the stripped remainder.

    Single shared implementation for what used to be two independently-
    drifting copies of this strip-loop (`_find_is_find_segment` and
    `check_offer_git_c`'s `cd`/`git` detection) -- the divergence between
    those two copies is exactly how a leading `FOO=1 cd ... && git ...`
    evaded `check_offer_git_c` while an equivalent prefix was already
    handled by `_find_is_find_segment`. Don't grow a third copy.
    """
    n = len(buf)
    prev = -1
    while i != prev:
        prev = i
        while i < n and buf[i] in " \t":
            i += 1
        # BRACE-GROUPING FIX (2026-07-29, EM-run second-pass integration,
        # confirmed live): `{ find / -delete; }` was never peeled here,
        # same class as `_seg_has_git_bypass_flag`'s brace-peel fix above --
        # bash requires a space after `{` (a reserved word), so it always
        # surfaces as its own leading token/character run.
        if i < n and buf[i] == "{" and (i + 1 >= n or buf[i + 1] in (" ", "\t")):
            i += 1
            continue
        # PAREN-GROUPING FIX (2026-07-29, EM-run confinement-corpus pass,
        # confirmed live): `( find / -delete )` was never peeled here either
        # -- same class as the brace fix directly above, `(` always
        # surfaces as its own leading token/character run when
        # whitespace-separated (the tested shape), and this function had no
        # tolerance for it.
        if i < n and buf[i] == "(" and (i + 1 >= n or buf[i + 1] in (" ", "\t")):
            i += 1
            continue
        m = _ENV_ASSIGN_RE.match(buf, i)
        if m:
            after = m.end()
            if after < n and buf[after] in (" ", "\t"):
                i = after
                continue
        m2 = _WRAPPER_WORD_RE.match(buf, i)
        if m2:
            i = m2.end()
            # NICE-BARE-NUMERIC FIX (2026-07-29, EM-run confinement-corpus
            # pass, confirmed live -- the Staff Engineer staff-eng review Finding 3):
            # `nice -19 find / -delete` was never recognized as wrapping a
            # `find` invocation -- `_WRAPPER_WORD_RE` only ever consumed the
            # bare word `nice` (plus its trailing whitespace), leaving
            # `-19` sitting at what this function treats as command
            # position; `_find_is_find_segment`'s `^find` anchor then never
            # matched, and the whole-disk `find` sailed through unguarded.
            # This is the single highest-value cell in the corpus: reaching
            # for `nice` is exactly the load-conscious behaviour this guard
            # exists to reward, not punish. Consume nice's own niceness
            # argument here, immediately after its wrapper word, the same
            # way `_skip_wrapper_own_argv` does for the tokenized guards
            # elsewhere in this file.
            if m2.group(1) == "nice":
                m3 = _NICE_NICENESS_ARG_RE.match(buf, i)
                if m3:
                    i = m3.end()
            continue
    return i


def _strip_leading_env_and_wrappers(seg: str) -> str:
    """String-returning wrapper over `_skip_leading_env_and_wrappers_idx` --
    the stripped remainder is always a literal suffix of `seg` (every step
    only drops a prefix), so `seg[len(seg) - len(result):]` recovers the
    exact consumed prefix text when a caller needs it verbatim."""
    return seg[_skip_leading_env_and_wrappers_idx(seg, 0):]


def _find_is_find_segment(seg: str) -> bool:
    s = _strip_leading_env_and_wrappers(seg)
    return bool(re.match(r"^(/\S*/)?find(\s|$)", s))


def check_runaway_find(cmd: str, session_id: str = "") -> Optional[Dict[str, Any]]:
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)

    if not re.search(r"\bfind\b", cmd):
        return None

    find_override = _override("COORDINATOR_ALLOW_FIND_ROOT")

    for seg in _split_segments(cmd):
        if not seg.strip():
            continue
        if not _find_is_find_segment(seg):
            continue

        toks = seg.split()
        anchor_tok = ""
        seen_find = False
        skip_next = False
        for tok in toks:
            if skip_next:
                skip_next = False
                continue
            if not seen_find:
                if tok == "find" or tok.endswith("/find"):
                    seen_find = True
                continue
            if tok in ("-H", "-L", "-P"):
                continue
            if tok == "-D":
                skip_next = True
                continue
            if tok.startswith("-O"):
                continue
            if tok.startswith("-") or tok in ("(", "!", ","):
                break
            stripped = _strip_q(tok)
            if any(c in stripped for c in ("$", "*", "?", "[", "`")):
                break
            if _find_is_root_anchor(tok):
                anchor_tok = stripped
            break

        if anchor_tok:
            if find_override:
                continue
            return _deny(
                "BLOCKED: 'find' anchored at '%s' scans the whole disk — "
                "hangs for tens of CPU-minutes and orphans on exit.\n\n"
                "Use instead:\n"
                "  find . -maxdepth 3 -name '<pattern>'" % anchor_tok
            )

    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too.
    for payload in _shell_c_unwrap_payloads(cmd):
        result = check_runaway_find(payload, session_id)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# 9. check_probe_spray -- nudge-probe-spray.sh (advisory-only, never denies)
# ---------------------------------------------------------------------------

_WINDOW = 90
_THRESHOLD = 3
_COOLDOWN = 30
_RING_N = 8
_RING_RECUR_MIN = 2

_TS_PROBE_RE = re.compile(r"(\$\(date|`date|\$EPOCHSECONDS|\$RANDOM)")
_LEXEME_RE = re.compile(r"(^|[^a-z0-9_])(alive|heartbeat|chan[_-]?ok|still[_-](alive|here|there))([^a-z0-9_]|$)")


def check_probe_spray(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Advisory-only heuristic nudge (never denies). All scratch-state
    read/write around ring_f/times_f/cool_f below deliberately swallows
    OSError without a diagnostic: this is best-effort /tmp bookkeeping for a
    nudge, not a correctness-bearing guard, and it fires on the hot path of
    every Bash tool call -- a stderr warning on every transient /tmp
    permission/race hiccup would spam far more often than it would inform.
    Worst case on a persistent failure is simply "the probe-spray nudge stops
    firing," which is silently self-limiting, not silently dangerous.

    STATE LOCALITY. The counter is keyed by `session_id`, falling back to the
    parent PID when no caller threaded one through. That fallback deliberately
    buckets together everything sharing one parent process -- fine in
    production, where the parent IS the session -- but it makes the state
    process-global for any harness that fans one parent out into workers.
    Under pytest-xdist every worker inherits the SAME parent PID, so all
    workers accumulate into one counter and cross-contaminate: three
    channel-test-shaped commands anywhere in the run tripped the nudge, whose
    `additionalContext` then displaced what an unrelated guard's test was
    asserting on (observed 2026-08-03 as an intermittent two-test failure
    under `-n 6`, ~1 run in 8, always the same pair).
    `COORDINATOR_PROBE_SPRAY_STATE_DIR` redirects the state files so a test
    can own a private counter; `coordinator_core/conftest.py` points it at a
    per-test tmp dir for the whole suite. Production reads no such variable
    and keeps `tempfile.gettempdir()`.
    """
    if not cmd:
        return None
    if _override("COORDINATOR_PROBE_NUDGE_OFF"):
        return None

    now = int(time.time())
    key = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or str(os.getppid()) or "default")
    import tempfile

    state_dir = os.environ.get("COORDINATOR_PROBE_SPRAY_STATE_DIR") or tempfile.gettempdir()
    prefix = os.path.join(state_dir, "coordinator-probe-spray-%s" % key)
    times_f = prefix + ".times"
    ring_f = prefix + ".ring"
    cool_f = prefix + ".cool"

    import hashlib

    h = hashlib.sha256(cmd.encode("utf-8", "replace")).hexdigest()

    in_ring = False
    if os.path.isfile(ring_f):
        try:
            with open(ring_f, "r", encoding="utf-8", errors="replace") as fh:
                ring_lines = fh.read().splitlines()
        except OSError:
            ring_lines = []
        if ring_lines.count(h) >= _RING_RECUR_MIN:
            in_ring = True
    try:
        existing: List[str] = []
        if os.path.isfile(ring_f):
            with open(ring_f, "r", encoding="utf-8", errors="replace") as fh:
                existing = fh.read().splitlines()
        existing.append(h)
        existing = [x for x in existing if x][-_RING_N:]
        with open(ring_f, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(existing) + "\n")
    except OSError:
        # Ring-buffer state is best-effort recurrence tracking for the
        # probe-spray nudge; a write failure just means this command isn't
        # remembered for the next call, not a correctness issue -- silent
        # here to avoid warning on every bash dispatch when it fails.
        pass

    is_probe = False
    if re.match(r'^\s*echo\s+(["\'][A-Za-z0-9][A-Za-z0-9_-]*["\']|[A-Za-z0-9][A-Za-z0-9_-]*)\s*$', cmd):
        is_probe = True
    if re.match(r"^\s*(echo|true|false|:|pwd|date)\s*$", cmd):
        is_probe = True
    if re.match(r"^\s*printf\s", cmd) and not re.search(r"[|><]", cmd) and len(cmd) < 40:
        is_probe = True
    if re.match(r"^\s*sleep\s+[0-9]", cmd):
        is_probe = True
    if in_ring:
        is_probe = True

    is_strong_probe = False
    cmd_lc = cmd.lower()
    if re.match(r"^\s*echo\s", cmd) and _TS_PROBE_RE.search(cmd) and not re.search(r"[|>]", cmd):
        is_strong_probe = True
    if re.match(r"^\s*echo\s", cmd_lc) and _LEXEME_RE.search(cmd_lc) and not re.search(r"[|>]", cmd):
        is_strong_probe = True
    if is_strong_probe:
        is_probe = True

    if not is_probe:
        try:
            open(times_f, "w", encoding="utf-8", newline="\n").close()
        except OSError:
            # Best-effort reset of the probe window on a non-probe command;
            # a stale times_f just means the next probe run over-counts
            # slightly, which the cooldown already tolerates -- not worth
            # a warning on this very-hot path.
            pass
        try:
            if os.path.isfile(cool_f):
                os.remove(cool_f)
        except OSError:
            # Same rationale: best-effort cooldown-file cleanup, not
            # correctness-bearing.
            pass
        return None

    newtimes: List[int] = []
    if os.path.isfile(times_f):
        try:
            with open(times_f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.isdigit() and now - int(line) <= _WINDOW:
                        newtimes.append(int(line))
        except OSError:
            # Unreadable times_f just means the prior window is lost --
            # newtimes falls back to only this call's timestamp below,
            # which under-counts rather than false-triggers the nudge.
            pass
    newtimes.append(now)
    try:
        with open(times_f, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(str(t) for t in newtimes) + "\n")
    except OSError:
        # Best-effort persistence of the probe window; a write failure
        # only affects the next call's count, not this one's.
        pass
    count = len(newtimes)

    effective_threshold = 1 if is_strong_probe else _THRESHOLD

    if count >= effective_threshold:
        last_nudge = 0
        if os.path.isfile(cool_f):
            try:
                with open(cool_f, "r", encoding="utf-8", errors="replace") as fh:
                    v = fh.read().strip()
                    last_nudge = int(v) if v.isdigit() else 0
            except OSError:
                last_nudge = 0
        if now - last_nudge >= _COOLDOWN:
            try:
                with open(cool_f, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(str(now))
            except OSError:
                # Cooldown stamp is best-effort; a write failure only
                # risks re-firing the advisory sooner than _COOLDOWN, not
                # a correctness break -- the advisory below still fires.
                pass
            _ps_note = operator_override_note(
                "COORDINATOR_PROBE_NUDGE_OFF", payload=payload, git_root=git_root
            )
            return _advisory(
                (
                    "PROBE-SPRAY: %d channel-test commands in %ds — the "
                    "channel isn't broken, re-probing diagnoses nothing "
                    "(docs/wiki/tool-output-flakiness-protocol.md).\n\n"
                    "Use instead:\n"
                    "  git -C <path> log -1   # one real command, not "
                    "another probe"
                    % (count, _WINDOW)
                )
                + ("\n\n" + _ps_note if _ps_note else "")
            )

    return None


# ---------------------------------------------------------------------------
# (retired) check_windows_popup -- nudge-windows-console-popup.sh
# Removed 2026-07-15: the advisory fired on bare `python -c` at the Bash-tool
# EXECUTION layer, where the popup is harness-owned (DR-044 popup-a) and NOT
# suppressible by any coordinator-side wrapper -- a pure-bash python-quiet.sh
# cannot set CREATE_NO_WINDOW; it can only swap to pythonw (loses stdout,
# breaks live stdin, breaks pytest-xdist). The genuinely-fixable case
# (Python-spawns-Python) is covered at AUTHORING time by
# write_guards.nudge_windows_subprocess_popup, which offers the correct portable form
# creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0). This execution-layer
# nag only mis-pointed the fixable case at a bash wrapper and nagged the
# unfixable one. See DR-054 (2026-07-15) for the reversal rationale.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 11. check_validate_commit -- validate-commit.sh
# FAIL-OPEN on every standalone error path (non-git-repo, absent/erroring
# bin/ delegate, unparseable subject) -- the dispatcher's F1 harness must
# NOT route this through the crash-deny path (recipe Sec(c) phase 3).
# ---------------------------------------------------------------------------

def _extract_commit_subject(command: str) -> str:
    """Port of Check 8's subject-extraction sed pipeline (validate-commit.sh,
    DoE e91827a7, 2026-07-20): pull the ``-m "<subject>"`` / ``-m '<subject>'`` payload out
    of a ``git commit`` command line.

    Bash's ``sed -n '...p' | head -1`` is a PER-LINE substitution (no
    ``N``/multi-line join) followed by taking the FIRST matching line --
    on a multi-``-m`` command that spans multiple physical lines (subject
    on its own line, body ``-m`` on a later line), bash picks the subject's
    OWN line, not the last ``-m`` anywhere in the command. Within a single
    line, bash's greedy ``.*`` still means the LAST ``-m`` on that line wins.
    Review: code-reviewer (Finding 1) -- a bare `re.findall(...)[-1]` over
    the whole (possibly multi-line) string does not reproduce this: `\\s`
    matches newlines and `[^"']*` spans across them too, so it picks the
    last match in the ENTIRE command (the body), not the first LINE's match
    (the subject). Fixed here by scanning line-by-line and returning the
    first line that has any match (its own last-on-that-line match).

    Falls back to collapsing embedded newlines (heredoc-adjacent commits)
    and retrying whole-string, matching the bash fallback, only if no
    single line matched on its own; returns ``""`` if neither parse
    succeeds (bash: fail-open, empty SUBJECT).
    """
    for line in command.split("\n"):
        matches = re.findall(r"-m\s*[\"']([^\"']*)[\"']", line)
        if matches:
            return matches[-1]
    collapsed = re.sub(r"\n+", " ", command)
    matches = re.findall(r"-m\s*[\"']([^\"']*)[\"']", collapsed)
    if matches:
        return matches[-1]
    return ""


def _owner_clause_budget_bytes() -> int:
    """Check 5's owner-clause byte budget -- a FRACTION of the shipped guard
    message-size-discipline prose cap (``_message_size.MESSAGE_PROSE_CAP_BYTES``),
    never a hardcoded literal (C3 spec: "read it from the ... budget CONSTANT").
    The owner clause is one ingredient of the larger SCOPE/BLOCKED sentence
    (which also carries the staged path, twice in the strict-deny form), so
    it is capped at a THIRD of the whole-message budget rather than the
    whole budget itself -- leaving headroom for the fixed template text and
    the staged file's own (unbounded) path length.

    Function-local import (not module-top): ``_message_size`` transitively
    imports ``dispatch.py``, which imports THIS module at its own top level
    (``_dc``) -- a module-level import here would deadlock that cycle on a
    fresh interpreter (proven empirically: ``_is_hazard_repo`` unresolved on
    a partially-initialized ``dispatch_checks``). By the time any
    ``check_*`` function actually runs, both modules have long finished
    loading, so deferring the import to call time is safe.
    """
    from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES

    return MESSAGE_PROSE_CAP_BYTES // 3


def _truncate_to_budget(text: str, max_bytes: int) -> str:
    """Byte-safe truncation (never split a multi-byte UTF-8 codepoint) with
    a trailing ellipsis marker when truncated."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "…"


def _owner_liveness_basis(owner_id: str, live_verdicts: Dict[str, Tuple[bool, str, Optional[int]]]) -> Optional[str]:
    """The liveness BASIS (``"stable-pid"``, ``"recency-window"``, ...) C8's
    ``live_session_verdicts()`` seam recorded for ``owner_id``, or ``None``
    when that id has no verdict at all (not a live session dir this call --
    e.g. a sentinel identifier, never a real session id)."""
    verdict = live_verdicts.get(owner_id)
    return verdict[1] if verdict is not None else None


def _format_owner_sentence(
    fact: Optional["OwnerFact"],
    live_verdicts: Dict[str, Tuple[bool, str, Optional[int]]],
) -> str:
    """Render the OPERATOR-FACING owner clause for Check 5's advisory AND its
    strict-mode deny -- ONE formatter so the two renderings cannot drift
    (AC10). Takes an ``OwnerFact`` (``coordinator_core.session.scope``, C1)
    and renders one of SIX distinct classes (AC6), plus the AC7 CONTESTED
    rendering for an undetermined liveness verdict:

      1. live peer               -- claim_source "session", liveness "live"
      2. dead peer                -- claim_source "session", liveness "dead"
      3. peer's dispatched agent  -- claim_source "agent" (any liveness)
      4. unresolved agent-race    -- claim_source "agent-race"
      5. claims-unreadable        -- claim_source "unreadable"
      6. no-claim-found           -- ``fact is None`` -> "unknown owner"

    Negative-spec: the word "orphan" must appear in NO string this function
    returns -- that is the machine TOKEN's vocabulary (see
    ``_format_owner_token``), not this operator sentence's. The agent-race
    and claims-unreadable classes must NEVER collapse into class 6's
    "unknown owner" -- both are a positive, recent, on-disk claim (just not
    yet resolvable to a session id), and re-rendering either as an absence
    of evidence would reproduce this plan's own defect in a new spelling.
    Class 6 alone states outright that it is NOT a safety verdict.
    """
    if fact is None:
        return _truncate_to_budget(
            "unknown owner (no claim found -- NOT a safety verdict)",
            _owner_clause_budget_bytes(),
        )

    if fact.claim_source == "agent-race":
        sentence = (
            "an in-flight dispatched agent (%s) not yet attributed to an "
            "owning session -- CONTESTED: agent-race, unresolved"
            % fact.owner
        )
        return _truncate_to_budget(sentence, _owner_clause_budget_bytes())

    if fact.claim_source == "unreadable":
        sibling = fact.owner if fact.owner and fact.owner != ".agents" else None
        sentence = (
            ("sibling %s" % sibling if sibling else "an unresolved sibling")
            + " (its claim record is unreadable this call)"
        )
        return _truncate_to_budget(sentence, _owner_clause_budget_bytes())

    # claim_source in ("session", "agent") from here down.
    subject = (
        "session %s" % fact.owner
        if fact.claim_source == "session"
        else "session %s's dispatched agent" % fact.owner
    )
    if fact.liveness == "live":
        basis = _owner_liveness_basis(fact.owner, live_verdicts)
        sentence = "%s (confirmed live%s)" % (
            subject,
            " via %s" % basis if basis else "",
        )
    elif fact.liveness == "dead":
        sentence = "%s (no longer live)" % subject
    else:
        sentence = "%s (liveness undetermined this call -- CONTESTED)" % subject
    return _truncate_to_budget(sentence, _owner_clause_budget_bytes())


def _format_owner_token(fact: Optional["OwnerFact"]) -> str:
    """Render the MACHINE-STABLE ``owner:`` token for ``scope-warnings.log``
    -- a stable vocabulary distinct from ``_format_owner_sentence``'s prose
    (AC10 split), because the log column has a documented sibling-repo
    consumer.

    NEGATIVE SPEC: the no-claim-found token stays the literal ``"orphan"`` --
    do NOT change it. A cross-repo answer on this column's vocabulary is
    still pending, and ``test_check_validate_commit.py:100`` pins
    ``owner:orphan`` literally for exactly this case; changing it breaks a
    documented sibling-repo surface.
    """
    if fact is None:
        return "orphan"
    if fact.claim_source in ("session", "agent"):
        return "session %s" % fact.owner
    if fact.claim_source == "agent-race":
        return "agent-race:%s" % fact.owner
    if fact.claim_source == "unreadable":
        return "unreadable:%s" % fact.owner
    return "orphan"


_TESTS_FIXTURE_SEGMENT_RE = re.compile(r"(^|/)tests/fixtures/")
_SETTINGS_JSON_RE = re.compile(r"(^|/)settings\.json$")


def _is_settings_json(rel_path: str) -> bool:
    """Does ``rel_path`` name a settings.json Check 11 (machine-path-leak) scans?

    Every one of them, fixtures included. Which findings a fixture is allowed
    to suppress is `_fixture_suppressible_detail`'s question, not this one --
    see there for why the two were split.
    """
    return bool(_SETTINGS_JSON_RE.search(rel_path))


def _fixture_suppressible_detail(rel_path: str, detail: str) -> bool:
    """Is ``detail`` the one finding a fixture path is allowed to suppress?

    Only the unparseable-JSON finding, and only under a ``tests/fixtures/``
    tree. ``coordinator/tests/fixtures/stranded-claude/F-truncated-json/
    settings.json`` is deliberately malformed
    (``coordinator/bin/tests/test_break_glass.py``) and
    ``commit_tripwires.check_machine_path_leak``'s JSONDecodeError branch
    reports unparseable JSON as a hard violation, so staging that fixture used
    to hard-block every commit in the repo, whoever's pathspec it was.

    Review (code-reviewer, chain review of `abbbac67d`): the original fix
    excluded fixture settings.json from the scan ENTIRELY, which is a wider
    grant than its own justification -- a genuine machine-path leak in a file
    that happens to sit under a fixtures path would silently skip the scan.
    Pathname decides only whether the PARSE failure is tolerable; a real leak
    still blocks wherever it lives. Confirmed against the tracked corpus at the
    time of this change: four tracked settings.json files, one finding, and it
    is this fixture's parse error.
    """
    if not _TESTS_FIXTURE_SEGMENT_RE.search(rel_path):
        return False
    return detail.startswith("ERROR")


def check_validate_commit(
    cmd: str,
    session_id: str = "",
    cwd: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    command = _crlf_strip(cmd) if cmd else ""
    if not command:
        return None
    _cwd = cwd or None

    contains_git_commit = bool(re.match(r"^git\s+commit(\s|$)", command))
    if not contains_git_commit:
        for tok in re.split(r"&&|\|\||;", command):
            tok = tok.lstrip()
            if re.match(r"^git\s+commit(\s|$)", tok):
                contains_git_commit = True
                break
    if not contains_git_commit:
        return None

    rc, staged_out = _run_git(["diff", "--cached", "--name-only"], _cwd)
    staged = [l for l in staged_out.splitlines() if l]
    if not staged:
        return None

    warnings: List[str] = []

    # C4: bare-vs-pathspec distinction for Check 5's warn set below -- a
    # `git commit`, `commit -a`, and `commit -- <paths>` were all being
    # warned against the SAME whole-index `staged` list, so a genuinely
    # scoped `git commit -- <a file this session owns>` warned about a
    # peer's untouched, unrelated staged file it will never actually
    # commit. INTERSECT instead: `_bt_commit_own_pathspec` (a WHICH-paths
    # extractor, distinct from `_bt_commit_has_explicit_pathspec`'s
    # WHETHER-scoped boolean -- see that function's own docstring for why
    # the boolean must never be reused as a suppressor here) resolves the
    # commit segment's own pathspec, then that pathspec is re-run through
    # `git diff --cached --name-only --` to get the actual matched subset
    # (a pathspec may be a directory or glob, not only a literal path).
    #
    # NEGATIVE SPEC: pathspec presence must NEVER suppress this warning.
    # `git commit -- <a live peer's file>` still resolves that peer's file
    # into `commit_scope` below and still warns (AC8a) -- only a peer file
    # NOT named by the commit's own pathspec drops out of the warn set.
    #
    # Fails toward the WHOLE index (never toward silence) on any ambiguity:
    # `_bt_commit_own_pathspec` itself returns `None` for `-a`/`--all`,
    # `--pathspec-from-file`/`--pathspec-file-nul` (not statically
    # knowable), `-i`/`--include` (merges into the index, does not narrow),
    # or no pathspec at all; a failed re-derivation git call below also
    # falls back to the whole index rather than guessing.
    commit_scope: List[str] = staged
    _bt_tokens = _bt_tokenize_full_command(command)
    _commit_seg_tokens: Optional[List[str]] = None
    if _bt_tokens is not None:
        for _seg_tokens, _pipe_before in _bt_segments_from_tokens_with_pipe_flag(_bt_tokens):
            if _seg_tokens and _bt_git_resolved_subcommand(_seg_tokens) == "commit":
                _commit_seg_tokens = _seg_tokens
                break
    if _commit_seg_tokens is not None:
        _own_pathspec = _bt_commit_own_pathspec(_commit_seg_tokens)
        if _own_pathspec is not None:
            _rc_scope, _scoped_out = _run_git(
                ["diff", "--cached", "--name-only", "--", *_own_pathspec], _cwd
            )
            if _rc_scope == 0:
                commit_scope = [l for l in _scoped_out.splitlines() if l]
            # else: extractor ambiguity/failure -> commit_scope stays the
            # whole `staged` list (fail loud, never silent).

    # Staged DELETIONS keep warning unconditionally regardless of pathspec
    # narrowing -- the recorded incident shape. A deletion narrowed OUT of
    # `commit_scope` above (e.g. it sits outside the commit's own pathspec)
    # is re-added here rather than left silently dropped. A failed status
    # probe falls back to the whole index (fail loud), same posture as the
    # pathspec re-derivation above.
    if commit_scope is not staged:
        _rc_status, _status_out = _run_git(
            ["diff", "--cached", "--name-status"], _cwd
        )
        if _rc_status != 0:
            commit_scope = staged
        else:
            _scope_set = set(commit_scope)
            for _line in _status_out.splitlines():
                if not _line or not _line.startswith("D\t"):
                    continue
                _deleted_path = _line.split("\t", 1)[1]
                if _deleted_path not in _scope_set:
                    _scope_set.add(_deleted_path)
                    commit_scope.append(_deleted_path)

    # --- Check 5: Scoped staging -- warn-only by default (Phase 2). Strict
    # mode (COORDINATOR_SCOPE_STRICT=1) promotes this to a DENY (Phase 5 --
    # the bash's dormant strict-mode branch is now live here; see the module
    # docstring "KNOWN PORTING GAPS", CLOSED entry for this promotion). Reuses
    # compute_scope() -- no separate declared-scope carrier is introduced. ---
    scope_strict = _override("COORDINATOR_SCOPE_STRICT")
    if session_id:
        git_root = _run_git(["rev-parse", "--show-toplevel"], _cwd)[1].strip()
        if git_root:
            sessions_root = os.path.join(git_root, ".git", "coordinator-sessions")
            session_dir = os.path.join(sessions_root, session_id)
            if os.path.isdir(session_dir):
                # Review: code-reviewer (Finding 3) -- lazy, per-call import.
                # AC7 (docs/plans/2026-08-03-check5-owner-attribution-
                # liveness.md): a raising compute_scope() must degrade
                # toward CONTESTED, never toward silence -- silence is
                # fail-OPEN in the direction that plan's Anti-scope
                # prohibits (liveness/scope resolving toward permissiveness
                # and thereby permitting a sweep of a peer's file). So an
                # ImportError/exception here does NOT skip the warn loop
                # below; `compute_scope_raised` routes every staged file not
                # already in scope through the "claims-unreadable" rendering
                # (see `_format_owner_sentence`'s ``unreadable`` branch --
                # reused as-is, not a new rendering path). It is still
                # warn-only, never promoted to a strict-mode deny (see the
                # `compute_scope_raised` guard on the deny branch below): if
                # compute_scope() itself is broken, denying every staged
                # file in strict mode would turn one exception into a total
                # commit outage repo-wide, a tradeoff this plan does not
                # resolve -- left for a PM ruling, not decided here.
                try:
                    from coordinator_core.session.scope import compute_scope
                    scope_result = compute_scope(session_id, _cwd)
                    compute_scope_raised = False
                except Exception:
                    scope_result = None
                    compute_scope_raised = True
                my_scope = set(scope_result.my_scope) if scope_result is not None else None

                # AC12 two-consumer split: compute_scope() narrows my_scope
                # so that an uncontested dirty file freshly touched during
                # THIS session -- picked up only by the Step-2 mtime
                # fallback -- moves OUT of my_scope and INTO orphans instead
                # of staying allow-listed. safe_commit_offer wants that
                # narrowing; Check 5 here is a different consumer (an
                # allow-list test, not a claimant test), so we forgive
                # exactly that mtime-fresh subset of orphans, re-deriving
                # the same mtime >= started_at test compute_scope() itself
                # uses, rather than reusing scope.py's own predicate directly
                # (C1 landed a `ScopeResult.attribution` sidecar since this
                # comment was first written, so "owned by a peer chunk and
                # cannot be touched here" is no longer literally true of
                # scope.py itself -- but C3's OWN surface is dispatch_checks.py
                # only, so the re-derivation below stays as-is rather than
                # moving behind a shared scope.py helper; see this chunk's
                # divergence note). This keeps Check 5's own warn/no-warn and deny/no-deny
                # outcomes unchanged across the narrowing -- EXCEPT in the
                # readable-but-EMPTY started_at corner: compute_scope() gates
                # on `started_at_readable` (empty content still enters its
                # mtime loop with epoch 0, forgiving everything), while this
                # re-derivation gates on truthiness and so forgives nothing
                # in that corner. That divergence is deliberate, not parity
                # debt: compute_scope()'s empty-content widening is arguably
                # a latent bug of its own, and mirroring it here would
                # propagate that bug into a second site. This code's
                # direction fails toward "still warn" (the safe side for an
                # allow-list consumer), never toward silently forgiving a
                # foreign file, so byte-for-byte parity was deliberately not
                # pursued (Review: code-reviewer Finding 2).
                #
                # A STALE orphan (mtime predates this session's
                # started_at, or started_at is unreadable) is a genuinely
                # foreign file with no claim on it at all -- pre-C1a AND
                # post-C1a it always fell straight to Step 5's orphan
                # bucket without ever being a my_scope candidate, so it
                # must keep warning/denying exactly as before. Do NOT
                # "simplify" this to a bare `my_scope |=
                # set(scope_result.orphans)` -- that also forgives the
                # stale/never-claimed population and silently defeats this
                # check for a genuinely foreign file (see
                # test_foreign_unowned_file_warns_orphan).
                #
                # scope.py holds the CANONICAL mtime >= started_at rule
                # (session/scope.py Step 2/4/5); this block re-derives only
                # the comparison predicate (not iso_to_epoch/mtime_epoch,
                # which are reused as-is) rather than importing a shared
                # helper from scope.py -- C3's declared surface is
                # dispatch_checks.py only (see the AC12 divergence note
                # above). If that canonical rule ever changes (e.g. a `>`
                # vs `>=` boundary fix), this block must be updated to match
                # -- no test currently pins the two predicates staying in
                # sync (Review: code-reviewer Finding 4).
                #
                # Wrapped in the same broad try/except as the compute_scope()
                # call above (Review: code-reviewer Finding 1): a non-UTF8
                # started_at raises UnicodeDecodeError (not an OSError), and
                # the iso_to_epoch/mtime_epoch import plus the per-orphan
                # loop had no guard at all. check_validate_commit spans
                # Check 5 through Check 8 in one function body and is
                # registered fail_closed=False, so an uncaught exception
                # anywhere in this block would silently disable Check 8 too.
                # Degrading to "skip forgiveness only" here keeps the
                # failure in the safe direction -- it can never turn into a
                # deny.
                # C7 verification (2026-08-05-touched-sibling-escape-and-
                # suppressed-trailer): C1 narrows session.scope.compute_scope's
                # Step-1 candidate set (an unresolvable touched.txt entry is
                # now DROPPED rather than rescued). Checked against this
                # forgiveness block explicitly, per that chunk's own AC, and
                # found UNAFFECTED on both of the two named cases -- but case
                # (a) has TWO distinct invisibility routes, only one of which
                # is the literal mechanism AC10's own text names (a dropped
                # candidate's `mtime_epoch()` returning 0 for a DELETED path,
                # forgiven-or-not inside the loop immediately below):
                #   (a) a dropped candidate for a path that was DELETED.
                #       Route 1 (never reaches this block at all): a
                #       never-committed file that is staged then deleted
                #       before disk still exists is invisible to Step 2's
                #       dirty scan ENTIRELY -- neither `git diff --name-only
                #       HEAD` nor `git ls-files --others --exclude-standard`
                #       reports a path absent from both HEAD and the
                #       worktree, so it never reaches `scope_result.orphans`
                #       and this block below never sees it to forgive. This
                #       is pre-existing behaviour of Step 2's dirty scan
                #       (session/scope.py), orthogonal to C1's Step-1
                #       narrowing. Pinned by
                #       test_staged_vanished_untracked_file_still_warns_
                #       though_absent_from_orphans.
                #       Route 2 (the literal AC10 mechanism, DOES reach this
                #       block): a file tracked at HEAD, then deleted and
                #       staged, DOES enter `dirty_files`/`scope_result.
                #       orphans` -- `core.mtime_epoch()` of the now-missing
                #       path returns 0, which fails the `>= started_epoch`
                #       comparison in the loop immediately below, so the
                #       orphan is correctly left unforgiven (still warns/
                #       denies). Pinned by
                #       test_tracked_then_deleted_file_hits_mtime_epoch_
                #       zero_in_forgiveness_loop.
                #       Either route, the warn loop a few lines below keys
                #       off `staged_file not in my_scope` (never off
                #       `orphans` membership), so both shapes still warn/
                #       deny exactly as any other foreign staged file would.
                #   (b) an unreadable/empty `started_at` skips this whole
                #       block via the `if _started_at_iso:` guard below,
                #       which predates C1 and does not depend on the size of
                #       the dropped-candidate set -- also orthogonal.
                try:
                    if scope_result is not None and scope_result.orphans:
                        _started_at_path = os.path.join(session_dir, "started_at")
                        try:
                            _started_at_iso = Path(_started_at_path).read_text(
                                encoding="utf-8"
                            ).strip()
                        except OSError:
                            _started_at_iso = ""
                        if _started_at_iso:
                            from coordinator_core.session.core import (
                                iso_to_epoch as _iso_to_epoch,
                                mtime_epoch as _mtime_epoch,
                            )
                            _started_epoch = _iso_to_epoch(_started_at_iso)
                            for _orphan in scope_result.orphans:
                                _orphan_abs = os.path.join(git_root, _orphan)
                                if _mtime_epoch(_orphan_abs) >= _started_epoch:
                                    my_scope.add(_orphan)
                except Exception:
                    pass

                # Union dispatched-agent touched files (broadened mode) --
                # mirrors coordinator-safe-commit's own default-path scope
                # computation (bin/coordinator-safe-commit ~:1304-1318),
                # which is the ACTUAL commit-time oracle this advisory is
                # trying to preview. Without this union, a file edited only
                # by a dispatched subagent (never by an EM-context tool call)
                # is absent from compute_scope()'s my_scope -- compute_scope
                # only reads THIS session's own touched.txt plus mtime, it
                # has no notion of `.agents/<agent_id>/touched.txt` -- so the
                # warning below misattributes the EM's own fan-out output as
                # "orphan" (or, worse, as owned by whichever sibling session
                # happens to also be live), even though the real commit-time
                # helper would have staged it cleanly. Same fail-open posture
                # as the compute_scope() import above: an exception here only
                # narrows what gets unioned in (my_scope stays whatever
                # compute_scope already gave), it can never turn an ALLOW
                # into a wider allowance or a deny.
                if my_scope is not None:
                    try:
                        from coordinator_core.session.claims import my_agent_touched
                        my_scope |= {f for f in my_agent_touched(session_id, "broadened", _cwd) if f}
                    except Exception:
                        pass

                # C3: render Check 5's owner attribution from
                # ScopeResult.attribution (C1's ungated per-path sidecar)
                # rather than a second, local peer-directory scan -- the
                # dropped scan re-read every sibling touched.txt a second
                # time (compute_scope() already did this work in Step 3/3b)
                # and could only ever answer "which live session claims
                # this path", never the dead-peer/agent/agent-race/
                # unreadable distinctions attribution now carries. Liveness
                # BASIS (not just the live/dead verdict) comes from C8's
                # `live_session_verdicts()` seam, fetched once per call
                # (not per candidate) and looked up per owner id.
                attribution: Dict[str, "OwnerFact"] = (
                    scope_result.attribution if scope_result is not None else {}
                )
                try:
                    from coordinator_core.session.liveness import live_session_verdicts
                    live_verdicts = live_session_verdicts(_cwd)
                except Exception:
                    live_verdicts = {}

                # AC7: a raising compute_scope() renders every staged file
                # not already forgiven as "claims-unreadable" -- CONTESTED,
                # via the same `_format_owner_sentence` formatter every
                # other class uses, rather than falling through to
                # `attribution.get(staged_file)` (which would be `None` for
                # every path here, since `attribution` is `{}` on this arm,
                # and render as "unknown owner" -- exactly the collapse AC7
                # forbids).
                _compute_scope_raised_fact: Optional["OwnerFact"] = None
                if compute_scope_raised:
                    from coordinator_core.session.scope import OwnerFact
                    _compute_scope_raised_fact = OwnerFact(
                        owner="", liveness="undetermined", claim_source="unreadable"
                    )

                # Forward instrumentation (sizing object 2026-08-04-forward-
                # instrumentation-for-commit-scope): record ONE JSONL event per
                # commit attempt so a real sweep rate and FP rate become
                # computable. Purely observational -- it consumes the
                # ScopeResult already in hand (no recompute of scope or
                # attribution, no extra git call) and can never reach a
                # verdict: `record_commit_attempt` returns None, raises
                # nothing, and nothing below branches on it. `attribution` is a
                # reporting-only sidecar (session/scope.py `OwnerFact`) and
                # stays one here. Exactly one append per call -- the deny arm
                # records and then returns, so the post-loop record is
                # unreachable on that path.
                _pathspec_scoped = False
                _sweep_all = False
                if _commit_seg_tokens is not None:
                    try:
                        _pathspec_scoped = _bt_commit_has_explicit_pathspec(_commit_seg_tokens)
                        _sweep_all = _bt_commit_has_sweep_all_flag(_commit_seg_tokens)
                    except Exception:
                        pass
                _warned_paths: List[str] = []

                def _record_scope_event(_verdict: str, _warned: List[str]) -> None:
                    try:
                        from coordinator_core.session.commit_scope_events import (
                            record_commit_attempt,
                        )
                        record_commit_attempt(
                            session_id=session_id,
                            session_dir=session_dir,
                            repo_root=git_root,
                            staged=staged,
                            warned_paths=_warned,
                            attribution=attribution,
                            pathspec_scoped=_pathspec_scoped,
                            sweep_all=_sweep_all,
                            verdict=_verdict,
                        )
                    except Exception:
                        # Second fail-open ring around the module's own: an
                        # ImportError here must be as inert as a write failure
                        # inside it (same posture as the scope-warnings.log
                        # write below).
                        pass

                for staged_file in commit_scope:
                    if my_scope is not None and staged_file in my_scope:
                        continue

                    owner_fact = (
                        _compute_scope_raised_fact
                        if compute_scope_raised
                        else attribution.get(staged_file)
                    )
                    owner_sentence = _format_owner_sentence(owner_fact, live_verdicts)
                    owner_token = _format_owner_token(owner_fact)

                    warn_log = os.path.join(session_dir, "scope-warnings.log")
                    warn_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    try:
                        with open(warn_log, "a", encoding="utf-8", newline="\n") as fh:
                            fh.write(
                                "%s | %s | foreign-staged | %s | owner:%s | "
                                "pending-resolution\n"
                                % (warn_ts, session_id, staged_file, owner_token)
                            )
                    except OSError:
                        # scope-warnings.log is a supplementary audit trail;
                        # the actual advisory still reaches the caller via
                        # warnings.append() below regardless of whether this
                        # write succeeds, so a log-write failure here is not
                        # user-visible silence.
                        pass

                    # Warn-only on this arm even in strict mode: promoting a
                    # raising compute_scope() to a deny would turn one
                    # exception into a repo-wide commit outage (every staged
                    # file across every commit would render CONTESTED and
                    # DENY). PM-RULED 2026-08-03: warn-only stands, and
                    # strict mode is not being pursued until something makes
                    # it necessary -- an outage teaches operators to reach
                    # for override keys, which costs more than the sweep it
                    # would prevent. Do NOT "complete" AC7 by deleting the
                    # `not compute_scope_raised` guard below on the grounds
                    # that fail-closed is always safer; that reading is
                    # what this ruling settles against.
                    _warned_paths.append(staged_file)

                    if scope_strict and not compute_scope_raised:
                        _record_scope_event("deny", _warned_paths)
                        return _deny(
                            "BLOCKED (strict scope): %s is staged but not in "
                            "this session's touch list — likely owned by %s.\n\n"
                            "Unstage it (git restore --staged %s) or, if it "
                            "genuinely belongs to this session's work, add it "
                            "to touched.txt first." % (staged_file, owner_sentence, staged_file)
                        )

                    warnings.append(
                        "SCOPE: %s is staged but not in this session's touch "
                        "list — likely owned by %s. Strict mode would block "
                        "this commit." % (staged_file, owner_sentence)
                    )

                # `verdict` here is CHECK 5's own verdict, not the function's
                # final one -- Check 7/8 below can still deny a call recorded
                # as "advisory". See commit_scope_events.py's docstring.
                _record_scope_event(
                    "advisory" if _warned_paths else "none", _warned_paths
                )

    # Check 7: CLAUDE.md char budget -- GOVERNED (fleet-loaded OR audience-
    # manifest-declared) surfaces only, per
    # coordinator_core.claude_md_budget.is_governed_claude_md (claude-klabauter-owned
    # SSOT, unified with DoE's check-claude-md-size.py hook). A bare
    # basename match (the pre-unification shape) fires on ANY file named
    # CLAUDE.md, including a repo-scoped copy that is not fleet-loaded and
    # must not share this budget -- see that module's own docstring and
    # coordinator/docs/wiki/claude-md-surfaces.md.
    #
    # C7b (AC3): governance is now BY AUDIENCE, not only by the CLAUDE.md
    # basename -- a repo's own coordinator/audience-manifest.txt can name a
    # differently-named surface (e.g. em-operating-doctrine.md) and it joins
    # this same check with no further code change here (see
    # coordinator_core.claude_md_budget.load_audience_manifest).
    try:
        from coordinator_core.claude_md_budget import (
            HARD_LIMIT_BYTES as hard_limit,
            SOFT_LIMIT_BYTES as soft_limit,
            is_governed_claude_md,
            load_audience_manifest,
            parse_watermark,
            ratchet_check,
            resolve_ledger_path,
            RatchetWatermarkError,
        )
    except Exception:
        # SSOT unresolvable (should not happen in-process -- this module IS
        # coordinator_core -- but degrade to the pre-unification literal
        # pair + bare basename match rather than crash the whole commit gate
        # on an unrelated import failure).
        hard_limit, soft_limit = 40000, 38000

        def is_governed_claude_md(_path, **_kw):
            return True

        load_audience_manifest = None
        parse_watermark = None
        ratchet_check = None
        resolve_ledger_path = None
        RatchetWatermarkError = Exception

    _repo_root_for_governance = _run_git(["rev-parse", "--show-toplevel"], _cwd)[1].strip()
    _audience_manifest = (
        load_audience_manifest(_repo_root_for_governance)
        if load_audience_manifest is not None and _repo_root_for_governance
        else []
    )
    claude_md_named = [f for f in staged if re.search(r"(^|/)CLAUDE\.md$", f)]
    manifest_named = [f for f in staged if f in _audience_manifest]
    basename_candidates = sorted(set(claude_md_named) | set(manifest_named))
    claudemd_files = [
        cf
        for cf in basename_candidates
        if is_governed_claude_md(
            os.path.join(_repo_root_for_governance, cf) if _repo_root_for_governance else cf,
            repo_root=_repo_root_for_governance or None,
            audience_manifest=_audience_manifest,
        )
    ]

    try:
        from coordinator_core.ops.measure_token_envelope import estimate_tokens
    except Exception:
        estimate_tokens = None  # AC1 token-reporting is best-effort, never load-bearing here

    hard_violation = ""
    soft_names = ""
    _claudemd_blobs = _batch_show_index_blobs(claudemd_files, _cwd)
    for cf in claudemd_files:
        blob = _claudemd_blobs.get(cf)
        if not blob:
            continue
        size = len(blob)
        token_note = ""
        if estimate_tokens is not None:
            token_note = ", ~%d tokens (estimate)" % estimate_tokens(blob)
        if size > hard_limit:
            hard_violation += "\n  %s = %d chars%s (limit %d)" % (cf, size, token_note, hard_limit)
        elif size > soft_limit:
            soft_names += "\n  %s = %d chars%s (soft %d; hard %d)" % (
                cf, size, token_note, soft_limit, hard_limit,
            )

        # C7b (AC4): the per-surface ratchet watermark, read from the same
        # repo-local ledger convention C7a's DoE-resident admission gate
        # uses -- unarmed (no ledger, or no "## Watermark" section) is a
        # silent no-op, never a violation.
        if resolve_ledger_path is not None and _repo_root_for_governance:
            ledger_path = resolve_ledger_path(_repo_root_for_governance, cf)
            try:
                watermark = parse_watermark(ledger_path)
            except RatchetWatermarkError as exc:
                hard_violation += "\n  %s: %s" % (cf, exc)
                continue
            ok, ratchet_msg = ratchet_check(size, watermark)
            if not ok:
                hard_violation += "\n  %s: %s" % (cf, ratchet_msg)

    if soft_names:
        warnings.append(
            "CLAUDEMD-BUDGET (soft):%s\n  → Approaching 40K "
            "perf warning. Demote a section to docs/wiki/ before the next "
            "addition." % soft_names
        )

    if hard_violation and not _override("COORDINATOR_OVERRIDE_CLAUDEMD_BUDGET"):
        _budget_note = operator_override_note(
            "COORDINATOR_OVERRIDE_CLAUDEMD_BUDGET", payload=payload, git_root=_repo_root_for_governance
        )
        reason = (
            "BLOCKED: staged CLAUDE.md exceeds 40K char limit (Claude Code "
            "perf warning threshold):%s\n\n"
            "Trim before committing: demote a section to docs/wiki/ and "
            "replace with a pointer."
            % hard_violation
        ) + ("\n\n" + _budget_note if _budget_note else "")
        return _deny(reason)

    if hard_violation and _override("COORDINATOR_OVERRIDE_CLAUDEMD_BUDGET"):
        warnings.append("CLAUDEMD-BUDGET (override):%s" % hard_violation)

    # --- Check 8: Plan/handoff frontmatter mutation needs commit-subject
    # discipline -- warn-only (strict-mode deny is a bash-only dormant
    # branch, see the module docstring "KNOWN PORTING GAPS"). Uses the
    # bash's own post-migration fallback prefix "state/handoffs" (no Python
    # coordinator_state_root seam exists yet -- see module docstring). ---
    handoffs_prefix = "state/handoffs"
    frontmatter_files = [
        f for f in staged
        if re.match(r"^(tasks/plans|%s|docs/plans)/.*\.md$" % re.escape(handoffs_prefix), f)
    ]
    # Batched replacement for a per-file `git diff --cached -U0 -- <path>`
    # spawn: ONE `git diff --cached -U0 --` call carries every surviving
    # frontmatter path, and the combined patch is split back into per-file
    # sections on its own `diff --git a/<path> b/<path>` header lines (no
    # rename in play here -- every path is a staged, tracked frontmatter
    # file, so the a/- and b/-side names are always identical). The header
    # lookup is an exact-string dict keyed off the KNOWN target paths rather
    # than a regex over the header line, so a path containing regex
    # metacharacters can never mis-tokenize.
    frontmatter_mutations: List[str] = []
    _frontmatter_diff_targets: List[str] = []
    for f in frontmatter_files:
        _f_abs = os.path.join(_cwd, f) if _cwd else f
        if not os.path.isfile(_f_abs):
            continue
        _frontmatter_diff_targets.append(f)

    if _frontmatter_diff_targets:
        # `-c core.quotePath=false` -- without it, git C-quotes a header
        # path containing non-ASCII/control characters (default
        # core.quotePath=true), and the exact-string `_diff_headers` lookup
        # below would silently miss that header line, never flagging the
        # file's frontmatter mutation. Forcing quoting off is safe here
        # because every candidate path is already a known plain string from
        # `_frontmatter_diff_targets`, not something parsed back out of git.
        _rc_d, diff_out = _run_git(
            ["-c", "core.quotePath=false", "diff", "--cached", "-U0", "--", *_frontmatter_diff_targets], _cwd
        )
        if diff_out:
            _diff_headers = {
                "diff --git a/%s b/%s" % (p, p): p for p in _frontmatter_diff_targets
            }
            _current_file: Optional[str] = None
            for line in diff_out.splitlines():
                if line in _diff_headers:
                    _current_file = _diff_headers[line]
                    continue
                if _current_file is None:
                    continue
                if line.startswith("+++") or line.startswith("---"):
                    continue
                if re.match(
                    r"^[+-](status|deployment_state|consumed_by|claimed_by|shipped_in|predecessor|kind):",
                    line,
                ):
                    if _current_file not in frontmatter_mutations:
                        frontmatter_mutations.append(_current_file)

    if frontmatter_mutations:
        subject = _extract_commit_subject(command)
        subject_lc = subject.lower()
        subject_ok = any(
            token in subject_lc
            for token in (
                "status:", "deployment_state:", "consumed_by:", "claimed_by:",
                "shipped_in:", "predecessor:", "kind:", "pickup", "handoff",
                "claim", "unclaim", "consume", "unconsume", "ship", "closed",
                "continued", "abandon", "supersede",
            )
        )
        if not subject_ok:
            warnings.append(
                "FRONTMATTER-MUTATION: staged files modify load-bearing "
                "frontmatter (status/deployment_state/consumed_by/"
                "claimed_by/shipped_in/predecessor/kind) without naming the "
                "mutation in the commit subject:%s\n  → Commit subject "
                "should include the changed key (e.g., 'deployment_state:') "
                "OR a lifecycle verb (pickup/handoff/claim/unclaim/ship/"
                "closed/continued/abandon/supersede). Without this, git log "
                "-- <file> loses the "
                "audit trail. See coordinator/CLAUDE.md § Handoff Lineage. "
                "(heredoc commit subjects may not parse — confirm your "
                "subject names the mutation if you used a heredoc form)"
                % (" " + " ".join(frontmatter_mutations))
            )

    # Checks 9-11 -- ported in-process (coordinator_core.bash_guards.
    # commit_tripwires) as of the Windows de-bash campaign
    # (docs/plans/2026-07-19-debash-coordinator-windows.md). Formerly
    # delegated to bin/*.sh by FILENAME via _delegate_bin_check/
    # _find_bin_script -- a lookup that silently no-ops (guard never fires,
    # no error surfaced) the moment a DoE-side rename moves the target
    # filename, which is exactly what happened to check-machine-path-leak.sh
    # (renamed to .py) and was mid-happening to check-bin-sh-polyglot.sh at
    # port time. See commit_tripwires.py's own module docstring for the full
    # defect writeup and the resolution-mechanism split between Checks 9/10
    # (DoE-plugin-repo-scoped) and Check 11 (target-commit-repo-scoped).
    schema_bump_violation = commit_tripwires.check_schema_version_bump()
    if schema_bump_violation:
        warnings.append("SCHEMA-BUMP-TRIPWIRE:\n%s" % schema_bump_violation)

    bin_sh_polyglot_violation = commit_tripwires.check_bin_sh_polyglot()
    if bin_sh_polyglot_violation:
        warnings.append("BIN-SH-POLYGLOT-TRIPWIRE:\n%s" % bin_sh_polyglot_violation)

    # Check 11 (machine-path-leak) -- hard-block sink for settings.json.
    # Negative spec: a settings.json under a tests/fixtures/ tree is test data,
    # scanned, fixtures included: a fixture path suppresses only the
    # unparseable-JSON finding (F-truncated-json/settings.json is deliberately
    # malformed, and the checker's JSONDecodeError branch flags that as a hard
    # violation, so staging it used to hard-block every commit in the repo).
    # A genuine machine-path leak still blocks wherever the file sits --
    # see `_fixture_suppressible_detail`.
    settings_staged = [f for f in staged if _is_settings_json(f)]
    for sf in settings_staged:
        detail = commit_tripwires.check_machine_path_leak(sf, _cwd)
        if detail and not _fixture_suppressible_detail(sf, detail):
            reason = (
                "BLOCKED: %s contains machine-specific absolute "
                "path(s) that must not be committed.\n\n"
                "%s\n\n"
                "Machine-specific paths must live in gitignored "
                "settings.local.json or machine-local registry, not in "
                "tracked settings.json.\n"
                "See docs/plans/2026-06-23-machine-path-leak-guard.md"
                % (sf, detail)
            )
            return _deny(reason)

    # Check 12 -- registration-quad-completeness -- REGISTRATION-QUAD-INVARIANT.
    # Unlike Checks 9-11, this is a HARD DENY by default (see commit_tripwires.
    # check_registration_quad_completeness's own docstring for the fail-open
    # rationale: an incomplete quad is a data-cost bug, not a style nit),
    # subject to COORDINATOR_OVERRIDE_REGISTRATION_QUAD downgrading it to an
    # advisory, mirroring the CLAUDEMD-BUDGET override branch above.
    registration_quad_violation = commit_tripwires.check_registration_quad_completeness(_cwd)
    if registration_quad_violation:
        if _override("COORDINATOR_OVERRIDE_REGISTRATION_QUAD"):
            warnings.append("REGISTRATION-QUAD-TRIPWIRE (override):\n%s" % registration_quad_violation)
        else:
            _quad_note = operator_override_note(
                "COORDINATOR_OVERRIDE_REGISTRATION_QUAD", payload=payload, git_root=_repo_root_for_governance
            )
            reason = (
                "%s" % registration_quad_violation
            ) + ("\n\n" + _quad_note if _quad_note else "")
            return _deny(reason)

    # Check 13 -- staged-pathspec-divergence -- STAGED-PATHSPEC-DIVERGENCE.
    # Advisory only (never denies) -- see commit_tripwires.
    # check_staged_pathspec_divergence's own docstring + module comment
    # block for the deny-vs-warn call (SC-DR-015). The check does its own
    # override handling/logging internally (COORDINATOR_OVERRIDE_
    # PATHSPEC_DIVERGENCE), unlike Checks 9/10/12 above, so no extra
    # override branch is needed at this call site.
    pathspec_divergence_violation = commit_tripwires.check_staged_pathspec_divergence(
        command, _cwd, session_id, payload=payload
    )
    if pathspec_divergence_violation:
        warnings.append(pathspec_divergence_violation)

    # Single warn-only flush (bash: the "Single warn-only flush" comment near
    # the end of validate-commit.sh). Every warn-only check above (5, 7 soft/
    # override, 8, 9, 10) appends to `warnings`; this is the one sink that
    # surfaces them as an allow+additionalContext advisory instead of a
    # silent-allow `None` (a silent `None` here would have made every
    # warn-only check in this function a no-op -- there is no bash-style
    # stderr channel available to a plain function return).
    if warnings:
        return _advisory("\n\n".join(warnings))

    return None


# ---------------------------------------------------------------------------
# 12-16. BX-16 -- generalising the offer-git-c rewrite seam from cd-over-git
# to bash-over-op. DoE docs/plans/2026-07-29-windows-viability-stop-the-
# spawn-storms.md, row BX-16.
#
# check_offer_git_c (above) is the template: auto-rewrite when a faithful
# translation is derivable, else deny-with-suggestion. Every check below
# reuses that TWO-tier subset of the template (auto-rewrite / advise-with-
# suggestion) and deliberately drops the THIRD (deny) tier: this row's own
# scope is the rewrite VOCABULARY, not Windows deny policy -- that belongs
# to BX-6/BX-7/BX-8's own platform-gated guards, which consume the targets
# named here for their "did you mean" messages (AC-3/AC-4/AC-5). A check in
# this section therefore only ever returns an auto-rewrite (allow +
# updatedInput), an advisory (allow + additionalContext, command
# unchanged), or None -- never `_deny`.
#
# Detection is tokenizer-based throughout (`_shape_classifier.classify_
# command` for the two measured fork-tax shapes this row translates;
# `_command_tokenizer.tokenize_full_command` directly for the two shapes
# outside that classifier's five, sed-range and cat-heredoc) -- never regex
# over raw command text, per the plan's "Never regex for shape detection"
# rule. `_has_banner_marker`-style single-token scans are not needed here
# since none of these five shapes hinge on free-text pattern matching.
# ---------------------------------------------------------------------------

_FIND_EXEC_TRANSLATABLE_VERBS = frozenset({"rm", "cat", "wc"})


def _bt_parse_find_exec_segment(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """`tokens` is one already-tokenized SEGMENT (post
    `segments_from_tokens_with_pipe_flag`) whose first token is a `find`
    invocation. Returns a parsed
    ``{"path", "name_pattern", "only_files", "exec_argv"}`` dict, or `None`
    if this segment carries no `-exec` this function can confidently
    isolate.

    A `;`-terminated `-exec ARGV ;` is the common case, and its terminator
    is NEVER visible inside `tokens` here: `tokenize_full_command` treats
    `;` as an always-separate punctuation token regardless of the shell's
    OWN escaping (`\\;` and bare `;` tokenize identically), so
    `segments_from_tokens_with_pipe_flag` has already consumed it as a
    segment BOUNDARY before this function ever sees the segment -- the
    segment's own end IS the terminator in that case, there is no
    remaining `;` token to search for. Only the `+`-terminated form
    (`-exec ARGV +`) leaves its terminator inside the segment, since `+` is
    not one of the tokenizer's punctuation/separator characters. So: stop
    `exec_argv` at a literal `+` token if one appears, else take the
    segment's own remainder as `exec_argv` (the semicolon-consumed case)."""
    if "-exec" not in tokens:
        return None
    exec_idx = tokens.index("-exec")
    pred = tokens[1:exec_idx]
    path = "."
    i = 0
    if pred and not pred[0].startswith("-"):
        path = pred[0]
        i = 1
    name_pattern: Optional[str] = None
    only_files = False
    while i < len(pred):
        tok = pred[i]
        if tok == "-name" and i + 1 < len(pred):
            name_pattern = pred[i + 1]
            i += 2
            continue
        if tok == "-type" and i + 1 < len(pred):
            only_files = pred[i + 1] == "f"
            i += 2
            continue
        i += 1
    rest = tokens[exec_idx + 1:]
    plus_idx = rest.index("+") if "+" in rest else None
    exec_argv = rest[:plus_idx] if plus_idx is not None else rest
    # Strip a literal trailing ";" for the rare case it DID survive inside
    # the segment (e.g. a quoted `';'` operand -- tokenize_full_command
    # respects quoting, so a quoted semicolon is one ordinary token, not a
    # separator, and would otherwise be mistaken for part of the invoked
    # command's own arguments).
    if exec_argv and exec_argv[-1] == ";":
        exec_argv = exec_argv[:-1]
    if not exec_argv:
        return None
    return {
        "path": path,
        "name_pattern": name_pattern,
        "only_files": only_files,
        "exec_argv": exec_argv,
    }



def _bt_python3_invocation_cache_path() -> str:
    """On-disk location for `_bt_python3_invocation`'s cross-process cache.
    Prefers claude-klabauter's own `state/cache/` (this repo's disk-truth substrate,
    never `~/.claude` -- a plane this repo owns none of, see CLAUDE.md
    § What this repo is); falls back to the OS temp dir if `state/` cannot
    be created (read-only checkout, permissions), matching the fail-open
    discipline `_bt_python3_invocation` itself already promises."""
    import tempfile

    try:
        repo_root = Path(__file__).resolve().parents[2]
        state_dir = repo_root / "state" / "cache"
        state_dir.mkdir(parents=True, exist_ok=True)
        return str(state_dir / "bt-python3-invocation-cache.json")
    except OSError:
        return os.path.join(tempfile.gettempdir(), "coordinator-bt-python3-invocation-cache.json")


def _bt_python3_invocation_cache_key() -> Optional[List[Any]]:
    """Build the cache key this advisory's resolution actually depends on.

    Mirrors `pyresolve._machine_local_get`'s own in-process memo, which keys
    on ``(key, resolved impl path)`` rather than the lookup key alone --
    the impl path already folds in every env var that steers *which*
    `_machine_local.py` gets consulted. This cross-process cache widens that
    same idea to cover every input `resolve_python_bin(prefer_windowless=
    False)` can observe: the resolved impl path PLUS its mtime+size (so an
    edited/rebuilt `_machine_local.py` invalidates the entry even though its
    path string is unchanged), and the four env vars that steer or
    short-circuit which store is read (`MACHINE_LOCAL_IMPL`, `CLAUDE_HOME`,
    `COORDINATOR_SETTINGS_HOME`, `COORDINATOR_PYTHON`).

    Returns ``None`` (never cache) on `pyresolve` import failure -- the same
    condition `_bt_python3_invocation` itself falls open to `"python3"` on."""
    try:
        from coordinator_core.pyresolve import _machine_local_impl
    except ImportError:
        return None
    impl = _machine_local_impl()
    try:
        st = os.stat(impl)
        impl_sig: Optional[List[Any]] = [st.st_mtime_ns, st.st_size]
    except OSError:
        impl_sig = None
    return [
        impl,
        impl_sig,
        os.environ.get("MACHINE_LOCAL_IMPL", ""),
        os.environ.get("CLAUDE_HOME", ""),
        os.environ.get("COORDINATOR_SETTINGS_HOME", ""),
        os.environ.get("COORDINATOR_PYTHON", ""),
    ]


def _bt_python3_invocation() -> str:
    """Resolve the shell-ready interpreter prefix (e.g. ``python3``, or on a
    python.org Windows install with no `python3.exe` on PATH, ``py -3`` or an
    absolute ``python.exe`` path) for the BX-16 rewrite/advisory payloads
    below, instead of hardcoding ``python3`` -- a bare ``python3`` is
    frequently absent on stock Windows (the interpreter there is
    ``python.exe``, or the ``py``/``pyw`` launcher; a bare ``python3`` can
    also hit the WindowsApps Store-Python stub, see
    ``claude-code-platform-gotchas.md``'s "orphan AppX stub" entry).

    Reuses ``coordinator_core.pyresolve`` (the existing Windows-safe
    interpreter-resolution precedent already used for the same pin-precedence
    contract elsewhere in this package) rather than inventing a second
    resolver. ``prefer_windowless=False`` is mandatory here -- every payload
    this helper prefixes prints to stdout for the harness to read, and
    ``pythonw.exe`` (the windowless preference) silently swallows stdout (see
    that same wiki's "pythonw.exe swallows stdout/stderr" entry).

    Lazy-imported and fails open to the literal ``"python3"`` (today's
    behavior, unconditionally regenerated as an on-host verification item
    since it cannot be executed from macOS) on ANY resolution failure --
    ImportError, empty ``python_bin`` (nothing found), or
    ``PythonPinInvalid`` -- mirroring this module's existing lazy-import
    discipline (see the Finding-3 note above `_CREATIONFLAGS`): a broken
    resolver must never crash the whole dispatcher, only this one advisory
    rewrite's quality.

    The import and the resolution call are two SEPARATE `try`/`except`
    blocks, not one -- `PythonPinInvalid` is itself a name bound BY the
    import this function is trying to fail open around. A single combined
    `try: from ... import PythonPinInvalid, resolve_python_bin; ... except
    (ImportError, PythonPinInvalid, OSError)` has to evaluate its own except
    tuple to decide whether a raised `ImportError` matches it -- at which
    point `PythonPinInvalid` is UNBOUND, so Python raises `UnboundLocalError`
    instead of falling open to `"python3"`, contradicting this docstring's
    own "on ANY resolution failure" promise. Splitting the import into its
    own `except ImportError` (builtin name only, never unbound) guarantees
    `PythonPinInvalid` is bound by the time the second block's `except`
    clause can ever reference it.

    CROSS-PROCESS CACHE. This fires on the fleet's highest-firing advisory,
    so the resolution below is memoized to `_bt_python3_invocation_cache_
    path()` keyed by `_bt_python3_invocation_cache_key()` (see that
    function's docstring for exactly what the key covers). The read/write
    wraps the two try/except blocks below WITHOUT touching them -- a cache
    miss or any read failure falls straight through to the same live
    resolution this function has always performed, and a write failure is
    swallowed the same way: this helper's fail-open discipline is load-
    bearing and applies identically to the cache path. The write is an
    atomic replace (`os.replace` from a pid-suffixed temp file in the same
    directory), never a truncate-then-write -- at 50-70 concurrent sessions
    a torn write from a truncate is the norm, not an edge case, and a torn
    or unreadable cache file must fall through to live resolution rather
    than ever raise or return garbage.
    """
    cache_path = _bt_python3_invocation_cache_path()
    cache_key = _bt_python3_invocation_cache_key()
    if cache_key is not None:
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if isinstance(cached, dict) and cached.get("key") == cache_key:
                cached_value = cached.get("value")
                if isinstance(cached_value, str) and cached_value:
                    return cached_value
        except (OSError, ValueError):
            # Missing, torn, or unreadable cache -- fall through to live
            # resolution below, same as any other cache miss.
            pass

    try:
        from coordinator_core.pyresolve import PythonPinInvalid, resolve_python_bin
    except ImportError:
        return "python3"
    try:
        python_bin, python_args = resolve_python_bin(prefer_windowless=False)
    except (PythonPinInvalid, OSError):
        return "python3"
    if not python_bin:
        return "python3"
    result = " ".join(shlex.quote(tok) for tok in (python_bin, *python_args))

    if cache_key is not None:
        try:
            tmp_path = "%s.%d.tmp" % (cache_path, os.getpid())
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"key": cache_key, "value": result}, fh)
            os.replace(tmp_path, cache_path)
        except OSError:
            # Best-effort cache write -- a failure here just means the next
            # firing resolves live again, not a correctness issue.
            pass

    return result


def _bt_find_exec_python_rewrite(parsed: Dict[str, Any]) -> Optional[str]:
    """Translate a parsed `-exec` invocation into a single `python3 -c`
    one-liner, when the exec'd verb is one of `_FIND_EXEC_TRANSLATABLE_
    VERBS` (rm/cat/wc -- the observed census/cleanup habit: delete matches,
    print matches, count lines across matches). Returns `None` for any
    other verb -- an arbitrary `-exec <binary>` cannot be translated
    without knowing its semantics, and this function never guesses; the
    caller falls back to an advisory rather than a false auto-rewrite."""
    verb_norm = _normalize_executable_basename(parsed["exec_argv"][0])
    if verb_norm not in _FIND_EXEC_TRANSLATABLE_VERBS:
        return None
    path = parsed["path"]
    pattern = parsed["name_pattern"]
    match_expr = (
        # fnmatchcase, not fnmatch: fnmatch.fnmatch() normalizes case via
        # os.path.normcase, which is a no-op on POSIX but lower-cases both
        # sides on Windows -- silently case-INSENSITIVE there, while `find
        # -name` (unlike `-iname`) is case-sensitive on every platform.
        "fnmatch.fnmatchcase(fn, %s)" % json.dumps(pattern) if pattern else "True"
    )
    if verb_norm == "rm":
        body = (
            "import fnmatch, os\n"
            "n = 0\n"
            "for root, dirs, files in os.walk(%s):\n"
            "    for fn in files:\n"
            "        if %s:\n"
            "            os.remove(os.path.join(root, fn)); n += 1\n"
            'print(n, "file(s) removed")' % (json.dumps(path), match_expr)
        )
    elif verb_norm == "cat":
        body = (
            "import fnmatch, os\n"
            "for root, dirs, files in os.walk(%s):\n"
            "    for fn in files:\n"
            "        if %s:\n"
            '            print(open(os.path.join(root, fn), encoding="utf-8", errors="replace").read())'
            % (json.dumps(path), match_expr)
        )
    else:  # wc -- only the `-l` (line-count) form is translated
        if "-l" not in parsed["exec_argv"][1:]:
            return None
        body = (
            "import fnmatch, os\n"
            "total = 0\n"
            "for root, dirs, files in os.walk(%s):\n"
            "    for fn in files:\n"
            "        if %s:\n"
            '            with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as fh:\n'
            "                total += sum(1 for _ in fh)\n"
            "print(total)" % (json.dumps(path), match_expr)
        )
    return "%s -c %s" % (_bt_python3_invocation(), shlex.quote(body))


def check_find_exec_rewrite(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 1 (flagship) -- `find ... -exec <binary> {} ;` and its
    `for f in $(find ...); do <binary> "$f"; done` sibling both fork ONE
    CHILD PROCESS PER MATCH, which is the exact mechanism behind the
    founding incident's 879-process stall on Windows (DoE
    ``state/plan-sidecars/2026-07-28-bash-tax-negative-space.md``).

    Auto-rewrites to a single `python3 -c` process (zero per-match forks)
    when the exec'd verb is translatable (rm/cat/wc -l); otherwise advises
    with a generic os.walk skeleton rather than guessing at an arbitrary
    binary's semantics. Never denies -- see this section's module comment.

    shell-doc-ok: the two spellings above are the real bash command shapes
    this check matches; re-rendering them would leave the docstring unable to
    name what it detects.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_FIND_EXEC"):
        return None
    classification = _bt_classify_command(cmd)
    if classification.tokens is None:
        return None
    if not (
        classification.has_shape(_BT_Shape.FIND_EXEC_XARGS)
        or classification.has_shape(_BT_Shape.FOR_LOOP)
    ):
        return None

    segments = _bt_segments_from_tokens_with_pipe_flag(classification.tokens)
    # LATENT-BUG FIX (BX-12 audit, same day): `_allow_rewrite` replaces the
    # ENTIRE command via `updatedInput.command` -- it is only sound when the
    # matched `find ... -exec` segment IS the whole command. Before this
    # guard, this loop found the first `find`+`-exec` segment ANYWHERE in a
    # `;`/`&`-joined chain and silently replaced the FULL original command
    # (an unrelated for-loop, an unrelated `echo`, etc.) with just that one
    # segment's python rewrite -- verified: `echo hi; find . -exec rm {} \;`
    # and `for x in 1 2 3; do echo $x; done; find . -exec rm {} \;` both came
    # back with `updatedInput.command` silently dropping the loop/echo and
    # keeping only the `rm` rewrite, a silent command-corruption hazard, not
    # merely a misdescribed message. Mirrors BX-6's own single-segment
    # substitutable-residue rule (`guard_grep_via_bash._substitutable_
    # rewrite`): only a lone, standalone `find ... -exec` segment (nothing
    # chained before/after it) is auto-rewritten; a multi-segment match
    # (including the for-loop-wraps-a-trailing-find-exec shape) degrades to
    # an advisory that still names the specific segment, never a full-command
    # replacement of work this function never inspected.
    single_segment = len(segments) == 1
    _find_exec_note = operator_override_note(
        "COORDINATOR_ALLOW_FIND_EXEC", payload=payload, git_root=git_root
    )
    for tokens, _pipe_before in segments:
        if not tokens or not _bt_token_matches_binary(tokens[0], "find"):
            continue
        parsed = _bt_parse_find_exec_segment(tokens)
        if not parsed:
            continue
        rewrite = _bt_find_exec_python_rewrite(parsed)
        if rewrite and single_segment:
            return _allow_rewrite(
                rewrite,
                (
                    "Auto-rewritten: 'find ... -exec %s ... {} ;' forks one "
                    "process PER MATCH (the founding-incident 879-process shape "
                    "on Windows) -> one python3 process, zero per-match forks."
                    % (parsed["exec_argv"][0],)
                )
                + (" %s" % _find_exec_note if _find_exec_note else ""),
            )
        if rewrite:
            return _advisory(
                (
                    "Advisory: 'find ... -exec %s ... {} ;' (segment: %s) forks "
                    "one process PER MATCH -- the founding-incident 879-process "
                    "shape on Windows. A single python3 -c os.walk(...) loop "
                    "does the same enumeration in one process, but this "
                    "find-exec segment runs alongside OTHER work in the same "
                    "command (a for-loop, a chained command, or both), so no "
                    "full-command auto-rewrite is offered -- replacing the "
                    "whole command would silently drop that other work."
                    % (parsed["exec_argv"][0], " ".join(tokens))
                )
                + (" %s" % _find_exec_note if _find_exec_note else "")
            )
        return _advisory(
            (
                "Advisory: 'find ... -exec %s ... {} ;' forks one process PER "
                "MATCH -- the founding-incident 879-process shape on Windows. "
                "A single python3 -c os.walk(...) loop does the same "
                "enumeration in one process; this exec'd verb has no known "
                "translation on file, so the rewrite is not offered "
                "automatically."
                % (parsed["exec_argv"][0],)
            )
            + (" %s" % _find_exec_note if _find_exec_note else "")
        )
    return None


#: grep-family short flags this rewrite considers "substitutable residue"
#: (BX-16 body's own phrase) -- a plain recursive/case-insensitive/count/
#: filenames-only/line-numbered/extended-regex/fixed-string search. A long
#: option or an unrecognized short flag (context lines, invert-match, etc.)
#: falls through to `None` -- that residue is BX-6's to police, not this
#: row's to guess a translation for.
#:
#: `E`/`F` (uppercase ONLY, matching real grep's own spelling of
#: `-E`/`-F`) are recognized here only to steer dialect selection below
#: (`_bt_grep_dialect`) -- they do not themselves change the generated
#: Python source the way `i`/`l`/`c` do. Deliberately NOT lowercase `e`/`f`:
#: real grep's own lowercase `-e PATTERN` (specify a pattern operand) and
#: `-f FILE` (read patterns from a file) are unrelated flags that TAKE AN
#: ARGUMENT -- treating them as dialect flags here would silently consume a
#: real grep operand as if it were a bare boolean switch. Membership is
#: checked case-sensitively BEFORE `ch.lower()` folds the recognized
#: uppercase `E`/`F` into the lowercase `"e"`/`"f"` dialect-flag entries
#: `_bt_grep_dialect` reads.
#:
#: Review: code-reviewer (Finding 4) -- `w` (whole-word match) is
#: deliberately NOT in this set. Neither `_bt_grep_python_rewrite` nor
#: `_bt_build_generator_lines`'s `"grep"` kind ever wraps the translated
#: pattern in a word-boundary (`\b...\b`), so accepting `-w` here while
#: silently dropping its semantics would make `grep -w foo` over-match
#: (`foobar` too) with no error signal -- exactly the "confident wrong
#: answer" class this row exists to refuse rather than guess at. Per this
#: file's own convention (refuse rather than guess), an unrecognized `-w`
#: now falls through to the advisory/refuse path like any other
#: untranslated flag instead of being silently accepted and dropped.
_GREP_SUBSTITUTABLE_SHORT_FLAGS = frozenset({"r", "R", "n", "i", "l", "c", "E", "F"})

#: A POSIX bracket-expression class/collating-symbol/equivalence-class
#: (`[[:alpha:]]`, `[.ch.]`, `[=a=]`) has no Python `re` equivalent at all --
#: any pattern containing one is refused outright, in every dialect.
_GREP_POSIX_BRACKET_CLASS_RE = re.compile(r"\[[:.=][a-zA-Z]*[:.=]\]")

#: Backslash-escapes that mean the identical literal character in BOTH a
#: POSIX ERE (egrep/`grep -E`/ripgrep's default dialect) and Python `re`.
#: Any OTHER escape (`\+`, `\d`, `\<`, ...) is dialect-ambiguous or a
#: Perl/Python-only shorthand absent from POSIX ERE -- refused, not guessed.
_GREP_EXTENDED_SAFE_ESCAPES = frozenset(".*^$[](){}?+|\\")

#: Same idea for POSIX BRE (bare `grep`, no `-E`/`-F`) -- BRE's own
#: metacharacter set is narrower (`+ ? ( ) { } |` are GNU *extensions* whose
#: escaped-vs-bare meaning flips relative to Python `re`; see
#: `_bt_grep_pattern_safe_for_dialect`'s own `dialect == "basic"` branch,
#: which refuses bare occurrences of that set directly rather than via this
#: escape whitelist).
_GREP_BASIC_SAFE_ESCAPES = frozenset(".*^$[]\\")


def _bt_grep_dialect(binary_token: str, flags: set) -> Optional[str]:
    """Classify a grep-family invocation's REGEX DIALECT -- "fixed"
    (`-F`/`fgrep`: no metacharacters at all), "extended" (`-E`/`egrep`, and
    ripgrep's own default -- Rust regex is ERE/Perl-like, not POSIX BRE), or
    "basic" (bare `grep`, POSIX BRE, the founding-incident dialect: `\\|`
    `\\+` `\\?` `\\{ \\}` are GNU-extension operators, and bare
    `| + ? { }` are ordinary literal characters -- the OPPOSITE of Python
    `re`, which is why compiling a BRE pattern as-is silently inverted this
    dispatch's own regression case, see the module-level negative-spec note
    on `check_grep_via_bash_rewrite`).

    Returns `None` for the `-E -F`/`-F -E` combination -- real grep lets the
    LAST flag on the command line win, and this function has no ordering
    information (`flags` is an unordered set), so guessing which one "wins"
    would be exactly the guess this row's whole convention refuses to make.
    """
    has_f = "f" in flags
    has_e = "e" in flags
    if has_f and has_e:
        return None
    if has_f or _bt_token_matches_binary(binary_token, "fgrep"):
        return "fixed"
    if has_e or _bt_token_matches_binary(binary_token, "egrep"):
        return "extended"
    if _bt_token_matches_binary(binary_token, "rg"):
        return "extended"
    return "basic"


def _bt_grep_pattern_safe_for_dialect(pattern: str, dialect: str, safe_escapes: frozenset) -> bool:
    """Bracket-aware scan over `pattern`: `True` only if every construct in
    it means the IDENTICAL thing under `dialect` and under Python `re`.
    Refuses (returns `False`) rather than guesses at anything ambiguous:

    - a POSIX bracket-expression class anywhere in the pattern (checked by
      the caller via `_GREP_POSIX_BRACKET_CLASS_RE` before this scan even
      starts -- not re-checked here);
    - a backslash appearing INSIDE a `[...]` bracket expression -- POSIX
      bracket expressions have no escape mechanism at all (backslash is an
      ordinary bracket member), while Python `re` treats it as an escape
      inside a character class too (`[\\.]` means two different things);
    - an escape of any character not in `safe_escapes` (a dialect-specific
      GNU/Perl shorthand like `\\d`, `\\w`, `\\<`, or a BRE/ERE-extension
      operator whose escaped-vs-bare meaning is dialect-dependent);
    - (basic dialect only) a BARE occurrence of `+ ? ( ) { } |` outside a
      bracket expression -- literal in BRE, a metacharacter in Python `re`;
    - (basic dialect only) a BARE `^` anywhere except the pattern's very
      first character, or a bare `$` anywhere except its very last
      character -- Review: code-reviewer (Finding 5) -- POSIX BRE treats
      `^`/`$` as anchors ONLY at those positions and as ordinary literal
      characters everywhere else, while Python `re` treats a bare `^`/`$`
      as an anchor UNCONDITIONALLY regardless of position. `grep -n 'a^b'`/
      `grep -n 'a$b'` (BRE: the literal substrings `a^b`/`a$b`) used to be
      classified "safe" and compiled unchanged under Python `re`, producing
      a pattern that can never match anything -- the identical silent-
      dialect-conflation failure this function already refuses for BRE
      alternation, just on the anchor-literalness axis instead.
    """
    in_bracket = False
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if in_bracket:
            if c == "\\":
                return False
            if c == "]":
                in_bracket = False
            i += 1
            continue
        if c == "\\":
            if i + 1 >= n or pattern[i + 1] not in safe_escapes:
                return False
            i += 2
            continue
        if dialect == "basic":
            if c in "+?(){}|":
                return False
            if c == "^" and i != 0:
                return False
            if c == "$" and i != n - 1:
                return False
        if c == "[":
            in_bracket = True
        i += 1
    return True


def _bt_grep_translate_pattern(pattern: str, dialect: str) -> Optional[str]:
    """Return a Python `re`-equivalent SOURCE STRING for `pattern` under
    `dialect`, or `None` if no translation is provably faithful -- this
    module's "refuse rather than guess" convention (see the `find -exec`
    unknown-verb case, above), applied to the regex-dialect axis instead of
    the exec'd-verb axis.

    `dialect == "fixed"` (`-F`/`fgrep`) is unconditionally translatable via
    `re.escape` -- there are no metacharacters to misinterpret when the
    ENTIRE pattern is a literal string, which is exactly what makes this the
    one dialect this function never has to refuse.
    """
    if dialect == "fixed":
        return re.escape(pattern)
    if _GREP_POSIX_BRACKET_CLASS_RE.search(pattern):
        return None
    safe_escapes = _GREP_EXTENDED_SAFE_ESCAPES if dialect == "extended" else _GREP_BASIC_SAFE_ESCAPES
    if not _bt_grep_pattern_safe_for_dialect(pattern, dialect, safe_escapes):
        return None
    return pattern


def _bt_grep_flags_and_operands(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """`tokens` is one already-tokenized segment whose first token is a
    grep-family binary. Returns
    ``{"flags": {...short flag chars...}, "pattern": str, "targets": [...]}``,
    or `None` if a long option (`--foo`) or an unrecognized short flag makes
    this non-substitutable, OR if the pattern's own regex dialect
    (`_bt_grep_dialect`) cannot be PROVABLY translated into the Python `re`
    syntax the generated rewrite compiles it with (`_bt_grep_translate_
    pattern`) -- the returned `"pattern"` is already the translated Python
    `re` source, not the raw operand, so every caller of this function
    (`_bt_grep_python_rewrite`, `_bt_build_generator_lines`'s `"grep"` kind)
    gets dialect-safety for free without needing its own translation step.

    Negative-spec: this function used to hand the RAW grep-family pattern
    straight through to `json.dumps(...)` in both callers, which then
    compiled it as Python `re` unconditionally -- silently WRONG for the
    default (POSIX BRE) dialect, where `\\|`/`\\+`/`\\?`/`\\{ \\}` are
    operators and bare `| + ? { }` are literals, the OPPOSITE of Python
    `re`. Hit live: `grep -n "^| AC-3 \\|^| AC-4 \\|^| AC-5 " <file>`
    (BRE alternation) silently became a single-character-class-free
    catch-some-lines-none pattern under Python `re`'s reading and matched
    every line of a 4000-line file instead of six.

    Also declines (returns `None`) on a shell redirection operator
    (`_bt_has_redirection`) among the operand tokens -- see that helper's
    own docstring for the live incident this guards against (a redirection
    token silently taken as a grep target/pattern instead of being
    recognized as shell syntax). Shared by both callers of this function
    (`check_grep_via_bash_rewrite`'s standalone-grep rewrite and
    `_bt_build_generator_lines`'s `"grep"` kind via `check_head_tail_
    plumbing_rewrite`), so both get the same protection from one fix.
    """
    if _bt_has_redirection(tokens[1:]):
        return None
    flags: set = set()
    operands: List[str] = []
    for tok in tokens[1:]:
        if tok.startswith("--"):
            return None
        if tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                if ch not in _GREP_SUBSTITUTABLE_SHORT_FLAGS:
                    return None
                flags.add(ch.lower())
            continue
        operands.append(tok)
    if not operands:
        return None
    pattern, targets = operands[0], operands[1:]
    dialect = _bt_grep_dialect(tokens[0], flags)
    if dialect is None:
        return None
    translated = _bt_grep_translate_pattern(pattern, dialect)
    if translated is None:
        return None
    return {"flags": flags, "pattern": translated, "targets": targets or ["."]}


def _bt_grep_python_rewrite(parsed: Dict[str, Any]) -> str:
    """Translate a parsed grep-family invocation into a single `python3 -c`
    `os.walk`/`re` one-liner. Honors `-i` (case-insensitive), `-l`
    (filenames only), `-c` (count only); the default output shape mirrors
    `grep -n` (`path:lineno:line`)."""
    flags = parsed["flags"]
    body_lines = [
        "import os, re",
        "pat = re.compile(%s%s)"
        % (json.dumps(parsed["pattern"]), ", re.IGNORECASE" if "i" in flags else ""),
        "targets = %s" % json.dumps(parsed["targets"]),
        "for base in targets:",
        "    walk = os.walk(base) if os.path.isdir(base) else "
        '[(os.path.dirname(base) or ".", [], [os.path.basename(base)])]',
        "    for root, dirs, files in walk:",
        "        for fn in files:",
        "            p = os.path.join(root, fn)",
        "            try:",
        '                with open(p, encoding="utf-8", errors="replace") as fh:',
        "                    lines = fh.readlines()",
        "            except OSError:",
        "                continue",
        "            hits = [(i + 1, ln) for i, ln in enumerate(lines) if pat.search(ln)]",
        "            if not hits:",
        "                continue",
    ]
    if "l" in flags:
        body_lines.append("            print(p)")
    elif "c" in flags:
        body_lines.append('            print(p + ":" + str(len(hits)))')
    else:
        body_lines.append("            for lineno, ln in hits:")
        body_lines.append(
            '                print(p + ":" + str(lineno) + ":" + ln.rstrip())'
        )
    return "%s -c %s" % (_bt_python3_invocation(), shlex.quote("\n".join(body_lines)))


_GREP_FAMILY_BINARIES_BT = ("grep", "egrep", "fgrep", "rg")


def check_grep_via_bash_rewrite(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 2 -- `grep`-via-Bash (50.9% of measured forks, the
    single largest shape). Auto-rewrites a "substitutable residue" grep
    invocation (see `_GREP_SUBSTITUTABLE_SHORT_FLAGS`) into a single
    `python3 -c os.walk`/`re` process; falls through to `None` for anything
    piped, composed, or carrying an unrecognized flag -- BX-6 owns the
    Windows deny policy for THAT residue, this row only owns the rewrite
    target. Never denies.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_GREP_VIA_BASH"):
        return None
    classification = _bt_classify_command(cmd)
    if classification.tokens is None:
        return None
    if not classification.has_shape(_BT_Shape.GREP_VIA_BASH):
        return None
    segments = _bt_segments_from_tokens_with_pipe_flag(classification.tokens)
    if len(segments) != 1:
        return None  # a composed/piped command is left to BX-6, not rewritten here
    tokens, pipe_before = segments[0]
    if pipe_before or not tokens:
        return None  # `... | grep ...` -- the upstream command already did work
    if not any(_bt_token_matches_binary(tokens[0], b) for b in _GREP_FAMILY_BINARIES_BT):
        return None
    parsed = _bt_grep_flags_and_operands(tokens)
    if parsed is None:
        return None
    _grep_note = operator_override_note(
        "COORDINATOR_ALLOW_GREP_VIA_BASH", payload=payload, git_root=git_root
    )
    return _allow_rewrite(
        _bt_grep_python_rewrite(parsed),
        # Portability claim, not a fork-count one: GNU and BSD grep disagree
        # on where `^` binds inside an anchored alternation (e.g.
        # `grep -E '^status:|^deployment_state:'`), so the same pattern can
        # match a different line set depending on host grep dialect. That is
        # exactly the case regex_translate.py's compile_spec refuses to
        # translate ("regex dialect not provably translatable"), so it is
        # also exactly the case that still reaches this rewrite in practice
        # once the in-process answerer has already shadowed everything it
        # can safely answer. The python3 rewrite evaluates the pattern with
        # Python's own `re` engine, so behavior is identical on every host.
        (
            "Auto-rewritten: 'grep' via Bash spawns a child process, and on "
            "anchored-alternation patterns (`^a|^b`) GNU and BSD grep disagree "
            "on anchor position, so host grep can silently match a different "
            "line set on Mac/BSD than on Linux/GNU. The python3 rewrite uses "
            "Python's own regex engine, giving identical behavior on every "
            "host."
        )
        + (" %s" % _grep_note if _grep_note else ""),
    )


def check_sed_range_read_advise(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 3 -- `sed -n 'A,Bp' FILE` reads a line range via a
    subprocess; the Read tool does the identical paged read natively, no
    subprocess. Advisory only -- a Bash-to-Bash `updatedInput` cannot name a
    harness tool call, so this can only ever suggest, never rewrite.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_SED_RANGE"):
        return None
    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return None
    segments = _bt_segments_from_tokens_with_pipe_flag(tokens)
    if len(segments) != 1:
        return None
    seg_tokens, pipe_before = segments[0]
    if pipe_before or not seg_tokens:
        return None
    if not _bt_token_matches_binary(seg_tokens[0], "sed"):
        return None
    rest = [t for t in seg_tokens[1:] if t != "-n"]
    if "-n" not in seg_tokens[1:] or not rest:
        return None
    script = rest[0]
    file_arg = rest[1] if len(rest) > 1 else None
    parts = script.split(",")
    if len(parts) != 2:
        return None
    start_s, tail = parts[0], parts[1]
    if not (tail.endswith("p") and start_s.isdigit() and tail[:-1].isdigit()):
        return None
    start, end = int(start_s), int(tail[:-1])
    if end < start:
        return None
    _sed_note = operator_override_note(
        "COORDINATOR_ALLOW_SED_RANGE", payload=payload, git_root=git_root
    )
    return _advisory(
        (
            "Advisory: 'sed -n \"%s\"' reads a line range via a subprocess -- "
            "the Read tool does the same paged read natively: Read(%s, "
            "offset=%d, limit=%d)."
            % (script, file_arg or "<file>", start, end - start + 1)
        )
        + (" %s" % _sed_note if _sed_note else "")
    )


def check_cat_heredoc_write_advise(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 4 -- `cat > FILE <<'EOF' ... EOF` writes a file via a
    subprocess heredoc; the Write tool authors the same file directly, no
    subprocess. Advisory only, for the same reason as the sed check above.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_CAT_HEREDOC"):
        return None
    if "<<" not in cmd or not re.search(r"\bcat\b", cmd):
        return None
    intro, _, _ = cmd.partition("<<")
    tokens = _bt_tokenize_full_command(intro)
    if not tokens:
        return None
    segments = _bt_segments_from_tokens_with_pipe_flag(tokens)
    if not segments:
        return None
    seg_tokens, pipe_before = segments[-1]
    if pipe_before or not seg_tokens:
        return None
    if not _bt_token_matches_binary(seg_tokens[0], "cat"):
        return None
    redir = ">" if ">" in seg_tokens else (">>" if ">>" in seg_tokens else None)
    if redir is None:
        return None
    redir_idx = seg_tokens.index(redir)
    if redir_idx + 1 >= len(seg_tokens):
        return None
    target = seg_tokens[redir_idx + 1]
    _cat_note = operator_override_note(
        "COORDINATOR_ALLOW_CAT_HEREDOC", payload=payload, git_root=git_root
    )
    return _advisory(
        (
            "Advisory: 'cat %s %s <<EOF ... EOF' writes a file via a subprocess "
            "heredoc -- the Write tool authors the same file directly, no "
            "subprocess."
            % (redir, target)
        )
        + (" %s" % _cat_note if _cat_note else "")
    )


#: A literal-string open()/Path(...).write_text()/write_bytes() call inside a
#: scriptable-interpreter heredoc body (the same "scriptable" classification
#: `check_cat_heredoc_write_advise`'s neighbours use -- see
#: `_HEREDOC_SCRIPTABLE_INTERPRETERS`). Three shapes, each captured with the
#: (quote, path, quote) group at a fixed position so a caller can reject any
#: match whose path group contains an f-string/format placeholder:
#:   1. ``open(<path>, <mode>)`` where mode starts with w/a/x (not a bare 'r').
#:   2. ``pathlib.Path(<path>).write_text(``/``.write_bytes(``
#:   3. ``Path(<path>).write_text(``/``.write_bytes(`` (the un-prefixed import
#:      shape, e.g. ``from pathlib import Path``).
#: A quote prefixed by an f/F/rb/br/etc. string-prefix letter is excluded at
#: the call site (checked via the character immediately before the opening
#: quote), not in the regex itself -- Python's string-prefix alphabet is
#: small but not fixed-width, and a lookbehind can't express "zero or more
#: prefix letters, but not zero-plus-anything-else" cleanly. Same for a path
#: containing ``{`` (an f-string placeholder that slipped through because the
#: opening quote itself wasn't prefixed, e.g. a `.format()`-style literal
#: reused as an f-string body) -- rejected post-match, not in-regex.
_HEREDOC_WRITE_TARGET_RE = re.compile(
    r"""
    (?:
        \bopen\s*\(\s*
        (?P<q1>['"])(?P<path1>[^'"]*)(?P=q1)
        \s*,\s*
        (?P<q1m>['"])(?P<mode1>[a-zA-Z+]+)(?P=q1m)
    )
    |
    (?:
        (?:pathlib\.)?Path\s*\(\s*
        (?P<q2>['"])(?P<path2>[^'"]*)(?P=q2)
        \s*\)\s*\.\s*write_(?:text|bytes)\s*\(
    )
    """,
    re.VERBOSE,
)

#: Scratch/temp roots a heredoc write into is the CORRECT use of a heredoc
#: (ephemeral working files, never repo content) -- never advised. Mirrors
#: the scratchpad shape this session's own harness uses
#: (``…/AppData/Local/Temp/claude/…``), plus the bare OS temp dirs.
_HEREDOC_SCRATCH_ROOT_MARKERS = ("/appdata/local/temp/claude/",)


def _heredoc_write_target_is_scratch(abs_path: str) -> bool:
    """True if `abs_path` (already absolute, forward-slash-normalized by the
    caller) resolves under a scratch/temp root: the coordinator scratchpad
    shape, a bare ``/tmp``, or the live ``$TEMP``/``$TMP``/``%TEMP%``/
    ``%TMP%`` env var's own directory. Env vars are read fresh per call
    (never cached at module scope) for the same reason `_override` reads
    fresh -- a test that monkeypatches ``os.environ`` must see it take
    effect on the very next call, not a stale import-time snapshot."""
    norm = abs_path.replace("\\", "/").lower()
    if any(marker in norm for marker in _HEREDOC_SCRATCH_ROOT_MARKERS):
        return True
    if norm == "/tmp" or norm.startswith("/tmp/"):
        return True
    for env_var in ("TEMP", "TMP"):
        val = os.environ.get(env_var)
        if not val:
            continue
        val_norm = os.path.normcase(os.path.normpath(val))
        if _paths_match_prefix(val_norm, abs_path):
            return True
    return False


def _paths_match_prefix(root: str, target: str) -> bool:
    """True if normalized `target` is `root` itself or lives underneath it --
    pure `os.path` string arithmetic, no `stat`/`samefile` (the target of a
    write-in-progress heredoc usually does not exist on disk yet, so
    `os.path.samefile` would raise for the common case). Case-insensitive
    (`os.path.normcase`) so this behaves identically on Windows, where the
    guard's own repo (`claude-klabauter`, "Windows is first-class" per this
    repo's CLAUDE.md) actually runs."""
    root_n = os.path.normcase(os.path.normpath(root))
    target_n = os.path.normcase(os.path.normpath(target))
    return target_n == root_n or target_n.startswith(root_n + os.sep)


def _heredoc_scriptable_bodies(cmd: str) -> List[List[str]]:
    """Yield the RAW body-line lists of every heredoc in `cmd` whose intro
    classifies as `"scriptable"` (`_classify_heredoc_intro`) -- the identical
    classification walk `_strip_heredoc_bodies_for_prose_scan` performs, but
    collecting bodies for a write-pattern content scan instead of a
    strip/keep decision. Does not itself decide spawn-indicator visibility
    (`_heredoc_body_has_spawn_indicator` is a DIFFERENT question -- whether
    the body might shell out -- orthogonal to whether it writes a file).
    Fails closed by yielding nothing on an unterminated heredoc, matching
    every sibling heredoc walker in this module."""
    lines = cmd.split("\n")
    out: List[List[str]] = []
    in_hd = False
    hd_word = ""
    hd_strip = False
    hd_class = "unknown"
    pending: List[str] = []
    for line in lines:
        if not in_hd:
            m = _HEREDOC_INTRO_RE.search(line)
            if m:
                seg = m.group(0)
                hd_strip = len(seg) > 2 and seg[2] == "-"
                w = seg[3:] if hd_strip else seg[2:]
                if w and w[0] in ("'", '"'):
                    w = w[1:-1]
                hd_word = w
                in_hd = True
                hd_class = _classify_heredoc_intro(line[: m.start()], line)
                pending = []
        else:
            check = line
            if hd_strip:
                check = re.sub(r"^\t+", "", check)
            if check == hd_word:
                in_hd = False
                if hd_class == "scriptable" and pending:
                    out.append(pending)
                pending = []
            elif hd_class == "scriptable":
                pending.append(line)
    return out


def check_heredoc_repo_write_advise(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16-family sibling of `check_cat_heredoc_write_advise`, for the
    DIFFERENT hazard a scriptable-interpreter heredoc creates (`python3 -
    <<'PY' ... PY`, a generator, a formatter): its write is invisible to
    `coordinator_core/hooks/track_touched_files.py::_handler` (matcher is
    `Write|Edit|MultiEdit|NotebookEdit` ONLY -- a ratified permanent limit,
    DR-258, NOT touched or widened by this guard), so `safe-commit-offer`
    silently drops the file from the session's commit pathspec. Advisory
    only, and silence-biased: this can only ever SUGGEST the Write tool, and
    stays silent on anything it cannot resolve to a literal repo-relative
    path rather than risk a false positive on every python heredoc.

    Detection is pure path arithmetic on `git_root` (handed in by the
    caller, never derived here) -- NO subprocess, NO `git status`/`git
    ls-files`. **What production actually hands in is the payload's `cwd`**,
    not a resolved git root: this module never resolves one (F0 -- see
    `dispatch`'s module docstring), so the containment test is "under the
    invoking cwd," and a repo-relative write from a subdirectory resolves
    against that subdirectory. That is narrower than "inside the repo" and
    the difference is a SILENCE, never a false advisory, which is the bias
    this guard is built around. `git_root` empty/None means "cannot classify," which fails
    CLOSED to silence (the mirror image of every hard-deny in this module
    failing closed to DENY): a false silence here costs nothing but a missed
    advisory; a false advisory would be noise on every unrelated heredoc.

    Known-uncovered sibling case, NOT this guard's job: a PowerShell
    here-string (`@'...'@`) has the identical invisibility to the touch
    tracker, but `matchers=("Bash",)` (deliberate, see this function's
    `GuardEntry` registration) makes a Bash-matched guard inert under the
    PowerShell tool. PowerShell here-string syntax is different enough
    (`@'`/`'@` delimiters, no interpreter-intro line to classify) that
    covering it is separate work, not a gap in this guard's own logic.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_HEREDOC_REPO_WRITE"):
        return None
    if not git_root:
        return None
    if "<<" not in cmd:
        return None
    bodies = _heredoc_scriptable_bodies(cmd)
    if not bodies:
        return None
    git_root_abs = os.path.normpath(git_root)
    for body_lines in bodies:
        body = "\n".join(body_lines)
        for m in _HEREDOC_WRITE_TARGET_RE.finditer(body):
            if m.group("path1") is not None:
                path = m.group("path1")
                mode = m.group("mode1")
                if not any(c in mode for c in ("w", "a", "x")):
                    continue  # read-only mode ('r', 'rb') -- not a write
                q_idx = m.start("q1")
            else:
                path = m.group("path2")
                q_idx = m.start("q2")
            if not path:
                continue
            if "{" in path or "%" in path:
                continue  # likely an interpolation placeholder, not a literal
            # Reject an f-string/raw/byte-prefixed quote -- the character(s)
            # immediately before the opening quote, if they look like a
            # Python string-prefix letter run, mean `path` is a template,
            # not the literal text it appears to be.
            pre = body[:q_idx]
            pre_word = re.search(r"([A-Za-z]{1,2})$", pre)
            if pre_word and set(pre_word.group(1).lower()) <= set("frbu"):
                continue
            target = path.replace("\\", "/")
            if os.path.isabs(target) or re.match(r"^[A-Za-z]:/", target):
                candidate = os.path.normpath(target)
            else:
                candidate = os.path.normpath(os.path.join(git_root_abs, target))
            if not _paths_match_prefix(git_root_abs, candidate):
                continue  # outside git_root -- stay silent
            rel = os.path.relpath(candidate, git_root_abs).replace("\\", "/")
            if rel == ".git" or rel.startswith(".git/"):
                continue
            if _heredoc_write_target_is_scratch(candidate):
                continue
            _hd_note = operator_override_note(
                "COORDINATOR_ALLOW_HEREDOC_REPO_WRITE", payload=payload, git_root=git_root
            )
            return _advisory(
                (
                    "Advisory: this heredoc writes `%s`, a path inside this repo. "
                    "Writes through Bash aren't recorded as touched (DR-258), "
                    "so safe-commit-offer will drop it from this session's commit. "
                    "Use the Write tool."
                    % rel
                )
                + (" %s" % _hd_note if _hd_note else "")
            )
    return None


#: BX-13 peel, reused token-wise rather than re-derived (C1a Fix 3): mirrors
#: `_GC_CLEAN_CMD_RE`'s wrapper-prefix vocabulary (`sudo`/`command`/`time`/
#: `exec`/`nice`/`nohup`/`ionice`/`timeout`/`stdbuf`/`which`/`type`, an `env`
#: token, or a bare `NAME=value` assignment) -- `_bt_git_resolved_subcommand`
#: previously required `tokens[0]` to be the git binary outright, so
#: `GIT_INDEX_FILE=/tmp/i git commit -m x` and `nice git commit -m x` were
#: never recognized as a git invocation at all and silently bypassed
#: `check_git_commit_safe_commit_advise`.
_GIT_WRAPPER_PREFIX_WORDS = frozenset(
    {
        "sudo", "command", "time", "exec", "nice", "nohup",
        "ionice", "timeout", "stdbuf", "which", "type", "env",
    }
)
# Known limitation: `\S*` cannot match an embedded space, so a value like
# `GIT_INDEX_FILE="/tmp/my index"` (a single shlex token containing a
# literal space) fails this regex and is not recognized as an env
# assignment -- a silent under-fire on that low-likelihood, space-containing
# path shape, not widened here.
_ENV_ASSIGNMENT_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _bt_peel_wrapper_prefix(tokens: List[str]) -> List[str]:
    """Drop leading env-var-assignment and wrapper-binary tokens (BX-13
    shape, see `_GIT_WRAPPER_PREFIX_WORDS` above) so a caller resolving the
    git binary at position 0 sees the same command a shell would actually
    exec. Stops at the first token that is neither -- never guesses past an
    ambiguous token."""
    i = 0
    n = len(tokens)
    while i < n and (
        any(_bt_token_matches_binary(tokens[i], w) for w in _GIT_WRAPPER_PREFIX_WORDS)
        or _ENV_ASSIGNMENT_TOKEN_RE.match(tokens[i])
    ):
        i += 1
    return tokens[i:]


def _bt_git_subcommand_start_index(tokens: List[str]) -> Optional[int]:
    """Positional walk locating the git subcommand token, returning the
    index of the first token AFTER it in the ORIGINAL (unpeeled) `tokens`
    list -- so a caller that needs to keep scanning past the subcommand
    (e.g. `_bt_commit_operand_scan`) doesn't have to re-derive the
    wrapper-prefix offset itself. `None` on anything unresolvable; never
    guesses.

    Peels a leading env-var-assignment/wrapper-binary prefix (BX-13 shape,
    C1a Fix 3) before resolving the git binary -- see
    `_bt_peel_wrapper_prefix` -- and re-adds that offset to the returned
    index. `_bt_git_resolved_subcommand` is a thin wrapper over this that
    returns the subcommand token itself instead of an index."""
    peeled = _bt_peel_wrapper_prefix(tokens)
    offset = len(tokens) - len(peeled)
    if not peeled or not _bt_token_matches_binary(peeled[0], "git"):
        return None
    i = 1
    n = len(peeled)
    while i < n:
        tok = peeled[i]
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
            if tok in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                i += 1
                continue
            return None
        return offset + i + 1
    return None


def _bt_git_resolved_subcommand(tokens: List[str]) -> Optional[str]:
    """Positional git-subcommand walk over an already-tokenized segment
    (mirrors `_seg_resolved_git_subcommand`'s walk, operating on tokens
    directly rather than re-shlex-splitting a rejoined string -- avoids
    re-tokenizing a `-m "multi word"` operand that the caller has no need
    to re-parse). `None` on anything unresolvable; never guesses.

    Peels a leading env-var-assignment/wrapper-binary prefix (BX-13 shape,
    C1a Fix 3) before resolving the git binary -- see
    `_bt_peel_wrapper_prefix`. Thin wrapper over
    `_bt_git_subcommand_start_index`, which does the actual walk."""
    idx = _bt_git_subcommand_start_index(tokens)
    if idx is None:
        return None
    return tokens[idx - 1]


#: `git commit` options whose VALUE is a separate following token -- skipped
#: as a pair by `_bt_commit_has_explicit_pathspec` so an option value can
#: never be misread as the `--` pathspec separator (nor a pathspec-looking
#: value be mistaken for scope).
#:
#: `-S`/`--gpg-sign` are deliberately ABSENT (review finding, P0): both are
#: OPTIONAL-argument git flags whose value must be ATTACHED (`-Skeyid`,
#: `--gpg-sign=keyid`) -- a standalone `-S`/`--gpg-sign` token consumes
#: NOTHING, so bucketing them here caused unconditional two-token
#: consumption that swallowed the NEXT real token (e.g. `-m`) as a phantom
#: value. `git commit -S -m x` then misread the commit MESSAGE as a
#: pathspec, silently narrowing/suppressing the sweep warning -- a
#: false-suppression, the worst-case direction under bound 4 below.
#: Standalone `-S`/`--gpg-sign` now falls through to the generic
#: short-cluster/long-flag handling in `_bt_commit_operand_scan`, which
#: marks the parse `ambiguous` (fails open to advisory, never to
#: suppression) exactly as the short-cluster docstring below already
#: claims for `-S`. The ATTACHED forms (`-Skeyid`, `--gpg-sign=keyid`) are
#: handled explicitly in `_bt_commit_operand_scan` as single, non-ambiguous
#: tokens.
_GIT_COMMIT_OPT_WITH_ARG = frozenset(
    {
        "-m", "--message", "-F", "--file", "-c", "--reedit-message",
        "-C", "--reuse-message", "--fixup", "--squash", "--author",
        "--date", "--cleanup", "--trailer", "--pathspec-from-file",
        "-t", "--template",
    }
)


#: Long `git commit` options taking NO value. Anything outside this set (and
#: outside `_GIT_COMMIT_OPT_WITH_ARG`) is an option this parser cannot prove
#: does not consume the following token -- SC-DR-020's bound 4 requires that
#: to fail OPEN (no suppression), never to be assumed operand-less.
_GIT_COMMIT_LONG_NO_ARG = frozenset(
    {
        "--all", "--patch", "--signoff", "--no-signoff", "--verbose",
        "--quiet", "--no-verify", "--verify", "--no-post-rewrite", "--amend",
        "--no-edit", "--edit", "--allow-empty", "--allow-empty-message",
        "--status", "--no-status", "--dry-run", "--short", "--branch",
        "--no-branch", "--porcelain", "--long", "--null", "--reset-author",
        "--interactive", "--only", "--include", "--no-gpg-sign",
        "--pathspec-file-nul",
    }
)

#: Short `git commit` flag letters taking NO value, for bundled-cluster
#: parsing. `m` (consumes the message), `S` (OPTIONAL keyid), and `u`
#: (OPTIONAL mode) are deliberately absent -- an optional-argument flag
#: cannot be resolved statically, so it renders the parse ambiguous.
_GIT_COMMIT_SHORT_NO_ARG = frozenset("apsvqnezoi")


#: `git add` options taking a SEPARATE-token value, so `_bt_git_add_own_
#: pathspec` cannot misread an option's value as a path operand. `--chmod`
#: is git's `=`-joined spelling in practice (`--chmod=+x`) but accepts the
#: separated form too; the `=`-joined form is skipped by the generic
#: leading-`-` arm and needs no entry here.
_GIT_ADD_OPT_WITH_ARG = frozenset({"--chmod", "--pathspec-from-file"})


#: A bundled short-flag cluster ending in `m` (`-am`, `-sm`, `-asm`) -- the
#: only bundled position where the message operand follows as its own token.
_BUNDLED_SHORT_M_RE = re.compile(r"^-[a-zA-Z]*m$")


#: A short-flag cluster carrying `o` (`-o`, `-om`, `-so`) -- `git commit -o`
#: bundles like any other short flag (`git commit -om "msg" a.txt` verified
#: live against git 2.50.1: commits only `a.txt`, leaves a peer's staged
#: `b.txt` in the index untouched).
_COMMIT_ONLY_SHORT_RE = re.compile(r"^-[a-zA-Z]*o[a-zA-Z]*$")


def _bt_commit_operand_scan(
    seg_tokens: List[str],
) -> Tuple[List[str], bool, bool, bool]:
    """Walk a `git commit` segment once and report
    ``(positional_operands, ambiguous, has_include, has_only)``.

    `has_only` is resolved HERE rather than by a caller re-scanning raw
    tokens, because only this walk performs option-value pair-skipping: a
    caller matching `-o` across bare tokens reads the MESSAGE in
    `git commit -m -o` as the `--only` flag and fakes scope out of it.

    The single parser behind BOTH `--`-keyed predicates below. SC-DR-020
    (`DoE-claude coordinator/docs/wiki/scoped-safety-commits.md`, token
    `SEPARATOR-IS-DISAMBIGUATION-NOT-SCOPE`) rules that `--` is git's
    revision/path DISAMBIGUATION token and carries no scope of its own:
    `git commit a.py -m x` and `git commit -m x -- a.py` are the same
    operation. A guard whose verdict turns on that token reads syntax where
    it means to read semantics -- the defect this module already hit at
    opposite polarity in `_bt_git_add_own_pathspec` (deny keyed ON the
    separator) and here (suppression keyed on it).

    `ambiguous` is True when the walk cannot prove the operand list: an
    option outside the known no-arg/with-arg sets may or may not consume the
    next token, and an optional-argument flag (`-S`, `-u`) cannot be resolved
    statically at all. Per SC-DR-020 bound 4 the asymmetry is deliberate --
    callers must fail OPEN to advisory on ambiguity, never to suppression: a
    spurious advisory costs attention, a spurious suppression costs a peer's
    work.

    Operands after a standalone `--` are returned as operands too, so a
    caller never needs to re-walk for the separator form.

    A surviving shell redirection token (`_bt_is_redirection_token`, e.g.
    `<<'MSG'`, `>/dev/null`, `2>&1`, `<file`) is NEVER counted as an operand,
    whether bare-positional or after `--` (2026-08-05 cockpit-incident fix):
    it is shell syntax the earlier redirection-stripping pass failed to
    remove, not a git pathspec, and treating it as one made an UNSCOPED
    `git commit` carrying any redirection read as explicitly scoped --
    silencing both `_bt_commit_has_explicit_pathspec` and (via this same
    walk) `_bt_commit_own_pathspec` on exactly the shape they exist to
    catch. A whitespace-separated redirection (`> /dev/null`, `2> log`,
    `< input`) tokenizes as a BARE operator token followed by a separate
    target token; `_bt_is_bare_redirection_token` distinguishes this case
    and the walk below also skips that following target token, or it
    would survive into `operands` untouched (2026-08-05 follow-up
    review, Finding 1).

    `-S`/`--gpg-sign` special-case (review finding, P0): both are
    OPTIONAL-argument flags whose value must be ATTACHED. A standalone `-S`
    or `--gpg-sign` token consumes nothing and cannot be resolved
    statically -- it renders the parse `ambiguous`, handled by the
    `--gpg-sign`/`-S`-absent `_GIT_COMMIT_OPT_WITH_ARG` (see that
    frozenset's docstring) falling through to the generic long-flag /
    short-cluster arms below. The ATTACHED forms (`-Skeyid`,
    `--gpg-sign=keyid`) are single tokens: `--gpg-sign=keyid` is handled by
    the generic `"=" in tok` long-flag arm below; `-Skeyid` needs its own
    check here because the generic short-cluster arm would otherwise
    decompose `keyid`'s letters as a bundle of unrelated single-char flags."""
    operands: List[str] = []
    ambiguous = False
    has_include = False
    has_only = False
    start_idx = _bt_git_subcommand_start_index(seg_tokens)
    if start_idx is None or seg_tokens[start_idx - 1] != "commit":
        return ([], True, False, False)
    i = start_idx
    n = len(seg_tokens)
    while i < n:
        tok = seg_tokens[i]
        if tok == "--":
            rest = seg_tokens[i + 1:]
            j = 0
            m = len(rest)
            while j < m:
                t = rest[j]
                if _bt_is_redirection_token(t):
                    if _bt_is_bare_redirection_token(t) and j + 1 < m:
                        j += 2
                    else:
                        j += 1
                    continue
                operands.append(t)
                j += 1
            break
        if tok in _GIT_COMMIT_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--"):
            base = tok.split("=", 1)[0]
            if base == "--include":
                has_include = True
            elif base == "--only":
                has_only = True
            elif "=" not in tok and tok not in _GIT_COMMIT_LONG_NO_ARG:
                ambiguous = True
            i += 1
            continue
        if tok.startswith("-S") and tok != "-S":
            # Attached optional-arg form (`-Skeyid`): a single, resolved
            # token that consumes nothing further and is not a bundled
            # short-flag cluster -- see the docstring above.
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            letters = tok[1:]
            if "i" in letters:
                has_include = True
            if "o" in letters:
                has_only = True
            if _BUNDLED_SHORT_M_RE.match(tok):
                letters = letters[:-1]
                i += 2
            else:
                i += 1
            if any(c not in _GIT_COMMIT_SHORT_NO_ARG for c in letters):
                ambiguous = True
            continue
        if _bt_is_redirection_token(tok):
            if _bt_is_bare_redirection_token(tok) and i + 1 < n:
                i += 2
            else:
                i += 1
            continue
        operands.append(tok)
        i += 1
    return (operands, ambiguous, has_include, has_only)


def _bt_commit_has_explicit_pathspec(seg_tokens: List[str]) -> bool:
    """True iff a `git commit` segment carries an explicit trailing
    `-- <paths>` scope (a standalone `--` separator with at least one
    operand after it), skipping option values so `-m -- ` shapes cannot
    fake one. This is the ratified default scoped-commit form
    (DoE `docs/wiki/scoped-safety-commits.md` SC-DR-008/SC-DR-015), so its
    presence is the suppression condition for the advisory below.

    `--pathspec-from-file[=<f>]`/`--pathspec-file-nul` also count as
    explicit scope (C1a Fix 1): per `man git-commit`(1), `--pathspec-from-
    file` is git's documented equivalent of naming the paths on the command
    line, so it selects the same index-bypassing, self-scoped mode as a
    trailing `-- <paths>`. This was UNDECIDED prior to this fix -- the
    option sat in `_GIT_COMMIT_OPT_WITH_ARG` purely so its (space-separated)
    value token was not misread as something else, and not-counting-as-scope
    was a side effect of that placement, not a considered ruling. Two shapes
    handled explicitly because neither was recognized before this fix: the
    `=`-joined form (`--pathspec-from-file=list.txt`) is a SINGLE token that
    never matches `_GIT_COMMIT_OPT_WITH_ARG` (written for the space-separated
    form only); `--pathspec-file-nul` takes no value at all and was absent
    from every option set in this module.

    `-o`/`--only` (bare or bundled, `-om`) counts for the SAME reason, raised
    by doe-claude-em against the rule the paragraph above states: it selects
    git's identical index-bypassing self-scoped mode. Verified live against
    git 2.50.1 -- `git commit -o a.txt -m x` with a peer's `b.txt` staged
    commits `a.txt` alone and leaves `b.txt` staged. A no-paths `--only` is
    not a hole: git rejects it outright (`fatal: No paths with
    --include/--only does not make sense.`), and the one legal no-paths
    spelling, `--amend --only`, reuses HEAD's tree rather than the index.

    NEGATIVE SPEC -- `-i`/`--include` is the near-neighbour and is NOT scope:
    it MERGES the named paths INTO the staged index and commits the union.
    Verified live on the same repo: `git commit -i -m x a.txt` landed BOTH
    `a.txt` and the peer's staged `b.txt`. That is precisely the sweep this
    advisory exists to catch, so `--include` must keep firing.

    SC-DR-020 (doe-claude-em, 2026-08-04, `5a5fbe89f`): a BARE POSITIONAL
    pathspec counts too -- `git commit a.py -m x` is the same operation as
    `git commit -m x -- a.py`, verified live on git 2.54.0 (with a peer's
    `b.txt` also staged, `a.py` lands alone and `b.txt` stays staged). The
    separator was never the thing carrying scope. SC-DR-008's `-- <paths>`
    form stays canonical, enforced by prose and by `ceremony.scoped_git_
    commit` being the preferred path -- not by advising a form git already
    treats as safe. The four bounds that survive the change: `--include`
    still suppresses nothing, an operand-less commit still fires
    (SC-DR-017), `--pathspec-from-file` still counts as scope, and an
    AMBIGUOUS parse fails open to the advisory rather than to silence."""
    for tok in seg_tokens:
        if tok == "--pathspec-file-nul" or tok == "--pathspec-from-file" or (
            tok.startswith("--pathspec-from-file=")
        ):
            return True
    operands, ambiguous, has_include, has_only = _bt_commit_operand_scan(
        seg_tokens
    )
    if has_include:
        return False
    # `-o`/`--only` is scope in its own right even with no operands visible
    # to this walk -- kept ahead of the ambiguity bail so the pre-SC-DR-020
    # behaviour for that flag is bit-identical.
    if has_only:
        return True
    if ambiguous:
        return False
    if _bt_commit_has_sweep_all_flag(seg_tokens):
        return False
    return bool(operands)


# ---------------------------------------------------------------------------
# C7 (docs/plans/2026-08-01-advisory-firing-shape-predicate.md) -- the index
# probe that escalates ONE shape of the check below (the compound `git add
# -- <paths> && git commit -m "x"` bare-commit-half) from advisory to deny.
# PM Ruling 2: escalate-only, no index-based suppression exists anywhere in
# this plan; `-a`/`-am`/`--all` are excluded from index-based reasoning
# unconditionally. See `_bt_c7_index_holds_foreign_paths`'s own docstring for
# the monotonicity argument and the deliberate compound-shape-only scope cut.
# ---------------------------------------------------------------------------

#: A bundled short-flag cluster carrying `a` (`-a`, `-am`, `-sa`, `-asm`, ...)
#: or the long form `--all` -- PM Ruling 2, finding 6: these stage every
#: modified TRACKED file from the WORKTREE at commit time, not the index, so
#: a clean-index probe would read "safe" for a command about to sweep
#: everything. Excluded from C7's index-based reasoning unconditionally.
_COMMIT_BUNDLED_SHORT_RE = re.compile(r"^-[a-zA-Z]+$")


def _bt_commit_has_sweep_all_flag(seg_tokens: List[str]) -> bool:
    """True iff a `git commit` segment carries `-a`/`--all` (bare or bundled
    into a short-flag cluster, e.g. `-am`, `-sa`), stopping at the segment's
    own `--` pathspec separator (a flag can never appear in the pathspec
    tail) and skipping option-with-arg VALUE tokens so a value token can
    never be misread as a bundled flag."""
    i = 0
    n = len(seg_tokens)
    while i < n:
        tok = seg_tokens[i]
        if tok == "--":
            break
        if tok == "--all":
            return True
        if tok in _GIT_COMMIT_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--"):
            i += 1
            continue
        if _COMMIT_BUNDLED_SHORT_RE.match(tok) and "a" in tok[1:]:
            return True
        i += 1
    return False


def _bt_commit_has_amend_flag(seg_tokens: List[str]) -> bool:
    """True iff a `git commit` segment carries `--amend` (stopping at the
    segment's own `--` pathspec separator, since a flag can never appear in
    the pathspec tail -- this also guards against a file literally named
    `--amend` sitting in the pathspec tail reading as the flag). No
    bundled short form of `--amend` exists, so a plain token scan bounded
    by the separator is sufficient.

    `check_git_commit_safe_commit_advise` calls this from TWO sites with
    different pathspec state, and neither can assume the other's
    precondition:

    - The amend-ownership gate (example-retrieval-repo-em cross-repo memo, 2026-08-15,
      Finding 2) calls this BEFORE the explicit-pathspec early return, so a
      `True` here may still carry `--only` or an explicit `-- <paths>` --
      pathspec scoping bounds which files change, not whose commit
      `--amend` is about to overwrite, so the gate must fire on the scoped
      form too.
    - The remediation-text selection later in the same function calls this
      AFTER that early return has already exited on any scoped form, so a
      `True` there always means the bare-`--amend`-with-index shape."""
    for tok in seg_tokens:
        if tok == "--":
            break
        if tok == "--amend":
            return True
    return False


def _bt_commit_own_pathspec(seg_tokens: List[str]) -> Optional[List[str]]:
    """Extract WHICH staged paths a `git commit` segment will actually take,
    for Check 5 (`check_validate_commit`) to INTERSECT against the full
    staged index -- a DIFFERENT question from
    `_bt_commit_has_explicit_pathspec`'s WHETHER-scoped boolean, and it
    requires its own extractor rather than reusing that boolean as a
    suppressor.

    NEGATIVE SPEC, restated here because it is load-bearing for the caller:
    `_bt_commit_has_explicit_pathspec` answers "did the operator name a
    scope?", not "which staged paths will this commit take?". Using it as a
    suppressor would silently unwarn `git commit -- <a live peer's file>` --
    precisely the class Check 5 exists to catch (a recorded live incident
    shape). This function instead returns the actual pathspec tokens so the
    caller can re-derive the true committed-path set via its own `git diff
    --cached --name-only -- <pathspec>` call (a pathspec may be a directory
    or glob, not only a literal path, so this function's return value is an
    input to that follow-up call, not the final answer by itself). This is
    NOT the "second pathspec predicate" this chunk otherwise forbids -- it
    answers a different question (WHICH paths, not WHETHER scoped).

    Returns `None` when the actually-committed path set is NOT statically
    knowable, or is knowably "the whole index" -- both mean the caller MUST
    fall back to warning on the full staged list, never to silence:

    - `-a`/`--all` (`_bt_commit_has_sweep_all_flag`): stages from the
      WORKTREE at commit time, not the index -- returns `None`
      unconditionally, even if a trailing pathspec is also present.
    - `--pathspec-from-file[=<f>]`/`--pathspec-file-nul`: the path list
      lives in a file (or stdin) this guard does not read -- returns `None`
      (never guesses at file contents).
    - `-i`/`--include` (bare, long, or bundled, e.g. `-si`): MERGES the
      named paths INTO the staged index and commits the union -- returns
      `None` (must NOT narrow; verified live against git 2.50.1 alongside
      `_bt_commit_has_explicit_pathspec`'s own `-i` note).
    - No trailing `-- <paths>` at all: nothing to narrow by -- returns
      `None`.

    - An AMBIGUOUS parse (an unrecognized option, or an optional-argument
      flag like `-S`/`-u`, either of which may consume the next token):
      returns `None`. SC-DR-020 bound 4 -- narrowing on a mis-parsed operand
      list would drop a genuinely-staged foreign path out of the warning,
      which is the failure this whole check exists to prevent.

    Returns the (non-empty) list of pathspec tokens when none of the above
    applies -- this covers `-o`/`--only` (which bypasses the index for those
    paths), the `-- <paths>` separator form, and a BARE POSITIONAL pathspec
    (SC-DR-020: `git commit a.py -m x` is the same operation as
    `git commit -m x -- a.py`; `man git-commit` treats a trailing pathspec
    with neither `-i` nor `-a` identically to `-o`). All three narrow the
    actually-committed set the same way and none needs distinguishing here.
    Sharing `_bt_commit_operand_scan` with the suppression predicate is
    deliberate: two independent `--`-keyed walks is how this module came to
    hold the same defect at both polarities."""
    if _bt_commit_has_sweep_all_flag(seg_tokens):
        return None
    for tok in seg_tokens:
        if tok == "--pathspec-file-nul" or tok == "--pathspec-from-file" or (
            tok.startswith("--pathspec-from-file=")
        ):
            return None
    operands, ambiguous, has_include, _has_only = _bt_commit_operand_scan(
        seg_tokens
    )
    if has_include or ambiguous or not operands:
        return None
    return operands


def _bt_git_add_own_pathspec(seg_tokens: List[str]) -> Optional[List[str]]:
    """Extract the pathspec tokens from a `git add` segment, so C7's probe can
    compute "the command's own pathspec" for a compound `git add <paths> &&
    git commit -m "x"` shape. `None` if this segment is not a git-add
    invocation, or if it is one but names no paths of its own (an unscoped
    add, e.g. `git add -A` / `git add -u`, contributes no "own" scope to the
    compound command either -- treated the same as no preceding add at all).

    POSITIONAL operands count, not only a `-- <paths>` separator. Requiring
    the separator inverted the guard: `git add -- a.py && git commit -m x`
    (the careful spelling) escalated to DENY, while `git add a.py && git
    commit -m x` (the overwhelmingly common one, and the shape that actually
    swept a peer's staged work in practice) degraded to advisory. The prior
    docstring justified this by equating a separator-less add with `git add
    -A`, but those are different: `-A` is genuinely unbounded, whereas
    `git add a.py` is exactly as scoped as `git add -- a.py`. Only the
    ABSENCE of path operands means unscoped.

    A flag-bounded add (`git add -A a.py`, `git add -u src/`) still yields
    its operands: git bounds `-A`/`-u` BY the pathspec when one is given, so
    the monotonicity argument in the caller's docstring holds unchanged.

    `--pathspec-from-file` yields `None` (fail open, no escalation): the
    paths live in a file this predicate does not read, so no set-difference
    is computable and a deny would rest on an unknown pathspec.

    A trailing `-- <paths>` UNIONS with any positional operands that
    preceded it (review finding, P2), rather than replacing them: `git add`
    has no revision argument, so `--` there means only "stop parsing
    options" -- `git add foo.py -- bar.py` genuinely adds BOTH `foo.py` and
    `bar.py`. Returning only the post-separator tokens under-counted `own_
    pathspec`, which could make the caller's own just-added `foo.py` appear
    in the caller's "foreign" set and trigger a spurious deny."""
    if _bt_git_resolved_subcommand(seg_tokens) != "add":
        return None
    try:
        start = seg_tokens.index("add") + 1
    except ValueError:
        return None
    paths: List[str] = []
    i = start
    n = len(seg_tokens)
    while i < n:
        tok = seg_tokens[i]
        if tok == "--":
            paths.extend(seg_tokens[i + 1:])
            break
        if tok == "--pathspec-from-file" or tok.startswith(
            "--pathspec-from-file="
        ):
            return None
        if tok in _GIT_ADD_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("-") and tok != "-":
            i += 1
            continue
        paths.append(tok)
        i += 1
    return paths or None


def _bt_git_dash_c_value(tokens: List[str]) -> Optional[str]:
    """Extract the LAST `-C <dir>` global-option value from a (wrapper-
    peeled) `git ...` invocation, so C7's probe runs against the SAME
    repo/cwd the command itself targets rather than the guard process's own
    cwd. Mirrors `_bt_git_resolved_subcommand`'s global-option walk (added
    alongside it rather than folded in, so C1a's landed walk stays
    untouched). Real git chains multiple `-C` tokens (`-C a -C b` == cwd
    `a/b`); this returns only the LAST raw value -- a documented
    simplification, safe because a probe-cwd miss fails OPEN (no deny),
    never fails closed."""
    tokens = _bt_peel_wrapper_prefix(tokens)
    if not tokens or not _bt_token_matches_binary(tokens[0], "git"):
        return None
    i = 1
    n = len(tokens)
    result: Optional[str] = None
    while i < n:
        tok = tokens[i]
        if tok == "-C" and i + 1 < n:
            result = tokens[i + 1]
            i += 2
            continue
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
            if tok in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                i += 1
                continue
            break
        break
    return result


def _bt_git_index_file_env(tokens: List[str]) -> Optional[str]:
    """Extract a leading `GIT_INDEX_FILE=<path>` assignment ahead of the git
    binary (BX-13 peel vocabulary, `_bt_peel_wrapper_prefix`'s own token
    set), so C7's probe honors a non-default index the command itself sets
    rather than the guard process's own -- an env-scoped `git diff --cached`
    against the WRONG index would probe state the command never touches."""
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if _ENV_ASSIGNMENT_TOKEN_RE.match(tok):
            name, _, value = tok.partition("=")
            if name == "GIT_INDEX_FILE":
                return value
            i += 1
            continue
        if any(_bt_token_matches_binary(tok, w) for w in _GIT_WRAPPER_PREFIX_WORDS):
            i += 1
            continue
        break
    return None


def _bt_c7_index_holds_foreign_paths(
    seg_tokens: List[str],
    segments: List[Tuple[List[str], bool]],
    seg_index: int,
) -> bool:
    """C7's escalation predicate for one bare `git commit` segment: True iff
    a PRECEDING `git add -- <paths>` segment exists earlier in the SAME
    command, and the staged index holds paths outside that pathspec --
    foreign work (a concurrent session's staging, or an unrelated earlier
    `git add`) sitting in the shared index alongside whatever this command's
    own `add` will put there.

    Scope decision (executor call, C7): escalation is attempted ONLY for the
    compound `git add -- <paths> && git commit -m "x"` shape (AC1's
    bare-commit-half). A solo bare `git commit` with NO preceding `add`
    segment anywhere in the command carries no "own pathspec" for the
    set-difference formula below to compare against -- the monotonicity
    argument only reasons about a `git add` that is PART OF THIS COMMAND and
    has not run yet; it does not apply when there is no such add at all. This
    check also receives no `cwd` from its own caller, so a solo bare
    commit's probe would read whatever the ambient process cwd's index holds
    with no command-level signal at all to tell session-own staging apart
    from a peer's. That shape stays advisory-only (unchanged, still fires
    unconditionally per AC1) rather than probing indiscriminately.

    Monotonicity argument (why this is computable at PreToolUse, BEFORE the
    `git add` segment has actually run): `index_post ⊇ index_pre` and
    `index_post \\ index_pre ⊆ the command's own pathspec`, where "the
    command's own pathspec" is the UNION of every preceding `git add --
    <paths>` segment in this same compound command (each such `add` only
    ever adds the paths it was given, and a command may run more than one
    before its commit), so "pre-add index holds no foreign paths" ⟺
    "post-add index holds no foreign paths" -- probing the PRE-add index
    (the only one available at PreToolUse, since this check runs before the
    tool call executes) is equivalent to probing the index the commit will
    actually see.

    `-a`/`-am`/`--all` are excluded unconditionally (PM Ruling 2, finding
    6) -- see `_bt_commit_has_sweep_all_flag`.

    Fails OPEN on any probe failure (timeout, non-zero exit, unresolvable
    `git`, bare/detached repo): returns `False` -- the OPPOSITE posture from
    the advisory's own "probe failure fires", since a false deny blocks real
    work on a guard-process error while a false silence merely returns to
    today's advisory-only baseline.
    """
    if _bt_commit_has_sweep_all_flag(seg_tokens):
        return False
    own_pathspec: List[str] = []
    for prior_tokens, _pipe in segments[:seg_index]:
        add_paths = _bt_git_add_own_pathspec(prior_tokens)
        if add_paths:
            own_pathspec.extend(add_paths)
    if not own_pathspec:
        return False
    cwd = _bt_git_dash_c_value(seg_tokens)
    extra_env: Optional[Dict[str, str]] = None
    index_file = _bt_git_index_file_env(seg_tokens)
    if index_file:
        extra_env = {"GIT_INDEX_FILE": index_file}
    rc_full, out_full = _run_git(
        ["diff", "--cached", "--name-only"], cwd=cwd, extra_env=extra_env
    )
    if rc_full != 0:
        return False
    full_paths = {ln for ln in out_full.splitlines() if ln}
    if not full_paths:
        return False
    rc_scoped, out_scoped = _run_git(
        ["diff", "--cached", "--name-only", "--", *own_pathspec],
        cwd=cwd,
        extra_env=extra_env,
    )
    if rc_scoped != 0:
        return False
    scoped_paths = {ln for ln in out_scoped.splitlines() if ln}
    return bool(full_paths - scoped_paths)


#: Matches a `Session-Id: <sid>` trailer line exactly -- the shape
#: `ceremony.scoped_git_commit` stamps on every commit it authors (see
#: `85d55a001`'s own commit message for a live example, and
#: `ops/ceremony/branch_resolution.py`'s `_session_commit_log`/
#: `_trailer_reliable`, which already treat this trailer as authoritative
#: commit provenance elsewhere in the engine). Anchored to the whole line
#: (`^...$` under MULTILINE) so a session id that is a PREFIX of another
#: commit's trailer value can never read as a match -- unlike the plain
#: substring `--grep=Session-Id: <sid>` the rest of the engine uses for
#: RECALL (where a false-positive widen is the safe direction), this
#: predicate feeds a fail-CLOSED deny, where a false-positive MATCH is the
#: unsafe direction.
_SESSION_ID_TRAILER_RE = re.compile(r"(?m)^Session-Id:\s*(\S+)\s*$")


def _bt_head_commit_amend_provenance(
    cwd: Optional[str], session_id: str
) -> Optional[Tuple[str, str, bool]]:
    """Probe HEAD's short sha, subject, and whether it carries THIS
    session's `Session-Id:` trailer -- ONE `_run_git` call (`log -1
    --format=%h\\x02%s\\x02%B`), so an amend-ownership check spends exactly
    one slot of this dispatch's probe budget (`_GIT_PROBE_BUDGET_SECONDS`),
    not a second one layered on top of the sibling index probes this same
    check function may already have spent.

    Returns `None` when HEAD is not knowable at all: the probe declined
    unspawned (`_GIT_PROBE_BUDGET_SPENT_RC`), timed out (`rc == -1`), the
    repo has no commits yet, or the format string failed to split into
    exactly three fields. The CALLER decides what `None` means for
    enforcement (see `check_git_commit_safe_commit_advise`'s amend gate) --
    this predicate only reports what it could observe.

    Returns `(short_sha, subject, owned)` otherwise. `owned` is True iff
    the full message contains an EXACT `Session-Id: <session_id>` trailer
    line. An empty `session_id` can never read as owned: `owned` starts
    `False` and is only set from an equality comparison against
    `session_id`, so an empty `session_id` would need an empty CAPTURED
    trailer value to match, and `_SESSION_ID_TRAILER_RE`'s `\\S+` can never
    capture an empty string -- there is no input that makes an unresolved
    session id read as "mine"."""
    rc, out = _run_git(["log", "-1", "--format=%h\x02%s\x02%B"], cwd=cwd)
    if rc != 0 or not out:
        return None
    parts = out.split("\x02", 2)
    if len(parts) != 3:
        return None
    short_sha = parts[0].strip()
    subject = parts[1].strip()
    body = parts[2]
    if not short_sha:
        return None
    owned = False
    if session_id:
        m = _SESSION_ID_TRAILER_RE.search(body)
        owned = bool(m and m.group(1) == session_id)
    return (short_sha, subject, owned)


def _bt_solo_bare_commit_index_nonempty(
    seg_tokens: List[str],
    segments: List[Tuple[List[str], bool]],
    seg_index: int,
) -> bool:
    """True iff `seg_tokens` is a solo bare `git commit` -- no preceding
    `git add -- <paths>` anywhere earlier in the same command (the shape
    `_bt_c7_index_holds_foreign_paths` deliberately declines to probe, see
    its own docstring) -- AND the index it would commit currently holds
    ANY staged path.

    Unlike C7's set-difference (which can name "foreign" paths because it
    has the command's own `add` pathspec to diff against), a solo bare
    commit supplies NO pathspec at all in this operation -- there is
    nothing here to subtract, so nothing here can be verified as this
    command's own staging. On a shared tree the guard cannot tell "I
    staged this myself in an earlier command" from "a peer staged this
    concurrently"; the empirical cost of guessing wrong (fourth recorded
    recurrence, 2026-08-15, `state/lessons/2026-08-03-git-add-mine-then-
    bare-git-commit-sweeps-70d1438f8f01.yaml`) is silent cross-session
    mis-attribution with no rewrite remedy under live peers. A non-empty
    index is therefore treated as unverifiable, not as "probably mine".

    Excludes `-a`/`-am`/`--all` only (worktree-sourced, invisible to an
    index probe -- same exclusion C7 already applies). `--amend` is NOT
    excluded: bare `git commit --amend -m "x"` (no `-a`, no pathspec)
    commits the CURRENT INDEX amended onto HEAD, not HEAD's tree -- only
    `--amend --only` with an explicit pathspec restricts to HEAD's tree,
    and that shape already exits earlier via
    `_bt_commit_has_explicit_pathspec` (`--only` counts as scope). A prior
    version of this function excluded bare `--amend` on the false premise
    that amend always reuses HEAD's tree; corrected 2026-08-15 (P1,
    coordinator:code-reviewer) -- on a shared dirty index, bare `--amend`
    sweeps a peer's staged work into the amended commit exactly like the
    bare-commit shape this check exists to deny. Fails OPEN on any probe
    failure, same posture as C7's own probe.
    """
    if _bt_commit_has_sweep_all_flag(seg_tokens):
        return False
    for prior_tokens, _pipe in segments[:seg_index]:
        # ANY preceding `git add` segment -- scoped or not (`-A`/`-u`/
        # `--pathspec-from-file` included) -- takes this command out of
        # "solo bare commit" and into the compound bare-commit-half shape
        # C7's own probe already owns (advisory-only when it cannot
        # extract a pathspec, per that check's own fail-open posture).
        # Checking `_bt_git_add_own_pathspec(...)` truthiness alone here
        # (rather than "is this an `add` segment at all") wrongly treated
        # `git add -A && git commit` as a solo bare commit -- an unscoped
        # add still means the caller DID stage something in this
        # operation, just not precisely enough for C7's set-difference.
        if _bt_git_resolved_subcommand(prior_tokens) == "add":
            return False  # compound shape -- C7's own probe handles this
    cwd = _bt_git_dash_c_value(seg_tokens)
    extra_env: Optional[Dict[str, str]] = None
    index_file = _bt_git_index_file_env(seg_tokens)
    if index_file:
        extra_env = {"GIT_INDEX_FILE": index_file}
    rc, out = _run_git(["diff", "--cached", "--name-only"], cwd=cwd, extra_env=extra_env)
    if rc != 0:
        return False
    return bool([ln for ln in out.splitlines() if ln])


def _bt_sweep_all_holds_unverifiable_paths(seg_tokens: List[str]) -> bool:
    """C1's worktree-union escalation predicate: the sweep-all (`-a`/`-am`/
    `--all`) mirror of `_bt_solo_bare_commit_index_nonempty`, not of C7's
    set-difference above -- `-a` supplies no pathspec of its own, so there
    is nothing for a set-difference to subtract and nothing here provable
    as this command's own staging.

    Compound-shape decision (`git add -- mine.py && git commit -am "x"`):
    fires REGARDLESS of any preceding `add` segment. Unlike C7, a scoped
    `add` does not make the `-a` swept set provably own -- `-a` reaches
    worktree paths no preceding `add` pathspec bounds. This is why this
    signature takes only `seg_tokens`, not `segments`/`seg_index`: the
    decision does not depend on surrounding segments.

    NARROWED (PM ruling, 2026-08-15, `docs/plans/2026-08-15-blanket-gits-
    proffer-the-scoped-commit-helper.md` § C1): gated behind
    `_is_hazard_repo`, not a blanket deny. Escalating advisory to deny
    removes the message from `_advisory_dedupe`'s terse-repeat path, which
    applies to non-blocking advisories only; on a shared tree at this
    box's documented 50-70-concurrent-LLM load norm the tree is
    essentially never clean, so an unnarrowed deny would print its full
    body on nearly every `git commit -am`. Narrowing to hazard repos keeps
    identical protection wherever peers actually exist (the ~/.claude
    meta-repo, and every repo this machine's fleet registry tracks -- see
    `_is_hazard_repo`'s own docstring) without that cost everywhere else.

    1. Fires ONLY when `_bt_commit_has_sweep_all_flag(seg_tokens)` is True
       -- the one predicate that INVERTS that gate; the sibling C7 and
       solo-bare predicates above keep short-circuiting on it.
    2. Resolves cwd via `_bt_git_dash_c_value` and env via
       `_bt_git_index_file_env` -- reused, not re-derived (AC4).
    3. The hazard check runs against that same resolved cwd (falling back
       to the guard process's own cwd when no `-C` was given), spending no
       probe of its own: `_is_hazard_repo` samefile-compares against
       already-known repo roots, it does not need a fresh `rev-parse`.
    4. Swept set = UNION of `git diff --cached --name-only` (staged) and
       `git diff --name-only` (tracked-modified, worktree) -- matches the
       empirically verified semantics of `git commit -a`. Untracked paths
       excluded: `-a` does not touch them.
    5. Non-empty union -> True (escalate). Empty -> False.

    Fails OPEN on ANY probe failure -- rc != 0 on either `git diff` call,
    explicitly INCLUDING `_run_git` returning `_GIT_PROBE_BUDGET_SPENT_RC`
    (127) once `_GIT_PROBE_BUDGET_SECONDS` is spent for this dispatch:
    "a false deny blocks work on a guard-process error while a false
    silence merely returns to today's advisory-only baseline"
    (`docs/plans/2026-08-01-advisory-firing-shape-predicate.md`, C7). Same
    posture as `_bt_c7_index_holds_foreign_paths` and
    `_bt_solo_bare_commit_index_nonempty` above -- inherited, not
    reinvented.

    The `-a`/`-am`/`--all`/`--amend` short-circuit this predicate INVERTS
    is itself provenanced from `state/lessons/2026-08-03-git-add-mine-
    then-bare-git-commit-sweeps-70d1438f8f01.yaml` -- the fourth recorded
    recurrence of a shared-index sweep silently mis-attributing a peer's
    work, which is what left the two index-based predicates' unconditional
    `-a` exclusion (and therefore this gap) in place."""
    if not _bt_commit_has_sweep_all_flag(seg_tokens):
        return False
    cwd = _bt_git_dash_c_value(seg_tokens)
    if not _is_hazard_repo(cwd or os.getcwd()):
        return False
    extra_env: Optional[Dict[str, str]] = None
    index_file = _bt_git_index_file_env(seg_tokens)
    if index_file:
        extra_env = {"GIT_INDEX_FILE": index_file}
    rc_staged, out_staged = _run_git(
        ["diff", "--cached", "--name-only"], cwd=cwd, extra_env=extra_env
    )
    if rc_staged != 0:
        return False
    rc_worktree, out_worktree = _run_git(
        ["diff", "--name-only"], cwd=cwd, extra_env=extra_env
    )
    if rc_worktree != 0:
        return False
    swept = {ln for ln in out_staged.splitlines() if ln} | {
        ln for ln in out_worktree.splitlines() if ln
    }
    return bool(swept)


def check_git_commit_safe_commit_advise(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 5 -- a `git commit` that names NO scope advised toward
    the ratified scoped-commit default. (Function name predates the offer
    correction below and is kept because it is the registry key; the offer
    is no longer `coordinator-safe-commit`.)

    Two properties this check exists to hold, both of them regressions
    found in the field (doe-claude-em, 2026-07-29 cross-repo memo, after
    four ignored firings):

    - **It never fires on the ratified, both-halves scoped form.** A
      command already carrying `-- <paths>` on its commit segment (or
      `--pathspec-from-file`/`--pathspec-file-nul`, see
      `_bt_commit_has_explicit_pathspec`) IS the ratified default
      (`git add -- <paths> && git commit -m "x" -- <paths>`, DoE
      `docs/wiki/scoped-safety-commits.md` SC-DR-015, which ratifies
      pathspec on BOTH halves, not one); advising it toward anything
      spends the band's credibility on the case that needs it least, and
      the resulting nag-fatigue does not stay scoped to one check. This is
      NOT a claim that a bare-commit-half `git add -- <paths> && git commit
      -m "x"` is doctrinally correct -- per PM ruling (`docs/plans/
      2026-08-01-advisory-firing-shape-predicate.md`, Ruling 1) it is not:
      it commits the whole shared index, not just the added paths, and the
      guard is correct to keep firing on it. No index probe of any kind
      suppresses this check anywhere -- the bare-commit-half shape stays
      unconditionally firing.
    - **The offer is never a scope-narrowing command.** The earlier text
      printed `coordinator-safe-commit "<subject>"`, which (a) silently
      dropped any `-- <pathspec>` the caller had typed, (b) named a form
      the shared wiki marks *deprecated*, and (c) has no flag on the target
      CLI to take a pathspec back, so following it through its own
      multi-session abort path bottomed out in `git add -A`. An offer must
      be better than what it replaces.

    Advisory only, never a rewrite: the caller's paths are not knowable
    from the command text, so the message states the shape and lets the
    operator fill it -- silently synthesizing a scope inside
    `updatedInput` is exactly the kind of clever, unverified substitution
    `check_offer_git_c`'s own docstring warns against.

    C7 (docs/plans/2026-08-01-advisory-firing-shape-predicate.md, PM Ruling
    2): the bare-commit-half shape (`git add -- <paths> && git commit -m
    "x"`, still unconditionally firing per the paragraph above) escalates
    from advisory to DENY when `_bt_c7_index_holds_foreign_paths` finds the
    staged index holds paths OUTSIDE that command's own `git add` pathspec
    -- a peer's staged work sitting in the shared index alongside what this
    commit is about to sweep under one subject. A bare commit against an
    index the session staged in full on its own stays advisory (allowed),
    so the deny never fires on the safe single-session case. This is the
    ONLY index-based reasoning anywhere in this plan and it only ESCALATES,
    never suppresses -- see that function's own docstring for the
    monotonicity argument, the deliberate compound-shape-only scope cut, the
    `-a`/`-am`/`--all` exclusion, and the fail-open-on-probe-failure
    posture -- a probe failure here never denies (a false deny would block
    real work on a guard-process error), unlike a fail-closed hard-deny
    guard elsewhere in this package where a probe failure denies (a false
    silence there would be the worse outcome for that guard's own class).

    AMEND GATE (example-retrieval-repo-em cross-repo memo, 2026-08-15): a segment
    carrying `--amend` is checked for HEAD ownership FIRST, ahead of the
    explicit-pathspec early return above -- a scoped `--amend -- <paths>`
    bounds which files change but still overwrites whatever commit is at
    HEAD, message and authorship both, which pathspec scoping says nothing
    about. This is the ONLY fail-CLOSED reasoning in this function (every
    other branch above fails open on probe failure); see
    `_bt_head_commit_amend_provenance`'s own docstring for why an amend's
    blast radius inverts the posture. Gated by its own override key,
    `COORDINATOR_ALLOW_GIT_COMMIT_AMEND`, independent of
    `COORDINATOR_ALLOW_GIT_COMMIT_BARE`.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return None
    segments = _bt_segments_from_tokens_with_pipe_flag(tokens)
    for seg_index, (seg_tokens, pipe_before) in enumerate(segments):
        # A piped segment is still inspected for scope (unlike
        # `check_multiprobe_banner_rewrite`, where `pipe_before` genuinely
        # means "composed, don't offer a rewrite" -- that rationale never
        # applied here, since this check is advisory-only and has no
        # rewrite to get wrong). Verified live pre-fix:
        # `echo y | git commit -m "x"` (bare, unscoped, index-wide) was
        # silent; only an EMPTY segment is skipped now.
        if not seg_tokens:
            continue
        if _bt_git_resolved_subcommand(seg_tokens) != "commit":
            continue
        # Amend-ownership gate (example-retrieval-repo-em cross-repo memo, Finding 2):
        # evaluated BEFORE the explicit-pathspec early return below, so a
        # SCOPED amend (`--amend --only -- <paths>`) no longer exits this
        # check silently -- pathspec scoping bounds which FILES change, it
        # says nothing about whose commit `--amend` is about to overwrite.
        # Own key (`COORDINATOR_ALLOW_GIT_COMMIT_AMEND`), independent of
        # `COORDINATOR_ALLOW_GIT_COMMIT_BARE` below -- amend's blast radius
        # (destroys a peer's message/authorship at HEAD, no rewrite remedy
        # under live peers) differs from the bare-commit shape that key
        # governs, so one must not silently unlock the other.
        if _bt_commit_has_amend_flag(seg_tokens) and not _override(
            "COORDINATOR_ALLOW_GIT_COMMIT_AMEND"
        ):
            provenance = _bt_head_commit_amend_provenance(
                _bt_git_dash_c_value(seg_tokens), session_id
            )
            owned = provenance is not None and provenance[2]
            if not owned:
                _amend_note = operator_override_note(
                    "COORDINATOR_ALLOW_GIT_COMMIT_AMEND", payload=payload, git_root=git_root
                )
                if provenance is not None:
                    head_sha, head_subject, _owned = provenance
                    head_subject = _truncate_to_budget(head_subject, 40)
                    return _deny(
                        (
                            "Deny: '--amend' rewrites HEAD (%s %s), not "
                            "provably yours -- replaces its message/"
                            "authorship; without --only, also commits "
                            "staged.\n\n"
                            "Use instead:\n"
                            '  git notes add -f -m "<correction>" %s'
                            % (head_sha, head_subject, head_sha)
                        )
                        + ("\n\n%s" % _amend_note if _amend_note else "")
                    )
                return _deny(
                    (
                        "Deny: '--amend' rewrites HEAD, not provably yours "
                        "(its provenance could not be verified) -- "
                        "replaces its message/authorship; without --only, "
                        "also commits staged.\n\n"
                        "Use instead:\n"
                        '  git notes add -f -m "<correction>" <sha>'
                    )
                    + ("\n\n%s" % _amend_note if _amend_note else "")
                )
        if _override("COORDINATOR_ALLOW_GIT_COMMIT_BARE"):
            return None
        if _bt_commit_has_explicit_pathspec(seg_tokens):
            return None
        subject = None
        for i, tok in enumerate(seg_tokens):
            # `-m`, `--message`, and the bundled short forms (`-am`, `-sm`,
            # ...) where `m` must be last for the operand to follow.
            if (
                tok in ("-m", "--message") or _BUNDLED_SHORT_M_RE.match(tok)
            ) and i + 1 < len(seg_tokens):
                subject = seg_tokens[i + 1]
                break
        # `subject_operand` is interpolated into the remediation at
        # most once (the `git add ... && git commit` suggestion below) --
        # the `scoped-git-commit` suggestion reuses the SAME `-m` argument
        # by reference ("same subject") rather than re-printing it, so the
        # subject text itself never appears twice in one advisory.
        subject_operand = shlex.quote(subject) if subject else '"<subject>"'
        # `--amend` reaching here always carries no `--only` and no explicit
        # pathspec (both exit earlier via `_bt_commit_has_explicit_pathspec`),
        # so it is always the risky bare-`--amend`-with-index shape -- the
        # remediation below must say the amend-specific thing rather than
        # the scoped-new-commit shape, which does not amend anything (DoE
        # cross-repo memo, example-retrieval-repo-em, 2026-08-15, "amend has no safe
        # helper and the scope advisory reads generic", Findings 2-3). This
        # branches on TEXT ONLY -- the deny/advisory/allow decision above is
        # unchanged.
        is_amend = _bt_commit_has_amend_flag(seg_tokens)
        # Escalate to DENY when the compound bare-commit-half's own `git
        # add` pathspec leaves foreign staged paths behind
        # -- see `_bt_c7_index_holds_foreign_paths`'s own docstring for the
        # monotonicity argument and the scope cut. Every other bare-commit
        # shape (no preceding `add`, or `-a`/`-am`/`--all` present) stays
        # advisory, unchanged.
        _commit_bare_note = operator_override_note(
            "COORDINATOR_ALLOW_GIT_COMMIT_BARE", payload=payload, git_root=git_root
        )
        # Amend-specific remediation (see `is_amend` comment above): the
        # scoped-new-commit shapes below never apply to `--amend`, so an
        # amending command gets this text instead of any of the three
        # branches' ordinary body. Severity (deny vs advisory) is decided
        # by the branch below exactly as before -- only the body text
        # differs. `git notes add -f -m "<correction>" <sha>` repairs a
        # commit's record without rewriting it, so it stays safe under a
        # live peer; there is deliberately no rewrite-HEAD alternative
        # offered here (none exists yet that verifies HEAD is still this
        # session's).
        _amend_body = (
            "'git commit --amend' rewrites whatever commit is at HEAD — on "
            "a shared branch that may be a peer's, landed since your last "
            "commit. It replaces that commit's message and authorship, and "
            "(without --only) also commits everything currently staged.\n\n"
            "To fix a message without rewriting:\n"
            "  git notes add -f -m \"<correction>\" <sha>"
        )
        if _bt_c7_index_holds_foreign_paths(seg_tokens, segments, seg_index):
            return _deny(
                (
                    "Deny: " + _amend_body
                    if is_amend
                    else (
                        "Deny: the index holds staged paths OUTSIDE this "
                        "command's own 'git add' — a peer's staged work would be "
                        "swept under your subject.\n\n"
                        "Use instead:\n"
                        "  git add -- <paths> && git commit -m %s -- <paths>\n\n"
                        "For unusual staging (partial-hunk, GIT_INDEX_FILE) use "
                        "instead:\n"
                        "  scoped-git-commit -m <same subject> -- <paths>"
                        % (subject_operand,)
                    )
                )
                + ("\n\n%s" % _commit_bare_note if _commit_bare_note else "")
            )
        if _bt_solo_bare_commit_index_nonempty(seg_tokens, segments, seg_index):
            return _deny(
                (
                    "Deny: " + _amend_body
                    if is_amend
                    else (
                        "Deny: this 'git commit' names no scope and stages "
                        "nothing itself — the shared index already holds staged "
                        "content this command cannot prove is its own, on a "
                        "shared branch a peer's.\n\n"
                        "Use instead:\n"
                        "  git commit -m %s -- <paths>\n\n"
                        "For unusual staging (partial-hunk, GIT_INDEX_FILE) use "
                        "instead:\n"
                        "  scoped-git-commit -m <same subject> -- <paths>"
                        % (subject_operand,)
                    )
                )
                + ("\n\n%s" % _commit_bare_note if _commit_bare_note else "")
            )
        if _bt_sweep_all_holds_unverifiable_paths(seg_tokens):
            return _deny(
                (
                    "Deny: " + _amend_body
                    if is_amend
                    else (
                        "Deny: this 'git commit' sweeps every modified tracked "
                        "file from the worktree ('-a'/'--all'), not just the "
                        "index — a peer's un-staged edits would be swept under "
                        "your subject too.\n\n"
                        "Use instead:\n"
                        "  git add -- <paths> && git commit -m %s -- <paths>\n\n"
                        "For unusual staging (partial-hunk, GIT_INDEX_FILE) use "
                        "instead:\n"
                        "  scoped-git-commit -m <same subject> -- <paths>"
                        % (subject_operand,)
                    )
                )
                + ("\n\n%s" % _commit_bare_note if _commit_bare_note else "")
            )
        return _advisory(
            (
                "Advisory: " + _amend_body
                if is_amend
                else (
                    "Advisory: this 'git commit' names no scope — commits "
                    "whatever is staged, including a peer's concurrent work.\n\n"
                    "Use instead:\n"
                    "  git add -- <paths> && git commit -m %s -- <paths>\n\n"
                    "For unusual staging (partial-hunk, GIT_INDEX_FILE) use "
                    "instead:\n"
                    "  scoped-git-commit -m <same subject> -- <paths>"
                    % (subject_operand,)
                )
            )
            + ("\n\n%s" % _commit_bare_note if _commit_bare_note else "")
        )
    return None


# ---------------------------------------------------------------------------
# 17-18. BX-7/BX-8's missing rewrite targets -- closing the two-shape gap in
# BX-16's vocabulary. DoE docs/plans/2026-07-29-windows-viability-stop-the-
# spawn-storms.md, row BX-16 (this dispatch's own remit): `_shape_classifier
# .Shape` names FIVE measured fork-tax shapes, and two of them (MULTI_PROBE_
# BANNER at 40.1% of forks, HEAD_TAIL_PLUMBING at 25%) had no rewrite target
# in this section until now -- BX-7 and BX-8 name exactly this gap as the
# reason they are not yet unblocked.
#
# Same three-tier-minus-one shape as every check above: auto-rewrite
# (allow + updatedInput) when every stage of the command is one of this
# function's recognized, translatable forms; advise (allow +
# additionalContext, command unchanged) when any stage is not; NEVER deny --
# see this section's own module comment above (checks 12-16) for why: this
# row owns the rewrite VOCABULARY, Windows deny policy belongs to BX-7/BX-8's
# own platform-gated guards, which consume the targets these two checks name.
# ---------------------------------------------------------------------------

#: git-fact probe forms this rewrite recognizes and batches into ONE
#: `git status --porcelain=v2 --branch` call -- an unrecognized git
#: subcommand (e.g. `git log`, `git diff`) makes the WHOLE banner chain fall
#: through to the advisory skeleton rather than a partial/guessed rewrite.
_SESSION_FACT_GIT_BRANCH_FORMS = (
    ("rev-parse", "--abbrev-ref", "HEAD"),
    ("branch", "--show-current"),
)
_SESSION_FACT_GIT_HEAD_FORMS = (("rev-parse", "HEAD"),)
_SESSION_FACT_GIT_STATUS_FORMS = (
    ("status",),
    ("status", "--short"),
    ("status", "-s"),
    ("status", "--porcelain"),
)


def _bt_git_probe_kind(tokens: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """Classify a tokenized `git ...` segment as one of the three
    session-fact kinds this rewrite batches into a single `git status
    --porcelain=v2 --branch` invocation (branch name, HEAD sha, dirty-file
    status), or `None` for any other git subcommand -- never guessed.

    Returns `(kind, form)`. For `kind == "branch"`, `form` distinguishes
    which of the two original commands this segment was (`"revparse"` for
    `git rev-parse --abbrev-ref HEAD`, `"showcurrent"` for `git branch
    --show-current`) -- Review: code-reviewer (Finding 2) -- the two
    commands disagree on detached-HEAD output (`rev-parse --abbrev-ref`
    prints the literal `HEAD`; `--show-current` prints empty), so batching
    them into one `_branch` variable with no memory of which was asked
    silently reproduced NEITHER real command's output on detached HEAD.
    `form` is `None` for `head_sha`/`status` (no such divergence there)."""
    if not tokens or not _bt_token_matches_binary(tokens[0], "git"):
        return None
    rest = tuple(tokens[1:])
    if rest == ("rev-parse", "--abbrev-ref", "HEAD"):
        return ("branch", "revparse")
    if rest == ("branch", "--show-current"):
        return ("branch", "showcurrent")
    if rest in _SESSION_FACT_GIT_HEAD_FORMS:
        return ("head_sha", None)
    if rest in _SESSION_FACT_GIT_STATUS_FORMS:
        return ("status", None)
    return None


def _bt_probe_segment_kind(tokens: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """Classify one non-piped multi-probe-banner segment as a translatable
    session-fact probe. Returns `(kind, extra)` or `None` if this segment is
    not one of the recognized bare-invocation forms this rewrite translates
    -- an unrecognized flag/operand/extra-argument shape makes the WHOLE
    command fall through to the advisory skeleton rather than a partial or
    guessed rewrite."""
    if not tokens:
        return None
    head = tokens[0]
    rest = tokens[1:]
    if _bt_token_matches_binary(head, "pwd") and not rest:
        return ("pwd", None)
    if _bt_token_matches_binary(head, "whoami") and not rest:
        return ("whoami", None)
    if _bt_token_matches_binary(head, "date") and not rest:
        return ("date", None)
    if _bt_token_matches_binary(head, "uname") and rest in ([], ["-a"]):
        return ("uname_a" if rest == ["-a"] else "uname", None)
    if _bt_token_matches_binary(head, "echo"):
        if any(a.startswith("-") for a in rest):
            return None  # `-e`/`-n` etc. change echo's own semantics -- don't guess
        return ("echo", " ".join(rest))
    git_kind = _bt_git_probe_kind(tokens)
    if git_kind is not None:
        kind, form = git_kind
        return ("git:" + kind, form)
    return None


def check_multiprobe_banner_rewrite(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 6 (BX-7's rewrite target) -- the multi-probe session-facts
    banner (40.1% of measured forks, second only to grep-via-Bash): a
    banner-marked chain like ``echo "=== SESSION FACTS ==="; git rev-parse
    --abbrev-ref HEAD; git status --short; pwd; whoami; date; uname`` forks
    ONE PROCESS PER PROBE to re-derive facts the harness already knows
    (session-fact re-derivation rates measured at 89%/84%/71%/49% are what
    make this worth building rather than a rule worth writing -- BX-7's own
    body).

    Auto-rewrites to a single `python3 -c` one-liner reproducing the SAME
    facts in one process when every probe segment is one of
    `_bt_probe_segment_kind`'s recognized forms -- batching every git fact
    into ONE `git status --porcelain=v2 --branch` call rather than one `git`
    fork per fact, per this dispatch's own instruction. Never denies -- see
    this section's module comment.

    Firing-shape fix (2026-08-01, C4): emits EITHER a concrete rewrite (every
    segment recognized) OR NOTHING (`None`) -- never a prose-only advisory
    with no `updatedInput`. The prior behavior fell back to `_advisory(...)`
    the moment any segment was unrecognized: prose asking the agent to
    reconsider the command, the unrecognized segment merely echoed back, no
    concrete alternative offered. That is Axis A's exact failure mode
    (`docs/plans/2026-08-01-advisory-firing-shape-predicate.md`), and it is
    worse than merely uninformative here: this rewrite's ENTIRE value
    proposition is re-deriving session facts the harness already knows, so
    an advisory that fires on a command with NO recognized session-fact
    probe at all (see finding below) would be asking the agent to reconsider
    a command that never asked for a session fact in the first place --
    syntactically Axis-A-compliant (it names an "alternative": batch into
    python3 -c) while being substantively nonsense for that input. Falling
    through to silence when no rewrite can be offered sidesteps that case
    entirely: the model sees nothing, exactly as if this check did not
    exist for the segment shape it cannot handle. `COORDINATOR_ALLOW_
    MULTIPROBE_BANNER` still exists for the rewrite-offered branch below.

    Known open finding, NOT fixed in this chunk (write-overlap boundary --
    this chunk owns this function's emission shape, not the shape
    classifier's recognition boundary): `_bt_classify_command`'s
    `_BT_Shape.MULTI_PROBE_BANNER` over-triggers on chains that carry no
    session-fact probe at all -- live-confirmed, `sed -n 1,5p a.py; echo ===;
    sed -n 6,9p b.py` (two `sed` segments and a bare `echo` with no probe
    shape among `_bt_probe_segment_kind`'s recognized forms) still
    classifies as `MULTI_PROBE_BANNER` upstream of this function. Under the
    prior advisory-fallback behavior that meant a syntactically-compliant
    but substantively-nonsense advisory (see above); under this chunk's
    silent-fallthrough fix it means this function correctly emits nothing
    for that input, but only because every recognized-vs-unrecognized
    command reaching this function is CLASSIFIED as a banner in the first
    place by a predicate that is itself too broad. That classifier
    over-trigger is the same over-firing defect class this plan exists to
    close, one layer up the call stack from this function's own contract --
    left as a follow-up finding against `_shape_classifier.MULTI_PROBE_
    BANNER` rather than fixed in-band here, since narrowing the classifier
    is a shape-recognition change, not this chunk's declared emission-shape
    scope.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_MULTIPROBE_BANNER"):
        return None
    classification = _bt_classify_command(cmd)
    if classification.tokens is None:
        return None
    if (
        classification.primary is None
        or classification.primary.shape is not _BT_Shape.MULTI_PROBE_BANNER
    ):
        return None

    segments = _bt_segments_from_tokens_with_pipe_flag(classification.tokens)
    kinds: List[Tuple[str, Optional[str]]] = []
    for tokens, pipe_before in segments:
        # A piped stage inside a "banner" chain is genuinely composed
        # (e.g. `... | tee log.txt`) rather than a bare fact probe -- treat
        # it the same as any other unrecognized segment: advise, don't guess.
        kind = None if pipe_before else _bt_probe_segment_kind(tokens)
        if kind is None:
            # Exit (b), per C4: no concrete rewrite can be offered for this
            # segment shape, so emit NOTHING rather than a prose-only
            # advisory naming no applicable alternative. See this
            # function's docstring ("Firing-shape fix") for why exit (a)
            # (advise-with-skeleton) is the defective shape this replaces.
            return None
        kinds.append(kind)

    needs_git = any(k.startswith("git:") for k, _ in kinds)
    lines: List[str] = ["import os"]
    if any(k == "whoami" for k, _ in kinds):
        lines.append("import getpass")
    if any(k == "date" for k, _ in kinds):
        lines.append("import time")
    if any(k in ("uname", "uname_a") for k, _ in kinds):
        lines.append("import platform")
    # NOTE: every string literal in the generated payload below is
    # double-quoted by convention, not because it is load-bearing --
    # `shlex.quote()` at the `_allow_rewrite` call site below POSIX-escapes
    # this whole script correctly even if it contained a raw `'` (the
    # apostrophe-breaks-the-outer-quote defect this rewrite used to carry,
    # fixed across all four `python3 -c` emission sites in this module).
    # Kept double-quoted anyway for readability, not correctness.
    if needs_git:
        lines.append("import subprocess")
        lines.append(
            '_gs = subprocess.run(["git", "status", "--porcelain=v2", '
            '"--branch"], capture_output=True, text=True).stdout.splitlines()'
        )
        lines.append("_branch = _head = None")
        lines.append("_status_lines = []")
        lines.append("for _l in _gs:")
        lines.append('    if _l.startswith("# branch.head "):')
        lines.append('        _branch = _l.split(" ", 2)[2]')
        lines.append('    elif _l.startswith("# branch.oid "):')
        lines.append('        _head = _l.split(" ", 2)[2]')
        lines.append('    elif _l.startswith("#"):')
        lines.append("        continue")
        lines.append("    else:")
        # Review: code-reviewer (Finding 7) -- porcelain=v2's kind-"2"
        # (renamed/copied) record appends a rename-score field the kind-"1"
        # record doesn't have, THEN the two paths joined by a literal TAB
        # (`new\told`), not another space -- a blind `_l.split(" ")` doesn't
        # special-case this, so the "last field" it picks up is actually
        # `"new\told"` glued together, never split into the two paths. A
        # blind full-string split also fragments any path containing its
        # OWN literal space before the "last field" slice is even taken.
        # Fixed by splitting each kind on its OWN documented fixed-field
        # count (via `maxsplit`, so the path/rename-pair remainder is never
        # itself re-split on an embedded space), then splitting the kind-"2"
        # remainder on the tab that actually separates its two paths --
        # reproducing `git status --short`'s own `R  <new> -> <old>` line
        # shape for a rename instead of the glued-together sentinel.
        lines.append('        _kind = _l.split(" ", 1)[0]')
        lines.append('        if _kind == "1":')
        lines.append('            _f = _l.split(" ", 8)')
        lines.append('            _status_lines.append(_f[1] + " " + _f[8])')
        lines.append('        elif _kind == "2":')
        lines.append('            _f = _l.split(" ", 9)')
        lines.append('            _new, _old = _f[9].split("\\t", 1)')
        lines.append(
            '            _status_lines.append(_f[1] + " " + _new + " -> " + _old)'
        )
        lines.append('        elif _kind == "?":')
        lines.append('            _status_lines.append("?? " + _l.split(" ", 1)[1])')
        lines.append('        elif _kind == "!":')
        lines.append('            _status_lines.append("!! " + _l.split(" ", 1)[1])')

    for kind, extra in kinds:
        if kind == "pwd":
            lines.append("print(os.getcwd())")
        elif kind == "whoami":
            lines.append("print(getpass.getuser())")
        elif kind == "date":
            # Review: code-reviewer (Finding 3) -- `%e` (space-padded
            # day-of-month) is a glibc/BSD `strftime` EXTENSION, not part of
            # the C89 set Python's own docs guarantee portable; the Windows
            # CRT does not implement it and `time.strftime` raises
            # `ValueError` there -- a bare `date` fork (which works fine on
            # Windows Git Bash's own coreutils) would trade for a CRASHING
            # `python3 -c` one-liner on the very platform this rewrite
            # exists for (BX-7). Fixed portably (no platform branch needed)
            # by computing the space-padded day with plain Python string
            # formatting (`"%2d" % ...`) instead of asking `strftime` for
            # it, then splicing it into the surrounding `strftime` output --
            # preserves the real `date` command's exact space-padded-day
            # fidelity on every platform, not merely a same-behavior-minus-
            # padding stand-in.
            #
            # SINGLE-SAMPLE FIX (2026-07-29, code-reviewer Finding 2,
            # confirmed): the original form called `time.localtime()` once
            # for the day-of-month and then TWO separate no-arg
            # `time.strftime()` calls, each of which internally re-samples
            # the clock via its own `time.localtime()` -- three independent,
            # uncorrelated reads of "now". A day/hour/minute rollover between
            # any of those three calls could print an internally
            # inconsistent banner (e.g. a weekday/month from before midnight
            # glued to a day-of-month or time-of-day from after it). Sampling
            # `time.localtime()` exactly once and deriving both `strftime`
            # calls from that single struct_time makes the three pieces
            # atomic with respect to each other.
            lines.append("_now = time.localtime()")
            lines.append("_day = \"%2d\" % _now.tm_mday")
            lines.append(
                'print(time.strftime("%a %b ", _now) + _day + time.strftime(" %H:%M:%S %Z %Y", _now))'
            )
        elif kind == "uname":
            lines.append("print(platform.uname().system)")
        elif kind == "uname_a":
            # Review: code-reviewer (Finding 8, nit) -- GNU coreutils'
            # `uname -a` appends processor/hardware-platform/operating-
            # system fields this rewrite omits. NOT adding `platform.uname()
            # .processor` here despite that suggestion: differential
            # execution against the real `uname -a` on this development
            # platform (macOS/BSD userland) shows `.processor` ("arm")
            # is NOT one of the fields real `uname -a` prints there at all
            # -- it is a GNU-coreutils-only convention, so adding it
            # unconditionally would trade this omission for a NEW fidelity
            # gap on every non-GNU host, the identical "confident wrong
            # answer" class this whole dispatch exists to close. Left as
            # documented residue (nit, low stakes per the finding: `uname`
            # isn't native to Windows anyway, only present via Git Bash/WSL).
            lines.append(
                '_u = platform.uname(); print(" ".join([_u.system, _u.node, '
                "_u.release, _u.version, _u.machine]))"
            )
        elif kind == "echo":
            lines.append("print(%s)" % json.dumps(extra))
        elif kind == "git:branch":
            # Review: code-reviewer (Finding 2) -- `# branch.head` prints
            # the literal sentinel `(detached)` on a detached HEAD, which
            # neither original command actually outputs verbatim: `git
            # rev-parse --abbrev-ref HEAD` prints `HEAD` there, while `git
            # branch --show-current` prints an empty line. `extra` (`form`,
            # set by `_bt_git_probe_kind`) tracks which of the two this
            # segment actually was so each gets its OWN real behavior
            # instead of one shared, sentinel-leaking `_branch` reading.
            if extra == "revparse":
                lines.append(
                    'print("HEAD" if _branch == "(detached)" else (_branch or ""))'
                )
            else:  # "showcurrent"
                lines.append(
                    'print("" if _branch in (None, "(detached)") else _branch)'
                )
        elif kind == "git:head_sha":
            # Review: code-reviewer (Finding 2) -- `# branch.oid` prints the
            # literal sentinel `(initial)` on an unborn/initial branch (no
            # commits yet), where the real `git rev-parse HEAD` instead
            # exits non-zero with NO stdout. Map the sentinel to empty
            # output rather than printing it verbatim, matching the real
            # command's observable (stdout-side) failure behavior.
            lines.append('print("" if _head in (None, "(initial)") else _head)')
        elif kind == "git:status":
            lines.append('print("\\n".join(_status_lines))')

    script = "\n".join(lines)
    _multiprobe_note = operator_override_note(
        "COORDINATOR_ALLOW_MULTIPROBE_BANNER", payload=payload, git_root=git_root
    )
    return _allow_rewrite(
        "%s -c %s" % (_bt_python3_invocation(), shlex.quote(script)),
        (
            "Auto-rewritten: this multi-probe session-facts banner re-derives "
            "facts the harness already knows, one process PER PROBE (measured "
            "session-fact re-derivation rates of 89%%/84%%/71%%/49%%). A single "
            "python3 process reproduces the same facts, batching every git fact "
            "into ONE 'git status --porcelain=v2 --branch' call."
        )
        + (" %s" % _multiprobe_note if _multiprobe_note else ""),
    )



#: Task 4 fix (2026-07-29, live incident): a shell redirection operator
#: token (`2>`, `>`, `>>`, `<`, `<<`, and their glued-target spelling
#: `2>/dev/null`) is NOT a path/pattern operand -- before this fix, every
#: segment parser below (`_bt_parse_ls_segment`, `_bt_parse_find_census_
#: segment`, `_bt_grep_flags_and_operands`) walked its own non-flag tokens
#: and treated the LAST one as the path/target, so
#: `ls DIR 2>/dev/null | head -40` silently took `"2>/dev/null"` as the
#: directory to list, and the emitted rewrite crashed with
#: `FileNotFoundError: [Errno 2] No such file or directory: '2>/dev/null'`
#: -- an AUTO-APPLIED rewrite (`updatedInput`), not an advisory, so the
#: caller's command was silently replaced with one that meant something
#: different before it failed.
#:
#: `&`-involving forms (`2>&1`, `&>/dev/null`) are a SEPARATE, already-safe
#: case: the shared tokenizer's `punctuation_chars=";&|"` splits a bare `&`
#: out as its own chain-separator token (confirmed empirically -- `2>&1`
#: tokenizes to `['2>', '&', '1']`), which makes `_bt_segments_from_tokens_
#: with_pipe_flag` see 3+ segments instead of the 2 this rewrite requires,
#: so `check_head_tail_plumbing_rewrite`'s own segment-count guard already
#: falls through to its "longer chain" advisory for those forms -- this fix
#: does not need to (and, per the file-scope constraint, must not) touch
#: that tokenizer behavior.
#:
#: This detector matches only the OPERATOR's own leading characters (a
#: `re.match`, not `fullmatch`), so it catches both the glued form
#: (`2>/dev/null`, one token) and the separate-token form (`2>` followed by
#: `/dev/null` as its own token) -- for the separate form, seeing the bare
#: `2>` operator token anywhere in a segment is already sufficient to
#: decline the whole segment, so the target token that follows it never
#: needs its own check.
#:
#: A quoted operand that merely CONTAINS this shape (e.g. a `find -name`
#: pattern of literally `"2>foo"`) cannot be distinguished from a real
#: unquoted redirection once the shared tokenizer has stripped quoting --
#: this is a coverage loss (a rewrite that could have been offered isn't),
#: never a correctness risk, and is the same "prefer not rewriting over
#: rewriting approximately" bias this whole fix exists to apply.
_BT_REDIRECTION_TOKEN_RE = re.compile(r"^(?:\d*(?:>>|<<|>|<)|&>>?)")


def _bt_is_redirection_token(tok: str) -> bool:
    """`True` if `tok` alone looks like a shell redirection operator
    (`_BT_REDIRECTION_TOKEN_RE`) -- the single per-token predicate behind
    both `_bt_has_redirection` (segment-level "does this contain one at
    all") and `_bt_commit_operand_scan` (token-level "is THIS surviving
    token a redirection, not a git pathspec operand"). Factored out so
    there remains exactly one definition of "is a redirection token" in
    this module, per the 2026-08-05 cockpit incident fix: a surviving
    redirection token (`<<'MSG'`, `>/dev/null`, `2>&1`, ...) was being read
    as a git commit pathspec operand, which made an UNSCOPED `git commit`
    carrying any redirection look explicitly scoped and silenced the
    sweep-commit advisory/deny entirely."""
    return bool(_BT_REDIRECTION_TOKEN_RE.match(tok))


def _bt_is_bare_redirection_token(tok: str) -> bool:
    """`True` if `tok` is a redirection operator with nothing glued after
    it (`>`, `>>`, `2>`, `<`, `<<`, `&>`), as opposed to a glued form
    (`>/dev/null`, `2>&1`, `<<'MSG'`) where the target is part of the same
    token. Derived from `_BT_REDIRECTION_TOKEN_RE`'s match LENGTH against
    the token's own length -- not a literal string list -- so a future
    change to that regex stays automatically consistent here (Finding 1,
    2026-08-05 cockpit-incident follow-up review): `_bt_tokenize_full_command`
    uses `punctuation_chars=";&|"`, which excludes `>`/`<`, so a whitespace-
    separated redirection (`> /dev/null`, `2> file`, `< file`) tokenizes as
    TWO tokens -- the bare operator, then its target as a separate plain
    token. A bare operator token consumes nothing of its target itself;
    the caller must additionally skip the NEXT token as the redirect
    destination, or that target token survives into `operands` and reads
    an unscoped `git commit ... > /dev/null` as explicitly scoped."""
    m = _BT_REDIRECTION_TOKEN_RE.match(tok)
    return bool(m) and m.end() == len(tok)


def _bt_has_redirection(tokens: List[str]) -> bool:
    """`True` if any token in `tokens` looks like a shell redirection
    operator (`_bt_is_redirection_token`) -- shared by every BX-16
    head/tail-upstream segment parser (`_bt_parse_ls_segment`,
    `_bt_parse_find_census_segment`, `_bt_grep_flags_and_operands`) so a
    redirection-bearing segment is declined identically everywhere, rather
    than three independently-hand-derived checks drifting apart."""
    return any(_bt_is_redirection_token(tok) for tok in tokens)



