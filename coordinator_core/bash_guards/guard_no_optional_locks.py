"""coordinator_core.bash_guards.guard_no_optional_locks -- auto-rewrites a
lock-acquiring ``git status``/``git diff`` invocation to its behavior-
preserving ``git --no-optional-locks``-flagged equivalent, prompt-free.

Subject: fleet-wide `.git/index.lock` contention -- ~18 concurrent EM
sessions run against shared trees, most of them in this repo, and
`git status`/`git diff` are the two commands agents reach for constantly
that take the worktree lock for no operational reason (they only refresh
cached stat data, then write it back). Doctrine prose cannot hold this line
-- `git status` is too ingrained in the model -- so the enforcement is
mechanical, same posture as ``guard_offer_git_c.check_offer_git_c``'s
``cd``-prefix rewrite, which this module follows for shape, message format,
and tokenization.

MEASURED EVIDENCE (operator's Windows box, git 2.55.0.windows.3):
``git status`` creates ``.git/index.lock``; ``git --no-optional-locks
status`` does not (reproduced 4/4 via FileSystemWatcher against a positive
control, corroborated by ``.git/index`` mtime). Output and exit code are
byte-identical with and without the flag -- the flag only suppresses
write-back of refreshed stat data, not the refresh itself, so this rewrite
is behavior-preserving and safe to apply prompt-free.

THE FLAG IS PRE-SUBCOMMAND ONLY. ``git --no-optional-locks status`` works;
``git status --no-optional-locks`` exits 129 ("error: unknown option"), no
output. This module inserts the flag strictly between the resolved git
GLOBAL-OPTION span (``git``, any ``-C <path>``/``-c ...``/etc.) and the
resolved SUBCOMMAND token -- never after it. See
``test_no_optional_locks_rewrite.py``'s
``TestFlagLandsPreSubcommand`` for the explicit pin.

NOT rewritten (evidence: same measurement pass found these do not take the
worktree lock at all, so rewriting them would be a no-op flag with no
lock-contention benefit and pure risk of an unfamiliar exit-129 shape if
this module's subcommand-arg heuristic is ever wrong):
  - ``git diff --cached`` / ``--staged`` (any spelling/position).
  - ``git ls-files -m`` (not a targeted subcommand at all -- see
    ``_REWRITE_SUBCOMMANDS``).
  - A ref-to-ref ``git diff`` (``git diff HEAD:<f> stash@{0}:<f>``,
    ``git diff <sha>..HEAD``) -- detected heuristically (see
    ``_diff_args_are_ref_shaped``) rather than resolved against a live
    repository, because this guard runs on the PreToolUse hot path with no
    repository access.

Reuses the package's shared git-global-flag vocabulary
(``_GIT_GLOBAL_OPT_WITH_ARG``/``_GIT_GLOBAL_OPT_NO_ARG_SIMPLE`` --
``dispatch_checks.py`` already lists ``--no-optional-locks`` among them, see
that module's own comment on ``_GIT_GLOBAL_OPT_NO_ARG_SIMPLE``) rather than
hand-rolling a second one, and the package's shared quote-aware tokenizer
(``tokenize_full_command``/``token_matches_binary``) rather than a fresh
regex over raw command text -- same reuse posture ``guard_offer_git_c.py``'s
own docstring calls out for its own segmentation.

Spec backlink: docs/plans/2026-08-07-git-index-lock-contention-campaign.md
(fleet-wide lock-contention campaign) -- this guard is the mechanical leg of
that campaign; doctrine prose alone was assessed insufficient (see this
module's own opening paragraph).
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards.dispatch_checks import (
    _GIT_GLOBAL_OPT_NO_ARG_SIMPLE,
    _GIT_GLOBAL_OPT_WITH_ARG,
    _allow_rewrite,
    _crlf_strip,
    _override,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._command_tokenizer import (
    token_matches_binary as _bt_token_matches_binary,
    tokenize_full_command as _bt_tokenize_full_command,
)

#: The two subcommands this guard rewrites -- the two lock-acquiring
#: commands agents reach for constantly (see module docstring). Deliberately
#: closed and small: widening this set is a future decision, not something
#: this guard infers from a subcommand's name.
_REWRITE_SUBCOMMANDS = frozenset({"status", "diff"})

#: A `git diff` argument spelling this guard treats as "already excluded
#: from the worktree lock" -- rewriting these would be a no-op flag, not a
#: contention fix (see module docstring's NOT-rewritten list).
_DIFF_STAGED_FLAGS = frozenset({"--cached", "--staged"})

#: Token characters that make a shlex punctuation token a command separator
#: -- identical set to `guard_offer_git_c._OFFER_SEP_TOKEN_CHARS`, not
#: imported from there because that name is that module's own private
#: implementation detail, not a shared export.
_SEP_TOKEN_CHARS = frozenset(";&|")


def _diff_args_are_ref_shaped(args: List[str]) -> bool:
    """Return ``True`` when any non-flag `git diff` argument looks like a
    ref-to-ref or ref:path comparison (``<sha>..HEAD``, ``HEAD:file``,
    ``stash@{0}:file``) rather than an ordinary worktree-vs-index diff.

    Heuristic, not a real ref resolution (this guard has no repository
    access on the PreToolUse hot path) -- a flag token (leading ``-``) is
    never inspected, since a flag's OWN spelling cannot itself be a ref."""
    for arg in args:
        if arg.startswith("-"):
            continue
        if ".." in arg or "@{" in arg or ":" in arg:
            return True
    return False


