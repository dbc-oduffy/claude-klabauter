"""A bare `coordinator-publish` round that deletes a file commits.

`publish.py :: _commit_published_dests` hands deleted paths to `commit_paths`,
which refuses an undeclared staged deletion. Its subject never mentioned one,
so every deleting round left the mirror dirty and exited 3 (doe-claude-4d,
2026-09-23: `bin/lib/model-pricing.json` on coordinator-claude).

Run: python -m pytest coordinator/bin/tests/test_publish_sync_commit_declares_removals.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from coordinator_core.git import action_guard

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("publish_sync_removals_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load()


def test_a_deleting_round_passes_the_undeclared_deletion_guard():
    deleted = ["bin/lib/model-pricing.json"]
    message = publish._sync_commit_message(
        "coordinator-claude", ["coordinator_claude"], ["bin/a.py"], deleted, " [source-head 0123456789ab]"
    )

    action_guard.assert_no_undeclared_staged_deletion(deleted, message)
    assert "Removed: bin/lib/model-pricing.json" in message
    assert "1 added-or-updated, 1 removed" in message.splitlines()[0]
    assert message.splitlines()[0].endswith(" [source-head 0123456789ab]")


def test_a_round_without_deletions_has_no_body():
    message = publish._sync_commit_message("dest", [], ["a.txt", "b.txt"], [], "")

    assert message == "percolate: sync 2 path(s) to dest (no named rows; 2 added-or-updated, 0 removed)"


def test_removed_names_are_capped_and_the_rest_counted():
    deleted = [f"gone/{i:03d}.txt" for i in range(publish._REMOVED_NAME_CAP + 5)]
    message = publish._sync_commit_message("dest", ["row"], [], deleted, "")

    assert message.count("Removed: ") == publish._REMOVED_NAME_CAP
    assert message.endswith("...and 5 more removed")
