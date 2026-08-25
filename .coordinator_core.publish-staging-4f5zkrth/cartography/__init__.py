"""makima's cartography substrate — machine-computed repo-structure primitives.

Purpose: pure-function producers of the JSON contract a downstream survey/atlas
consumer ingests (symbol table + edges + file->system index + churn schema),
replacing agentic full-inventory passes with cheap, deterministic Python.
Package established as the shared foundation the individual primitives
(``tree``, ``file_index``, ``churn``, ``symbols``, ``edges``) import for
caller-supplied-path containment; this module itself contains none of those
primitives.

Spec backlink: pln-makima-cartography-substrate-a-26eb2e
§ chunk C2 (package foundation + containment helper).

Negative-spec: this package marker does NOT import any op submodule — the
five primitives above land in separate parallel chunks and importing them
here before they exist would break the package at import time. Consumers
import each primitive by its full module path (e.g.
``coordinator_core.cartography.tree``), never via a re-export from here.
"""

from __future__ import annotations
