from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_MODULE_PATH = _BIN_DIR / "engine-provenance-inventory.py"

_spec = importlib.util.spec_from_file_location(
    "engine_provenance_inventory", str(_MODULE_PATH)
)
epi = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
sys.modules["engine_provenance_inventory"] = epi
_spec.loader.exec_module(epi)


def test_confirmed_divergent_shape(tmp_path):
    src = (
        "import repo_identity\n"
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "\n"
        "def main():\n"
        "    root = require_dispatch_engine_on_path()\n"
        "    return root\n"
    )
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    result = epi.classify_carrier(path)
    assert result["carries_bootstrap"] is True
    assert result["order_hazard_candidate"] is True
    assert result["binder_modules"] == ["repo_identity"]


def test_clean_order_not_divergent(tmp_path):
    src = (
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "def main():\n"
        "    return require_dispatch_engine_on_path()\n"
        "import repo_identity\n"
        "main()\n"
    )
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    result = epi.classify_carrier(path)
    assert result["carries_bootstrap"] is True
    assert result["order_hazard_candidate"] is False
    assert result["binder_modules"] == []


def test_module_level_bootstrap_below_a_binder_import_is_a_candidate(tmp_path):
    src = (
        "import repo_identity\n"
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "root = require_dispatch_engine_on_path()\n"
    )
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    result = epi.classify_carrier(path)
    assert result["order_hazard_candidate"] is True
    assert result["binder_modules"] == ["repo_identity"]


def test_bootstrap_above_the_binder_import_is_not_a_candidate(tmp_path):
    src = (
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "root = require_dispatch_engine_on_path()\n"
        "import coordinator_core\n"
        "import repo_identity\n"
    )
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    result = epi.classify_carrier(path)
    assert result["order_hazard_candidate"] is False
    assert result["binder_modules"] == []


def test_attribute_form_bootstrap_call_is_recognised(tmp_path):
    src = (
        "import cc_invoke\n"
        "cc_invoke.require_dispatch_engine_on_path()\n"
        "import coordinator_core\n"
        "import repo_identity\n"
    )
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    result = epi.classify_carrier(path)
    assert result["carries_bootstrap"] is True
    assert result["order_hazard_candidate"] is False


def test_no_bootstrap_call_out_of_population(tmp_path):
    src = "import repo_identity\n\ndef main():\n    return repo_identity\n"
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    assert epi.classify_carrier(path) is None


def test_unparseable_file_degrades_to_none(tmp_path):
    path = tmp_path / "carrier.py"
    path.write_text("def broken(:\n", encoding="utf-8")
    assert epi.classify_carrier(path) is None


def test_null_byte_in_source_degrades_to_none(tmp_path):
    path = tmp_path / "carrier.py"
    path.write_bytes(b"import repo_identity\n\x00\n")
    assert epi.classify_carrier(path) is None


def test_guarded_module_level_binder_import_inside_try_is_seen(tmp_path):
    src = (
        "try:\n"
        "    import repo_identity\n"
        "except ImportError:\n"
        "    repo_identity = None\n"
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "\n"
        "def main():\n"
        "    return require_dispatch_engine_on_path()\n"
    )
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    result = epi.classify_carrier(path)
    assert result["order_hazard_candidate"] is True
    assert result["binder_modules"] == ["repo_identity"]


def test_guarded_binder_import_inside_function_scope_still_ignored(tmp_path):
    src = (
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "\n"
        "def helper():\n"
        "    try:\n"
        "        import repo_identity\n"
        "    except ImportError:\n"
        "        repo_identity = None\n"
        "    return repo_identity\n"
        "\n"
        "def main():\n"
        "    return require_dispatch_engine_on_path()\n"
    )
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    result = epi.classify_carrier(path)
    assert result["order_hazard_candidate"] is False
    assert result["binder_modules"] == []


def test_from_import_binds_the_module_itself(tmp_path):
    src = (
        "from repo_identity import resolve_checked_repo_root\n"
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "\n"
        "def main():\n"
        "    return require_dispatch_engine_on_path()\n"
    )
    path = tmp_path / "carrier.py"
    path.write_text(src, encoding="utf-8")
    result = epi.classify_carrier(path)
    assert result["order_hazard_candidate"] is True
    assert result["binder_modules"] == ["repo_identity"]


