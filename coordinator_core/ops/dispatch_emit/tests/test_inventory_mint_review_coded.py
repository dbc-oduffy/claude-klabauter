"""mint_spine keeps a `coded` plan chunk that a review-only request names, and drops it otherwise."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im

_HEAD = (
    "---\nrun_id: 20261019T000000-rev\n---\n\n## Chunk table\n\n"
    "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
    "|---|---|---|---|---|---|---|---|\n"
)
_PLAN = (
    "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: one\n  disposition: coded\n  writes:\n    - src/c1.py\n"
    "- id: C2\n  title: two\n  disposition: open\n  writes:\n    - src/c2.py\n```\n"
)


def _inventory(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "p.md").write_text(_PLAN, encoding="utf-8", newline="\n")
    inv = tmp_path / "state" / "mise-inventory" / "inv.md"
    inv.write_text(
        _HEAD + "| eet-06 | `docs/plans/p.md` | s | `src/c1.py` | — | v | S | pending |\n",
        encoding="utf-8",
    )
    return inv


def _ids(text):
    return [r["id"] for r in yaml.safe_load(text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]) or []]


def test_coded_chunk_is_absent_without_a_review_request(tmp_path):
    text, _ = im.mint_spine(str(_inventory(tmp_path)))
    assert _ids(text) == ["eet-06.C2"]


def test_review_request_keeps_the_named_coded_chunk(tmp_path):
    text, _ = im.mint_spine(str(_inventory(tmp_path)), review_rows=frozenset({"eet-06.C1"}))
    assert "eet-06.C1" in _ids(text)
