"""coordinator_core.bash_guards._dc_rm -- guard family 3 of the
``dispatch_checks`` split: ``check_destructive_rm`` (block-destructive-rm.sh). Target-resolution and
touch-record helpers live in ``destructive_rm_support``; ``check_stale_write``
lives in ``stale_write_guard``.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.

CLOSED PORTING GAP (2026-07-31, incident + code-reviewer findings
1/2/3/4/5/6): the 2026-07-31 `~/.claude` destruction (see
`check_destructive_rm`'s own root-deny comment and
`test_check_destructive_rm_repo_root.py`) turned up four further gaps in the
same function beyond the root-resolution/identity fix itself: a bare
repository target bypassed the guard entirely (no `--show-toplevel` output
and no dirty-state fallback for a bare repo -- now probed via
`--is-bare-repository`); the root-deny and dirty-work branches spawned an
identical `git -C <dir> rev-parse --show-toplevel` twice per directory
target (now computed once and threaded through); the incident's own literal
command (`rm -rf ~/.Claude`) was STILL allowed post-fix because the
target-token loop never tilde-expanded `tgt` before its `os.path.exists`
gate, and unconditionally skipped any token containing `$` -- including
`$HOME`-relative targets, the same incident class under a different spelling
(now: `~` expanded via `os.path.expanduser`, and `$HOME`/`${HOME}` narrowly
resolved via `_expand_home_var`; other `$VAR`/glob tokens remain a known,
unchanged bound, see the token-loop comment and finding 8); and the deny
message overclaimed full history loss for a linked `git worktree add` target
(whose store lives in the main repo), now branched by `is_linked_worktree`.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards import _dc_git_probe, _unborn_scratch_repo
from coordinator_core.bash_guards._command_tokenizer import (
    split_unquoted_newlines as _bt_split_unquoted_newlines,
)
from coordinator_core.bash_guards._dc_git_probe import (
    _new_dirty_paths_memo,
    _rm_peer_claim_of,
)
from coordinator_core.bash_guards._dc_git_shared import _bt_leading_cd_prefix_cwd, _new_git_memo
from coordinator_core.bash_guards._dc_shell_text import (
    _join_backslash_newlines,
    _shell_c_unwrap_payloads,
    _split_segments,
    _strip_heredoc_bodies,
    _strip_q,
    _word_present,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    resolve_segments_for_dialect,
)
from coordinator_core.bash_guards._rewrite_support import _alt_record, _crlf_strip, _offer, _override, _ready
from coordinator_core.session_hub_path import HUB_NAME
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _PS_REMOVE_VERBS,
    _ps_normalize_verb_token,
    _ps_resolve_head_verb,
)
from coordinator_core.bash_guards.destructive_rm_support import (
    _STATUS_UNKNOWN,
    _abs_path,
    _attribute_porcelain_z,
    _expand_home_var,
    _is_same_dir,
    _is_within,
    _ps_recurse_flag,
    _resolves_under_repo_scratch,
    _rm_flush_touch,
    _rm_is_rm_segment,
)

_REGENERABLE_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _is_repo_regenerable_cache_dir(resolved: str) -> bool:
    """True when `resolved` (symlink/`..`-resolved) is a directory whose final component is
    exactly a regenerable tool cache name and which lies below a directory holding a `.git`
    entry. Filesystem stats only, no spawn."""
    cur = resolved.replace("/", os.sep).rstrip(os.sep)
    if os.path.basename(cur) not in _REGENERABLE_CACHE_DIRS or not os.path.isdir(cur):
        return False
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        if os.path.exists(os.path.join(parent, ".git")):
            return True
        cur = parent


# ---------------------------------------------------------------------------
# 3. check_destructive_rm -- block-destructive-rm.sh
# F0: per-target repo resolution (git -C "$(dirname TGT_ABS)"), never a
# single dispatcher-level root.
# ---------------------------------------------------------------------------

def check_destructive_rm(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
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

    # The `\brm\b` gate is BASH-shaped: PowerShell's `Remove-Item` and its
    # `ri`/`rd`/`del`/`erase` aliases never contain `rm`. Widen the gate
    # (cheap pre-filter only; real detection is `_PS_REMOVE_VERBS` below).
    #
    # Deliberately NOT gated on `dialect_from_tool_name(payload["tool_name"])`:
    # coordinator-content-repo's `_rearm_command_tool_name` rewrites a genuine `PowerShell`
    # tool call to `tool_name: "Bash"` before it reaches the dispatcher, so
    # `Remove-Item -Recurse -Force .git` must deny under BOTH tool_name values.
    # The verb scan always runs; the cost is a literal `Remove-Item`/`ri`/`rd`/
    # `del`/`erase` word in an unrelated bash command, not a realistic shape.
    #
    # Scanned on the raw command AND a quote-stripped copy: the shell resolves
    # `'r''m'` to `rm`, but `\brm\b` matched neither, so the guard never ran and
    # `'r''m' -rf <path>` was ALLOWED where `rm -rf <path>` was denied.
    #
    # WIDENING ONLY: this gate decides solely whether to LOOK; every downstream
    # check still requires a real rm segment, an existing target and
    # uncommitted work under it.
    _has_bash_rm_word = _word_present(r"\brm\b", cmd)
    _has_ps_remove_word = _word_present(r"(?i)\b(remove-item|ri|rd|del|erase)\b", cmd)
    if not _has_bash_rm_word and not _has_ps_remove_word:
        return None

    rm_override = _override("COORDINATOR_ALLOW_RM", payload=payload)
    # A relative target is relative to the session's cwd: on the warm engine the process
    # cwd is wherever the server booted, and an unresolved target is silently skipped.
    _rm_session_cwd = (payload or {}).get("cwd") or os.getcwd()
    # A leading `cd <dir> &&` moves the executed command's cwd, and with it the repo.
    _rm_cwd = _bt_leading_cd_prefix_cwd(cmd, _rm_session_cwd) or _rm_session_cwd

    # Per-call memo keyed on (cwd, args); deliberately NOT module-level (see
    # `_new_git_memo`).
    _memo_run_git = _new_git_memo()

    # Built once and threaded through `_rm_peer_claim_of` (see
    # `_new_dirty_paths_memo`), or its fallback spawns `git status` per target.
    _dirty_paths_lookup = _new_dirty_paths_memo()

    # Per-call, per-ROOT batched `git status --porcelain`: one spawn per repo root.
    #
    # LAZY, NOT A PRE-PASS: a pre-pass would spawn for targets the loop below
    # `continue`s past (scratch-allowlisted, nonexistent, claims-dir paths).
    #
    # `-z` output is unquoted and carries rename origins, so every entry is
    # attributable; an entry that is not reads as dirty for every target in the
    # root, never clean. A failed `git status` (rc != 0) reads dirty for every
    # target in the root, never clean.
    _status_by_root: Dict[str, Optional[Dict[str, str]]] = {}

    def _batched_status_for(root: str, tgt_abs: str, all_targets: List[str]) -> Optional[str]:
        if root not in _status_by_root:
            under = [t for t in all_targets if _is_within(t, root)]
            if not under:
                _status_by_root[root] = None
            else:
                rc_b, out_b = _dc_git_probe._run_git(
                    ["-C", root, "--no-optional-locks", "status", "--porcelain", "-z", "--"] + under
                )
                if rc_b != 0:
                    # Unknown is not clean: a failed or timed-out batch marks every
                    # target in the root dirty.
                    _status_by_root[root] = {
                        os.path.normcase(os.path.normpath(t)): "?? (git status failed; treated as uncommitted)"
                        for t in under
                    }
                else:
                    attributed = _attribute_porcelain_z(out_b, root, under)
                    if attributed is None:
                        # Unattributable entry: every target in the root reads the whole
                        # batch (never clean) rather than re-spawning per target.
                        whole = "\n".join(e for e in out_b.split("\0") if e)
                        attributed = {
                            os.path.normcase(os.path.normpath(t)): whole for t in under
                        }
                    _status_by_root[root] = attributed
        table = _status_by_root[root]
        if table is None:
            return _STATUS_UNKNOWN
        return table.get(os.path.normcase(os.path.normpath(tgt_abs)), _STATUS_UNKNOWN)

    cur_repo = None
    rc, out = _dc_git_probe._run_git(["rev-parse", "--show-toplevel"])
    if rc == 0:
        cur_repo = out.strip()

    # Every target this call lets through (bash `rm` or `git rm`) is collected
    # here and flushed as a TOUCH record ONLY at the final `return None`. Load-
    # bearing: an earlier `return _deny(...)` means the command never executes,
    # so nothing may have been recorded yet.
    # See `_rm_flush_touch`'s own docstring for why this is a plain TOUCH
    # (not a delete-flavored verb) and what it closes.
    _pending_rm_touch_paths: List[str] = []

    # A bare newline separates commands; unsplit, `rm a` + newline + `cd <repo>` reads
    # `cd` and `<repo>` as rm targets and denies the repo root.
    _rm_cmd = _bt_split_unquoted_newlines(cmd)
    _rm_segments = list(_split_segments(_rm_cmd))
    #: Segment string -> the flag token that declared recursion, or None when
    #: the segment was parsed and declared none. Only PowerShell-synthesized
    #: segments get an entry; a bash segment is absent and falls through to the
    #: free-text scan below, which is correct for bash and only for bash.
    _parsed_recursion = {}
    if _has_ps_remove_word:
        # Alias/flag resolution is table-driven and shared with
        # `block_subagent_destructive_action` (`_PS_REMOVE_VERBS`,
        # `_ps_resolve_head_verb`, `_ps_normalize_verb_token`), not a second
        # alias list. `resolve_segments_for_dialect` already expands
        # `Start-Process` forms and resolves backtick escapes and here-strings.
        #
        # Each recognized invocation is rewritten to an equivalent bash-shaped
        # `rm -rf <argv>` segment and appended to the list the loop below walks,
        # so both dialects share one deny ladder. `-Path`/`-LiteralPath` and
        # positional targets leave the target as the sole non-flag operand.
        # Gating is deliberately NOT conditioned on `-Recurse`/`-Force`: the
        # irreversibility is a property of the TARGET, as on the bash leg.
        _ps_segments = resolve_segments_for_dialect(
            _rm_cmd, Dialect.POWERSHELL, guard_name="destructive-rm"
        )
        if _ps_segments:
            for _ps_tokens, _ps_pipe_before in _ps_segments:
                if not _ps_tokens:
                    continue
                _ps_clean = [_ps_normalize_verb_token(t) for t in _ps_tokens]
                _ps_verb, _ps_rest = _ps_resolve_head_verb(_ps_clean)
                if _ps_verb not in _PS_REMOVE_VERBS:
                    continue
                # `-rf` is stamped only when recursion was declared: any
                # PowerShell flag CONTAINING an `r` (`-Force`) would otherwise
                # re-match the bash short-flag scan below and refuse a
                # single-file `Remove-Item -Force` as a recursive delete. A
                # `.git`/repo-root target is denied on `tgt_is_dir`, not this flag.
                _ps_recurse = _ps_recurse_flag(_ps_rest)
                _ps_seg = ("rm -rf " if _ps_recurse else "rm ") + " ".join(_ps_rest)
                _rm_segments.append(_ps_seg)
                _parsed_recursion[_ps_seg] = _ps_recurse
        # `_ps_segments is None` (unparseable PowerShell): no synthetic segment,
        # so the fail-open posture holds rather than the bash free-text scan
        # below misattributing a PowerShell parse failure as a fail-close.

    for seg in _rm_segments:
        if not seg.strip():
            continue

        # `git ... rm` never matches `_rm_is_rm_segment` below (its head word
        # must be `rm`; `git` is not in `_RM_WRAPPER_WORDS`), so this runs
        # BEFORE that gate. The deny ladder never runs for it (staged removal
        # is git-recoverable), but without `--cached` it deletes the
        # working-tree file too and still needs its own TOUCH.
        if re.search(
            r"\bgit(\s+(-C\s+\S+|-c\s+\S+|--(git-dir|work-tree|namespace)(=\S+|\s+\S+)|"
            r"--exec-path(=\S+|\s+\S+)?|-p|--paginate|--no-pager|--bare|"
            r"--no-replace-objects|--literal-pathspecs|--glob-pathspecs|"
            r"--noglob-pathspecs|--icase-pathspecs|--no-optional-locks))*\s+rm\b",
            seg,
        ):
            _gr_after = re.sub(r".*\bgit\b.*?\brm(\s|$)", " ", seg, count=1)
            if "--cached" not in _gr_after.split():
                # Directory and glob tokens name files git rm deletes without
                # naming them; ONE batched `ls-files` per segment expands them.
                _gr_specs: List[str] = []
                _gr_toks = _gr_after.split()
                _gr_nul = "--pathspec-file-nul" in _gr_toks
                _gr_pff = ""
                for _gr_i, _gr_tok in enumerate(_gr_toks):
                    if _gr_tok.startswith("--pathspec-from-file="):
                        _gr_pff = _strip_q(_gr_tok.split("=", 1)[1])
                    elif _gr_tok == "--pathspec-from-file" and _gr_i + 1 < len(_gr_toks):
                        _gr_pff = _strip_q(_gr_toks[_gr_i + 1])
                if _gr_pff and _gr_pff != "-" and "$" not in _gr_pff:
                    # Entries are raw git pathspecs (no shell expansion), so they
                    # go to ls-files verbatim, like the argv form of `git rm`.
                    try:
                        with open(
                            os.path.join(_rm_cwd, os.path.expanduser(_gr_pff)),
                            encoding="utf-8",
                        ) as _gr_fh:
                            _gr_body = _gr_fh.read()
                    except OSError:
                        _gr_body = ""
                    _gr_ents = _gr_body.split("\0" if _gr_nul else "\n")
                    _gr_specs.extend(
                        (e if _gr_nul else e.rstrip("\r")) for e in _gr_ents if e.strip()
                    )
                for _gr_tok in _gr_toks:
                    if _gr_tok == "--" or _gr_tok.startswith("-"):
                        continue
                    if _gr_tok == _gr_pff:
                        continue
                    if "$" in _gr_tok:
                        continue
                    _gr_t = _strip_q(_gr_tok)
                    if _gr_t.endswith(")"):
                        _gr_t = _gr_t[:-1]
                    if _gr_t.endswith("}"):
                        _gr_t = _gr_t[:-1]
                    if any(c in _gr_t for c in ("*", "?", "[")):
                        # `:(glob)` keeps shell semantics: `*` does not cross `/`.
                        _gr_specs.append(":(glob)" + _gr_t)
                        continue
                    _gr_resolved = os.path.join(_rm_cwd, os.path.expanduser(_gr_t)) if _gr_t else ""
                    if not _gr_resolved or not os.path.exists(_gr_resolved):
                        continue
                    if os.path.isdir(_gr_resolved):
                        _gr_specs.append(_gr_t)
                        continue
                    _pending_rm_touch_paths.append(_abs_path(_gr_resolved))
                if _gr_specs:
                    # Output is cwd-relative, so it joins against the same base
                    # the pathspecs resolved from.
                    _gr_rc, _gr_out = _dc_git_probe._run_git(
                        ["-C", _rm_cwd, "--no-optional-locks", "ls-files", "-z", "--"]
                        + _gr_specs
                    )
                    if _gr_rc == 0:
                        for _gr_rel in _gr_out.split("\0"):
                            if _gr_rel:
                                _pending_rm_touch_paths.append(
                                    _abs_path(os.path.join(_rm_cwd, _gr_rel))
                                )
            continue

        if not _rm_is_rm_segment(seg):
            continue
        # Quote-stripped like `_rm_is_rm_segment`'s `probe`: `'r''m'` is `rm` to
        # the shell, and the verb-strip needs a LITERAL `rm` token or `after`
        # stays the whole segment. Detector and extractor must read alike.
        seg_unquoted = seg.replace("'", "").replace('"', "")
        after = re.sub(r".*(^|\s)rm(\s|$)", " ", seg_unquoted, count=1)
        if seg in _parsed_recursion:
            recursion_flag = _parsed_recursion[seg]
        else:
            _rec_m = re.search(r"(^|\s)(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(\s|$)", after)
            recursion_flag = _rec_m.group(2) if _rec_m else None
        recursive = recursion_flag is not None

        if recursive and re.search(r"\$\(|`", after) and not rm_override:
            return _alt_record(
                "deny", "destructive-rm",
                "BLOCKED: 'rm' has a recursive flag and a subshell-resolved "
                "target ($(...) or backticks) — cannot verify what it would "
                "delete. Resolve it to a literal path first.",
                (_offer("git status --porcelain -- <resolved-path>", "the resolved path"),),
                report="the rm you need", authorize="a recursive rm of an unverifiable target",
                em_text=(
                    "BLOCKED: 'rm' has a recursive flag and a subshell-resolved "
                    "target ($(...) or backticks) — cannot verify what it would "
                    "delete.\n\n"
                    "Resolve it to a literal path first; use instead:\n"
                    "  git status --porcelain -- <resolved-path>"
                ),
            )

        targets: List[Tuple[str, str]] = []
        for tok in after.split():
            if tok == "--" or tok.startswith("-"):
                continue
            raw_tok = tok
            if "$" in tok:
                expanded = _expand_home_var(tok)
                if expanded is None or any(c in expanded for c in ("*", "?", "[")):
                    # A `$`/`${...}` reference the hook cannot resolve
                    # in-process is skipped (known bound: no shell environment).
                    # Only $HOME/${HOME} is resolved, the incident class.
                    continue
                tok = expanded
            elif any(c in tok for c in ("*", "?", "[")):
                continue
            t = _strip_q(tok)
            if t.endswith(")"):
                t = t[:-1]
            if t.endswith("}"):
                t = t[:-1]
            # `t_raw` is the as-typed token (before `$HOME` expansion), kept
            # apart from `t` so deny MESSAGES show the raw spelling.
            t_raw = _strip_q(raw_tok)
            if t_raw.endswith(")"):
                t_raw = t_raw[:-1]
            if t_raw.endswith("}"):
                t_raw = t_raw[:-1]
            targets.append((t_raw, t))

        for tgt, tgt_expanded in targets:
            # `os.path.exists`/`isdir` do not expand a leading `~`, so
            # `rm -rf ~/.Claude` was silently `continue`d before any guard leg
            # ran. Expand it (as `_offer_normalize_path` does) for every
            # filesystem probe; `tgt` stays as typed for deny MESSAGES, and
            # `tgt_expanded` carries the `$HOME`-expanded path.
            tgt_resolved = os.path.join(_rm_cwd, os.path.expanduser(tgt) if tgt.startswith("~") else tgt_expanded)
            if not tgt or not os.path.exists(tgt_resolved):
                continue
            # `rm` unlinks a symlink and never follows it (no trailing slash): judging it by what it
            # points at denied removing a shortcut to a repo.
            if os.path.islink(tgt_resolved) and not tgt_resolved.endswith(("/", os.sep)):
                continue
            tgt_abs = _abs_path(tgt_resolved)
            # Flushed only at the final `return None`; see the list's comment above.
            _pending_rm_touch_paths.append(tgt_abs)

            norm = tgt_abs.replace("\\", "/")
            _hub_seg = "/" + HUB_NAME + "/"
            if (
                _hub_seg in norm and "-claims/" in norm.split(_hub_seg, 1)[-1]
            ) or re.search("/" + re.escape(HUB_NAME) + r"/[^/]*-claims$", norm):
                if rm_override:
                    continue
                return _alt_record(
                    "deny", "destructive-rm",
                    "BLOCKED: '%s' is a coordinator claim lock dir — "
                    "regenerable session scratch, NOT git history. Do not rm "
                    "it by hand: the manual-rm path is how a live peer gets "
                    "stomped (CLAIM-CLEAR-LIVENESS in coordinator-tripwires.md)." % tgt,
                    (_offer(
                        "cs_clear_claim_if_dead <class> <basename> (lib/coordinator-session.sh; "
                        "refuses to clear a LIVE peer's claim)", "the class and basename"),),
                    report="the claim dir you need cleared", authorize="removing the claim lock dir",
                    em_text=(
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
                    ),
                )

            # `<repo>/scratch` is the sanctioned disposal area: nothing resolving
            # under it is guarded, repo roots and git stores included. Evaluated
            # before every git-store and repo-root deny below.
            if _resolves_under_repo_scratch(tgt_abs):
                continue

            # A regenerable tool cache inside a repo is not work: the next
            # run rebuilds it. Exact final-component match, resolved path only.
            if _is_repo_regenerable_cache_dir(tgt_abs):
                continue

            # A `*.lock` file under the git store holds no committed data: git
            # renames `index.lock` onto `index`, so unlinking it mid-flight only
            # fails that git command, and the index is regenerable. Hard blocks
            # are reserved for irreversible harm, so an EM must be able to clear
            # a git lock. This leg fires FIRST and narrowly; every non-lock
            # git-store path still meets the deny below.
            #
            # NEGATIVE SPEC -- do NOT turn this into an advisory that names
            # the reaper, however helpful that reads. `dispatch.py`'s guard
            # loop returns on the FIRST non-None envelope, so returning
            # anything here short-circuits every guard registered after this
            # one for the same command. A silent `continue` is the required
            # shape; the allow IS the message.
            is_git_store_target = norm.endswith("/.git") or "/.git/" in norm or os.path.basename(tgt_abs) == ".git"
            # Scoped to a lock FILE: a `.lock`-suffixed directory falls through
            # to the git-store deny below.
            if is_git_store_target and os.path.basename(tgt_abs).endswith(".lock") and os.path.isfile(tgt_abs):
                continue

            # The write-confinement bump's clear marker
            # (`allow-xrepo-write-<session-id>`, `_write_bump_marker.py`) is a
            # zero-byte sentinel under the gitdir; removing it only re-arms the
            # bump. Denying it strands a session with no way to put it back.
            # FILE only, as with `.lock`: a same-prefix directory is still denied.
            if (
                is_git_store_target
                and os.path.basename(tgt_abs).startswith("allow-xrepo-write-")
                and os.path.isfile(tgt_abs)
            ):
                continue

            # The doctrine-edit approval marker is a zero-byte file in the git
            # dir; removing it only withdraws an approval, and
            # `block_approval_sentinel_creation` documents removal as allowed.
            # FILE only; a same-named directory is still denied.
            if (
                is_git_store_target
                and os.path.basename(tgt_abs) == ".coordinator-doctrine-edit-approved"
                and os.path.isfile(tgt_abs)
            ):
                continue

            # The engine's own `claude-klabauter-*.json` caches sit directly under the
            # gitdir, keyed on (size, mtime_ns) and rebuilt on a miss -- not
            # objects, refs, or logs. A file only, directly under `.git`, so a
            # nested path or a directory of that name still meets the deny.
            if (
                is_git_store_target
                and os.path.basename(os.path.dirname(tgt_abs)) == ".git"
                and os.path.basename(tgt_abs).startswith("claude-klabauter-")
                and os.path.basename(tgt_abs).endswith(".json")
                and os.path.isfile(tgt_abs)
            ):
                continue

            # A target whose basename is literally `.git` is denied HERE with the
            # generic git-store message, before the bare-repo probe below, so a
            # `.git`-named bare repo gets the less specific wording.
            if is_git_store_target:
                if rm_override:
                    continue
                return _alt_record(
                    "deny", "destructive-rm",
                    "BLOCKED: 'rm' would delete part of the git store at "
                    "'%s'. This corrupts/destroys repository history "
                    "irreversibly — no checkout, reflog, or stash recovers it." % tgt, (),
                    report="the git-store removal you need", authorize="deleting part of the git store",
                    em_text=(
                        "BLOCKED: 'rm' would delete part of the git store at "
                        "'%s'. This corrupts/destroys repository history "
                        "irreversibly — no checkout, reflog, or stash recovers it." % tgt
                    ),
                )

            # A repo ROOT is the same irreversibility class as the `.git` target
            # above but reaches here as an ordinary directory. It is denied on
            # IDENTITY (resolves to a toplevel, or IS a bare repo), not on
            # dirty-state: once the store is gone there is no status to consult.
            #
            # Resolution is by st_dev/st_ino (`_is_same_dir`), never string
            # compare: on a case-insensitive filesystem `~/.Claude` and
            # `~/.claude` are the SAME directory while `os.path.normcase` (a
            # no-op off Windows) reports them distinct. That aliasing deleted a
            # live `~/.claude` on 2026-07-31.
            #
            # `tgt_top`/`tgt_is_bare` are computed ONCE here and reused by the
            # dirty-work branch below rather than re-spawning `_run_git`.
            tgt_is_dir = os.path.isdir(tgt_resolved)
            tgt_top = ""
            tgt_is_bare = False
            if tgt_is_dir:
                # F0-b: memoized -- safe to reuse if the same dir target
                # repeats (e.g. `rm -rf dirA dirA`); see `_new_git_memo`.
                rc_top, out_top = _memo_run_git(["-C", tgt_abs, "rev-parse", "--show-toplevel"])
                tgt_top = out_top.strip() if rc_top == 0 else ""
                if not tgt_top:
                    # `--show-toplevel` fails inside a BARE repo, which would then
                    # fall through both branches undenied, so probe for one. It
                    # also fails for any plain non-repo directory (the common
                    # case), so cheap `HEAD` + `objects/` checks gate the spawn:
                    # a genuine bare repo always has both.
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
                    if rm_override or _unborn_scratch_repo.is_unborn_scratch_repo(tgt_abs):
                        continue
                    clone_failed = (
                        None
                        if tgt_is_bare
                        else _unborn_scratch_repo.scratch_clone_failures(tgt_abs, _memo_run_git)
                    )
                    if clone_failed == []:
                        continue
                    store_desc = tgt_top if tgt_top else tgt_abs
                    # A LINKED worktree's store lives in the MAIN repo, so deleting
                    # it loses only local uncommitted changes: branch the claim.
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
                    failed_note = (
                        "Scratch clone not recoverable by re-clone, failed: %s.\n\n"
                        % ", ".join(clone_failed)
                        if clone_failed
                        else ""
                    )
                    return _alt_record(
                        "deny", "destructive-rm",
                        "BLOCKED: '%s' is the ROOT of a git repository (%s). "
                        "'rm' on it %s "
                        "is gone irreversibly, "
                        "plus all gitignored content (settings, caches, "
                        "credentials, session state) that no commit ever "
                        "held. %s"
                        "This deny is on identity, not on dirty-state: a "
                        "clean worktree does NOT make deleting the store "
                        "safe. Beware case-insensitive filesystems: a path that "
                        "LOOKS like a case-variant probe directory can be the "
                        "real one. To delete it anyway: move it under "
                        "<repo>/scratch, then delete it there."
                        % (tgt, store_desc, loss_desc, failed_note),
                        (
                            _offer(
                                'git -C "%s" log --branches --not --remotes --oneline (empty == nothing unpushed); '
                                'git -C "%s" stash list (stashes are never pushed); '
                                'git -C "%s" status --porcelain --ignored (untracked + gitignored content)'
                                % (tgt_abs, tgt_abs, tgt_abs),
                                "nothing; run to confirm a fully pushed CLONE before removing it",
                            ),
                            _ready('python3 -c "import os;print(os.stat(%r).st_ino)"' % tgt_abs),
                        ),
                        report="the repository removal you need", authorize="deleting a repository root",
                        em_text=(
                            "BLOCKED: '%s' is the ROOT of a git repository (%s). "
                            "'rm' on it %s "
                            "is gone irreversibly, "
                            "plus all gitignored content (settings, caches, "
                            "credentials, session state) that no commit ever "
                            "held.\n\n"
                            "%s"
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
                            '  python3 -c "import os;print(os.stat(%r).st_ino)"\n\n'
                            "To delete it anyway: move it under <repo>/scratch, "
                            "then delete it there."
                            % (tgt, store_desc, loss_desc, failed_note, tgt_abs, tgt_abs, tgt_abs, tgt_abs)
                        ),
                    )

            # Scratch allowlist.
            scratch_bn = os.path.basename(tgt_abs)
            scratch_par_abs = os.path.dirname(tgt_abs)
            # Memoized: identical argv for every target sharing this parent, and
            # for the file-target `probe_dir` call further below.
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
                # Asking the parent unconditionally silently skipped every target
                # whose parent sits outside a repo (`~/.claude`, `~/X/coordinator-content-repo`:
                # `~` and `~/X` are not repos), so the dirty-work protection
                # never ran on the paths with the most to lose.
                #
                # For a directory target, reuse `tgt_top` from the root-deny
                # branch above. A bare repo never reaches here (denied above).
                if tgt_is_dir:
                    root = tgt_top
                else:
                    probe_dir = os.path.dirname(tgt_abs)
                    # Memoized: same argv as `scratch_par_abs` above.
                    rc_r, out_r = _memo_run_git(["-C", probe_dir, "rev-parse", "--show-toplevel"])
                    root = out_r.strip() if rc_r == 0 else ""
                if not root:
                    continue
                # BATCHED per repo root: the pathspec varies per target, so the
                # (cwd, args) memo cannot absorb it. `_batched_status_for` fills
                # on first need (never a pre-pass; see its docstring).
                _all_tgt_abs = [
                    _abs_path(_joined)
                    for t, te in targets
                    if t
                    for _joined in [os.path.join(_rm_cwd, os.path.expanduser(t) if t.startswith("~") else te)]
                    if os.path.exists(_joined)
                ]
                out_st = _batched_status_for(root, tgt_abs, _all_tgt_abs)
                status = "\n".join(out_st.splitlines()[:9])
                if status:
                    peer_sid = _rm_peer_claim_of(tgt_abs, root, _dirty_paths_lookup)
                    if peer_sid:
                        return _alt_record(
                            "deny", "destructive-rm",
                            "BLOCKED (not overridable): '%s' holds "
                            "untracked/uncommitted work claimed by LIVE peer "
                            "session %s in this shared worktree. "
                            "Untracked files are byte-indistinguishable from "
                            "your own and git CANNOT recover them. If you "
                            "believe it is stale, confirm with the peer or "
                            "wait for their handoff — do not delete a live "
                            "peer's uncommitted work." % (tgt, peer_sid), (),
                            report="the removal you need", authorize="deleting a live peer's work",
                            em_text=(
                                "BLOCKED (not overridable): '%s' holds "
                                "untracked/uncommitted work claimed by LIVE peer "
                                "session %s in this shared worktree. "
                                "Untracked files are byte-indistinguishable from "
                                "your own and git CANNOT recover them. If you "
                                "believe it is stale, confirm with the peer or "
                                "wait for their handoff — do not delete a live "
                                "peer's uncommitted work." % (tgt, peer_sid)
                            ),
                        )
                    if rm_override:
                        continue
                    disp = "\n".join(status.splitlines()[:8])
                    more = "\n  ... and more (first 8 shown)" if len(status.splitlines()) > 8 else ""
                    # Name the flag when recursion is the ONLY reason this armed
                    # (a file target); a directory target needs no flag.
                    why = ""
                    if not tgt_is_dir and recursion_flag:
                        why = " (read as recursive: %s)" % recursion_flag
                    return _alt_record(
                        "deny", "destructive-rm",
                        "BLOCKED: 'rm' on '%s'%s would destroy uncommitted/"
                        "untracked work that git CANNOT recover (untracked "
                        "files and unstaged edits live in no commit, no "
                        "stash, no reflog):\n%s%s\n"
                        "Re-derive what would actually be lost rather than trusting a "
                        "remembered state; reserve irreversible deletion for genuinely "
                        "disposable, self-authored, uncontested paths."
                        % (tgt, why, disp, more),
                        (
                            _ready('git -C "%s" status --porcelain -- "%s"' % (root, tgt_abs)),
                            _ready(
                                'cp -r -- "%s" "${TMPDIR:-/tmp}/preserved-$(basename "%s")"'
                                % (tgt_abs, tgt_abs)
                            ),
                        ),
                        report="the removal you need", authorize="deleting uncommitted work",
                        em_text=(
                            "BLOCKED: 'rm' on '%s'%s would destroy uncommitted/"
                            "untracked work that git CANNOT recover (untracked "
                            "files and unstaged edits live in no commit, no "
                            "stash, no reflog):\n%s%s\n\n"
                            "Before overriding, re-derive what would actually be "
                            "lost (do not trust a remembered or narrated state):\n"
                            '  git -C "%s" status --porcelain -- "%s"\n\n'
                            "To preserve the work first, COPY it aside — never "
                            "stash on this tree, which a dozen sessions share "
                            "(`stash@{0}` is whoever stashed last, not you):\n"
                            '  cp -r -- "%s" "${TMPDIR:-/tmp}/preserved-$(basename "%s")"\n\n'
                            "Reserve irreversible deletion for genuinely "
                            "disposable, self-authored, uncontested paths."
                            % (tgt, why, disp, more, root, tgt_abs, tgt_abs, tgt_abs)
                        ),
                    )

    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too.
    #: The loop variable is the unwrapped COMMAND STRING, not the guard payload
    #: dict -- named apart from `payload` because binding it to that name shadowed
    #: this function's own `payload` parameter, so the recursion below silently
    #: re-scanned with caller context dropped and `_override` fell back to ambient
    #: process env. Forward the parameter explicitly (C14c).
    for unwrapped_cmd in _shell_c_unwrap_payloads(cmd):
        result = check_destructive_rm(unwrapped_cmd, session_id, payload=payload)
        if result is not None:
            return result

    # C9: reached only once every target above (bash `rm` AND `git rm`
    # alike) AND every unwrapped `sh -c`/`bash -c` sub-command cleared the
    # deny ladder with no `return _deny(...)` anywhere -- i.e. only when
    # this call is actually about to ALLOW the removal through. Flushing
    # here, and nowhere earlier, is what keeps a denied `rm` from ever
    # recording a touch for work that never actually happened.
    _rm_flush_touch(_pending_rm_touch_paths, session_id, cur_repo)
    return None
