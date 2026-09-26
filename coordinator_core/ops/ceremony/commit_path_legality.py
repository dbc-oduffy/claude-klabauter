"""coordinator_core.ops.ceremony.commit_path_legality -- refuses an engine commit
that would land a path Windows cannot check out.

`write_guards.block_illegal_filename` denies such a name on a Write/Edit tool
call. An op writes files in-process and makes no tool call, and every engine
commit lands through `_commit_via_head_spine` or its plumbing ladder, neither of
which runs a git hook -- so an op that minted a name from a title (a colon, a
trailing dot) reached the tree unchecked, and every Windows clone of it then
fails to check out. `_commit_via_head_spine` calls `illegal_path_refusal` beside
the doctrine-surface admission check, where a refusal also stops the ladder.

Every component of the path is checked, not only the leaf: a directory named
`a:b` breaks a checkout as surely as a file. Deletions are never refused --
removing an illegal path is the fix.

Negative-spec:
    Does NOT sanitize or rename; it refuses and names a safe alternative.
    Does NOT read the working tree or spawn.
    Honours `COORDINATOR_OVERRIDE_ILLEGAL_FILENAME=1`, the same key the
    Write/Edit guard honours, so the two routes agree on one name.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

from coordinator_core.bash_guards._helpers import csn_check
from coordinator_core.write_guards.block_illegal_filename import _OVERRIDE_ENV, _safe_suggestion


def illegal_path_refusal(assembled: Mapping[str, object]) -> Optional[str]:
    if os.environ.get(_OVERRIDE_ENV) == "1":
        return None
    refusals = []
    for path, entry in assembled.items():
        if not (isinstance(entry, tuple) and len(entry) == 2):
            continue
        for component in path.split("/"):
            hint = csn_check(component)
            if hint is not None:
                refusals.append(
                    f"{path}: component '{component}' carries a {hint!r}, which breaks "
                    f"Windows checkout. Use instead: `{_safe_suggestion(component)}`."
                )
                break
    return "\n".join(refusals) if refusals else None
