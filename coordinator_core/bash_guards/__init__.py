"""coordinator_core.bash_guards — PreToolUse:Bash guard engines.

Python engine-ification of example-doctrine-repo's PreToolUse:Bash guard cohort, per
the W3a/W3b naked-Python hook migration recipe
(scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md).

Each guard is a module exposing the interface pinned in write_guards/INTERFACE.md
(CLASS / MATCHERS / PRIORITY / check(payload)), adapted for the Bash matcher --
per-guard modules land here as the W3b build wave ports them one at a time.

Shared primitives (identity resolution, confined-findings-agent SSOT,
filename-legality predicate) live in `_helpers.py` -- see that module's
docstring for provenance. Do NOT duplicate those primitives inside a per-guard
module; import them from here.
"""
