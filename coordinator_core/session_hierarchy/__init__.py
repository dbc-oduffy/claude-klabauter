"""
coordinator_core.session_hierarchy — session/workstream hierarchy derivation package.

Purpose: derive the session/workstream hierarchy projection from handoff
frontmatter lineage. See ``derive.py`` for the pure transform and
``coordinator_core.ops.session_hierarchy_derive`` for the op wrapper (query +
atomic write) that composes it.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g3
Port of: derive-session-hierarchy.sh (example-doctrine-repo f0aa2d56, 2026-07-16)
"""

from __future__ import annotations