def test_static_scan_aggregates_directory(tmp_path):
    divergent_src = (
        "import repo_identity\n"
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "\n"
        "def main():\n"
        "    return require_dispatch_engine_on_path()\n"
    )
    clean_src = (
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "\n"
        "def main():\n"
        "    return require_dispatch_engine_on_path()\n"
    )
    unrelated_src = "def main():\n    return 1\n"
    (tmp_path / "a.py").write_text(divergent_src, encoding="utf-8")
    (tmp_path / "b.py").write_text(clean_src, encoding="utf-8")
    (tmp_path / "c.py").write_text(unrelated_src, encoding="utf-8")

    result = epi.static_scan(tmp_path)
    assert result["total_carriers"] == 2
    assert result["order_hazard_candidate_count"] == 1
    assert result["order_hazard_candidates"][0]["carrier"].endswith("a.py")
    assert result["spike_measured_total_carriers"] == 201
    assert result["spike_measured_divergent_carriers"] == 15


def test_runtime_aggregate_groups_by_wrapper_axis_verdict(tmp_path):
    counts_path = tmp_path / "engine-provenance-counts.jsonl"
    records = [
        {
            "caller": "require_dispatch_engine_on_path",
            "axis": "dispatch",
            "verdict": "divergent",
            "imported_file": "/x/coordinator_core/__init__.py",
            "engine_root": "/y",
            "at": "2026-08-26T00:00:00+00:00",
        },
        {
            "caller": "require_dispatch_engine_on_path",
            "axis": "dispatch",
            "verdict": "divergent",
            "imported_file": "/x/coordinator_core/__init__.py",
            "engine_root": "/y",
            "at": "2026-08-26T00:00:01+00:00",
        },
        {
            "caller": "ensure_engine_on_path",
            "axis": "locator",
            "verdict": "match",
            "imported_file": "/x/coordinator_core/__init__.py",
            "engine_root": "/x",
            "at": "2026-08-26T00:00:02+00:00",
        },
    ]
    counts_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )

    result = epi.runtime_aggregate(counts_path)
    assert result["total_records"] == 3
    assert result["divergent_record_total"] == 2
    row = {
        (r["caller"], r["axis"], r["verdict"]): r["count"]
        for r in result["by_wrapper_axis_verdict"]
    }
    assert row[("require_dispatch_engine_on_path", "dispatch", "divergent")] == 2
    assert row[("ensure_engine_on_path", "locator", "match")] == 1


def test_runtime_aggregate_skips_malformed_lines(tmp_path):
    counts_path = tmp_path / "engine-provenance-counts.jsonl"
    counts_path.write_text(
        json.dumps(
            {
                "caller": "ensure_engine_on_path",
                "axis": "locator",
                "verdict": "match",
                "imported_file": None,
                "engine_root": None,
                "at": "2026-08-26T00:00:00+00:00",
            }
        )
        + "\n"
        + "{not json\n"
        + "\n",
        encoding="utf-8",
    )
    result = epi.runtime_aggregate(counts_path)
    assert result["total_records"] == 1


def test_runtime_aggregate_missing_file_is_empty_not_error(tmp_path):
    result = epi.runtime_aggregate(tmp_path / "does-not-exist.jsonl")
    assert result["total_records"] == 0
    assert result["divergent_record_total"] == 0
    assert result["by_wrapper_axis_verdict"] == []


def test_build_report_never_joins_static_and_runtime_by_carrier(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "a.py").write_text(
        "import repo_identity\n"
        "from cc_invoke import require_dispatch_engine_on_path\n"
        "\n"
        "def main():\n"
        "    return require_dispatch_engine_on_path()\n",
        encoding="utf-8",
    )
    counts_path = tmp_path / "counts.jsonl"
    counts_path.write_text(
        json.dumps(
            {
                "caller": "require_dispatch_engine_on_path",
                "axis": "dispatch",
                "verdict": "divergent",
                "imported_file": "/x",
                "engine_root": "/y",
                "at": "2026-08-26T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = epi.build_report(bin_dir, counts_path)
    assert "static" in report and "runtime" in report
    static_keys = set(report["static"].keys())
    runtime_keys = set(report["runtime"].keys())
    assert static_keys.isdisjoint(runtime_keys)
    for row in report["runtime"]["by_wrapper_axis_verdict"]:
        assert "carrier" not in row
    for row in report["static"]["order_hazard_candidates"]:
        assert "verdict" not in row
    assert "NOT joined" in report["note"] or "not joined" in report["note"].lower()


def test_human_summary_line_reports_both_views(tmp_path):
    (tmp_path / "empty-bin").mkdir()
    report = epi.build_report(tmp_path / "empty-bin", tmp_path / "no-counts.jsonl")
    line = epi.human_summary_line(report)
    assert "static:" in line
    assert "runtime:" in line
