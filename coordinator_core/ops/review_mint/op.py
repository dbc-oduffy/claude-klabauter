"""
coordinator_core.ops.review_mint.op — ``load_fragment()``, the one runtime
reader of coordinator-content-repo's review-roster fragment.

Resolves the sibling root via ``coordinator_core.content_root_pointer.
read_content_root_pointer()`` and joins ``contract/review-roster-fragment.json``
onto the content root — never a hardcoded cross-repo path. Registers no op.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.content_root_pointer import read_content_root_pointer
from coordinator_core._content_root_primitive import content_root_for

#: Relative to the content root, which is `<doe>/coordinator` in a private
#: clone and the checkout root itself in the flat mirror.
_REVIEW_ROSTER_FRAGMENT_RELPATH = "contract/review-roster-fragment.json"
_REVIEW_STAGE_SCHEMA_RELPATH = "schemas/review-stage.schema.json"


def load_fragment(repo_root: Optional[Path] = None) -> dict:
    """Resolve the sibling coordinator-content-repo root, read its shipped review-roster
    fragment, and return the parsed dict.

    The ONLY place in this plan's surface that touches the sibling clone at
    runtime (see module docstring). Raises ``FileNotFoundError`` if the
    sibling root does not resolve or the fragment file is absent -- never a
    silently empty/fabricated fragment.
    """
    fragment_path = _doe_content_root() / _REVIEW_ROSTER_FRAGMENT_RELPATH
    if not fragment_path.is_file():
        raise FileNotFoundError(
            f"review roster fragment not found at {fragment_path}"
        )
    return json.loads(fragment_path.read_text(encoding="utf-8"))


def _doe_content_root() -> Path:
    content_root = read_content_root_pointer()
    if not content_root:
        raise FileNotFoundError(
            "could not resolve the coordinator-content-repo sibling "
            "root (read_content_root_pointer() returned empty)"
        )
    content_root = content_root_for(content_root)
    if content_root is None:
        raise FileNotFoundError(
            f"{content_root} is neither a private clone "
            "(no coordinator/) nor a flat mirror (no plugin marker)"
        )
    return content_root


def load_stage_schemas() -> dict:
    """Return the ``$defs`` mapping of DoE's ``review-stage.schema.json``.

    Raises ``FileNotFoundError`` on an unresolved root or absent file and
    ``ValueError`` on unparseable JSON or a missing ``$defs`` mapping.
    """
    schema_path = _doe_content_root() / _REVIEW_STAGE_SCHEMA_RELPATH
    try:
        doc = json.loads(schema_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(
            f"review-stage schemas unloadable at {schema_path} ({exc})"
        ) from exc
    stage_schemas = doc.get("$defs") if isinstance(doc, dict) else None
    if not isinstance(stage_schemas, dict):
        raise ValueError(f"{schema_path} carries no `$defs` mapping")
    return stage_schemas
