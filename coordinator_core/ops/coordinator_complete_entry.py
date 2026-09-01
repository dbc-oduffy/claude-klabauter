"""
coordinator_core.ops.coordinator_complete_entry — Port of:
coordinator-complete-entry.sh (DoE a1a568d2, 2026-07-22, 392 lines).

Purpose: scaffold a pre-filled workstream completion entry — the mechanical
spine of /workstream-complete Step 2.6 (completion-entry ladder) in one
callable, emitting a pre-filled entry with up to two model-residue
placeholders: ``<!-- NATURE-INFER -->`` (present only when nature is omitted)
and ``<!-- PROSE: ... -->`` (always present — title + body slot for the
model).

Not a JSON-RPC op — a plain module, NOT ``@register_op``'d, called by direct
import from the DoE polyglot trampoline (template-variant #1, mirrors
coordinator-auto-push / handoff-gate-aging / coordinator-render-rollup): the
DoE caller (coordinator/bin/coordinator-complete-entry) is itself Python, so
an in-process call is strictly cheaper than a JSON-RPC round-trip.

Wraps (never reimplements) three sibling helpers, exactly as the bash oracle
did:
  - Step 2.6.2 legacy-monolith migrate → now an IN-PROCESS call to the
    already-ported ``coordinator_core.ops.migrate_completion_log_legacy.main``
    (was: ``bash migrate-completion-log-legacy.sh --root <repo_root>``).
  - Step 2.6.5a chain-terminal LoE aggregation → an IN-PROCESS call to the
    already-ported ``coordinator_core.session_ledger.aggregate_chain_loe.main``
    (was: ``bash aggregate-chain-loe.sh --terminal-handoff <path> --format
    yaml-frontmatter``).
  - AC4 rollup-sentence render → PATH-first shim check (``shutil.which``),
    THEN an IN-PROCESS call to the already-ported
    ``coordinator_core.ops.coordinator_render_rollup.main`` (was: PATH-first
    ``command -v coordinator-render-rollup.sh``, then sibling, then ``bash
    <resolved> <deliverable_id> <repo_root>``). The PATH-first rung is
    PRESERVED (not dropped) — it exists specifically to let a test PATH-
    prepend a stub executable named ``coordinator-render-rollup.sh``
    (coordinator_render_rollup.py's own docstring: "PATH-first (enables test
    shims)"); an always-in-process call would have silently broken that
    testability path — caught by DoE's own
    ``coordinator/bin/tests/test-complete-entry-rollup.sh`` Test A during this
    port's own verification. Production runs (no such shim on PATH) always
    take the faster in-process branch. 2026-07-21: the shim invocation itself
    dropped its hardcoded ``bash`` prefix — the resolved shim path is now
    exec'd directly (``subprocess.run([shim, dlv_id, repo_root], ...)``),
    letting the OS resolve the interpreter via the shim's own shebang line
    (the sanctioned polyglot-trampoline pattern), rather than this module
    hardcoding bash as the interpreter. Byte-identical stdout capture for the
    existing POSIX ``#!/usr/bin/env bash`` test shim (chmod 0o755 + shebang
    already required for direct exec); this module no longer needs bash on
    PATH at all for its own bridges.
  - Step 2.6.5a single-session LoE (was: ``bash coordinator-session-loe.sh
    --format yaml-frontmatter``) is now a fully native, in-process
    reimplementation (``_native_single_session_loe`` /
    ``_count_session_dispatches`` below) — no subprocess, no bash, no DoE
    coupling. Resolves the session id via the already-ported
    ``coordinator_core.session.core.resolve_session_id`` (the same 4-tier
    chain the bash oracle's own ``cs_resolve_session_id`` implements),
    reads ``<sessions_dir>/<sid>/dispatched-agents.txt`` directly (the same
    file ``track_dispatched_agents.py`` already writes natively), and
    computes the t-shirt tier with the same any-criterion table
    ``coordinator_core.loe_thresholds.DEFAULT_THRESHOLDS`` encodes — see
    those functions' docstrings for the byte-parity notes (an absent
    ``dispatched-agents.txt`` degrades to unset/``null`` fields, exactly
    mirroring the bash oracle's ``ad=""``/``od=""`` unset state, NOT ``0``).
  - The Step 2.6.3a idempotency-guard primary signal (was: ``node
    query-records.js --type completion --where chain=<slug>``, a subprocess
    call into DoE's ``coordinator/bin/``) is now an IN-PROCESS call to
    ``coordinator_core.ops.ceremony.records_query.query_records("completion",
    ..., where=f"chain={chain_slug}")`` (2026-07-22 repoint onto claude-klabauter's
    native records-query seam). ``_query_records_existing_path`` re-renders
    each match with the same one-line-per-record shape
    ``query-records.js``'s ``TYPE_DISPLAY.completion`` produces
    (query-records.js:314 — ``- **title** [nature] (chain: ...) —
    commit,commit``, joined with ``formatRecords``'s markdown-list
    ``join('\n')`` at query-records.js:1601-1636) so the caller's stdout-
    content contract is reproduced exactly, not just its truthiness. The
    degrade-on-absence-or-failure contract is preserved byte-for-byte: an
    unknown-type ``ValueError`` or an unparseable-``where`` ``SystemExit``
    (both ports of query-records.js's own non-zero-exit paths) are caught
    and degrade to ``None``, exactly mirroring the oracle's ``[[ -x ... ]]``
    existence gate plus ``|| _qr_rc=$?`` discard-and-continue — the caller
    (``_idempotency_guard``) falls through to the grep-scan on any of these,
    same as before. No DoE coupling remains in this function.

Spec backlink: docs/plans/2026-07-06-cockpit-contract-v27 (§ coordinator-complete-entry)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md (BIG_PORT Wave B)

Negative-spec (faithfully reproduced from the bash oracle — do NOT "fix" mid-port):
    - Never seeds the ``commits:`` field — always ``commits: []``. Step 2.6.8
      owns that field exclusively via reconcile-completion-commits (Session-Id
      trailer reconcile), a separate port not in scope here.
    - The idempotency guard's grep-scan is a LITERAL match on the quoted YAML
      value as written (``chain: "<slug>"``), not a regex — mirrors the bash
      oracle's ``grep -qF`` (avoids metacharacter mismatches when the slug
      contains ``.`` or ``-``).
    - ``query-records.js`` degrades silently to the grep-scan result on
      absence, non-zero exit, empty stdout, or the sibling script simply not
      existing at the resolved DoE bin dir — mirrors the oracle's
      ``[[ -x ... ]]`` existence gate plus ``|| _qr_rc=$?`` discard-and-continue.
    - LoE block degrades to an explicit null block
      (``agent_dispatches/opus_dispatches/em_tokens/tshirt: null``) on ANY
      failure or empty output from either LoE path — never a hard error.
    - Rollup-sentence resolution is fail-open at every stage (contract §4.2):
      any guard miss (no ``--governing-plan-slug``, no plan file, no
      ``deliverable_id:`` frontmatter line, render exception) leaves
      ``rollup_sentence`` empty; render failure never aborts the write.
    - Exit codes reproduced exactly: 0 — entry written, or idempotent no-op
      (pre-existing chain entry found); 1 — argument error; 2 — environment
      error (not inside a git repository). A NEW dedicated code (3) is used
      ONLY by the DoE-side trampoline for an engine-root/import transport
      failure (this module never returns 3 itself — see the trampoline's own
      docstring; this is a fail-loud ceremony tool, not a best-effort/
      never-block script, so a transport outage must not misclassify as
      business rc 0/1/2 per the porter-brief transport-failure rule).
"""

