from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pa
from coordinator_core.baton_assemble.apply import exec_module_with_own_dir_on_path
from coordinator_core.ops.handoff_normalize import _normalize_one_text
from coordinator_core.pickup_assemble import _role_axis_is_unknown

_BIN_DIR = Path(__file__).resolve().parents[3] / "coordinator" / "bin"


def _load_doc_new_module():
    """Loads `coordinator-doc-new.py` by path, through the engine's own
    `exec_module_with_own_dir_on_path` rather than a second copy of its
    sys.path dance.

    That function is the single home for the reason: a by-path load leaves the
    script's directory off `sys.path`, so its bare `import lib` bound CPython's
    own `<prefix>/lib` as a namespace package, the bin bootstrap never ran, and
    this module died at COLLECTION time on `ModuleNotFoundError: No module
    named 'memo_compose'`. Composing rather than restating is deliberate: the
    duplicate this replaced had no structural pressure to receive the lock that
    landed on the production copy.
    """
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_kind_axis_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_doc_new_kind_axis_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    exec_module_with_own_dir_on_path(loader, mod, str(_BIN_DIR))
    return mod


_MOD = _load_doc_new_module()


class TestAuthoringPathsStampWork:

    def test_scaffold_handoff_stamps_work(self):
        rendered = _MOD._scaffold_handoff(title="A continuation baton", branch="work/x/2026-08-19")
        assert "baton_role: work\n" in rendered
        assert "kind: session-handoff\n" in rendered

    def test_scaffold_spinoff_stamps_work(self):
        rendered = _MOD._scaffold_spinoff(title="A spinoff baton", branch="work/x/2026-08-19")
        assert "baton_role: work\n" in rendered
        assert "kind: spinoff\n" in rendered


class TestNormalizeNeverBackfills:

    def test_normalize_leaves_absent_baton_role_absent(self, tmp_path):
        content = (
            "---\n"
            'title: "Legacy Handoff"\n'
            "created: 2026-01-01\n"
            "branch: work/legacy/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: ready_to_fire\n"
            "---\n\n# Handoff\n\nBody.\n"
        )
        file_path = tmp_path / "state" / "handoffs" / "legacy.md"
        result = _normalize_one_text(content, file_path)
        rebuilt = result["rebuilt"] if result is not None else content
        assert "baton_role" not in rebuilt


class TestRoleAxisUnknownPredicate:

    @pytest.mark.parametrize("raw", [None, "null", "None", "~", "", "  "])
    def test_absent_or_null_ish_is_unknown(self, raw):
        assert _role_axis_is_unknown(raw) is True

    @pytest.mark.parametrize("raw", ["work", "record"])
    def test_stamped_value_is_known(self, raw):
        assert _role_axis_is_unknown(raw) is False


class TestPickupOfUnstampedLegacyBatonUnaffected:

    def test_brief_admits_operator_named_unstamped_legacy_baton(self, tmp_path: Path) -> None:
        target = tmp_path / "state" / "handoffs" / "legacy.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\n"
            'title: "Legacy Handoff"\n'
            "created: 2026-01-01\n"
            "branch: work/legacy/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: ready_to_fire\n"
            "---\n\n# Handoff\n\nBody.\n",
            encoding="utf-8",
        )
        result = pa.brief("state/handoffs/legacy.md", repo_root=tmp_path)
        assert result.decision_object["artifact"]["path"] == "state/handoffs/legacy.md"
