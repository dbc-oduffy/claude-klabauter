"""Shared JS fake for the deferred emit's receipt-check leg (MATCH/MISMATCH relay shape)."""

from __future__ import annotations

# Keyed on the leg label, never the command text. Harnesses call it first in their `agent` fake.
RECEIPT_MATCH_FAKE_JS = (
    "function _receiptCheckReply(prompt, opts) {\n"
    "  if (opts && opts.label === 'receipt-check') return { found: true, verdict: 'MATCH' };\n"
    "  return undefined;\n"
    "}\n"
)
