"""
coordinator_core.ops.session_commits — "session.commits" op.

Purpose: the missing primitive `ops/handoff_close_origin_stub.py`'s own
negative-spec documents as absent from disk ("no existing op accepts a
session_id and performs a `git show --format=%(trailers:key=Session-Id)`
walk" — see that module's docstring, "Does NOT fall back to a branch-tip
SHA"). Given a `session_id` and an optional `commit_range`, returns the
attributed sha list plus per-sha subject, touched paths and numstat — from
ONE `git log` invocation, not a per-sha loop.

Anchoring resolution (this chunk's required decision — two sites on disk
disagree, and the divergence is a live production defect, not a style
question):

    - `workstream_complete/__init__.py :: _session_owned_shas` greps
      `^Session-Id: <sid>` (anchored at line-start only, no trailing `$`).
      Documented KNOWN over-match: `git log --grep` scans every line of the
      FULL commit message, so a body line quoting another session's trailer
      verbatim also matches. Documented as the SAFE direction on purpose.
    - `ops/review_brightline_gate.py :: _compute_session_oracle_single`
      (removed 2026-08-19, state/kill-ledger.md K-007)
      greps `^Session-Id: <sid>$` (anchored at BOTH ends). Verified live
      (workstream_complete's own docstring) to silently DROP a commit whose
      `Session-Id` line is not the message's final line — e.g. one followed
      by `Co-Authored-By`/`Commit-Token`, a shape this history actually
      contains. That is an UNDER-count: it quietly shrinks whatever the
      caller is measuring (a review scale, an attribution set, ...).

    This op takes `_session_owned_shas`'s form: `^Session-Id: <sid>`,
    unanchored at the end. An under-count is the wrong failure mode for a
    primitive other ops build attribution and audit trails on top of — a
    missing commit silently vanishes evidence, where an occasional
    over-matched body-line quote is a false positive a caller can filter
    (and duplicate-sha collapse below already guards the case where the
    SAME commit tags this session's own sid in its own trailer, which is
    not a body-line quote at all). A same-prefix collision is not a real
    risk against fixed-length UUID session ids (same acceptance the
    unanchored form already carries on disk).

Multi-Session-Id commits are real in this history (a fold can tag a commit
with more than one session's trailer) — `--grep` matches the commit once
regardless of how many Session-Id lines it carries, so this op's output has
no duplicate sha for that shape. A plumbing commit (`git commit-tree`,
bypassing the porcelain `commit` path) carries no Session-Id trailer at all
and is correctly absent from the result, same as any other untagged commit
— NOT an error, since "session committed nothing (attributable)" is a real,
representable answer distinct from a git failure (see Returns below).

NOT in this chunk: migrating any of the five call sites this primitive is
meant to replace (`branch_resolution.py`'s `_session_commit_log` /
`_session_touched_paths` / `_session_added_plans` /
`session_commit_count_attributed` / `_session_diff_loc`,
`quick_wrap_assemble :: _novel_loc_split`,
`workstream_complete :: _session_owned_shas`, `coverage.py`'s per-add-sha
`git log -1` loop). That is C5 — deliberately a separate chunk so a
regression is bisectable to the migration rather than to this primitive.

Per-file add-status (follow-up to C5, same plan): `files[i].status` composes
`--raw` alongside `--numstat` in the SAME invocation — git emits both diff
formats, in the same per-file order, for one walk, so this is a second
FORMAT of the one walk, not a second git call. This is what lets
`_session_added_plans` (`branch_resolution.py`) migrate onto this primitive
too: it needs to tell a file ADDED by a commit apart from one merely
touched, which the numstat-only shape could not answer.

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C4

Negative-spec (hard-won):
  - Does NOT spawn one `git` invocation per commit — the whole point of the
    primitive (see `handoff_close_origin_stub.py`'s and `coverage.py`'s
    un-batched loops this replaces) is ONE `git log` call, composing
    `--raw` and `--numstat` as two output formats of that same walk, whose
    output already interleaves subject + raw status + numstat per commit.
  - Does NOT use `--grep=...$` (the anchored-both-ends form) — see the
    anchoring resolution above; that form is a documented under-count.
  - Does NOT accept a session_id that resolves to `--all`/no scoping — an
    empty/blank `session_id` raises rather than silently returning every
    commit in the repo.
  - Does NOT collapse "session committed nothing (attributable)" and "git
    itself failed" into the same return shape — the former is `[]`, the
    latter raises (mirrors `ceremony.chunk_commits`'s AC6 discipline).
  - Does NOT migrate any existing caller onto itself — see "NOT in this
    chunk" above; that is C5.

C1 (docs/plans/2026-08-26-the-close-path-spends-its-last-known-levers.md
§ C1): `resolve_session_commits` gained a `sha_only=True` path that issues
neither `--raw` nor `--numstat` and skips the diff-payload parser entirely,
for `workstream_complete._session_owned_shas` — which only ever read
`c["sha"]` off the full-data return and threw the rest away. ADDITIVE: the
default (`sha_only=False`) return is byte-identical to before this
parameter existed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import _git
from coordinator_core.ops.fleet._common import main_worktree_root

#: Field separator between the per-commit header's sha and subject in the
#: `--format=` string below. A control byte that cannot appear in a commit
#: subject line (`%s` is single-line by definition), so a naive
#: `str.partition` split on this byte is unambiguous.
_FIELD_SEP = "\x1f"

#: Line-prefix marker distinguishing a per-commit header line (sha+subject)
#: from a `--numstat` data row in the same concatenated output stream. A
#: control byte that cannot appear at the start of a `--numstat` row (those
#: always start with a digit or `-`), so a plain `startswith` test is
#: unambiguous.
_HEADER_SENTINEL = "\x02"


def resolve_session_commits(
    repo_root: Path,
    session_id: str,
    commit_range: Optional[str] = None,
    *,
    sha_only: bool = False,
) -> List[Dict[str, Any]]:
    """Return this session's attributed commits, oldest-first, from ONE git
    invocation.

    Params:
        repo_root:     git worktree root the query runs in.
        session_id:    the `Session-Id` trailer value to select on. Matched
                        via `--grep=^Session-Id: <session_id>` — see this
                        module's docstring for why that form (not the
                        trailing-`$`-anchored one) was chosen.
        commit_range:  optional git revision range/arg (e.g. `"<sha>..HEAD"`
                        or `"--all"`) narrowing the log walk. Absent ->
                        no revision argument, i.e. git's own default walk
                        from HEAD over the current branch (mirrors
                        `_session_owned_shas`'s un-ranged form).
        sha_only:      ADDITIVE. When True, the `git log` invocation omits
                        `--raw`/`--numstat` entirely and the diff-payload
                        parser below is skipped — for a caller that only
                        needs the attributed sha list (e.g.
                        `workstream_complete._session_owned_shas`) and
                        previously threw the whole parsed diff payload away.
                        When False (default), behavior is byte-identical to
                        before this parameter existed — see AC2.

    Returns:
        A list of, oldest-first:
            {
              "sha": str,
              "subject": str,
              "committer_epoch": int,       # %ct, from the same header line
              "touched_paths": List[str],   # deduped, insertion order
              "added": int,                 # total insertions, this commit
              "deleted": int,                # total deletions, this commit
              "files": [{"path": str, "added": Optional[int],
                         "deleted": Optional[int], "status": Optional[str]},
                        ...],
            }
        `committer_epoch` is read off the same per-commit header line as
        `sha`/`subject` (`%ct` in the `--format=` string) — no per-commit
        date lookup, and what lets a caller like `_session_added_plans`
        (`branch_resolution.py`) apply its own `--since`-equivalent floor
        without a second git call.
        `files[i].added`/`deleted` is `None` for a binary row (git numstat
        emits `-` for both columns on a binary diff); that commit's
        `added`/`deleted` totals only sum resolvable (non-binary) rows.
        `files[i].status` is the single-letter `git log --raw` change-type
        code for that row (`"A"` added, `"M"` modified, `"D"` deleted, `"R"`
        renamed, `"C"` copied, ...; a rename/copy score suffix like `R100` is
        truncated to its leading letter) from the SAME invocation as the
        numstat counts (`--raw` composed alongside `--numstat` — git emits
        both diff formats, in the same per-file order, for one walk; no
        second git call). `None` when the raw/numstat row counts for a
        commit diverge (defensive — should not happen for a non-merge
        commit) and no status could be paired with that file row.
        `[]` when `session_id` is attributable but matched zero commits —
        a real, representable answer, never conflated with a git failure.

        When `sha_only=True`, each dict is `{"sha": str}` only — no
        `subject`/`committer_epoch`/`touched_paths`/`added`/`deleted`/
        `files` keys, since none of that data was requested from git.

    Raises:
        ValueError: `session_id` is blank.
        RuntimeError: the underlying `git log` invocation failed (non-zero
            exit).
    """
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError(
            "session.commits: session_id is required and must be a "
            "non-empty string — an empty/blank value would silently widen "
            "the query to every commit in the repo"
        )

    args = ["log", "--reverse", f"--grep=^Session-Id: {sid}"]
    if not sha_only:
        args.extend(["--raw", "--numstat"])
    args.append(f"--format={_HEADER_SENTINEL}%H{_FIELD_SEP}%ct{_FIELD_SEP}%s")
    if commit_range:
        args.append(commit_range)

    result = _git(args, cwd=repo_root)
    if not result.ok:
        raise RuntimeError(
            f"session.commits: git log failed for session_id={sid!r} "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )

    if sha_only:
        shas: List[Dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if not line.startswith(_HEADER_SENTINEL):
                continue
            sha, sep, _rest = line[len(_HEADER_SENTINEL):].partition(_FIELD_SEP)
            if not sep:
                continue
            shas.append({"sha": sha})
        return shas

    commits: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    seen_paths: Optional[set] = None
    pending_statuses: List[str] = []

    for line in result.stdout.splitlines():
        if line.startswith(_HEADER_SENTINEL):
            sha, sep, rest = line[len(_HEADER_SENTINEL):].partition(_FIELD_SEP)
            ct_raw, sep2, subject = rest.partition(_FIELD_SEP)
            if not sep or not sep2:
                # Malformed/unsplit header (should not happen with a
                # well-formed --format) — skip rather than guess.
                current = None
                seen_paths = None
                continue
            try:
                committer_epoch = int(ct_raw)
            except ValueError:
                committer_epoch = 0
            current = {
                "sha": sha,
                "subject": subject,
                "committer_epoch": committer_epoch,
                "touched_paths": [],
                "added": 0,
                "deleted": 0,
                "files": [],
            }
            seen_paths = set()
            pending_statuses = []
            commits.append(current)
            continue

        if current is None or seen_paths is None:
            continue

        if line.startswith(":"):
            # --raw row: ":<old-mode> <new-mode> <old-sha> <new-sha> "
            # "<status>[<score>]\t<path>[\t<path2>]" — <status> is the
            # single-letter change-type code this function surfaces as
            # files[i].status. Collected here, oldest-first per commit,
            # then paired positionally with the --numstat rows below (git
            # emits both diff formats in the same per-file order for one
            # walk, so positional pairing needs no path-matching — load-
            # bearing for a rename, whose --raw path and --numstat "old =>
            # new" path text do not match verbatim).
            meta = line[1:].split(None, 4)
            if len(meta) == 5:
                status_field = meta[4].split("\t", 1)[0]
                if status_field:
                    pending_statuses.append(status_field[0])
            continue

        if not line.strip():
            continue

        # --numstat row: "<added>\t<deleted>\t<path>" (binary: "-\t-\t<path>")
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path = parts
        added = int(added_raw) if added_raw.isdigit() else None
        deleted = int(deleted_raw) if deleted_raw.isdigit() else None
        status = pending_statuses.pop(0) if pending_statuses else None
        current["files"].append(
            {"path": path, "added": added, "deleted": deleted, "status": status}
        )
        if added is not None:
            current["added"] += added
        if deleted is not None:
            current["deleted"] += deleted
        if path not in seen_paths:
            seen_paths.add(path)
            current["touched_paths"].append(path)

    return commits


@register_op("session.commits")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """JSON-RPC "session.commits" handler.

    Params:
        session_id     (str, required) — see `resolve_session_commits`.
        commit_range   (str, optional) — see `resolve_session_commits`.

    `repo_root` is the engine-injected common dir (`_OP_KEY_SCOPE =
    "common_dir"` — see `coordinator_core.op_scopes`); the worktree root is
    derived via `main_worktree_root(repo_root)`, same convention as
    `ceremony.wsc_tail` / `resolve_session_branches`.

    Returns:
        The bare list `resolve_session_commits` returns — the JSON-RPC
        "result" field IS this list, unwrapped.

    Raises (propagates to a non-zero CLI exit — dispatch_message converts an
    uncaught handler exception to a JSON-RPC INTERNAL_ERROR response):
        ValueError: params.session_id missing/blank, or repo_root not
            supplied by the engine.
        RuntimeError: the underlying `git log` invocation failed.
    """
    if repo_root is None:
        raise ValueError(
            "session.commits: repo_root arg is None — common_dir not "
            "supplied by engine (check op_scopes['session.commits'] == "
            "'common_dir')"
        )

    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError(
            "session.commits: params.session_id is required and must be a "
            "non-empty string"
        )

    commit_range = params.get("commit_range")
    if commit_range is not None and (
        not isinstance(commit_range, str) or not commit_range.strip()
    ):
        raise ValueError(
            "session.commits: params.commit_range, when given, must be a "
            "non-empty string"
        )

    worktree_root = main_worktree_root(Path(repo_root))
    return resolve_session_commits(
        worktree_root, session_id.strip(), commit_range.strip() if commit_range else None
    )
