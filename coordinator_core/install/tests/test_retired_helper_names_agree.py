"""`_resolve_claude_klabauter.RETIRED_HELPER_STEMS` must not drift from
`substrate._KILLED_OP_ORPHAN_NAMES`.

The two sets name the same thing -- helper stems this engine deleted on
purpose -- and are duplicated rather than shared because `_resolve_claude_klabauter.py`
is copied verbatim into settings-home and runs with no `coordinator_core` on
`sys.path`. Duplication is the correct call there; unpinned duplication is
not, because the two failure modes are opposite and both silent:

- a name in the sweep set but not the resolver's: the forwarder is swept
  eventually, but until it is, its error still tells the operator to repair
  an install that is correct.
- a name in the resolver's set but not the sweep's: the message is right and
  the image is never removed, so it stays right forever.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from coordinator_core.install import substrate

_RESOLVER = (
    Path(__file__).resolve().parents[3]
    / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
)


def _load_resolver():
    spec = importlib.util.spec_from_file_location("_resolve_claude_klabauter_for_test", _RESOLVER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_retired_helper_stems_match_the_sweep_set() -> None:
    resolver = _load_resolver()
    assert resolver.RETIRED_HELPER_STEMS == substrate._KILLED_OP_ORPHAN_NAMES, (
        "_resolve_claude_klabauter.RETIRED_HELPER_STEMS and substrate._KILLED_OP_ORPHAN_NAMES "
        "have drifted — retire a helper in both, or in neither"
    )


def test_resolver_does_not_import_coordinator_core() -> None:
    """The pin above is the only link between the two modules; an actual
    import would defeat the reason the duplication exists."""
    text = _RESOLVER.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(("import coordinator_core", "from coordinator_core"))
    ]
    assert not offenders, f"_resolve_claude_klabauter.py must stay coordinator_core-free: {offenders}"
