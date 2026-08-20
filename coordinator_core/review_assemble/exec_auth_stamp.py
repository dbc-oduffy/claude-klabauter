"""
coordinator_core.review_assemble.exec_auth_stamp — mutating assembler for the
`/review` skill's execution-authorization stamp.

Purpose: `/review`'s "Cross-reference exit" gate (DoE-claude
`coordinator/skills/review/SKILL.md` L197-200) narrates a three-step ORDINAL
sequence in prose -- land every plan-body edit the approval entails FIRST,
THEN compute the plan-body hash, THEN write the four
`execution_authorized_{by,at,sha,note}` frontmatter fields -- as an inline
step for the EM to carry out by hand. That is a SKILL-NARRATES-PROCEDURE
shape (DR-090): the step is pure sequencing ("then... then..."), not EM
judgment. This module collapses it to ONE named op --
`stamp_execution_authorization(plan_path, by, note, repo_root=None)` -- that
computes the plan-body hash and writes all four fields atomically. The
caller (the skill / the EM) supplies only `by` and `note` *after* every
plan-body edit the approval entails has already landed on disk; the hash
computation and the frontmatter write are both owned by this op, not
narrated by the skill prose.

`append_note` (CLI: `--append-note`, mutually exclusive with `--note`'s
default replace behaviour) supports the `/execute-plan` stale-bookkeeping
re-stamp path, which explicitly instructs the EM to APPEND the re-stamp
reason to `execution_authorized_note` rather than overwrite the PM's
verbatim utterance. Under `--append-note`, `note` is appended text, not the
new whole value: it lands after whatever is already in the field, separated
by a literal two-character `\n` marker -- NOT a real line break. A genuine
embedded newline would break every single-line-per-field frontmatter
primitive in this tree (`replace_fm_field`/`insert_fm_field` match one
physical line; see `serialize_yaml_scalar`'s own "does not handle
multi-line values" negative-spec) and reintroduce exactly the class of
silent corruption this module exists to prevent -- so the literal `\n`
marker is the append separator, not an actual newline byte. Appending onto
an absent or empty note behaves like a plain set (no leading separator).
Re-running an identical `--append-note` call is a no-op: the convergence
check for the note field asks whether the existing note already ENDS WITH
the given text, not whether it equals it, so a repeat append cannot grow
the field without bound.

Canonical hashing recipe (byte-identical to
`docs/wiki/plan-execute-session-split.md` § Pinned conventions and to
`coordinator_core.pickup_assemble.compute_execution_stamp_match`'s own
recipe): the plan BODY is everything below the second `---` frontmatter
delimiter line, hashed via the literal `git hash-object --stdin` blob-hash
algorithm -- byte-identical to what a reader re-deriving the hash with the
documented `awk ... | git hash-object --stdin` one-liner gets. Frontmatter
fields (including this op's own writes) never enter the hash -- only a
material change to the plan BODY invalidates a previously-computed stamp.

Shares an "exec-auth-stamp" contract family with
`coordinator_core.pickup_assemble.stamp_check` (the READ-side verb,
`coordinator_core/pickup_assemble/stamp_check.py`, built concurrently in a
sibling chunk of the same baton) -- both compute the same canonical
plan-body hash. This module and `pickup_assemble.compute_execution_stamp_match`
both route through the ONE shared
`coordinator_core.frontmatter.primitives.canonical_body_sha` recipe (Review:
code-reviewer -- Finding 3, extracted from two independently
hand-maintained copies given the hash is authorization-staleness-detection
load-bearing; a one-sided drift between them would silently break that
detection) -- this module no longer hand-rolls its own hashing.

Negative-spec:
  - Does NOT enforce the write-bar (has the PM actually named execution?).
    That judgment call stays the calling skill's, per
    `docs/wiki/plan-execute-session-split.md` § Write-bar -- this op is a
    pure mutation once the skill has already decided to write.
  - Does NOT git-commit. Frontmatter mutation only, via `locked_rmw`.
  - Does NOT touch any file other than the single target plan.
  - Does NOT re-derive a body-hash algorithm independently of the shared
    `coordinator_core.frontmatter.primitives.canonical_body_sha` recipe --
    both this module and `pickup_assemble` route through that one
    extracted function, never a local copy.
  - Does NOT emit a `coordinator_core.contract.decision_object` envelope --
    this op is a pure RMW mutation with no `judgment_points`/`directives`
    of its own (it IS the mutating primitive a directive would name, not a
    judgment-point producer). The canonical-constructor import path was
    verified importable as a dispatch-time sanity check (per this chunk's
    spec note) but is not consumed by this module's body.

Spec backlink: DoE-claude:pln-computed-skills-b8-review-ci-c-ffa5ad, chunk C6
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.primitives import (
    BlockScalar,
    append_fm_block_scalar_line,
    canonical_body_sha,
    insert_fm_field,
    read_fm_block_scalar,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.pickup_assemble import resolve_repo_root

#: The four-field execution-authorization stamp, byte-identical to the
#: pinned convention (plan-execute-session-split.md § Pinned conventions).
EXEC_FIELDS: tuple[str, ...] = (
    "execution_authorized_by",
    "execution_authorized_at",
    "execution_authorized_sha",
    "execution_authorized_note",
)

EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2

#: Literal (non-newline) marker separating an appended reason from whatever
#: `execution_authorized_note` already held -- see `_append_note`.
NOTE_APPEND_SEPARATOR = "\\n"


def _append_note(current: Optional[str], addition: str) -> str:
    """Compute the new `execution_authorized_note` value for `--append-note`
    mode: *addition* landed after *current*, separated by
    `NOTE_APPEND_SEPARATOR`.

    An absent or empty *current* makes this a plain set (no leading
    separator) -- there is nothing to append after. Idempotent: if *current*
    already ends with *addition*, it is returned unchanged (a suffix match
    subsumes the separator-prefixed case, since anything ending in
    `NOTE_APPEND_SEPARATOR + addition` also ends in *addition*), so calling
    this twice with the same *addition* converges rather than growing the
    note without bound.

    Does NOT reflow, re-indent, or otherwise alter *current*'s own shape --
    it is preserved verbatim as the prefix.
    """
    if not current:
        return addition
    if current.endswith(addition):
        return current
    return current + NOTE_APPEND_SEPARATOR + addition


def _current_note_text(fm: str, block: Optional[BlockScalar]) -> Optional[str]:
    """The `execution_authorized_note` text a convergence test should compare
    against, for either value shape.

    A single-line note reads back through `read_fm_field_unquoted`. A block
    scalar does NOT: that reader returns only the `key:` line, so on a `|` or
    `>` note it yields the bare header sigil. Comparing an appended note
    against `"|"` never converges, so the caller re-stamped on every
    invocation -- the non-convergence half of the same defect that made the
    write itself refuse (cross-repo memo, example-retrieval-repo-em, 2026-08-20).

    The block's authored lines are joined with the same
    `NOTE_APPEND_SEPARATOR` the single-line path writes, so one `endswith`
    test serves both shapes.
    """
    if block is None:
        return read_fm_field_unquoted(fm, "execution_authorized_note")
    return NOTE_APPEND_SEPARATOR.join(block.lines)


def _canonical_body_sha(text: str, repo_root: Path) -> str:
    """Hash the plan BODY (everything below the second `---` frontmatter
    delimiter) via the shared `coordinator_core.frontmatter.primitives.
    canonical_body_sha` recipe -- byte-identical to a real
    `git hash-object --stdin` call over the same body (Review: code-reviewer
    -- Finding 3; no longer a local hand-rolled subprocess call). `repo_root`
    is retained for call-site-signature parity only -- the shared recipe
    needs no repo state, only the already-read text."""
    sha = canonical_body_sha(text)
    if sha is None:
        raise RuntimeError("canonical_body_sha: could not UTF-8-encode plan body text")
    return sha


def stamp_execution_authorization(
    plan_path: str,
    by: str,
    note: str,
    *,
    at: Optional[str] = None,
    repo_root: Optional[Path] = None,
    append_note: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Compute the plan-body hash and write all four
    `execution_authorized_*` fields onto *plan_path*'s own frontmatter,
    atomically, under `locked_rmw`.

    `at` defaults to today's UTC date (`YYYY-MM-DD`) when omitted -- pass it
    explicitly only for reproducible tests.

    `append_note=True` treats *note* as text to append to whatever
    `execution_authorized_note` already holds (see `_append_note`) instead
    of replacing it outright -- for the `/execute-plan` stale-bookkeeping
    re-stamp path, which must not clobber the PM's verbatim utterance.

    Returns `(exit_code, result_dict)`:
      - `EXIT_OK` with `{"applied": bool, "sha": str, "message": str}` on
        success. Idempotent: with `append_note=False`, re-stamping with the
        identical by/at/sha/note already present is a no-op (`applied=False`);
        with `append_note=True`, re-appending the identical text is a no-op
        once the existing note already ends with it -- see `_append_note`.
      - `EXIT_BUSINESS_FAIL` with `{"error": str}` when the repo root can't
        be resolved, the plan is unreadable, or has no parseable
        frontmatter.
    """
    root = repo_root or resolve_repo_root()
    if root is None:
        return EXIT_BUSINESS_FAIL, {"error": "could not resolve a git worktree root"}

    live_path = Path(plan_path) if Path(plan_path).is_absolute() else root / plan_path
    if not live_path.is_file():
        return EXIT_BUSINESS_FAIL, {"error": f"{plan_path}: not found"}

    try:
        text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return EXIT_BUSINESS_FAIL, {"error": f"{plan_path}: could not read ({exc})"}

    if split_frontmatter(text) is None:
        return EXIT_BUSINESS_FAIL, {"error": f"{plan_path}: no parseable frontmatter"}

    try:
        sha = _canonical_body_sha(text, root)
    except RuntimeError as exc:
        return EXIT_BUSINESS_FAIL, {"error": str(exc)}

    at_value = at or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    intended_exact = {
        "execution_authorized_by": by,
        "execution_authorized_at": at_value,
        "execution_authorized_sha": sha,
    }

    state: dict[str, Any] = {"applied": False}

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"{plan_path}: no parseable frontmatter (race)")

        fm = split.fm_text
        note_block = read_fm_block_scalar(fm, "execution_authorized_note")
        current_note = _current_note_text(fm, note_block)

        # The note field's convergence test differs from the other three:
        # under append_note it asks "does the existing note already end with
        # this text?" (see _append_note), never exact equality -- an exact-
        # equality test would make every repeat append non-convergent and
        # grow the field without bound.
        already_converged = all(
            read_fm_field_unquoted(fm, field) == value
            for field, value in intended_exact.items()
        )
        if already_converged:
            if append_note:
                already_converged = (current_note or "").endswith(note)
            else:
                already_converged = current_note == note
        if already_converged:
            state["applied"] = False
            return old_text

        # A block-scalar note is appended INTO its block and then excluded
        # from the single-line writer below. `replace_fm_field` refuses that
        # shape by design -- correctly, since a single-line rewrite would
        # truncate the PM's verbatim multi-line words -- and routing an
        # append through it made `authorize-invocation` (i.e. /execute-plan
        # Phase 1 step 2) unrunnable on every plan whose note is a `|` or `>`
        # block, with no flag to get past it (cross-repo memo, example-retrieval-repo-em,
        # 2026-08-20). Appending is a different operation, so it takes the
        # different primitive rather than softening the guard.
        fields = list(EXEC_FIELDS)
        if note_block is not None and not append_note:
            # The replace-outright verb (`stamp --note`) over a block-scalar
            # note. Deliberately still refused -- that field holds the PM's
            # verbatim authorizing words, and collapsing a multi-line quote
            # to one line destroys evidence with no reconstruction. What is
            # NOT deliberate is surfacing it as a raw ValueError traceback
            # from a frontmatter primitive: this is the layer that knows the
            # verb the operator typed, so it owns the message.
            raise MutateAbort(
                f"{plan_path}: execution_authorized_note is a block scalar "
                f"holding multi-line verbatim text; --note would replace it "
                f"with a single line. Use `authorize-invocation` (or `stamp "
                f"--append-note`) to add a line instead."
            )
        if append_note and note_block is not None:
            fm = append_fm_block_scalar_line(fm, "execution_authorized_note", note)
            fields.remove("execution_authorized_note")
            final_note = note
        else:
            final_note = _append_note(current_note, note) if append_note else note
        final_values = {**intended_exact, "execution_authorized_note": final_note}

        # Append-only insert (no `after_key`): a first-time stamp has no
        # prior execution_authorized_* fields to anchor after, so each
        # field lands at the end of the frontmatter block, in EXEC_FIELDS
        # order.
        for field in fields:
            value = final_values[field]
            numeric_quoting = field == "execution_authorized_sha"
            if read_fm_field_unquoted(fm, field) is not None:
                fm = replace_fm_field(fm, field, value, numeric_quoting=numeric_quoting)
            else:
                fm = insert_fm_field(fm, field, value, numeric_quoting=numeric_quoting)

        state["applied"] = True
        return rebuild(split, fm)

    try:
        locked_rmw(live_path, _mutate, repo_root=root)
    except LockTimeout as exc:
        return EXIT_BUSINESS_FAIL, {"error": f"lock timeout acquiring file lock: {exc}"}
    except MutateAbort as exc:
        return EXIT_BUSINESS_FAIL, {"error": str(exc.args[0]) if exc.args else "mutate aborted"}
    except OSError as exc:
        return EXIT_BUSINESS_FAIL, {"error": f"cannot read/write plan file: {exc}"}

    message = (
        f"stamped execution_authorized_* onto {plan_path} (sha={sha})"
        if state["applied"]
        else f"{plan_path} already stamped with the identical execution_authorized_* fields -- no-op"
    )
    return EXIT_OK, {"applied": state["applied"], "sha": sha, "message": message}


