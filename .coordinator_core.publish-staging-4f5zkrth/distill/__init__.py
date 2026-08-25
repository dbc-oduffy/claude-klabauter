"""
coordinator_core.distill — the distill-ceremony engine scripts package.

Purpose: read-only logic modules backing the 5 distill-ceremony CLIs
(ripe-filter, sidecar-sweep, delete-guard, harvest-debt, memo-triage). Every
script under this package reads repo/state substrate and emits JSON; none
writes repo substrate or forms a durable store (makima's store-less
invariant — a future write-capable variant would need DR-211's five-bound
admission test).

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C0
"""

from __future__ import annotations
