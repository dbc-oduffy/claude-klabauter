"""coordinator_core.bash_guards._tool_names -- the command tool-name universe.

Exports ONE constant, ``COMMAND_TOOL_NAMES``: the set of ``tool_name`` values
a PreToolUse payload for a shell-command tool can carry, as observed by this
package's own dispatcher (``dispatch.py::evaluate_payload_json``). This is
the UNIVERSE the dispatcher may see -- it is NOT a value every guard adopts.
Most guards declare their own, narrower ``MATCHERS`` tuple naming only the
command languages their own detection can actually read; a guard widened to
the full universe references this constant directly (``MATCHERS =
COMMAND_TOOL_NAMES``), never a copy or re-wrap. See ``dispatch.py``'s
``GuardEntry.matchers`` and ``_build_guard_chain`` for how a declared set
(this constant's or a guard's own subset) governs chain entry.

Negative spec: this is NOT ``write_guards/engine.py::_VALID_MATCHERS``. That
is a validation whitelist over write-shaped tools -- a different role. Do
not merge the two constants or treat one as a substitute for the other.

Underscore-prefixed to match this package's existing private-helper naming
convention (``_helpers.py``, ``_platform_verdict.py``).
"""

from __future__ import annotations

from typing import Tuple

#: The command tool-name universe -- a TUPLE, deliberately, not a list:
#: every existing ``MATCHERS`` declaration in this package is a list, so
#: this is a type change on a public-ish attribute, made so identity
#: (``is``) holds for any guard that references this constant directly and
#: no caller can mutate the shared object out from under another.
COMMAND_TOOL_NAMES: Tuple[str, ...] = ("Bash", "PowerShell")
