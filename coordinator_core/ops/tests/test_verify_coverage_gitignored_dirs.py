"""verify-coverage's sweep never descends a gitignored directory, whatever it is named."""

from __future__ import annotations

import os
import subprocess

from coordinator_core.ops.verify_coverage import gitignored_dirs, walk_markdown


def test_ignored_directory_is_skipped_and_tracked_one_is_walked(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("data-hold/\n", encoding="utf-8")
    for d in ("data-hold/deep", "docs"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "data-hold" / "deep" / "stale.md").write_text("x", encoding="utf-8")
    (tmp_path / "docs" / "live.md").write_text("x", encoding="utf-8")

    ignored = gitignored_dirs(str(tmp_path))
    found = {os.path.relpath(p, tmp_path) for p in walk_markdown(str(tmp_path), ignored_dirs=ignored)}
    assert found == {os.path.join("docs", "live.md")}


def test_outside_a_checkout_nothing_is_ignored(tmp_path):
    assert gitignored_dirs(str(tmp_path / "missing")) == set()
