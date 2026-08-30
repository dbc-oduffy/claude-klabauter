"""tests/test_coordinator_currency.py
T1-T5 (stamp write/read). T6-T10 dropped on port:

  - T6-T9 (probe classifications) are covered by claude-klabauter's native
    coordinator_core/ops/test_probe_onboarding_currency.py — the bash oracle's
    `coordinator_currency_probe` is NOT reproduced here (DR-059 fix-in-port:
    already exists and passes tests). See lib/coordinator_currency.py's module
    docstring.
  - T10 (check-schema-version-bump.sh tripwire) tested a sibling script that
    never depended on coordinator-currency.sh's functions — split out to
    bin/tests/test-check-schema-version-bump.sh (untouched port boundary).

Port of: test-coordinator-currency.sh (DoE 9cc1d315, 2026-07-21).
Port: docs/plans/2026-07-19-debash-coordinator-windows.md (chunk E3-f).
Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 1
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import coordinator_currency as cc  # noqa: E402


def _make_plugin_root(tmp_path, version: str = "1") -> str:
    plugin_root = tmp_path / "plugin_root"
    plugin_root.mkdir()
    (plugin_root / "coordinator-schema-version").write_text(f"{version}\n", encoding="utf-8")
    return str(plugin_root)


def _make_repo_root(tmp_path) -> str:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir()
    return str(repo_root)


def test_t1_write_creates_stamp_with_correct_version(tmp_path):
    repo = _make_repo_root(tmp_path)
    plugin = _make_plugin_root(tmp_path, "1")
    cc.coordinator_currency_write(repo, plugin)

    stamp = os.path.join(repo, "docs", "coordinator-currency.yaml")
    assert os.path.isfile(stamp)
    assert cc.coordinator_currency_read(repo) == "1"


def test_t2_second_write_same_version_is_byte_identical_noop(tmp_path):
    repo = _make_repo_root(tmp_path)
    plugin = _make_plugin_root(tmp_path, "1")
    cc.coordinator_currency_write(repo, plugin)

    stamp = os.path.join(repo, "docs", "coordinator-currency.yaml")
    content_before = open(stamp, encoding="utf-8").read()
    mtime_before = os.path.getmtime(stamp)

    time.sleep(1.1)  # ensure mtime would differ if the file were rewritten
    cc.coordinator_currency_write(repo, plugin)

    content_after = open(stamp, encoding="utf-8").read()
    mtime_after = os.path.getmtime(stamp)

    assert content_after == content_before, "idempotent write rewrote a file it should have no-op'd"
    assert mtime_after == mtime_before, "idempotent write touched the file even though version was unchanged"


def test_t3_write_overwrites_stamp_on_version_bump(tmp_path):
    repo = _make_repo_root(tmp_path)
    plugin_v1 = _make_plugin_root(tmp_path, "1")
    cc.coordinator_currency_write(repo, plugin_v1)

    plugin_v2 = tmp_path / "plugin_v2"
    plugin_v2.mkdir()
    (plugin_v2 / "coordinator-schema-version").write_text("2\n", encoding="utf-8")
    cc.coordinator_currency_write(repo, str(plugin_v2))

    assert cc.coordinator_currency_read(repo) == "2"


def test_t4_read_returns_stamped_version(tmp_path):
    repo2 = tmp_path / "repo2"
    (repo2 / "docs").mkdir(parents=True)
    (repo2 / "docs" / "coordinator-currency.yaml").write_text(
        "schema_version: 3\nstamped_at: 2026-05-29\n", encoding="utf-8"
    )
    assert cc.coordinator_currency_read(str(repo2)) == "3"


def test_t5_read_returns_none_on_absent_stamp(tmp_path):
    repo3 = tmp_path / "repo3"
    repo3.mkdir()
    assert cc.coordinator_currency_read(str(repo3)) is None


def test_write_raises_currency_error_on_unreadable_schema_constant(tmp_path):
    repo = _make_repo_root(tmp_path)
    plugin_no_version = tmp_path / "plugin_no_version"
    plugin_no_version.mkdir()  # deliberately no coordinator-schema-version file

    import pytest

    with pytest.raises(cc.CurrencyError):
        cc.coordinator_currency_write(repo, str(plugin_no_version))
