"""coordinator_core.op_census — per-module static summary census.

Purpose: the load-bearing infrastructure every other op_census chunk reads.
`module_summary.py` is the only file in this package that does real work;
this `__init__.py` re-exports its public surface so a sibling chunk imports
`coordinator_core.op_census` rather than reaching into the submodule by name.

Negative-spec: this package does not itself read the authoritative op
registry (`coordinator_core.ops`) — that is a sibling chunk's concern (see
`module_summary.py`'s own docstring, hard constraint 7 amended). Importing
this package never triggers an engine boot.
"""

from __future__ import annotations

from coordinator_core.op_census.module_summary import (
    ModuleSummary,
    load_index,
    save_index,
    summarize_paths,
)

__all__ = ["ModuleSummary", "load_index", "save_index", "summarize_paths"]
