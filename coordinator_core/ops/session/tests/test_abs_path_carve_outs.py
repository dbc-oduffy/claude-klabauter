"""Tests for the abs-path-ok carve-out registry loader and marker grammar."""
from __future__ import annotations

from coordinator_core.ops.session.abs_path_carve_outs import (
    CODE_SURFACE_EXTS,
    CARVE_OUTS_FILENAME,
    load_carve_outs,
    marker_is_discharged,
)

TOML = """
[registry_keys]
keys = ["data_root"]

[[carve_out]]
id = "fixtures"
reason = "test fixture"
paths = ["a/b.py", "g/*.py"]
sites = [{path = "c/d.py", line = 'X = "C:/data"  # abs-path-ok: legacy'}]

[[carve_out]]
id = "noreason"
reason = ""
paths = ["z.py"]
"""


def _load(tmp_path, text=TOML):
    (tmp_path / CARVE_OUTS_FILENAME).write_text(text, encoding="utf-8")
    return load_carve_outs(tmp_path)


def test_missing_file_is_empty(tmp_path):
    co = load_carve_outs(tmp_path)
    assert not co.keys and not co.by_id and not co.sites and co.malformed == ""


def test_malformed_file_is_empty_with_sentinel(tmp_path):
    co = _load(tmp_path, "this = [not toml")
    assert not co.keys and not co.by_id and co.malformed


def test_loads_entries_and_rejects_glob_and_reasonless(tmp_path):
    co = _load(tmp_path)
    assert co.keys == frozenset({"data_root"})
    assert co.by_id["fixtures"] == frozenset({"a/b.py"})
    assert "noreason" not in co.by_id
    assert any("glob" in r for r in co.rejected)
    assert any("missing reason" in r for r in co.rejected)


def test_key_marker(tmp_path):
    co = _load(tmp_path)
    assert marker_is_discharged('X = "C:/d"  # abs-path-ok: key=data_root', "any.py", co)
    assert not marker_is_discharged('X = "C:/d"  # abs-path-ok: key=unlisted', "any.py", co)


def test_carve_out_marker_needs_listed_path(tmp_path):
    co = _load(tmp_path)
    line = 'X = "C:/d"  # abs-path-ok: carve-out=fixtures'
    assert marker_is_discharged(line, "a/b.py", co)
    assert marker_is_discharged(line, "a\\b.py", co)
    assert not marker_is_discharged(line, "other.py", co)
    assert not marker_is_discharged('# abs-path-ok: carve-out=nope', "a/b.py", co)


def test_bare_reason_only_at_listed_site(tmp_path):
    co = _load(tmp_path)
    listed = 'X = "C:/data"  # abs-path-ok: legacy'
    assert marker_is_discharged("   " + listed + "  ", "c/d.py", co)
    assert not marker_is_discharged(listed, "other.py", co)
    assert not marker_is_discharged('Y = "C:/new"  # abs-path-ok: legacy', "c/d.py", co)
    assert not marker_is_discharged('Y = "C:/new"  # abs-path-ok:', "c/d.py", co)


def test_bare_reason_without_registry_not_discharged(tmp_path):
    assert not marker_is_discharged('X  # abs-path-ok: because', "a.py", load_carve_outs(tmp_path))


def test_code_surface_exts():
    assert ".py" in CODE_SURFACE_EXTS and ".md" not in CODE_SURFACE_EXTS
