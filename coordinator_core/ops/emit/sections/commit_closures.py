"""Section porter — commit closures (envelope key: ``commit_closures``).

Emits one ``CommitClosure`` record per ``(sha, item_id)`` pair recovered from the
**commit ledger** (``coordinator_core.commit_ledger.store``) — the deterministic "did a
landed commit close this work item" fact cockpit's ``recs-05``/B4 code-complete auto-assert
needs (source memo: 2026-07-17-example-cockpit-repo-em-wsc-commit-closure-emit.md).

**C2 supersedes the git-log scan this porter used to run (DECISION-1/DECISION-3, C1's
commit-closure-pipe-carries-rows plan).** The closure fact (``closes``) and the revert
linkage (``reverts_sha``) are now stamped at COMMIT time — by
``coordinator_core.git.commit_trailers.extract_closure_facts_from_text`` off the commit
message's own raw text, appended to the ledger by ``contract/apply_base.py`` alongside the
existing per-commit entry (C1) — and this porter's ``collect(ctx)`` does nothing but READ
those already-recorded facts back. There is no ``git log``, no trailer-block parsing, no
``--grep`` pre-filter, and no scan horizon here any more: the record-at-write-time doctrine
(``docs/wiki/record-at-write-time.md``) states plainly that a corpus walk reconstructing a
fact already known at write time is "what gets done instead of choosing one of them" — this
module is the "choosing one of them" arm.

**Ledger read (whole-corpus, one caller, zero subprocesses).** ``coordinator_core.commit_
ledger.store.read_entries`` is keyed per ``handoff_id`` (one ``.jsonl`` file per baton), so a
whole-corpus read globs ``store.ledger_dir(...)`` for every ``*.jsonl`` file and calls
``read_entries`` once per file, keyed on the file's own stem — reusing that function's
existing malformed-line-skipping and per-sha dedup semantics rather than hand-rolling a
second JSONL parser over the same on-disk shape. Measured **6.7ms over 128 files / 1,019
entries** on this repo. This is pure local file I/O: no subprocess, so AC6's "zero
history-scanning git subprocesses" is trivially true for this leg — the only subprocess
``collect()`` still spawns is the ONE reachability check below.

Each ledger entry already carries ``closes`` (the item_ids the commit's own message closes,
normalized via ``ops.emit.closure_trailer.parse_closure_trailers`` AT WRITE TIME — this
porter never re-normalizes a trailer value) and, optionally, ``reverts_sha`` (the sha of the
commit this one's message names as reverted, via git's own auto-generated body linkage). One
CLOSE record is emitted per ``(sha, item_id)`` pair in every entry's ``closes`` list
(DECISION-4 — no cross-commit dedup at write time: a re-close, cherry-pick, or trailer
copy-paste landing the same item_id in two commits emits two distinct rows, since each is a
genuine distinct fact). One REVERT record is emitted per entry whose ``reverts_sha`` names a
sha that some OTHER ledger entry's ``closes`` list also names — the join is over ALL ledger
entries gathered above (a revert commit and the commit it reverts are not necessarily
recorded under the same handoff's ledger file), never a second read pass.

``reverts_sha`` (see ``CommitClosure``) is the sole revert/close distinguishing marker AND,
per DR-318 §D8/G5, the transitive-binding fact itself: its non-null value names the commit
that WAS ``closes``-bound to ``item_id`` at write time, and this row's own ``sha`` is the
commit whose OWN recorded ``reverts_sha`` names it. A revert entry whose named sha matches no
other entry's ``closes`` list produces nothing (D4's measured hand-authored-revert coverage
limit carries forward unchanged: a revert with no git-generated body linkage line was never
recorded with a ``reverts_sha`` at write time, so it simply never joins — "not a revert",
never an error, AC16/AC17).

Malformed rows: a ledger entry missing ``sha`` is already dropped by ``read_entries`` itself
(see that function's own malformed-line-skipping contract) and never reaches this module. A
``sha`` that IS present but fails this porter's own defensive 40-lowercase-hex-char shape
check (matching ``CommitClosure.sha``'s own pydantic pattern) is quarantined into the
``malformed`` bucket rather than emitted as a record with a corrupt identity key — the same
posture the git-log-scan predecessor held, now applied to a ledger-sourced sha instead of a
``%H``-sourced one.

**§ Why reachability costs a spawn.** ``reachable_on_default_branch`` is resolved HERE, in
``collect()``, in exactly ONE ``git rev-list origin/main`` subprocess (measured **82ms** on
this repo) — never frozen at commit time (a sha becomes default-branch-reachable only
post-merge, so freezing it at write time would go stale the moment the commit lands) and
never cached across calls (the whole point of resolving fresh: a cache keyed on sha would, by
construction, serve a stale ``False``/``None`` for a sha that has since merged, since nothing
on this path invalidates it on merge — the failure direction is silent, permanent
under-crediting of a genuinely-closed item, not a crash or a stale-but-safe ``True``). A
bounded corpus-walk-based cache was the rejected alternative (the plan's own discarded second
draft, per ``docs/wiki/record-at-write-time.md``'s citation) — rejected for the same reason
the git-log scan itself was retired: a reader-side index over a fact the writer already knows
just moves the staleness problem rather than removing it. Re-resolving on every query is
cheap enough (one spawn, one shared budget) that there is no dial worth building.

Historically this repo also had an ``envelope._stamp_closure_reachability`` (measured 137ms,
THREE spawns: one ``rev-list`` plus a ``cat-file --batch-check`` validity leg); do not
reimplement its ``cat-file`` leg here even in spirit. That leg existed to distinguish "not an
ancestor" from "not a valid object" for a caller that could not otherwise tell the two apart —
but every sha this porter's ``collect()`` handles came from a commit **we ourselves recorded**
in the commit ledger at the moment it was created (``contract/apply_base.py``'s post-commit
append, with the sha in hand from the commit that just happened), so it is a valid commit BY
CONSTRUCTION; the validity question ``cat-file`` answered is already closed before this module
ever sees the sha, and paying for it again would be answering a question this porter cannot
ask. Instead: one ``git rev-list origin/main`` builds the ancestor set for every distinct sha
this ``collect()`` call touches (any N, still one spawn); set membership yields
``True``/``False`` directly. An unresolvable ``origin/main`` (missing git, no such ref,
timeout) degrades EVERY row's ``reachable_on_default_branch`` to ``None`` — never coerced to
``False`` (DECISION-1) — matching the tri-state ``CommitClosure.reachable_on_default_branch:
bool | None`` already declares.

``envelope.py`` (which held ``_stamp_closure_reachability``) and the ``cockpit-emission.json``
writer pipeline it fed are gone — deleted per DR-351 (``docs/decisions/
DR-351-the-emission-is-deleted-not-halted.md``), not merely superseded. There is no shared
build path calling into this section any more; this porter's own resolution below is the only
place ``reachable_on_default_branch`` gets computed.

**§ Why reachability still costs a spawn, restated on its own terms.** The reachability fact
this section produces used to feed ``tracker_tier_a.py :: evidence_for_item`` ->
``classify_code_complete_tier``, which asserted ``auto`` only when BOTH a transitive
``trailer_bound`` fact AND ``reachable_on_default_branch`` resolved ``True``. That module is
deleted (commit e71a003c9); the interlock it named is retired, not replaced by a citable
successor in this tree. The current reader of this fact is example-cockpit-repo's auto-assert
(``coordinator/bin/query-commit-closures.py``), across the repo boundary. What survives the
retired consumer is the failure-direction argument itself, which never depended on who was
reading: a cached or frozen ancestry result fails toward a stale ``True``, which asserts work
as landed when it may not be — an over-claim, and unsafe. A live, uncached spawn instead fails
toward ``False``/``None`` on any resolution trouble — an under-claim, and safe. That asymmetry,
not the 82ms cost, is why this stays a live per-query spawn rather than a cached or
commit-time-frozen value. ``null`` must never be coerced to ``False`` downstream: ``null``
means "not checked," and coercing it would manufacture a negative where none was established.

Negative-spec:
  - Does NOT add any code to the commit hot path (``wsc_tail.py`` / ``commit_anchors.py`` /
    ``run_commit_pipeline``) — Anti-scope. This porter runs at emit/query time only; the
    write-time recording it reads back happened in C1 (``commit_trailers.py`` /
    ``commit_ledger/store.py`` / ``contract/apply_base.py``), not here.
  - Does NOT run ``git log`` or any other history-scanning subprocess (AC6, pinned by test,
    not by inspection) — the retired ``_SCAN_SINCE_HORIZON``/``_MARKER_GREPS`` scan-bound and
    pre-filter machinery is deleted with the scan, not left inert for a future reader to
    puzzle over (F8).
  - Does NOT re-normalize a ``Closes:`` trailer value — ``closes`` on a ledger entry is
    already the normalized item_id list, stamped at write time by
    ``extract_closure_facts_from_text`` via ``ops.emit.closure_trailer.parse_closure_
    trailers``. Calling that normalizer again here would be operating on values that are
    already IDs, not raw trailer text.
  - Does NOT compute or freeze reachability at commit time, and does NOT cache it across
    queries — see § Why reachability costs a spawn above.
  - Does NOT reimplement the deleted ``envelope._stamp_closure_reachability``'s ``cat-file``
    validity leg — see § Why reachability costs a spawn above for why that leg is a question
    this porter never needs to ask.
  - Does NOT recognize a hand-authored revert message (no auto-generated body line, so no
    ``reverts_sha`` was ever recorded for it) as a revert — D4's measured coverage limit is a
    stated design boundary, not a defect; a miss here fails safe (no retract), never a wrong
    retract (AC16).
  - Does NOT add a `Reverts:` trailer convention — DR-318 §D4 rejected that design outright:
    nothing in git or this tree produces such a trailer.

Spec backlink: pln-the-commit-closure-pipe-carrie-6cd275 § Chunk C2, DECISION-1, DECISION-4,
  AC4, AC5, AC6, AC9, AC11, AC16, AC17; DR-318 §D4, §D8.
Parity oracle: none — net-new record type; no bash equivalent ever emitted this fact.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from coordinator_core.commit_ledger import store as ledger_store
from coordinator_core.git.run import run_git
from coordinator_core.ops.emit.context import EmitContext

# %H-derived shas (the pre-ledger source) always emitted a full 40-char lowercase hex SHA;
# a ledger entry's ``sha`` field is arbitrary JSON, so this defensive check is still owed --
# a value failing this shape indicates a corrupt/hand-edited ledger line, not a valid commit
# identity, and is quarantined into ``malformed`` rather than emitted with a bad key.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _read_all_ledger_entries(ctx: EmitContext) -> List[Dict[str, Any]]:
    """Read every commit-ledger entry across every ``handoff_id``'s ``.jsonl`` file.

    ``store.read_entries`` is keyed per ``handoff_id``; this globs ``store.ledger_dir(...)``
    for every ledger file and calls that function once per file (its stem is the
    ``handoff_id``), reusing its existing malformed-line-skipping and per-sha dedup
    semantics rather than re-parsing the JSONL shape here. Pure local file I/O -- no
    subprocess. Returns ``[]`` when the session hub or ledger directory is unresolvable, or
    when no ledger files exist yet (a fresh repo, or one with no ledger-wired commits).
    """
    ldir = ledger_store.ledger_dir(str(ctx.repo_root))
    if ldir is None or not ldir.is_dir():
        return []

    entries: List[Dict[str, Any]] = []
    for fpath in sorted(ldir.glob("*.jsonl")):
        handoff_id = fpath.stem
        entries.extend(ledger_store.read_entries(handoff_id, str(ctx.repo_root)))
    return entries


def _resolve_reachability(repo_root: Path, shas: set) -> Dict[str, bool | None]:
    """Resolve ``reachable_on_default_branch`` for every sha in ``shas`` in ONE spawn.

    ``git rev-list origin/main`` builds the ancestor set once; membership yields
    ``True``/``False`` directly, since every sha handed in here is a commit BY CONSTRUCTION
    (recorded off a real commit at write time -- see § Why reachability costs a spawn in
    this module's docstring). An unresolvable ``origin/main`` (missing git, no such ref,
    timeout) degrades EVERY sha to ``None`` -- never coerced to ``False`` (DECISION-1).
    """
    if not shas:
        return {}
    result = run_git(["rev-list", "origin/main"], cwd=str(repo_root))
    if not result.ok:
        return {sha: None for sha in shas}
    ancestor_set = set(result.stdout.split())
    return {sha: (sha in ancestor_set) for sha in shas}


def collect(ctx: EmitContext) -> Tuple[List[dict], List[dict]]:
    """Build ``(records, malformed)`` for commit-closure facts (DECISION-1/4, AC4/5/6/9/11).

    One CLOSE record per ``(sha, item_id)`` pair from a ledger entry's already-normalized
    ``closes`` list, PLUS one REVERT record per (revert-commit entry, reverted close row)
    join (C3, DR-318 §D4/D8, AC9/AC16/AC17) -- both arms riding the same ``commit_closures``
    array, distinguished by ``reverts_sha`` (null on a close row, the reverted sha on a
    revert row). ``reachable_on_default_branch`` is resolved HERE, per query, in exactly one
    ``git rev-list origin/main`` spawn (AC4) -- see this module's docstring § Why
    reachability costs a spawn. Zero history-scanning subprocesses (AC6): the ledger read is
    pure local file I/O.

    Every row's ``repo`` is ``ctx.repo_name`` (AC11 -- per-repo scoped by construction, no
    multi-root parameter anywhere on this path).
    """
    entries = _read_all_ledger_entries(ctx)

    malformed: List[dict] = []
    sha_to_item_ids: Dict[str, List[str]] = {}
    close_pairs: List[Tuple[str, str]] = []  # (sha, item_id)
    revert_pairs: List[Tuple[str, str]] = []  # (sha, reverted_sha)
    all_shas: set = set()

    for entry in entries:
        sha = entry.get("sha")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            malformed.append({
                "sha": sha,
                "reason": "commit-ledger entry failed 40-char lowercase-hex SHA validation",
            })
            continue

        closes = entry.get("closes") or []
        item_ids = [item_id for item_id in closes if isinstance(item_id, str) and item_id]
        if item_ids:
            all_shas.add(sha)
            sha_to_item_ids.setdefault(sha, []).extend(item_ids)
            for item_id in item_ids:
                close_pairs.append((sha, item_id))

        reverts_sha = entry.get("reverts_sha")
        if isinstance(reverts_sha, str) and reverts_sha:
            all_shas.add(sha)
            revert_pairs.append((sha, reverts_sha))

    reachability = _resolve_reachability(ctx.repo_root, all_shas)

    records: List[dict] = []
    for sha, item_id in close_pairs:
        records.append({
            "repo": ctx.repo_name,
            "coordinator_root_path": ".",
            "provenance": ctx.provenance("local_fs", path="", derivation="parsed"),
            "item_id": item_id,
            "sha": sha,
            "reachable_on_default_branch": reachability.get(sha),
            "reverts_sha": None,
        })

    for sha, reverted_sha in revert_pairs:
        item_ids = sha_to_item_ids.get(reverted_sha)
        if not item_ids:
            continue  # reverted sha names no closure row (AC17) -- "not a revert", never an error.
        for item_id in item_ids:
            records.append({
                "repo": ctx.repo_name,
                "coordinator_root_path": ".",
                "provenance": ctx.provenance("local_fs", path="", derivation="parsed"),
                "item_id": item_id,
                "sha": sha,
                "reachable_on_default_branch": reachability.get(sha),
                "reverts_sha": reverted_sha,
            })

    return records, malformed
