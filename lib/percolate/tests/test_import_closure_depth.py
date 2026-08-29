"""A dotted import must resolve at the depth it names, not at its first component.

Until 2026-08-28 both `_extract_top_level_imports` and `_resolves_in_tree`
truncated to the first path component, so an import of
`coordinator_core.benchmarks.leaf_spawn_migration_verify` was graded against
`benchmarks/` — which exists — and passed while the module it names was absent
from the published tree.

That is not a hypothetical. It shipped for three weeks and was reported from
outside as klabauter#3: a fresh clone of the published mirror could not reach a
verdict on its own documented fast-tier command, because tests imported modules
the publish filter had dropped. This gate is the one that should have refused
those rounds, and it passed them.

The pins below are polarity pairs. A resolver that answers True because some
PREFIX of the path exists passes the positive case and fails these; a resolver
that answers False for everything passes these and fails the negative cases.
Both directions are pinned deliberately — the first fix attempt at this class of
bug reintroduced the opposite defect.
"""

import sys
from pathlib import Path

import pytest

# `coordinator/` and `coordinator/lib/` carry no `__init__.py`, so there is no
# dotted import available from the repo root; `coordinator/lib/percolate/` DOES
# have one. Putting `coordinator/lib` on `sys.path` and importing
# `percolate.import_closure` as an ordinary package member is the route the
# sibling tests in this directory already use.
_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.import_closure import (  # noqa: E402
    _extract_top_level_imports,
    _resolves_in_tree,
    find_import_closure_violations,
)


def _tree(tmp_path, files):
    """Materialise a restricted-tree shape. `files` maps relative path -> source."""
    root = tmp_path / "coordinator_core"
    root.mkdir()
    for rel, src in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8")
    return root


# --- the reported defect, as a regression pin -----------------------------


def test_missing_submodule_under_a_present_package_is_a_violation(tmp_path):
    """THE klabauter#3 SHAPE. `benchmarks/` ships, the module inside it does
    not, and the test importing it ships anyway. Depth-1 truncation graded
    this against `benchmarks` and passed."""
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
    """The opposite polarity, and the one a too-eager depth fix breaks: when
    the module IS there, resolution must succeed at full depth."""
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


# --- the resolver itself, both polarities ---------------------------------


@pytest.mark.parametrize(
    "entry,expected",
    [
        # package present, module absent -- a violation, and the case
        # depth-1 truncation could not see
        ("benchmarks.leaf_spawn_migration_verify", False),
        # package absent entirely -- also a violation, and must not become
        # indistinguishable from the above
        ("nosuchpkg.nosuchmod", False),
        ("nosuchpkg", False),
        # present at each depth
        ("benchmarks", True),
        ("benchmarks.present", True),
        ("toplevel", True),
        # a deep path whose LEAF is absent under two present parents
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
    """`module_refs` must carry the whole path. Truncation here is what made
    the resolver's depth irrelevant, so pin the extractor independently —
    fixing one without the other leaves the gate blind."""
    _, module_refs = _extract_top_level_imports(
        "from coordinator_core.telemetry.op_latency import record\n"
        "import coordinator_core.benchmarks.leaf_spawn_migration_verify\n",
        filename="t.py",
    )
    assert module_refs == {
        "telemetry.op_latency",
        "benchmarks.leaf_spawn_migration_verify",
    }


# --- contracts the depth fix must not disturb -----------------------------


def test_bare_shape_still_gets_the_init_attribute_exemption(tmp_path):
    """`from coordinator_core import X` stays ambiguous between a submodule
    and a re-exported attribute, and keeps its depth-1 exemption. Extending
    the dotted fix to this shape would make every lazy re-export a
    violation."""
    root = _tree(
        tmp_path,
        {
            "__init__.py": "__all__ = ['OP_KEY_SCOPE']\n",
            "consumer.py": "from coordinator_core import OP_KEY_SCOPE\n",
        },
    )
    assert find_import_closure_violations(root)[1] == []


def test_guarded_import_of_a_missing_submodule_is_still_exempt(tmp_path):
    """A `try/except ImportError` import is a deliberate soft dependency.
    Measured on the published mirror: this exemption is the difference
    between 21 orphan files and the 18 that actually abort collection."""
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


# --- never-published roots, and the false positive next door --------------


def test_scripts_rooted_import_is_a_violation(tmp_path):
    """THE OTHER klabauter#3 SHAPE, and the one depth alone never reached.
    `scripts/` publishes only `setup.py`/`setup.cmd`, so a shipped test
    importing a generator out of it can never resolve on a fresh clone. The
    gate graded only `coordinator_core`-rooted imports, so this passed."""
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
    """The guarded-import exemption is not bypassed by the new root set: a
    `try/except ImportError` around a `scripts.*` import is the same
    deliberate soft dependency it is around a `coordinator_core` one."""
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


# --- the denominator ------------------------------------------------------


def test_clean_result_carries_the_count_of_files_examined(tmp_path):
    """A caller must be able to tell "0 violations over N files" from "0 over
    0". Today's bare-list return could not, which is how a gate scoped out
    of every row it might have graded reads as a clean one — the abstention
    the parent plan's anti-scope names."""
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
    """The distinction the previous test exists to preserve, from the other
    side: an empty tree is also zero violations, and must not read alike."""
    root = tmp_path / "coordinator_core"
    root.mkdir()
    assert find_import_closure_violations(root) == (0, [])
