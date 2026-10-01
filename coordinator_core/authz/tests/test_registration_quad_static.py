"""Function-level tests for the static registration-quad oracle, over in-memory sources."""

from __future__ import annotations

from coordinator_core.authz import registration_quad_static as rqs
from coordinator_core.authz.registration_quad_static import registration_violations

CLS = "coordinator_core/authz/classification.py"
SCOPE = "coordinator_core/op_scopes.py"
MAP = "coordinator_core/ops/_registry_map.py"
EAGER = "coordinator_core/ops/__init__.py"
LEDGER = "coordinator_core/authz/registration_quad.py"
OPFILE = "coordinator_core/ops/x/ops.py"


def _tree(ops_complete=(), debt=(), incomplete=None) -> dict[str, bytes]:
    cls = ",\n".join(f'    "{k}": 1' for k in ops_complete)
    scope = ",\n".join(f'    "{k}": "s"' for k in ops_complete)
    mp = ",\n".join(f'    "{k}": "pkg.mod"' for k in ops_complete)
    inc = ",\n".join(f'    "{k}": {tuple(v)!r}' for k, v in (incomplete or {}).items())
    debt_lit = "frozenset({" + ", ".join(f'"{d}"' for d in debt) + "})" if debt else "frozenset()"
    return {
        CLS: f"import types\nOP_CLASSIFICATION: types.MappingProxyType[str, int] = types.MappingProxyType({{\n{cls}\n}})\n".encode(),
        SCOPE: f"_OP_KEY_SCOPE: dict = {{\n{scope}\n}}\n".encode(),
        MAP: f"OP_MODULE_MAP: dict = {{\n{mp}\n}}\n".encode(),
        EAGER: b'_EAGER_OP_MODULES = [\n    ("pkg.mod", "note"),\n]\n',
        LEDGER: (
            f"_KNOWN_UNCLASSIFIED_OPS_DEBT = {debt_lit}\n"
            f"_KNOWN_INCOMPLETE_REGISTRATIONS = {{\n{inc}\n}}\n"
        ).encode(),
    }


def _reader(files: dict[str, bytes]):
    return files.get


def _with_op(files: dict[str, bytes], key: str) -> dict[str, bytes]:
    out = dict(files)
    out[OPFILE] = f'@register_op("{key}")\ndef h(): ...\n'.encode()
    return out


def test_r1_new_op_without_table_entries_refuses():
    files = _with_op(_tree(), "x.y")
    v = registration_violations(_reader(files), [OPFILE])
    assert v.outcome == "refuse"
    assert [q.op_key for q in v.violations] == ["x.y"]
    assert "x.y" in v.reason and CLS in v.reason


def test_r2_table_edits_left_out_of_paths_refuses():
    stale_head = _tree()
    files = _with_op(stale_head, "x.y")
    v = registration_violations(_reader(files), [OPFILE])
    assert v.outcome == "refuse"
    assert v.violations[0].surfaces_missing == (
        "OP_CLASSIFICATION",
        "_OP_KEY_SCOPE",
        "OP_MODULE_MAP",
    )


def test_r5_broken_surface_refuses_fail_closed():
    files = _with_op(_tree(("x.y",)), "x.y")
    files[SCOPE] = b"_OP_KEY_SCOPE = {"
    v = registration_violations(_reader(files), [OPFILE, SCOPE])
    assert v.outcome == "refuse"
    assert SCOPE in v.reason


def test_non_literal_surface_refuses():
    files = _with_op(_tree(("x.y",)), "x.y")
    files[MAP] = b"OP_MODULE_MAP = build()\n"
    assert registration_violations(_reader(files), [OPFILE]).outcome == "refuse"


def test_name_bound_twice_refuses():
    files = _with_op(_tree(("x.y",)), "x.y")
    files[SCOPE] = files[SCOPE] + files[SCOPE]
    v = registration_violations(_reader(files), [OPFILE])
    assert v.outcome == "refuse" and "more than once" in v.reason


def test_partially_present_surfaces_refuse():
    files = _with_op(_tree(("x.y",)), "x.y")
    del files[EAGER]
    v = registration_violations(_reader(files), [OPFILE])
    assert v.outcome == "refuse" and EAGER in v.reason


def test_p1_complete_op_passes():
    files = _with_op(_tree(("x.y",)), "x.y")
    v = registration_violations(_reader(files), [OPFILE])
    assert v.outcome == "pass" and v.violations == ()


def test_p2_no_coordinator_core_skips():
    files = {"tools/x.py": b'@register_op("x.y")\n'}
    assert registration_violations(_reader(files), ["tools/x.py"]).outcome == "skip"


def test_no_surfaces_in_tree_skips():
    files = {OPFILE: b'@register_op("x.y")\n'}
    assert registration_violations(_reader(files), [OPFILE]).outcome == "skip"


def test_no_keys_and_no_surface_path_skips_without_reading_surfaces():
    seen: list[str] = []

    def read(p):
        seen.append(p)
        return b"x = 1\n"

    assert registration_violations(read, ["coordinator_core/util.py"]).outcome == "skip"
    assert seen == ["coordinator_core/util.py"]


def test_p3_baselined_op_restaged_passes():
    files = _with_op(_tree(debt=("x.y",)), "x.y")
    files[SCOPE] = _tree(("x.y",))[SCOPE]
    files[MAP] = _tree(("x.y",))[MAP]
    assert registration_violations(_reader(files), [OPFILE]).outcome == "pass"


def test_baseline_forgives_only_recorded_surface():
    files = _tree(incomplete={"x.y": ("OP_MODULE_MAP",)})
    files[CLS] = _tree(("x.y",))[CLS]
    files = _with_op(files, "x.y")
    v = registration_violations(_reader(files), [OPFILE])
    assert v.outcome == "refuse"
    assert v.violations[0].surfaces_missing == ("_OP_KEY_SCOPE",)


def test_surface_edit_without_register_op_key_passes():
    files = _tree(("a.b",))
    assert registration_violations(_reader(files), [CLS]).outcome == "pass"


def test_backslash_paths_are_normalised():
    files = _with_op(_tree(("x.y",)), "x.y")
    assert registration_violations(_reader(files), [OPFILE.replace("/", "\\")]).outcome == "pass"


def test_module_not_in_eager_list_refuses():
    files = _with_op(_tree(("x.y",)), "x.y")
    files[EAGER] = b'_EAGER_OP_MODULES = [("other.mod", "n")]\n'
    v = registration_violations(_reader(files), [OPFILE])
    assert v.violations[0].surfaces_missing == ("_EAGER_OP_MODULES",)


def test_verdict_ignores_importing_packages_own_tables(monkeypatch):
    import coordinator_core.authz.classification as live

    files = _with_op(_tree(("x.y",)), "x.y")
    before = registration_violations(_reader(files), [OPFILE])
    monkeypatch.setattr(live, "OP_CLASSIFICATION", {})
    assert registration_violations(_reader(files), [OPFILE]) == before
    assert before.outcome == "pass"


def test_module_spawns_nothing():
    import ast

    tree = ast.parse(open(rqs.__file__, encoding="utf-8").read())
    imported = {
        a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for a in n.names
    } | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "subprocess" not in imported
