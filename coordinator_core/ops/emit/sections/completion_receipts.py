"""Parity oracle: none (net-new section; covered by tests/test_completion_receipts_section.py).

Section porter — CompletionReceipt (envelope key: ``completion_receipts``).

Emits one CompletionReceipt per parseable ``state/completion-receipts/<YYYY-MM>/*.md`` file,
read through ``coordinator_core.completion_receipts.store.read_receipts``. Every receipt is
emitted, superseded ones included: ``superseded`` is derived by the reader from a later
receipt's ``supersedes``. ``repo``, ``coordinator_root_path`` and ``provenance`` are
connector-injected (D4); the file's authored values are discarded.

A receipt that fails the live ``CompletionReceipt`` entity model is returned in the malformed
list (``path``/``reason``/``frontmatter_keys``). The envelope has no ``malformed_records``
bucket for this section, so the envelope layer drops that list. No receipts yields ``[]``.
"""

from __future__ import annotations

from pydantic import ValidationError

from coordinator_core.completion_receipts.store import read_receipts
from coordinator_core.contract.cockpit_schema.entities.completion_receipt import (
    CompletionReceipt,
)
from coordinator_core.ops.emit.context import EmitContext


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root

    records: list[dict] = []
    malformed: list[dict] = []
    for fm in read_receipts(root):
        path = fm.pop("_path")
        rec = {
            **fm,
            "repo": ctx.repo_name,
            "coordinator_root_path": ".",
            "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
        }
        try:
            CompletionReceipt.model_validate(rec)
        except ValidationError as exc:
            malformed.append(
                {
                    "path": path,
                    "reason": f"fails CompletionReceipt: {exc.error_count()} violation(s)",
                    "frontmatter_keys": sorted(fm.keys()),
                }
            )
            continue
        records.append(rec)
    return records, malformed
