"""
coordinator_core.tests.test_fleet_mode_reader_set — the floor: a positive,
literal allowlist of every PRODUCTION module that reads the fleet record
DIRECTLY, red in both directions.

A direct reader is a non-test module under ``coordinator_core`` whose OWN
source imports ``coordinator_core.session.fleet_mode`` or ``.mode_resolution``
(absolute, relative, or ``from coordinator_core.session import fleet_mode``)
or names the literal ``fleet-mode.json``. No import is followed: that no
denial-class guard's verdict depends on the record is proven at runtime by
``test_denial_guard_fleet_mode_independence``, so this test only pins WHO
reads the file. A new direct reader, or a listed one that stops reading,
turns the equality red.

``EXPECTED_READERS`` is a hardcoded literal tuple, never derived from the
scan (a derived-then-compared set would pass forever), and
``direct_readers`` takes a root path so the self-verification class runs the
same red-in-both-directions property against a fixture tree.

The basename leg sees only a LITERAL ``fleet-mode.json`` in source text; a
path handed across a runtime boundary (``fleet_mode_path()`` re-exported or
composed from ``settings_home()``) is an accepted residual.

Static analysis only: no guard or reader module is imported or executed.
"""

from __future__ import annotations

import ast
from pathlib import Path

# --- targets ----------------------------------------------------------------

_TARGET_MODULES = (
    "coordinator_core.session.fleet_mode",
    "coordinator_core.session.mode_resolution",
)
_TARGET_BASENAME = "fleet-mode.json"


# Each entry names its own source reading the record; the reason says why.
EXPECTED_READERS = (
    # Advisory hook: resolves the job mode to offer a better dispatch path; never denies.
    "coordinator_core.hooks.nudge_em_code_dispatch",
    # Advisory dispatch: resolves the mode for its advisory context only.
    "coordinator_core.hooks.postuse_advisory_dispatch",
    # SessionStart: announces the resolved job mode as context only.
    "coordinator_core.hooks.session_start_announce_job_mode",
    # Ask gate: installs the fleet interaction-mode provider for route selection.
    "coordinator_core.ops.dispatch_emit.ask_gate",
    # The op that writes the fleet record.
    "coordinator_core.ops.fleet.mode_control",
)


# --- static direct-reader scan (never imports anything) ----------------------


def _is_test_path(root: Path, path: Path) -> bool:
    rel = path.relative_to(root)
    leaf = rel.name
    return "tests" in rel.parts[:-1] or leaf.startswith("test_") or leaf == "conftest.py"


def _dotted_module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_from_import(current_module: str, is_package: bool, node: ast.ImportFrom) -> set[str]:
    if node.level == 0:
        base = node.module or ""
    else:
        pkg_parts = current_module.split(".") if is_package else current_module.split(".")[:-1]
        up = node.level - 1
        anchor_parts = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
        anchor = ".".join(p for p in anchor_parts if p)
        base = f"{anchor}.{node.module}" if node.module else anchor
    if not base:
        return set()
    targets = {base}
    for alias in node.names:
        if alias.name != "*":
            targets.add(f"{base}.{alias.name}")
    return targets


