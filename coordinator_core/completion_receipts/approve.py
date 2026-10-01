"""coordinator_core.completion_receipts.approve — append a human-approved or rejected receipt.

`receipt.approve` writes one superseding receipt citing a PM quote found verbatim in a repo file.
It only writes; the caller commits with a scoped commit.

DR-208 five-question affirmation (MUTATING):
  1. Writes state?  YES — one new file under state/completion-receipts/<YYYY-MM>/.
  2. Writes into rag's relational store?  No.
  3. Opens a file for write?  YES — exclusive-create of that one receipt, via store.write_receipt.
  4. Mutates shared state outside this module?  YES — the receipts directory.
  5. Persistent change observable across processes?  YES — a reader sees the new chain head.
  Classified MUTATING unconditionally.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.completion_receipts.model import mint_receipt_id, receipt_rel_path
from coordinator_core.completion_receipts.store import (
    ReceiptWriteError,
    current_heads,
    read_receipts,
    write_receipt,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path

VERDICTS = ("human-approved", "rejected")
DERIVATION = "receipt.approve"


class ApproveRefusal(Exception):
    """The approval was refused; the message is one fact plus a terse alternative."""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _slug_source(receipt_id: str) -> str:
    return re.sub(r"-[0-9a-f]{6}$", "", receipt_id[len("rcp-"):])


def approve(
    worktree_root: Path,
    receipt_id: str,
    verdict: str,
    approved_by: str,
    pm_quote: str,
    pm_quote_ref: str,
) -> str:
    """Write the superseding receipt; returns its repo-relative path. Raises ApproveRefusal."""
    root = Path(worktree_root)
    if verdict not in VERDICTS:
        raise ApproveRefusal(f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}.")
    if not (approved_by or "").strip():
        raise ApproveRefusal("approved_by is empty. Name the approving human.")
    quote = _norm(pm_quote or "")
    if not quote:
        raise ApproveRefusal("pm_quote is empty. Pass the PM's recorded words.")

    heads = current_heads(read_receipts(root))
    target = heads.get(receipt_id)
    if target is None:
        raise ApproveRefusal(
            f"{receipt_id} is not a current receipt head. Approve the head of its chain."
        )

    ref = contained_path(root / (pm_quote_ref or ""), [root]) if pm_quote_ref else None
    if ref is None or not ref.is_file():
        raise ApproveRefusal(f"pm_quote_ref {pm_quote_ref!r} is not a readable repo file.")
    try:
        text = ref.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ApproveRefusal(f"pm_quote_ref {pm_quote_ref!r} is not a readable repo file.") from None
    if quote not in _norm(text):
        raise ApproveRefusal(f"pm_quote is not in {pm_quote_ref}. Quote the file verbatim.")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_id = mint_receipt_id(_slug_source(receipt_id))
    fm = copy.deepcopy({k: v for k, v in target.items() if k != "_path"})
    fm.update(
        receipt_id=new_id,
        verdict=verdict,
        supersedes=receipt_id,
        approved_by=approved_by.strip(),
        concluded_at=now,
        prose_ref=receipt_rel_path(new_id, now),
        provenance={
            "observed_at": now,
            "derivation": f"{DERIVATION} (pm_quote_ref: {pm_quote_ref})",
            "ref": copy.deepcopy((target.get("provenance") or {}).get("ref"))
            or {"branch": None, "sha": None},
        },
    )
    quoted = "\n".join(f"> {line}" if line else ">" for line in pm_quote.strip().splitlines())
    prose = (
        f"## Approval\n\n{quoted}\n\n"
        f"Source: `{pm_quote_ref}`\n\n"
        f"{approved_by.strip()} marked {receipt_id} {verdict}.\n"
    )
    try:
        return write_receipt(root, fm, prose)
    except ReceiptWriteError as exc:
        raise ApproveRefusal(str(exc)) from exc


@register_op("receipt.approve")
def _approve_op(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "receipt.approve": params receipt_id, verdict, approved_by, pm_quote, pm_quote_ref.

    MUTATING (DR-208); see the module docstring for the five-question affirmation.
    """
    root = repo_root or params.get("repo_root")
    if not root:
        return {"ok": False, "error": "repo_root is required."}
    try:
        path = approve(
            Path(root),
            params.get("receipt_id") or "",
            params.get("verdict") or "",
            params.get("approved_by") or "",
            params.get("pm_quote") or "",
            params.get("pm_quote_ref") or "",
        )
    except ApproveRefusal as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "receipt_path": path}
