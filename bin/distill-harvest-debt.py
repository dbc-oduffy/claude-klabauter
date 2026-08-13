"""Deprecated location — forwards to coordinator/bin/distill-harvest-debt.py.

DEC-3 (2026-07-23 claude-klabauter-driven-ceremony-redesign): the distill CLIs relocated to
coordinator/bin/ conventions (discoverability + Windows .cmd twins). This forwarder
keeps `bin/distill-harvest-debt.py` working for one release while example-doctrine-repo's PIPELINE.md
is repointed (M1); delete once that repoint lands.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.stderr.write(
    "bin/distill-harvest-debt.py is deprecated — use "
    "coordinator/bin/distill-harvest-debt.py instead.\n"
)
_target = Path(__file__).resolve().parent.parent / "coordinator" / "bin" / "distill-harvest-debt.py"
_spec = importlib.util.spec_from_file_location("_distill_harvest_debt_relocated", _target)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod  # register before exec
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

if __name__ == "__main__":
    sys.exit(_mod.main(sys.argv[1:]))
