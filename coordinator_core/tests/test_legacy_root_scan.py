"""Tests for the legacy-root scan helper."""

from __future__ import annotations

from coordinator_core.tests._legacy_root_scan import unmarked_hits
import pytest

# The spawn is statically reachable from the code under test; tiered so a future change cannot spawn on the fast tier.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NAME = "d" + "oe-root"
_KEY = "repos." + "d" + "oe_claude"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def test_unmarked_hit_reported_and_marked_line_skipped(tmp_path):
    rel = _write(
        tmp_path, "a/mod.py",
        f"ok = 1\nbad = '{_NAME}'\nfine = '{_KEY}'  # private-name-ok: compat-fallback\nalso = '{_KEY.upper()}'\n",
    )
    hits = unmarked_hits([rel], root=tmp_path)
    assert [(p, n) for p, n, _ in hits] == [(rel, 2), (rel, 4)]


def test_excluded_dirs_ignored(tmp_path):
    rels = [_write(tmp_path, f"{d}/x.md", _NAME) for d in ("archive", "state", "docs", "tasks", ".coordinator-local", ".structural-index")]
    assert unmarked_hits(rels, root=tmp_path) == []


def test_underscore_spelling_and_clean_file(tmp_path):
    rel = _write(tmp_path, "m.py", "x = 'd" + "oe_root'\n")
    clean = _write(tmp_path, "n.py", "x = 'content_root'\n")
    assert len(unmarked_hits([rel, clean], root=tmp_path)) == 1


def test_helper_source_is_clean():
    assert unmarked_hits(["coordinator_core/tests/_legacy_root_scan.py", "coordinator_core/testing/content_root.py"]) == []


def test_a_dated_record_id_is_history_not_a_live_root(tmp_path):
    from coordinator_core.tests._legacy_root_scan import unmarked_hits

    name = "d" "oe-root"
    (tmp_path / "g.json").write_text(f'["2026-07-22-em-shim-{name}-seam-landed"]\n["{name}"]\n', encoding="utf-8")
    assert [h[1] for h in unmarked_hits(["g.json"], root=tmp_path)] == [2]
