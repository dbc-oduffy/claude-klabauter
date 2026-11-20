from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).parent.parent


@pytest.fixture()
def doc_new():
    loader = SourceFileLoader("coordinator_doc_new_case", str(_BIN_DIR / "coordinator-doc-new.py"))
    spec = importlib.util.spec_from_loader("coordinator_doc_new_case", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("cid", ["C0", "R4", "c1-executor-prompt", "W4-b"])
def test_engine_row_ids_are_valid_chunk_ids(doc_new, cid):
    # DoE survey dc0799 (halt f779a8dab0): the C0 brief's `--chunk C0` was refused, so no sidecar was provisioned.
    assert doc_new._CHUNK_ID_RE.match(cid)


@pytest.mark.parametrize("cid", ["../x", "C0/..", "a.b", "c:0", "-c0", ""])
def test_path_and_yaml_unsafe_chunk_ids_stay_refused(doc_new, cid):
    assert not doc_new._CHUNK_ID_RE.match(cid)
