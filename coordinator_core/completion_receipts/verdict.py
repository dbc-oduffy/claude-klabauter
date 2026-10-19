"""coordinator_core.completion_receipts.verdict — the refusal predicates and the run verdict.

`mint_refusal` is `review_stamp.mint`'s refusal ladder in the order mint states it, returning
mint's exact message strings; `judge_verdict` decides agent-delivered from the same record before
the terminal commit exists.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_JUDGE_IDENTITY = "execute-review"

#: The prep producer (coordinator-test-runner) reports in prose: either a line-start
#: `product_files: N` / `- product_files: N` / `**product_files:** N`, or a mid-line
#: `product_files: N.` inside its `## Run Report` verdict sentence. A mid-line mention elsewhere
#: (an Observations note quoting the launcher's stderr) is not the report.
_RUN_REPORT_HEADING = re.compile(r"^##[ \t]+Run Report[ \t]*$", re.MULTILINE)
_NEXT_H2 = re.compile(r"^##[ \t]", re.MULTILINE)


def _run_report(text: str) -> str:
    """The `## Run Report` section body, or ``""``."""
    head = _RUN_REPORT_HEADING.search(text)
    if head is None:
        return ""
    nxt = _NEXT_H2.search(text, head.end())
    return text[head.end(): nxt.start() if nxt else len(text)]


def _body_field(text: str, key: str, value: str) -> Optional[str]:
    line_start = re.compile(
        rf"^[ \t]*(?:[-*][ \t]+)?\**{key}\**[ \t]*:\**[ \t]*`?({value})\b", re.MULTILINE
    )
    match = line_start.search(text)
    if match is None:
        match = re.search(rf"\b{key}[ \t]*:[ \t]*`?({value})\b", _run_report(text))
    return match.group(1) if match else None


def body_product_files(text: str) -> Optional[int]:
    """The body-form `product_files` count in `text`, else None. Callers apply it only when
    the frontmatter carries no `product_files` key: frontmatter wins."""
    found = _body_field(text, "product_files", r"\d+")
    return int(found) if found is not None else None


def body_run_base_sha(text: str) -> Optional[str]:
    """The body-form `run_base_sha` in `text` (a sha token only), else None; frontmatter wins."""
    return _body_field(text, "run_base_sha", r"[0-9a-fA-F]{7,64}")


def with_body_prep_fields(data: dict, text: str) -> dict:
    """`data` (a prep sidecar's frontmatter) with `product_files` and `run_base_sha` lifted from
    the prose body for each key the frontmatter lacks. The test-runner writes both in prose."""
    lifted = dict(data)
    if "product_files" not in lifted:
        count = body_product_files(text)
        if count is not None:
            lifted["product_files"] = count
    if "run_base_sha" not in lifted:
        sha = body_run_base_sha(text)
        if sha is not None:
            lifted["run_base_sha"] = sha
    return lifted


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


def superseding_delivery(record: dict, delivery: dict) -> dict:
    """The delivery block a superseding record is minted against: its own when it carries a
    verdict, else PASS read off a `met` criterion with an observation (the fresh review's
    judgment), since a stranded run has no delivery stage to return one. A recorded FAIL stands."""
    if delivery.get("verdict") is not None:
        return delivery
    criterion = record.get("criterion")
    if not isinstance(criterion, dict) or criterion.get("status") != "met":
        return delivery
    observation = criterion.get("observation")
    if not isinstance(observation, str) or not observation.strip():
        return delivery
    return {**delivery, "verdict": "PASS"}


def _unbacked_suffix(delivery: object) -> str:
    """The verifier's unbacked claims (claim and the evidence it lacked) as a refusal suffix; empty when none."""
    items = delivery.get("unbacked") if isinstance(delivery, dict) else None
    if not isinstance(items, list) or not items:
        return ""
    lines = [
        f"- {c.get('claim')} [lacked: {c.get('anchor')}]" for c in items if isinstance(c, dict)
    ]
    return "; unbacked claims:\n" + "\n".join(lines)


def test_verdict_of(build_test: dict) -> Any:
    """The build/test verdict of a record: `test_verdict` when present, else `status` (old
    sidecars, where `status` was the verdict; on a run-report it is the lifecycle)."""
    verdict = build_test.get("test_verdict")
    return verdict if verdict is not None else build_test.get("status")


def baseline_clears(build_test: dict) -> bool:
    """True for a `fail` whose every failure the runner re-ran in an export of the run base or of
    the landing tree and attributed to `pre_existing` or `foreign`: nothing the run caused, nothing
    unverified. Mirrors the digest's `wake_digest._BASELINE_CLEARS_JS`; keep the two in step."""
    if test_verdict_of(build_test) != "fail" or build_test.get("baseline_method") != "export":
        return False
    baseline = build_test.get("baseline")
    if not isinstance(baseline, dict):
        return False
    pre_existing = baseline.get("pre_existing") or []
    foreign = baseline.get("foreign") or []
    failed = build_test.get("tests_failed", build_test.get("failed")) or 0
    if not (isinstance(pre_existing, list) and isinstance(foreign, list)):
        return False
    cleared = len(pre_existing) + len(foreign)
    return (
        not (baseline.get("caused") or [])
        and not (baseline.get("unverified") or [])
        and cleared > 0
        and isinstance(failed, int)
        and cleared >= failed
    )


def effective_test_verdict(build_test: dict) -> Any:
    """`test_verdict_of`, with a baseline-equal `fail` read as `pass`: a run is held to the
    baseline it started from, never to an all-green suite it did not break."""
    return "pass" if baseline_clears(build_test) else test_verdict_of(build_test)


def mint_refusal(
    integration: dict,
    prep: dict,
    build_test: dict,
    *,
    criterion_open_ok: bool = False,
    delivery_exempt: bool = False,
) -> str | None:
    """The first mint refusal message that applies to this record, else `None`.

    ``criterion_open_ok`` lets a ``not_met``/``indeterminate`` criterion through: the stamp
    attests review and delivery and carries the criterion status, so the plan's close stays
    a separate act. A build/test verdict of ``not_run`` is then accepted under any criterion.

    ``delivery_exempt`` accepts an ABSENT delivery verdict (the roster's tiers keep the verifier
    off this run's tier); a present non-PASS verdict still refuses."""
    footprint = prep.get("slice_files")
    foreign_claims = [
        c for c in prep.get("foreign_claims") or []
        if not isinstance(footprint, list) or str(c).split(" ", 1)[0] in footprint
    ]
    reviewed_files = max(_count(prep.get("slice_files")), _count(prep.get("product_files")))

    delivery = integration.get("delivery")
    delivery_verdict = delivery.get("verdict") if isinstance(delivery, dict) else None
    unresolved = integration.get("unresolved") or []
    confinement_violations = _count(integration.get("confinement_violations"))
    tests_status = effective_test_verdict(build_test)
    criterion = integration.get("criterion")
    criterion_status = criterion.get("status") if isinstance(criterion, dict) else None

    if delivery_verdict != "PASS" and not (delivery_exempt and delivery_verdict is None):
        return (
            f"review-stamp: refusing to mint: delivery verdict is {delivery_verdict!r}, not PASS"
            + _unbacked_suffix(delivery)
        )
    if criterion_status in ("not_met", "indeterminate") and not criterion_open_ok:
        return f"review-stamp: refusing to mint: exit criterion is {criterion_status}"
    if tests_status != "pass" and not (
        tests_status == "not_run" and (criterion_status == "met" or criterion_open_ok)
    ):
        return (
            f"review-stamp: refusing to mint: build/test verdict is {tests_status!r}, not pass"
            + (f" (exit criterion {criterion_status})" if tests_status == "not_run" else "")
        )
    if len(unresolved) > 0:
        return f"review-stamp: refusing to mint: {len(unresolved)} unresolved finding(s)"
    if confinement_violations > 0:
        return f"review-stamp: refusing to mint: {confinement_violations} confinement violation(s)"
    if len(foreign_claims) > 0:
        return (
            f"review-stamp: refusing to mint: {len(foreign_claims)} foreign claim(s) on spine paths: "
            + ", ".join(str(c) for c in foreign_claims)
        )
    # An empty diff mints only on a met criterion: the outcome already held (a peer landed it),
    # so there is nothing to review and the judge's observation is the evidence.
    if reviewed_files == 0 and criterion_status != "met":
        return "review-stamp: refusing to mint: zero files in the reviewed diff and the exit criterion is not met"
    return None


def judge_verdict(
    record: dict | None, *, all_rows_landed: bool, now: str
) -> tuple[str | None, dict | None]:
    """`("agent-delivered", judge)` only for a record mint would accept, a met criterion with an
    observation, and every row landed; otherwise `(None, None)`."""
    if not isinstance(record, dict) or not all_rows_landed:
        return None, None
    prep = record.get("prep")
    build_test = record.get("tests")
    if not isinstance(prep, dict) or not isinstance(build_test, dict):
        return None, None
    if mint_refusal(record, prep, build_test) is not None:
        return None, None
    criterion = record.get("criterion")
    if not isinstance(criterion, dict) or criterion.get("status") != "met":
        return None, None
    observation = criterion.get("observation")
    if not isinstance(observation, str) or not observation.strip():
        return None, None
    return "agent-delivered", {
        "identity": _JUDGE_IDENTITY,
        "observation_summary": observation.strip(),
        "judged_at": now,
    }
