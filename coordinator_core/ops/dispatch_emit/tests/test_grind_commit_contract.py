"""Tests for the grind terminal-commit contract."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import grind_commit_contract as gcc
from coordinator_core.win_portability import no_console_creationflags


def _good() -> dict:
    return {
        "profile": "bug",
        "rows": [
            {"row_id": "a", "outcome": "committed", "paths": ["src/a.py"], "deleted_paths": []},
            {"row_id": "b", "outcome": "committed", "paths": ["src/b.py"], "deleted_paths": ["state/backlog/b.md"]},
        ],
        "halted": None,
    }


def test_reply_keys_are_pinned_and_documented():
    assert gcc.ALREADY_LANDED_KEY == "already_landed"
    assert gcc.UNLANDED_KEY == "unlanded"
    assert gcc.ALREADY_LANDED_KEY in gcc.__doc__ and gcc.UNLANDED_KEY in gcc.__doc__


def test_landing_constants_are_pinned_and_documented():
    assert gcc.DROP_PEER_HUNKS_CONFLICT == "peer-hunks-conflict"
    assert gcc.DROP_UNATTRIBUTED_SHARED == "unattributed-shared"
    assert gcc.REFUSED_IMPORT_CLOSURE == "import-closure"
    assert gcc.ROW_IMAGES_ROOT == ".coordinator-local/grind-row-images"
    assert gcc.IMAGE_VERB == "image"
    assert gcc.IMAGE_PHASES == ("pre", "post")
    for name in (gcc.DROP_PEER_HUNKS_CONFLICT, gcc.DROP_UNATTRIBUTED_SHARED, gcc.REFUSED_IMPORT_CLOSURE):
        assert name in gcc.__doc__ or name.upper().replace("-", "_") in gcc.__doc__
    for const in ("DROP_PEER_HUNKS_CONFLICT", "DROP_UNATTRIBUTED_SHARED", "REFUSED_IMPORT_CLOSURE"):
        assert const in gcc.__doc__


def test_row_images_rel_joins_safe_segments():
    assert gcc.row_images_rel("run1", "C1") == ".coordinator-local/grind-row-images/run1/C1"


@pytest.mark.parametrize(
    "run_id,row_id",
    [("..", "a"), ("r", ".."), ("r/x", "a"), ("r", "a/b"), ("", "a"), ("r", ""), ("r\\x", "a"), ("C:", "a"), ("/r", "a")],
)
def test_row_images_rel_refuses_unsafe_segment(run_id, row_id):
    with pytest.raises(ValueError):
        gcc.row_images_rel(run_id, row_id)


def test_accepts_well_formed_params():
    assert gcc.validate_grind_params(_good()) is None


def test_halted_str_and_absent_accepted():
    g = _good()
    g["halted"] = "budget-exhausted"
    assert gcc.validate_grind_params(g) is None
    del g["halted"]
    assert gcc.validate_grind_params(g) is None


def test_trailer_collapses_whitespace():
    assert gcc.render_row_trailer("a  b\n", " committed\t") == "Grind-Row: a b committed"


def test_is_grind_call():
    assert gcc.is_grind_call({gcc.GRIND_COMMIT_PARAM: {"rows": []}})
    assert not gcc.is_grind_call({gcc.GRIND_COMMIT_PARAM: {"rows": "x"}})
    assert not gcc.is_grind_call({gcc.GRIND_COMMIT_PARAM: []})
    assert not gcc.is_grind_call({})
    assert not gcc.is_grind_call(None)


def _mutated(fn) -> dict:
    g = copy.deepcopy(_good())
    fn(g)
    return g


@pytest.mark.parametrize(
    "mutate",
    [
        lambda g: g.update(profile=""),
        lambda g: g.update(profile=3),
        lambda g: g.update(rows="x"),
        lambda g: g["rows"].append("junk"),
        lambda g: g["rows"][0].update(row_id=""),
        lambda g: g["rows"][0].update(outcome=None),
        lambda g: g["rows"][1].update(row_id="a"),
        lambda g: g["rows"][0].update(paths="src/a.py"),
        lambda g: g["rows"][0].update(deleted_paths=None),
        lambda g: g["rows"][0].update(paths=[3]),
        lambda g: g["rows"][0].update(paths=["/abs/x.py"]),
        lambda g: g["rows"][0].update(paths=["C:/x.py"]),
        lambda g: g["rows"][0].update(paths=["a\\b.py"]),
        lambda g: g["rows"][0].update(paths=["a/../b.py"]),
        lambda g: g["rows"][0].update(deleted_paths=["../b.py"]),
        lambda g: g.update(halted=""),
        lambda g: g.update(halted=7),
    ],
)
def test_refuses_each_rule(mutate):
    assert isinstance(gcc.validate_grind_params(_mutated(mutate)), str)


_EVIDENCE = "state/queue-grind/bug/evidence/run1/a.done.md"


def test_validate_accepts_ledger_root_path():
    g = _mutated(lambda g: g["rows"][0].update(paths=["src/a.py", _EVIDENCE]))
    assert gcc.validate_grind_params(g) is None


def test_partition_splits_ledger_root_and_keeps_order():
    kept, dropped = gcc.partition_ledger_paths(["src/z.py", _EVIDENCE, "src/a.py"])
    assert kept == ["src/z.py", "src/a.py"]
    assert dropped == [_EVIDENCE]


def test_refuses_non_dict():
    assert isinstance(gcc.validate_grind_params([]), str)


def test_run_close_names_are_pinned():
    assert gcc.DECLARED_FLAG == "declared-urlenc"
    assert gcc.DECISIONS_FLAG == "decisions-file"
    assert gcc.QUEUE_FLAG == "queue"
    assert gcc.SWEEP_FLAG == "sweep"
    assert gcc.SWEEP_FAILED_HEAD == "drain sweep did not complete"
    assert gcc.BASELINE_DIRTY_KEY == "baseline_dirty"
    assert gcc.MINTED_KEY == "minted"
    assert gcc.RUN_RECORD_REQUIRED == ("profile", "run_id", "counts", "spend", "handed_back", "halted")
    assert gcc.HISTORICAL_RECORD_PREFIXES == (
        "state/warp/", "docs/plans/", "state/handoffs/", "state/sizings/", "docs/decisions/",
    )


def test_run_write_roots():
    roots = gcc.run_write_roots("bug", ["state/bug-backlog", "state/other/"])
    assert roots == (
        "state/bug-backlog/", "state/other/", "state/queue-grind/bug/",
        "state/sizings/", "state/handoffs/", "state/apm-rulings/",
    )
    assert all(r.endswith("/") for r in roots)
    assert gcc.run_write_roots("bug", ["state/sizings/"]).count("state/sizings/") == 1


def test_under_roots():
    roots = gcc.run_write_roots("bug", ["state/bug-backlog"])
    assert gcc.under_roots("state/bug-backlog/x.md", roots)
    assert gcc.under_roots("state/sizings/s.yaml", roots)
    assert not gcc.under_roots("state/bug-backlogger/x.md", roots)
    assert not gcc.under_roots("src/a.py", roots)


@pytest.mark.parametrize("path", [
    "state/warp/run/x.md", "docs/plans/p.md", "state/handoffs/h.md",
    "state/sizings/s.yaml", "docs/decisions/DR-1.md",
])
def test_historical_records_refused(path):
    assert gcc.is_historical_record(path)


@pytest.mark.parametrize("path", ["state/bug-backlog/a.md", "src/a.py", "state/warpish/x", "docs/plans"])
def test_work_product_not_historical(path):
    assert not gcc.is_historical_record(path)


@pytest.mark.parametrize("paths", [
    ["a/b.py"], [], ["it's/x.md", "state/sizings/café.yaml", "a b/c%d.md"],
])
def test_declared_paths_round_trip(paths):
    token = gcc.encode_declared_paths(paths)
    assert "'" not in token and " " not in token
    assert gcc.decode_declared_paths(token) == sorted(set(paths))


@pytest.mark.parametrize("token", ["%7B", "%5B%5D", "%7B%22paths%22%3A%20%5B1%5D%7D", "%7B%22x%22%3A%201%7D"])
def test_decode_refuses_malformed(token):
    with pytest.raises(ValueError):
        gcc.decode_declared_paths(token)


def _token() -> str:
    from urllib.parse import quote

    return quote(json.dumps({"a": {"b": "x" * 40}}), safe="")


def test_relay_digest_known_vector():
    assert gcc.relay_digest("abc") == (
        3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_relay_digest_refuses_non_ascii():
    with pytest.raises(ValueError):
        gcc.relay_digest("café")


def test_check_relay_accepts_match():
    tok = _token()
    n, h = gcc.relay_digest(tok)
    assert gcc.check_relay("record-urlenc", tok, str(n), h) is None


def test_check_relay_refuses_dropped_char_in_closing_braces():
    tok = _token()
    assert tok.endswith("%7D%7D")
    n, h = gcc.relay_digest(tok)
    bad = tok[:-1]
    msg = gcc.check_relay("record-urlenc", bad, str(n), h)
    assert msg is not None and msg.startswith(gcc.RELAY_CORRUPT_HEAD)
    assert "--record-urlenc" in msg and str(n) in msg and str(n - 1) in msg
    assert h[:12] in msg and "\n" not in msg


@pytest.mark.parametrize("length,sha", [("x", "0" * 64), ("-1", "0" * 64), ("", "0" * 64),
                                        ("3", "ABC"), ("3", "g" * 64), ("3", "")])
def test_check_relay_reports_malformed_digest_without_raising(length, sha):
    msg = gcc.check_relay("declared-urlenc", "abc", length, sha)
    assert msg is not None and msg.startswith(gcc.RELAY_CORRUPT_HEAD)


def test_check_relay_reports_non_ascii_token_without_raising():
    msg = gcc.check_relay("record-urlenc", "café", "4", "0" * 64)
    assert msg is not None and msg.startswith(gcc.RELAY_CORRUPT_HEAD)


@pytest.mark.parametrize("word", ["crypto", "Date", "Math.random", "require", "import"])
def test_relay_sha256_js_uses_no_unsupplied_global(word):
    assert word not in gcc.RELAY_SHA256_JS


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_relay_sha256_js_matches_hashlib_under_node(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    from urllib.parse import quote

    samples = ["", "abc", quote(json.dumps({"k": "{}é" * 4000}), safe="")]
    assert len(samples[2]) > 20000
    js = gcc.RELAY_SHA256_JS + (
        "\nconsole.log(JSON.stringify(JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8')).map(_sha256Ascii)));")
    samples_file = tmp_path / "samples.json"
    samples_file.write_text(json.dumps(samples), encoding="utf-8")
    res = subprocess.run([node, "-", str(samples_file)], input=js, capture_output=True, text=True,
                         check=False, **no_console_creationflags())
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == [gcc.relay_digest(s)[1] for s in samples]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_import_adds_no_non_stdlib_module():
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import coordinator_core.ops.dispatch_emit.grind_commit_contract\n"
        "new = set(sys.modules) - before\n"
        "print('\\n'.join(sorted(m for m in new if m.split('.')[0] == 'coordinator_core')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.split()
    allowed = {
        "coordinator_core.ops.dispatch_emit.grind_commit_contract",
        "coordinator_core._engine_landing",
        "coordinator_core.op_scopes",
        "coordinator_core.session",
        "coordinator_core.session.record_homes",
    }
    extra = [m for m in out if m not in allowed and not any(allowed_pkg == m for allowed_pkg in (
        "coordinator_core", "coordinator_core.ops", "coordinator_core.ops.dispatch_emit"))]
    assert extra == []
