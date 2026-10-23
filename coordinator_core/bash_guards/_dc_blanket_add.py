"""coordinator_core.bash_guards._dc_blanket_add -- guard family 6 of the
``dispatch_checks`` split: ``check_blanket_git_add``
(block-blanket-git-add.sh).

Hazard-discriminated guard: it fires only where the concurrent-session hazard
is actually possible. ``_is_hazard_repo`` DENIES inside the ~/.claude
meta-repo OR inside any repo this MACHINE's fleet registry (``repos.*`` in
the machine-local settings-home registry, see
``coordinator_core.machine_resolver``) knows about -- an OSS consumer install
has no such registry entry, so the guard stays inert there. FAILS OPEN by
construction: any error resolving the discriminator (missing HOME, unreadable
settings home, malformed registry) degrades to "not a hazard repo" (ALLOW),
never to a deny.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards import _dc_git_probe, _dc_git_shared
from coordinator_core.bash_guards._dc_git_shared import (
    _BYPASS_PREFIX,
    _paths_match,
    _ps_git_bypass_segments,
)
from coordinator_core.bash_guards._dc_shell_text import (
    _join_backslash_newlines,
    _shell_c_unwrap_payloads,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._override_log_path import _override_log_path
from coordinator_core.bash_guards._rewrite_support import _alt_record, _crlf_strip, _offer, _override


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
#: C6 (Items 28/16.6): the COMMIT-side gate this guard's top-of-function
#: short-circuit also checks -- widens the same "does this command line even
#: mention the subcommand" pre-filter to `git commit`, reusing the identical
#: global-option vocabulary `_GIT_ADD_GLOBAL_OPT_RE` already enumerates (the
#: options that may sit between `git` and its subcommand are the same set
#: for every subcommand; duplicating the alternation under a second name
#: would just be the same list twice).
_GIT_COMMIT_GATE_RE = re.compile(r"\bgit(?:\s+" + _GIT_ADD_GLOBAL_OPT_RE + r")*\s+commit\b")


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


def _add_has_narrowing_pathspec(after: str) -> bool:
    """Whether a `git add` argv restricts itself to named paths.

    True for a `--` with at least one token after it, and for
    `--pathspec-from-file[=<f>]` / `--pathspec-file-nul` (the path list is in
    the file; SC-DR-017 already counts these as scope at this file's two
    other scope readers). A bare `--` with nothing after it narrows nothing.

    Why `-u` is allowed to consult this while `-A` is not, since the
    asymmetry looks arbitrary: `git add -u -- X` stages tracked
    modifications under X and is a STRICT SUBSET of `git add -- X`, which
    this guard already permits — it is the same paths minus the untracked
    files. Refusing the narrower spelling of a permitted command cannot
    protect anything; it only pushes the committer toward a coarser form.
    `-A -- X` has no such subset relation and keeps denying.

    example-store-repo-em reported this twice in one evening (2026-09-03,
    cross-repo/inbox `scoped-commit-guard-the-permitted-form-has-now-caused-
    the-harm-twice`): a near-miss, then `176ce18` in example-store-repo, where a
    literal three-file pathspec — the discipline SC-DR-014 asks for, followed
    exactly — still swept ~164 lines of a peer's in-progress work, because a
    file pathspec scopes to the FILE and not to the committer's hunks in it.
    The refused forms were the most precise ones available.

    Negative-spec: this does NOT make `-u` unconditionally safe. A
    root-shaped pathspec (`.`, `:/`, the repo root spelled absolutely) is
    still denied by the path checks in the caller's loop, which a scoped
    `-u` now falls through to instead of short-circuiting past.
    """
    tokens = after.split()
    for i, tok in enumerate(tokens):
        if tok == "--":
            return i + 1 < len(tokens)
        if tok in ("--pathspec-from-file", "--pathspec-file-nul") or tok.startswith(
            "--pathspec-from-file="
        ):
            return True
    return False


def check_blanket_git_add(
    cmd: str,
    session_id: str = "",
    hook_payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)

    # C3: see `_ps_git_bypass_segments` docstring -- git's own argv is
    # unchanged across dialects; only the anti-bypass surface is new here.
    _ga_ps_segments = _ps_git_bypass_segments(
        cmd, "blanket-git-add", hook_payload
    ) or []

    if (
        not _GIT_ADD_GATE_RE.search(cmd)
        and not _GIT_COMMIT_GATE_RE.search(cmd)
        and not _ga_ps_segments
    ):
        return None

    _ga_dash_c_cwd = _bt_blanket_add_dash_c_cwd(cmd)
    rc, out = _dc_git_probe._run_git(["rev-parse", "--show-toplevel"], cwd=_ga_dash_c_cwd)
    git_root = out.strip() if rc == 0 else ""
    if not git_root:
        return None

    if not _dc_git_shared._is_hazard_repo(git_root):
        return None

    if _override("COORDINATOR_OVERRIDE_BLANKET_ADD", payload=hook_payload) or _override(
        "_COORDINATOR_SAFE_COMMIT_INTERNAL_BLANKET", payload=hook_payload
    ):
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
    for seg in list(_awk_quote_aware_split(cmd)) + _ga_ps_segments:
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
        # Quote characters are stripped so a quoted operand reads as its
        # payload. The BACKSLASH strip that used to ride along here (host-
        # gated to POSIX only) is gone entirely: it deleted every separator
        # in a drive-absolute operand on `nt` (`C:\repo` -> `X:repo`), and
        # even gated to run only on POSIX it made a backslash-spelled
        # absolute pathspec disagree with its forward-slash twin on any
        # non-Windows host (the verdict must not depend on which host is
        # running the guard, only on which separator the operand used).
        # There is no later seam that can recover a separator once it is
        # gone, so operand text now keeps every backslash it arrived with;
        # the path-shaped comparisons below (root-anchor, subtree) do their
        # own separator normalization instead of relying on this strip.
        after = after.replace('"', "").replace("'", "")

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
            # Flag detection is pre-`--` ONLY: after the separator every
            # token is a literal pathspec, never a flag, so `--all`/
            # `--update`/a bundled `-A` spelled post-`--` is a filename,
            # not the flag -- denying it here would be a false positive.
            # The PATH-shaped checks below (`.`, `:/`, absolute-root, C3
            # subtree) are the opposite: paths only ever appear after `--`
            # in the canonical `git add -- <path>` form this guard's own
            # deny message prescribes, so gating them on `not past_dd` is
            # exactly the hole DoE's example-game-repo-em finding (2026-08-31,
            # cross-repo/inbox) reported -- `git add -- state/subagent-
            # share/` swept 130 foreign paths past C3 because C3 sat
            # inside the same blanket `if past_dd: continue` built for
            # flag detection. Every post-`--` token is walked here, not
            # just the first (same finding, observation 1).
            if not past_dd:
                if tok == "--all":
                    should_deny = True
                    deny_reason = tok
                    break
                if tok == "--update":
                    if not _add_has_narrowing_pathspec(after):
                        should_deny = True
                        deny_reason = tok
                        break
                    # Scoped `-u`: fall through so the root-shaped pathspec
                    # checks below still get to deny `-u -- .` and friends.
                    continue
                if tok.startswith("-") and not tok.startswith("--"):
                    if "/" not in tok and not tok.startswith("./"):
                        flag_chars = tok[1:]
                        if "A" in flag_chars or "U" in flag_chars:
                            should_deny = True
                            deny_reason = tok
                            break
                        if "u" in flag_chars:
                            if not _add_has_narrowing_pathspec(after):
                                should_deny = True
                                deny_reason = tok
                                break
                            continue
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
            # An absolute pathspec that resolves to the repo root itself is
            # `.` written a different way -- same finding as `:/` above. A
            # DEEPER absolute path (a genuinely scoped subdirectory/file) is
            # deliberately NOT matched here, same "root, not a subtree"
            # asymmetry this file's own `_find_is_root_anchor` documents for
            # `check_runaway_find`.
            elif (
                tok.startswith("/") or tok.startswith("\\") or re.match(r"^[A-Za-z]:[\\/]", tok)
            ) and git_root:
                tok_slashed = tok.replace("\\", "/")
                norm_tok = os.path.normpath(tok_slashed.rstrip("/")) or tok_slashed
                if _paths_match(norm_tok, git_root):
                    should_deny = True
                    deny_reason = tok
                    break
            # C3 (docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md): a
            # SUBTREE pathspec was deliberately unmatched above (the
            # "root, not a subtree" asymmetry this module's own
            # `_find_is_root_anchor` docstring names for `check_runaway_
            # find`) -- DoE's cross-repo finding is that this is exactly
            # the shape that swept three peer memos. Resolve what the add
            # will actually stage (one `git add --dry-run` spawn, only for
            # a token that is genuinely an existing directory -- a literal
            # file token never reaches this branch, so the common single-
            # file `git add path/to/file` case costs nothing extra, per
            # DR-344) and refuse if it reaches a path this session did not
            # write. Does NOT reuse `_bt_commit_own_pathspec`/its WHETHER-
            # scoped boolean (that function's own docstring: never reuse a
            # WHETHER-scoped boolean as a WHICH-paths answer) -- this is a
            # new WHICH-paths answer for `git add`, Check 5's `git diff
            # --cached --name-only -- <pathspec>` re-derivation pattern
            # applied at the add site rather than a second predicate.
            elif git_root and not tok.startswith("-"):
                _ga_subtree_dir = _bt_add_resolve_subtree_dir_token(
                    tok, git_root, _ga_dash_c_cwd
                )
                if _ga_subtree_dir is not None:
                    _ga_foreign, _ga_churn = _bt_add_subtree_foreign_paths_and_churn(
                        _ga_subtree_dir, _ga_dash_c_cwd, git_root, session_id
                    )
                    if _ga_foreign:
                        should_deny = True
                        deny_reason = "%s [foreign: %s]" % (
                            tok,
                            ", ".join(sorted(_ga_foreign)[:5]),
                        )
                        if _ga_churn:
                            deny_reason += " [build churn: %d not counted]" % _ga_churn
                        break

        if should_deny:
            full_seg_trimmed = seg.lstrip()[:120]
            matched_cmd = "%s [matched flag: %s]" % (full_seg_trimmed, deny_reason)
            break

    # C6 (Items 28/16.6, `docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-
    # thread.md`): the COMMIT-side of this guard, flipped to the same shape
    # as the ADD side above -- one branch, not a restructure. Only reached
    # when the ADD-side scan above found nothing to deny. Scoped to the
    # canonical `-- <paths>` spelling only (SC-DR-008's ratified form; `-o`/
    # `--only`'s sweeping-scope shape is `check_git_commit_safe_commit_
    # advise`'s own remit and stays there, unescalated, per that check's own
    # negative spec) -- a root-anchor or `-A`-equivalent operand denies
    # unconditionally, exactly like `.`/`:/`/`-A` do on the ADD side; a
    # subtree operand denies ONLY if it would actually commit a foreign
    # path (`_bt_commit_subtree_foreign_paths`, the commit-side analogue of
    # `_bt_add_subtree_foreign_paths` -- `git diff --cached --name-only`
    # instead of `git add --dry-run`, since COMMIT never stages); a literal
    # file operand is never touched here, matching "a scoped pathspec
    # commit still passes."
    commit_matched_cmd = ""
    if not matched_cmd:
        for seg in list(_awk_quote_aware_split(cmd)) + _ga_ps_segments:
            if not seg.strip():
                continue
            seg_cmd = re.sub(r'"[^"]*"', " ", seg)
            seg_cmd = re.sub(r"'[^']*'", " ", seg_cmd)
            if not re.match(
                r"^\s*" + _BYPASS_PREFIX + r"git(?:\s+" + _GIT_ADD_GLOBAL_OPT_RE + r")*\s+commit\b",
                seg_cmd,
            ):
                continue
            after = re.sub(
                r".*(^|\s)git(?:\s+" + _GIT_ADD_GLOBAL_OPT_RE + r")*\s+commit\s*",
                " ",
                seg,
                count=1,
            )
            after = after.replace('"', "").replace("'", "")
            gc_toks = after.split()
            if "--" not in gc_toks:
                continue
            gc_operands = gc_toks[gc_toks.index("--") + 1 :]
            for gc_tok in gc_operands:
                if gc_tok in (".", "./", ":/", ":/."):
                    commit_matched_cmd = "%s [root pathspec]" % gc_tok
                    break
                gc_tok_slashed = gc_tok.replace("\\", "/")
                if (
                    gc_tok.startswith("/")
                    or gc_tok.startswith("\\")
                    or re.match(r"^[A-Za-z]:[\\/]", gc_tok)
                ) and git_root:
                    gc_norm = os.path.normpath(gc_tok_slashed.rstrip("/")) or gc_tok_slashed
                    if _paths_match(gc_norm, git_root):
                        commit_matched_cmd = "%s [root pathspec]" % gc_tok
                        break
                gc_abs_dir = _bt_add_resolve_subtree_dir_token(
                    gc_tok, git_root, _ga_dash_c_cwd
                )
                if gc_abs_dir is not None:
                    gc_foreign, gc_churn = _bt_commit_subtree_foreign_paths_and_churn(
                        gc_abs_dir, _ga_dash_c_cwd, git_root, session_id
                    )
                    if gc_foreign:
                        commit_matched_cmd = "%s [foreign: %s]" % (
                            gc_tok,
                            ", ".join(sorted(gc_foreign)[:5]),
                        )
                        if gc_churn:
                            commit_matched_cmd += " [build churn: %d not counted]" % gc_churn
                        break
            if commit_matched_cmd:
                full_seg_trimmed = seg.lstrip()[:120]
                commit_matched_cmd = "%s [matched: %s]" % (
                    full_seg_trimmed,
                    commit_matched_cmd,
                )
                break

    if not matched_cmd and not commit_matched_cmd:
        # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted
        # argument is executed, not inert text -- unwrap and re-scan it too.
        for payload in _shell_c_unwrap_payloads(cmd):
            result = check_blanket_git_add(payload, session_id, hook_payload)
            if result is not None:
                return result
        return None

    _kw = dict(report="the blanket git add or commit you need", authorize="the sweep of sibling sessions' edits")
    _pick = _offer("git commit -m <subject> -- path/to/file", "the subject and path")
    if commit_matched_cmd and not matched_cmd:
        _commit_note = operator_override_note(
            "COORDINATOR_OVERRIDE_BLANKET_ADD", payload=hook_payload, git_root=git_root
        )
        commit_reason = (
            "BLOCKED: this 'git commit' names a pathspec that sweeps in "
            "sibling sessions' edits (SC-DR-014, COMMIT-side). Matched: %s\n\n"
            "Use instead:\n"
            "  git commit -m <subject> -- path/to/file"
            % (commit_matched_cmd,)
        ) + ("\n\nOr: %s" % _commit_note if _commit_note else "")
        return _alt_record(
            "deny", "blanket-git-add",
            "BLOCKED: this 'git commit' names a pathspec that sweeps in sibling sessions' "
            "edits (SC-DR-014, COMMIT-side). Matched: %s" % (commit_matched_cmd,),
            (_pick,), "COORDINATOR_OVERRIDE_BLANKET_ADD", em_text=commit_reason, **_kw,
        )
    _add_note = operator_override_note(
        "COORDINATOR_OVERRIDE_BLANKET_ADD", payload=hook_payload, git_root=git_root
    )
    reason = (
        "BLOCKED: blanket `git add` sweeps in sibling sessions' edits "
        "(SC-DR-014). Matched: %s\n\n"
        "Use instead:\n"
        "  git add -- path/to/file\n"
        "  git commit -m <subject> -- path/to/file\n\n"
        "For many files (keep only yours), use instead:\n"
        "  git ls-files -om --exclude-standard -- <dir> > \"${TMPDIR:-/tmp}/p\"\n"
        "  git add --pathspec-from-file=\"${TMPDIR:-/tmp}/p\"\n"
        "  git commit -m <subject> --pathspec-from-file=\"${TMPDIR:-/tmp}/p\""
        % (matched_cmd,)
    ) + ("\n\nOr: %s" % _add_note if _add_note else "")
    return _alt_record(
        "deny", "blanket-git-add",
        "BLOCKED: blanket `git add` sweeps in sibling sessions' edits (SC-DR-014). "
        "Matched: %s" % (matched_cmd,),
        (
            _offer("git add -- path/to/file", "the path"),
            _pick,
            _offer(
                'git ls-files -om --exclude-standard -- <dir> > "${TMPDIR:-/tmp}/p"; '
                'git add --pathspec-from-file="${TMPDIR:-/tmp}/p"; '
                'git commit -m <subject> --pathspec-from-file="${TMPDIR:-/tmp}/p" (many files, keep only yours)',
                "the dir and subject",
            ),
        ),
        "COORDINATOR_OVERRIDE_BLANKET_ADD", em_text=reason, **_kw,
    )


def _bt_add_resolve_subtree_dir_token(
    tok: str, git_root: str, cwd: Optional[str]
) -> Optional[str]:
    """C3: resolve a `git add` pathspec token to an absolute directory path
    IFF it denotes an existing subtree under ``git_root`` -- returns
    ``None`` for a root-anchor token (already handled by the caller's
    earlier branches), a literal file token, or a nonexistent path, so the
    per-add ``git`` spawn in `_bt_add_subtree_foreign_paths` only ever
    fires for the directory shape this chunk targets (DR-344: the common
    single-file `git add path/to/file` case must cost nothing extra)."""
    if tok in (".", "./", ":/", ":/.") or tok.startswith(":"):
        return None
    tok_slashed = tok.replace("\\", "/")
    if os.path.isabs(tok_slashed) or re.match(r"^[A-Za-z]:[\\/]", tok):
        abs_tok = os.path.normpath(tok_slashed.rstrip("/")) or tok_slashed
    else:
        abs_tok = os.path.normpath(os.path.join(cwd or os.getcwd(), tok_slashed))
    if _paths_match(abs_tok, git_root):
        return None
    if not os.path.isdir(abs_tok):
        return None
    return abs_tok


def _bt_no_peer_can_share_the_index(git_root: str, session_id: str) -> bool:
    """True when no other session can be staging into this repo: it has no
    commits yet, or ``.git/coordinator-sessions`` holds no session dir other
    than ``session_id``. Zero-spawn (file reads only); False on any doubt."""
    try:
        git_dir = os.path.join(git_root, ".git")
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            if not os.path.isfile(os.path.join(git_dir, *ref.split("/"))):
                packed = os.path.join(git_dir, "packed-refs")
                packed_text = ""
                if os.path.isfile(packed):
                    with open(packed, encoding="utf-8") as fh:
                        packed_text = fh.read()
                if (" " + ref + "\n") not in packed_text:
                    return True
        sessions = os.path.join(git_dir, "coordinator-sessions")
        if not os.path.isdir(sessions):
            return True
        others = [
            name
            for name in os.listdir(sessions)
            if name != session_id and os.path.isdir(os.path.join(sessions, name))
        ]
        return not others
    except OSError:
        return False


def _bt_add_subtree_foreign_paths(
    abs_dir: str, cwd: Optional[str], git_root: str, session_id: str
) -> List[str]:
    """The foreign paths of `_bt_add_subtree_foreign_paths_and_churn`, without the churn count."""
    return _bt_add_subtree_foreign_paths_and_churn(abs_dir, cwd, git_root, session_id)[0]


def _bt_commit_subtree_foreign_paths(
    abs_dir: str, cwd: Optional[str], git_root: str, session_id: str
) -> List[str]:
    """The foreign paths of `_bt_commit_subtree_foreign_paths_and_churn`, without the churn count."""
    return _bt_commit_subtree_foreign_paths_and_churn(abs_dir, cwd, git_root, session_id)[0]


def _bt_drop_build_churn(foreign: List[str]) -> "tuple[List[str], int]":
    """``foreign`` minus generated build artifacts (no session owns them), and how many were dropped.

    Trap: matching is by basename glob only, so a peer's hand-edited ``*.generated.*`` file is
    also dropped; the deny reason's churn count is the only trace of it."""
    from coordinator_core.git.residue_classes import is_build_churn

    kept = [p for p in foreign if not is_build_churn(p)]
    return kept, len(foreign) - len(kept)


