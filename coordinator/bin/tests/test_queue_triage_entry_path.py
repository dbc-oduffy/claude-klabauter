"""test_queue_triage_entry_path.py -- C19 regression test for
`coordinator/bin/queue-triage.py scaffold-baton --entry-path` without
`--title`.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C19.md
(D6 sweep finding).

D6 claimed `--entry-path X` with no `--title` refuses every time, because
`_build_scaffold_params` emits `params["entry"] = {"path": ...}` with no
`title` key, and the op's `_extract_items` reads `item["title"]` as the
default title.

Live reproduction (run before writing this test, per the brief) shows this
does NOT reproduce against the current tree: `handoff.scaffold_from_queue`'s
`_normalize_item` (coordinator_core/ops/queue_scaffold_baton.py) already
falls back `title = raw.get("title") or item_id or path`, and `item_id`
itself falls back to `Path(path).stem` when `id` is absent -- so a
CLI-built entry with only `path` still derives a non-empty title as long as
`path` is non-empty, which `_entry_path_for` guarantees for every
`--entry-path` invocation (bare filename or already-qualified). This test
locks that behaviour at both layers -- the CLI's params shape (no `title`
key on the entry leg) and the op's derivation (`_extract_items` never
returns an empty default title for a well-formed entry) -- so a future
regression in either layer is caught rather than silently reintroducing the
100%-refusal dead CLI path.

Negative-spec: this module does not invoke the CLI's `main()` end-to-end
(that would perform a REAL, MUTATING `handoff.scaffold_from_queue` write via
the live engine) -- it exercises `_build_scaffold_params` and the op's own
`_extract_items`/`_normalize_item` helpers directly, which is where D6's
claimed defect actually lives.

Run: python -m pytest coordinator/bin/tests/test_queue_triage_entry_path.py -q
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_cli_module():
    lib_dir = str(_BIN_DIR / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    engine_root = str(_REPO_ROOT)
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    spec = importlib.util.spec_from_file_location("queue_triage_cli", _BIN_DIR / "queue-triage.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def cli_mod():
    return _load_cli_module()


@pytest.fixture(scope="module")
def op_mod():
    from coordinator_core.ops import queue_scaffold_baton

    return queue_scaffold_baton


def _scaffold_args(**overrides) -> argparse.Namespace:
    base = dict(
        family="misc",
        entry_path=None,
        cluster_json=None,
        title=None,
        branch=None,
        kind=None,
        workstream=None,
        body=None,
        origin_plan_id=None,
        origin_goal_id=None,
        match_text=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestBuildScaffoldParamsEntryLeg:
    def test_bare_filename_no_title_builds_entry_without_title_key(self, cli_mod):
        args = _scaffold_args(entry_path="plainfile.md")
        params = cli_mod._build_scaffold_params(args)
        assert params["entry"] == {"path": "state/misc/plainfile.md"}
        assert "title" not in params

    def test_already_qualified_path_no_title_builds_entry_without_title_key(self, cli_mod):
        args = _scaffold_args(entry_path="foo/bar/test-entry.md")
        params = cli_mod._build_scaffold_params(args)
        assert params["entry"] == {"path": "foo/bar/test-entry.md"}
        assert "title" not in params

    def test_explicit_title_is_forwarded_at_top_level(self, cli_mod):
        args = _scaffold_args(entry_path="plainfile.md", title="Explicit Title")
        params = cli_mod._build_scaffold_params(args)
        assert params["entry"] == {"path": "state/misc/plainfile.md"}
        assert params["title"] == "Explicit Title"


class TestOpDerivesTitleFromEntryPathAlone:
    """Locks the op-side fallback the CLI's entry-only params rely on."""

    def test_extract_items_derives_nonempty_title_from_bare_filename_entry(self, op_mod):
        params = {"entry": {"path": "state/misc/plainfile.md"}}
        items, default_title, err = op_mod._extract_items(params)
        assert err is None
        assert default_title == "plainfile"
        assert items[0]["title"] == "plainfile"

    def test_extract_items_derives_nonempty_title_from_qualified_path_entry(self, op_mod):
        params = {"entry": {"path": "foo/bar/test-entry.md"}}
        items, default_title, err = op_mod._extract_items(params)
        assert err is None
        assert default_title == "test-entry"
        assert items[0]["title"] == "test-entry"

    def test_final_title_resolution_is_nonempty_with_no_title_param(self, op_mod):
        """Mirrors the handler's own `title = (params.get("title") or default_title
        or "").strip()` step -- the exact line D6 claims goes empty."""
        params = {"entry": {"path": "state/misc/plainfile.md"}}
        _items, default_title, err = op_mod._extract_items(params)
        assert err is None
        title = (params.get("title") or default_title or "").strip()
        assert title == "plainfile"
