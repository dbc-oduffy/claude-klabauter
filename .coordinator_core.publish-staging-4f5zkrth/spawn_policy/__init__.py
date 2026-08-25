"""One AST-based definition of a shell-shaped spawn.

The single source of truth every consumer (census, test gate, lint CLI,
write-time guard) imports rather than re-deriving the rule. See
`tasks/shell-spawn-regrowth-gate/PINNED-API.md` for the frozen contract.
"""

from .detect import (
    ExcludedReport,
    SpawnKind,
    SpawnParseError,
    SpawnSite,
    is_test_tree_site,
    site_key,
    sites_in_source,
    walk_repo,
)
from .allowlist import (
    AllowlistEntry,
    DEFAULT_CARVE_OUTS_DOC,
    is_sanctioned,
    load_allowlist,
    unpinned_entries,
)

__all__ = [
    "SpawnKind",
    "SpawnSite",
    "SpawnParseError",
    "ExcludedReport",
    "sites_in_source",
    "walk_repo",
    "site_key",
    "is_test_tree_site",
    "AllowlistEntry",
    "DEFAULT_CARVE_OUTS_DOC",
    "load_allowlist",
    "is_sanctioned",
]
