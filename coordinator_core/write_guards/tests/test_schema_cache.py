"""Behavior of the shared process-level schema cache."""

from __future__ import annotations

import os

import pytest

from coordinator_core.frontmatter.schema_validate import load_schemas
from coordinator_core.write_guards import _schema_cache as cache

_SCHEMA = "schema: demo\napplies_to: 'docs/*.md'\nfields:\n  title:\n    type: string\n"


@pytest.fixture(autouse=True)
def _clear():
    cache.clear()
    yield
    cache.clear()


def _dir(tmp_path):
    d = tmp_path / "schemas"
    d.mkdir()
    (d / "demo.yaml").write_text(_SCHEMA, encoding="utf-8")
    return str(d)


def test_second_call_with_unchanged_dir_opens_no_schema_file(tmp_path):
    d = _dir(tmp_path)
    first = cache.cached_load_schemas(d, load_schemas)
    assert "demo" in first

    def loader(_d):
        raise AssertionError("loader (every schema-file open) must not run on an unchanged dir")

    second = cache.cached_load_schemas(d, loader)
    assert second["demo"] == first["demo"]


def test_changed_content_reloads(tmp_path):
    d = _dir(tmp_path)
    calls = []

    def loader(x):
        calls.append(x)
        return load_schemas(x)

    cache.cached_load_schemas(d, loader)
    p = os.path.join(d, "demo.yaml")
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("  extra:\n    type: string\n")
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    out = cache.cached_load_schemas(d, loader)
    assert len(calls) == 2
    assert "extra" in str(out["demo"])


def test_added_file_reloads(tmp_path):
    d = _dir(tmp_path)
    calls = []

    def loader(x):
        calls.append(x)
        return load_schemas(x)

    cache.cached_load_schemas(d, loader)
    with open(os.path.join(d, "other.yaml"), "w", encoding="utf-8") as fh:
        fh.write("schema: other\napplies_to: 'x/*.md'\nfields:\n  a:\n    type: string\n")
    out = cache.cached_load_schemas(d, loader)
    assert len(calls) == 2
    assert "other" in out


def test_caller_mutation_does_not_leak_into_cache(tmp_path):
    d = _dir(tmp_path)
    first = cache.cached_load_schemas(d, load_schemas)
    first["_byGlob"].clear()
    first["_byKind"]["x"] = "y"
    first["injected"] = {}
    second = cache.cached_load_schemas(d, load_schemas)
    assert second["_byGlob"]
    assert "x" not in second["_byKind"]
    assert "injected" not in second


def test_loader_failure_propagates_and_is_not_cached(tmp_path):
    d = _dir(tmp_path)

    def broken(_d):
        raise ValueError("torn")

    with pytest.raises(ValueError):
        cache.cached_load_schemas(d, broken)
    assert "demo" in cache.cached_load_schemas(d, load_schemas)


def test_missing_dir_raises(tmp_path):
    with pytest.raises(OSError):
        cache.cached_load_schemas(str(tmp_path / "nope"), load_schemas)