from __future__ import annotations

# Generator-provenance declaration: _write_entry() (via resolve_entry_path/
# resolve_effective_entry_path) writes a new completion-entry scaffold under
# archive/completed/{yyyymm}/{yyyymmdd}-{chain_slug}-{sid6}.md -- a
# date/session/chain-dependent output path with no single fixed artifact.
MUTATES = ["archive/completed/**/*.md"]

import io
import os
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from typing import List, NamedTuple, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.launchable import resolve_launchable, which_path_ordered
from coordinator_core.ops._git_root_util import git_root
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.ceremony.wsc_disposition import (
    MEMO_PREDECESSOR,
    PREDECESSOR_CONSUMED,
    SINGLE_SESSION,
    VALID,
    canonicalize,
)
from coordinator_core.ops import coordinator_render_rollup as _render_rollup_mod
from coordinator_core.ops import migrate_completion_log_legacy as _migrate_mod
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.session_ledger import aggregate_chain_loe as _agg_loe_mod
from coordinator_core.session import core as _session_core
from coordinator_core.loe_thresholds import compute_tshirt_nullable as _compute_tshirt_nullable
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

_VALID_NATURES = ("roadmap", "bugfix", "tech-debt", "infra")
_GOVERNING_PLAN_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Review: code-reviewer — --sid reaches the same class of filename-construction
# sink as --governing-plan-slug (sid6 = sid[-6:] spliced into entry_filename)
# but was missing the adjacent flag's allowlist guard; validate at parse time.
_SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
#: A git object name as `--commits` will accept it.
#: Bounded 7-40 to match git's own abbreviation floor and full-sha length --
#: shorter is ambiguous in any real repo, longer is not a sha at all. NOT
#: resolved against the object database here: a backfill routinely reconstructs
#: a record for work on a branch this checkout may not hold, and refusing an
#: unresolvable sha would make the tool useless in exactly its own use case.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# A2 hardening — every subprocess call into a still-bash/node sibling gets a
# bounded timeout + stdin guard, never present in the bash oracle (which had
# no such protection), per the porter-brief addendum rule 2.
_SUBPROCESS_TIMEOUT_SECS = 20

_NULL_LOE_BLOCK = (
    "loe:\n"
    "  agent_dispatches: null\n"
    "  opus_dispatches: null\n"
    "  em_tokens: null\n"
    "  tshirt: null"
)

#: The exact placeholder title `_write_entry` seeds a fresh scaffold with —
#: also the ONE literal this module's own idempotency predicate below
#: compares against to decide whether an existing entry's title has been
#: hand-authored yet.
_PLACEHOLDER_TITLE = "PLACEHOLDER — replace with past-tense workstream title"

#: The exact placeholder prose comment `_write_entry` seeds a fresh scaffold
#: with. An authored entry has REPLACED this line (not merely appended
#: text after it) — its continued presence in the body is the prose-residue
#: signal.
_PROSE_PLACEHOLDER_MARKER = (
    "<!-- PROSE: Replace this with a ≤8-sentence past-tense description of "
    "what shipped and why it matters. -->"
)


class _ExistingScaffoldState(NamedTuple):
    """One read of a completion entry's own three EM-authored surfaces —
    title, nature, prose — classified placeholder vs. authored. THE single
    placeholder-detection predicate for this CLI (state/bug-backlog/
    2026-07-28-workstream-complete-apply-re-scaffolds-t-e925d597e0af.yaml):
    consumed both by `_write_entry`'s own idempotent-preserving rewrite
    below and by `scaffold_residue_fields`, the public view
    `coordinator_core.workstream_complete.directives_
    completion.compute_completion_entry_scaffold_gate` reads to decide
    whether `workstream_complete.apply` must halt before `d-run-wsc-tail`.
    Never re-implemented a second time anywhere else in the tree."""

    exists: bool
    title: Optional[str]
    title_authored: bool
    nature: Optional[str]
    nature_authored: bool
    created: Optional[str]
    body: str
    prose_authored: bool


