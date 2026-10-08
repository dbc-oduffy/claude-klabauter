
import sys
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.import_closure import (  # noqa: E402
    _extract_top_level_imports,
    _resolves_in_tree,
    find_import_closure_violations,
)


def _tree(tmp_path, files):
    root = tmp_path / "coordinator_core"
    root.mkdir()
    for rel, src in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8")
    return root


def test_missing_submodule_under_a_present_package_is_a_violation(tmp_path):
    root = _tree(
        tmp_path,
        {
            "benchmarks/__init__.py": "",
            "benchmarks/tests/test_leaf_spawn.py": (
                "from coordinator_core.benchmarks.leaf_spawn_migration_verify import x\n"
            ),
        },
    )
    _examined, violations = find_import_closure_violations(root)
    assert violations == [
        (
            "benchmarks/tests/test_leaf_spawn.py",
            "benchmarks.leaf_spawn_migration_verify",
        )
    ]


def test_present_submodule_under_a_present_package_is_not_a_violation(tmp_path):
    root = _tree(
        tmp_path,
        {
            "benchmarks/__init__.py": "",
            "benchmarks/leaf_spawn_migration_verify.py": "x = 1\n",
            "benchmarks/tests/test_leaf_spawn.py": (
                "from coordinator_core.benchmarks.leaf_spawn_migration_verify import x\n"
            ),
        },
    )
    assert find_import_closure_violations(root)[1] == []


@pytest.mark.parametrize(
    "entry,expected",
    [
        ("benchmarks.leaf_spawn_migration_verify", False),
        ("nosuchpkg.nosuchmod", False),
        ("nosuchpkg", False),
        ("benchmarks", True),
        ("benchmarks.present", True),
        ("toplevel", True),
        ("benchmarks.sub.absent", False),
        ("benchmarks.sub.present", True),
    ],
)
def test_resolves_in_tree_answers_at_full_depth(tmp_path, entry, expected):
    root = _tree(
        tmp_path,
        {
            "toplevel.py": "",
            "benchmarks/__init__.py": "",
            "benchmarks/present.py": "",
            "benchmarks/sub/__init__.py": "",
            "benchmarks/sub/present.py": "",
        },
    )
    assert _resolves_in_tree(root, entry) is expected


def test_extractor_keeps_the_full_dotted_remainder():
    _, module_refs = _extract_top_level_imports(
        "from coordinator_core.telemetry.op_latency import record\n"
        "import coordinator_core.benchmarks.leaf_spawn_migration_verify\n",
        filename="t.py",
    )
    assert module_refs == {
        "telemetry.op_latency",
        "benchmarks.leaf_spawn_migration_verify",
    }


def test_bare_shape_still_gets_the_init_attribute_exemption(tmp_path):
    root = _tree(
        tmp_path,
        {
            "__init__.py": "__all__ = ['OP_KEY_SCOPE']\n",
            "consumer.py": "from coordinator_core import OP_KEY_SCOPE\n",
        },
    )
    assert find_import_closure_violations(root)[1] == []


def test_guarded_import_of_a_missing_submodule_is_still_exempt(tmp_path):
    root = _tree(
        tmp_path,
        {
            "benchmarks/__init__.py": "",
            "consumer.py": (
                "try:\n"
                "    from coordinator_core.benchmarks.absent import thing\n"
                "except ImportError:\n"
                "    thing = None\n"
            ),
        },
    )
    assert find_import_closure_violations(root)[1] == []


def test_scripts_rooted_import_is_a_violation(tmp_path):
    root = _tree(
        tmp_path,
        {
            "ops/tests/test_gen_dod.py": (
                "import scripts.gen_dod_backlog_fragment\n"
                "from scripts.gen_ported_ops_fragment import discover_records\n"
            ),
        },
    )
    _examined, violations = find_import_closure_violations(root)
    assert violations == [
        ("ops/tests/test_gen_dod.py", "scripts.gen_dod_backlog_fragment"),
        ("ops/tests/test_gen_dod.py", "scripts.gen_ported_ops_fragment"),
    ]


def test_coordinator_rooted_import_is_not_a_violation(tmp_path):
    """NEGATIVE SPEC, and the expensive one to get wrong. `coordinator` and
    `lib` are SEPARATELY PUBLISHED ROWS: their names resolve in the
    assembled mirror, never inside one row's restricted tree. Grading them
    here manufactures false positives — 372 measured on
    `-coordinator-bin` (2026-08-13), and 3 more measured 2026-08-29 against
    two files that genuinely ship (`tests/test_home_resolution_lint.py`,
    `install/tests/test_fleet_env_publish_reachability.py`, both present in
    mirror `c587c774` with their imports resolving there).

    A future widening that adds `coordinator` or `lib` to
    `NEVER_PUBLISHED_ROOTS` fails this test, which is the point: the
    assembled union is `assembled_mirror_gate`'s question, not this
    gate's."""
    root = _tree(
        tmp_path,
        {
            "tests/test_home_resolution_lint.py": (
                "from coordinator.lib.home_resolution_lint import scan\n"
            ),
            "install/tests/test_reachability.py": (
                "from coordinator.lib.percolate.allowlist import build\n"
                "import lib.percolate.targets\n"
            ),
        },
    )
    assert find_import_closure_violations(root)[1] == []


def test_guarded_never_published_import_is_exempt(tmp_path):
    root = _tree(
        tmp_path,
        {
            "ops/tests/test_soft.py": (
                "try:\n"
                "    import scripts.gen_ported_ops_fragment\n"
                "except ImportError:\n"
                "    scripts = None\n"
            ),
        },
    )
    assert find_import_closure_violations(root)[1] == []


def test_clean_result_carries_the_count_of_files_examined(tmp_path):
    root = _tree(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "pkg/__init__.py": "",
            "pkg/b.py": "from coordinator_core.pkg import b\n",
        },
    )
    examined, violations = find_import_closure_violations(root)
    assert violations == []
    assert examined == 3


def test_examined_count_is_zero_on_an_empty_tree(tmp_path):
    root = tmp_path / "coordinator_core"
    root.mkdir()
    assert find_import_closure_violations(root) == (0, [])


def test_unparseable_file_is_not_counted_in_examined_and_produces_no_violation(tmp_path):
    root = _tree(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "broken.py": "def broken(:\n",
        },
    )
    examined, violations = find_import_closure_violations(root)
    assert examined == 1
    assert violations == []
