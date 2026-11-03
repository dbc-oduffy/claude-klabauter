"""Pin: every sizing and handoff writer places a new record at `<tree>/<YYYY-MM>/<name>`, and a flat
literal sizing path keeps resolving.

(a) ratchet: a static scan of non-test engine modules for a dated flat `state/(sizings|handoffs)/` path
    (f-string, parts-joined `os.path.join`, or JS literal); readers that name an EXISTING record by
    `{basename}`-style names are not dated and never trip it.
(b) fresh run: each writer's path constructor is called in-process and must return the month shard.
(c) flat literal: a flat sizing cited by its literal path still resolves.

Negative-spec: zero subprocess spawns; static scan is stdlib `ast` only, no import of scanned modules.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
from functools import lru_cache
from pathlib import Path

import pytest

from coordinator_core.ops.generator_census.assemble import _is_test_scaffolding
from coordinator_core.session import output_tree_layout as otl
from coordinator_core.session import record_homes

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCAN_TOPS = ("coordinator_core", "coordinator/bin")
_CLI_PATH = Path(_REPO_ROOT) / "coordinator" / "bin" / "coordinator-doc-new.py"
_TODAY = "2026-11-03"
_MONTH = "2026-11"

_TREE = r"state/(?:sizings|handoffs)/"
_DATE_LEAD = r"\{(?:today|date_str)\}"
_DATE_PARTS = r"\{d\[:4\]\}[^/]*\{d\[6:8\]\}"
_DATED_FLAT_PY = re.compile(_TREE + r"(?:" + _DATE_LEAD + "|" + _DATE_PARTS + ")")
_DATED_FLAT_JS = re.compile(_TREE + r"'\s*\+\s*d\.slice\(0, 4\)(?:(?!'/')[\s\S])*?slice\(6, 8\)")
_JOIN_TREES = ("handoffs", "sizings")


def _fstring_text(node: ast.JoinedStr) -> str:
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        elif isinstance(v, ast.FormattedValue):
            parts.append("{" + ast.unparse(v.value) + "}")
    return "".join(parts)


def _is_os_path_join(func: ast.expr) -> bool:
    return isinstance(func, ast.Attribute) and func.attr == "join" and "path" in ast.unparse(func.value)


def _dated_flat_sites(text: str) -> int:
    """Count dated flat sizing/handoff path constructions outside docstrings."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return 0
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
    count = 0
    nested_fstring_parts: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            nested_fstring_parts.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            if _DATED_FLAT_PY.search(_fstring_text(node)):
                count += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in nested_fstring_parts:
                continue
            if _DATED_FLAT_JS.search(node.value):
                count += 1
        elif isinstance(node, ast.Call) and _is_os_path_join(node.func) and len(node.args) >= 3:
            a0, a1, a2 = node.args[0], node.args[1], node.args[2]
            if (
                isinstance(a0, ast.Constant) and a0.value == "state"
                and isinstance(a1, ast.Constant) and a1.value in _JOIN_TREES
                and isinstance(a2, ast.JoinedStr)
                and re.match(_DATE_LEAD, _fstring_text(a2))
            ):
                count += 1
    return count


@lru_cache(maxsize=1)
def _offenders() -> dict[str, int]:
    found: dict[str, int] = {}
    for top in _SCAN_TOPS:
        for dirpath, _dirs, files in os.walk(os.path.join(_REPO_ROOT, top)):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")
                if _is_test_scaffolding(rel):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        text = fh.read()
                except OSError:
                    continue
                n = _dated_flat_sites(text)
                if n:
                    found[rel] = n
    return found


def test_no_engine_module_builds_a_dated_flat_sizing_or_handoff_path():
    assert not _offenders(), (
        "dated flat state/sizings|handoffs path construction (route through "
        f"output_tree_layout.write_rel): {_offenders()}"
    )


