"""Per-repo engine record directory under the settings home (off the repo's `.git`).

`engine_record_dir` is where hot engine records (live-run index, run tokens) live;
`legacy_record_dir` is the old in-`.git` home, kept for read fallback. Neither creates
a directory: callers mkdir.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

_KEY_LEN = 16


def legacy_record_dir(repo_root: Path) -> Optional[Path]:
    """The git common dir that held engine records before the move; None when `repo_root` has none."""
    from coordinator_core.ops.dispatch_emit.run_token import _common_dir

    return _common_dir(Path(repo_root))


def engine_record_dir(repo_root: Path) -> Optional[Path]:
    """`<settings home>/machine-local/engine-records/<key>`, `key` = short sha256 of the resolved
    git common dir (shared by every worktree of one repo); None when `repo_root` has no common dir."""
    common = legacy_record_dir(repo_root)
    if common is None:
        return None
    from coordinator_core import _settings_home

    try:
        resolved = common.resolve()
    except OSError:
        resolved = common
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:_KEY_LEN]
    return _settings_home.machine_local_dir() / "engine-records" / key
