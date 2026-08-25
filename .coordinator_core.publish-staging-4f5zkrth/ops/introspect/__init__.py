"""
coordinator_core.ops.introspect — read-only, cross-signal "is this shipped?" primitives.

Purpose: a small home for library functions that ANSWER questions about the live repo's
state rather than MUTATE it — distinct from `ops/emit/` (which assembles/writes the
cockpit snapshot) and `ops/fleet/` (which archives terminal artifacts). The first
inhabitant is `verify_shipped` (see `verify_shipped.py`).

Spec backlink: state/handoffs/2026-07-25_000823_shipped-state-verifier.md

Deliberately no re-export here (import `coordinator_core.ops.introspect.verify_shipped`
directly): a package-level `from .verify_shipped import verify_shipped` would rebind this
package's `verify_shipped` attribute from the submodule to the function of the same name,
shadowing `coordinator_core.ops.introspect.verify_shipped` as a module reference for any
later `import coordinator_core.ops.introspect.verify_shipped as x` / monkeypatch target.
"""
