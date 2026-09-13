"""
coordinator_core/p4/workspace.py — D1 marker/identity readers + D9 S3 session-state reader.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § D1, § D9.

Never imports ``coordinator_core.p4.runner`` — every reader here is a pure
local read (frontmatter key, machine-local registry, ``meta.json``), never a
p4 spawn (D1: "detection is a declared marker, never a probe").

- ``is_p4_repo`` — D1's marker: ``vcs_mirror: p4`` in the repo's
  ``coordinator.local.md`` frontmatter, via the existing
  ``resolve_validation_cmd.cs_read_local_md_key``.
- ``identity`` — the machine-local ``p4.<repo_key>.{port,user,client,client_root}``
  row. Raises the typed ``P4WorkspaceUnregistered`` when the marker is
  present but the machine-local row is not — never falls back to ambient
  ``P4*`` env (D1).
- ``session_change`` — the D9 S3 reader of ``meta.json``'s ``p4_change``,
  ``p4_base_sha``, ``p4_shelved_at``, ``p4_shelved_sha``.

Negative-spec:
  - No ``.p4config`` walk-up, no ``p4 set``, no ambient ``P4*`` env read (D1).
  - No fragment loader, no provider-slot machinery (D9) — the read contract
    is DoE's committed ``p4-provider-fragment.md``; nothing here reads it at
    runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from coordinator_core.machine_resolver import registry_get
from coordinator_core.resolve_validation_cmd import cs_read_local_md_key
from coordinator_core.session.core import read_meta_field

#: D1 — the one declared marker key, and the one value that means "p4-mirrored".
_MARKER_KEY = "vcs_mirror"
_MARKER_VALUE = "p4"

#: Review: overengineering-reviewer F5 (integrator-applied) -- `client_root`
#: is required on the `p4.<repo_key>.*` registry ROW (register.py still
#: writes both rows) but dropped from the fields `identity()` treats as
#: hard-fail. No in-repo consumer reads `identity().client_root` (shelve,
#: session_change._mint, the checkout guard, session_state all use only
#: port/user/client) -- it exists for a cross-repo reader (cockpit takes
#: provider identity from `.client_root`), which should not hold hard-fail
#: authority over local ops that never touch it.
_IDENTITY_FIELDS = ("port", "user", "client")


class P4WorkspaceUnregistered(Exception):
    """Raised by ``identity()`` when the repo declares ``vcs_mirror: p4``
    but the machine-local registry carries no (or a partial)
    ``p4.<repo_key>.*`` row. Fails loud once, on the first p4-gated op —
    never falls back to ambient ``P4*`` env (D1)."""

    def __init__(self, repo_key: str):
        self.repo_key = repo_key
        super().__init__(f"p4 workspace unregistered for repo_key={repo_key!r}")


@dataclass(frozen=True)
class P4Identity:
    port: str
    user: str
    client: str
    #: Optional (F5) -- carried for a cross-repo reader (cockpit), not
    #: read by any in-repo consumer; absence never blocks `identity()`.
    client_root: Optional[str] = None


def is_p4_repo(repo_root: str) -> bool:
    """D1's marker check — a flat top-level ``coordinator.local.md`` read,
    zero spawns. Never a probe: no ``.p4config`` walk-up, no ``p4 set``."""
    return cs_read_local_md_key(repo_root, _MARKER_KEY).strip() == _MARKER_VALUE


def identity(repo_key: str) -> P4Identity:
    """The machine-local ``p4.<repo_key>.{port,user,client,client_root}`` row.

    Raises ``P4WorkspaceUnregistered`` if any of ``port``/``user``/``client``
    is absent — a partial row is treated the same as no row, since a
    partial identity cannot spawn a valid ``p4`` invocation either.
    ``client_root`` (F5) is read best-effort and never blocks: it has no
    in-repo consumer and exists for a cross-repo reader (cockpit), so an
    incomplete registration on that one field should not fail every
    p4-gated local op.
    """
    values = {}
    for name in _IDENTITY_FIELDS:
        value = registry_get(f"p4.{repo_key}.{name}")
        if not value:
            raise P4WorkspaceUnregistered(repo_key)
        values[name] = value
    values["client_root"] = registry_get(f"p4.{repo_key}.client_root") or None
    return P4Identity(**values)


def session_change(sdir: str) -> dict:
    """D9 S3 reader — ``meta.json``'s ``p4_change`` (``int | None``),
    ``p4_base_sha``, ``p4_shelved_at``, ``p4_shelved_sha`` (each
    ``str | None``). Never raises: absent file/fields read as ``None``,
    matching ``read_meta_field``'s own never-raises contract.

    ``p4_change`` is the *pending* CL number and goes stale the moment
    anyone submits it (D3) — this reader does not know or care whether the
    number it returns is still pending; that is the caller's problem.
    """
    raw_change = read_meta_field(sdir, "p4_change")
    p4_change: Optional[int]
    try:
        p4_change = int(raw_change) if raw_change else None
    except ValueError:
        p4_change = None
    return {
        "p4_change": p4_change,
        "p4_base_sha": read_meta_field(sdir, "p4_base_sha") or None,
        "p4_shelved_at": read_meta_field(sdir, "p4_shelved_at") or None,
        "p4_shelved_sha": read_meta_field(sdir, "p4_shelved_sha") or None,
    }
