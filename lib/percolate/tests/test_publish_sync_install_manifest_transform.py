"""Regression tests for `ManifestLayoutRewrite`/`apply_manifest_layout_rewrite`
— the declarative, row-supplied manifest layout-rewrite transform that folds
DoE-claude's `setup/publish_sync.py` per-root override (the coordinator
install-manifest layout transform) into this engine module —
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W3-C10 / reviewer
finding 6 (ACCEPT).

DoE's override kept that transform out of this module because it was keyed
to ONE publish row's identity (`coordinator-claude-toplevel-install`) and its
own DoE-repo-specific nested-vs-flat layout — hardcoding a row name and a
layout into a generic percolate library. `ManifestLayoutRewrite` is how a row
supplies that instead: every field — filename, the source-directory suffix
gate, the rewrite pairs, the excluded top-level fields — is caller data, so
this module holds no row name and no source-repo layout anywhere.

This file pins:
  1. `_rewrite_manifest_json_value` recurses through nested dicts/lists and
     leaves non-string leaves untouched.
  2. `apply_manifest_layout_rewrite`'s no-op contract: empty `path_rewrites`
     never even reads `dst_file`; malformed JSON and a non-object top-level
     document are reported and left untouched, never raised; excluded
     top-level fields are never touched even when they contain a matching
     substring; a real rewrite is applied and reported as changed; a rewrite
     that matches nothing reports unchanged and does not rewrite the file
     (mtime-stable).
  3. `sync_flat_mirror`'s wiring: the transform fires ONLY when a copied
     file's basename equals `manifest_layout_rewrite.filename` AND its
     `src_dir` ends with `manifest_layout_rewrite.src_dir_suffix`; it never
     fires under dry-run (dst_file is not written under dry-run at all); the
     default (`None`) is a true no-op for every existing caller, matching the
     `renamed_dir_names`/`foreign_dir_names` no-op contract `sync_mirror`
     already holds.

Loaded via the `percolate` package (§ sibling `test_publish_sync_changed_
paths_sink.py`'s own note on why a bare `spec_from_file_location` can't
resolve `publish_sync.py`'s relative `.ignore` import).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402
from percolate.ignore import PercolateIgnoreMatcher  # noqa: E402

_NO_IGNORE = PercolateIgnoreMatcher([])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, data: object) -> str:
    text = json.dumps(data, indent=2) + "\n"
    _write(path, text)
    return text


# ---------------------------------------------------------------------------
# 1 — _rewrite_manifest_json_value
# ---------------------------------------------------------------------------
def test_rewrite_value_replaces_string_leaf():
    out = publish_sync._rewrite_manifest_json_value("old/path", (("old", "new"),))
    assert out == "new/path"


def test_rewrite_value_recurses_nested_dicts_and_lists():
    value = {
        "posix": "old/setup.py",
        "nested": {"windows": "old/setup.cmd", "list": ["old/a", "old/b"]},
    }
    out = publish_sync._rewrite_manifest_json_value(value, (("old", "new"),))
    assert out == {
        "posix": "new/setup.py",
        "nested": {"windows": "new/setup.cmd", "list": ["new/a", "new/b"]},
    }


def test_rewrite_value_leaves_non_string_leaves_untouched():
    value = {"count": 3, "flag": True, "nothing": None, "ratio": 1.5}
    out = publish_sync._rewrite_manifest_json_value(value, (("old", "new"),))
    assert out == value


def test_rewrite_value_applies_pairs_in_order():
    out = publish_sync._rewrite_manifest_json_value("a", (("a", "b"), ("b", "c")))
    assert out == "c"


# ---------------------------------------------------------------------------
# 2 — apply_manifest_layout_rewrite
# ---------------------------------------------------------------------------
def test_apply_rewrite_empty_path_rewrites_is_a_true_no_op(tmp_path):
    dst_file = tmp_path / "manifest.json"
    original = _write_json(dst_file, {"a": "old/value"})
    before_mtime = dst_file.stat().st_mtime_ns

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json", src_dir_suffix="/coordinator/docs/install"
    )
    changed = publish_sync.apply_manifest_layout_rewrite(dst_file, rewrite)

    assert changed is False
    assert dst_file.read_text(encoding="utf-8") == original
    assert dst_file.stat().st_mtime_ns == before_mtime


def test_apply_rewrite_malformed_json_is_reported_and_untouched(tmp_path, capsys):
    dst_file = tmp_path / "manifest.json"
    _write(dst_file, "{not valid json")

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
    )
    changed = publish_sync.apply_manifest_layout_rewrite(dst_file, rewrite)

    assert changed is False
    assert dst_file.read_text(encoding="utf-8") == "{not valid json"
    assert "could not parse as JSON" in capsys.readouterr().err


def test_apply_rewrite_non_object_top_level_is_reported_and_untouched(tmp_path, capsys):
    dst_file = tmp_path / "manifest.json"
    original = _write_json(dst_file, ["old/value"])

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
    )
    changed = publish_sync.apply_manifest_layout_rewrite(dst_file, rewrite)

    assert changed is False
    assert dst_file.read_text(encoding="utf-8") == original
    assert "not an object" in capsys.readouterr().err


def test_apply_rewrite_excludes_named_top_level_fields(tmp_path):
    dst_file = tmp_path / "manifest.json"
    _write_json(
        dst_file,
        {
            "standalone_setup_script": {"posix": "old/setup.py"},
            "other_field": {"posix": "old/other.py"},
        },
    )
    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
        excluded_top_level_fields=frozenset({"standalone_setup_script"}),
    )
    publish_sync.apply_manifest_layout_rewrite(dst_file, rewrite)

    result = json.loads(dst_file.read_text(encoding="utf-8"))
    assert result["standalone_setup_script"]["posix"] == "old/setup.py"
    assert result["other_field"]["posix"] == "new/other.py"


def test_apply_rewrite_applies_and_reports_changed(tmp_path, capsys):
    dst_file = tmp_path / "manifest.json"
    _write_json(dst_file, {"field": "old/value"})

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
    )
    changed = publish_sync.apply_manifest_layout_rewrite(dst_file, rewrite)

    assert changed is True
    assert json.loads(dst_file.read_text(encoding="utf-8"))["field"] == "new/value"


def test_apply_rewrite_matching_nothing_reports_unchanged_and_does_not_rewrite(tmp_path):
    dst_file = tmp_path / "manifest.json"
    _write_json(dst_file, {"field": "unrelated/value"})
    before_mtime = dst_file.stat().st_mtime_ns

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
    )
    changed = publish_sync.apply_manifest_layout_rewrite(dst_file, rewrite)

    assert changed is False
    assert dst_file.stat().st_mtime_ns == before_mtime


# ---------------------------------------------------------------------------
# 3 — sync_flat_mirror wiring
# ---------------------------------------------------------------------------
def test_flat_mirror_default_manifest_layout_rewrite_is_a_true_no_op(tmp_path):
    src_dir = tmp_path / "src" / "coordinator" / "docs" / "install"
    dst_dir = tmp_path / "dst"
    _write_json(src_dir / "manifest.json", {"field": "old/value"})

    synced, removed = publish_sync.sync_flat_mirror(src_dir, dst_dir, _NO_IGNORE, dry_run=False)

    assert synced == 1
    published = json.loads((dst_dir / "manifest.json").read_text(encoding="utf-8"))
    assert published["field"] == "old/value"


def test_flat_mirror_fires_when_filename_and_suffix_both_match(tmp_path, capsys):
    src_dir = tmp_path / "src" / "coordinator" / "docs" / "install"
    dst_dir = tmp_path / "dst"
    _write_json(src_dir / "manifest.json", {"field": "old/value"})

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
    )
    synced, removed = publish_sync.sync_flat_mirror(
        src_dir, dst_dir, _NO_IGNORE, dry_run=False, manifest_layout_rewrite=rewrite
    )

    assert synced == 1
    published = json.loads((dst_dir / "manifest.json").read_text(encoding="utf-8"))
    assert published["field"] == "new/value"
    assert "TRANSFORM: manifest.json layout rewrite applied" in capsys.readouterr().err


def test_flat_mirror_does_not_fire_on_filename_mismatch(tmp_path, capsys):
    src_dir = tmp_path / "src" / "coordinator" / "docs" / "install"
    dst_dir = tmp_path / "dst"
    _write_json(src_dir / "other.json", {"field": "old/value"})

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
    )
    publish_sync.sync_flat_mirror(
        src_dir, dst_dir, _NO_IGNORE, dry_run=False, manifest_layout_rewrite=rewrite
    )

    published = json.loads((dst_dir / "other.json").read_text(encoding="utf-8"))
    assert published["field"] == "old/value"
    assert "TRANSFORM:" not in capsys.readouterr().err


def test_flat_mirror_does_not_fire_on_src_dir_suffix_mismatch(tmp_path, capsys):
    src_dir = tmp_path / "src" / "coordinator" / "docs" / "somewhere-else"
    dst_dir = tmp_path / "dst"
    _write_json(src_dir / "manifest.json", {"field": "old/value"})

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
    )
    publish_sync.sync_flat_mirror(
        src_dir, dst_dir, _NO_IGNORE, dry_run=False, manifest_layout_rewrite=rewrite
    )

    published = json.loads((dst_dir / "manifest.json").read_text(encoding="utf-8"))
    assert published["field"] == "old/value"
    assert "TRANSFORM:" not in capsys.readouterr().err


def test_flat_mirror_never_applies_transform_under_dry_run(tmp_path):
    src_dir = tmp_path / "src" / "coordinator" / "docs" / "install"
    dst_dir = tmp_path / "dst"
    _write_json(src_dir / "manifest.json", {"field": "old/value"})

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
    )
    publish_sync.sync_flat_mirror(
        src_dir, dst_dir, _NO_IGNORE, dry_run=True, manifest_layout_rewrite=rewrite
    )

    assert not (dst_dir / "manifest.json").exists()


def test_flat_mirror_excluded_top_level_fields_survive_end_to_end(tmp_path):
    src_dir = tmp_path / "src" / "coordinator" / "docs" / "install"
    dst_dir = tmp_path / "dst"
    _write_json(
        src_dir / "manifest.json",
        {
            "standalone_setup_script": {"posix": "old/setup.py"},
            "other_field": "old/value",
        },
    )

    rewrite = publish_sync.ManifestLayoutRewrite(
        filename="manifest.json",
        src_dir_suffix="/coordinator/docs/install",
        path_rewrites=(("old", "new"),),
        excluded_top_level_fields=frozenset({"standalone_setup_script"}),
    )
    publish_sync.sync_flat_mirror(
        src_dir, dst_dir, _NO_IGNORE, dry_run=False, manifest_layout_rewrite=rewrite
    )

    published = json.loads((dst_dir / "manifest.json").read_text(encoding="utf-8"))
    assert published["standalone_setup_script"]["posix"] == "old/setup.py"
    assert published["other_field"] == "new/value"