def _reads_record(path: Path, root: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if _TARGET_BASENAME in source:
        return True
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False
    current = _dotted_module_name(root, path)
    is_package = path.name == "__init__.py"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in _TARGET_MODULES for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if _resolve_from_import(current, is_package, node) & set(_TARGET_MODULES):
                return True
    return False


def direct_readers(root: Path, package: str = "coordinator_core") -> frozenset[str]:
    """Dotted names of every non-test module under ``root/package`` whose own
    source imports a ``_TARGET_MODULES`` entry or names ``_TARGET_BASENAME``.
    The target modules themselves are excluded. Takes a root path so it runs
    against a fixture tree."""
    found: set[str] = set()
    for path in sorted((root / package).rglob("*.py")):
        if "__pycache__" in path.parts or _is_test_path(root, path):
            continue
        name = _dotted_module_name(root, path)
        if name in _TARGET_MODULES:
            continue
        if _reads_record(path, root):
            found.add(name)
    return frozenset(found)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# --- the floor, over the real tree -------------------------------------------


def test_reader_set_matches_allowlist_exactly():
    """RED in both directions: a new direct reader, or a listed reader that no
    longer reads the record, both fail this equality."""
    computed = direct_readers(_repo_root())
    expected = frozenset(EXPECTED_READERS)

    unexpected = computed - expected
    missing = expected - computed

    assert not unexpected, (
        f"module(s) {sorted(unexpected)} now read the fleet record "
        "(coordinator_core.session.fleet_mode / .mode_resolution / "
        "fleet-mode.json) directly but are not in EXPECTED_READERS -- someone "
        "wired a new reader. Add it with a one-line reason, or remove the read."
    )
    assert not missing, (
        f"module(s) {sorted(missing)} are in EXPECTED_READERS but no longer "
        "read the fleet record directly. Remove them from EXPECTED_READERS if "
        "the removal was deliberate."
    )


def test_expected_readers_has_no_test_module():
    listed = sorted(
        m
        for m in EXPECTED_READERS
        if ".tests." in m or m.rsplit(".", 1)[-1].startswith("test_") or m.endswith(".conftest")
    )
    assert not listed, f"test module(s) {listed} are in EXPECTED_READERS; the scan never reports them."


# --- self-verification: the mechanism itself is red in both directions ------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_IMPORT_FLEET = "from coordinator_core.session.fleet_mode import read_fleet_mode\n"


class TestDirectReadersSelfVerification:
    """direct_readers() against a disposable fixture tree, never the real repo."""

    def _base_fixture(self, tmp_path: Path) -> Path:
        root = tmp_path / "fixture_repo"
        _write(root / "coordinator_core" / "__init__.py", "")
        _write(root / "coordinator_core" / "session" / "__init__.py", "")
        _write(
            root / "coordinator_core" / "session" / "fleet_mode.py",
            "def read_fleet_mode():\n    return {}\n",
        )
        _write(
            root / "coordinator_core" / "session" / "mode_resolution.py",
            _IMPORT_FLEET,
        )
        _write(root / "coordinator_core" / "unrelated" / "__init__.py", "")
        _write(root / "coordinator_core" / "unrelated" / "other.py", "import json\n")
        return root

    def test_unrelated_module_is_absent(self, tmp_path):
        root = self._base_fixture(tmp_path)
        assert direct_readers(root) == frozenset()

    def test_absolute_import_found(self, tmp_path):
        root = self._base_fixture(tmp_path)
        _write(root / "coordinator_core" / "unrelated" / "direct.py", _IMPORT_FLEET)
        assert direct_readers(root) == frozenset({"coordinator_core.unrelated.direct"})

    def test_plain_import_and_mode_resolution_found(self, tmp_path):
        root = self._base_fixture(tmp_path)
        pkg = root / "coordinator_core" / "unrelated"
        _write(pkg / "plain.py", "import coordinator_core.session.mode_resolution\n")
        _write(pkg / "package_form.py", "from coordinator_core.session import fleet_mode\n")
        assert direct_readers(root) == frozenset(
            {"coordinator_core.unrelated.plain", "coordinator_core.unrelated.package_form"}
        )

    def test_relative_import_found(self, tmp_path):
        root = self._base_fixture(tmp_path)
        _write(
            root / "coordinator_core" / "session" / "sibling.py",
            "from .fleet_mode import read_fleet_mode\n",
        )
        _write(
            root / "coordinator_core" / "unrelated" / "up.py",
            "from ..session import mode_resolution\n",
        )
        assert direct_readers(root) == frozenset(
            {"coordinator_core.session.sibling", "coordinator_core.unrelated.up"}
        )

    def test_basename_literal_found(self, tmp_path):
        root = self._base_fixture(tmp_path)
        _write(
            root / "coordinator_core" / "unrelated" / "basename.py",
            'PATH = "fleet-mode.json"\n',
        )
        assert direct_readers(root) == frozenset({"coordinator_core.unrelated.basename"})

    def test_indirect_importer_is_not_reported(self, tmp_path):
        """No import is followed: a module importing a reader is not a reader."""
        root = self._base_fixture(tmp_path)
        pkg = root / "coordinator_core" / "unrelated"
        _write(pkg / "direct.py", _IMPORT_FLEET)
        _write(pkg / "indirect.py", "from coordinator_core.unrelated.direct import read_fleet_mode\n")
        assert direct_readers(root) == frozenset({"coordinator_core.unrelated.direct"})

    def test_removed_reader_drops_out(self, tmp_path):
        root = self._base_fixture(tmp_path)
        reader = root / "coordinator_core" / "unrelated" / "direct.py"
        _write(reader, _IMPORT_FLEET)
        assert "coordinator_core.unrelated.direct" in direct_readers(root)
        _write(reader, "import json\n")
        assert "coordinator_core.unrelated.direct" not in direct_readers(root)

    def test_test_modules_are_not_reported(self, tmp_path):
        root = self._base_fixture(tmp_path)
        pkg = root / "coordinator_core" / "unrelated"
        _write(pkg / "tests" / "__init__.py", "")
        _write(pkg / "tests" / "helper.py", _IMPORT_FLEET)
        _write(pkg / "test_sibling.py", _IMPORT_FLEET)
        _write(pkg / "conftest.py", _IMPORT_FLEET)
        assert direct_readers(root) == frozenset()
        _write(pkg / "production.py", _IMPORT_FLEET)
        assert direct_readers(root) == frozenset({"coordinator_core.unrelated.production"})

    def test_expected_readers_tuple_is_not_derived_from_the_scan(self):
        """EXPECTED_READERS must be a literal tuple of string constants, never a
        call into direct_readers() -- that would make the equality pass forever."""
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(Path(__file__)))
        assign_node = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXPECTED_READERS" for t in node.targets
            ):
                assign_node = node
                break
        assert assign_node is not None, "EXPECTED_READERS assignment not found"
        assert isinstance(assign_node.value, ast.Tuple), (
            "EXPECTED_READERS must be a literal tuple expression, not a "
            f"computed value ({type(assign_node.value).__name__})."
        )
        for element in assign_node.value.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                "every EXPECTED_READERS element must be a literal string "
                f"constant, found {ast.dump(element)}"
            )
