"""A `--slug` copied off a dated stem never doubles the plan's date prefix (example-game-repo plan-blitz
20261101T185048Z minted docs/plans/2026-11-01-2026-11-01-footballer-02-...)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load():
    loader = importlib.machinery.SourceFileLoader("coordinator_doc_new_no_double_date", str(_BIN_DIR / "coordinator-doc-new.py"))
    spec = importlib.util.spec_from_loader("coordinator_doc_new_no_double_date", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load()


def test_a_dated_slug_gets_one_date_prefix(monkeypatch):
    monkeypatch.setattr(_MOD, "_today", lambda: "2026-11-01")
    monkeypatch.setattr(_MOD, "_bootstrap_engine", lambda: None)
    out = _MOD._default_output_path("plan", "t", None, slug_override="2026-11-01-footballer-04-mapping")
    assert Path(out).as_posix() == "docs/plans/2026-11/2026-11-01-footballer-04-mapping.md"


def test_an_undated_slug_is_unchanged(monkeypatch):
    monkeypatch.setattr(_MOD, "_today", lambda: "2026-11-01")
    monkeypatch.setattr(_MOD, "_bootstrap_engine", lambda: None)
    out = _MOD._default_output_path("plan", "t", None, slug_override="footballer-03-reach")
    assert Path(out).as_posix() == "docs/plans/2026-11/2026-11-01-footballer-03-reach.md"
