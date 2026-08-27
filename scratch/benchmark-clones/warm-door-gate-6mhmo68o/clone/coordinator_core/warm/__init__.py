"""coordinator_core.warm — the warm-engine execution model's own code home.

Purpose: DR-315's (docs/decisions/DR-315-warm-engine-execution-model.md)
package for process-lifetime concerns specific to a resident engine —
starting with this package's `tests/` subpackage, which characterizes the
nine process-global sites the mechanism-2 audit (plus staff-eng finding 8)
found blocking concurrent in-process dispatch.

Spec backlink: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C2
"""