def test_detector_flags_dated_flat_writers_and_spares_readers_and_shard_forms():
    assert _dated_flat_sites('p = f"state/handoffs/{today}-{slug}.md"\n') == 1
    assert _dated_flat_sites('p = f"state/sizings/{date_str}-{slug}.yaml"\n') == 1
    assert _dated_flat_sites('p = f"state/sizings/{d[:4]}-{d[4:6]}-{d[6:8]}-x.yaml"\n') == 1
    assert _dated_flat_sites('p = os.path.join("state", "handoffs", f"{today}_000000_roadmap-x.md")\n') == 1
    flat_js = "s = \"return 'state/sizings/' + d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8) + '-x.yaml'\"\n"
    assert _dated_flat_sites(flat_js) == 1
    shard_js = (
        "s = \"return 'state/sizings/' + d.slice(0, 4) + '-' + d.slice(4, 6) + '/' + d.slice(0, 4)"
        " + '-' + d.slice(4, 6) + '-' + d.slice(6, 8)\"\n"
    )
    assert _dated_flat_sites(shard_js) == 0
    assert _dated_flat_sites('p = f"state/sizings/{d[:4]}-{d[4:6]}/{d[:4]}-{d[4:6]}-{d[6:8]}-x.yaml"\n') == 0
    assert _dated_flat_sites('p = f"state/handoffs/{today[:7]}/{today}-x.md"\n') == 0
    for reader in (
        'p = f"state/handoffs/{basename}"\n',
        'p = f"state/sizings/{entry.name}"\n',
        'p = f"state/sizings/{target.name}"\n',
        'p = f"state/sizings/{n}.yaml"\n',
        'p = os.path.join("state", "handoffs")\n',
        'p = os.path.join("state", "handoffs", name)\n',
    ):
        assert _dated_flat_sites(reader) == 0, reader
    assert _dated_flat_sites('"""state/handoffs/{today}-x.md"""\nx = 1\n') == 0


# --- (b) fresh run: every writer's path constructor lands at the month shard --------------------------


def _assert_shard(path: str, tree: str, name_prefix: str | None = None) -> None:
    p = path.replace("\\", "/")
    m = re.match(rf"^{re.escape(tree)}/(\d{{4}}-\d{{2}})/(\1)-\d{{2}}[-_].+$", p)
    assert m, f"{p!r} is not {tree}/<YYYY-MM>/<same-month dated name>"
    if name_prefix is not None:
        assert p.startswith(name_prefix), p


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_sharded_writers", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_new_default_paths_for_handoff_sizing_and_roadmap_baton_are_sharded(cli, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: _TODAY)

    def posix(p: str) -> str:
        return p.replace("\\", "/")

    assert posix(cli._default_output_path("handoff", "Shard baton", None)) == (
        f"state/handoffs/{_MONTH}/{_TODAY}-shard-baton.md"
    )
    assert posix(cli._default_output_path("sizing-object", "Shard sizing", None)) == (
        f"state/sizings/{_MONTH}/{_TODAY}-shard-sizing.yaml"
    )
    assert posix(cli._default_output_path("roadmap-baton", "t", None, stub_id="stub-1")) == (
        f"state/handoffs/{_MONTH}/{_TODAY}_000000_roadmap-stub-1.md"
    )
    assert cli._baton_rel_for_sizing("state/sizings/2026-11-03-x.yaml") == f"state/handoffs/{_MONTH}/2026-11-03-x.md"
    assert cli._baton_rel_for_sizing("state/sizings/x.yaml") == f"state/handoffs/{_MONTH}/{_TODAY}-x.md"


def test_baton_assemble_fresh_output_path_is_sharded():
    from coordinator_core import baton_assemble as ba

    out = ba._compute_fresh_output_path("docs/plans/2026-11-03-some-plan.md")
    _assert_shard(out, "state/handoffs")
    assert ba._repo_rel_handoff_out_path(f"{_TODAY}-x.md") == f"state/handoffs/{_MONTH}/{_TODAY}-x.md"