#: Shape a PM's typed command must have for the invocation mint to fire: a
#: literal slash command. The mint records a PM ACT, so its trigger has to be
#: a thing the PM literally typed -- never an EM inference, a peer's message,
#: or an intent-shaped remark (see this spinoff's anti-scope).
INVOCATION_COMMAND_PREFIX = "/"


def _compose_invocation_note(utterance: Optional[str], typed_command: str) -> str:
    """The `execution_authorized_note` value for an invocation-authorized
    mint: the command the PM typed, plus their verbatim words when there
    were any.

    An utterance, when present, is embedded unaltered (no reflow, no
    truncation, no paraphrase) -- a paraphrase here is the same class of
    failure as a stealth-skip, since the field would then record the EM's
    reading of consent rather than the consent itself.

    But the utterance is EVIDENCE, not the authorization. The typed command
    is the authorization (PM ruling, verbatim, 2026-08-19: *"my using
    `/execute-plan` should be registered as execution authorization. It's
    literally the command to do so."*), so a bare invocation with no
    accompanying words mints just as well and records exactly that. Demanding
    prose before the engine will honour a command the PM literally typed
    turns a consent primitive into a password prompt.
    """
    if utterance and utterance.strip():
        return f'PM verbatim: "{utterance}" (authorized by typed command: {typed_command})'
    return f"PM authorized by typed command: {typed_command} (no accompanying words)"


