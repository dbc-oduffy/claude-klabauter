"""coordinator_core.ops.test_red_record — the makima-side emitter for
``state/test-red/<machine>.yaml``.

Spec backlink: cross-repo commitment
    DoE-claude state/cross-repo-commitments/2026-07-25-makima-to-answer-the-test-red-record-con-bff3653a45f8.yaml
Frozen contract:
    project-makima cross-repo/archive/2026-07-25-doe-claude-em-test-red-record-contract-consult.md
    § "## EM Response" (the reply that froze the schema — Q1 parse-stdout with
    output-derived ``runner``, Q2 read-modify-write + atomic write + monotonic
    guard (yes to all three, plus two amendments), Q3 makima-owned ``test-red
    ack`` engine op).
Consumers (read-only, DoE-claude, verified byte-identical on the schema they
parse):
    coordinator/commands/workday-start.md § Step 1.66 "Test-Red Delta Surface"
    coordinator/skills/workstream-start/SKILL.md § Engage, item 6
        "Red-suite predicate"

WHY THIS EXISTS
    Both consumer copies above have read `state/test-red/<machine>.yaml`
    since they landed, and both openly documented the gap: "expected state on
    every machine until the makima-side emitter lands." Nothing in
    ``coordinator_core`` wrote that file before this module. `red-set-report.py`
    (coordinator/bin/red-set-report.py) already computes an equivalent
    failing-set census, but by RUNNING pytest itself under a fresh
    ``--collect-only``/execution pair — a different shape than this module's
    job, which is to derive the SAME kind of failing-node-id set from output
    a caller already captured at its own validate seam
    (``workday-complete-step1-validate.py`` / ``validate-fast-and-packageability.py``),
    without spawning a second test run. This module does not reimplement or
    call red-set-report.py; the "reuse the census" instruction is satisfied by
    NOT re-deriving node-id parsing logic that already exists nowhere else in
    this repo (there was none to reuse for this shape) and by not spawning a
    second pytest execution the way that script does.

RECORD SHAPE (frozen, do not invent fields)
    state/test-red/<machine>.yaml:
        tiers:
          <tier-name>:
            ran_at: <ISO8601Z>            # emitter-owned
            sha: <git HEAD sha>           # emitter-owned
            exit_code: <int>              # emitter-owned, raw passthrough
            outcome: green|test-failures|build-failure|runner-error|not-run
            runner: pytest|node-test|bats|unknown
            failing: [<nodeid>, ...] | [] | null   # TRI-STATE, see Q1
            previous:                     # rotated forward only when the
              ran_at: <ISO8601Z>          # OUTGOING failing was authoritative
              failing: [...]              # (including []) — Q2 amendment 1
            acknowledged:                 # operator-owned — emitter NEVER
              owner: <path>               # authors or clears this block
              acknowledged_at: <ISO8601Z>
              baseline: [...]
              expires_at: <ISO8601Z>

    ``machine`` resolves via ``coordinator_core.machine_resolver.compute_machine``
    (the ``coordinator.machine_slug`` registry key, same resolution both
    consumer docs cite: "resolved the same way /workday-start Step 1.66
    does"). The record lives under the INVOKING repo's ``state/test-red/`` —
    Q2 amendment 2 — never under DoE-claude's tree.

NEGATIVE SPEC
    - Does NOT run pytest, node --test, or any test tier itself. Callers pass
      already-captured process output; this module only parses it.
    - Does NOT author, clear, or otherwise touch the ``acknowledged`` block
      from ``write_test_red_record`` — that block is exclusively
      ``set_acknowledgement``/``clear_acknowledgement``'s (Q3), which in turn
      touch nothing outside it.
    - Does NOT rotate ``previous`` forward on a non-authoritative
      (``failing is None``) run — Q2 amendment 1.
    - Does NOT overwrite a newer on-disk run with an older one — the
      monotonic ``ran_at`` guard aborts the write via ``MutateAbort`` rather
      than clobbering (ISO8601 ``...Z`` timestamps sort lexicographically, so
      plain string comparison is exact).
    - Does NOT decide when an acknowledgement is void (owner unresolvable,
      past ``expires_at``) — that evaluation is consumer-side (DoE-claude),
      per Q3's plane split.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.locked_write import LOCK_TIMEOUT_SECS, MutateAbort, locked_rmw
from coordinator_core.machine_resolver import compute_machine

VALID_OUTCOMES = frozenset(
    {"green", "test-failures", "build-failure", "runner-error", "not-run"}
)
VALID_RUNNERS = frozenset({"pytest", "node-test", "bats", "unknown"})

# Q1 counter-proposal (frozen): runner is a property of the OUTPUT, derived
# from an ordered set of parsers, never of the invocation string. First
# matcher wins; `unknown` (failing=None) when none match.
_PYTEST_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_NODE_TAP_NOT_OK_RE = re.compile(r"^not ok \d+ - (.+?)\s*$", re.MULTILINE)
_BATS_NOT_OK_RE = re.compile(r"^not ok \d+ (?!-)(.+?)\s*$", re.MULTILINE)


def parse_failing_nodeids(output: str) -> tuple[str, Optional[list[str]]]:
    """Derive ``(runner, failing)`` from already-captured stdout+stderr text.

    Ordered parsers, first match wins (Q1 counter-proposal — ``runner``
    records which parser matched, not what command was run):
      1. pytest ``FAILED <nodeid>`` / ``ERROR <nodeid>`` summary lines.
      2. ``node --test`` TAP ``not ok N - <name>`` lines.
      3. bats TAP ``not ok N <name>`` lines (no dash — bats and node --test
         share the TAP shape; the dash is the only reliable discriminator
         observed between the two, and is a best-effort heuristic, not a
         contract term the freeze pinned).

    Returns ``("unknown", None)`` when no parser matches — the tri-state
    ``failing: null`` case, never treated as an empty/clean run.
    """
    pytest_matches = sorted(set(_PYTEST_FAILED_RE.findall(output)))
    if pytest_matches:
        return "pytest", pytest_matches

    node_matches = sorted(set(_NODE_TAP_NOT_OK_RE.findall(output)))
    if node_matches:
        return "node-test", node_matches

    bats_matches = sorted(set(_BATS_NOT_OK_RE.findall(output)))
    if bats_matches:
        return "bats", bats_matches

    return "unknown", None


def _test_red_path(repo_root: Path, machine: str) -> Path:
    return repo_root / "state" / "test-red" / f"{machine}.yaml"


def build_tier_entry(
    *,
    existing: Optional[dict],
    ran_at: str,
    sha: str,
    exit_code: int,
    outcome: str,
    runner: str,
    failing: Optional[list[str]],
) -> dict:
    """Pure function: compute the new tier entry given the prior one.

    ``previous`` rotation (Q2 amendment 1): rotates forward to the OUTGOING
    run's own ``ran_at``/``failing`` only when that outgoing ``failing`` was
    itself authoritative (a list, including ``[]``) — never on a ``null``
    (non-authoritative) outgoing run, which would otherwise destroy the only
    baseline consumers have absent an ``acknowledged`` block.

    ``acknowledged`` is carried forward byte-for-byte (never authored or
    cleared here) — Q2's core invariant.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"invalid outcome: {outcome!r} (want one of {sorted(VALID_OUTCOMES)})")
    if runner not in VALID_RUNNERS:
        raise ValueError(f"invalid runner: {runner!r} (want one of {sorted(VALID_RUNNERS)})")
    if runner == "unknown" and failing is not None:
        raise ValueError("runner='unknown' must pair with failing=None (tri-state null)")

    existing = existing or {}
    previous = existing.get("previous")
    prior_failing = existing.get("failing")
    prior_ran_at = existing.get("ran_at")

    if failing is not None and prior_failing is not None and prior_ran_at is not None:
        previous = {"ran_at": prior_ran_at, "failing": list(prior_failing)}

    entry: dict = {
        "ran_at": ran_at,
        "sha": sha,
        "exit_code": exit_code,
        "outcome": outcome,
        "runner": runner,
        "failing": list(failing) if failing is not None else None,
    }
    if previous is not None:
        entry["previous"] = previous
    if existing.get("acknowledged") is not None:
        entry["acknowledged"] = existing["acknowledged"]
    return entry


