"""
coordinator_core.ops.gate_liveness.resolve — "gate_liveness.resolve" JSON-RPC op.

Purpose: the READER half of the gate-closure-signal contract (the PRODUCER
half is `gate_liveness.emit_discharge`, C4). Per the landed contract
(`plan-tasks.schema.json` 1.10.0's `external_gate[].closure_key` +
`cross-repo-memo.schema.json` 1.7.0's optional `discharges` object — SSOT
`coordinator_core.contract.emit_memo_schema`), this op answers, for every
`external_gate` entry across one or more plans: has the thing that
discharges this gate actually landed?

AMENDED 2026-08-21 — ONE resolver, not two, and it is a JOIN, not a
heuristic. `resolve` matches identities instead of guessing from lifecycle
state:

  `closure_key` — the entry carries `closure_key: {kind, id}`. This module
    scans cross-repo memos for a `discharges:` block whose `closure_key`
    matches on BOTH `kind` and `id`; on a match the verdict is
    `discharged`, cited to that memo path plus the block's `evidence` and
    `landed_at`. `memo-thread` ids compare in the form `in_reply_to`
    normalizes to (bare basename, case-insensitive, with or without
    `.md`) — see `_memo_thread_ids_match`. `deliverable` ids compare by
    exact (stripped) string equality — see `_deliverable_ids_match`.

    TWO BINDING READER RULES, from the ruling, neither optional:
      - Scans BOTH `cross-repo/inbox/` AND `cross-repo/archive/` (the
        latter recursively — archive is nested by date). The boot sweep
        moves actioned memos; an actioned discharge memo is still the
        discharge record. Scanning inbox only loses discharges to a sweep
        that runs on someone else's schedule.
      - Keys on the `discharges` block ALONE — never on `status:`. This
        plan's own census falsified status-derived inference five separate
        ways; this module does not read a memo's `status` field at all.

    No `closure_key` on the entry -> `undetermined`, reason
    `no-closure-key`. A `closure_key` with no matching `discharges` block
    -> `undetermined`, reason `awaiting-discharge`. NEVER `holds` for
    either: absence of a discharge record is not evidence the blocker is
    live, only that nothing has said otherwise.

  RETIRED, not deferred: the `inbound_memo` and `ledgered_sha` heuristics
  from the pre-contract draft. Both were narrow guesses at a signal that
  now exists explicitly, and keeping either as a fallback would re-admit
  exactly the inference the census falsified. `_memo_resolver`'s identity
  normalization is reused only for the routing sanity check below (never
  to produce a match on its own).

Routing sanity check (on a match only): a matched memo's `from:` must
belong to the gate's `owner_repo` — `_from_matches_owner_repo` strips the
memo sender's trailing `-em` and compares against `owner_repo`'s bare
hyphenated shortname (case-insensitive). This is checked ONLY once a
`closure_key` has already matched on kind+id; it never produces a match by
itself, and a mismatch degrades the candidate to a non-match (the scan
continues over any other memo) rather than being surfaced as its own
verdict/reason — a `closure_key` collision across two unrelated senders is
exactly what this check exists to catch.

ZERO process spawns — no `subprocess`, no git, per DR-344 §4 and AC9. A
`discharged` verdict without both a resolver name and a citation is a
programming error (asserted in tests).

Reads the spine through the existing `read_spine`/`load_rows` seam:
specifically `coordinator_core.ops.plan_tasks_render.load_rows`, the same
fenced-block locator `dispatch_emit.spine_read.read_spine` itself wraps.
This module reads the RAW rows (not `read_spine`'s dispatchable-filtered
view) because it must see every `external_gate` entry regardless of the
row's own disposition/cleared state — a row already marked `cleared: true`
or `disposition: coded` still has a real verdict worth reporting, and
`read_spine` would silently exclude it. Does not re-parse the fenced block
and does not modify that seam.

Negative-spec:
  - Does NOT call `dispatch_emit.spine_read.read_spine` — that module's
    non-dispatchable-row exclusion is a DIFFERENT concern (which rows are
    safe to hand to a wave-builder), not this op's (what does every
    declared gate currently resolve to).
  - Does NOT mutate `cleared` or any other frontmatter field on the plan —
    read-only, per the schema's own description ("a reader ... may PROPOSE
    the `cleared: true` flip with a citation — it never performs it").
  - Does NOT shell out to git to enumerate memos — plain `Path.rglob`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops.gate_liveness.emit_discharge import CLOSURE_KEY_KINDS
from coordinator_core.ops.plan_tasks_render import load_rows

VERDICT_HOLDS = "holds"
VERDICT_DISCHARGED = "discharged"
VERDICT_UNDETERMINED = "undetermined"

_RESOLVER_CLOSURE_KEY = "closure_key"

_REASON_NO_CLOSURE_KEY = "no-closure-key"
_REASON_AWAITING_DISCHARGE = "awaiting-discharge"


def _memo_thread_ids_match(a: str, b: str) -> bool:
    """True when two `memo-thread` closure-key ids name the same memo.

    Compares in the form `in_reply_to` normalizes to (see
    `memo_send._normalize_in_reply_to`): bare basename, case-insensitive,
    with or without the trailing `.md`.
    """

    def _norm(value: str) -> str:
        basename = Path(value.strip()).name.lower()
        return basename[:-3] if basename.endswith(".md") else basename

    return _norm(a) == _norm(b)


def _deliverable_ids_match(a: str, b: str) -> bool:
    """True when two `deliverable` closure-key ids are the same `dlv-...` id.

    Exact (stripped) string equality — a deliverable_id is a minted,
    case-sensitive token, never a filesystem path subject to basename/
    extension normalization the way a `memo-thread` id is.
    """
    return a.strip() == b.strip()


def _closure_key_ids_match(kind: str, a: str, b: str) -> bool:
    if kind == "memo-thread":
        return _memo_thread_ids_match(a, b)
    if kind == "deliverable":
        return _deliverable_ids_match(a, b)
    return False


def _repo_shortname_from_em_id(em_id: str) -> str:
    """Bare hyphenated shortname for a sender `from:` em-id.

    `'claude-klabauter-em' -> 'claude-klabauter'` — mirrors the stripping half
    of `_memo_resolver.convention_repo_key` without importing that module
    (this is a routing SANITY check, not a receiver-registry resolution;
    it never reads the machine-local registry).
    """
    normalized = em_id.strip().lower()
    return normalized[:-3] if normalized.endswith("-em") else normalized


def _from_matches_owner_repo(from_id: str, owner_repo: str) -> bool:
    """Routing sanity check: does a matched memo's `from:` belong to
    `owner_repo`? Checked ONLY after a closure_key kind+id match — see
    module docstring. Never used to produce a match on its own."""
    if not isinstance(from_id, str) or not from_id.strip():
        return False
    return _repo_shortname_from_em_id(from_id) == owner_repo.strip().lower()


class _DischargeRecord:
    """One well-formed `discharges:` block found on a scanned memo."""

    __slots__ = ("memo_path", "from_id", "kind", "id", "evidence", "landed_at")

    def __init__(
        self,
        memo_path: Path,
        from_id: Any,
        kind: Any,
        id_value: Any,
        evidence: Any,
        landed_at: Any,
    ) -> None:
        self.memo_path = memo_path
        self.from_id = from_id
        self.kind = kind
        self.id = id_value
        self.evidence = evidence
        self.landed_at = landed_at


def _extract_discharge_record(memo_path: Path, frontmatter: dict) -> Optional[_DischargeRecord]:
    """Return a `_DischargeRecord` if `frontmatter` carries a well-formed
    `discharges:` block, else `None`. Tolerant of malformed shapes (a
    non-dict `discharges`, a non-dict `closure_key`, missing sub-fields) —
    a malformed block is simply not a candidate, never a raise; this
    module's fail-loud discipline is reserved for the `discharged`
    verdict's own resolver/citation invariant, not for reading foreign
    memo files it does not control the shape of.
    """
    discharges = frontmatter.get("discharges")
    if not isinstance(discharges, dict):
        return None
    closure_key = discharges.get("closure_key")
    if not isinstance(closure_key, dict):
        return None
    kind = closure_key.get("kind")
    id_value = closure_key.get("id")
    if kind not in CLOSURE_KEY_KINDS:
        return None
    if not isinstance(id_value, str) or not id_value.strip():
        return None
    return _DischargeRecord(
        memo_path=memo_path,
        from_id=frontmatter.get("from"),
        kind=kind,
        id_value=id_value,
        evidence=discharges.get("evidence"),
        landed_at=discharges.get("landed_at"),
    )


def _scan_discharge_records(repo_root: Path) -> list:
    """Scan `repo_root`'s own `cross-repo/inbox/` and `cross-repo/archive/`
    trees for memos carrying a well-formed `discharges:` block.

    Both directories are scanned unconditionally (archive recursively,
    nested by date) — the boot sweep moves actioned memos, so an actioned
    discharge memo is still the discharge record (binding reader rule, see
    module docstring). Sorted by path for deterministic match order.
    """
    records: list = []
    cross_repo_dir = repo_root / "cross-repo"
    for sub in ("inbox", "archive"):
        search_dir = cross_repo_dir / sub
        if not search_dir.is_dir():
            continue
        for memo_path in sorted(search_dir.rglob("*.md")):
            try:
                text = memo_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parsed = parse_frontmatter(text)
            frontmatter = parsed.get("frontmatter")
            if not isinstance(frontmatter, dict):
                continue
            record = _extract_discharge_record(memo_path, frontmatter)
            if record is not None:
                records.append(record)
    return records


def _resolve_closure_key(
    closure_key: Any, owner_repo: str, records: list
) -> tuple:
    """Resolve one `external_gate` entry's `closure_key` against the scanned
    `discharges` records.

    Returns `(verdict, resolver, evidence, reason)`.
    """
    if not isinstance(closure_key, dict):
        return VERDICT_UNDETERMINED, _RESOLVER_CLOSURE_KEY, None, _REASON_NO_CLOSURE_KEY

    kind = closure_key.get("kind")
    id_value = closure_key.get("id")
    if kind not in CLOSURE_KEY_KINDS or not isinstance(id_value, str) or not id_value.strip():
        return VERDICT_UNDETERMINED, _RESOLVER_CLOSURE_KEY, None, _REASON_NO_CLOSURE_KEY

    for record in records:
        if record.kind != kind:
            continue
        if not _closure_key_ids_match(kind, record.id, id_value):
            continue
        # Routing sanity check — on a match only; a mismatch degrades this
        # candidate to a non-match and the scan continues (module docstring).
        if not _from_matches_owner_repo(record.from_id, owner_repo):
            continue
        evidence = {
            "memo_path": str(record.memo_path),
            "evidence": record.evidence,
            "landed_at": record.landed_at,
        }
        return VERDICT_DISCHARGED, _RESOLVER_CLOSURE_KEY, evidence, None

    return VERDICT_UNDETERMINED, _RESOLVER_CLOSURE_KEY, None, _REASON_AWAITING_DISCHARGE


def resolve_gate_liveness(plan_paths: list, repo_root: Path) -> list:
    """Resolve every `external_gate` entry across `plan_paths` against
    `repo_root`'s own inbound `discharges:` records.

    Returns a list of `{plan, row_id, owner_repo, verdict, resolver,
    evidence, reason}` dicts, one per `external_gate` entry, in
    plan-then-row-then-entry order. A `plan_path` whose task-spine block is
    absent or malformed contributes no entries (tolerant read — matches
    `load_rows`'s own tolerant-read posture; this module is not the
    schema-enforcement surface).
    """
    records = _scan_discharge_records(repo_root)
    results: list = []
    for plan_path in plan_paths:
        with open(plan_path, encoding="utf-8") as handle:
            source = handle.read()
        loaded = load_rows(source)
        if loaded.status is not LocateStatus.LOCATED:
            continue
        for raw_row in loaded.rows:
            if not isinstance(raw_row, dict):
                continue
            row_id = raw_row.get("id")
            external_gate = raw_row.get("external_gate")
            if not isinstance(external_gate, list):
                continue
            for entry in external_gate:
                if not isinstance(entry, dict):
                    continue
                owner_repo = entry.get("owner_repo")
                if not isinstance(owner_repo, str) or not owner_repo.strip():
                    continue
                closure_key = entry.get("closure_key")
                verdict, resolver, evidence, reason = _resolve_closure_key(
                    closure_key, owner_repo, records
                )
                assert verdict != VERDICT_DISCHARGED or (
                    resolver and evidence
                ), "gate_liveness.resolve: a discharged verdict requires both a resolver name and a citation"
                results.append(
                    {
                        "plan": str(plan_path),
                        "row_id": row_id,
                        "owner_repo": owner_repo,
                        "verdict": verdict,
                        "resolver": resolver,
                        "evidence": evidence,
                        "reason": reason,
                    }
                )
    return results


def _err(msg: str) -> dict:
    return {"exit_code": 1, "error": msg}


@register_op("gate_liveness.resolve")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "gate_liveness.resolve" handler.

    Params:
        plan_path   (str)         — a single plan path (repo-relative or
                                     absolute). Mutually inclusive with
                                     `plan_paths` — supply at least one.
        plan_paths  (list[str])   — one or more plan paths. Merged with a
                                     supplied `plan_path` (both may be given).
        repo_root   (str)         — OPTIONAL wire override of the injected
                                     `repo_root` (worktree-scoped, "show_top"
                                     — see `coordinator_core.op_scopes`).

    Returns `{exit_code: 0, verdicts: [...]}` — see `resolve_gate_liveness`
    for the per-entry shape. `exit_code: 1` with an `error` string when
    neither `plan_path` nor `plan_paths` is supplied, or `repo_root` cannot
    be resolved (neither the wire param nor the injected worktree root is
    present), or a named plan path does not exist on disk.
    """
    plan_paths: list = []
    single = params.get("plan_path")
    if isinstance(single, str) and single.strip():
        plan_paths.append(single.strip())
    many = params.get("plan_paths")
    if isinstance(many, list):
        plan_paths.extend(p.strip() for p in many if isinstance(p, str) and p.strip())

    if not plan_paths:
        return _err("missing required param: plan_path or plan_paths (non-empty)")

    root_param = params.get("repo_root")
    if isinstance(root_param, str) and root_param.strip():
        resolved_root = Path(root_param.strip())
    elif repo_root is not None:
        resolved_root = Path(repo_root)
    else:
        return _err("repo_root could not be resolved (no wire param, no injected worktree root)")

    resolved_plan_paths: list = []
    for p in plan_paths:
        candidate = Path(p)
        if not candidate.is_absolute():
            candidate = resolved_root / candidate
        if not candidate.is_file():
            return _err(f"plan path does not exist: {candidate}")
        resolved_plan_paths.append(candidate)

    verdicts = resolve_gate_liveness(resolved_plan_paths, resolved_root)
    return {"exit_code": 0, "verdicts": verdicts}
