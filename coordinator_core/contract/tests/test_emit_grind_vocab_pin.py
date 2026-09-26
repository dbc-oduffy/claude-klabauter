from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from coordinator_core.contract.grind_vocab import ENGINE_CONCURRENCY_CEILING

_HEADS_UP = (
    "the queue-grind vocabulary emission is a cross-repo contract "
    "(docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § Design § "
    "Vocabulary contract): DoE's queue-grind-profile.schema.json imports "
    "this module's emitted grind-vocab.json / "
    "queue-grind-handback.schema.json. DO NOT regenerate the golden "
    "fixtures or otherwise silence this failure locally -- send a heads-up "
    "to DoE-claude before anything ships."
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_VOCAB_GOLDEN = _FIXTURES_DIR / "grind-vocab.golden.json"
_HANDBACK_SCHEMA_GOLDEN = _FIXTURES_DIR / "queue-grind-handback.schema.golden.json"

_EXPECTED_MODULE = "coordinator_core.contract.grind_vocab"
_EXPECTED_SYMBOL = "emit_vocab"


def _import_grind_vocab_module():
    try:
        return importlib.import_module(_EXPECTED_MODULE)
    except ImportError as exc:  # pragma: no cover - exercised only on drift
        raise AssertionError(
            f"{_EXPECTED_MODULE} could not be imported -- the module has "
            f"moved or been renamed. {_HEADS_UP}"
        ) from exc


def test_emit_vocab_symbol_is_stable_at_its_documented_location():
    module = _import_grind_vocab_module()
    assert hasattr(module, _EXPECTED_SYMBOL), (
        f"{_EXPECTED_MODULE}.{_EXPECTED_SYMBOL} no longer exists -- the "
        f"canonical emission symbol has moved or been renamed. {_HEADS_UP}"
    )
    assert callable(getattr(module, _EXPECTED_SYMBOL))


def test_out_dir_resolution_explicit_arg_beats_default(tmp_path):
    from coordinator_core.contract.grind_vocab import _vocab_out_dir

    explicit_dir = tmp_path / "explicit"
    assert _vocab_out_dir(explicit_dir) == explicit_dir.resolve(), (
        "an explicit out_dir= argument must win over the default "
        f"resolution -- resolution precedence changed. {_HEADS_UP}"
    )

    repo_root = Path(__file__).resolve().parents[3]
    assert _vocab_out_dir(None) == (repo_root / "schema").resolve(), (
        "the no-argument default out_dir no longer resolves to "
        f"<repo-root>/schema. {_HEADS_UP}"
    )


def test_engine_concurrency_ceiling_is_pinned_at_or_below_sixteen():
    assert ENGINE_CONCURRENCY_CEILING <= 16, (
        "ENGINE_CONCURRENCY_CEILING exceeds the eng-director F4 pin of "
        f"<= 16. {_HEADS_UP}"
    )


def test_emitted_bytes_are_pinned_for_grind_vocab_json(tmp_path):
    from coordinator_core.contract import grind_vocab as gv

    out_dir = tmp_path / "vocab-emit"
    gv.emit_vocab(out_dir=out_dir)

    emitted_path = out_dir / "grind-vocab.json"
    assert emitted_path.exists(), (
        "emit_vocab no longer writes grind-vocab.json into out_dir. "
        f"{_HEADS_UP}"
    )

    emitted_bytes = emitted_path.read_bytes()
    golden_bytes = _VOCAB_GOLDEN.read_bytes()

    assert emitted_bytes == golden_bytes, (
        "emit_vocab's emitted bytes for grind-vocab.json have changed -- "
        "this is the exact class of change that would silently desync "
        f"DoE's committed queue-grind-profile.schema.json. {_HEADS_UP}\n\n"
        f"golden:\n{golden_bytes.decode('utf-8')}\n\n"
        f"emitted:\n{emitted_bytes.decode('utf-8')}"
    )

    json.loads(emitted_bytes)


def test_emitted_bytes_are_pinned_for_handback_schema_json(tmp_path):
    from coordinator_core.contract import grind_vocab as gv

    out_dir = tmp_path / "vocab-emit"
    gv.emit_vocab(out_dir=out_dir)

    emitted_path = out_dir / "queue-grind-handback.schema.json"
    assert emitted_path.exists(), (
        "emit_vocab no longer writes queue-grind-handback.schema.json into "
        f"out_dir. {_HEADS_UP}"
    )

    emitted_bytes = emitted_path.read_bytes()
    golden_bytes = _HANDBACK_SCHEMA_GOLDEN.read_bytes()

    assert emitted_bytes == golden_bytes, (
        "emit_vocab's emitted bytes for queue-grind-handback.schema.json "
        "have changed -- this is the exact class of change that would "
        f"silently desync DoE's schema. {_HEADS_UP}\n\n"
        f"golden:\n{golden_bytes.decode('utf-8')}\n\n"
        f"emitted:\n{emitted_bytes.decode('utf-8')}"
    )

    json.loads(emitted_bytes)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