def _read_existing_scaffold_state(entry_path: str) -> _ExistingScaffoldState:
    if not os.path.isfile(entry_path):
        return _ExistingScaffoldState(
            exists=False,
            title=None,
            title_authored=False,
            nature=None,
            nature_authored=False,
            created=None,
            body="",
            prose_authored=False,
        )
    try:
        with open(entry_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        print(f"skip: _read_existing_scaffold_state: open({entry_path!r}) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return _ExistingScaffoldState(
            exists=False,
            title=None,
            title_authored=False,
            nature=None,
            nature_authored=False,
            created=None,
            body="",
            prose_authored=False,
        )

    parsed = parse_frontmatter(text)
    fm = parsed.get("frontmatter") or {}
    body = parsed.get("body") or ""

    title = fm.get("title")
    nature = fm.get("nature")
    created = fm.get("created")

    title_authored = bool(title) and str(title) != _PLACEHOLDER_TITLE
    nature_authored = nature is not None
    prose_authored = _PROSE_PLACEHOLDER_MARKER not in body

    return _ExistingScaffoldState(
        exists=True,
        title=str(title) if title is not None else None,
        title_authored=title_authored,
        nature=str(nature) if nature is not None else None,
        nature_authored=nature_authored,
        created=str(created) if created is not None else None,
        body=body,
        prose_authored=prose_authored,
    )


def scaffold_residue_fields(entry_path: str) -> List[str]:
    """Which of the three EM-authored surfaces (title, nature, prose) are
    STILL a placeholder at `entry_path` — `[]` means fully authored. A
    not-yet-existing entry reports all three as residue (nothing to author
    against yet)."""
    state = _read_existing_scaffold_state(entry_path)
    residue: List[str] = []
    if not state.title_authored:
        residue.append("title")
    if not state.nature_authored:
        residue.append("nature")
    if not state.prose_authored:
        residue.append("prose")
    return residue


def resolve_entry_path(
    repo_root: str, sid: str, chain_slug: str, for_date: Optional[date] = None
) -> str:
    """Derives TODAY's canonical filename (`{yyyymmdd}-{chain_slug or
    'adhoc'}-{sid6}.md` under `archive/completed/{yyyymm}/`) — the path
    `main()` writes to on a fresh (non-stand-down) run. This is NOT
    necessarily the path `main()` ends up reading/writing on any given
    invocation: when `chain_slug` already has an entry elsewhere in
    `archive/completed/` (any prior day, any prior session), `main()`
    stands down onto THAT entry instead (`_idempotency_guard`) and never
    reaches this derivation. Callers that need the path `main()` actually
    uses — including that stand-down case — must call
    `resolve_effective_entry_path`, not this function directly. Never
    re-derived a second time — see module Negative-spec."""
    # `for_date` is the BACKFILL date (--for-date), defaulting to today for
    # every ordinary close. Threaded as a parameter rather than re-read from
    # `date.today()` here: `main()` and `resolve_effective_entry_path` must
    # agree on ONE date, and a second `date.today()` call in this function is
    # also a live midnight-rollover race for a close that starts at 23:59:59.
    today = for_date or date.today()
    yyyymm = today.strftime("%Y-%m")
    yyyymmdd = today.strftime("%Y-%m-%d")
    sid6 = sid[-6:]
    if chain_slug:
        entry_filename = f"{yyyymmdd}-{chain_slug}-{sid6}.md"
    else:
        entry_filename = f"{yyyymmdd}-adhoc-{sid6}.md"
    return os.path.join(repo_root, "archive", "completed", yyyymm, entry_filename)


def _refuse_if_live_foreign_entry_holder(entry_path: str, repo_root: str, closing_sid: str) -> Optional[str]:
    """Refuse a stand-down onto an existing completion entry AUTHORED BY a
    DIFFERENT, LIVE session — defence-in-depth for the same session-shape-
    misdetection incident `plan_status_transition._refuse_if_live_foreign_
    holder` guards against (cross-repo memo `2026-08-10-example-retrieval-repo-em-wsc-
    misdetection-wrote-to-a-live-peers-plan.md`), but for the completion-
    entry stand-down path rather than the plan stamp: a misdetected session
    shape resolves the SAME `chain_slug` a live peer session already holds
    an in-progress completion entry for, `resolve_effective_entry_path`
    stands down onto that PEER's entry, `main()` still prints the foreign
    path to stdout and returns 0, and `d-reconcile-completion-commits`
    threads that path via `{d-complete-entry.entry_path}` and writes the
    CLOSING session's commit SHAs into the PEER's live entry.

    The discriminator is OWNERSHIP of the existing entry (its
    `authored_by` frontmatter field — the session id `_write_entry` stamps
    it with), never `chain_slug` — the slug is exactly what the
    misdetection got wrong in the first place, so a slug-keyed check would
    be vacuous under the same failure mode `plan_status_transition`'s own
    docstring rules out an equivalent plan-claim-keyed check for.

    Returns a human-readable refusal reason (non-None -> caller must fail
    loud, never print the foreign path) or `None` when the stand-down may
    proceed. TERMINAL-SAFE by construction, mirroring `plan_status_
    transition._refuse_if_live_foreign_holder` exactly: unreadable/missing
    `authored_by`, an unresolvable closing sid, self-authorship, or a dead
    holder all proceed (return `None`) — this only fires on a POSITIVELY-
    established live foreign holder, never on absence of evidence.
    """
    from coordinator_core.session.liveness import session_live

    state = _read_existing_scaffold_state(entry_path)
    if not state.exists:
        return None

    try:
        with open(entry_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    parsed = parse_frontmatter(text)
    fm = parsed.get("frontmatter") or {}
    authored_by = fm.get("authored_by")
    if not authored_by:
        return None
    authored_by = str(authored_by)

    if not closing_sid or authored_by == closing_sid:
        return None

    if not session_live(authored_by, cwd=repo_root):
        return None

    return (
        f"{entry_path} is a completion entry authored by LIVE session "
        f"{authored_by!r} (this session: {closing_sid!r}) — refusing to "
        "stand down onto a foreign in-flight entry"
    )


def resolve_effective_entry_path(
    repo_root: str, sid: str, chain_slug: str, for_date: Optional[date] = None
) -> tuple[str, Optional[str]]:
    """THE single resolution of the path `main()` actually reads/writes for
    a given session — the SAME decision `main()` makes between standing
    down onto an existing chain entry (`_idempotency_guard`'s date/sid-
    independent frontmatter scan across the whole `archive/completed/`
    tree) and deriving today's canonical path (`resolve_entry_path`) on a
    fresh run. Shared by `main()` (which uses the returned marker to skip
    the write pipeline entirely on stand-down) and
    `coordinator_core.workstream_complete.directives_completion.
    compute_completion_entry_scaffold_gate` (which reads only the path, to
    check scaffold residue against whichever entry `main()` would actually
    touch). Never re-derived a second time.

    Returns ``(entry_path, marker)``:
      - ``marker == "stand-down"``: `entry_path` is an EXISTING entry found
        elsewhere in the archive for `chain_slug` — `main()` never writes
        to it.
      - ``marker == "UNRECOVERABLE"``: the idempotency guard detected a
        `chain_slug` frontmatter match it could not resolve to a concrete
        file path — `entry_path` falls back to today's derived path (the
        pre-existing degrade-on-ambiguity shape), but `main()` treats this
        marker as a hard error, not a stand-down.
      - ``marker == "FOREIGN-LIVE"``: the stand-down candidate is owned by
        a DIFFERENT, LIVE session (`_refuse_if_live_foreign_entry_holder`)
        — `entry_path` is the FOREIGN entry's path for diagnostics only;
        `main()` treats this marker as a hard error and must NOT print it
        to stdout (that print is precisely what would hand the path
        downstream to `d-reconcile-completion-commits`).
      - ``marker is None``: `entry_path` is the freshly-derived canonical
        path for `for_date` (default today) — either `chain_slug` is empty,
        or no existing entry was found — the path `main()` will write to.

    `for_date` (--for-date, the backfill mode) moves ONLY the derived path.
    The idempotency guard above is date-independent BY CONSTRUCTION — it
    scans `chain_slug` frontmatter across the whole `archive/completed/`
    tree — so a backfill for an old date still stands down onto an existing
    entry for that chain wherever it lives, and cannot mint a second entry
    for a chain that already has one. That property is what makes backfill
    safe to expose at all; do not "optimize" the guard to the target month.
    """
    completed_dir = os.path.join(repo_root, "archive", "completed")
    if chain_slug:
        existing_path, marker = _idempotency_guard(repo_root, completed_dir, chain_slug)
        if existing_path == "UNRECOVERABLE":
            return resolve_entry_path(repo_root, sid, chain_slug, for_date), "UNRECOVERABLE"
        if existing_path:
            refusal = _refuse_if_live_foreign_entry_holder(existing_path, repo_root, sid)
            if refusal is not None:
                print(f"skip: resolve_effective_entry_path: {refusal}", file=sys.stderr)
                return existing_path, "FOREIGN-LIVE"
            return existing_path, marker
    return resolve_entry_path(repo_root, sid, chain_slug, for_date), None

_USAGE = """Usage: coordinator-complete-entry --sid <SID> --disposition <disp> [OPTIONS]

Required:
  --sid <WSC_SID>
      Session ID; last 6 characters become sid6 in the output filename.
  --disposition <single-session|predecessor-consumed|memo-predecessor>
      Single-session: one-session workstream.
      Predecessor-consumed: consuming and closing a multi-session handoff chain.
      (legacy spelling "chain-terminal" is also accepted)
      Memo-predecessor: this session's arc started at a cross-repo memo it
      picked up, not at a consumed handoff — no --consumed-handoff applies.

Options:
  --consumed-handoff <path>
      Path to the consumed terminal handoff file.
      Required when --disposition predecessor-consumed (or the legacy chain-terminal).
      Not applicable to --disposition memo-predecessor.
  --governing-plan-slug <slug>
      Plan slug driving this workstream (e.g. "2026-07-06-my-plan").
      When supplied: drives idempotency guard AND chain-slug in the filename.
      When omitted: filename uses "adhoc" segment.
  --nature <roadmap|bugfix|tech-debt|infra>
      Work category for the completion entry.
      When omitted: a <!-- NATURE-INFER --> residue placeholder is left in the file.
  --help
      Show this message.

Backfill mode (reconstructing the record of a session that has ended):
  --for-date <YYYY-MM-DD>
      Author the entry as of that date rather than today: it sets the
      archive/completed/<yyyy-mm>/ directory, the filename date segment, and
      created:. Required by the three flags below. May not be in the future.
      The idempotency guard is date-independent, so a backfill still stands
      down onto an existing entry for the same chain wherever it lives.
  --commits <sha,sha,...>
      Seed commits: with these shas (7-40 hex, comma-separated, de-duplicated,
      order preserved). The only path that seeds this field -- on a live close
      commits: is [] and belongs to the reconcile step.
  --authored-by-unknown
      OMIT authored_by rather than stamping --sid. For a reconstructed entry
      whose real session id is unknown -- never fabricate one, it would pollute
      the coverage sweep's known-session set. Omitted rather than written null
      because the schema types the field `string` and does not require it, so
      absent is valid and null is not.

Exit codes:
  0  entry written, or idempotent no-op (entry already exists for this chain slug)
  1  argument error
  2  environment error (not in a git repo)
"""


def _parse_args(argv: List[str]):
    """Parse argv. Returns (namespace_dict, exit_code_or_None).

    exit_code_or_None is non-None (and namespace_dict is None) when parsing
    itself terminates the run (--help, missing value, unknown flag, invalid
    --governing-plan-slug). Caller returns that code verbatim.
    """
    sid = ""
    disposition = ""
    consumed_handoff = ""
    governing_plan_slug = ""
    nature_val = ""
    for_date: Optional[date] = None
    commits: List[str] = []
    commits_source = ""
    authored_by_unknown = False

    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--sid":
            if i + 1 >= n or not argv[i + 1]:
                print("ERROR: --sid requires a value", file=sys.stderr)
                return None, 1
            val = argv[i + 1]
            if not _SID_RE.match(val) or ".." in val:
                print(f"ERROR: --sid contains invalid characters: '{val}'", file=sys.stderr)
                return None, 1
            sid = val
            i += 2
            continue
        if a == "--disposition":
            if i + 1 >= n or not argv[i + 1]:
                print("ERROR: --disposition requires a value", file=sys.stderr)
                return None, 1
            disposition = argv[i + 1]
            i += 2
            continue
        if a == "--consumed-handoff":
            if i + 1 >= n or not argv[i + 1]:
                print("ERROR: --consumed-handoff requires a value", file=sys.stderr)
                return None, 1
            consumed_handoff = argv[i + 1]
            i += 2
            continue
        if a == "--governing-plan-slug":
            if i + 1 >= n or not argv[i + 1]:
                print("ERROR: --governing-plan-slug requires a value", file=sys.stderr)
                return None, 1
            val = argv[i + 1]
            if not _GOVERNING_PLAN_SLUG_RE.match(val) or ".." in val:
                print(f"ERROR: --governing-plan-slug contains invalid characters: '{val}'", file=sys.stderr)
                return None, 1
            governing_plan_slug = val
            i += 2
            continue
        if a == "--nature":
            if i + 1 >= n or not argv[i + 1]:
                print("ERROR: --nature requires a value", file=sys.stderr)
                return None, 1
            nature_val = argv[i + 1]
            i += 2
            continue
        if a == "--for-date":
            if i + 1 >= n or not argv[i + 1]:
                print("ERROR: --for-date requires a value", file=sys.stderr)
                return None, 1
            try:
                for_date = date.fromisoformat(argv[i + 1])
            except ValueError:
                print(
                    f"ERROR: --for-date must be YYYY-MM-DD; got: '{argv[i + 1]}'",
                    file=sys.stderr,
                )
                return None, 1
            i += 2
            continue
        if a == "--commits":
            if commits_source:
                # Review: coordinatorcode-reviewer.a2ea175d92501b498 -- the
                # --claim-shas-from removal also dropped the only guard
                # against a repeated --commits, letting a second occurrence
                # silently merge without de-duplicating across invocations,
                # which contradicts this flag's own "de-duplicated" promise.
                # Refuse the repeat rather than merge it.
                print(f"ERROR: {a} may only be given once", file=sys.stderr)
                return None, 1
            if i + 1 >= n or not argv[i + 1]:
                print(f"ERROR: {a} requires a value", file=sys.stderr)
                return None, 1
            raw_shas = argv[i + 1].split(",")
            seen: set[str] = set()
            for token in raw_shas:
                token = token.strip()
                if not token:
                    continue
                if not _SHA_RE.match(token):
                    print(
                        f"ERROR: {a}: '{token}' is not a hex sha (7-40 chars)",
                        file=sys.stderr,
                    )
                    return None, 1
                # De-duplicated but ORDER-PRESERVING: the memo's own hand-built
                # entries are read by the coverage sweep as a flat membership
                # set, so order carries no meaning to the consumer -- but it
                # carries meaning to a HUMAN diffing a reconstructed entry
                # against `git log`, and sorting would destroy that for free.
                if token not in seen:
                    seen.add(token)
                    commits.append(token)
            commits_source = a
            i += 2
            continue
        if a == "--authored-by-unknown":
            authored_by_unknown = True
            i += 1
            continue
        if a in ("--help", "-h"):
            print(_USAGE)
            return None, 0
        print(f"ERROR: unknown option: {a}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return None, 1

    if not sid:
        print("ERROR: --sid is required", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return None, 1
    if not disposition:
        print("ERROR: --disposition is required", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return None, 1
    if disposition not in VALID:
        print(
            f"ERROR: --disposition must be one of {sorted(VALID)}; got: '{disposition}'",
            file=sys.stderr,
        )
        return None, 1
    if canonicalize(disposition) == PREDECESSOR_CONSUMED and not consumed_handoff:
        print(
            "ERROR: --consumed-handoff is required when --disposition is predecessor-consumed "
            "(or the legacy chain-terminal)",
            file=sys.stderr,
        )
        return None, 1
    if nature_val and nature_val not in _VALID_NATURES:
        print(f"ERROR: --nature '{nature_val}' is not a valid completion nature.", file=sys.stderr)
        print("Valid values: roadmap | bugfix | tech-debt | infra", file=sys.stderr)
        return None, 1
    # The three backfill flags are gated on --for-date, and the gate is the
    # point rather than tidiness. `commits:` is otherwise owned exclusively by
    # the reconcile step (see this module's own Negative-spec), and
    # `authored_by` is what `_refuse_if_live_foreign_entry_holder` keys on --
    # so seeding either on a LIVE close would let a session hand-write fields
    # two guards depend on. Reconstructing a dead session's record is the one
    # case where nothing else can supply them, and --for-date is what declares
    # that case.
    if commits_source and for_date is None:
        print(
            f"ERROR: {commits_source} requires --for-date (backfill mode) — on a live "
            "close, commits: is seeded by the reconcile step, not by hand",
            file=sys.stderr,
        )
        return None, 1
    if authored_by_unknown and for_date is None:
        print(
            "ERROR: --authored-by-unknown requires --for-date (backfill mode)",
            file=sys.stderr,
        )
        return None, 1
    if for_date is not None and for_date > date.today():
        print(
            f"ERROR: --for-date is in the future: {for_date.isoformat()}",
            file=sys.stderr,
        )
        return None, 1

    return (
        {
            "sid": sid,
            "disposition": disposition,
            "consumed_handoff": consumed_handoff,
            "governing_plan_slug": governing_plan_slug,
            "nature_val": nature_val,
            "for_date": for_date,
            "commits": commits,
            "authored_by_unknown": authored_by_unknown,
        },
        None,
    )


def _migrate_legacy_monolith(repo_root: str, completed_dir: str, yyyymm: str) -> None:
    """Step 2.6.2 — idempotent monolith migrate (in-process, direct-import)."""
    if os.environ.get("COORDINATOR_OVERRIDE_LEGACY_MONOLITH"):
        return
    monolith = os.path.join(completed_dir, f"{yyyymm}.md")
    if not os.path.isfile(monolith):
        return
    _migrate_mod.main(["--root", repo_root])


def _query_records_existing_path(repo_root: str, chain_slug: str) -> Optional[str]:
    """Primary existence signal — native records-query (best-effort, fail-open).

    Was: a ``node query-records.js --type completion --where chain=<slug>``
    subprocess spawned against DoE-claude's ``coordinator/bin/`` (resolved via
    ``coordinator_doe_root``). Repointed 2026-07-22 onto claude-klabauter's own
    ``ceremony.records_query.query_records`` seam — no DoE coupling, no node.

    Reproduces the oracle's markdown-list ``completion`` rendering
    (query-records.js:314's ``TYPE_DISPLAY.completion`` — ``- **title**
    [nature] (chain: ...) — commit,commit``, joined one-per-line the same
    way ``formatRecords``'s markdown-list branch does at
    query-records.js:1601-1636) so the returned string carries the same
    stdout-content contract the caller previously got from the node
    subprocess's stdout, not merely the same truthiness.

    Degrades to ``None`` on zero matches, an unknown-type ``ValueError``, or
    an unparseable-``where`` ``SystemExit`` — the same
    absence-or-failure-degrades-silently posture the node spawn had (its own
    ``[[ -x ... ]]`` existence gate plus ``|| _qr_rc=$?`` discard-and-
    continue). The caller (``_idempotency_guard``) falls through to the
    grep-scan on ``None``, same as before.
    """
    try:
        records = query_records("completion", Path(repo_root), where=f"chain={chain_slug}")
    except (ValueError, SystemExit):
        print(f"skip: _query_records_existing_path: query_records(...) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if not records:
        return None
    lines = []
    for rec in records:
        fm = rec.get("frontmatter") or {}
        title = fm.get("title")
        nature = fm.get("nature")
        chain = fm.get("chain") or "none"
        commits = fm.get("commits")
        commits_str = ", ".join(commits) if commits else "no-commit"
        lines.append(f"- **{title}** [{nature}] (chain: {chain}) — {commits_str}")
    out = "\n".join(lines).strip()
    return out or None


def _grep_scan_existing_path(completed_dir: str, chain_slug: str) -> Optional[str]:
    """Step 2.6.3a — BSD-portable literal-match scan (maxdepth 2, excludes legacy/)."""
    if not os.path.isdir(completed_dir):
        return None
    needle = f'chain: "{chain_slug}"'
    # maxdepth 2 equivalent: completed_dir/*.md and completed_dir/*/*.md
    candidates: List[str] = []
    try:
        for name in sorted(os.listdir(completed_dir)):
            full = os.path.join(completed_dir, name)
            if os.path.isfile(full) and name.endswith(".md"):
                candidates.append(full)
            elif os.path.isdir(full) and name != "legacy":
                try:
                    for sub in sorted(os.listdir(full)):
                        subfull = os.path.join(full, sub)
                        if os.path.isfile(subfull) and sub.endswith(".md"):
                            candidates.append(subfull)
                except OSError:
                    print(f"skip: _grep_scan_existing_path: for sub in sorted(os.listdir(full)): failed: {sys.exc_info()[1]}", file=sys.stderr)
                    continue
    except OSError:
        print(f"skip: _grep_scan_existing_path: for name in sorted(os.listdir(completed_dir)): failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    for f in candidates:
        if "/legacy/" in f.replace(os.sep, "/"):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            print(f"skip: _grep_scan_existing_path: with open(f, \"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        if needle in text:
            return f
    return None


def _idempotency_guard(repo_root: str, completed_dir: str, chain_slug: str):
    """Step 2.6.3a. Returns an existing entry path (str) to stand down on, or None to proceed."""
    if not chain_slug or not os.path.isdir(completed_dir):
        return None, None

    existing_path = _grep_scan_existing_path(completed_dir, chain_slug)
    qr_out = _query_records_existing_path(repo_root, chain_slug)
    found_existing = bool(existing_path) or bool(qr_out)

    if not found_existing:
        return None, None

    if not existing_path and qr_out and os.path.isfile(qr_out):
        existing_path = qr_out

    if not existing_path:
        return "UNRECOVERABLE", None

    return existing_path, "stand-down"


def _count_session_dispatches(agents_file: Path):
    """Port of ``count_session()``'s core counting logic (bash
    coordinator-session-loe.sh, DoE b644d5a9, 2026-07-22).

    Returns ``(agent_dispatches, opus_dispatches)`` as ``(Optional[int],
    Optional[int])`` — ``(None, None)`` when *agents_file* does not exist,
    mirroring the bash function's ``ad=""``/``od=""`` UNSET state (distinct
    from a present-but-zero count: an empty-but-existing file yields
    ``(0, 0)``, matching ``wc -l`` / ``grep -c`` on an empty stream).

    ``agent_dispatches`` = newline count (``wc -l`` parity — a final line
    without a trailing newline is NOT counted, matching ``wc -l`` exactly).
    ``opus_dispatches`` = count of lines whose tab-delimited 2nd field
    (``cut -f2``; the whole line when no tab is present, mirroring `cut`'s
    no-delimiter passthrough) contains ``opus`` as a case-insensitive
    substring (``grep -ci opus`` parity, not an anchored/whole-field match).
    """
    if not agents_file.is_file():
        return None, None
    try:
        text = agents_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"skip: _count_session_dispatches: text = agents_file.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None, None

    ad = text.count("\n")

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]  # drop the trailing empty split past a final \n
    od = 0
    for line in lines:
        field2 = line.split("\t", 1)[1] if "\t" in line else line
        if "opus" in field2.lower():
            od += 1

    return ad, od


def _native_single_session_loe() -> str:
    """Step 2.6.5a single-session LoE — native port of
    ``coordinator-session-loe.sh --format yaml-frontmatter`` (no
    ``--session-id``, no ``--include-children``: the only invocation shape
    this module ever used the bash oracle with).

    Resolves the session id via the already-ported 4-tier
    ``coordinator_core.session.core.resolve_session_id`` (byte-parity port
    of the oracle's own ``cs_resolve_session_id``), reads
    ``<sessions_dir>/<sid>/dispatched-agents.txt`` directly, and computes
    the t-shirt tier with the same any-criterion, highest-tier-first table
    ``loe_thresholds.DEFAULT_THRESHOLDS`` encodes (identical values to the
    oracle's hardcoded ``TSHIRT_TABLE``).

    Returns the ``loe:`` YAML block (no trailing newline — mirrors the old
    bridge's ``.strip()``ed return), or ``""`` on the oracle's sole
    hard-failure path (session id unresolvable) — the caller degrades to
    ``_NULL_LOE_BLOCK`` on an empty return, exactly as it did for the bash
    bridge's failure case.

    Negative-spec (mirrors the bash oracle, preserve exactly): an unresolved
    ``ad``/``od`` (dispatched-agents.txt absent) renders ``null``, NOT ``0``
    — a present-but-empty file legitimately renders ``0``. ``tshirt`` stays
    the literal string ``"null"`` (quoted, matching the oracle's
    ``TSHIRT="null"`` default) only when NEITHER dispatch count NOR
    ``em_tokens`` is present at all — the XS tier's all-zero thresholds mean
    ANY present (even zero-valued) dispatch count always qualifies for at
    least XS, exactly as the oracle's ``(( AGENT_DISPATCHES >= 0 ))`` always
    fires once the variable is merely set.
    """
    sid = _session_core.resolve_session_id()
    if not sid:
        return ""

    base = _session_core.sessions_dir()
    if not base:
        return ""

    agents_file = Path(base) / sid / "dispatched-agents.txt"
    ad, od = _count_session_dispatches(agents_file)

    em_tokens: Optional[int] = None
    in_tok = os.environ.get("CLAUDE_SESSION_INPUT_TOKENS", "")
    out_tok = os.environ.get("CLAUDE_SESSION_OUTPUT_TOKENS", "")
    if in_tok.isdigit() and out_tok.isdigit():
        em_tokens = int(in_tok) + int(out_tok)

    # Review: code-reviewer — share loe_thresholds' table walk instead of a
    # hand-rolled duplicate (the exact drift class that module centralizes
    # against).
    tshirt = _compute_tshirt_nullable(ad, od, em_tokens)

    ad_out = str(ad) if ad is not None else "null"
    od_out = str(od) if od is not None else "null"
    tok_out = str(em_tokens) if em_tokens is not None else "null"
    # `_compute_tshirt_nullable` returns the LITERAL STRING "null" as its
    # all-three-unresolved sentinel. Emitting that quoted yields YAML string
    # "null", which the completion-entry schema rejects (XS|S|M|L|XL or null) —
    # the three siblings above already emit the sentinel bare, and so does the
    # other scaffold path (`ops/ceremony/completion_entry.py`). Quote real tiers
    # only.
    tshirt_out = "null" if tshirt == "null" else f'"{tshirt}"'

    return (
        "loe:\n"
        f"  agent_dispatches: {ad_out}\n"
        f"  opus_dispatches: {od_out}\n"
        f"  em_tokens: {tok_out}\n"
        f"  tshirt: {tshirt_out}"
    )


def _closing_session_argv() -> List[str]:
    """`--closing-*` flags naming THIS session to the chain aggregator.

    The closing session heads no handoff, and its Session Ledger row is a
    genuine-EM action with no directive — it lands after this scaffold runs.
    Without these flags the aggregate omits the closing session entirely and
    its `N of M` tell reads all-clear (`"1 of 1"` on a single-handoff chain)
    in precisely the case where the undercount is total.

    Degrades to `[]` on an unresolvable session id — the aggregate then falls
    back to its handoff-based counts rather than failing the scaffold.
    """
    sid = _session_core.resolve_session_id()
    if not sid:
        return []

    argv = ["--closing-session-id", sid]

    base = _session_core.sessions_dir()
    if base:
        ad, od = _count_session_dispatches(Path(base) / sid / "dispatched-agents.txt")
        if ad is not None:
            argv += ["--closing-agent-dispatches", str(ad)]
        if od is not None:
            argv += ["--closing-opus-dispatches", str(od)]

    in_tok = os.environ.get("CLAUDE_SESSION_INPUT_TOKENS", "")
    out_tok = os.environ.get("CLAUDE_SESSION_OUTPUT_TOKENS", "")
    if in_tok.isdigit() and out_tok.isdigit():
        argv += ["--closing-em-tokens", str(int(in_tok) + int(out_tok))]

    return argv


def _chain_terminal_loe(consumed_handoff: str) -> str:
    """Step 2.6.5a chain-terminal LoE — in-process call to the ported aggregator."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = _agg_loe_mod.main(
                ["--terminal-handoff", consumed_handoff, "--format", "yaml-frontmatter"]
                + _closing_session_argv()
            )
    except Exception:
        print(f"skip: _chain_terminal_loe: with redirect_stdout(buf): failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    if rc != 0:
        return ""
    return buf.getvalue().strip()


def _resolve_loe_block(disposition: str, consumed_handoff: str) -> str:
    """Step 2.6.5a LoE dispatch.

    `memo-predecessor` routes to the SAME native single-session tally as
    `single-session`, not to `_chain_terminal_loe` — the memo leg carries no
    consumed handoff to aggregate a chain LoE from (`consumed_handoff` is
    contractually "" on that leg, docs/plans/2026-08-05-memo-predecessor-
    representable-outcome.md § Fix-locus discrimination), so there is no
    predecessor chain this session's own LoE could be summed across. Falling
    through to the bare-else `""` (rendering `_NULL_LOE_BLOCK`) would silently
    under-report a real, measurable session on the one disposition where a
    genuine tally is always available.
    """
    canonical = canonicalize(disposition)
    if canonical in (SINGLE_SESSION, MEMO_PREDECESSOR):
        loe = _native_single_session_loe()
    elif canonical == PREDECESSOR_CONSUMED and consumed_handoff:
        loe = _chain_terminal_loe(consumed_handoff)
    else:
        loe = ""
    return loe if loe else _NULL_LOE_BLOCK


def _which_render_rollup_shim() -> str:
    """PATH lookup for the ``coordinator-render-rollup.sh`` test shim.

    `shutil.which("coordinator-render-rollup.sh")` never finds it on
    Windows: CPython's `shutil.which` only probes `cmd + ext` for each
    `PATHEXT` entry when `cmd` itself doesn't already end in one of those
    extensions, and `.sh` is not a `PATHEXT` member — so it silently builds
    candidates like `coordinator-render-rollup.sh.COM`,
    `coordinator-render-rollup.sh.EXE`, ... none of which exist, and never
    even tries the literal `coordinator-render-rollup.sh` filename. That
    made the PATH-first shim rung this function exists to preserve (see
    module docstring) permanently unreachable on Windows — a shim any test
    prepends to PATH is silently never found, and every call falls through
    to the in-process branch regardless of what a test set up.

    Delegates to the shared `coordinator_core.launchable.which_path_ordered`
    walk with `extensions=[]` (bare-name-only — the filename already carries
    its own `.sh` extension, so no `PATHEXT` candidate should ever be
    appended to it). Same underlying CPython gap `_resolve_claude_bin` in
    `coordinator/bin/claude-doe.py` guards against; that site can't import this
    module (installed standalone) and keeps its own PATHEXT-aware walk
    in sync by hand — see its docstring.
    """
    return which_path_ordered("coordinator-render-rollup.sh", extensions=[]) or ""


def _resolve_governing_deliverable_id(repo_root: str, governing_plan_slug: str) -> str:
    """Reads the governing plan file's own `deliverable_id:` frontmatter line.

    sedge-18 (`state/handoffs/2026-08-06_170018_roadmap-sedge-18.md`) —
    Contested-block resolution: this is the SAME plan-file read
    `_resolve_rollup_sentence` already performs for its own (unrelated)
    rollup-sentence purpose, factored out here so both callers share one
    PARSING implementation rather than each hand-rolling the
    `deliverable_id:` line-scan logic. This dedupes the LOGIC, not the file
    READ: `main()` calls this function and `_resolve_rollup_sentence`
    separately per invocation, and `_resolve_rollup_sentence` calls this
    same helper again internally — the plan file is still opened and parsed
    twice per run, exactly as before the extraction. A follow-up that
    threads a resolved `dlv_id` into `_resolve_rollup_sentence` as a
    parameter would close that gap; not done here. Fail-open at every
    stage, mirroring `_resolve_rollup_sentence`: any guard miss (no
    `governing_plan_slug`, no plan file, no `deliverable_id:` line) returns
    `""`, never raises.
    """
    if not governing_plan_slug:
        return ""
    plan_file = os.path.join(repo_root, "docs", "plans", f"{governing_plan_slug}.md")
    if not os.path.isfile(plan_file):
        return ""
    try:
        with open(plan_file, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        print(f"skip: _resolve_governing_deliverable_id: with open(plan_file, \"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""

    dlv_id = ""
    for line in text.splitlines():
        if line.startswith("deliverable_id:"):
            raw = line[len("deliverable_id:") :].strip()
            if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
                raw = raw[1:-1]
            elif raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
                raw = raw[1:-1]
            dlv_id = raw
            break
    return dlv_id


def _resolve_rollup_sentence(repo_root: str, governing_plan_slug: str) -> str:
    """AC4 — fail-open at every stage; never raises, never aborts the write."""
    if not governing_plan_slug:
        return ""
    plan_file = os.path.join(repo_root, "docs", "plans", f"{governing_plan_slug}.md")
    if not os.path.isfile(plan_file):
        return ""

    dlv_id = _resolve_governing_deliverable_id(repo_root, governing_plan_slug)
    if not dlv_id:
        return ""

    # PATH-first-shim preservation: the bash oracle resolved the render helper
    # PATH-first (`command -v coordinator-render-rollup.sh`), THEN sibling —
    # explicitly "enables test shims" per coordinator_render_rollup.py's own
    # module docstring. An always-in-process call would silently drop that
    # testability path (a real scope-drop per the porter-brief addendum rule
    # 7, caught by DoE's own test-complete-entry-rollup.sh Test A, which PATH-
    # prepends a stub `coordinator-render-rollup.sh` and expects it honored).
    # Preserve it: PATH-first subprocess shim wins when present (mirrors the
    # oracle's own `command -v` resolution + `bash "$_RENDER_HELPER" ...`
    # invocation exactly); otherwise fall through to the faster in-process
    # call to the already-ported Python module (the production path, where no
    # such shim exists on PATH).
    shim = _which_render_rollup_shim()
    if shim:
        # Review: code-reviewer — bare shebang exec was a Windows regression
        # (the retired oracle explicitly named `bash`; a bare-path exec has no
        # shebang mechanism on Windows and silently degrades to an empty
        # rollup). Resolve the interpreter explicitly via the shared
        # `resolve_launchable` seam instead of relying on shebang resolution.
        try:
            result = subprocess.run(
                [*resolve_launchable(shim), dlv_id, repo_root],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECS,
                stdin=subprocess.DEVNULL,
                **_CREATIONFLAGS,
            )
        except (OSError, subprocess.TimeoutExpired):
            print(f"skip: _resolve_rollup_sentence: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
            return ""
        return (result.stdout or "").strip()

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            _render_rollup_mod.main([dlv_id, repo_root])
    except Exception:
        print(f"skip: _resolve_rollup_sentence: with redirect_stdout(buf): failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    return buf.getvalue().strip()


def _write_entry(
    entry_path: str,
    sid: str,
    nature_val: str,
    chain_slug: str,
    chain_terminal: bool,
    loe_block: str,
    rollup_sentence: str,
    yyyymmdd: str,
    deliverable_id: str,
    commits: Optional[List[str]] = None,
    authored_by_unknown: bool = False,
) -> bool:
    """Writes (or idempotent-preserving re-writes) the completion-entry
    scaffold at `entry_path`. Returns `True` when the file was written,
    `False` when a pre-existing entry was left untouched because ALL three
    EM-authored surfaces (title/nature/prose) were already authored —
    (state/bug-backlog/2026-07-28-workstream-complete-apply-re-scaffolds-t-
    e925d597e0af.yaml: a re-run of this scaffolder used to silently revert
    hand-written title/nature/prose back to placeholders).

    Per-field preservation, not all-or-nothing: an entry authored on only
    SOME of the three surfaces keeps each authored surface's exact
    existing text verbatim and only (re)writes the surfaces still carrying
    a placeholder — robust to an EM who filled in, say, the title and
    prose but hasn't yet resolved `nature`. Purely mechanical/computed
    fields this CLI itself owns (`created` once set, `commits` — always
    `[]`, `status`, `chain_terminal`, `authored_by`, `chain`, `deliverable_id`,
    the `loe:` block, the rollup sentence) are recomputed fresh on every call
    regardless of authoring state, exactly as before this fix — only the
    three EM-owned surfaces are sacred. `created` is the one exception:
    once an entry exists, its original `created` date is preserved rather
    than bumped to today, since re-running this scaffolder across
    midnight must not misdate an already-in-progress entry.

    `deliverable_id` (sedge-18) — the spine's exit join key, stamped
    ceremony-internal (ninth parameter) rather than by a later cascade
    pass, per the roadmap's `## Spine reach: the exit` finding that
    cascade-written forward edges populate in the single digits regardless
    of schema strictness while ceremony-internal writers (`continued_into`,
    `closed_reason`) reach 100%. `""` renders as an explicit `null`, never
    an omitted line — this stub covers population only, not backfill of
    pre-existing zero-population entries.
    """
    existing = _read_existing_scaffold_state(entry_path)

    if existing.exists and existing.title_authored and existing.nature_authored and existing.prose_authored:
        return False

    lines: List[str] = []
    lines.append("---")
    if existing.title_authored:
        _title_esc = str(existing.title).replace('"', '\\"')
        lines.append(f'title: "{_title_esc}"')
    else:
        lines.append(f'title: "{_PLACEHOLDER_TITLE}"')
    lines.append(f"created: {existing.created if existing.exists and existing.created else yyyymmdd}")
    if existing.nature_authored:
        lines.append(f"nature: {existing.nature}")
        lines.append("nature_inferred: false")
    elif nature_val:
        lines.append(f"nature: {nature_val}")
        lines.append("nature_inferred: false")
    else:
        lines.append("nature: null")
        lines.append("nature_inferred: true")
    if chain_slug:
        # Review: code-reviewer — escape embedded double-quotes before
        # splicing into a double-quoted YAML scalar (nit F4).
        _chain_slug_esc = chain_slug.replace('"', '\\"')
        lines.append(f'chain: "{_chain_slug_esc}"')
    if deliverable_id:
        _dlv_id_esc = deliverable_id.replace('"', '\\"')
        lines.append(f'deliverable_id: "{_dlv_id_esc}"')
    else:
        lines.append("deliverable_id: null")
    if commits:
        # The ONLY path that seeds this field, and it exists because the
        # session whose commits these are is gone: `reconcile-completion-
        # commits` resolves its session-id from the CURRENT live session, so
        # composed with a hardcoded `date.today()` there was no supported way
        # to author a dead session's record at all (example-cockpit-repo-em,
        # cross-repo/archive/2026-07-30-example-cockpit-repo-em-completion-entry-
        # backfill-mode-and-obligation-gap.md § 2 — /workday-complete Step 9
        # instructed an action its own toolchain could not perform, and they
        # covered 231 orphaned commits by hand-writing frontmatter instead,
        # which is how schema drift starts). Gated on --for-date; every live
        # close still writes `commits: []` and leaves the field to reconcile.
        lines.append("commits:")
        for sha in commits:
            lines.append(f'  - "{sha}"')
    else:
        lines.append("commits: []")
    lines.append("status: pending-release")
    lines.append(f"chain_terminal: {'true' if chain_terminal else 'false'}")
    if authored_by_unknown:
        # OMITTED, not `null`, and the difference is schema-enforced rather
        # than stylistic: `completion-entry.schema.json` (1.4.0) types
        # `authored_by` as `string` and does NOT list it in `required`, so an
        # absent key is valid and `authored_by: null` fails validation
        # ("expected string, got null" -- measured, 2026-08-31). The requester
        # asked for present-as-null (their D9), but their REASON was that a
        # fabricated session UUID pollutes the coverage sweep's
        # `known_session_ids` set and makes a reconstructed entry
        # indistinguishable from a real session's. Omission serves that reason
        # exactly, costs a schema bump nobody needs, and is indistinguishable
        # to every consumer: `_refuse_if_live_foreign_entry_holder` does
        # `if not authored_by`, which treats absent and null identically.
        pass
    else:
        _sid_esc = sid.replace('"', '\\"')
        lines.append(f'authored_by: "{_sid_esc}"')
    lines.append(loe_block)
    lines.append("---")
    lines.append("")
    if existing.prose_authored:
        lines.append(existing.body.rstrip("\n"))
    else:
        lines.append(_PROSE_PLACEHOLDER_MARKER)
        if rollup_sentence:
            lines.append("")
            lines.append(rollup_sentence)
    if not existing.nature_authored and not nature_val:
        lines.append("")
        lines.append("<!-- NATURE-INFER: Set the nature: field above (roadmap | bugfix | tech-debt | infra) and remove this comment. -->")

    with open(entry_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(entry_path)
    return True


def main(argv: List[str]) -> int:
    """CLI entry point — mirrors the bash oracle's parse/validate/write pipeline."""
    parsed, early_exit = _parse_args(argv)
    if early_exit is not None:
        return early_exit
    assert parsed is not None

    sid = parsed["sid"]
    disposition = parsed["disposition"]
    consumed_handoff = parsed["consumed_handoff"]
    governing_plan_slug = parsed["governing_plan_slug"]
    nature_val = parsed["nature_val"]
    for_date = parsed["for_date"]
    seeded_commits = parsed["commits"]
    authored_by_unknown = parsed["authored_by_unknown"]

    repo_root = git_root()
    if not repo_root:
        print("ERROR: not inside a git repository. Run from within the target repo.", file=sys.stderr)
        return 2

    # git_root() shells out to `git rev-parse --show-toplevel`, which always
    # answers forward-slash-normalized even on Windows. Every downstream
    # os.path.join() in this module joins with os.sep, so leaving repo_root
    # forward-slash produced a mixed-separator absolute path
    # ("C:/Users/...\archive\completed\...") once printed or handed to
    # --root. Normalize once, here, to the platform-native form so the rest
    # of this function's os.path.join calls stay internally consistent
    # (C5 root-cause: os.sep-in-wire-id class — this printed path is a local
    # filesystem absolute path, not a repo-relative wire id, so the correct
    # normalization target is the platform's own separator, not posix).
    repo_root = str(Path(repo_root))

    completed_dir = os.path.join(repo_root, "archive", "completed")
    # ONE date for the whole run -- the entry's directory, its filename and its
    # `created:` all derive from this single value, so a backfill cannot land a
    # 2026-07-28 entry under a 2026-08 directory.
    today = for_date or date.today()
    yyyymm = today.strftime("%Y-%m")
    yyyymmdd = today.strftime("%Y-%m-%d")

    _migrate_legacy_monolith(repo_root, completed_dir, yyyymm)

    chain_slug = governing_plan_slug

    entry_path, stand_down_marker = resolve_effective_entry_path(
        repo_root, sid, chain_slug, for_date
    )
    if stand_down_marker == "UNRECOVERABLE":
        print(
            f"ERROR: existing entry detected for chain '{chain_slug}' but path unrecoverable",
            file=sys.stderr,
        )
        return 1
    if stand_down_marker == "FOREIGN-LIVE":
        # Deliberately NOT printed to stdout — printing the foreign path is
        # exactly what would hand it downstream to
        # d-reconcile-completion-commits via {d-complete-entry.entry_path}
        # (see resolve_effective_entry_path's own docstring on this marker).
        print(
            f"ERROR: existing entry for chain '{chain_slug}' is owned by a "
            "different LIVE session — refusing to stand down onto a foreign "
            "in-flight completion entry",
            file=sys.stderr,
        )
        return 1
    if stand_down_marker:
        print(entry_path)
        print(
            f"stand-down: completion entry already exists for chain '{chain_slug}' — no duplicate written",
            file=sys.stderr,
        )
        return 0

    loe_block = _resolve_loe_block(disposition, consumed_handoff)

    os.makedirs(os.path.dirname(entry_path), exist_ok=True)

    chain_terminal = canonicalize(disposition) == PREDECESSOR_CONSUMED

    rollup_sentence = _resolve_rollup_sentence(repo_root, governing_plan_slug)
    deliverable_id = _resolve_governing_deliverable_id(repo_root, governing_plan_slug)

    wrote = _write_entry(
        entry_path,
        sid,
        nature_val,
        chain_slug,
        chain_terminal,
        loe_block,
        rollup_sentence,
        yyyymmdd,
        deliverable_id,
        seeded_commits,
        authored_by_unknown,
    )

    print(entry_path)
    if wrote:
        # Title residue is never named here — matches this CLI's pre-
        # existing stderr contract, which only ever flagged nature/prose
        # (title has no dedicated placeholder-removal marker an EM is
        # instructed to hunt for the way NATURE-INFER/PROSE are).
        residue = scaffold_residue_fields(entry_path)
        markers = {
            "nature": "nature (<!-- NATURE-INFER -->)",
            "prose": "prose (<!-- PROSE: ... -->)",
        }
        residue_named = [markers[field] for field in residue if field in markers]
        if residue_named:
            print(f"Residue: {', '.join(residue_named)}", file=sys.stderr)
    else:
        print(
            f"stand-down: completion entry at '{entry_path}' is already fully authored "
            "(title/nature/prose) — not overwritten",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