def _bt_add_subtree_foreign_paths_and_churn(
    abs_dir: str, cwd: Optional[str], git_root: str, session_id: str
) -> "tuple[List[str], int]":
    """C3: resolve WHICH paths a `git add -- <abs_dir>` would actually
    stage (one ``git add --dry-run`` spawn -- the add-side analogue of
    Check 5's ``git diff --cached --name-only -- <pathspec>``
    re-derivation in `check_validate_commit`; NOT a reuse of
    `_bt_commit_own_pathspec`'s WHETHER-scoped boolean, which that
    function's own docstring says must never answer a WHICH-paths
    question), then checks each resolved path against THIS session's own
    claimed scope via ``session.touch_record.project_live_claims`` -- the
    same reader `_rm_peer_claim_of` already uses for peer-claim checks,
    not a second ownership predicate.

    Fails toward the EMPTY list (never flagging) on any ambiguity --
    missing session_id, a failed/unparseable dry-run, or a degraded
    touch-record projection -- matching `check_blanket_git_add`'s existing
    soft/fail-open posture (this guard is registered fail_closed=False;
    see the override-log comment earlier in this module for the same
    posture stated explicitly)."""
    if not session_id or _bt_no_peer_can_share_the_index(git_root, session_id):
        return [], 0
    rc, out = _dc_git_probe._run_git(["add", "--dry-run", "--", abs_dir], cwd)
    if rc != 0:
        return [], 0

    staged: List[str] = []
    for line in out.splitlines():
        m = re.match(r"^add\s+'(.*)'$", line.strip())
        if not m:
            continue
        raw = m.group(1)
        raw_abs = raw if os.path.isabs(raw) else os.path.normpath(
            os.path.join(cwd or os.getcwd(), raw)
        )
        try:
            rel = os.path.relpath(raw_abs, git_root).replace(os.sep, "/")
        except ValueError:
            continue
        staged.append(rel)
    if not staged:
        return [], 0

    try:
        from coordinator_core.session.touch_record import (
            project_live_claims,
            sink_path,
        )

        sid_dir = os.path.join(git_root, ".git", "coordinator-sessions", session_id)
        touch_record_path = sink_path(sid_dir)
        projection = project_live_claims(touch_record_path, cwd=git_root)
        if projection.degraded:
            return [], 0
        my_claims = set(projection.claims.keys())
    except Exception:
        return [], 0

    # data/serving/ is regenerated by a session's own rebuild, never hand-authored.
    foreign = [p for p in staged if p not in my_claims and not p.startswith("data/serving/")]
    foreign, churn = _bt_drop_build_churn(foreign)
    # Run record dirs are written by the engine, never through a claimed touch, so they are
    # never in my_claims. Trap: foreign there means held by a live peer, not unclaimed.
    run_records = _bt_run_record_paths(foreign)
    if run_records:
        held_foreign = set(_bt_run_record_paths_foreign(run_records, git_root, session_id))
        foreign = [p for p in foreign if p not in run_records or p in held_foreign]
    return foreign, churn


