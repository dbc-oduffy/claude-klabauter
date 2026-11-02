"""Generate the op catalog, the MCP tool definitions and the op reference doc.

Every output is rendered from `op_contract.build_contract` alone, one contract per
OP_MODULE_MAP key. `--write` regenerates the three files; `--check` renders in
memory and exits 1 naming each file that differs from disk.

Traps: MCP tool names map `.` to `__`; the mapping must stay injective, and
`render_all` raises on a collision. Output bytes are sorted and `\\n`-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

from coordinator_core.contract.op_contract import build_contract
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_REPO = Path(__file__).resolve().parents[2]
OUTPUTS: Dict[str, Path] = {
    "op-catalog.json": _REPO / "coordinator_core" / "contract" / "op-catalog.json",
    "mcp-tools.json": _REPO / "coordinator_core" / "contract" / "mcp-tools.json",
    "op-catalog.md": _REPO / "docs" / "reference" / "op-catalog.md",
}
_GENERATOR = "python -m coordinator_core.contract.emit_op_catalog --write"
_NAME_MAPPING = "op name with '.' replaced by '__'"


def mcp_tool_name(op: str) -> str:
    return op.replace(".", "__")


def _json(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _contracts() -> List[dict]:
    import coordinator_core.ops as ops

    ops._eager_import_all()
    return [build_contract(op).to_dict() for op in sorted(OP_MODULE_MAP)]


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_all() -> Dict[str, str]:
    """Return {output key: file text} for the three generated files."""
    contracts = _contracts()
    tools = []
    seen: Dict[str, str] = {}
    for c in contracts:
        tool = mcp_tool_name(c["name"])
        if tool in seen:
            raise ValueError(
                f"MCP tool name collision: {seen[tool]!r} and {c['name']!r} both map to {tool!r}"
            )
        seen[tool] = c["name"]
        tools.append(
            {"name": tool, "description": c["description"], "inputSchema": c["params_schema"]}
        )

    catalog = {"generated_by": _GENERATOR, "ops": contracts}
    mcp = {"generated_by": _GENERATOR, "name_mapping": _NAME_MAPPING, "tools": tools}

    lines = [
        "<!-- GENERATED FILE: do not edit. Regenerate with:",
        f"     {_GENERATOR} -->",
        "",
        "# Op catalog",
        "",
        f"{len(contracts)} ops, rendered from `build_contract` (one contract per op).",
        "",
        "Known gap: per-op `params_schema` population is deferred. Every `inputSchema` in",
        '`mcp-tools.json` is the placeholder `{"type": "object"}` and does not describe parameters.',
        "",
        "| Op | Class | Timeout (s) | Module | Summary |",
        "|---|---|---|---|---|",
    ]
    for c in contracts:
        lines.append(
            f"| `{c['name']}` | {c['op_class']} | {c['timeout_s']:g} | "
            f"`{c['module']}` | {_md_cell(c['summary'])} |"
        )
    return {
        "op-catalog.json": _json(catalog),
        "mcp-tools.json": _json(mcp),
        "op-catalog.md": "\n".join(lines) + "\n",
    }


def _disk(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8")


def drifted(rendered: Dict[str, str]) -> List[str]:
    return [k for k, text in rendered.items() if _disk(OUTPUTS[k]) != text]


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="emit_op_catalog")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    rendered = render_all()
    if args.write:
        for key, text in rendered.items():
            OUTPUTS[key].parent.mkdir(parents=True, exist_ok=True)
            with open(OUTPUTS[key], "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        return 0
    bad = drifted(rendered)
    for key in bad:
        print(f"drift: {OUTPUTS[key]}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