def write_test_red_record(
    *,
    repo_root: Path,
    tier: str,
    sha: str,
    exit_code: int,
    outcome: str,
    runner: str,
    failing: Optional[list[str]],
    machine: Optional[str] = None,
    ran_at: Optional[str] = None,
    timeout: float = LOCK_TIMEOUT_SECS,
) -> dict:
    """Read-modify-write one tier's entry into ``state/test-red/<machine>.yaml``.

    Atomic (locked_rmw: flock + mkstemp/os.replace) and race-safe under
    concurrent sessions on the same machine (Q2). Raises ``MutateAbort`` —
    propagated by ``locked_rmw`` without writing — if an existing on-disk
    entry for this tier carries a ``ran_at`` at or after the incoming one
    (monotonic guard: a slow run finishing second must not clobber a fast
    run that finished first).
    """
    machine = machine or compute_machine()
    ran_at = ran_at or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = _test_red_path(repo_root, machine)
    target.parent.mkdir(parents=True, exist_ok=True)

    result: dict = {}

    def _mutate(old_text: str) -> str:
        doc = yaml.safe_load(old_text) if old_text.strip() else None
        doc = doc if isinstance(doc, dict) else {}
        tiers = doc.get("tiers") or {}
        existing_entry = tiers.get(tier)

        if existing_entry and existing_entry.get("ran_at") and ran_at <= existing_entry["ran_at"]:
            raise MutateAbort(
                f"stale run: incoming ran_at={ran_at!r} <= on-disk ran_at="
                f"{existing_entry['ran_at']!r} for tier {tier!r} on {machine!r}"
            )

        new_entry = build_tier_entry(
            existing=existing_entry,
            ran_at=ran_at,
            sha=sha,
            exit_code=exit_code,
            outcome=outcome,
            runner=runner,
            failing=failing,
        )
        tiers[tier] = new_entry
        doc["tiers"] = tiers
        result["entry"] = new_entry
        return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)

    locked_rmw(target, _mutate, repo_root=repo_root, timeout=timeout, missing_ok=True)
    return result["entry"]


