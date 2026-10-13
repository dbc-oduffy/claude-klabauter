"""SETTLED.md skeleton for a canvas harvest: unsettled until the EM writes a list item."""

from pathlib import Path

from coordinator_core.canvas_harvest import model
from coordinator_core.session.claimed_write import create_exclusive

SETTLED_NAME = "SETTLED.md"
HEADING = "## Thread-settled behaviour"
NO_THREADS_ITEM = "- [no-threads]"


def settled_skeleton(thread_ids) -> str:
    """The skeleton text. With ids it holds no line starting `- ` or `* `, so the check reads it unsettled."""
    if not thread_ids:
        return f"{HEADING}\n\n{NO_THREADS_ITEM}\n"
    lines = [HEADING, "", "<!--", "Threads to settle:"]
    lines += [f"  {tid}" for tid in thread_ids]
    lines += [
        "Item grammar, one per decision, written below this comment:",
        "  [item] [thread:<id>] <one decision>  (prefix each with a list dash)",
        "-->",
    ]
    return "\n".join(lines) + "\n"


def write_settled_skeleton(lifted, out_dir) -> bool:
    """Write SETTLED.md in `out_dir` only when absent; True when written, False when left as it was."""
    comments = lifted.comments
    ids = model.thread_ids(comments.threads) if comments is not None else []
    try:
        create_exclusive(Path(out_dir) / SETTLED_NAME, settled_skeleton(ids))
    except FileExistsError:
        return False
    return True
