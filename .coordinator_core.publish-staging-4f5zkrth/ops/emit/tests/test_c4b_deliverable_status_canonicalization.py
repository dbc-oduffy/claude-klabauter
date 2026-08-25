"""Formerly: tests for C4b's `canonicalize()` wiring into
`deliverable_status._compute_map` / `stamp()`.

`state/deliverable-equivalence.yaml` + `canonicalize()` are condemned and
collapsed to identity (plan 2026-08-20-the-close-ceremony-stops-paying-for-
the-join, F-1) -- the fork-pair join this module pinned no longer exists,
`_compute_map`/`stamp()` group strictly on raw `deliverable_id` now, and
there is nothing left here to test.

Spec backlink: pln-deliverable-id-fork-remediatio-894e26 § C4 (AC6, AC6b) --
superseded.
"""

from __future__ import annotations
