"""coordinator_core.bash_guards._dc_commit -- commit-side guard families: check_validate_commit and the safe-commit advisor.

Folded: guard family 11 of the
``dispatch_checks`` split: ``check_validate_commit`` (validate-commit.sh) and
its subject / lessons-path helpers.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.

KNOWN PORTING GAPS (surfaced, not silently patched):
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

CLOSED PORTING GAP: ``check_validate_commit``'s Checks 9-11 (schema-version-bump
tripwire, bin/sh polyglot shebang tripwire, machine-path-leak guard) run
in-process in ``coordinator_core.bash_guards.commit_tripwires``, not via
``bin/*.sh`` subprocess-by-filename.

Folded: guard family of the
``dispatch_checks`` split: ``check_git_commit_safe_commit_advise`` (BX-16
shape 5, the scoped-commit advisory/deny) and its ``_bt_commit_*``
predicates.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING, Tuple
from coordinator_core import machine_path_leak
from coordinator_core.bash_guards import _dc_git_probe, _dc_git_shared, commit_tripwires
from coordinator_core.bash_guards._command_tokenizer import (
    _strip_heredocs as _bt_strip_heredocs,
    segments_from_tokens_with_pipe_flag as _bt_segments_from_tokens_with_pipe_flag,
    token_matches_binary as _bt_token_matches_binary,
    tokenize_full_command as _bt_tokenize_full_command,
)
from coordinator_core.bash_guards._dc_commit_named_stage import (
    named_set_refusal,
    named_stage_paths,
    unnamed_staged_paths,
)
from coordinator_core.bash_guards._dc_git_probe import (
    _BULK_FOREIGN_INDEX_PATHS,
    _NOTE_CORRECTION,
    _SAFE_COMMIT_ROUTE_CLAUSE,
    _bulk_foreign_index_refusal,
    _format_batched_scope_denial,
    _format_owner_sentence,
    _format_owner_token,
    _owner_name_provenance_note,
    _piped_exit_code_chain,
    _run_git,
    _truncate_to_budget,
)
from coordinator_core.bash_guards._dc_git_shared import (
    _BUNDLED_SHORT_M_RE,
    _GIT_COMMIT_OPT_WITH_ARG,
    _bt_commit_has_explicit_pathspec,
    _bt_commit_has_sweep_all_flag,
    _bt_commit_operand_scan,
    _bt_commit_own_pathspec,
    _bt_git_sequencer_in_progress,
    _bt_leading_cd_prefix_cwd,
    _bt_git_dash_c_value,
    _bt_probe_cwd as _bt_probe_cwd_unexpanded,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    expand_start_process_invocations,
    tokenize_command,
)
from coordinator_core.bash_guards._commit_route import COMMIT_ROUTE
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._merge_incoming import merge_incoming_paths
from coordinator_core.bash_guards._rewrite_support import (
    _ENV_ASSIGNMENT_TOKEN_RE,
    _GIT_WRAPPER_PREFIX_WORDS,
    _alt_record,
    _bt_git_resolved_subcommand,
    _crlf_strip,
    _offer, _override, _ready,
)
from coordinator_core.bash_guards.block_subagent_commit import (
    _bracket_candidate_exists_literally as bracket_candidate_exists_literally,
    _literal_pathspec_inner_path as literal_pathspec_inner_path,
)

if TYPE_CHECKING:
    from coordinator_core.session.scope import OwnerFact


def _vc(summary, members, env, report, authorize, *, kind="deny", guard="validate-commit", em_text=None):
    return _alt_record(kind, guard, summary, members, env, report=report, authorize=authorize, em_text=em_text)


def _scope_denial(text: str, foreign_hunk: bool) -> Any:
    """Strict-scope denial; `text` is the legacy batched-denial string, returned verbatim to the EM."""
    return _vc(
        text,
        (_offer(COMMIT_ROUTE, "the params file listing your literal paths, never a glob"),),
        "COORDINATOR_SCOPE_STRICT_OFF" if foreign_hunk else None,
        "the commit you need", "committing paths another session owns",
        em_text=text,
    )


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
    A bare `re.findall(...)[-1]` over
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


_LESSONS_LIVE_PREFIX = "state/lessons/"
_LESSONS_ARCHIVE_PREFIX = "archive/lessons-archived/"


def _lessons_archive_paired_paths(status_lines: Optional[List[str]]) -> Set[str]:
    """Staged paths that are one half of a lessons archival move.

    A `state/lessons/<f>` deletion is paired when the same index stages an
    `archive/lessons-archived/**/<f>` addition or modification of the same
    basename; the archive write is the evidence the ceremony ran. A rename
    record between the two prefixes pairs its destination. An unpaired
    deletion is not returned.
    """
    deleted: Dict[str, str] = {}
    archived: Set[str] = set()
    paired: Set[str] = set()
    for line in status_lines or []:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0][:1]
        if code in ("R", "C") and len(parts) >= 3:
            if (
                parts[1].startswith(_LESSONS_LIVE_PREFIX)
                and parts[2].startswith(_LESSONS_ARCHIVE_PREFIX)
                and posixpath.basename(parts[1]) == posixpath.basename(parts[2])
            ):
                paired.add(parts[2])
            continue
        path = parts[1]
        if code == "D" and path.startswith(_LESSONS_LIVE_PREFIX):
            deleted[path] = posixpath.basename(path)
        elif code in ("A", "M") and path.startswith(_LESSONS_ARCHIVE_PREFIX):
            archived.add(posixpath.basename(path))
    paired.update(path for path, base in deleted.items() if base in archived)
    return paired


def check_validate_commit(
    cmd: str,
    session_id: str = "",
    cwd: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    command = _crlf_strip(cmd) if cmd else ""
    if not command:
        return None
    # A leading `cd <dir> &&` moves the EXECUTED command's cwd away from
    # the payload's session-level `cwd` -- see `_bt_leading_cd_prefix_cwd`'s
    # docstring and state/bug-backlog/2026-08-27-destructive-git-and-scope-
    # guards-resolve-295928a71726.yaml. `None` (no leading `cd`, or an
    # unresolvable one) falls back to the pre-existing `cwd or None`
    # unchanged.
    _cwd = _bt_leading_cd_prefix_cwd(command, cwd or None) or cwd or None

    contains_git_commit = bool(re.match(r"^git\s+commit(\s|$)", command))
    if not contains_git_commit:
        for tok in re.split(r"&&|\|\||;", command):
            tok = tok.lstrip()
            if re.match(r"^git\s+commit(\s|$)", tok):
                contains_git_commit = True
                break
    if not contains_git_commit:
        return None

    # ONE status probe, three consumers: `staged` here, Check 5's deletion
    # re-add below, and Check 14 (undeclared-staged-deletion). This used to be
    # a `--name-only` call with a SECOND `--name-status` call further down for
    # the deletion re-add; folding them removes a process from the pathspec'd
    # commit path and adds none to the bare one. Process creation, not the
    # query, is what the brightline budget is spent on (DR-344), so a third
    # probe for Check 14 was not an option.
    #
    # `-M` is explicit but NOT a behaviour change: `diff.renames` defaults to
    # true (verified, git 2.43), so the `--name-status` call this replaces was
    # already reporting `R` for a `git mv` and the `--name-only` call was
    # already reporting only the rename's NEW path. Spelling it out keeps that
    # true if a repo or a future default turns rename detection off -- Check 14
    # must never fire on the archive path, which is a `git mv`.
    # The hook process's own GIT_INDEX_FILE is not the index the commit will
    # use; inheriting it made this probe read an empty/alternate index.
    _probe_env = {"GIT_INDEX_FILE": None}
    rc, staged_out = _dc_git_probe._run_git(["diff", "--cached", "--name-status", "-M"], _cwd, extra_env=_probe_env)
    _status_lines: Optional[List[str]] = staged_out.splitlines() if rc == 0 else None
    from coordinator_core.bash_guards._implausible_index import implausible_deletion_note
    _probe_note = implausible_deletion_note(
        _status_lines, _cwd,
        lambda a, c, **kw: _dc_git_probe._run_git(a, c, extra_env=_probe_env, **kw),
    )
    if _probe_note:
        return _vc(
            _probe_note, (), None, "the staged deletions you need", "committing suspect deletions",
            kind="advisory", em_text=_probe_note,
        )

    # Reconstruct exactly what `--name-only` returned: the NEW path for a
    # rename/copy (fields 1,2,3 -> take 3), the single path otherwise.
    staged = []
    for _line in _status_lines or []:
        if not _line:
            continue
        _parts = _line.split("\t")
        if len(_parts) >= 3 and _parts[0][:1] in ("R", "C"):
            staged.append(_parts[2])
        elif len(_parts) >= 2 and _parts[1]:
            staged.append(_parts[1])
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
    # C2: the pathspec-matched subset, captured BEFORE the staged-deletion
    # re-add loop below extends `commit_scope`. Used only to choose Check
    # 5's SCOPE advisory tail clause (b) vs (c) -- never re-derived with a
    # second git call.
    _pathspec_matched_set: Optional[Set[str]] = None
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
            _rc_scope, _scoped_out = _dc_git_probe._run_git(
                ["diff", "--cached", "--name-only", "--", *_own_pathspec], _cwd
            )
            if _rc_scope == 0:
                commit_scope = [l for l in _scoped_out.splitlines() if l]
                _pathspec_matched_set = set(commit_scope)
            # else: extractor ambiguity/failure -> commit_scope stays the
            # whole `staged` list (fail loud, never silent).

    # Staged DELETIONS keep warning unconditionally regardless of pathspec
    # narrowing -- the recorded incident shape. A deletion narrowed OUT of
    # `commit_scope` above (e.g. it sits outside the commit's own pathspec)
    # is re-added here rather than left silently dropped. A failed status
    # probe falls back to the whole index (fail loud), same posture as the
    # pathspec re-derivation above.
    #
    # Reuses `_status_lines` resolved at the top of this function -- this
    # block used to run its own second `--name-status` probe. Only true `D`
    # records are re-added, exactly as before: a rename's SOURCE side is not a
    # deletion and was never in this warn set (rename detection was already on
    # by default, so the probe this replaces was already collapsing `git mv`
    # into `R`).
    if commit_scope is not staged:
        if _status_lines is None:
            commit_scope = staged
        else:
            _scope_set = set(commit_scope)
            for _line in _status_lines:
                if not _line or not _line.startswith("D\t"):
                    continue
                _parts = _line.split("\t")
                if len(_parts) < 2 or not _parts[1]:
                    continue
                _deleted_path = _parts[1]
                if _deleted_path not in _scope_set:
                    _scope_set.add(_deleted_path)
                    commit_scope.append(_deleted_path)

    # R07: paths staged as a `D` record (a true deletion, never a rename's
    # source side -- `_status_lines` already collapsed `git mv` into `R`,
    # same reasoning as the re-add loop just above). Used only to pick the
    # owned-by-another-session deny's remedy direction: unstaging a
    # deletion is the safe direction (it restores the tracked content),
    # the OPPOSITE of unstaging an addition/modification (which discards
    # uncommitted content). Built once here, off the same probe, rather
    # than re-querying git per staged file.
    _staged_deletion_paths: Set[str] = {
        _line.split("\t", 1)[1]
        for _line in (_status_lines or [])
        if _line.startswith("D\t") and len(_line.split("\t")) >= 2
    }

    _lessons_archive_paired = _lessons_archive_paired_paths(_status_lines)

    # --- Check 5: Scoped staging -- warn-only by default (Phase 2). Strict
    # mode (COORDINATOR_SCOPE_STRICT=1) promotes this to a DENY (Phase 5 --
    # the bash's dormant strict-mode branch is now live here; see the module
    # docstring "KNOWN PORTING GAPS", CLOSED entry for this promotion). Reuses
    # compute_scope() -- no separate declared-scope carrier is introduced.
    #
    # FLIPPED TO DENY-BY-DEFAULT (2026-08-28, C6 of plan 2026-08-27-a-
    # pathspec-is-not-a-scope). The PM ruled for it and the retired census
    # gate below is superseded -- see that plan's C6 dispatch brief for the
    # ruling. Strict mode is now the default; COORDINATOR_SCOPE_STRICT_OFF is
    # the escape hatch back to warn-only.
    #
    # THE TWO BLOCKERS THIS COMMENT USED TO NAME ARE BOTH RETIRED. Do not
    # re-derive them from an older revision of this comment:
    #
    #   - "compute_scope misclassifies a session's OWN touched file as an
    #     orphan" was a BAD REPRO, withdrawn at `89ac844cc`. It wrote
    #     `touched.txt` -- the RETIRED record. The live one is
    #     `touch-record.jsonl`. Re-proved 2026-08-27 through the canonical
    #     writer (`session.scope.touch`) on a throwaway repo: a staged,
    #     touched `foo.txt` comes back `my_scope=['foo.txt'] orphans=[]`.
    #   - The three named reds (test_own_scoped_file_no_scope_warning,
    #     test_dispatched_agent_touched_file_no_scope_warning,
    #     test_foreign_file_owned_by_another_session_warns_with_owner) were
    #     repointed to the live record at `ca1929145` and are green.
    #
    # The successor handoff's "167 stranded claims" blocker is retired on the
    # same fault: those 167 live in `touched.txt` across 8 dirs, which
    # `_read_touch_record_as_legacy_lines` stopped reading on 2026-08-26. The
    # LIVE corpus is clean -- 265 `touch-record.jsonl` files, 2667 entries,
    # ZERO escaping the worktree (census 2026-08-27), and
    # `legacy_touch_corpus_straggler_check` reads 0 legacy-only dirs. Nothing
    # `compute_scope` reads can strand a claim. `migrate_touched_prefix` is
    # NOT the repair for them either: post-C1 it PRUNES `../`-escaping entries
    # rather than rescuing them, so "run it and see if it rescues them" is a
    # premise that tool cannot satisfy.
    #
    # THE ONE REMAINING GATE IS THE ORPHAN ARM, measured on production
    # traffic rather than argued -- for the current count, run
    # `scope_orphan_census.py` rather than trusting any number hand-copied
    # into this comment; every figure below is a snapshot, already stale by
    # the time the section below this one re-measured it. Under warn-only an
    # orphan is a spurious SCOPE line; under the flip it denies a commit the
    # session is entitled to make, which is the false-positive outage the
    # 2026-08-03 ruling below feared.
    #
    # The orphan arm was originally attributed to THREE causes, not one, and
    # the dominant one was not what it first looked like:
    #
    #   - DELETIONS: `compute_scope` Step 1 (`session/scope.py`) admits a
    #     touched-then-deleted path to `my_scope` with NO existence check,
    #     so the deletions that render `owner:orphan` are only the ones
    #     carrying no touch claim from anyone -- pinned to warn by ratified
    #     AC10 (`test_tracked_then_deleted_file_hits_mtime_epoch_zero_in_
    #     forgiveness_loop`), and split by C7 into attributable (`rm`/`git
    #     rm` run through Bash -- C9 now records a TOUCH for those, closing
    #     that half) and unattributable (a staged deletion no session ever
    #     claimed at all -- the sweep shape the flip must deny, never
    #     forgive; widening `my_scope` to cover it would defeat the flip's
    #     own purpose).
    #   - ARCHIVAL MOVES (5): `archive/handoffs/**`, `state/handoffs/**`.
    #     `archive_terminal_handoffs`/`archive_actioned_memos` are exempted
    #     from `relocate_touched_path` on the reasoned allow-list in
    #     `session/tests/test_no_untracked_relocation.py` for having no
    #     session_id in scope -- true of the mover, but it leaves the moved
    #     file unclaimed at the sink.
    #   - UNDECLARED OP OUTPUTS (4): `state/sizings/*.yaml` (3),
    #     `state/memo-outbox/sent/*` (1). Files no Edit/Write hook touches,
    #     whose writing op did not declare them through `ipc.py`'s
    #     `_SCOPE_TOUCH_PATHS_KEY`. `memo.reconcile_outbox` was one of these
    #     and now declares both ends of its moves; the sizings writer has not
    #     been traced yet.
    #
    # THE CENSUS ABOVE NO LONGER HOLDS (re-measured 2026-08-27, evidence and
    # method: state/audits/2026-08-27-the-orphan-arm-is-not-the-three-causes-
    # named.md). Across every `scope-warnings.log` in the repo, live and
    # archived: 150 orphan events over 22 paths, not 17 over 10 -- and NONE of
    # them archival-shaped, none on the deletion paths named above. Only the
    # sizings-writer line survives contact.
    #
    # The arm is dominated by a FOURTH cause not named above: long-tracked
    # source files staged with no touch record at all (`ops/eol/*.py` at 19
    # events apiece, 74+ between them). Something stages an edit to an
    # already-tracked file through a path that records no claim; that is what
    # the flip actually waits on, and it is untraced.
    #
    # The archival-mover seam named below IS built (`_common.declare_move_
    # claims`, declaring both ends of every landed move through ipc.py's
    # `_SCOPE_TOUCH_PATHS_KEY`) -- kept for the residual `archive_and_commit`
    # documents rather than for events on the board, since the live act path
    # commits its own moves and nothing has fired it automatically since
    # `abd587695`.
    #
    # So the flip gates on driving that arm to zero: close the deleted-path
    # asymmetry in the warn loop, TRACE THE FOURTH CAUSE (the arm's bulk),
    # trace the sizings writer, then re-read `scope-warnings.log` over a full
    # day and confirm only named-owner events remain. Re-census before acting
    # on any figure in this comment, including the corrected ones -- the count
    # accrues live. The archival-mover leg is done. ---
    scope_strict = not _override("COORDINATOR_SCOPE_STRICT_OFF", payload=payload)
    if session_id:
        git_root = _dc_git_probe._run_git(["rev-parse", "--show-toplevel"], _cwd)[1].strip()
        if git_root:
            sessions_root = os.path.join(git_root, ".git", "coordinator-sessions")
            session_dir = os.path.join(sessions_root, session_id)
            if os.path.isdir(session_dir):
                # lazy, per-call import.
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
                except Exception as _union_exc:  # noqa: BLE001 -- fail-open, but declared
                    from coordinator_core.bash_guards._verdict import record_silent

                    record_silent(
                        "validate-commit",
                        f"orphan scope union raised {_union_exc!r}; my_scope not widened",
                    )

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

                # C11 defect-1 fix (2026-09-02, example-cockpit-repo-em): a
                # dispatched subagent's own edit was reading as foreign. The
                # `_own_content_hashes` map above is built from THIS
                # session's OWN touch-record sink only; when an executor or
                # review-integrator THIS session dispatched edits a file the
                # EM had already recorded a hash for, the subagent's
                # PostToolUse hook (hooks/track_touched_files.py::_handler)
                # appends the new hash under the SUBAGENT's own session id,
                # into the agent-keyed sink `.agents/<aid>/touch-record.jsonl`
                # -- never into the EM's own record -- so the EM's recorded
                # hash goes stale and the file below reads as "a foreign
                # edit landed" even though it is this session's own
                # dispatched work.
                #
                # Fix: a hash recorded by an agent dir whose
                # `em-session-id.txt` back-pointer resolves to THIS
                # session_id counts as this session's own provenance too --
                # the exact self/other boundary
                # `session/scope.py::release_committed_claims` already draws
                # (its `.agents` scan + back-pointer read), reused rather
                # than inventing a second rule.
                #
                # LAZY BY CONSTRUCTION -- the `.agents` walk is O(agent dirs
                # on disk): measured 14µs/dir, 1893 dirs on this box (state/
                # audits/2026-08-25-the-two-unattributed-pids-are-conhost-
                # and-the-release-leg-is-a-directory-walk.md § Thread 2). It
                # must NOT run on every commit, so it is built on the FIRST
                # own-session hash disagreement only and memoised in
                # `_agent_owned_hashes_cache` for the rest of this loop --
                # mirrors `_head_tracked_paths_lazy`'s own list-cell cache
                # a few lines below. The clean path (every own-recorded hash
                # matches disk) never calls this and pays zero extra
                # syscalls -- exactly today's cost.
                #
                # Fail-open: any read/import failure anywhere in this lookup
                # means the state is unprovable, so it must NOT deny -- the
                # caller below only trusts a hash this function actually
                # returned; any failure here yields an empty map, which is
                # indistinguishable from "no self-agent hash found" and
                # falls through to the existing deny, same fail-open
                # contract as `compute_content_hash`'s own read above.
                _agent_owned_hashes_cache: List[Dict[str, str]] = []

                def _agent_owned_content_hashes() -> Dict[str, str]:
                    if _agent_owned_hashes_cache:
                        return _agent_owned_hashes_cache[0]
                    _built: Dict[str, str] = {}
                    try:
                        _agents_base = os.path.join(sessions_root, ".agents")
                        if os.path.isdir(_agents_base):
                            from coordinator_core.session import (
                                touch_record as _touch_record_mod_agent,
                            )
                            for _entry in os.scandir(_agents_base):
                                try:
                                    if not _entry.is_dir():
                                        continue
                                except OSError:
                                    continue
                                try:
                                    with open(
                                        os.path.join(
                                            _entry.path, "em-session-id.txt"
                                        ),
                                        "r",
                                        encoding="utf-8",
                                    ) as _fh:
                                        _em_sid = _fh.readline().strip()
                                except OSError:
                                    continue
                                if _em_sid != session_id:
                                    continue  # not this session's own fan-out
                                _agent_sink = os.path.join(
                                    _entry.path,
                                    _touch_record_mod_agent.RECORD_FILENAME,
                                )
                                try:
                                    _agent_projection = (
                                        _touch_record_mod_agent.project_live_claims(
                                            _agent_sink, cwd=_cwd
                                        )
                                    )
                                except Exception:
                                    continue
                                for _apath, _aevent in _agent_projection.claims.items():
                                    if _aevent.content_hash is not None:
                                        _built[_apath] = _aevent.content_hash
                    except Exception:
                        _built = {}
                    _agent_owned_hashes_cache.append(_built)
                    return _built

                # C11 (plan 2026-08-27-a-pathspec-is-not-a-scope): the third
                # granularity -- a foreign hunk landed inside a file THIS
                # session genuinely owns (the `bf6099f85` shape). C10 records
                # a whole-file sha256 alongside a TOUCH; this reads THIS
                # session's own live claims back (its own sink only -- an
                # agent-touched or mtime-forgiven path never had a fingerprint
                # recorded for it by this mechanism, so it is deliberately
                # left out of this map rather than guessed at) and compares
                # the recorded hash against disk-now for every path already
                # in `my_scope`.
                #
                # FAILURE DIRECTION (pinned, see this chunk's own brief): a
                # RECORDED hash that disagrees with disk is demonstrable --
                # refuse, naming the path. NO recorded hash (pre-C10 line,
                # a RELEASE, or -- today -- every write channel, since no
                # caller passes `content_hash` into `append_event` yet; C10's
                # own docstring: "this chunk records only") is NOT
                # demonstrable and must NEVER be read as foreignness -- it
                # falls through to the existing my_scope/orphan rendering
                # unchanged. Fail-open on any read failure (unreadable
                # record, import error): an unprovable state stays
                # unprovable, never promoted to a refusal.
                _own_content_hashes: Dict[str, str] = {}
                # This session's own LIVE claims, read before `compute_scope`
                # subtracts anything. `my_scope` is `own − ⋃(LIVE peers' claims
                # ∩ dirty)`, so a path this session genuinely recorded still
                # falls through to the foreign-owner arm below the moment a live
                # peer also claims it. Without this set that arm can only say
                # "not in this session's touch list", which is FALSE in exactly
                # the case whose printed remedy cannot work — recording the path
                # again lands it straight back in the subtracted set.
                _own_claimed_paths: set = set()
                if my_scope is not None:
                    try:
                        from coordinator_core.session import touch_record as _touch_record_mod
                        _own_sink = os.path.join(
                            session_dir, _touch_record_mod.RECORD_FILENAME
                        )
                        _own_projection = _touch_record_mod.project_live_claims(
                            _own_sink, cwd=_cwd
                        )
                        _own_claimed_paths = {
                            _path
                            for _path, _event in _own_projection.claims.items()
                            if _event.session_id == session_id
                        }
                        _own_content_hashes = {
                            _path: _event.content_hash
                            for _path, _event in _own_projection.claims.items()
                            if _event.session_id == session_id
                            and _event.content_hash is not None
                        }
                    except Exception:
                        _own_content_hashes = {}
                        _own_claimed_paths = set()

                # C6b item 2 (2026-08-27-a-pathspec-is-not-a-scope): a staged
                # file that no longer exists ANYWHERE on disk AND was never
                # tracked at HEAD (a never-committed staged-then-vanished
                # path, AC10 case (a)'s strict half) is DIRECTLY OBSERVABLE
                # -- disk absence, not an ownership accusation -- so it gets
                # its own predicate and its own denial text, independent of
                # `_owner_is_provable`. Computed once (one `git ls-tree`
                # spawn), not per staged file. Fails OPEN: `None` (the git
                # call errored) never denies -- an indeterminate HEAD listing
                # is not proof of anything, same posture as every other arm
                # here.
                _head_tracked_cache: List[Optional[set]] = []

                def _head_tracked_paths_lazy() -> Optional[set]:
                    """Resolve HEAD's tracked set at most once per call, and
                    ONLY when a staged path is already known to be missing
                    from disk.

                    Measured on this repo 2026-08-28: the `ls-tree` costs
                    ~36ms and materializes ~34.8k entries, against a 500ms
                    end-to-end brightline. The predicate it feeds fires only
                    for a staged-then-vanished path, which is rare, so paying
                    it eagerly spent that on EVERY commit to answer a
                    question almost none of them ask. The cheap `os.path.
                    exists` check gates this one; do not hoist it back above
                    that check for tidiness.
                    """
                    if _head_tracked_cache:
                        return _head_tracked_cache[0]
                    _resolved: Optional[set] = None
                    try:
                        _rc_head, _head_out = _dc_git_probe._run_git(
                            ["ls-tree", "-r", "--name-only", "HEAD"], _cwd
                        )
                        if _rc_head == 0:
                            _resolved = {l for l in _head_out.splitlines() if l}
                    except Exception:
                        _resolved = None
                    _head_tracked_cache.append(_resolved)
                    return _resolved

                def _record_scope_event(_verdict: str, _warned: List[str]) -> None:
                    try:
                        from coordinator_core.session.commit_scope_events import (
                            record_commit_attempt,
                        )
                        record_commit_attempt(
                            session_id=session_id,
                            session_dir=session_dir,
                            repo_root=git_root,
                            # The event's `staged` field is meant to describe
                            # what THIS commit actually scopes, not the whole
                            # index -- `commit_scope` narrows to the pathspec
                            # when one is present (see its own derivation
                            # above); passing `staged` here recorded the
                            # whole-index list even for a scoped commit.
                            staged=commit_scope,
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

                # C12 (report: three independent sessions hit the same drip
                # 2026-09-11): the loop below used to `return _deny(...)` on
                # the FIRST offending path across all four arms (foreign
                # hunk, vanished-from-disk, contested, unclaimed), so a
                # multi-path staged set surfaced exactly one name per
                # clearing attempt with no way to see the at-risk set's
                # size. `_deny_entries` collects every arm's own
                # already-composed text instead of returning immediately;
                # the walk still finishes in the same per-file cost as
                # before (every predicate an arm below consults --
                # `_own_content_hashes`, `_agent_owned_content_hashes()`,
                # `_head_tracked_paths_lazy()` -- was already being computed
                # per staged file or lazily-cached once for the whole call;
                # deferring the return adds no new git spawn). One combined
                # deny is emitted after the walk by
                # `_format_batched_scope_denial`, which also caps how many
                # paths it prints in full (see that function's docstring)
                # so the message itself cannot grow unbounded on a
                # thousand-path index.
                _deny_entries: List[Dict[str, str]] = []

                _merge_incoming_cache: List[Set[str]] = []  # one probe per command

                def _merge_incoming() -> Set[str]:
                    if not _merge_incoming_cache:
                        _merge_incoming_cache.append(merge_incoming_paths(
                            _bt_git_sequencer_in_progress, _run_git,
                            _commit_seg_tokens or [], payload, _cwd))
                    return _merge_incoming_cache[0]

                # A path this session was already told about, that this commit's own
                # pathspec leaves staged, is told once: the log below is the record.
                _already_warned: Set[str] = set()
                try:
                    with open(
                        os.path.join(session_dir, "scope-warnings.log"), encoding="utf-8"
                    ) as fh:
                        for _log_line in fh:
                            _cols = [c.strip() for c in _log_line.split("|")]
                            if len(_cols) >= 4 and _cols[2] == "foreign-staged":
                                _already_warned.add(_cols[3])
                except OSError:
                    pass

                for staged_file in commit_scope:
                    if staged_file in _lessons_archive_paired:
                        continue
                    if my_scope is not None and staged_file in my_scope:
                        _own_hash = _own_content_hashes.get(staged_file)
                        if _own_hash is not None:
                            try:
                                from coordinator_core.session.touch_record import (
                                    compute_content_hash as _compute_content_hash,
                                )
                                _abs_staged = (
                                    os.path.join(git_root, staged_file)
                                    if git_root
                                    else staged_file
                                )
                                _current_hash = _compute_content_hash(_abs_staged)
                            except Exception:
                                _current_hash = None
                            # `_current_hash is None` means unreadable (deleted
                            # between stage and this check, a race, an import
                            # failure) -- NOT demonstrable, so this never
                            # denies on a `None` read (same fail-open posture
                            # as `compute_content_hash`'s own contract).
                            if _current_hash is not None and _current_hash != _own_hash:
                                # D2 (docs/research/2026-09-26-foreign-hunk-
                                # discriminator.md, H4): `compute_content_hash`
                                # has no EOL awareness -- a CRLF checkout or an
                                # EOL-repair pass rewriting the SAME logical
                                # content reproduces this identical disagreement.
                                # Re-hash disk-now with CRLF/CR normalized to LF
                                # and excuse the disagreement if THAT matches the
                                # recorded hash -- this only excuses a byte-for-
                                # byte-after-normalization match, so a genuinely
                                # foreign edit (different content, not just line
                                # endings) still falls through and denies below.
                                try:
                                    _current_bytes = Path(_abs_staged).read_bytes()
                                    _eol_normalized_hash = hashlib.sha256(
                                        _current_bytes.replace(
                                            b"\r\n", b"\n"
                                        ).replace(b"\r", b"\n")
                                    ).hexdigest()
                                except OSError:
                                    _eol_normalized_hash = None
                                if (
                                    _eol_normalized_hash is not None
                                    and _eol_normalized_hash == _own_hash
                                ):
                                    continue
                                # First disagreement -- pay the lazy `.agents`
                                # walk (see `_agent_owned_content_hashes`'s own
                                # docstring above). A self-back-pointed
                                # dispatched agent that recorded exactly this
                                # disk-now content is this session's own
                                # provenance, not a foreign edit.
                                _agent_hash = _agent_owned_content_hashes().get(
                                    staged_file
                                )
                                if _agent_hash is not None and _agent_hash == _current_hash:
                                    continue
                                # A merge conclusion stages the merge's own rewrite of this path.
                                if staged_file in _merge_incoming():
                                    continue
                                _fh_note = operator_override_note(
                                    "COORDINATOR_SCOPE_STRICT_OFF",
                                    payload=payload,
                                    git_root=git_root,
                                )
                                _deny_entries.append({
                                    "path": staged_file,
                                    "kind": "foreign hunk",
                                    "text": (
                                        "BLOCKED (foreign hunk): %s is staged and this "
                                        "session owns it, but its on-disk content no "
                                        "longer matches the content this session last "
                                        "recorded for it.\n\n"
                                        "Confirm the staged content is genuinely this "
                                        "session's own work (git diff --cached -- %s) "
                                        "before committing, or coordinate with "
                                        "whoever else touched it."
                                        % (staged_file, staged_file)
                                        + ("\n\n" + _fh_note if _fh_note else "")
                                    ),
                                })
                        continue

                    # C6b item 2: own predicate, own reason -- a staged path
                    # never tracked at HEAD and absent from disk right now.
                    # This does NOT route through `_owner_is_provable`: the
                    # provable fact here is the disk absence itself, not a
                    # claim about who owns it.
                    if scope_strict and not compute_scope_raised:
                        _abs_staged_vanish = (
                            os.path.join(git_root, staged_file)
                            if git_root
                            else staged_file
                        )
                        _head_tracked_paths = (
                            _head_tracked_paths_lazy()
                            if not os.path.exists(_abs_staged_vanish)
                            else None
                        )
                        if (
                            _head_tracked_paths is not None
                            and staged_file not in _head_tracked_paths
                        ):
                            _deny_entries.append({
                                "path": staged_file,
                                "kind": "vanished from disk",
                                "text": (
                                    "BLOCKED (strict scope): %s is staged but no "
                                    "longer exists on disk and was never "
                                    "committed -- its content cannot be "
                                    "verified.\n\nUnstage it (git restore "
                                    "--staged %s) or restore the file before "
                                    "committing." % (staged_file, staged_file)
                                ),
                            })
                            continue

                    # A merge conclusion must carry the merged tree, so the merge's own paths are not a sweep.
                    if staged_file in _merge_incoming():
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
                    # DENY). PM-RULED 2026-08-03, and this arm's warn-only
                    # SURVIVES the 2026-08-28 deny-by-default flip: strict
                    # mode now applies to the arm below, but a raising
                    # compute_scope() still may not deny, because the outage
                    # shape that ruling names is unchanged by the flip.
                    # Do NOT "complete" AC7 by deleting the
                    # `not compute_scope_raised` guard below on the grounds
                    # that fail-closed is always safer; that reading is
                    # what this ruling settles against.
                    _warned_paths.append(staged_file)

                    # Deny only on a PROVABLE owner. `claim_source` in
                    # ("session", "agent") means another session's claim was
                    # actually read for this path; every other rendering --
                    # no fact at all, "agent-race", "unreadable" -- is an
                    # ABSENT or INDETERMINATE write record, which is not
                    # evidence a foreign edit happened. Denying there accuses
                    # on state the engine cannot demonstrate, and it is the
                    # majority case: measured 2026-08-28, 22 of 37 committing
                    # sessions had no session directory at all
                    # (state/audits/2026-08-28-the-census-denominator-is-
                    # sessions-that-were-never-instrumented.md). A guard that
                    # denies the common case is overridden by habit within a
                    # day, which costs more than the sweep it prevents -- the
                    # surviving half of the 2026-08-03 ruling above, whose
                    # warn-only default the PM lifted on 2026-08-28.
                    _owner_is_provable = (
                        owner_fact is not None
                        and owner_fact.claim_source in ("session", "agent")
                    )

                    if scope_strict and not compute_scope_raised and _owner_is_provable:
                        # BULK IS TESTED FIRST, ahead of the CONTESTED and
                        # unclaimed arms, because the index size decides
                        # whether ANY per-file remedy is appropriate and the
                        # per-path facts do not -- still an immediate,
                        # single-shot return: the whole index is a peer's
                        # in-flight change at this size, and nothing else
                        # this loop could discover about the remaining
                        # paths changes that answer, so paying to enumerate
                        # the rest would only cost more without adding
                        # information the operator can act on.
                        if len(commit_scope) > _BULK_FOREIGN_INDEX_PATHS:
                            _record_scope_event("deny", _warned_paths)
                            return _scope_denial(
                                _bulk_foreign_index_refusal(
                                    len(commit_scope),
                                    staged_file,
                                    owner_sentence,
                                    _owner_name_provenance_note(owner_sentence),
                                ),
                                False,
                            )
                        if staged_file in _own_claimed_paths:
                            # CONTESTED, not unclaimed. Say so, and do NOT print
                            # the record-it-as-touched remedy: this session
                            # already recorded it and contention subtracted it
                            # anyway, so that sentence teaches its reader that
                            # the remedy does not work (measured live
                            # 2026-08-30 — the author unstaged and abandoned
                            # three of the close's own artifacts).
                            _deny_entries.append({
                                "path": staged_file,
                                "kind": "contested",
                                "text": (
                                    "BLOCKED (strict scope): %s is claimed by BOTH "
                                    "this session and %s, and a live peer's claim "
                                    "wins — recording it again will not clear this."
                                    "%s\n\n"
                                    "Unstage it (git restore --staged %s). Two "
                                    "causes: this session's write was recorded "
                                    "under the wrong id, or a peer commit landed "
                                    "in this file after this session's last read "
                                    "and this write discarded it. "
                                    "git log --oneline -3 -- %s tells you which."
                                    % (
                                        staged_file,
                                        owner_sentence,
                                        _owner_name_provenance_note(owner_sentence),
                                        staged_file,
                                        staged_file,
                                    )
                                ),
                            })
                            continue
                        # R07: a staged DELETION is the one case where
                        # unstaging is the safe direction -- it restores the
                        # owner's still-tracked content, the opposite of an
                        # addition/modification, where unstaging discards
                        # content the owner has not committed. Branch the
                        # remedy sentence on that fact rather than telling
                        # every kind to avoid the safe move for a deletion.
                        if staged_file in _staged_deletion_paths:
                            _owned_deletion_text = (
                                "BLOCKED (strict scope): %s is staged as a "
                                "deletion but not in this session's touch "
                                "list — owned by %s.%s\n\n"
                                "%s\n\n"
                                "Unstaging it (git restore --staged %s) is "
                                "the safe direction here: it restores the "
                                "owner's still-tracked content. Only record "
                                "it as touched first if this deletion "
                                "genuinely belongs to this session's work."
                                % (
                                    staged_file,
                                    owner_sentence,
                                    _owner_name_provenance_note(owner_sentence),
                                    _SAFE_COMMIT_ROUTE_CLAUSE,
                                    staged_file,
                                )
                            )
                        else:
                            _owned_deletion_text = (
                                "BLOCKED (strict scope): %s is staged but not in "
                                "this session's touch list — owned by %s.%s\n\n"
                                "%s\n\n"
                                "If it genuinely belongs to this session's work, "
                                "record it as touched first. Unstaging it (git "
                                "restore --staged %s) discards content the owner "
                                "has not committed."
                                % (
                                    staged_file,
                                    owner_sentence,
                                    _owner_name_provenance_note(owner_sentence),
                                    _SAFE_COMMIT_ROUTE_CLAUSE,
                                    staged_file,
                                )
                            )
                        _deny_entries.append({
                            "path": staged_file,
                            "kind": "owned by another session",
                            "text": _owned_deletion_text,
                        })
                        continue

                    # C2 tail clause: says whether THIS command sweeps
                    # `staged_file` in, without suppressing the advisory
                    # (NEGATIVE SPEC above `commit_scope`'s derivation --
                    # pathspec presence must never suppress this warning).
                    #   (a) no explicit pathspec on the commit -- the bare
                    #       commit sweeps every staged path in, this one
                    #       included.
                    #   (b) the own pathspec resolved and this path is a
                    #       staged deletion re-added outside it -- it lies
                    #       outside this commit's pathspec, so this commit
                    #       leaves it staged.
                    #   (c) anything else (the commit's own pathspec names
                    #       this path, or the pathspec could not be resolved
                    #       statically) -- no clause, text unchanged from
                    #       HEAD.
                    if not _pathspec_scoped:
                        _scope_tail = " This bare commit sweeps it in."
                    elif (
                        _pathspec_matched_set is not None
                        and staged_file not in _pathspec_matched_set
                    ):
                        _scope_tail = (
                            " This commit's pathspec does not name it; it "
                            "stays staged."
                        )
                    else:
                        _scope_tail = ""
                    if _scope_tail.startswith(" This commit's pathspec") and staged_file in _already_warned:
                        continue

                    warnings.append(
                        "SCOPE: %s is staged but not in this session's touch "
                        "list — likely owned by %s. Strict mode would block "
                        "this commit.%s%s"
                        % (
                            staged_file,
                            owner_sentence,
                            _owner_name_provenance_note(owner_sentence),
                            _scope_tail,
                        )
                    )

                if _deny_entries:
                    _record_scope_event(
                        "deny", _warned_paths + [e["path"] for e in _deny_entries]
                    )
                    return _scope_denial(
                        _format_batched_scope_denial(_deny_entries),
                        any(e["kind"] == "foreign hunk" for e in _deny_entries),
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
    from coordinator_core.claude_md_budget import budget_verdict, load_audience_manifest

    _repo_root_for_governance = _dc_git_probe._run_git(["rev-parse", "--show-toplevel"], _cwd)[1].strip()
    _audience_manifest = (
        load_audience_manifest(_repo_root_for_governance) if _repo_root_for_governance else []
    )
    _budget_candidates = [
        f for f in staged if re.search(r"(^|/)CLAUDE\.md$", f) or f in _audience_manifest
    ]
    _budget_blobs: Dict[str, Optional[str]] = {}
    _head_blobs: Dict[str, Optional[str]] = {}

    def _budget_read_post(path: str) -> Optional[bytes]:
        if not _budget_blobs:
            _budget_blobs.update(_dc_git_probe._batch_show_index_blobs(_budget_candidates, _cwd))
        blob = _budget_blobs.get(path)
        return blob.encode("utf-8") if blob is not None else None

    def _budget_read_head(path: str) -> Optional[bytes]:
        if not _head_blobs:
            _head_blobs.update(_dc_git_probe._batch_show_index_blobs(_budget_candidates, _cwd, ref="HEAD"))
        blob = _head_blobs.get(path)
        return blob.encode("utf-8") if blob is not None else None

    _budget = budget_verdict(
        _repo_root_for_governance, staged, _budget_read_post, _budget_read_head
    )
    hard_violation = "".join(_budget.hard)
    soft_names = "".join(_budget.soft)

    if soft_names:
        warnings.append(
            "CLAUDEMD-BUDGET (soft):%s\n  → Approaching 40K "
            "perf warning. Demote a section to docs/wiki/ before the next "
            "addition." % soft_names
        )

    if hard_violation and not _override("COORDINATOR_OVERRIDE_CLAUDEMD_BUDGET", payload=payload):
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
        return _vc(
            "BLOCKED: staged CLAUDE.md exceeds 40K char limit (Claude Code "
            "perf warning threshold):%s" % hard_violation,
            (_offer("demote a section to docs/wiki/ and replace it with a pointer", "the section"),),
            "COORDINATOR_OVERRIDE_CLAUDEMD_BUDGET", "the CLAUDE.md change you need", "an oversized CLAUDE.md",
            em_text=reason,
        )

    if hard_violation and _override("COORDINATOR_OVERRIDE_CLAUDEMD_BUDGET", payload=payload):
        warnings.append("CLAUDEMD-BUDGET (override):%s" % hard_violation)

    # Never blocks: advisory only. Reuses the blob readers above (one cached
    # batch per ref), so no per-file git spawn.
    for _doc_path in _budget_candidates:
        _post_bytes = _budget_read_post(_doc_path)
        if _post_bytes is None:
            continue
        _head_bytes = _budget_read_head(_doc_path)
        _head_lines = set((_head_bytes or b"").decode("utf-8", "replace").splitlines())
        _added = [
            ln
            for ln in _post_bytes.decode("utf-8", "replace").splitlines()
            if ln.strip() and ln not in _head_lines
        ]
        if not _added:
            continue
        _changelog_shaped = any(
            re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b[0-9a-f]{10,40}\b", ln) or ln.lstrip().startswith(">")
            for ln in _added
        )
        warnings.append(
            "CLAUDE.md CHANGE (%s): always-loaded doctrine, read by every session. "
            "Enforce the rule in code or a tripwire, not doctrine prose.%s"
            "\n  → Unless the PM asked for this exact text, unstage it and file the rule as a guard, "
            "test or op."
            % (
                _doc_path,
                " A dated, SHA-tagged or quoted addition is a changelog, which CLAUDE.md never is."
                if _changelog_shaped
                else "",
            )
        )

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
        _rc_d, diff_out = _dc_git_probe._run_git(
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
                (
                    "FRONTMATTER-MUTATION: staged file(s)%s edit "
                    "load-bearing frontmatter with no matching key/verb "
                    "in the commit subject — git log -- <file> loses the "
                    "audit trail.\n\n"
                    "Name it instead:\n"
                    "  status: / claim / ship / closed / supersede\n"
                    "  coordinator/CLAUDE.md § Handoff Lineage"
                )
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
    # see `machine_path_leak.fixture_suppressible`.
    settings_staged = [f for f in staged if machine_path_leak.is_settings_json(f)]
    for sf in settings_staged:
        detail = commit_tripwires.check_machine_path_leak(sf, _cwd)
        if detail and not machine_path_leak.fixture_suppressible(sf, detail):
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
            return _vc(
                reason, (_offer("move the path into gitignored settings.local.json", "the path"),), None,
                "the settings.json change you need", "a machine-specific path in settings.json",
                em_text=reason,
            )

    # Check 12 -- registration-quad-completeness -- REGISTRATION-QUAD-INVARIANT.
    # Unlike Checks 9-11, this is a HARD DENY by default (see commit_tripwires.
    # check_registration_quad_completeness's own docstring for the fail-open
    # rationale: an incomplete quad is a data-cost bug, not a style nit),
    # subject to COORDINATOR_OVERRIDE_REGISTRATION_QUAD downgrading it to an
    # advisory, mirroring the CLAUDEMD-BUDGET override branch above.
    registration_quad_violation = commit_tripwires.check_registration_quad_completeness(_cwd, staged)
    if registration_quad_violation:
        if _override("COORDINATOR_OVERRIDE_REGISTRATION_QUAD", payload=payload):
            warnings.append("REGISTRATION-QUAD-TRIPWIRE (override):\n%s" % registration_quad_violation)
        else:
            _quad_note = operator_override_note(
                "COORDINATOR_OVERRIDE_REGISTRATION_QUAD", payload=payload, git_root=_repo_root_for_governance
            )
            reason = (
                "%s" % registration_quad_violation
            ) + ("\n\n" + _quad_note if _quad_note else "")
            return _vc(
                registration_quad_violation,
                (_offer("complete the registration quad named above", "the missing registration"),),
                "COORDINATOR_OVERRIDE_REGISTRATION_QUAD",
                "the incomplete registration you need", "an incomplete registration quad",
                em_text=reason,
            )

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

    # Check 14 -- undeclared-staged-deletion -- UNDECLARED-STAGED-DELETION.
    # Advisory only, and deliberately so -- see commit_tripwires.
    # check_undeclared_staged_deletion's own docstring for the deny-vs-warn
    # call (it cannot read a `-F`/editor message, so it fails open on a real
    # fraction of commits, and denying only the readable half would buy
    # compliance rather than safety).
    #
    # Reuses `_commit_seg_tokens` and `_status_lines` already resolved above:
    # this check adds ZERO processes to the commit hot path.
    if _status_lines is not None:
        # A resolved pathspec commit carries only the paths it names; a deletion
        # it leaves staged is not this commit's removal.
        _carried_status = _status_lines
        if _pathspec_matched_set is not None:
            _carried_status = [
                l for l in _status_lines
                if l.split("\t")[-1] in _pathspec_matched_set
            ]
        undeclared_deletion_violation = commit_tripwires.check_undeclared_staged_deletion(
            _commit_seg_tokens, _carried_status, payload=payload
        )
        if undeclared_deletion_violation and not _override(
            "COORDINATOR_OVERRIDE_UNDECLARED_DELETION", payload=payload
        ):
            warnings.append(undeclared_deletion_violation)

    # Check 15 -- trailer-demoted-to-body -- TRAILER-DEMOTED-TO-BODY.
    # Advisory only -- see commit_tripwires.check_trailer_demoted_to_body's
    # own module comment block for the recorded incident (state/bug-backlog/
    # 2026-09-19-a-blank-line-turns-a-git-trailer-into-bo-964db9e54ea6.yaml).
    # Reuses `_commit_seg_tokens` already resolved above: this check adds
    # ZERO processes to the commit hot path.
    trailer_demoted_violation = commit_tripwires.check_trailer_demoted_to_body(
        _commit_seg_tokens, payload=payload
    )
    if trailer_demoted_violation and not _override(
        "COORDINATOR_OVERRIDE_TRAILER_DEMOTED_TO_BODY", payload=payload
    ):
        warnings.append(trailer_demoted_violation)

    # PIPED-EXIT-CODE-IS-THE-PIPES, chained form. Static, no spawn, and placed
    # here rather than in its own guard because the shape that matters is a git
    # MUTATION whose status is swallowed, which is what this function already
    # exists to see.
    _piped_head = _piped_exit_code_chain(command)
    if _piped_head is not None:
        warnings.append(
            "PIPED-EXIT-CODE: `%s` is piped, so the status reported is the LAST "
            "stage's, not the git command's. A failed commit reads as success -- to "
            "the harness, and to any `&&` after it.\n\n"
            "Drop the pipe, or open with:\n"
            "  set -o pipefail" % _piped_head
        )

    # Single warn-only flush (bash: the "Single warn-only flush" comment near
    # the end of validate-commit.sh). Every warn-only check above (5, 7 soft/
    # override, 8, 9, 10) appends to `warnings`; this is the one sink that
    # surfaces them as an allow+additionalContext advisory instead of a
    # silent-allow `None` (a silent `None` here would have made every
    # warn-only check in this function a no-op -- there is no bash-style
    # stderr channel available to a plain function return).
    if warnings:
        _scope_idx = [i for i, w in enumerate(warnings) if w.startswith("SCOPE: ")]
        if len(_scope_idx) > _BULK_FOREIGN_INDEX_PATHS:
            _drop = set(_scope_idx[_BULK_FOREIGN_INDEX_PATHS:])
            warnings = [w for i, w in enumerate(warnings) if i not in _drop]
            warnings.append(
                "SCOPE: ... and %d more staged paths not in this session's "
                "touch list (not listed)." % len(_drop)
            )
        _warn_text = "\n\n".join(warnings)
        return _vc(
            _warn_text, (), None, "the commit you need", "the flagged commit",
            kind="advisory", em_text=_warn_text,
        )

    return None


#: `git add` options taking a SEPARATE-token value, so `_bt_git_add_own_
#: pathspec` cannot misread an option's value as a path operand. `--chmod`
#: is git's `=`-joined spelling in practice (`--chmod=+x`) but accepts the
#: separated form too; the `=`-joined form is skipped by the generic
#: leading-`-` arm and needs no entry here.
_GIT_ADD_OPT_WITH_ARG = frozenset({"--chmod", "--pathspec-from-file"})


#: A short-flag cluster carrying `o` (`-o`, `-om`, `-so`) -- `git commit -o`
#: bundles like any other short flag (`git commit -om "msg" a.txt` verified
#: live against git 2.50.1: commits only `a.txt`, leaves a peer's staged
#: `b.txt` in the index untouched).
_COMMIT_ONLY_SHORT_RE = re.compile(r"^-[a-zA-Z]*o[a-zA-Z]*$")


#: Pathspec glob metacharacters. A `*`/`?`/`[...]` operand names however many
#: paths happen to match at commit time, so its blast radius is not visible in
#: the command the operator read back to themselves before running it.
_COMMIT_GLOB_META_RE = re.compile(r"[*?\[]")


def _bt_commit_scope_operand_is_sweeping(operand: str, cwd: Optional[str]) -> bool:
    """True iff ONE `git commit` scope operand names a subtree rather than a
    path the operator can be said to have chosen.

    Four sweeping shapes, cheapest test first (this runs on the
    PreToolUse(Bash) hot path -- `os.path.isdir` is one stat, no spawn, and
    the glob/separator/magic arms are pure string work; DR-344):

      1. a `:`-magic pathspec form (`:/`, `:(top)`, `:(glob)`, ...), with
         ONE named carve-out: `:(literal)<path>` is unwrapped to `<path>`
         (via `block_subagent_commit._literal_pathspec_inner_path`) and resolved as that
         ordinary path, glob-metacharacter test skipped -- `:(literal)`
         is git's own promise that `<path>` is never glob-expanded,
         deleted-file-or-not. A bare `:(literal)` with nothing after it
         stays sweeping via this same rule (the helper returns ``None``);
      2. a trailing forward or back slash -- an unambiguous directory
         before touching the filesystem so a deleted-or-not-yet-existing
         directory still reads as a sweep;
      3. a glob metacharacter -- membership resolves at commit time, not at
         read-back time; with its own narrow carve-out (shared with
         `block_subagent_commit._pathspec_element_is_sweeping` via
         `block_subagent_commit._bracket_candidate_exists_literally`): a `[`-bearing but
         `*`/`?`-free operand that exists on disk under `cwd` is a literal
         filename, not a glob character class -- e.g. a Next.js dynamic
         route, `src/app/[id]/page.tsx`;
      4. an operand that resolves to a real directory on disk.

    Resolution is relative to the payload cwd, never `os.getcwd()`: the guard
    runs in whatever directory the harness happens to sit in, and a relative
    operand resolved against the wrong root is a fabricated answer rather than
    a missing one.

    DELIBERATE NARROWING vs the spinoff spec, recorded because it is a
    departure and not an oversight. `state/handoffs/2026-08-03-commit-scope-
    guard-predicates.md` says an operand that cannot be resolved must fail
    toward FIRING. Taken literally that fires on every scoped commit of a
    DELETED path -- `git commit -m x -- gone.py` is the correct, ratified
    spelling for landing a deletion, and its operand is absent from disk by
    construction. An advisory that fires on a correct commit teaches the
    operator to dismiss it, which costs more than the case it catches. So a
    bare unresolvable operand (no trailing separator, no glob) reads as a
    named file and does NOT suppress the narrowing -- while every DIRECTORY
    spelling still sweeps, resolvable or not, which is what the incident
    command actually was. What would change this: an unresolvable bare
    operand shown to be a real sweep vector in a live incident.
    """
    if not operand:
        return True
    literal_inner = literal_pathspec_inner_path(operand)
    if literal_inner is not None:
        operand = literal_inner
    elif operand.startswith(":"):
        return True
    if operand.endswith("/") or operand.endswith("\\"):
        return True
    if literal_inner is None and _COMMIT_GLOB_META_RE.search(operand):
        if not (
            "[" in operand
            and "*" not in operand
            and "?" not in operand
            and bracket_candidate_exists_literally(operand, cwd or "")
        ):
            return True
    base = cwd or ""
    try:
        candidate = operand if os.path.isabs(operand) else os.path.join(base, operand)
        return os.path.isdir(candidate)
    except (OSError, ValueError):
        # A path the OS refuses to even stat is not evidence of a file.
        return False


def _bt_commit_scope_is_sweeping(
    seg_tokens: List[str], cwd: Optional[str]
) -> bool:
    """True iff a `git commit` segment's scope operands include a sweep.

    Reuses `_bt_commit_operand_scan` -- the one parser that already performs
    option-value pair-skipping and already returns operands for BOTH the
    `-- <paths>` and `-o`/`--only` spellings. Re-walking raw tokens here
    would be the fourth tokenizer dialect the spinoff's anti-scope forbids,
    and would reintroduce exactly the two-independent-recognizers defect that
    file is about.

    `-o`/`--only` with no operands this walk can see is a sweep: the flag
    selects git's self-scoped mode while naming nothing, so there is no
    chosen path to suppress an advisory on.
    """
    operands, ambiguous, has_include, has_only = _bt_commit_operand_scan(seg_tokens)
    if has_include or ambiguous:
        # Neither is a suppression case at the call site anyway; say "not
        # sweeping" so this predicate never becomes the reason an ambiguous
        # parse gets treated as a sweep. The caller's own SC-DR-020 bound-4
        # fail-open still governs.
        return False
    if has_only and not operands:
        return True
    return any(_bt_commit_scope_operand_is_sweeping(o, cwd) for o in operands)


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


def _bt_commit_is_noop_invocation(seg_tokens: List[str]) -> bool:
    """True iff a `git commit` segment carries `-h`/`--help`, `--dry-run`, or
    `--version`: git prints usage/output and exits WITHOUT staging or
    committing anything.

    Why a carve-out rather than one more shape the predicates below happen
    to deny: a no-op invocation names no pathspec, so every bare-commit
    predicate reads it as unscoped and the operator gets a deny on a
    `git commit` shape that cannot sweep anything. Noise on a harmless
    command is what trains operators to stop reading guard output --
    reported alongside a real sweep this guard did not stop
    (`state/bug-backlog/2026-08-21-bare-commit-guard-likely-fails-open-
    unde-0d2276775068.yaml`, SECONDARY).

    `--dry-run` and `--version` were named alongside `-h`/`--help` as an
    identical-property gap (`state/bug-backlog/2026-08-31-the-noop-commit-
    carve-out-stops-at-help.yaml`): no staging, no commit, no pathspec.
    `--short`/`--porcelain` are deliberately NOT members -- git still
    commits under either.

    Bounded by the segment's own `--` separator and skipping
    option-with-arg VALUE tokens, on the same footing as
    `_bt_commit_has_sweep_all_flag`: a message operand or a pathspec
    literally named `-h` must never read as a no-op, since the cost of a
    false positive here is a real bare commit going unguarded -- this reads
    argv OPERANDS only, never the commit message. A BUNDLED short cluster
    carrying `h` (`-sh`) is deliberately NOT matched -- git treats it as
    help, but leaving it to today's deny costs one noisy refusal, while
    widening the match risks the silence direction."""
    i = 0
    n = len(seg_tokens)
    while i < n:
        tok = seg_tokens[i]
        if tok == "--":
            break
        if tok in ("-h", "--help", "--dry-run", "--version"):
            return True
        if tok in _GIT_COMMIT_OPT_WITH_ARG:
            i += 2
            continue
        i += 1
    return False


def _bt_commit_sweeping_scope_operands(
    seg_tokens: List[str], cwd: Optional[str]
) -> List[str]:
    """The scope operands of a `git commit` segment that name a SUBTREE
    rather than a chosen path -- the ones `_bt_commit_scope_is_sweeping`
    reduces to a single bool.

    Exists so the deny/advisory text can NAME the operand it is objecting to.
    A message that says "names no scope" at `git commit -m x -- state/` is
    false about that command, and the operator who reads it concludes the
    parser failed rather than that their directory is the problem (two
    independent field reports: example-retrieval-repo-em 2026-09-02, coordinator-content-repo-em
    2026-09-04). `--only` with no visible operand sweeps too and has no
    operand to name, so it yields an empty list and the caller falls back to
    naming the flag.
    """
    operands, ambiguous, has_include, _has_only = _bt_commit_operand_scan(seg_tokens)
    if has_include or ambiguous:
        return []
    return [o for o in operands if _bt_commit_scope_operand_is_sweeping(o, cwd)]


def _sweeping_scope_sentence(operands: List[str]) -> str:
    """The one fact a sweeping-scope deny/advisory states: which operand is a
    directory and what a directory pathspec actually commits.

    `ceremony.commit_v2` already refuses the same input with this sentence
    ("<path> is a directory -- pass explicit file paths; a directory pathspec
    matches whatever lands inside it at commit time, including a peer's file
    added after the caller computed it"). Two components enforcing one rule
    must not disagree about how they say it -- `docs/wiki/guard-messaging.md`
    Register, and the sibling-guard lesson that a green suite cannot see a
    contradiction living in another module's string.

    The empty case is `--only` naming nothing: still a sweep, but with no
    operand to quote, so the flag itself is the subject."""
    if not operands:
        return (
            "'--only' selects git's self-scoped mode but names no path, so "
            "this commit's content is whatever is staged, including a peer's."
        )
    expansions = [o for o in operands if "$" in o]
    if expansions:
        return (
            "%s is a shell expansion — its paths are unknown until the shell "
            "runs, so the scope cannot be read back; name the files inline."
            % ", ".join(expansions)
        )
    if len(operands) == 1:
        return (
            "%s names a directory, not files — a directory pathspec commits "
            "whatever is inside it at commit time, including a file a peer "
            "adds after you read the command back." % operands[0]
        )
    return (
        "%s name directories, not files — a directory pathspec commits "
        "whatever is inside them at commit time, including a file a peer "
        "adds after you read the command back." % ", ".join(operands)
    )


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


def _bt_compound_add_bare_commit(
    seg_tokens: List[str],
    segments: List[Tuple[List[str], bool]],
    seg_index: int,
) -> bool:
    """True iff `seg_tokens` is the bare-commit half of a compound `git add
    ... && git commit` -- i.e. some earlier segment of this SAME command is a
    `git add`, and the commit carries no `-a`/`-am`/`--all`.

    This is the unconditional fire predicate (rewrite or deny) for that shape (PM ruling,
    2026-08-30). It replaces C7's deleted index probe, which gated the same
    deny on a two-`git diff --cached` set-difference proving the index held
    paths outside the command's own `git add`. The ruling drops that
    condition: the deny's ground is no longer "foreign paths were observed"
    but "this commit names no scope, so what it sweeps is decided by whatever
    the shared index happens to hold at run time". Whether a peer had already
    staged into it at the moment the guard looked is a race, not a property
    of the command.

    This reverses C7 PM Ruling 2 (the 2026-08-01 advisory-firing-shape plan),
    which deliberately kept the safe single-session case advisory. The PM was
    told it reverses that carve-out and proceeded.

    NO PROBE. The predicate is pure token inspection, so this deny costs zero
    subprocesses where the condition it replaces cost two `git diff --cached`
    spawns on the commit hot path -- see the brightline in CLAUDE.md.

    SCOPE JUDGMENT (EM, not settled by the ruling's text): an UNSCOPED
    preceding add (`git add -A`, `git add -u`, `--pathspec-from-file`) counts
    here, where the deleted probe short-circuited to False on it. That
    short-circuit existed because an unscoped add gives the set-difference
    nothing to compare against -- a reason that dies with the
    set-difference. Keeping it would leave `git add -A && git commit`
    ADVISORY while `git add one.py && git commit` DENIES, i.e. a guard that
    punishes the careful spelling and waves through the unbounded one. The
    unscoped shape is strictly the more dangerous of the two.

    `-a`/`-am`/`--all` stay excluded (PM Ruling 2, finding 6) -- that shape is
    not the bare-commit half at all and is owned by
    `_bt_sweep_all_holds_unverifiable_paths`.
    """
    if _bt_commit_has_sweep_all_flag(seg_tokens):
        return False
    for prior_tokens, _pipe in segments[:seg_index]:
        if _bt_git_resolved_subcommand(prior_tokens) == "add":
            return True
    return False


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
    rc, out = _dc_git_probe._run_git(["log", "-1", "--format=%h\x02%s\x02%B"], cwd=cwd)
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


def _bt_solo_bare_commit_staged_paths(
    seg_tokens: List[str],
    segments: List[Tuple[List[str], bool]],
    seg_index: int,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[List[str]]:
    """The staged paths (both sides of a rename) iff `seg_tokens` is a solo
    bare `git commit` -- no preceding
    `git add` anywhere earlier in the same command (that shape is
    `_bt_compound_add_bare_commit`'s, and denies unconditionally without
    probing) -- AND the index it would commit is probed. `None` when the
    command is not that shape or the probe failed; `[]` for an empty index.

    Unlike a set-difference against the command's own `add` pathspec, a
    solo bare
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
    bare-commit shape this check exists to deny. A commit that names its
    set (`git -c coordinator.staged=<path> commit`) is verifiable up to the
    guard-to-commit race; one that names nothing stays unverifiable. Fails OPEN on any probe
    failure, same posture as C7's own probe.
    """
    if _bt_commit_has_sweep_all_flag(seg_tokens):
        return None
    for prior_tokens, _pipe in segments[:seg_index]:
        # ANY preceding `git add` segment -- scoped or not (`-A`/`-u`/
        # `--pathspec-from-file` included) -- takes this command out of
        # "solo bare commit" and into the compound bare-commit-half shape,
        # which `_bt_compound_add_bare_commit` denies unconditionally. The
        # two predicates test the same condition with opposite polarity and
        # must stay bit-identical: any `add` at all, never "an add whose
        # pathspec we could extract". Keying on extractability instead
        # wrongly read `git add -A && git commit` as a SOLO bare commit --
        # an unscoped add still means the caller staged something here.
        if _bt_git_resolved_subcommand(prior_tokens) == "add":
            return None  # compound shape -- `_bt_compound_add_bare_commit`'s
    cwd = _bt_probe_cwd(seg_tokens, payload)
    extra_env: Optional[Dict[str, str]] = None
    index_file = _bt_git_index_file_env(seg_tokens)
    if index_file:
        extra_env = {"GIT_INDEX_FILE": index_file}
    rc, out = _dc_git_probe._run_git(["diff", "--cached", "--name-only", "--no-renames"], cwd=cwd, extra_env=extra_env)
    if rc != 0:
        _dc_git_probe._record_probe_fail_open(
            "index probe", "diff --cached --name-only --no-renames", rc
        )
        return None
    return [ln for ln in out.splitlines() if ln]


def _bt_probe_cwd(
    seg_tokens: List[str], payload: Optional[Dict[str, Any]], cmd: Optional[str] = None
) -> Optional[str]:
    """`_bt_probe_cwd` with a `-C ~/repo` / `-C $HOME/repo` value expanded: the
    guard sees the token before any shell does, and joining the raw `~` onto
    the payload cwd names no directory, so the probe spawn fails."""
    dash_c = _bt_git_dash_c_value(seg_tokens)
    if dash_c and (dash_c.startswith("~") or "$" in dash_c):
        expanded = os.path.expandvars(os.path.expanduser(dash_c))
        if os.path.isabs(expanded):
            return expanded
    return _bt_probe_cwd_unexpanded(seg_tokens, payload, cmd)


def _bt_index_only_removals(
    seg_tokens: List[str], payload: Optional[Dict[str, Any]], staged: List[str]
) -> List[str]:
    """Staged deletions whose worktree file still exists (`git rm --cached`).
    A pathspec commit cannot record these -- git reports nothing to commit --
    so the scoped `-- <paths>` form is not a remedy for them. Fails OPEN to []."""
    if not staged:
        return []
    cwd = _bt_probe_cwd(seg_tokens, payload)
    rc, deleted = _dc_git_probe._run_git(
        ["diff", "--cached", "--diff-filter=D", "--name-only", "--no-renames"], cwd=cwd
    )
    if rc != 0 or not deleted.strip():
        return []
    rc, top = _dc_git_probe._run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if rc != 0 or not top.strip():
        return []
    root = top.strip()
    return [
        p for p in deleted.splitlines()
        if p in staged and os.path.exists(os.path.join(root, p))
    ]


def _bt_solo_bare_commit_index_nonempty(
    seg_tokens: List[str],
    segments: List[Tuple[List[str], bool]],
    seg_index: int,
    payload: Optional[Dict[str, Any]] = None,
) -> bool:
    """True iff `_bt_solo_bare_commit_staged_paths` reports a non-empty
    index; fails OPEN (False) on any probe failure."""
    return bool(_bt_solo_bare_commit_staged_paths(seg_tokens, segments, seg_index, payload))


def _bt_sweep_all_holds_unverifiable_paths(
    seg_tokens: List[str], payload: Optional[Dict[str, Any]] = None
) -> bool:
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
    posture as `_bt_solo_bare_commit_index_nonempty` above -- inherited,
    not reinvented.

    The `-a`/`-am`/`--all`/`--amend` short-circuit this predicate INVERTS
    is itself provenanced from `state/lessons/2026-08-03-git-add-mine-
    then-bare-git-commit-sweeps-70d1438f8f01.yaml` -- the fourth recorded
    recurrence of a shared-index sweep silently mis-attributing a peer's
    work, which is what left the two index-based predicates' unconditional
    `-a` exclusion (and therefore this gap) in place."""
    if not _bt_commit_has_sweep_all_flag(seg_tokens):
        return False
    cwd = _bt_probe_cwd(seg_tokens, payload)
    if not _dc_git_shared._is_hazard_repo(cwd or os.getcwd()):
        return False
    extra_env: Optional[Dict[str, str]] = None
    index_file = _bt_git_index_file_env(seg_tokens)
    if index_file:
        extra_env = {"GIT_INDEX_FILE": index_file}
    rc_staged, out_staged = _dc_git_probe._run_git(
        ["diff", "--cached", "--name-only"], cwd=cwd, extra_env=extra_env
    )
    if rc_staged != 0:
        _dc_git_probe._record_probe_fail_open(
            "sweep-all staged probe", "diff --cached --name-only", rc_staged
        )
        return False
    rc_worktree, out_worktree = _dc_git_probe._run_git(
        ["diff", "--name-only"], cwd=cwd, extra_env=extra_env
    )
    if rc_worktree != 0:
        _dc_git_probe._record_probe_fail_open(
            "sweep-all worktree probe", "diff --name-only", rc_worktree
        )
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
    found in the field (coordinator-content-repo-em, 2026-07-29 cross-repo memo, after
    four ignored firings):

    - **It never fires on the ratified, both-halves scoped form.** A
      command already carrying `-- <paths>` on its commit segment (or
      `--pathspec-from-file`/`--pathspec-file-nul`, see
      `_bt_commit_has_explicit_pathspec`) IS the ratified default
      (`git add -- <paths> && git commit -m "x" -- <paths>`, DoE
      `coordinator-content-repo coordinator/docs/wiki/concurrent-em-git-operations/scoped-safety-commits.md` SC-DR-015, which ratifies
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

    Never a rewrite where the caller's paths are not knowable from the
    command text: the message states the shape and lets the operator fill
    it -- synthesizing a scope inside `updatedInput` is exactly the kind of
    clever, unverified substitution `check_offer_git_c`'s own docstring
    warns against. The one shape whose paths ARE in the text -- a compound
    whose own `git add` names literal paths -- is rewritten, below.

    BARE-COMMIT-HALF (PM ruling, 2026-08-30): the shape `git add <paths> &&
    git commit -m "x"` never passes unscoped -- it is rewritten to carry
    `-- <paths>` when the add names literal paths
    (`_bt_scope_compound_commit_to_its_add`), and denied otherwise. See
    `_bt_compound_add_bare_commit`, which decides it from the command tokens
    alone and spawns nothing. The ruling REVERSES C7 PM Ruling 2, which kept
    this shape advisory whenever the session had staged the whole index
    itself; the safe single-session case is no longer carved out, because
    whether a peer had staged into the shared index at the instant the guard
    looked is a race, not a property of the command. The two-`git diff
    --cached` probe that computed that carve-out is deleted, not suspended.

    The remaining index-based reasoning in this function serves DIFFERENT
    shapes -- the solo bare commit
    (`_bt_solo_bare_commit_index_nonempty`) and the `-a`/`--all` sweep
    (`_bt_sweep_all_holds_unverifiable_paths`) -- and still only ESCALATES,
    never suppresses. Both fail OPEN on probe failure: a probe failure never
    denies (a false deny would block real work on a guard-process error),
    unlike a fail-closed hard-deny guard elsewhere in this package where a
    probe failure denies (a false silence there would be the worse outcome
    for that guard's own class).

    NO-OP CARVE-OUT: `git commit -h`/`--help`/`--dry-run`/`--version` exits
    before staging or committing anything and is skipped ahead of every
    predicate -- see `_bt_commit_is_noop_invocation`.

    FAIL-OPEN LEGIBILITY: when an index probe fails, the escalation is
    still declined (posture unchanged), but the fall-through advisory now
    NAMES the probe failure rather than reading identically to a clean-index
    advisory -- `_record_probe_fail_open`, and the bug row its docstring
    cites.

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
    # The fail-open reasons
    # buffer used to be cleared a second time mid-cascade (just before the
    # index-probe predicates), which meant a reader had to reconstruct
    # which of two clearing sites ran, and in what order relative to the
    # probes, to know what a given advisory's note could contain. One set
    # point (here, at cascade entry) and one drain point
    # (`_take_probe_fail_open_reasons`, at the fall-through advisory) now
    # own the whole lifecycle between them.
    _dc_git_probe._clear_probe_fail_open_reasons()
    cmd = _crlf_strip(cmd)
    raw_cmd = cmd
    # Dialect-aware Start-Process expansion (C8,
    # pln-the-destructive-core-learns-the-she): this entry's `matchers`
    # already declares `COMMAND_TOOL_NAMES` but `_bt_tokenize_full_command`
    # below is a Bash-shaped tokenizer with no PowerShell awareness, so a
    # `Start-Process git -ArgumentList 'commit','-m','wip'` invocation
    # tokenizes to a segment headed by `Start-Process`, never `git`, and
    # every scope predicate below silently never runs -- a missed
    # detection, not a spurious advisory, since this check is
    # `fail_closed=False` (advisory only). Same narrow fix as
    # `_check_destructive_git_revert_full`: for a PowerShell payload only,
    # tokenize via `_dialect.tokenize_command` and run the SAME
    # `expand_start_process_invocations` pass, then rejoin the expanded
    # tokens back into text so the existing Bash-shaped pipeline below
    # (unchanged, still exercised byte-for-byte on the BASH leg) sees the
    # target's real argv in command position. A PowerShell parse failure
    # leaves `cmd` untouched, matching this function's long-standing
    # behavior for every dialect before this change.
    _gcsa_dialect = dialect_from_tool_name((payload or {}).get("tool_name"))
    if _gcsa_dialect is Dialect.POWERSHELL:
        _gcsa_ps_tokens = tokenize_command(
            cmd, _gcsa_dialect, guard_name="git-commit-safe-commit-advise"
        )
        if _gcsa_ps_tokens is not None:
            cmd = " ".join(expand_start_process_invocations(_gcsa_ps_tokens))
    # Heredoc BODIES are stripped before segmenting, joining the per-path
    # precedent `resolve_command_positions` and `block_illegal_filename`
    # already set -- NOT a change to the shared tokenizer, which stays
    # `$(...)`-unaware for its other consumers (see the boundary named in
    # `_shell_c_unwrap_payloads`' own comment).
    #
    # Without this, a `"` inside a heredoc body closes the OUTER double quote
    # opened at `-m "$(`, the rest of the body lexes UNQUOTED, and a `;` in the
    # message subject is read as a real segment separator. The command splits:
    # `git commit` heads one segment and its `-- <paths>` lands in the next, so
    # `_bt_commit_has_explicit_pathspec` correctly answers "no scope" about a
    # segment that genuinely has none -- and this check DENIES the ratified
    # both-halves scoped form, violating the first property named in its own
    # docstring above. Bug row: `state/bug-backlog/2026-08-27-commit-scope-
    # guard-denies-a-correct-pathspec-when-the-message-quotes-a-phrase-
    # 8f31c0a4e7b2.yaml`; both directions pinned in `bash_guards/tests/
    # test_commit_scope_survives_a_quoted_phrase_in_the_message.py`.
    #
    # No bypass is created HERE, and the reason is specific to this check
    # rather than general: the question asked is "does this command line carry
    # a pathspec", and a `git commit` appearing inside a heredoc BODY is stdin
    # data that this command never executes. That reasoning does NOT transfer
    # to a guard which must see commands inside a body (`bash <<'EOF'`), which
    # is exactly why the strip is applied at this seam and not in
    # `tokenize_full_command` itself.
    tokens = _bt_tokenize_full_command(_bt_strip_heredocs(cmd))
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
        # `continue`, not `return None`: a later segment of the same command
        # (`git commit -h && git commit -m x`) still gets every predicate.
        if _bt_commit_is_noop_invocation(seg_tokens):
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
            "COORDINATOR_ALLOW_GIT_COMMIT_AMEND", payload=payload
        ):
            provenance = _bt_head_commit_amend_provenance(
                _bt_probe_cwd(seg_tokens, payload, cmd), session_id
            )
            owned = provenance is not None and provenance[2]
            if not owned:
                _amend_note = operator_override_note(
                    "COORDINATOR_ALLOW_GIT_COMMIT_AMEND", payload=payload, git_root=git_root
                )
                if provenance is not None:
                    head_sha, head_subject, _owned = provenance
                    head_subject = _truncate_to_budget(head_subject, 40)
                    _amend_what = "HEAD (%s %s), not provably yours" % (head_sha, head_subject)
                    _amend_fix = _ready('git notes add -f -m "%s" %s' % (_NOTE_CORRECTION, head_sha))
                    _amend_em = (
                        "Deny: '--amend' rewrites HEAD (%s %s), not "
                        "provably yours -- replaces its message/"
                        "authorship; without --only, also commits "
                        "staged.\n\n"
                        "Use instead:\n"
                        '  git notes add -f -m "%s" %s'
                        % (head_sha, head_subject, _NOTE_CORRECTION, head_sha)
                    )
                else:
                    _amend_what = "HEAD, not provably yours (its provenance could not be verified)"
                    _amend_fix = _offer('git notes add -f -m "%s" <sha>' % _NOTE_CORRECTION, "the sha")
                    _amend_em = (
                        "Deny: '--amend' rewrites HEAD, not provably yours "
                        "(its provenance could not be verified) -- "
                        "replaces its message/authorship; without --only, "
                        "also commits staged.\n\n"
                        "Use instead:\n"
                        '  git notes add -f -m "' + _NOTE_CORRECTION + '" <sha>'
                    )
                return _vc(
                    "Deny: '--amend' rewrites %s -- replaces its message/authorship; "
                    "without --only, also commits staged." % _amend_what,
                    (_amend_fix,), "COORDINATOR_ALLOW_GIT_COMMIT_AMEND",
                    "the amend you need", "rewriting a commit that is not provably yours",
                    guard="git-commit-safe-commit-advise",
                    em_text=_amend_em + ("\n\n%s" % _amend_note if _amend_note else ""),
                )
        if _override("COORDINATOR_ALLOW_GIT_COMMIT_BARE", payload=payload):
            return None
        # Populated only on the fall-through below, where the command DID
        # name a scope and that scope is a subtree. Every message site past
        # this point must say so instead of reading from the no-scope-at-all
        # vocabulary -- see `_sweep_scope_body`.
        _scope_swept = False
        _sweeping_operands: List[str] = []
        if _bt_commit_has_explicit_pathspec(seg_tokens):
            # A scope FORM is not a scope BOUND. `-o state/` and
            # `-- state/` both name git's self-scoped mode over a whole
            # SUBTREE, and until this narrowing both suppressed the
            # advisory outright -- the 2026-08-03 incident committed 71
            # files across 10 sessions and produced no guard output at
            # all. Recognition is unchanged (`-o` really is scope, and
            # reverting that is the spinoff's own anti-scope); only
            # SUPPRESSION narrows, and only for operands that name a
            # subtree rather than a path. Spinoff:
            # `state/handoffs/2026-08-03-commit-scope-guard-predicates.md`.
            if not _bt_commit_scope_is_sweeping(
                seg_tokens, (payload or {}).get("cwd")
            ):
                return None
            _scope_swept = True
            _sweeping_operands = _bt_commit_sweeping_scope_operands(
                seg_tokens, (payload or {}).get("cwd")
            )
            # The sweep advisory carries its own key rather than reusing
            # `COORDINATOR_ALLOW_GIT_COMMIT_BARE` below. That key means "I
            # meant to commit the whole index"; this shape is the opposite
            # claim -- "I named a subtree and I meant that subtree" -- and an
            # operator silencing one has not decided the other. Naming follows
            # the family already established in this file
            # (`..._BARE`, `..._AMEND`), per coordinator-content-repo-aa 2026-09-01.
            # ALLOW-vs-OVERRIDE does not track advisory-vs-deny here
            # (`ALLOW_GIT_COMMIT_BARE` gates a deny), so this key survives
            # unrenamed if the advisory is ever escalated.
            if _override(
                "COORDINATOR_ALLOW_GIT_COMMIT_SCOPE_SWEEP", payload=payload
            ):
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
        # `subject_operand` is interpolated into the `git add ... && git
        # commit` remediation below.
        subject_operand = shlex.quote(subject) if subject else '"<subject>"'
        # `--amend` reaching here carries no NARROW pathspec -- that shape
        # exits earlier via `_bt_commit_has_explicit_pathspec` -- so it is
        # either the bare-`--amend`-with-index shape or an amend whose scope
        # is a whole subtree, and both overwrite HEAD wholesale. The
        # remediation below must say the amend-specific thing rather than
        # the scoped-new-commit shape, which does not amend anything (DoE
        # cross-repo memo, example-retrieval-repo-em, 2026-08-15, "amend has no safe
        # helper and the scope advisory reads generic", Findings 2-3). This
        # branches on TEXT ONLY -- the deny/advisory/allow decision above is
        # unchanged.
        is_amend = _bt_commit_has_amend_flag(seg_tokens)
        # DENY the compound bare-commit-half unconditionally (PM ruling,
        # 2026-08-30) -- see `_bt_compound_add_bare_commit`. `-a`/`-am`/
        # `--all` are not this shape and keep their own predicate below.
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
            "  git notes add -f -m \"" + _NOTE_CORRECTION + "\" <sha>",
            "'git commit --amend' rewrites whatever commit is at HEAD — on "
            "a shared branch that may be a peer's, landed since your last "
            "commit. It replaces that commit's message and authorship, and "
            "(without --only) also commits everything currently staged.",
            (_offer('git notes add -f -m "%s" <sha> (fixes a message without rewriting)' % _NOTE_CORRECTION, "the sha"),),
        )
        # Sweeping-scope remediation. A command carrying `-- state/` NAMED a
        # scope, so every no-scope-at-all body below is false about it, and
        # each of those bodies offers `git add -- <paths> && git commit -m x
        # -- <paths>` -- character-for-character the command it just refused.
        # An operator reading "names no scope" at a command that names one
        # concludes the parser failed and starts permuting spellings, which
        # is what both field reports describe (example-retrieval-repo-em 2026-09-02,
        # coordinator-content-repo-em 2026-09-04; the second reproduced it by calling this
        # function directly, no dispatcher and no `cd` prefix, so the
        # offer-git-c short-circuit is not the cause and reordering the chain
        # is not the fix). TEXT ONLY -- which branch fires, and therefore
        # deny-vs-advisory, is unchanged: escalating the sweeping form is
        # direction-class and sits with DoE's strict-mode question (negative
        # spec, `tests/test_commit_scope_directory_operand_still_advises.py`).
        # `ceremony.commit_v2` already refuses the same input with this
        # sentence; the two components now say the same thing.
        _sweep_scope_body = (
            "%s\n\nUse instead:\n  git commit -m %s -- <file> <file>" % (
                _sweeping_scope_sentence(_sweeping_operands),
                subject_operand,
            ),
            _sweeping_scope_sentence(_sweeping_operands),
            (_offer("git commit -m %s -- <file> <file>" % subject_operand, "the files"),),
        )
        #: The body every branch below uses INSTEAD of its own, when the
        #: command's real defect is not the one that branch is written about.
        #: `--amend` outranks a sweeping scope: a scoped amend still
        #: overwrites a commit that may be a peer's, which no pathspec
        #: wording addresses. `None` means the branch keeps its own text.
        _body_override: Optional[tuple] = (
            _amend_body if is_amend else (_sweep_scope_body if _scope_swept else None)
        )

        def _gc(kind: str, prefix: str, default: tuple, tail: str = "") -> Any:
            legacy, summary, members = _body_override or default
            return _vc(
                prefix + summary, members, "COORDINATOR_ALLOW_GIT_COMMIT_BARE",
                "the commit you need", "committing without a provable scope",
                kind=kind, guard="git-commit-safe-commit-advise",
                em_text=prefix + legacy + tail
                + ("\n\n%s" % _commit_bare_note if _commit_bare_note else ""),
            )
        # A sequencer operation (merge/cherry-pick/revert) is the one state in
        # which every deny below offers a remediation git itself rejects -- see
        # `_bt_git_sequencer_in_progress` for why the "prove it is yours"
        # question does not apply to an index git wrote and forbids narrowing.
        # Downgraded to an advisory naming the verb that actually finishes the
        # operation, rather than dropped: a peer can still `git add` into the
        # shared index mid-merge, so the scope warning is not vacuous, it just
        # has no scoped alternative to point at.
        _sequencer = _bt_git_sequencer_in_progress(seg_tokens, payload)
        if _sequencer is not None:
            _state_name, _continue_verb = _sequencer
            return _vc(
                "Advisory: %s exists — this commit concludes an in-progress "
                "operation, so its content is the whole index by "
                "construction and git refuses to narrow it. A peer's "
                "concurrent 'git add' into this shared index would still be "
                "swept in." % _state_name,
                (_ready(_continue_verb),), "COORDINATOR_ALLOW_GIT_COMMIT_BARE",
                "the commit you need", "committing without a provable scope",
                kind="advisory", guard="git-commit-safe-commit-advise",
                em_text=(
                    "Advisory: %s exists — this commit concludes an in-progress "
                    "operation, so its content is the whole index by "
                    "construction and git refuses to narrow it. A peer's "
                    "concurrent 'git add' into this shared index would still be "
                    "swept in.\n\n"
                    "Use instead:\n"
                    "  %s" % (_state_name, _continue_verb)
                )
                + ("\n\n%s" % _commit_bare_note if _commit_bare_note else ""),
            )
        if _bt_compound_add_bare_commit(seg_tokens, segments, seg_index):
            from coordinator_core.bash_guards.commit_scope_rewrite import (
                _bt_scope_compound_commit_to_its_add,
            )

            _rewritten = (
                None
                if _body_override or _gcsa_dialect is Dialect.POWERSHELL
                else _bt_scope_compound_commit_to_its_add(
                    raw_cmd, seg_tokens, segments, seg_index
                )
            )
            if _rewritten is not None:
                return _alt_record(
                    "rewrite", "git-commit-safe-commit-advise",
                    "Rewritten: this 'git commit' named no scope and would "
                    "have committed the whole shared index. It now commits "
                    "only what its own 'git add' named. "
                    "Next time put the paths after 'git commit -m … --'.",
                    (_ready(_rewritten),), report="the commit you need",
                    authorize="the original commit", updated_input={"command": _rewritten},
                    em_text=(
                        "Rewritten: this 'git commit' named no scope and would "
                        "have committed the whole shared index. It now commits "
                        "only what its own 'git add' named:\n  %s\n\n"
                        "Next time put the paths after 'git commit -m … --'."
                        % (_rewritten,)
                    ),
                )
            return _gc("deny", "Deny: ", (
                "this 'git commit' names no scope — it commits the "
                "whole shared index, not what its own 'git add' named. A "
                "peer staging concurrently is swept under your subject.\n\n"
                "Use instead:\n"
                "  git add -- <paths> && git commit -m %s -- <paths>"
                % (subject_operand,),
                "this 'git commit' names no scope — it commits the "
                "whole shared index, not what its own 'git add' named. A "
                "peer staging concurrently is swept under your subject.",
                (_offer("git add -- <paths> && git commit -m %s -- <paths>" % subject_operand, "the paths"),),
            ))
        _solo_staged = _bt_solo_bare_commit_staged_paths(
            seg_tokens, segments, seg_index, payload
        )
        if _solo_staged:
            _carrier = named_stage_paths(seg_tokens)
            if _carrier is not None and not is_amend and not _scope_swept:
                _refusal = named_set_refusal(_carrier)
                if _refusal is not None:
                    return _gc("deny", "Deny: ", ("%s." % _refusal, "%s." % _refusal, ()))
                _unnamed = unnamed_staged_paths(_solo_staged, _carrier)
                if not _unnamed:
                    return None
                return _gc("deny", "Deny: ", (
                    "staged but not named: %s — this commit would sweep "
                    "them under your subject.\n\n"
                    "Use instead:\n"
                    "  add a -c coordinator.staged operand for your own path, "
                    "or unstage the foreign one"
                    % ", ".join(_unnamed),
                    "staged but not named: %s — this commit would sweep "
                    "them under your subject." % ", ".join(_unnamed),
                    (_offer("add a -c coordinator.staged operand for your own path, "
                            "or unstage the foreign one", "your path"),),
                ))
            if _bt_index_only_removals(seg_tokens, payload, _solo_staged):
                return _gc("deny", "Deny: ", (
                    "this 'git commit' names no scope and the index holds a "
                    "staged 'git rm --cached' removal, which a pathspec "
                    "commit cannot record.\n\n"
                    "Use instead:\n"
                    "  git -c coordinator.staged=<path> commit -m %s\n"
                    "or ceremony.commit_v2 `prefer_staged`." % (subject_operand,),
                    "this 'git commit' names no scope and the index holds a "
                    "staged 'git rm --cached' removal, which a pathspec "
                    "commit cannot record.",
                    (_offer("git -c coordinator.staged=<path> commit -m %s (or ceremony.commit_v2 "
                            "`prefer_staged`)" % subject_operand, "the path"),),
                ))
            return _gc("deny", "Deny: ", (
                "this 'git commit' names no scope and stages "
                "nothing itself — the shared index already holds staged "
                "content this command cannot prove is its own, on a "
                "shared branch a peer's.\n\n"
                "Use instead:\n"
                "  git commit -m %s -- <paths>\n"
                "A staged partial hunk commits as staged via:\n"
                "  git -c coordinator.staged=<path> commit -m %s\n"
                "or ceremony.commit_v2 `prefer_staged`."
                % (subject_operand, subject_operand),
                "this 'git commit' names no scope and stages "
                "nothing itself — the shared index already holds staged "
                "content this command cannot prove is its own, on a "
                "shared branch a peer's.",
                (
                    _offer("git commit -m %s -- <paths>" % subject_operand, "the paths"),
                    _offer("git -c coordinator.staged=<path> commit -m %s (a staged partial hunk commits "
                           "as staged; or ceremony.commit_v2 `prefer_staged`)" % subject_operand, "the path"),
                ),
            ))
        if _bt_sweep_all_holds_unverifiable_paths(seg_tokens, payload):
            return _gc("deny", "Deny: ", (
                "this 'git commit' sweeps every modified tracked "
                "file from the worktree ('-a'/'--all'), not just the "
                "index — a peer's un-staged edits would be swept under "
                "your subject too.\n\n"
                "Use instead:\n"
                "  ceremony.commit_v2 with paths=[<your files>] and the subject %s"
                % (subject_operand,),
                "this 'git commit' sweeps every modified tracked "
                "file from the worktree ('-a'/'--all'), not just the "
                "index — a peer's un-staged edits would be swept under "
                "your subject too.",
                (_offer("ceremony.commit_v2 with paths=[<your files>] and the subject %s" % subject_operand, "the paths"),),
            ))
        # Fail-open legibility (`_record_probe_fail_open`): reaching here
        # after a failed index probe is NOT the same verdict as reaching here
        # with a clean index, and until now both printed the same advisory.
        # The escalation is still declined -- posture unchanged -- but the
        # operator is told the staged set was never read, so a silent
        # degradation under load stops being indistinguishable from safety.
        _fail_open_reasons = _dc_git_probe._take_probe_fail_open_reasons()
        _dc_git_probe._persist_probe_fail_open(_fail_open_reasons, cmd, session_id, git_root)
        _fail_open_note = (
            "The index was NOT read (%s) -- not because it checked and "
            "found nothing." % "; ".join(_fail_open_reasons)
            if _fail_open_reasons
            else ""
        )
        return _gc("advisory", "Advisory: ", (
            "this 'git commit' names no scope.\n\n"
            "Use instead:\n"
            "  git add -- <paths> && git commit -m %s -- <paths>"
            % (subject_operand,),
            "this 'git commit' names no scope.",
            (_offer("git add -- <paths> && git commit -m %s -- <paths>" % subject_operand, "the paths"),),
        ), "\n\n%s" % _fail_open_note if _fail_open_note else "")
    return None
