"""coordinator-doc-new writes plan, sizing, handoff and plan-sidecar records at their month shard.

Pins both sides: a new record lands at `<tree>/<YYYY-MM>/<file>` (default path and a flat `--out`),
while an existing flat record stays put and the in-file readers see sharded and flat entries alike.

In-process by construction: `main()` is driven through `sys.argv`, so this adds no spawn.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_shard_path.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"
_TODAY = "2026-11-03"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_shard_under_test", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _touch(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rel(path: str) -> str:
    return path.replace("\\", "/")


# --- default path ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_type, kwargs, expected",
    [
        ("handoff", {}, f"state/handoffs/2026-11/{_TODAY}-a-title.md"),
        ("plan", {}, f"docs/plans/2026-11/{_TODAY}-a-title.md"),
        ("sizing-object", {}, f"state/sizings/2026-11/{_TODAY}-a-title.yaml"),
        ("review", {"plan_stem": "2026-10-30-the-plan"}, "docs/plans/2026-10/2026-10-30-the-plan.review.md"),
    ],
)
def test_default_path_is_the_month_shard(cli, monkeypatch, doc_type, kwargs, expected):
    monkeypatch.setattr(cli, "_today", lambda: _TODAY)
    assert _rel(cli._default_output_path(doc_type, "A title", None, **kwargs)) == expected


def test_undated_sidecar_stem_stays_flat(cli, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: _TODAY)
    assert _rel(cli._default_output_path("review", "t", None, plan_stem="PLAN-STEM")) == "docs/plans/PLAN-STEM.review.md"


def test_roadmap_baton_default_is_the_month_shard(cli, monkeypatch):
    monkeypatch.setattr(cli, "_today", lambda: _TODAY)
    out = cli._default_output_path("roadmap-baton", "t", None, stub_id="s1")
    assert _rel(out) == f"state/handoffs/2026-11/{_TODAY}_000000_roadmap-s1.md"


# --- _place_in_output_tree ---------------------------------------------------------------------


def test_flat_out_without_a_flat_file_is_rewritten_to_the_shard(cli, tmp_path):
    flat = tmp_path / "docs" / "plans" / f"{_TODAY}-x.md"
    got = cli._place_in_output_tree(str(flat), explicit=True)
    assert _rel(got) == _rel(str(tmp_path / "docs" / "plans" / "2026-11" / f"{_TODAY}-x.md"))


def test_existing_flat_file_stays_put(cli, tmp_path):
    flat = _touch(tmp_path / "state" / "sizings" / f"{_TODAY}-x.yaml")
    assert cli._place_in_output_tree(str(flat), explicit=True) == str(flat)


def test_explicit_shard_out_is_left_alone_even_if_a_flat_twin_exists(cli, tmp_path):
    _touch(tmp_path / "docs" / "plans" / f"{_TODAY}-x.md")
    shard = tmp_path / "docs" / "plans" / "2026-11" / f"{_TODAY}-x.md"
    assert cli._place_in_output_tree(str(shard), explicit=True) == str(shard)


def test_default_shard_falls_back_to_an_existing_flat_twin(cli, tmp_path):
    flat = _touch(tmp_path / "docs" / "plans" / f"{_TODAY}-x.md")
    shard = tmp_path / "docs" / "plans" / "2026-11" / f"{_TODAY}-x.md"
    assert os.path.normpath(cli._place_in_output_tree(str(shard), explicit=False)) == os.path.normpath(str(flat))


def test_default_shard_without_a_flat_twin_stays_sharded(cli, tmp_path):
    shard = tmp_path / "docs" / "plans" / "2026-11" / f"{_TODAY}-x.md"
    assert cli._place_in_output_tree(str(shard), explicit=False) == str(shard)


def test_relative_out_is_rewritten(cli, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    got = cli._place_in_output_tree(os.path.join("docs", "plans", f"{_TODAY}-x.md"), explicit=True)
    assert _rel(got) == f"docs/plans/2026-11/{_TODAY}-x.md"


def test_non_tree_out_and_undated_name_are_untouched(cli, tmp_path):
    other = str(tmp_path / "scratch" / f"{_TODAY}-x.md")
    assert cli._place_in_output_tree(other, explicit=True) == other
    undated = str(tmp_path / "docs" / "plans" / "notes.md")
    assert _rel(cli._place_in_output_tree(undated, explicit=True)) == _rel(undated)


# --- main(): the production entry point --------------------------------------------------------


def _main(cli, monkeypatch, cwd: Path, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["coordinator-doc-new", *args])
    monkeypatch.chdir(cwd)
    try:
        return int(cli.main() or 0)
    except SystemExit as exc:
        return int(exc.code or 0)


def test_main_handoff_flat_out_lands_in_the_shard_and_prints_it(cli, monkeypatch, tmp_path, capsys):
    flat = tmp_path / "state" / "handoffs" / f"{_TODAY}-baton.md"
    code = _main(cli, monkeypatch, tmp_path, "--type", "handoff", "--title", "A shard baton", "--new-chain", "--out", str(flat))
    assert code == 0
    shard = tmp_path / "state" / "handoffs" / "2026-11" / f"{_TODAY}-baton.md"
    assert shard.is_file()
    assert not flat.exists()
    assert _rel(str(shard)) in _rel(capsys.readouterr().out)


def test_main_handoff_with_an_existing_flat_file_overwrites_it_in_place(cli, monkeypatch, tmp_path):
    flat = _touch(tmp_path / "state" / "handoffs" / f"{_TODAY}-baton.md", "old\n")
    code = _main(
        cli, monkeypatch, tmp_path, "--type", "handoff", "--title", "A shard baton", "--new-chain",
        "--force", "--out", str(flat),
    )
    assert code == 0
    assert flat.read_text(encoding="utf-8") != "old\n"
    assert not (tmp_path / "state" / "handoffs" / "2026-11").exists()


# --- readers in this file see both shapes ------------------------------------------------------


def _sizing_with_id(path: Path, slug: str) -> None:
    _touch(path, f'deliverable_id: "dlv-{slug}-abcdef"\nstatus: sized\n')


def test_same_title_sizing_reader_finds_a_sharded_and_a_flat_entry(cli, monkeypatch, tmp_path):
    import datetime

    monkeypatch.setattr(cli, "_NEW_CHAIN_REQUESTED", False)

    today = datetime.date.today().isoformat()
    _sizing_with_id(tmp_path / "state" / "sizings" / today[:7] / f"{today}-shard-title.yaml", "shard-title")
    got = cli._resolve_same_title_sizing_deliverable_id("shard-title", str(tmp_path))
    assert got == "dlv-shard-title-abcdef"

    _sizing_with_id(tmp_path / "state" / "sizings" / f"{today}-flat-title.yaml", "flat-title")
    assert cli._resolve_same_title_sizing_deliverable_id("flat-title", str(tmp_path)) == "dlv-flat-title-abcdef"


def test_same_title_baton_reader_finds_a_sharded_baton(cli, monkeypatch, tmp_path):
    import datetime

    monkeypatch.setattr(cli, "_NEW_CHAIN_REQUESTED", False)

    today = datetime.date.today().isoformat()
    _touch(
        tmp_path / "state" / "handoffs" / today[:7] / f"{today}-shard-baton.md",
        '---\ndeliverable_id: "dlv-shard-baton-abcdef"\n---\n',
    )
    assert cli._resolve_same_title_baton_deliverable_id("shard-baton", str(tmp_path)) == "dlv-shard-baton-abcdef"


def test_archived_twin_guard_looks_through_the_month_dir(cli, tmp_path, monkeypatch):
    seen: list[str] = []
    import coordinator_core.handoff_creation_guard as guard

    monkeypatch.setattr(guard, "assert_no_archived_twin", lambda out, root, handoff_id=None: seen.append(str(out)))
    cli._ensure_engine_on_path()
    shard = tmp_path / "state" / "handoffs" / "2026-11" / f"{_TODAY}-x.md"
    cli._assert_no_archived_handoff_twin(str(shard), None, str(tmp_path))
    other = tmp_path / "state" / "sizings" / "2026-11" / f"{_TODAY}-x.md"
    cli._assert_no_archived_handoff_twin(str(other), None, str(tmp_path))
    assert seen == [str(shard)]
