"""Probe: every DoE `coordinator/bin/*.py` the engine also owns must stay a thin forwarder.

Engine-native port of DoE's `check-bin-forwarders.py`. The name set is derived, not frozen:
each `*.py` in the DoE clone's `coordinator/bin` whose name is also an entry in the engine's
`coordinator/bin`, bar the names in `DOE_LIVE`. Each must reference `_engine_forward` and carry
exactly the canonical forwarder body. Exit 1 listing offenders, 0 when clean or when the DoE clone is unresolvable
(one note line, never a crash).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.plugin_health.forwarder_drift import _resolve_agent_bin, _resolve_content_root

_PROG = "doe-bin-forwarders"

# DoE's hooks/scripts/group-em-autofire.py loads this file by path and calls its functions, so a
# forwarder cannot stand in for it. A name leaves this set only once the engine/DoE split is reconciled.
DOE_LIVE = frozenset({"group-em-box-notice.py"})

CANONICAL_CODE = (
    "import sys",
    "from pathlib import Path",
    'sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))',
    "from _engine_forward import forward",
    "sys.exit(forward(__file__))",
)


def _code_lines(text: str) -> Tuple[str, ...]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return tuple(out)


def derive_names(doe_bin: Path, engine_bin: Path) -> List[str]:
    """Sorted `*.py` names present in both bins, excluding `DOE_LIVE`."""
    return sorted(
        p.name
        for p in doe_bin.glob("*.py")
        if p.name not in DOE_LIVE and p.is_file() and (engine_bin / p.name).exists()
    )


def offenders(doe_bin: Path, engine_bin: Path) -> List[str]:
    """One line per derived name that is not a canonical forwarder."""
    bad: List[str] = []
    for name in derive_names(doe_bin, engine_bin):
        try:
            text = (doe_bin / name).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            bad.append(f"{name}: unreadable ({exc.__class__.__name__})")
            continue
        if "_engine_forward" not in text:
            bad.append(f"{name}: does not reference _engine_forward")
        elif _code_lines(text) != CANONICAL_CODE:
            bad.append(f"{name}: carries logic beyond the forwarder body")
    return bad


def run(*, content_root: Optional[Path] = None, engine_bin: Optional[Path] = None) -> Tuple[int, List[str]]:
    """Return (exit code, output lines). Unresolvable inputs yield rc 0 with a note."""
    root = content_root if content_root is not None else _resolve_content_root()
    eng = engine_bin if engine_bin is not None else _resolve_agent_bin()
    doe_bin = root / "coordinator" / "bin" if root is not None else None
    if doe_bin is None or not doe_bin.is_dir() or eng is None:
        return 0, [f"[skip] {_PROG}: DoE clone or engine coordinator/bin unresolvable - nothing to check"]
    bad = offenders(doe_bin, eng)
    if bad:
        return 1, ["bin forwarder drift:"] + [f"  {line}" for line in bad]
    return 0, []


def main(argv: List[str]) -> int:
    del argv
    rc, lines = run()
    for line in lines:
        print(line, file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
