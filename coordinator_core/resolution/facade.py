"""
coordinator_core.resolution.facade — Tier-A resolution facade with TWO
structurally-distinct guard methods.

Purpose: collapse the "what kind of root is this?" question every
resolution call site in the canonical-resolution-engine migration has to
answer, into exactly two methods whose NAMES carry the provenance
discriminator — never a single generic ``validate_root()`` that would hide
it:

    resolve_operator_config()   — operator-authored, gitignored, per-machine
                                   config (settings home, claude-klabauter root/bin, example-doctrine-repo
                                   root). Corruption-checked ONLY: these
                                   values are typo'd or stale, never
                                   adversarial, so this method never touches
                                   the trust boundary.

    guard_plugin_root(root, *, mode)
                                 — a HARNESS-SUPPLIED (CLAUDE_PLUGIN_ROOT)
                                   root. Trust-checked via the shared,
                                   already-tested
                                   ``coordinator_core.trusted_root_guard``
                                   trust-core — this method is a THIN
                                   ADAPTER, not a re-derivation (see its
                                   docstring below for why re-deriving would
                                   be a correctness risk, not just
                                   duplication).

The discriminator is PROVENANCE, not operation: both methods answer "is this
path OK to use?", but one path comes from the operator's own machine config
and the other arrives from a caller this process does not fully trust
(a ``--plugin-dir`` spike, or any harness invocation the guard's own
docstring enumerates). Conflating the two into one ``validate_root()`` would
either over-apply the trust boundary to operator config (irrelevant — the
operator cannot attack their own machine) or under-apply it to harness input
(a real security regression). Keeping them as two named methods makes that
choice visible at every call site rather than buried in an argument.

Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md § W1-A1

Negative-spec:
  - Does NOT add symlink or world-writable checks to
    ``resolve_operator_config`` — those are trust concerns that blur the
    provenance boundary this module exists to keep sharp, and
    world-writable-bit checks are not Windows-portable (DR-148 lens).
  - Does NOT re-derive the four-anchor / ``/..`` / Windows ``_norm`` / mode
    logic inline in ``guard_plugin_root`` — it delegates to
    ``coordinator_trusted_root_guard`` so there is exactly one
    implementation of that trust-core, tested exactly once
    (``coordinator_core/test_trusted_root_guard.py``).
  - Does NOT author a fourth TOML/sentinel parser — composes the
    already-shipped readers (``coordinator_core.trusted_root_guard._doe_root``
    / ``._claude_klabauter_root``, which themselves reuse
    ``coordinator_core.machine_resolver._flatten`` / ``._load_toml``) rather
    than re-implementing registry/sentinel resolution here.
"""

from __future__ import annotations

import os
from typing import Optional

from coordinator_core.trusted_root_guard import (
    _doe_root,
    _claude_klabauter_root,
    _settings_home_dir_from_env,
    coordinator_trusted_root_guard,
)


class OperatorConfigError(RuntimeError):
    """Raised by ``resolve_operator_config`` when an operator-authored
    config value is corrupt.

    Corruption, not distrust: this exception is raised for empty/
    whitespace-only values, an embedded newline (the list-valued registry
    key shape — see ``_corruption_reason``), a ``/..``/``\\..`` traversal
    segment, or a path that does not exist as a directory on disk. It is
    NEVER raised for "root is untrusted" — that is ``guard_plugin_root``'s
    concern, and the two are not interchangeable (see module docstring).
    """


def _corruption_reason(value: str) -> Optional[str]:
    """Return a corruption reason string, or ``None`` if *value* is clean.

    Corruption set for operator-authored, gitignored, per-machine config
    (corruption-checked only, never trust-checked — see module docstring):

      - an embedded newline. Pinned edge case: the shipped
        ``_registry_example_doctrine_repo``/``_registry_claude_klabauter`` readers join a
        list-valued TOML registry key with ``"\\n"`` (``"\\n".join(str(i)
        for i in val)``) — that shape is a corruption REJECT here, single-line
        is a hard requirement, not a value this facade silently re-flattens.
      - empty after ``.strip()``. Pinned edge case: a whitespace-only value
        (``"   \\n"``) survives the shipped readers' bare ``.rstrip("\\n")``
        as a non-empty string (``"   "``) — checking truthiness alone would
        let it through, so this checks ``.strip()`` instead.
      - a ``/..`` or ``\\..`` traversal segment.
      - does not exist as a directory on disk.
    """
    if "\n" in value:
        return "embedded newline (multi-line/list-valued registry value)"
    if not value.strip():
        return "empty or whitespace-only"
    if "/.." in value or "\\.." in value:
        return "contains a '..' traversal segment"
    if not os.path.isdir(value):
        return "does not exist as a directory on disk"
    return None


