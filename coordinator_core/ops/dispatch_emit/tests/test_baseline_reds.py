"""Tests for baseline_reds: manifest loading fails closed to empty; the JS literal carries the ids."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import baseline_reds as br

EMPTY = {"test_ids": [], "errors": []}


def test_loads_manifest(tmp_path):
    (tmp_path / br.MANIFEST_NAME).write_text(
        json.dumps({"test_ids": ["a/t.py::x"], "errors": ["NotInGitWorktree"]}), encoding="utf-8"
    )
    assert br.load_manifest(tmp_path) == {"test_ids": ["a/t.py::x"], "errors": ["NotInGitWorktree"]}


@pytest.mark.parametrize(
    "text",
    ["{not json", "[]", '"s"', '{"test_ids": "a", "errors": 3}', "null"],
)
def test_malformed_reads_empty(tmp_path, text):
    (tmp_path / br.MANIFEST_NAME).write_text(text, encoding="utf-8")
    assert br.load_manifest(tmp_path) == EMPTY


def test_missing_reads_empty(tmp_path):
    assert br.load_manifest(tmp_path) == EMPTY


def test_non_string_items_dropped(tmp_path):
    (tmp_path / br.MANIFEST_NAME).write_text(
        json.dumps({"test_ids": ["ok", 3, None], "errors": [["x"]]}), encoding="utf-8"
    )
    assert br.load_manifest(tmp_path) == {"test_ids": ["ok"], "errors": []}


def test_manifest_js_declares_literal_and_helper(tmp_path):
    (tmp_path / br.MANIFEST_NAME).write_text(
        json.dumps({"test_ids": ["b.py::t", "a.py::t"], "errors": ["NotInGitWorktree"]}),
        encoding="utf-8",
    )
    js = br.manifest_js(tmp_path)
    assert js.startswith(
        'const _baselineReds = {"errors": ["NotInGitWorktree"], "test_ids": ["b.py::t", "a.py::t"]};'
    )
    assert "_isBaselineRed = (id, err)" in js
    assert ".replace(/\\\\/g, '/')" in js


def test_manifest_js_empty_when_missing(tmp_path):
    js = br.manifest_js(tmp_path)
    assert 'const _baselineReds = {"errors": [], "test_ids": []};' in js
    assert "_isBaselineRed" in js
