"""
coordinator_core.ops.archive_auto_memory_rows — the archive-on-drain sibling
op to ``check_auto_memory_drained``.

THE POINT: a closure ceremony's drain gate can today order a deletion whose
only record is the ceremony's own transcript. This op is the sibling the
ceremony calls BEFORE that deletion: it evicts the closing session's own
auto-memory residue from the hot tier into a tracked, greppable cold tier —
``<owning-repo>/state/auto-memory-archive/`` — and commits it, so a later
session can recover an evicted row by ``git show`` or a plain ``grep``.

NOT a mode on the gate. ``check_auto_memory_drained`` stays read-only by
design (see its own docstring's negative-spec); this is a standalone sibling
op with its own entrypoint, called by the ceremony as a separate step.

Universe by import, never re-derived: the row selection AND the
memory-directory enumeration both come from
``coordinator_core.ops.check_auto_memory_drained``'s ``_own_residue`` /
``_own_memory_dirs`` / ``_own_index_rows`` / ``_own_body_files`` — this
module defines no independent ``originSessionId`` parsing and no independent
``MEMORY.md`` row regex. A second copy of either is the defect this plan is
most likely to introduce (see that module's own docstring); importing keeps
AC5 literally true rather than aspirational.

Destination and naming: ``<root>/state/auto-memory-archive/<YYYY-MM-DD>-
<short-sid>.md``, where ``root`` is this op's own resolved repo root (same
resolution the gate uses, ``_resolve_root``), and ``<short-sid>`` is the
closing session id truncated to ``_ARCHIVE_SID_PREFIX_LEN`` characters. The
name alone does not guarantee uniqueness — a ceremony can run twice in a
day — so an existing day's file is APPENDED to, never overwritten: each run
adds its own drain-event section.

Artifact shape (AC7 — retrievable by content, not merely by ``git show``):
plain markdown. Each evicted body gets its own row section, delimited by an
explicit machine marker (``<!-- row: <path> -->`` / ``<!-- end row: <path>
-->``) so a body whose own content happens to start with ``---`` frontmatter
or contain ``## `` headings can never be mistaken for the archive artifact's
own section structure — fencing, not escaping (escaping would defeat a plain
grep for the remembered text, which is exactly what AC7 promises). Body
content is VERBATIM and UNESCAPED. ``MEMORY.md`` rows go in their own
section as raw reference lines pointing at the body path, never
re-embedding the body text a second time.

Dedupe (AC7/AC5): a body referenced by more than one index row, or present
in both the body-file scan and the index-row scan, is emitted exactly once,
keyed by ``os.path.normcase(str(path.resolve()))``. Index rows are NOT
deduped against each other — two distinct ``MEMORY.md`` lines pointing at
the same body both survive as separate reference lines (mirroring
``_own_index_rows``'s own non-collapsing contract).

Fail-closed identity branch: mirrors the gate exactly. An unresolvable
closing-session id archives nothing and exits 0 with a stderr advisory,
rather than guessing whose rows these are.

Commit (C3b, re-planned 2026-09-11): this op self-commits the single
artifact path it wrote, via ``coordinator_core.git.commit.commit_paths``
called in-process — zero git spawns, no push leg, no lock nesting (see that
call site's own comment below for the full rationale). This is the option
(a), commit-only carve-out resolved by
``cross-repo/inbox/2026-08-07-doe-claude-em-archive-op-carveout-answer-
option-a.md`` and registered at DR-413.

Exit codes:
    0 — memory dir(s) absent or holding no own-session residue (nothing to
        archive), OR the closing session's own identity is unresolvable
        (fail-closed, matching the gate), OR the artifact was already
        durable (``NothingToCommit`` on a byte-identical re-run).
    1 — the repo root could not be resolved (this op, unlike the read-only
        gate, cannot proceed without a destination to write into), OR a
        write to the artifact file failed, OR the commit was refused for
        any reason other than "already durable". Every failing path is
        named on stderr.

Windows-first: naked Python 3.11+, no new ``.sh``, no bash, and this module
spawns no subprocess of its own at all — ``commit_paths`` is zero-spawn by
contract, which is what keeps this op inside the 500ms brightline.

Spec backlink: this repo
  docs/plans/2026-08-07-archive-on-drain-memory-evicts-to-cold-tier.md
  § C3a, C3b (re-planned 2026-09-11), AC1, AC2, AC5, AC7, AC8, AC10, AC11,
  AC12.

Negative-spec:
  - Does NOT re-derive ``originSessionId`` filtering, ``MEMORY.md`` row
    resolution, home-root union resolution, or slug derivation — all of it
    is imported from ``check_auto_memory_drained``.
  - Does NOT add a mutation path to ``check_auto_memory_drained`` itself —
    this is a standalone sibling op, never a flag or mode on the gate.
  - Does NOT invert a slug to find a repo root, and does NOT repair
    ``discover_working_repos``'s POSIX decode gap — this op only ever
    consumes its own ``--root``, exactly like the gate.
  - Does NOT archive a peer session's body or index row — the universe it
    consumes (``_own_residue``) is already narrowed to the closing
    session's own rows, and this op adds no widening of its own.
  - Does NOT build a restore op — this is a record, kept retrievable; a
    restore mechanism is explicitly out of budget for this plan.
  - Does NOT push. ``commit_paths``/``commit_v2`` contain no push leg at
    all; the ceremony's own push step is untouched.
  - Does NOT delete anything — eviction from the hot tier is the
    ceremony's own job, ordered only after this op exits 0.
  - Never silently swallows a write or commit failure — a failure to write
    or to commit exits non-zero and names the failing path (AC11); the
    ceremony must not proceed to deletion on that non-zero exit.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

from coordinator_core.git.commit import CommitRefused, NothingToCommit, commit_paths
from coordinator_core.ops.check_auto_memory_drained import (
    _resolve_root,
    _own_residue,
)
from coordinator_core.session.worktree_safety import resolve_self_session_id

# Name-collision narrowing tradeoff: a ceremony can run twice on the same
# calendar day, so the archive filename is not unique on the date alone.
# 12 hex characters of a UUID4-shaped session id leaves an astronomically
# small collision probability for the "two drains, same day, same closing
# session" case this constant exists to narrow, without making every
# filename unreadably long.
_ARCHIVE_SID_PREFIX_LEN = 12

_ARCHIVE_DIRNAME = os.path.join("state", "auto-memory-archive")


def _archive_dest(root: str, self_sid: str, now: "datetime | None" = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    short_sid = self_sid[:_ARCHIVE_SID_PREFIX_LEN]
    return Path(root) / _ARCHIVE_DIRNAME / f"{stamp}-{short_sid}.md"


def _render_section(path: Path) -> "str | None":
    """The archive artifact's section for one body path, VERBATIM and
    unescaped. Returns ``None`` if the body cannot be read (skipped rather
    than fatal — a body that vanished between selection and write is not
    this op's failure to report)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    marker = str(path)
    return (
        f"<!-- row: {marker} -->\n"
        f"### {marker}\n\n"
        f"{text}\n"
        f"<!-- end row: {marker} -->\n"
    )


def _render_drain_event(
    self_sid: str,
    bodies: "List[Path]",
    rows: "List[Tuple[str, Path]]",
    now: "datetime | None" = None,
) -> "str | None":
    """The full section for one drain event (one op invocation), or
    ``None`` if there is nothing to render (every candidate body vanished
    before it could be read)."""
    ts = (now or datetime.now(timezone.utc)).isoformat()

    seen: "set[str]" = set()
    ordered_bodies: "List[Path]" = []
    for p in bodies:
        key = os.path.normcase(str(p.resolve()))
        if key not in seen:
            seen.add(key)
            ordered_bodies.append(p)
    for _line, target in rows:
        key = os.path.normcase(str(target.resolve()))
        if key not in seen:
            seen.add(key)
            ordered_bodies.append(target)

    body_sections = []
    for p in ordered_bodies:
        section = _render_section(p)
        if section is not None:
            body_sections.append(section)

    if not body_sections and not rows:
        return None

    parts = [f"<!-- drain event: {ts} self_sid={self_sid} -->\n"]
    if body_sections:
        parts.append("## Bodies\n\n")
        parts.append("\n".join(body_sections))
    if rows:
        parts.append("\n## Index rows\n\n")
        for line, target in rows:
            parts.append(f"<!-- row: {target} -->\n{line}\n<!-- end row: {target} -->\n")
    return "\n".join(parts) + "\n"


def main(argv: "List[str]") -> int:
    explicit_root: "str | None" = None
    explicit_session_id: "str | None" = None
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            explicit_root = argv[i + 1]
            i += 2
        elif argv[i] == "--session-id" and i + 1 < len(argv):
            explicit_session_id = argv[i + 1]
            i += 2
        else:
            i += 1

    root = _resolve_root(explicit_root)
    if not root:
        print(
            "archive-auto-memory-rows: FAILED -- could not resolve a repo "
            "root (no --root and no git toplevel), so no owning-repo slug "
            "and no archive destination could be determined. Nothing was "
            "written.",
            file=sys.stderr,
        )
        return 1

    self_sid = (
        explicit_session_id
        if explicit_session_id is not None
        else resolve_self_session_id(root)
    )
    if not self_sid:
        print(
            "archive-auto-memory-rows: SKIPPED -- could not resolve this "
            "session's own identity (no --session-id, no "
            "CLAUDE_CODE_SESSION_ID in the environment), so this op cannot "
            "honestly attribute any residue to it and was skipped rather "
            "than archiving the whole store.",
            file=sys.stderr,
        )
        return 0

    bodies, rows = _own_residue(root, self_sid)
    if not bodies and not rows:
        return 0

    event_text = _render_drain_event(self_sid, bodies, rows)
    if event_text is None:
        return 0

    dest = _archive_dest(root, self_sid)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            with dest.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write("\n")
                fh.write(event_text)
        else:
            dest.write_text(event_text, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(
            f"archive-auto-memory-rows: FAILED writing {dest}: {exc}. "
            "Nothing was committed.",
            file=sys.stderr,
        )
        return 1

    rel_path = str(dest.relative_to(Path(root))).replace(os.sep, "/")
    message = (
        f"archive-auto-memory-rows: archive drain residue for session "
        f"{self_sid[:_ARCHIVE_SID_PREFIX_LEN]}"
    )
    # Never `prefer_deliberate_stage`: this op commits a path it authored
    # itself in the same pass, so there is no third party's deliberate
    # partial stage to preserve. Zero git spawns (`commit_paths` contract),
    # no push leg (`commit_paths`/`commit_v2` carry none at all), and no
    # `wsc-commit` lock is taken here -- DR-413 binds this op to never being
    # invoked from inside a commit critical section, which this call site
    # cannot itself enforce (a caller-side sequencing constraint, not a
    # runtime check).
    try:
        commit_paths(
            repo=root,
            paths=[rel_path],
            message=message,
            allow_empty=False,
        )
    except NothingToCommit:
        # The artifact is byte-identical to HEAD's -- already durable from a
        # prior run of this same drain event. Not a failure (AC2/AC11
        # framing: option (a) buys a shorter window between write and
        # durability, not an enforced ordering this op could fail on).
        return 0
    except CommitRefused as exc:
        print(
            f"archive-auto-memory-rows: FAILED committing {rel_path}: {exc}. "
            "The ceremony must not proceed to deletion.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
