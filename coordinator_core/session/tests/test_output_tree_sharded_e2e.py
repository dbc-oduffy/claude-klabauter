"""End-to-end acceptance for sharded output trees: the default writers emit `<tree>/<YYYY-MM>/<file>`,
while every named reader still resolves a flat entry and a sharded one cited by its flat path.

Also measures `locate`: zero subprocess spawns and a sub-millisecond process time on a 4452-entry flat tree.
The writers run in-process through `main()`, so the writer legs add no spawn of their own.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.session import output_tree_layout as otl
from coordinator_core.session import record_homes
from coordinator_core.tests.git_seed import seeded_repo

pytestmark = pytest.mark.complete

_CLI_PATH = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "coordinator-doc-new.py"
_TODAY = "2026-11-03"
_MONTH = "2026-11"
_FLAT_ENTRIES = 4452
_LOCATE_CALLS = 1000
_TIMED_CALLS = 20000
_LOCATE_BUDGET_S = 0.001


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_sharded_e2e", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _touch(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(path: Path | str, root: Path) -> str:
    return Path(path).resolve().relative_to(root.resolve()).as_posix()


def _run_main(cli, monkeypatch, cwd: Path, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["coordinator-doc-new", *args])
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "_today", lambda: _TODAY)
    try:
        return int(cli.main() or 0)
    except SystemExit as exc:
        return int(exc.code or 0)


# --- writers: the default invocation lands at the month shard ------------------------------------


def test_default_writers_land_at_the_month_shard(cli, monkeypatch, tmp_path, capsys):
    root = tmp_path

    assert _run_main(cli, monkeypatch, root, "--type", "plan", "--title", "Shard plan", "--no-sizing-object") == 0
    plan = root / "docs" / "plans" / _MONTH / f"{_TODAY}-shard-plan.md"
    assert plan.is_file()
    assert (root / "docs" / "plans" / f"{_TODAY}-shard-plan.md").exists() is False

    assert (
        _run_main(
            cli, monkeypatch, root, "--type", "sizing-object", "--premise", "read",
            "--premise-evidence", "tests: premise read", "--title", "Shard sizing",
        )
        == 0
    )
    sizing = root / "state" / "sizings" / _MONTH / f"{_TODAY}-shard-sizing.yaml"
    assert sizing.is_file()

    assert _run_main(cli, monkeypatch, root, "--type", "handoff", "--title", "Shard baton", "--new-chain") == 0
    handoff = root / "state" / "handoffs" / _MONTH / f"{_TODAY}-shard-baton.md"
    assert handoff.is_file()

    for tree in ("docs/plans", "state/sizings", "state/handoffs"):
        flat_files = [p for p in (root / tree).glob("*") if p.is_file()]
        assert flat_files == [], f"{tree} grew a flat file: {flat_files}"
    assert _MONTH in capsys.readouterr().out


def test_delivery_note_rel_is_sharded_for_ask_and_warp_ids():
    from coordinator_core.ops.dispatch_emit.ask_contract import delivery_note_rel

    assert delivery_note_rel("ask-20261103T072223-8a7c52") == "docs/delivery/2026-11/ask-20261103T072223-8a7c52.md"
    assert (
        delivery_note_rel("warp-2026-11-03-d1d77f-1-x-20261103T072346-563e91")
        == "docs/delivery/2026-11/warp-2026-11-03-d1d77f-1-x-20261103T072346-563e91.md"
    )


# --- readers: flat entries resolve, sharded entries resolve when cited by their flat path -------------


def test_record_path_places_new_records_at_the_shard_and_keeps_flat_ones(tmp_path):
    root = str(tmp_path)
    for kind, name in (("sizings", f"{_TODAY}-a.yaml"), ("handoffs", f"{_TODAY}-a.md")):
        home = record_homes.home_dir(root, kind)
        got = Path(record_homes.record_path(root, kind, name))
        assert got == Path(home) / _MONTH / name

        _touch(Path(home) / name)
        assert Path(record_homes.record_path(root, kind, name)) == Path(home) / name

    assert record_homes.record_path("", "sizings", f"{_TODAY}-a.yaml") == str(
        Path(record_homes.home_dir("", "sizings")) / f"{_TODAY}-a.yaml"
    )


def test_delivery_note_locate_reads_flat_and_sharded_notes(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import delivery_note_locate

    flat_id, shard_id = "ask-20261103T072223-aaaaaa", "ask-20261103T072224-bbbbbb"
    _touch(tmp_path / "docs" / "delivery" / f"{flat_id}.md")
    _touch(tmp_path / "docs" / "delivery" / _MONTH / f"{shard_id}.md")

    assert delivery_note_locate(tmp_path, flat_id) == f"docs/delivery/{flat_id}.md"
    assert delivery_note_locate(tmp_path, shard_id) == f"docs/delivery/{_MONTH}/{shard_id}.md"


def test_sizing_resolvers_return_flat_sharded_and_flat_cited_shard(tmp_path):
    from coordinator_core.ops._research_sizing import _resolve
    from coordinator_core.ops._sizing_citation import resolve_sizing_citation
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

    flat = _touch(tmp_path / "state" / "sizings" / "2026-11-01-flat.yaml", "status: sized\n")
    shard = _touch(tmp_path / "state" / "sizings" / _MONTH / "2026-11-02-shard.yaml", "status: sized\n")

    assert resolve_sizing_citation(tmp_path, "state/sizings/2026-11-01-flat.yaml") == flat.resolve()
    assert resolve_sizing_citation(tmp_path, "state/sizings/2026-11-02-shard.yaml") == shard.resolve()
    assert resolve_sizing_citation(tmp_path, f"state/sizings/{_MONTH}/2026-11-02-shard.yaml") == shard.resolve()
    assert resolve_sizing_citation(tmp_path, "state/sizings/2026-11-09-absent.yaml") is None

    assert load_sizing(tmp_path, "state/sizings/2026-11-01-flat.yaml")["status"] == "sized"
    assert load_sizing(tmp_path, "state/sizings/2026-11-02-shard.yaml")["status"] == "sized"

    assert _resolve(tmp_path, "state/sizings/2026-11-01-flat.yaml") == flat.resolve()
    assert _resolve(tmp_path, "state/sizings/2026-11-02-shard.yaml") == shard.resolve()


def test_s_plan_path_names_the_shard_and_s_plan_located_follows_a_flat_plan(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_fire import s_plan_located, s_plan_path

    sizing = "state/sizings/2026-11-03-thing.yaml"
    assert s_plan_path(sizing) == f"docs/plans/{_MONTH}/2026-11-03-thing.md"
    assert s_plan_located(tmp_path, sizing) == f"docs/plans/{_MONTH}/2026-11-03-thing.md"

    _touch(tmp_path / "docs" / "plans" / "2026-11-03-thing.md")
    assert s_plan_located(tmp_path, sizing) == "docs/plans/2026-11-03-thing.md"


def test_terminal_sizing_drain_scan_lists_flat_and_sharded_sizings(tmp_path):
    from coordinator_core.ops.fleet.terminal_sizing_drain import scan_sizings

    _touch(tmp_path / "state" / "sizings" / "2026-10-30-flat.yaml", "status: shipped\n")
    _touch(tmp_path / "state" / "sizings" / _MONTH / "2026-11-02-shard.yaml", "status: shipped\n")

    assert set(scan_sizings(tmp_path).records) == {
        "state/sizings/2026-10-30-flat.yaml",
        f"state/sizings/{_MONTH}/2026-11-02-shard.yaml",
    }


def test_sizing_match_pairs_a_baton_with_a_flat_and_a_sharded_sizing(tmp_path):
    from coordinator_core.roadmap.sizing_match import match_sizings

    batons = []
    for tag, shard in (("flat", False), ("shard", True)):
        baton_rel = f"state/handoffs/2026-11-0{1 if not shard else 2}-{tag}.md"
        _touch(tmp_path / baton_rel, f'---\ndeliverable_id: "dlv-{tag}-abcdef"\n---\n')
        sizing_dir = tmp_path / "state" / "sizings" / (_MONTH if shard else "")
        _touch(
            sizing_dir / f"2026-11-0{1 if not shard else 2}-{tag}.yaml",
            f'deliverable_id: "dlv-{tag}-abcdef"\nstatus: sized\nbaton: {baton_rel}\n',
        )
        batons.append(baton_rel)

    got = {m["baton"]: m for m in match_sizings(tmp_path, batons)}
    assert got[batons[0]]["sizing"] == "state/sizings/2026-11-01-flat.yaml"
    assert got[batons[1]]["sizing"] == f"state/sizings/{_MONTH}/2026-11-02-shard.yaml"


def test_resolve_baton_path_follows_a_flat_citation_to_the_shard(tmp_path):
    from coordinator_core.ops.resolve_baton_path import _resolve_baton_path_and_repo

    repo = seeded_repo(tmp_path / "repo", branch="work/test/2026-01-01")
    flat = _touch(repo / "state" / "handoffs" / "2026-11-01-flat.md")
    shard = _touch(repo / "state" / "handoffs" / _MONTH / "2026-11-02-shard.md")

    got_flat = _resolve_baton_path_and_repo({"baton_path": str(flat)})
    assert got_flat["git_relative_path"] == "state/handoffs/2026-11-01-flat.md"

    cited = repo / "state" / "handoffs" / "2026-11-02-shard.md"
    got = _resolve_baton_path_and_repo({"baton_path": str(cited)})
    assert got["git_relative_path"] == f"state/handoffs/{_MONTH}/2026-11-02-shard.md"
    assert Path(got["abs_path"]) == shard.resolve()


def test_completion_ops_handoff_scan_sees_flat_and_sharded_handoffs(tmp_path):
    from coordinator_core.ops.completion_ops import _iter_files, _resolve_handoff_dirs

    flat = _touch(tmp_path / "state" / "handoffs" / "2026-11-01-flat.md")
    shard = _touch(tmp_path / "state" / "handoffs" / _MONTH / "2026-11-02-shard.md")

    dirs, _warning = _resolve_handoff_dirs(tmp_path)
    assert set(_iter_files(dirs[0])) == {flat, shard}


def test_pickup_assemble_live_listers_find_flat_and_sharded_artifacts(tmp_path):
    from coordinator_core import pickup_assemble as pa

    repo = tmp_path / "repo"
    seeded_repo(repo, branch="work/test/2026-01-01")
    flat = _touch(repo / "state" / "handoffs" / "2026-11-01_flat-slug.md")
    shard = _touch(repo / "state" / "handoffs" / _MONTH / "2026-11-02_shard-slug.md")
    plan = _touch(repo / "docs" / "plans" / _MONTH / "2026-11-03-sharded-plan.md")

    assert pa._live_fallback_search(repo, "2026-11-01_flat-slug.md") == [flat]
    assert pa._live_fallback_search(repo, "2026-11-02_shard-slug.md") == [shard]
    assert pa._live_fallback_search(repo, "2026-11-03-sharded-plan.md") == [plan]
    assert pa._live_fallback_search_suffix(repo, "shard-slug") == [shard]
    assert pa._live_fallback_search_suffix(repo, "flat-slug") == [flat]


@pytest.mark.parametrize(
    "baton_rel",
    ["state/handoffs/2026-11-02_b.md", f"state/handoffs/{_MONTH}/2026-11-02_b.md"],
)
def test_close_background_baton_containment_accepts_flat_cite_of_a_shard(tmp_path, baton_rel):
    from coordinator_core.ops.ceremony import close_background as cb

    _touch(tmp_path / "docs" / "plans" / "p.md", "---\nplan_id: p\nsizing_object: state/sizings/s.yaml\n---\n")
    _touch(tmp_path / "state" / "sizings" / "s.yaml", f"baton: {baton_rel}\n")
    _touch(tmp_path / "state" / "handoffs" / _MONTH / "2026-11-02_b.md")

    ctx = cb.JobContext(tmp_path, tmp_path / ".git", "run", (), None)
    assert cb._cited(ctx, "docs/plans/p.md")[1] == f"state/handoffs/{_MONTH}/2026-11-02_b.md"

    flat = _touch(tmp_path / "state" / "handoffs" / "2026-11-01_a.md")
    _touch(tmp_path / "state" / "sizings" / "s.yaml", "baton: state/handoffs/2026-11-01_a.md\n")
    assert cb._cited(ctx, "docs/plans/p.md")[1] == _rel(flat, tmp_path)


# --- measurement: locate is spawn-free and sub-millisecond on a large flat tree ---------------------


def test_locate_spawns_nothing_and_runs_under_a_millisecond(tmp_path, monkeypatch):
    tree = tmp_path / "state" / "sizings"
    tree.mkdir(parents=True)
    for i in range(_FLAT_ENTRIES):
        (tree / f"2026-0{1 + i % 9}-{1 + i % 28:02d}-entry-{i}.yaml").write_text("x\n", encoding="utf-8")

    root = str(tmp_path)
    flat_hit = "state/sizings/2026-01-01-entry-0.yaml"
    shard_miss = "state/sizings/2026-11-03-absent.yaml"
    spawns: list[object] = []
    real_popen = subprocess.Popen

    def counting_popen(*args, **kwargs):
        spawns.append(args)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    assert otl.locate(root, flat_hit) == flat_hit
    assert otl.locate(root, shard_miss) == f"state/sizings/{_MONTH}/2026-11-03-absent.yaml"

    for i in range(_LOCATE_CALLS):
        otl.locate(root, flat_hit if i % 2 else shard_miss)

    # process_time ticks at ~15ms on Windows: time enough calls that one tick is under the budget.
    start = time.process_time()
    for i in range(_TIMED_CALLS):
        otl.locate(root, flat_hit if i % 2 else shard_miss)
    per_call = (time.process_time() - start) / _TIMED_CALLS

    print(
        f"locate: spawns={len(spawns)} over {_LOCATE_CALLS} calls, "
        f"process_time_per_call_us={per_call * 1e6:.1f} over {_TIMED_CALLS} calls, flat_entries={_FLAT_ENTRIES}"
    )
    assert spawns == []
    assert per_call < _LOCATE_BUDGET_S, f"locate cost {per_call * 1e6:.1f}us per call"
