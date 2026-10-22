"""Tripwire: no test steers the ``dispatch_checks`` facade by patching it.

After the split a name patched on the facade module object is not the name the
family module reads, so the patch silently stops reaching its target. Zero-spawn,
pathlib + ast only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_FACADE_MODULE = "coordinator_core.bash_guards.dispatch_checks"
_FACADE_ATTRS = frozenset({"dispatch_checks", "_dc"})
_STRING_PATCHERS = frozenset({"patch", "setattr", "delattr", "object"})
_OBJECT_PATCHERS = frozenset({"setattr", "delattr", "object"})
_FIX = "patch the _dc_* module whose code reads the name"
_SELF = Path(__file__).resolve()
_DISPATCH_SRC = Path(__file__).resolve().parents[1] / "dispatch.py"


def _names_dispatch_reads_through_facade() -> frozenset[str]:
    """Attributes ``dispatch.py`` resolves on the facade at call time: assigning one is live."""
    text = _DISPATCH_SRC.read_text(encoding="utf-8")
    return frozenset(re.findall(r"\b_dc\.([A-Za-z_][A-Za-z_0-9]*)", text))


def _scan_roots() -> list[Path]:
    return [_REPO / "coordinator_core", _REPO / "coordinator", _REPO / "bin"]


def _in_test_dir(path: Path) -> bool:
    return "tests" in path.parts or path.name == "guard_message_corpus.py"


def _candidate_files() -> list[Path]:
    found: list[Path] = []
    for root in _scan_roots():
        if root.is_dir():
            found.extend(p for p in root.rglob("*.py") if _in_test_dir(p))
    return sorted(p for p in found if p.resolve() != _SELF)


def _facade_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "coordinator_core.bash_guards" or (node.level and mod in ("", "bash_guards")):
                for a in node.names:
                    if a.name == "dispatch_checks":
                        aliases.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == _FACADE_MODULE and a.asname:
                    aliases.add(a.asname)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            fn = call.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "import_module"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == _FACADE_MODULE
            ):
                aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return aliases


def _call_name(call: ast.Call) -> str:
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def _is_patch_object(call: ast.Call) -> bool:
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id == "setattr"
    if isinstance(fn, ast.Attribute):
        if fn.attr in ("setattr", "delattr"):
            return True
        if fn.attr == "object":
            base = fn.value
            tail = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            return tail == "patch"
    return False


def _reaches_facade(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr in _FACADE_ATTRS
    return False


def facade_patches(source: str, filename: str = "<src>") -> list[str]:
    """Return ``file:line`` strings for each facade patch in ``source``."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    aliases = _facade_aliases(tree)
    live_names = _names_dispatch_reads_through_facade()
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(t, ast.Attribute)
                and _reaches_facade(t.value, aliases)
                and t.attr not in live_names
                for t in targets
            ):
                hits.append(f"{filename}:{node.lineno}")
            continue
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        name = _call_name(node)
        if (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.startswith(_FACADE_MODULE + ".")
            and name in _STRING_PATCHERS
        ):
            hits.append(f"{filename}:{node.lineno}")
        elif _is_patch_object(node) and _reaches_facade(first, aliases):
            hits.append(f"{filename}:{node.lineno}")
    return hits


def _scan(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "dispatch_checks" not in source and "_dc" not in source:
            continue
        try:
            rel = path.relative_to(_REPO).as_posix()
        except ValueError:
            rel = path.as_posix()
        hits.extend(facade_patches(source, rel))
    return hits


def test_no_test_patches_the_dispatch_checks_facade():
    hits = _scan(_candidate_files())
    assert not hits, (
        "facade patch no longer reaches the code that reads the name; "
        + _FIX
        + ":\n"
        + "\n".join(hits)
    )


def test_red_leg_reports_a_synthetic_facade_patch(tmp_path):
    f = tmp_path / "test_synthetic.py"
    f.write_text(
        "from coordinator_core.bash_guards import dispatch_checks as dc\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(dc, 'thing', 1)\n",
        encoding="utf-8",
    )
    hits = _scan([f])
    assert len(hits) == 1 and hits[0].endswith(":3")


def test_red_leg_reports_string_and_attribute_targets(tmp_path):
    f = tmp_path / "test_synthetic2.py"
    f.write_text(
        "from unittest import mock\n"
        "import dispatch\n"
        "def test_x(monkeypatch):\n"
        "    mock.patch('coordinator_core.bash_guards.dispatch_checks.thing')\n"
        "    monkeypatch.setattr(dispatch._dc, 'thing', 1)\n"
        "    mock.patch.object(a.dispatch_checks, 'thing')\n",
        encoding="utf-8",
    )
    assert len(_scan([f])) == 3


def test_stdlib_attribute_chain_is_not_flagged(tmp_path):
    f = tmp_path / "test_ok.py"
    f.write_text(
        "from coordinator_core.bash_guards import dispatch_checks\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(dispatch_checks.subprocess, 'run', None)\n",
        encoding="utf-8",
    )
    assert _scan([f]) == []


def test_red_leg_reports_an_aliased_attribute_assignment(tmp_path):
    f = tmp_path / "test_synthetic3.py"
    f.write_text(
        "from coordinator_core.bash_guards import dispatch_checks as dc\n"
        "def test_x():\n"
        "    dc.thing = 1\n",
        encoding="utf-8",
    )
    hits = _scan([f])
    assert len(hits) == 1 and hits[0].endswith(":3")


def test_assignment_to_a_name_dispatch_reads_through_the_facade_is_not_flagged(tmp_path):
    live = sorted(_names_dispatch_reads_through_facade())
    assert live
    f = tmp_path / "test_ok_assign.py"
    f.write_text(
        "from coordinator_core.bash_guards import dispatch_checks as dc\n"
        f"def test_x():\n    dc.{live[0]} = None\n",
        encoding="utf-8",
    )
    assert _scan([f]) == []
