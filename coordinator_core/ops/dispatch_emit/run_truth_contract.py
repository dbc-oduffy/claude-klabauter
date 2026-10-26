"""Wave-0 interface of the run-truth rewrite: the one table of names the ask's close gate, stage receipts,
mutating-call replies, fold bar, anchor lane, review verdict and sizing fingerprint share.

Pure dataclasses and constants, no I/O. Mirrored in docs/reference/run-truth-fields.md; DoE adopts these
names verbatim, so a rename is a contract break pinned by test_run_truth_contract.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from coordinator_core.ops import requirement_register as _rr

# Part (P-A): the requirement-register row. `unwired` is a rollup state, not a register status.
STATUS_UNWIRED = "unwired"
PART_STATUSES = tuple(_rr.STATUSES) + (STATUS_UNWIRED,)
PART_CLOSING_STATUS = _rr.STATUS_MET
PART_EVIDENCE_KEYS = ("commit", "test", "test_verdict")

# Close verdict (P-A, P-B).
OUTCOME_DONE, OUTCOME_PARTIAL, OUTCOME_REFUSED = "done", "partial", "refused"
CLOSE_OUTCOMES = (OUTCOME_DONE, OUTCOME_PARTIAL, OUTCOME_REFUSED)

# Stage (P-B).
STAGE_SIZING = "sizing"
STAGE_PLAN_REVIEW = "plan_review"
STAGE_EXECUTE = "execute"
STAGE_REVIEW = "review"
STAGE_KIRA = "kira"
STAGE_TERMINAL_JUDGE = "terminal_judge"
STAGE_REACHER = "reacher"
MANDATORY_STAGES = (
    STAGE_SIZING,
    STAGE_PLAN_REVIEW,
    STAGE_EXECUTE,
    STAGE_REVIEW,
    STAGE_KIRA,
    STAGE_TERMINAL_JUDGE,
    STAGE_REACHER,
)
NOT_APPLICABLE = "not_applicable"
RECEIPT_PATH_TEMPLATE = "state/warp/{run_id}/receipts/{stage}.json"
RECEIPT_FIELDS = ("stage", "run_id", "verdict", "artifact", "at")

# Arms and sizes, as sizing_fire names them.
ARM_XS, ARM_S, ARM_M_PLUS, ARM_ROADMAP = "xs", "s", "m_plus", "roadmap"
ARMS = (ARM_XS, ARM_S, ARM_M_PLUS, ARM_ROADMAP)
TSHIRTS = ("XS", "S", "M", "L", "XL", "XXL")
KIRA_MIN_TSHIRT = "L"

# Mutation reply (P-C).
MUTATION_REPLY_FIELDS = ("ok", "applied", "refused", "target")

# Suite verdict (P-D).
SUITE_VERDICT_PATH_TEMPLATE = "state/warp/suite-verdicts/{sha}.json"
SUITE_TIER_FULL = "full"
SUITE_VERDICT_FIELDS = ("sha", "tier", "failed", "passed", "skipped", "reds", "grant_ref")

# Anchor verdict (P-F1).
ANCHOR_HELD, ANCHOR_BROKE, ANCHOR_UNEVALUABLE = "held", "broke", "unevaluable"
ANCHOR_OUTCOMES = (ANCHOR_HELD, ANCHOR_BROKE, ANCHOR_UNEVALUABLE)

# Replay tests per part, consumed by C21. Existence of the paths is C21's check, not this module's.
_T = "coordinator_core/ops/dispatch_emit/tests/"
PART_REPLAYS: dict[str, list[str]] = {
    "P-A": [_T + "test_replay_pa_part_dropped_08132c.py", _T + "test_replay_pa_essential_false_closes_partial.py"],
    "P-B": [_T + "test_replay_pb_missing_receipt_e53c57.py"],
    "P-C": [
        _T + "test_replay_pc_revise_applied.py",
        _T + "test_revise_row_on_no_replan_arm_warns.py",
        _T + "test_replay_pc_refire_applied.py",
        _T + "test_tier_u_grant_cli.py",
    ],
    "P-D": [_T + "test_replay_pd_fold_bar_wiring.py", _T + "test_ask_stage_write_baseline.py"],
    "P-F1": [_T + "test_replay_pf1_anchor_lane_3196ec.py"],
    "P-F2": [_T + "test_replay_pf2_blocked_after_fix.py"],
    "P-F3": [
        _T + "test_replay_pf3_refire_no_replan.py",
        _T + "test_replay_pf3_discarded_plan_halts.py",
        _T + "test_refire_sha_free_note_executes_remaining.py",
    ],
    "P-F17": [_T + "test_replay_pf17_blocked_plan_execute.py", _T + "test_replay_pf17_xs_heredoc_bypass.py"],
    "P-G6": [_T + "test_replay_pg6_executor_heavy_9bcd34.py", _T + "test_replay_pg6_oracle_falsifier_args.py"],
    "P-H9": [_T + "test_replay_ph9_sizing_deterministic.py"],
    "P-OWED": ["docs/research/2026-10-25-a27976-fix-history.md"],
    "P-REG4": [_T + "test_replay_preg4_halted_claims_successor_lands.py"],
}


@dataclass(frozen=True)
class Evidence:
    """A part's proof of delivery; the part closes only when all three are set and `test_verdict` is `pass`."""

    commit: Optional[str] = None
    test: Optional[str] = None
    test_verdict: Optional[str] = None

    def complete(self) -> bool:
        return bool(self.commit and self.test and self.test_verdict == "pass")


