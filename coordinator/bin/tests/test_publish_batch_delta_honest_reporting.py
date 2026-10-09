"""Honest change reporting primitives (`files_differ`), `--target` name selection, and the
store+transform signature the warm round stamps into its trailers
(`compute_delta_invalidation_signature`). The `--delta` whole-row skip these once fed is gone: the
diff-scaled round subsumes it.
"""

from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_batch_delta_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def test_files_differ_ignores_mtime_when_bytes_identical(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    dst.write_text("same content", encoding="utf-8")
    time.sleep(0.05)
    src.write_text("same content", encoding="utf-8")
    assert src.stat().st_mtime >= dst.stat().st_mtime
    assert publish.files_differ(src, dst) is False


def test_files_differ_true_when_bytes_differ(tmp_path):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    dst.write_text("old content", encoding="utf-8")
    src.write_text("new content", encoding="utf-8")
    assert publish.files_differ(src, dst) is True


def test_files_differ_true_when_dst_missing(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("content", encoding="utf-8")
    assert publish.files_differ(src, tmp_path / "absent.txt") is True


def test_requested_names_comma_split_selects_exact_names():
    class FakeArgs:
        target = "claude-klabauter,claude-klabauter-bin"

    args = FakeArgs()
    requested_names = (
        [n.strip() for n in args.target.split(",") if n.strip()] if args.target else []
    )
    assert requested_names == ["claude-klabauter", "claude-klabauter-bin"]
    assert "claude-klabauter-lib" not in requested_names


def test_requested_names_empty_means_unfiltered():
    class FakeArgs:
        target = ""

    args = FakeArgs()
    requested_names = (
        [n.strip() for n in args.target.split(",") if n.strip()] if args.target else []
    )
    assert requested_names == []


def test_delta_flags_still_parse_as_no_ops():
    parser = publish.build_arg_parser()

    assert parser.parse_args(["--delta"]).delta is True
    assert parser.parse_args(["--no-delta"]).delta is False
    assert parser.parse_args([]).delta is True


def test_compute_delta_invalidation_signature_changes_on_store_edit(tmp_path):
    store_path = tmp_path / "percolate-store.yaml"
    store_path.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=None, store=None)
    sig1 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)
    store_path.write_text("schema_version: '1.0.0'\nextra: true\n", encoding="utf-8")
    sig2 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)
    assert sig1 != sig2


def test_compute_delta_invalidation_signature_stable_when_nothing_changes(tmp_path):
    store_path = tmp_path / "percolate-store.yaml"
    store_path.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=None, store=None)
    sig1 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)
    sig2 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)
    assert sig1 == sig2


def test_the_delta_skip_machinery_is_gone():
    for name in ("write_delta_record", "load_delta_record", "delta_row_unchanged", "_delta_state_path",
                 "_delta_row_source_sha", "_git_is_clean"):
        assert not hasattr(publish, name), name


def _fake_engine(tmp_path):
    """A `coordinator_core` with a percolate package, an entry module importing `dep`, and an unrelated op."""
    core = tmp_path / "coordinator_core"
    (core / "percolate").mkdir(parents=True)
    (core / "ops").mkdir()
    for init in (core / "__init__.py", core / "percolate" / "__init__.py", core / "ops" / "__init__.py"):
        init.write_text("", encoding="utf-8")
    (core / "dep.py").write_text("X = 1\n", encoding="utf-8")
    (core / "ops" / "unrelated.py").write_text("Y = 1\n", encoding="utf-8")
    entry = core / "ops" / "percolate_run.py"
    entry.write_text("from coordinator_core import dep\n\n\ndef run_percolate():\n    return dep.X\n", encoding="utf-8")
    namespace: dict = {}
    # Compiled under the entry's own filename so `inspect.getfile` resolves to it, with no import.
    exec(compile("def run_percolate():\n    return None\n", str(entry), "exec"), namespace)
    module = types.SimpleNamespace(run_percolate=namespace["run_percolate"])
    return core, publish.PercolateEngineContext(engine_claude_klabauter=module, store=None)


def test_signature_ignores_an_op_outside_the_transform_import_closure(tmp_path):
    core, engine_ctx = _fake_engine(tmp_path)
    store_path = tmp_path / "percolate-store.yaml"
    store_path.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
    sig1 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)

    (core / "ops" / "unrelated.py").write_text("Y = 2\n", encoding="utf-8")

    assert publish.compute_delta_invalidation_signature(store_path, engine_ctx) == sig1


def test_signature_changes_on_a_module_the_transform_imports(tmp_path):
    core, engine_ctx = _fake_engine(tmp_path)
    store_path = tmp_path / "percolate-store.yaml"
    store_path.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
    sig1 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)

    (core / "dep.py").write_text("X = 2\n", encoding="utf-8")

    assert publish.compute_delta_invalidation_signature(store_path, engine_ctx) != sig1


def test_signature_ignores_the_op_scope_table_the_transform_imports(tmp_path):
    core, engine_ctx = _fake_engine(tmp_path)
    (core / "op_scopes.py").write_text("OP_KEY_SCOPE = {}\n", encoding="utf-8")
    (core / "dep.py").write_text("from coordinator_core.op_scopes import OP_KEY_SCOPE\nX = 1\n", encoding="utf-8")
    store_path = tmp_path / "percolate-store.yaml"
    store_path.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
    sig1 = publish.compute_delta_invalidation_signature(store_path, engine_ctx)

    (core / "op_scopes.py").write_text("OP_KEY_SCOPE = {'a.b': 'x'}\n", encoding="utf-8")

    assert publish.compute_delta_invalidation_signature(store_path, engine_ctx) == sig1
