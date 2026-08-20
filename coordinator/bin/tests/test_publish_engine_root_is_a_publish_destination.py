"""test_publish_engine_root_is_a_publish_destination — publish.py's discrimination
between a MIRROR-shaped engine root and a genuine engine import failure.

`_import_claude_klabauter_percolate` resolves its engine root by walking up from the SCRIPT'S
OWN location (`cc_invoke.resolve_engine_root`), so running the published mirror's copy
of publish.py resolves the engine root TO that mirror — which carries no percolate
engine, because `setup/publish-targets.portable`'s engine row negates
`!ops/percolate_*.py` on purpose. The resulting `No module named
coordinator_core.ops.percolate_run` reads as a fleet-wide publish outage when the
source repo's own copy publishes normally; that misreading survived 9 days as
`state/bug-backlog/2026-08-11-klabauter-mirror-ships-the-ops-registry-287f6526da3a`
and was escalated to a P1 that was never true.

These tests pin the DISCRIMINATION, not the wording: a root missing the percolate
engine gets the destination-vs-publisher message, and every other shape keeps the
generic message with its real cause attached.

Run: python -m pytest coordinator/bin/tests/test_publish_engine_root_is_a_publish_destination.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parents[1]


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_engine_root_destination_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _make_engine_root(tmp_path: Path, *, with_percolate: bool) -> Path:
    root = tmp_path / ("publisher" if with_percolate else "destination")
    ops = root / "coordinator_core" / "ops"
    ops.mkdir(parents=True)
    if with_percolate:
        (ops / "percolate_run.py").write_text("", encoding="utf-8")
    return root


class TestDescribeEngineImportFailure:
    def test_root_without_percolate_engine_is_named_a_publish_destination(self, tmp_path):
        root = _make_engine_root(tmp_path, with_percolate=False)

        message = publish._describe_engine_import_failure(
            str(root), ModuleNotFoundError("No module named 'coordinator_core.ops.percolate_run'")
        )

        assert "publish destination" in message
        assert str(root) in message

    def test_the_message_names_an_alternative(self, tmp_path):
        """WHAT TO DO INSTEAD, per the agent-facing message register
        (docs/wiki/guard-messaging.md § Register): a refusal with no alternative
        leaves the reader exactly where the 9-day misdiagnosis left them."""
        root = _make_engine_root(tmp_path, with_percolate=False)

        message = publish._describe_engine_import_failure(str(root), ImportError("boom"))

        assert "CLAUDE_KLABAUTER_ROOT" in message

    def test_root_with_percolate_engine_keeps_the_generic_cause(self, tmp_path):
        """A root that DOES carry the engine failed for some other reason — the real
        exception must survive rather than be overwritten by the mirror explanation."""
        root = _make_engine_root(tmp_path, with_percolate=True)

        message = publish._describe_engine_import_failure(str(root), ImportError("boom"))

        assert "publish destination" not in message
        assert "boom" in message

    def test_unresolved_root_keeps_the_generic_cause(self):
        """`engine_root is None` means the failure happened at or before
        `require_engine_on_path` — there is no root to characterise."""
        message = publish._describe_engine_import_failure(None, ImportError("boom"))

        assert "publish destination" not in message
        assert "boom" in message
