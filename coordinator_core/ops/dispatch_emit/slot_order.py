
from __future__ import annotations

import re
from pathlib import Path


def slot_order(run_dir: Path, rec: Path):
    parts = rec.relative_to(run_dir).parts
    slot = parts[0] if len(parts) > 1 else ""
    if not slot:
        return (0, 0, "")
    m = re.match(r"wave-(\d+)-", slot)
    return (1, int(m.group(1)), slot) if m else (2, 0, slot)
