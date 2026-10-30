"""coordinator_core.symbol_knowledge -- the post-commit ``symbol-knowledge`` close leg.

Purpose: after a run closes, flag the public symbols its terminal commit changed, hand them to one
detached batch job, and keep ``state/symbol-knowledge/definitions.json`` current. Interface in
``contract``, sidecar I/O in ``sidecar``, the close-leg composition in ``leg``.
"""