@dataclass(frozen=True)
class Part:
    """One requirement-register row of the ask."""

    id: str
    source_text: str
    anchor: Optional[str] = None
    status: str = _rr.STATUS_OPEN
    evidence: Evidence = field(default_factory=Evidence)

    def closed(self) -> bool:
        return self.status == PART_CLOSING_STATUS and self.evidence.complete()


@dataclass(frozen=True)
class Close:
    """The close verdict: `done` only with no open part and no missing receipt."""

    outcome: str
    open_parts: tuple[str, ...] = ()
    missing_receipts: tuple[str, ...] = ()


def close_verdict(parts, receipts_present, required_stages) -> Close:
    """`done` when every part is closed and every required stage has a receipt, else `partial`
    naming the open rows and stages."""
    open_parts = tuple(p.id for p in parts if not p.closed())
    have = set(receipts_present)
    missing = tuple(s for s in required_stages if s not in have)
    outcome = OUTCOME_DONE if not open_parts and not missing else OUTCOME_PARTIAL
    return Close(outcome=outcome, open_parts=open_parts, missing_receipts=missing)


@dataclass(frozen=True)
class Receipt:
    """A stage's receipt, written to RECEIPT_PATH_TEMPLATE."""

    stage: str
    run_id: str
    verdict: str
    artifact: Optional[str]
    at: str


@dataclass(frozen=True)
class MutationReply:
    """A mutating call's reply; `ok` is true only when something was applied."""

    applied: tuple = ()
    refused: tuple = ()
    target: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.applied)


@dataclass(frozen=True)
class SuiteVerdict:
    """A recorded full-suite verdict; fold and publish read it, never run the suite."""

    sha: str
    failed: int
    passed: int
    skipped: int
    grant_ref: Optional[str] = None
    reds: tuple[Mapping[str, str], ...] = ()
    tier: str = SUITE_TIER_FULL

    def green(self) -> bool:
        return self.tier == SUITE_TIER_FULL and self.failed == 0 and not self.reds and bool(self.grant_ref)


@dataclass(frozen=True)
class AnchorVerdict:
    """An anchor-premise evaluation; `unevaluable` only for a non-census-form command."""

    id: str
    outcome: str
    observed: Optional[str] = None
    expect: Optional[str] = None


@dataclass(frozen=True)
class ReviewVerdict:
    """A reviewer's verdict on the plan as fixed; `reverified` is true when it re-checked its own fixes."""

    verdict: str
    fixes_applied: tuple = ()
    reverified: bool = False


@dataclass(frozen=True)
class SizingFingerprint:
    """What sizing must reproduce for the same ask."""

    parts_count: int
    tshirt_floor: str
    rows: tuple[str, ...] = ()


def mandatory_stages(arm: Optional[str], tshirt: Optional[str]) -> tuple[str, ...]:
    """The stages that must write a receipt for this arm and size.

    An unknown arm or size makes every stage mandatory and never excludes one. Kira keys on the
    t-shirt (L and up), never on the review-tier label, which calls L `standard`.
    """
    if arm not in ARMS or tshirt not in TSHIRTS:
        return MANDATORY_STAGES
    kira = TSHIRTS.index(tshirt) >= TSHIRTS.index(KIRA_MIN_TSHIRT)
    out = [STAGE_SIZING]
    if arm in (ARM_M_PLUS, ARM_ROADMAP):
        out.append(STAGE_PLAN_REVIEW)
    out.append(STAGE_EXECUTE)
    if arm != ARM_XS:
        out.append(STAGE_REVIEW)
    if kira:
        out.append(STAGE_KIRA)
    out.append(STAGE_TERMINAL_JUDGE)
    if arm != ARM_XS:
        out.append(STAGE_REACHER)
    return tuple(out)
