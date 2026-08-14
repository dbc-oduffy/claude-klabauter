"""
coordinator_core.session_hierarchy — session/workstream hierarchy derivation package.

Purpose: derive the session/workstream hierarchy projection from handoff
frontmatter lineage. See ``derive.py`` for the pure transform and
``coordinator_core.ops.session_hierarchy_derive`` for the op wrapper (query +
atomic write) that composes it.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T3a-g3
Port of: derive-session-hierarchy.sh (DoE f0aa2d56, 2026-07-16)
"""

from __future__ import annotations
