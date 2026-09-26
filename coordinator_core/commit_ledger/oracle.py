"""
coordinator_core.commit_ledger.oracle -- the chain-wide reviewer-weight
oracle: two figures, each with its stated basis, no exit code.

Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C6.md

Copies the shape that survived K-007's row-6 removal: `decide_review_scale`
row 4 (`coordinator_core/workstream_complete/directives_review.py`) takes a
handful of integers already computed for other reasons, compares them to
fixed thresholds, and returns a row plus a reason string -- no persistence,
no cross-call state, no subprocess, nothing to carry forward, nothing to
explain when the caller supplies nothing. This module is the "already
computed inputs" producer C7 wires into that same row-selection function as
the restored chain-wide arm: `evaluate` reads `commit_ledger.store.read_chain`
once, folds the entries into two weight figures, and hands both back with
their basis stated in plain language. It performs NO threshold comparison
and NO row selection itself -- that stays C7's remit, wiring this output
into `decide_review_scale`.

Two figures, not one: a commit whose changed paths are entirely doctrine-
bucketed (`.md` / `.mdx`, `classify.py` -> `review_brightline_gate.
classify_surface` bucket `"doctrine"`) is EXCLUDED from the code-only
figure and INCLUDED in the with-docs figure. Every other bucket
(`python`, `js`, `shell`, `config`, `cpp`, `test`, `other`) counts toward
both. The delta between the two figures IS the signal (dispatch brief):
an EM shown "1 reviewer of code, 2 more if you count the wiki edit"
calibrates against the actual shape of the diff; one shown a bare "4"
cannot tell a big code change from a big doc change and games the number
instead.

`skipped_sha_count` (C5's sha-unresolved-commit undercount, carried by the
CALLER -- this module has no ledger write access and derives nothing from
the store beyond `read_chain`) is folded into BOTH bases so the stated
figure is honest about what it does not see, per AC3b.

No exit code, no refusal arm (AC6, plan Anti-scope: "Do not make the
oracle a gate"). `OracleReport.resolved=False` is the ONLY "I have nothing
useful" outcome this module can produce, and it carries a plain-language
reason rather than anything a caller could mistake for a pass/fail signal.
`resolved=False` fires in exactly one case: no ledger file exists yet for
`handoff_id` ("pending" -- the baton has not committed anything the store
has seen). A ledger that EXISTS with zero entries is NOT the same outcome
(AC15, DR-239's silent-empty-join failure mode) -- it resolves normally to
a zero-weight figure whose basis says so explicitly, distinguishable on
sight from the pending case.

Negative-spec:
    - Do NOT add a threshold comparison or row-selection here -- that is
      `decide_review_scale`'s job (C7), not this module's.
    - Do NOT return anything shaped like an exit code, return code, or
      refusal enum member -- `OracleReport` and `OracleFigure` carry only
      a weight and a basis string.
    - Do NOT collapse "ledger absent" and "ledger present, zero entries"
      into the same figure -- see AC15 above.
    - Do NOT read git, spawn a subprocess, or start a nested interpreter --
      this module only calls `commit_ledger.store.read_chain` /
      `commit_ledger.store.ledger_path`, both pure local file I/O.
"""

from __future__ import annotations

from typing import Any, Callable, List, NamedTuple, Optional

from coordinator_core.commit_ledger.store import ledger_path, read_chain

_DOCS_KIND = "doctrine"


class OracleFigure(NamedTuple):

    weight: Optional[float]
    basis: str


class OracleReport(NamedTuple):

    code_only: OracleFigure
    with_docs: OracleFigure
    resolved: bool = True


def _pending(reason: str) -> OracleReport:
    figure = OracleFigure(weight=None, basis=reason)
    return OracleReport(code_only=figure, with_docs=figure, resolved=False)


def _coerce_entry_weight(weight_basis: Any) -> float:
    if isinstance(weight_basis, bool):
        return 0.0
    if isinstance(weight_basis, (int, float)):
        return float(weight_basis)
    return 0.0


def evaluate(
    handoff_id: str,
    *,
    skipped_sha_count: int = 0,
    cwd: Optional[str] = None,
    read_chain_fn: Callable[..., List[Any]] = read_chain,
) -> OracleReport:
    if not handoff_id:
        return _pending("no handoff_id supplied — nothing to report")

    if ledger_path(handoff_id, cwd) is None or not _ledger_exists(handoff_id, cwd):
        return _pending(
            f"ledger pending for {handoff_id} — no ledger file recorded yet, no dispositions offered"
        )

    entries = read_chain_fn(handoff_id, cwd=cwd) or []

    code_weight = 0.0
    docs_only_weight = 0.0
    code_count = 0
    docs_only_count = 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        weight = _coerce_entry_weight(entry.get("weight_basis"))
        kind = entry.get("kind")
        if kind == _DOCS_KIND:
            docs_only_weight += weight
            docs_only_count += 1
        else:
            code_weight += weight
            code_count += 1

    with_docs_weight = code_weight + docs_only_weight
    with_docs_count = code_count + docs_only_count

    skip_note = (
        f"; {skipped_sha_count} commit(s) skipped as sha-unresolved and not counted here"
        if skipped_sha_count
        else ""
    )

    code_basis = (
        f"{code_count} code commit(s), weight {code_weight:g}, "
        f"{docs_only_count} doc-only commit(s) excluded{skip_note}"
    )
    with_docs_basis = (
        f"{with_docs_count} commit(s) counting docs, weight {with_docs_weight:g} "
        f"({code_count} code + {docs_only_count} doc-only){skip_note}"
    )

    return OracleReport(
        code_only=OracleFigure(weight=code_weight, basis=code_basis),
        with_docs=OracleFigure(weight=with_docs_weight, basis=with_docs_basis),
        resolved=True,
    )


def _ledger_exists(handoff_id: str, cwd: Optional[str]) -> bool:
    path = ledger_path(handoff_id, cwd)
    return path is not None and path.is_file()