def _bt_run_record_paths(paths: List[str]) -> List[str]:
    """The subset of ``paths`` under the warp run record root."""
    try:
        from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    except Exception:
        RECORD_DIR_ROOT = "state/warp"
    prefix = RECORD_DIR_ROOT + "/"
    return [p for p in paths if p.startswith(prefix)]


def _bt_run_record_paths_foreign(
    run_records: List[str], git_root: str, session_id: str
) -> List[str]:
    """The subset of ``run_records`` that is foreign to ``session_id``: a path a live
    peer's declaration contests (``contested_by_live_peers``) or a record dir a different
    live session's held run owns (``live_record_dir_owners``). A run this session submitted,
    or one nobody holds, is not foreign. Zero spawns. Either read failing (None or an
    exception) leaves every record path foreign."""
    try:
        from coordinator_core.session.scope import contested_by_live_peers

        held = contested_by_live_peers(run_records, session_id, cwd=git_root)
    except Exception:
        held = None
    if held is None:
        return list(run_records)
    try:
        from pathlib import Path

        from coordinator_core.ops.warp_run_deliverable import live_record_dir_owners

        owners = live_record_dir_owners(
            Path(git_root), run_records, exclude_session=session_id, cwd=git_root
        )
    except Exception:
        return list(run_records)
    return [p for p in run_records if held.get(p) or owners.get(p)]


