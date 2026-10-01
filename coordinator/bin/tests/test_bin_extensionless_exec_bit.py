"""Every tracked extensionless `coordinator/bin/` entry must be indexed 100755.

The sibling ratchets (test_no_bin_polyglot_invariant, test_shebang_removal_
ordering_ratchet, test_no_bin_docstring_command_substitution) scan blob
CONTENT; none reads the file MODE. An extensionless trampoline committed at
100644 passes all of them and is dead the first time it is invoked by path,
which is how these entrypoints are called (session-reachability-cli shipped
that way on 2026-08-13).

Reads the INDEX mode via `git ls-files -s` (what the next commit ships), not
the working-tree bit, which Windows checkouts do not carry.

An entry that is legitimately invoked only through an explicit interpreter
may be listed in `_NON_EXEC_EXEMPT` with the call site that justifies it.
The 2026-10-01 census found every extensionless entry already at 100755, so
the list starts empty.
"""
from __future__ import annotations

import pytest

from ._polyglot_git_scan import _git

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_EXEC_MODE = "100755"

# path (repo-relative) -> the interpreter-invoked call site that makes 100644 safe.
_NON_EXEC_EXEMPT: dict[str, str] = {}


def _extensionless_bin_modes() -> dict[str, str]:
    out = _git("ls-files", "-s", "--", "coordinator/bin").stdout
    modes: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        rel = path[len("coordinator/bin/"):]
        if "/" in rel or "." in rel:
            continue
        modes[path] = meta.split()[0]
    return modes


def test_extensionless_bin_entries_are_executable() -> None:
    modes = _extensionless_bin_modes()
    assert modes, (
        "found zero extensionless tracked coordinator/bin entries -- scan is "
        "mis-scoped (repo root resolution), not a genuinely empty directory"
    )
    offenders = sorted(
        f"{path} ({mode})"
        for path, mode in modes.items()
        if mode != _EXEC_MODE and path not in _NON_EXEC_EXEMPT
    )
    assert not offenders, (
        "extensionless coordinator/bin entries not indexed 100755: "
        + ", ".join(offenders)
        + ". Fix: `git update-index --chmod=+x <path>`; or add the path to "
        "_NON_EXEC_EXEMPT with its interpreter-invoked call site."
    )


def test_exemptions_name_real_non_exec_entries() -> None:
    modes = _extensionless_bin_modes()
    stale = sorted(
        p for p in _NON_EXEC_EXEMPT if modes.get(p, _EXEC_MODE) == _EXEC_MODE
    )
    assert not stale, f"_NON_EXEC_EXEMPT entries that are exec or untracked: {stale}"