def _checked(name: str, value: str) -> str:
    """Apply ``_corruption_reason`` to *value*, raising ``OperatorConfigError``
    (naming *name* and the reason) on a hit, else returning *value* unchanged."""
    reason = _corruption_reason(value)
    if reason is not None:
        raise OperatorConfigError(
            f"resolve_operator_config: '{name}' resolved to a corrupt value "
            f"{value!r} ({reason}) — this is operator-authored, per-machine "
            "config (registry.local.toml / registry.toml / sentinel files "
            "under machine-local/), not a harness-supplied value; fix it via "
            "'machine-local set <key> <path>' or by editing the sentinel "
            "file directly. Re-run coordinator:install if unsure."
        )
    return value


def resolve_operator_config(*, env: dict | None = None) -> dict:
    """Resolve the four operator-authored config paths — CORRUPTION-CHECKED
    ONLY, never trust-checked (see module docstring's provenance
    discriminator). This method must NEVER call
    ``coordinator_trusted_root_guard``/``is_trusted`` — that is
    ``guard_plugin_root``'s exclusive concern (see
    ``coordinator_core/resolution/test_facade.py``'s AC-2 regression test).

    Composes the already-shipped registry/sentinel readers
    (``trusted_root_guard._doe_root``/``._claude_klabauter_root``, themselves built on
    ``machine_resolver._flatten``/``._load_toml``) rather than re-deriving a
    fourth parser.

    Returns a plain dict: ``{settings_home, claude_klabauter_bin, claude_klabauter_root,
    doe_root}``. Raises ``OperatorConfigError`` naming the first corrupt
    value found (checked in that same order).
    """
    env = os.environ if env is None else env

    settings_home = _checked("settings_home", _settings_home_dir_from_env(env))
    claude_klabauter_root = _checked("claude_klabauter_root", _claude_klabauter_root(env))
    claude_klabauter_bin = _checked(
        "claude_klabauter_bin", os.path.join(claude_klabauter_root, "coordinator", "bin")
    )
    doe_root = _checked("doe_root", _doe_root(env))

    return {
        "settings_home": settings_home,
        "claude_klabauter_bin": claude_klabauter_bin,
        "claude_klabauter_root": claude_klabauter_root,
        "doe_root": doe_root,
    }


def guard_plugin_root(
    root: str,
    *,
    mode: str,
    site: str = "coordinator root",
    env: dict | None = None,
) -> bool:
    """Trust-check a HARNESS-SUPPLIED (``CLAUDE_PLUGIN_ROOT``) *root* — a
    THIN ADAPTER that delegates the entire decision to
    ``coordinator_core.trusted_root_guard.coordinator_trusted_root_guard``.

    Does NOT re-derive the four-anchor / ``/..`` / Windows ``_norm`` / mode
    logic — a re-implementation here would silently risk dropping the
    Windows backslash-traversal check, the single-trailing-slash parity
    quirk, or the fail-loud/fail-open mode tail (see
    ``coordinator_core/test_trusted_root_guard.py`` for the full fixture
    matrix this delegation stays parity-locked against).

    ``mode`` is keyword-only with NO default — inherited automatically via
    delegation to ``coordinator_trusted_root_guard``, which raises
    ``ValueError`` on empty/unknown ``mode``. The no-default is what stops a
    fail-open call site being silently promoted to fail-loud during the
    frontage migration.
    """
    return coordinator_trusted_root_guard(mode=mode, root=root, site=site, env=env)