def set_acknowledgement(
    *,
    repo_root: Path,
    tier: str,
    owner: str,
    machine: Optional[str] = None,
    acknowledged_at: Optional[str] = None,
    expires_days: int = 14,
    timeout: float = LOCK_TIMEOUT_SECS,
) -> dict:
    """``test-red ack --owner <path>`` (Q3). Writes ONLY the ``acknowledged``
    block for ``tier`` — never the emitter-owned scalars.

    ``baseline`` snapshots the record's CURRENT ``failing[]`` at
    acknowledgement time (must be authoritative — a ``null`` failing set
    cannot be acknowledged, per Q3's own design note). ``expires_at``
    defaults to ``acknowledged_at`` + ``expires_days`` (14).
    """
    machine = machine or compute_machine()
    acknowledged_at = acknowledged_at or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    expires_at = (
        _dt.datetime.strptime(acknowledged_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
        + _dt.timedelta(days=expires_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = _test_red_path(repo_root, machine)

    result: dict = {}

    def _mutate(old_text: str) -> str:
        doc = yaml.safe_load(old_text) if old_text.strip() else None
        doc = doc if isinstance(doc, dict) else {}
        tiers = doc.get("tiers") or {}
        entry = tiers.get(tier)
        if not entry:
            raise MutateAbort(f"no test-red record for tier {tier!r} on {machine!r}")
        baseline = entry.get("failing")
        if baseline is None:
            raise MutateAbort(
                f"tier {tier!r} failing is null (unavailable) — cannot acknowledge"
            )
        entry["acknowledged"] = {
            "owner": owner,
            "acknowledged_at": acknowledged_at,
            "baseline": list(baseline),
            "expires_at": expires_at,
        }
        tiers[tier] = entry
        doc["tiers"] = tiers
        result["entry"] = entry
        return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)

    locked_rmw(target, _mutate, repo_root=repo_root, timeout=timeout, missing_ok=False)
    return result["entry"]


def clear_acknowledgement(
    *,
    repo_root: Path,
    tier: str,
    machine: Optional[str] = None,
    timeout: float = LOCK_TIMEOUT_SECS,
) -> dict:
    """``test-red ack --clear`` (Q3). Removes ONLY the ``acknowledged`` block."""
    machine = machine or compute_machine()
    target = _test_red_path(repo_root, machine)

    result: dict = {}

    def _mutate(old_text: str) -> str:
        doc = yaml.safe_load(old_text) if old_text.strip() else None
        doc = doc if isinstance(doc, dict) else {}
        tiers = doc.get("tiers") or {}
        entry = tiers.get(tier)
        if not entry:
            raise MutateAbort(f"no test-red record for tier {tier!r} on {machine!r}")
        entry.pop("acknowledged", None)
        tiers[tier] = entry
        doc["tiers"] = tiers
        result["entry"] = entry
        return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)

    locked_rmw(target, _mutate, repo_root=repo_root, timeout=timeout, missing_ok=False)
    return result["entry"]
