
from __future__ import annotations

from pathlib import Path

import pytest


def _write_node(dir_path: Path, name: str, **frontmatter) -> Path:
    lines = ["---"]
    for key, value in frontmatter.items():
        if value is None:
            lines.append(f"{key}: none")
        elif isinstance(value, list):
            rendered = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    path = dir_path / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _ledger(**entries) -> dict:
    return {tid: {"priority": prio, "target_kind": "handoff"} for tid, prio in entries.items()}

_EMIT_DIR = Path(__file__).resolve().parent.parent
_VENDOR_ROOT = _EMIT_DIR / "_vendor"
_VENDOR_SCHEMA_DIR = _VENDOR_ROOT / "cockpit-contract" / "schema"
_VENDOR_SCHEMA_BUNDLE = _VENDOR_SCHEMA_DIR / "cockpit-contract.schema.json"


def _vendor_pin_present() -> bool:
    return _VENDOR_SCHEMA_BUNDLE.exists() and any(_VENDOR_SCHEMA_DIR.glob("*.schema.json"))


@pytest.fixture(scope="session")
def requires_vendor_pin() -> None:
    if not _vendor_pin_present():
        pytest.skip(
            "Vendored cockpit-contract pin not installed — "
            "run python bin/claude-klabauter-revendor-cockpit-contract.py to populate "
            "coordinator_core/ops/emit/_vendor/ and unblock emit-dependent tests."
        )
