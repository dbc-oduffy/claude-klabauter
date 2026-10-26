"""Pins the prep slice id helper: run-mode prefix equals the driver's id, outputs unchanged,
module import-cheap."""
import ast
from pathlib import Path

from coordinator_core.ops import review_slice_id as m
from coordinator_core.ops.review_slice_id import prep_slice_id_for, run_prep_slice_prefix

RUN = "warp-2026-10-26-avatarsdk-dense-measurement-extract-4d8b8b-20261026T133215-c4413e"
BASE = "a1b2c3d4e5f6" + "0" * 28


def test_run_prefix_equals_driver_prep_id():
    assert run_prep_slice_prefix(RUN, BASE) == prep_slice_id_for("x/y/manifest.json", BASE, RUN)


def test_run_prefix_keeps_run_token_and_prep_suffix():
    out = run_prep_slice_prefix(RUN, BASE)
    assert "-c4413e-" in out and out.endswith("-prep")
    assert out == f"manifest-{RUN}-a1b2c3d4e5f6-prep"


def test_prep_slice_id_outputs_unchanged():
    a = prep_slice_id_for("docs/plans/a.md", "1" * 40)
    assert a == "a-111111111111-prep"
    assert a != prep_slice_id_for("docs/plans/b.md", "1" * 40)
    assert a != prep_slice_id_for("docs/plans/a.md", "2" * 40)
    assert prep_slice_id_for("", "1" * 40, "run-a") == "run-a-111111111111-prep"
    assert prep_slice_id_for("x/manifest.json", "1" * 40, "run-b") == "manifest-run-b-111111111111-prep"
    assert prep_slice_id_for("../../x:y.md", "1" * 40, "r/../1") == "x-y-r-..-1-111111111111-prep"
    assert prep_slice_id_for("p.md", None) == "p-nobase-prep"


def test_module_imports_only_re_pathlib_typing():
    tree = ast.parse(Path(m.__file__).read_text(encoding="utf-8"))
    roots = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            roots |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            roots.add((n.module or "").split(".")[0])
    assert roots == {"re", "pathlib", "typing"}
