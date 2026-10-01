"""coordinator_core.completion_receipts.model — receipt id, path rule, render and validate.

A completion receipt is `state/completion-receipts/<YYYY-MM>/<receipt_id>.md`: LF-only YAML
frontmatter validated in-process against the vendored `completion-receipt` schema, then a prose
body. Every other receipt module imports this one and never re-derives the shape.
"""

from __future__ import annotations

import re
import secrets

import yaml

RECEIPTS_DIR = "state/completion-receipts"
TSHIRTS = ("XS", "S", "M", "L", "XL", "XXL")

_SCHEMA_NAME = "completion-receipt"
_SLUG_MAX = 40
_MONTH_RE = re.compile(r"^\d{4}-\d{2}")


def mint_receipt_id(slug_source: str) -> str:
    """`rcp-<slug>-<6hex>`; the slug is `slug_source` lowered to `[a-z0-9-]`, never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")[:_SLUG_MAX].strip("-")
    return f"rcp-{slug or 'run'}-{secrets.token_hex(3)}"


def receipt_rel_path(receipt_id: str, concluded_at: str) -> str:
    """Repo-relative path `RECEIPTS_DIR/<YYYY-MM>/<id>.md`, month taken from `concluded_at`."""
    if not _MONTH_RE.match(concluded_at or ""):
        raise ValueError(f"concluded_at is not an ISO timestamp: {concluded_at!r}")
    return f"{RECEIPTS_DIR}/{concluded_at[:7]}/{receipt_id}.md"


def render(fm: dict, prose: str) -> str:
    """LF-only frontmatter block followed by the prose body."""
    block = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)
    body = prose.replace("\r\n", "\n").strip("\n")
    return f"---\n{block}---\n\n{body}\n"


def validate(fm: dict) -> list[str]:
    """`[]` iff `fm` is valid against the vendored completion-receipt schema."""
    from coordinator_core.frontmatter.schema_validate import validate as schema_validate

    result = schema_validate(_SCHEMA_NAME, fm)
    if result.get("ok"):
        return []
    return [
        f"{e.get('field', '?')}: {e.get('error', '')}".rstrip(": ")
        for e in result.get("errors") or []
    ] or ["schema validation failed"]
