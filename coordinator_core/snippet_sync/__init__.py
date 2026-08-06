"""coordinator_core.snippet_sync — snippet-registry + sentinel-sync verification.

Port of example-doctrine-repo-side `coordinator/bin/snippet-registry` (bash CLI, 477 LoC,
itself shelling to a python3 heredoc for TOML parsing) and the 7
`coordinator/bin/verify-<name>-sync.sh` scripts (~1489 LoC total,
~85-95% byte-identical across the 7).

Modules:
  registry.py — reads `snippets/registry.toml`, resolves per-snippet
                consumer sets (unconditional + conditional), in-process
                TOML parsing via `tomllib`/`tomli`. Consumed by example-doctrine-repo-side
                `coordinator/bin/snippet-registry` trampoline.
  verify.py   — the shared verify/--fix/--list engine. Per-snippet
                behavioral divergence (header extraction dialect,
                fence-awareness, insert-when-absent, consumer-resolution
                strategy) is DATA read from registry.toml metadata, not
                per-script code branches. Consumed by example-doctrine-repo-side
                `coordinator/bin/verify-snippet-sync` trampoline.

Spec backlink: example-doctrine-repo scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 6
Plan: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a group g3f
"""
from __future__ import annotations
