"""coordinator_core.completion_receipts — the contract package for run-end completion receipts.

`model` owns shape and validation, `store` the append-only files and readers, `verdict` the
judge predicate, `day` the day derivation, `approve` the `receipt.approve` op. Other modules import these and never re-derive any of them.
"""