def _resolve_git_global_span(tokens: List[str]) -> Optional[Tuple[int, bool]]:
    """``tokens[0]`` is already confirmed to be a `git` invocation. Walk
    past git's own global options (the package's shared
    ``_GIT_GLOBAL_OPT_WITH_ARG``/``_GIT_GLOBAL_OPT_NO_ARG_SIMPLE``
    vocabulary -- reused, not re-enumerated, per this module's own
    docstring) to the SUBCOMMAND token. Returns
    ``(subcommand_index, already_has_no_optional_locks)``, or ``None`` when
    the walk runs out of tokens before finding one, or hits an
    unrecognized flag -- the same fail-closed "unknown shape, do not guess"
    contract ``dispatch_checks._seg_resolved_git_subcommand`` already uses
    for this exact walk shape, one call site over."""
    i = 1
    n = len(tokens)
    has_nol = False
    while i < n:
        tok = tokens[i]
        if tok == "--no-optional-locks":
            has_nol = True
            i += 1
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
            return None
        return (i, has_nol)
    return None


def _maybe_insert_no_optional_locks(seg_tokens: List[str]) -> Tuple[List[str], bool]:
    """Return ``(possibly-rewritten-tokens, did_rewrite)`` for one segment's
    token list. A no-op (``did_rewrite is False``) for every segment that is
    not itself a bare `git status`/`git diff` invocation, already carries
    ``--no-optional-locks`` (idempotence), or is a `git diff` shape this
    module's own NOT-rewritten list excludes."""
    if not seg_tokens or not _bt_token_matches_binary(seg_tokens[0], "git"):
        return seg_tokens, False

    resolved = _resolve_git_global_span(seg_tokens)
    if resolved is None:
        return seg_tokens, False
    sub_idx, has_nol = resolved
    subcommand = seg_tokens[sub_idx]
    if subcommand not in _REWRITE_SUBCOMMANDS:
        return seg_tokens, False
    if has_nol:
        return seg_tokens, False

    if subcommand == "diff":
        args = seg_tokens[sub_idx + 1:]
        if any(a in _DIFF_STAGED_FLAGS for a in args):
            return seg_tokens, False
        if _diff_args_are_ref_shaped(args):
            return seg_tokens, False

    new_tokens = seg_tokens[:sub_idx] + ["--no-optional-locks"] + seg_tokens[sub_idx:]
    return new_tokens, True


def check_git_no_optional_locks(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[dict]:
    """PreToolUse ADVISORY_REWRITE-band guard: auto-rewrites every top-level
    `git status`/bare `git diff` segment in ``cmd`` to insert
    ``--no-optional-locks`` immediately before the subcommand, prompt-free
    (no deny, no advisory-only offer -- see module docstring for why this
    campaign's mechanism has to be mechanical). Returns ``None`` (allow,
    unchanged) when nothing in ``cmd`` needs rewriting.

    Segmentation and quoting are delegated entirely to the package's shared
    tokenizer (``tokenize_full_command``) -- this function does no
    character-level quote/separator scanning of its own, same posture
    ``guard_offer_git_c._offer_anchor_followers`` documents for its own
    per-segment reconstruction, which this function's own reconstruction
    loop mirrors."""
    if not cmd or "git" not in cmd:
        return None
    if _override("COORDINATOR_ALLOW_OPTIONAL_LOCKS"):
        return None

    cmd = _crlf_strip(cmd)
    tokens = _bt_tokenize_full_command(cmd)
    if tokens is None:
        return None

    groups: List[Tuple[Optional[str], List[str]]] = []
    current: List[str] = []
    pending_sep: Optional[str] = None
    for tok in tokens:
        if tok and set(tok) <= _SEP_TOKEN_CHARS:
            groups.append((pending_sep, current))
            pending_sep = tok
            current = []
            continue
        current.append(tok)
    groups.append((pending_sep, current))

    pieces: List[str] = []
    rewritten_segments: List[str] = []
    changed = False
    for sep, seg_tokens in groups:
        if not seg_tokens:
            if sep is not None:
                pieces.append(sep)
            continue
        new_tokens, did_rewrite = _maybe_insert_no_optional_locks(seg_tokens)
        seg_text = " ".join(shlex.quote(t) for t in new_tokens)
        if sep is not None:
            pieces.append(sep)
        pieces.append(seg_text)
        if did_rewrite:
            changed = True
            rewritten_segments.append(" ".join(shlex.quote(t) for t in seg_tokens))

    if not changed:
        return None

    new_cmd = " ".join(pieces)
    note = (
        "Auto-rewritten: %s -> '--no-optional-locks' inserted before the "
        "subcommand on %d segment(s) -- avoids the shared-tree "
        "`.git/index.lock` acquisition."
        % (
            "; ".join("'%s'" % s for s in rewritten_segments),
            len(rewritten_segments),
        )
    ) + " " + operator_override_note("COORDINATOR_ALLOW_OPTIONAL_LOCKS", payload=payload)
    return _allow_rewrite(new_cmd, note)
