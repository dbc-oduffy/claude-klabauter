"""design.registry.lookup op: params, absent/malformed registry, hit, miss, zero spawns, warm cost."""

from __future__ import annotations

import subprocess
import time

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.authz.registration_quad import check_registration_quad
from coordinator_core.ops.design_registry_lookup import _handler

_BOARD = "https://claude.ai/artifact/abc123"
_REGISTRY = f"""schema: design-registry/1
entries:
  - id: settings
    board: {_BOARD}
    artboards: [main]
    harvest: docs/design/harvest/settings
    sources: ["app/(dash)/settings/**"]
    status: designed
"""


def _write_registry(root, text=_REGISTRY):
    path = root / "docs" / "design" / "registry.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def test_bad_params(tmp_path):
    assert _handler({}, tmp_path)["reason"] == "bad-params"
    assert _handler({"paths": "a"}, tmp_path)["reason"] == "bad-params"
    assert _handler({"paths": [1]}, tmp_path)["reason"] == "bad-params"
    assert _handler({"paths": []}, None)["reason"] == "bad-params"


def test_absent_registry(tmp_path):
    assert _handler({"paths": ["a.py"]}, tmp_path) == {"ok": True, "hits": [], "registry": None}


def test_malformed_registry(tmp_path):
    _write_registry(tmp_path, "entries: [oops\n")
    reply = _handler({"paths": ["a.py"]}, tmp_path)
    assert reply["ok"] is False and reply["reason"] == "registry-unparseable"


def test_hit_and_miss(tmp_path):
    _write_registry(tmp_path)
    hit = _handler({"paths": ["app/(dash)/settings/page.tsx", "other.py"]}, tmp_path)
    assert hit["ok"] is True
    assert hit["registry"] == "docs/design/registry.yaml"
    assert [h["id"] for h in hit["hits"]] == ["settings"]
    assert hit["hits"][0]["board_url"] == _BOARD.removeprefix("https://")
    assert hit["hits"][0]["paths"] == ["app/(dash)/settings/page.tsx"]
    miss = _handler({"paths": ["other.py"]}, tmp_path)
    assert miss == {"ok": True, "hits": [], "registry": "docs/design/registry.yaml"}


def test_zero_spawns_and_warm_cost(tmp_path, monkeypatch):
    _write_registry(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    params = {"paths": ["app/(dash)/settings/page.tsx"]}
    _handler(params, tmp_path)
    start = time.process_time()
    reply = _handler(params, tmp_path)
    elapsed = time.process_time() - start
    assert reply["ok"] is True and reply["hits"]
    assert elapsed < 0.05


def test_registered_compute_only_on_every_surface():
    assert OP_CLASSIFICATION["design.registry.lookup"] is OpClass.COMPUTE_ONLY
    violations = [v for v in check_registration_quad() if "design.registry.lookup" in str(v)]
    assert violations == []