def test_sizing_assemble_scaffold_directive_out_is_sharded():
    from coordinator_core import sizing_assemble as sa

    directive = sa._sizing_object_scaffold_directive("an intent", "shard probe")
    outs = [a[len("--out="):] for a in directive["args"] if a.startswith("--out=")]
    assert len(outs) == 1
    _assert_shard(outs[0], "state/sizings")


def test_roadmap_planning_assemble_baton_and_seed_outs_are_sharded():
    from coordinator_core import roadmap_planning_assemble as rpa

    directives = rpa._roadmap_baton_and_seed_directives("B", "run-1", "stub-1", None, None, ["g1"])
    outs = [a[len("--out="):] for d in directives for a in d["args"] if a.startswith("--out=")]
    assert len(outs) == 2
    for out in outs:
        _assert_shard(out, "state/handoffs")


def test_goals_seed_directive_out_is_sharded():
    from coordinator_core import goals

    directive = goals._goal_seed_directive("Deferred slice", None)
    outs = [a[len("--out="):] for a in directive["args"] if a.startswith("--out=")]
    assert len(outs) == 1
    _assert_shard(outs[0], "state/handoffs")


def test_ask_compose_scout_sizing_prefix_is_sharded():
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

    script = _compose()
    writes = re.findall(r"--write \S*?(state/sizings/\S*?)<name>", script)
    assert writes, "no --write template found for the scout"
    for w in writes:
        assert re.match(r"^state/sizings/\d{4}-\d{2}/(\d{4}-\d{2})-\d{2}-$", w), w
        assert w.split("/")[2] == w.split("/")[3][:7]
    js = re.search(r"const _sizingPathFor = [^\n]*", script).group(0)
    assert re.search(r"'state/sizings/\d{4}-\d{2}/'", js), js


def test_grind_sizing_path_is_sharded():
    from coordinator_core.ops.dispatch_emit import grind_compose as gc

    assert gc._sizing_path("20261103T000000Z", {"name": "Scout Name"}, "r0") == (
        f"state/sizings/{_MONTH}/{_TODAY}-scout-name-r0.yaml"
    )


def test_record_path_places_a_fresh_name_at_the_shard_for_both_trees(tmp_path):
    root = str(tmp_path)
    for kind, name in (("sizings", f"{_TODAY}-fresh.yaml"), ("handoffs", f"{_TODAY}-fresh.md")):
        got = Path(record_homes.record_path(root, kind, name))
        assert got == Path(record_homes.home_dir(root, kind)) / _MONTH / name


# --- (c) flat literal: a flat sizing cited by its literal path still resolves ------------------------------


def _touch(path: Path, text: str = "status: sized\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_flat_literal_sizing_citation_resolves_to_the_flat_file(tmp_path):
    from coordinator_core.ops._sizing_citation import resolve_sizing_citation
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

    cite = f"state/sizings/{_TODAY}-x.yaml"
    flat = _touch(tmp_path / "state" / "sizings" / f"{_TODAY}-x.yaml")

    assert resolve_sizing_citation(tmp_path, cite) == flat.resolve()
    assert otl.locate(str(tmp_path), cite) == cite
    assert load_sizing(tmp_path, cite)["status"] == "sized"
    assert Path(record_homes.record_path(str(tmp_path), "sizings", f"{_TODAY}-x.yaml")) == flat


def test_flat_literal_citation_of_a_sharded_sizing_resolves_and_an_absent_one_does_not(tmp_path):
    from coordinator_core.ops._sizing_citation import resolve_sizing_citation

    shard = _touch(tmp_path / "state" / "sizings" / _MONTH / f"{_TODAY}-y.yaml")

    assert resolve_sizing_citation(tmp_path, f"state/sizings/{_TODAY}-y.yaml") == shard.resolve()
    assert resolve_sizing_citation(tmp_path, f"state/sizings/{_TODAY}-absent.yaml") is None
