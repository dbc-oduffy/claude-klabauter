"""dispatch.reply_page: verify a reply sidecar against its handle and return one bounded page.

Read-only and in-process: one file read and one sha256, no git, no subprocess, no cache. The
script joins the pages and checks the joined text against the handle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit import reply_sidecar
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params

GENERATES = []

_PARAMS = (Field("handle", "dict", required=True),)

__all__ = ["reply_page"]


def reply_page(root: Path, handle: object, page: object) -> dict:
    """The page reply for ``handle``, or ``{"error": str}`` on a corrupt sidecar or an out-of-range page."""
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        return {"error": f"dispatch.reply_page params.page must be a non-negative integer, got {page!r}"}
    try:
        text = reply_sidecar.read_text(root, handle)
        return reply_sidecar.page_reply(handle, text, page)
    except reply_sidecar.ReplySidecarError as exc:
        return {"error": str(exc)}
    except IndexError as exc:
        return {"error": f"dispatch.reply_page: {exc}"}


@register_op(reply_sidecar.OP_REPLY_PAGE)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.reply_page": params `handle` ({path, sha256, bytes}), `page` (int, default 0)."""
    if repo_root is None:
        return {"error": "dispatch.reply_page requires repo_root"}
    if isinstance(params, dict) and params.get("page") is None:
        params = {**params, "page": 0}
    refusal = validate_params(reply_sidecar.OP_REPLY_PAGE, params, _PARAMS)
    if refusal is not None:
        return refusal
    return reply_page(main_worktree_root(Path(repo_root)), params["handle"], params["page"])