def stamp_invocation_authorization(
    plan_path: str,
    utterance: Optional[str],
    typed_command: str,
    *,
    at: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> tuple[int, dict[str, Any]]:
    """Mint execution authorization from a PM's LITERAL invocation of the
    command that means "execute this" -- so `/execute-plan` on a
    PM-approved plan is itself the authorization, instead of demanding a
    stamp only `/review`'s cross-reference exit could ever have written.

    Not a second stamping surface: this composes a note and delegates the
    whole four-field write to `stamp_execution_authorization`, which stays
    the sole mint. `by` is always `PM` -- the invocation IS the PM act, and
    a caller-chosen `by` would let a non-PM route wear the PM's attribution.

    Convergence (stricter than the plain `--append-note` path). Re-invoking
    on an already-authorized plan whose BODY is unchanged is a no-op even
    across a date boundary: the delegate's own convergence test compares
    `execution_authorized_at` exactly, so a bare re-delegate would re-stamp
    tomorrow purely because the default `at` moved. This function therefore
    preserves the recorded `at` whenever the stamped sha still matches the
    live body, and only takes a fresh timestamp when the sha differs (a
    genuinely new authorization against changed content). The note is
    appended, never replaced -- an earlier `/review` stamp's verbatim
    utterance is evidence too.

    `utterance` is OPTIONAL and an absent one is not a refusal: the typed
    command is the authorization, the words are evidence (see
    `_compose_invocation_note`). The only trigger check that survives is
    that `typed_command` really is a literal slash command -- that is what
    keeps the mint recording a PM ACT rather than an EM inference, and it
    does not need prose to do it.

    Refuses (EXIT_USAGE) an utterance carrying a real newline (a single
    embedded line break breaks every single-line-per-field frontmatter
    primitive in this tree -- see the module docstring's
    `NOTE_APPEND_SEPARATOR` note) and a `typed_command` that is not a
    literal slash command.

    Spine-population decision (RECORDED, per this baton's spec): this mint
    does NOT inspect the plan's `writes:` / `depends_on:` spine and does NOT
    refuse an unpopulated one. Authorization and executability are different
    questions; folding the second into the first would put a scheduling
    concern inside a consent primitive, and executability already has two
    owners -- `/execute-plan` Phase 1.4 and `dispatch.emit`'s
    `NoWritesDeclaredError`. A plan can be authorized and not yet runnable;
    those are separate refusals with separate remedies.

    Returns `(exit_code, result_dict)` in `stamp_execution_authorization`'s
    own shape, plus `note` (the composed value) on the OK arms.
    """
    if utterance and any(ch in utterance for ch in (chr(10), chr(13))):
        return EXIT_USAGE, {
            "error": (
                "refusing to mint: the utterance contains a real line break, which no "
                "single-line frontmatter field can hold -- pass it as one line"
            )
        }
    if not typed_command.startswith(INVOCATION_COMMAND_PREFIX):
        return EXIT_USAGE, {
            "error": (
                f"refusing to mint: --typed-command must be the literal slash command the PM "
                f"typed (got {typed_command!r})"
            )
        }

    root = repo_root or resolve_repo_root()
    if root is None:
        return EXIT_BUSINESS_FAIL, {"error": "could not resolve a git worktree root"}

    live_path = Path(plan_path) if Path(plan_path).is_absolute() else root / plan_path
    if not live_path.is_file():
        return EXIT_BUSINESS_FAIL, {"error": f"{plan_path}: not found"}

    try:
        text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return EXIT_BUSINESS_FAIL, {"error": f"{plan_path}: could not read ({exc})"}

    split = split_frontmatter(text)
    if split is None:
        return EXIT_BUSINESS_FAIL, {"error": f"{plan_path}: no parseable frontmatter"}

    try:
        sha = _canonical_body_sha(text, root)
    except RuntimeError as exc:
        return EXIT_BUSINESS_FAIL, {"error": str(exc)}

    note = _compose_invocation_note(utterance, typed_command)

    # This pre-read is advisory only -- it decides which `at` to hand the
    # delegate. The authoritative convergence test is the delegate's own,
    # inside `locked_rmw`; a concurrent write between these two reads costs
    # at worst a redundant identical stamp, never a lost one.
    fm = split.fm_text
    existing_sha = read_fm_field_unquoted(fm, "execution_authorized_sha")
    existing_note = _current_note_text(
        fm, read_fm_block_scalar(fm, "execution_authorized_note")
    ) or ""
    existing_at = read_fm_field_unquoted(fm, "execution_authorized_at")
    body_unchanged_since_stamp = existing_sha == sha

    if body_unchanged_since_stamp and existing_note.endswith(note):
        return EXIT_OK, {
            "applied": False,
            "sha": sha,
            "note": note,
            "message": (
                f"{plan_path} is already execution-authorized by this invocation against "
                "unchanged plan content -- no-op"
            ),
        }

    at_value = at or (existing_at if body_unchanged_since_stamp and existing_at else None)

    exit_code, result = stamp_execution_authorization(
        plan_path,
        "PM",
        note,
        at=at_value,
        repo_root=root,
        append_note=True,
    )
    if exit_code == EXIT_OK:
        result["note"] = note
    return exit_code, result


USAGE = (
    "usage: review-exec-auth-stamp stamp <plan-path> --by <who> "
    "(--note <note> | --append-note <text>) [--at <YYYY-MM-DD>]\n"
    "       review-exec-auth-stamp authorize-invocation <plan-path> "
    "--typed-command </command> [--utterance <PM's verbatim words>] [--at <YYYY-MM-DD>]"
)


def _main_authorize_invocation(rest: list[str]) -> int:
    """`authorize-invocation <plan-path> --utterance <text> --typed-command
    </cmd> [--at <YYYY-MM-DD>]` — the PM-invocation mint (see
    `stamp_invocation_authorization`). `--by` is deliberately NOT an
    argument: this verb only ever writes `PM`."""
    import json
    import sys

    if not rest or rest[0].startswith("--"):
        print("review-exec-auth-stamp: missing required <plan-path>", file=sys.stderr)
        return EXIT_USAGE
    plan_path = rest[0]

    utterance: Optional[str] = None
    typed_command: Optional[str] = None
    at: Optional[str] = None
    i = 1
    while i < len(rest):
        arg = rest[i]
        if arg == "--utterance" and i + 1 < len(rest):
            utterance = rest[i + 1]
            i += 2
        elif arg == "--typed-command" and i + 1 < len(rest):
            typed_command = rest[i + 1]
            i += 2
        elif arg == "--at" and i + 1 < len(rest):
            at = rest[i + 1]
            i += 2
        else:
            print(f"review-exec-auth-stamp: unrecognized argument: {arg}", file=sys.stderr)
            return EXIT_USAGE

    # `--utterance` is deliberately optional: a bare `/execute-plan` with no
    # accompanying words is still a PM act, and the mint records it as one.
    if typed_command is None:
        print(
            "review-exec-auth-stamp: --typed-command is required",
            file=sys.stderr,
        )
        return EXIT_USAGE

    exit_code, result = stamp_invocation_authorization(
        plan_path, utterance, typed_command, at=at
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


def main(argv: list[str]) -> int:
    """`review-exec-auth-stamp stamp <plan-path> --by <who> (--note <note> | --append-note <text>) [--at <date>]`

    `--note` replaces `execution_authorized_note` outright (the long-standing
    default -- unchanged, so no existing caller's behaviour shifts).
    `--append-note` instead appends its text to whatever the field already
    holds (see `stamp_execution_authorization`'s `append_note` docstring).
    The two are mutually exclusive.

    `authorize-invocation` is the second verb: the PM-invocation mint, which
    supplies its own `by`/`note` and delegates to the same single stamping
    surface -- see `_main_authorize_invocation`.
    """
    import json
    import sys

    if argv[:1] and argv[0] in ("--help", "-h"):
        print(USAGE)
        return EXIT_OK

    if argv[:1] and argv[0] == "authorize-invocation":
        return _main_authorize_invocation(argv[1:])

    if not argv or argv[0] != "stamp":
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE

    rest = argv[1:]
    if not rest or rest[0].startswith("--"):
        print("review-exec-auth-stamp: missing required <plan-path>", file=sys.stderr)
        return EXIT_USAGE
    plan_path = rest[0]

    by: Optional[str] = None
    note: Optional[str] = None
    append_note_text: Optional[str] = None
    at: Optional[str] = None
    i = 1
    while i < len(rest):
        arg = rest[i]
        if arg == "--by" and i + 1 < len(rest):
            by = rest[i + 1]
            i += 2
        elif arg == "--note" and i + 1 < len(rest):
            note = rest[i + 1]
            i += 2
        elif arg == "--append-note" and i + 1 < len(rest):
            append_note_text = rest[i + 1]
            i += 2
        elif arg == "--at" and i + 1 < len(rest):
            at = rest[i + 1]
            i += 2
        else:
            print(f"review-exec-auth-stamp: unrecognized argument: {arg}", file=sys.stderr)
            return EXIT_USAGE

    if note is not None and append_note_text is not None:
        print("review-exec-auth-stamp: --note and --append-note are mutually exclusive", file=sys.stderr)
        return EXIT_USAGE

    if by is None or (note is None and append_note_text is None):
        print(
            "review-exec-auth-stamp: --by and one of --note/--append-note are required",
            file=sys.stderr,
        )
        return EXIT_USAGE

    append_note = append_note_text is not None
    note_value = append_note_text if append_note else note
    assert note_value is not None  # narrowed by the checks above

    exit_code, result = stamp_execution_authorization(
        plan_path, by, note_value, at=at, append_note=append_note
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code