def _bt_commit_subtree_foreign_paths_and_churn(
    abs_dir: str, cwd: Optional[str], git_root: str, session_id: str
) -> "tuple[List[str], int]":
    """C6 (Items 28/16.6, `docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-
    doe-thread.md`) -- the COMMIT-side analogue of
    `_bt_add_subtree_foreign_paths`. A `git commit -- <abs_dir>` names no new
    content (COMMIT never stages), so the WHICH-paths oracle is `git diff
    --cached --name-only -- <abs_dir>` (Check 5's own re-derivation pattern,
    already reused at the add site by that function's own docstring) rather
    than a dry-run add. Checked against THIS session's own claimed scope via
    the same `session.touch_record.project_live_claims` reader.

    Fails toward the EMPTY list (never flagging) on any ambiguity -- missing
    session_id, a failed/unparseable diff, or a degraded touch-record
    projection -- same soft/fail-open posture as the add-side sibling this
    mirrors (this guard is registered fail_closed=False)."""
    if not session_id or _bt_no_peer_can_share_the_index(git_root, session_id):
        return [], 0
    rc, out = _dc_git_probe._run_git(["diff", "--cached", "--name-only", "--", abs_dir], cwd)
    if rc != 0:
        return [], 0

    staged = [ln for ln in out.splitlines() if ln]
    if not staged:
        return [], 0

    try:
        from coordinator_core.session.touch_record import (
            project_live_claims,
            sink_path,
        )

        sid_dir = os.path.join(git_root, ".git", "coordinator-sessions", session_id)
        touch_record_path = sink_path(sid_dir)
        projection = project_live_claims(touch_record_path, cwd=git_root)
        if projection.degraded:
            return [], 0
        my_claims = set(projection.claims.keys())
    except Exception:
        return [], 0

    foreign = [p for p in staged if p not in my_claims]
    foreign, churn = _bt_drop_build_churn(foreign)
    run_records = _bt_run_record_paths(foreign)
    if run_records:
        held_foreign = set(_bt_run_record_paths_foreign(run_records, git_root, session_id))
        foreign = [p for p in foreign if p not in run_records or p in held_foreign]
    return foreign, churn
