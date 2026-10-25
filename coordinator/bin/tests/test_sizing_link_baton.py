"""sizing-link-baton CLI: dry run, --write both edges, ambiguous exit 3, bad argv exit 2.

Run:
    python -m pytest -p no:cacheprovider -q coordinator/bin/tests/test_sizing_link_baton.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load():
    name = "sizing_link_baton_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_BIN_DIR / "sizing-link-baton.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load()

_BATON = "state/handoffs/2026-10-22-liveness-closeout.md"
_SIZING = "state/sizings/2026-10-22-liveness-closeout.yaml"


@pytest.fixture
def repo(tmp_path):
    # the matcher's `git grep --untracked` needs a real repository
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _BATON).write_text(
        '---\ntitle: "Liveness closeout"\ndeliverable_id: "dlv-live-aaaaaa"\n---\n\nbody\n',
        encoding="utf-8",
    )
    (tmp_path / _SIZING).write_text(
        'name: x\ndeliverable_id: "dlv-live-aaaaaa"\nstatus: open\n', encoding="utf-8"
    )
    return tmp_path


def _run(repo, capsys, *extra):
    rc = _MOD.main(["--repo-root", str(repo), "--baton", _BATON, *extra])
    rows = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    return rc, rows


def test_dry_run_writes_nothing(repo, capsys):
    before = {p: p.read_bytes() for p in (repo / _BATON, repo / _SIZING)}
    rc, rows = _run(repo, capsys)
    assert rc == 0
    assert rows == [{"baton": _BATON, "sizing": _SIZING, "basis": "deliverable_id",
                     "stamped": False, "conflict": None}]
    assert {p: p.read_bytes() for p in before} == before


def test_write_stamps_both_edges(repo, capsys):
    rc, rows = _run(repo, capsys, "--write")
    assert rc == 0 and rows[0]["stamped"] is True and rows[0]["conflict"] is None
    assert f'baton: "{_BATON}"' in (repo / _SIZING).read_text(encoding="utf-8")
    assert f'sizing_object: "{_SIZING}"' in (repo / _BATON).read_text(encoding="utf-8")
    rc, rows = _run(repo, capsys, "--write")
    assert rc == 0 and rows[0]["stamped"] is False


def test_ambiguous_exits_3_with_json(repo, capsys):
    (repo / "state" / "sizings" / "2026-10-22-other.yaml").write_text(
        'name: y\ndeliverable_id: "dlv-live-aaaaaa"\nstatus: open\n', encoding="utf-8"
    )
    rc, rows = _run(repo, capsys, "--write")
    assert rc == 3
    assert rows[0]["basis"] == "ambiguous" and rows[0]["sizing"] is None
    assert "baton:" not in (repo / _SIZING).read_text(encoding="utf-8")


def test_no_match_exits_3(repo, capsys):
    (repo / _SIZING).unlink()
    rc, rows = _run(repo, capsys)
    assert rc == 3 and rows[0]["basis"] == "none"


def test_bad_argv_exits_2(capsys):
    assert _MOD.main([]) == 2
