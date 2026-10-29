"""Tests for in_run_widen: classifier, resize verdict, run record, JS parity."""

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import in_run_widen as w

ROOT = "/repo"


def _reply(value):
    return f"BLOCKED: footprint mismatch\nROW-LOCAL-BLOCK: {value}\n"


@pytest.mark.parametrize(
    "old,new,expected",
    [
        ("S", "L", "continue"),
        ("M", "XL", "continue"),
        ("XS", "L", "continue"),
        ("XL", "XXL", "continue"),
        ("XS", "XL", "flag"),
        ("S", "XXL", "flag"),
        ("L", "XXL", "flag"),
        ("M", "XXL", "flag"),
        ("M", "M", "continue"),
        ("L", "S", "continue"),
        ("s", "l", "continue"),
        ("S", "HUGE", "flag"),
    ],
)
def test_resize_verdict(old, new, expected):
    assert w.resize_verdict(old, new) == expected


CASES = [
    (_reply("coordinator_core/ops/foo.py"), (), "widen", "coordinator_core/ops/foo.py"),
    (_reply("`coordinator_core/ops/foo.py`"), (), "widen", "coordinator_core/ops/foo.py"),
    ("**ROW-LOCAL-BLOCK**: a/b.py", (), "widen", "a/b.py"),
    ("x\\nROW-LOCAL-BLOCK: a/b.py\\ny", (), "widen", "a/b.py"),
    (_reply("a/b.py"), ("A/B.py",), "halt", None),
    (_reply("state/foo.yaml"), (), "halt", None),
    (_reply("CLAUDE.md"), (), "halt", None),
    (_reply("sub/dir/CLAUDE.md"), (), "halt", None),
    (_reply("coordinator.local.md"), (), "halt", None),
    (_reply("global-doctrine/CLAUDE.md"), (), "halt", None),
    (_reply("coordinator/snippets/em-operating-doctrine.md"), (), "halt", None),
    (_reply(".claude/rules/x.md"), (), "halt", None),
    (_reply(".git/config"), (), "halt", None),
    (_reply("../outside.py"), (), "halt", None),
    (_reply("/abs/path.py"), (), "halt", None),
    (_reply("C" + ":/x/y.py"), (), "halt", None),
    (_reply("a\\b.py"), (), "halt", None),
    (_reply("a.py, b.py"), (), "halt", None),
    (_reply("dir/"), (), "halt", None),
    (_reply("none"), (), "halt", None),
    (_reply("N/A"), (), "halt", None),
    ("ROW-LOCAL-BLOCK: a.py\nROW-LOCAL-BLOCK: b.py", (), "halt", None),
    ("BLOCKED: needs a human decision", (), "halt", None),
    ("", (), "halt", None),
]


@pytest.mark.parametrize("text,held,kind,path", CASES)
def test_classify_block(text, held, kind, path):
    got = w.classify_block(text, ROOT, held)
    if kind == "widen":
        assert got == w.Widen(path)
    else:
        assert isinstance(got, w.Halt) and got.reason


def test_doctrine_path_halts_via_guard_constants():
    for parts in (*w._CONTENT_ONLY_SURFACES, *w._PER_REPO_SURFACES):
        assert isinstance(w.classify_block(_reply("/".join(parts)), ROOT, ()), w.Halt)


def test_ordinary_source_file_widens_so_denial_is_not_blanket():
    assert w.classify_block(_reply("coordinator_core/x.py"), ROOT, ()) == w.Widen("coordinator_core/x.py")


def test_record_and_read_back(tmp_path):
    run = tmp_path / "run"
    assert w.widened_paths(run) == []
    w.record_widen(run, "C1", "a/b.py")
    w.record_widen(run, "C1", "a/b.py")
    w.record_widen(run, "C2", "a/b.py")
    w.record_widen(run, "C2", "c.py")
    assert w.widened_paths(run) == ["a/b.py", "c.py"]
    lines = (run / w.WIDEN_RECORD_NAME).read_text().splitlines()
    assert len(lines) == 3 and all(json.loads(x) for x in lines)


def test_widened_paths_skips_corrupt_lines(tmp_path):
    (tmp_path / w.WIDEN_RECORD_NAME).write_text('garbage\n{"row": "C1", "path": "a.py"}\n[1]\n')
    assert w.widened_paths(tmp_path) == ["a.py"]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_js_parity_with_python(tmp_path):
    held = ["held/file.py"]
    cases = [(t, h if h else held, k) for t, h, k, _ in CASES]
    cases.append((_reply("held/file.py"), held, "halt"))
    script = (
        w.classify_block_js()
        + "\nconst cases = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));\n"
        "console.log(JSON.stringify(cases.map(([t, h]) => classifyBlock(t, h))));\n"
    )
    js = tmp_path / "run.js"
    cj = tmp_path / "cases.json"
    js.write_text(script, encoding="utf-8")
    cj.write_text(json.dumps([[t, list(h)] for t, h, _ in cases]), encoding="utf-8")
    out = subprocess.run(["node", str(js), str(cj)], capture_output=True, text=True, check=True, timeout=30)
    results = json.loads(out.stdout)
    for (text, h, _), js_res in zip(cases, results):
        py = w.classify_block(text, ROOT, h)
        if isinstance(py, w.Widen):
            assert js_res == {"widen": py.path}, text
        else:
            assert "halt" in js_res, text


def test_js_extra_denylist_halts():
    src = w.classify_block_js(["secret/"])
    assert "secret/" in src


def test_record_widens_persists_wellformed_items_and_skips_the_rest(tmp_path):
    from coordinator_core.ops.dispatch_emit.in_run_widen import record_widens, widened_paths

    n = record_widens(
        tmp_path,
        [{"row": "r1", "path": "tests/test_a.py"}, {"row": "r1", "path": "../x"}, {"row": "", "path": "b.py"}, "junk"],
    )
    assert n == 1
    assert widened_paths(tmp_path) == ["tests/test_a.py"]
    assert record_widens(tmp_path, None) == 0


def test_widened_by_row_groups_paths_per_row_in_order_without_duplicates(tmp_path):
    for row, path in (("r1", "a.py"), ("r2", "b.py"), ("r1", "c.py"), ("r1", "a.py")):
        w.record_widen(tmp_path, row, path)
    assert w.widened_by_row(tmp_path) == {"r1": ["a.py", "c.py"], "r2": ["b.py"]}
    assert w.widened_by_row(tmp_path / "absent") == {}


def test_record_widens_refuses_denied_surfaces_the_classifier_halts_on(tmp_path):
    items = [{"row": "r1", "path": p} for p in ("state/x.yaml", "CLAUDE.md", "sub/CLAUDE.md", ".git/config", "a//b.py")]
    assert w.record_widens(tmp_path, items) == 0
    assert w.widened_paths(tmp_path) == []
