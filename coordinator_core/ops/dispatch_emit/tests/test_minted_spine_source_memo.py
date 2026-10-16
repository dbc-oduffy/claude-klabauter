"""Minted M and XS spines stamp `source_memo` from the sizing's named inbox memo."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.sizing_m_mint import mint_m_spine
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine

MEMO = "state/cross-repo/inbox/x.md"


def _sizing(source_text: str) -> dict:
    return {
        "intent": "do the thing",
        "exit_criterion": {"statement": "it works"},
        "requirement_register": {"rows": [{"id": "R1", "source_text": source_text}]},
    }


def _fm(text: str) -> dict:
    return yaml.safe_load(text.split("---\n")[1])


def _m(sizing: dict) -> dict:
    rows = [{"statement": "s", "check": "c", "writes": ["a/b.py"]}]
    text, _ = mint_m_spine(sizing, sizing_rel="state/sizings/z.yaml", rows=rows)
    return _fm(text)


def _xs(sizing: dict, tmp_path: Path) -> dict:
    text, _ = mint_xs_spine(sizing, sizing_rel="state/sizings/z.yaml", writes=["a/b.py"], out_dir=tmp_path)
    return _fm(text)


def test_m_spine_stamps_source_memo(tmp_path):
    assert _m(_sizing(f"fix it and close {MEMO} on landing"))["source_memo"] == MEMO


def test_xs_spine_stamps_source_memo(tmp_path):
    assert _xs(_sizing(f"fix it and close {MEMO} on landing"), tmp_path)["source_memo"] == MEMO


def test_backslash_spelling_is_stamped_posix(tmp_path):
    assert _m(_sizing("close state\\cross-repo\\inbox\\x.md"))["source_memo"] == MEMO


def test_no_memo_mints_no_key(tmp_path):
    assert "source_memo" not in _m(_sizing("nothing here"))
    assert "source_memo" not in _xs(_sizing("nothing here"), tmp_path)


def test_two_distinct_memos_mint_no_key(tmp_path):
    s = _sizing(f"{MEMO} and state/cross-repo/inbox/y.md")
    assert "source_memo" not in _m(s)
    assert "source_memo" not in _xs(s, tmp_path)
