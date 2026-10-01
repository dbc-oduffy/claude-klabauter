"""
CompletionReceipt — what one execute-plan or mise-en-place run concluded for one
baton, and who vouched for it. Pydantic projection of DoE
`coordinator/schemas/completion-receipt.schema.json` 1.0.0 into the cockpit
contract (DoE cockpit decision D55, CONTRACT_VERSION 4.10.0).

`verdict` lives here and never on the handoff entity: baton completion is derived.
`superseded` is derived by a reader from a later receipt's `supersedes`, never
written, so it is absent from the verdict enum. `repo` and `coordinator_root_path`
are connector-injected (D4); the file's authored `repo` is not trusted.
The `schema` wire key is carried by the alias `schema_` (a field named `schema`
shadows `BaseModel.schema`).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coordinator_core.contract.cockpit_schema.common import IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ProvenanceEnvelope

CompletionReceiptVerdict = Literal["agent-delivered", "human-approved", "rejected"]
CompletionReceiptRunKind = Literal["execute-plan", "mise-en-place"]
CompletionReceiptTshirt = Literal["XS", "S", "M", "L", "XL", "XXL"]

_RECEIPT_ID_PATTERN = r"^rcp-[a-z0-9-]+-[0-9a-f]{6}$"


class CompletionReceiptRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: CompletionReceiptRunKind
    id: str


class CompletionReceiptJudge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: str
    observation_summary: str
    judged_at: IsoDateTime


class CompletionReceiptCommitRange(BaseModel):
    """Base exclusive to head inclusive."""

    model_config = ConfigDict(extra="forbid")

    base: str | None
    head: str | None


class CompletionReceiptLoe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimated: CompletionReceiptTshirt | None
    actual: CompletionReceiptTshirt | None


class CompletionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["completion-receipt"] = Field(alias="schema")
    receipt_id: str = Field(pattern=_RECEIPT_ID_PATTERN)
    baton_id: str | None
    deliverable_id: str | None
    """At least one of `baton_id` and `deliverable_id` is non-null."""
    workstream_id: str | None
    repo: str
    """Connector-injected registry shortname (D4)."""
    coordinator_root_path: str
    """Connector-injected absolute path of the coordinator root (D4)."""
    plan_path: str | None
    branch: str | None
    run: CompletionReceiptRun
    verdict: CompletionReceiptVerdict | None
    judge: CompletionReceiptJudge | None
    """Non-null whenever `verdict` is `agent-delivered`."""
    commit_range: CompletionReceiptCommitRange | None
    started_at: IsoDateTime | None
    concluded_at: IsoDateTime
    loe: CompletionReceiptLoe
    prose_ref: str
    """Repo-relative path of the receipt file; a provenance pointer, never a transport."""
    supersedes: str | None = Field(pattern=_RECEIPT_ID_PATTERN)
    approved_by: str | None
    provenance: ProvenanceEnvelope

    @model_validator(mode="after")
    def _conditional_requirements(self) -> "CompletionReceipt":
        if self.baton_id is None and self.deliverable_id is None:
            raise ValueError("at least one of baton_id and deliverable_id is non-null")
        if self.verdict == "agent-delivered" and self.judge is None:
            raise ValueError("judge is non-null when verdict is agent-delivered")
        if self.verdict in ("human-approved", "rejected") and (
            self.supersedes is None or self.approved_by is None
        ):
            raise ValueError(
                "supersedes and approved_by are non-null when verdict is human-approved or rejected"
            )
        return self
