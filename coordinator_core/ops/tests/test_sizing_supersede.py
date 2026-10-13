"""sizing.supersede: routed -> superseded with a superseded_note, idempotent, routed-only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.sizing_decline as decline_mod
import coordinator_core.ops.sizing_supersede as sup_mod
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.tests.test_sizing_ship import _init_repo, _sizing_body
from coordinator_core.session import record_homes

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NAME = "20261013-supersede.yaml"
_NOTE = "refused run; live surface moved to the C1 plan"
_COMMENT = "  # draft | sized | routed | shipped | declined | superseded"
# Spawns for one applied call: measured on first run, equal to sizing.decline's.
_APPLIED_SPAWN_COUNT = 0


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _init_repo(r)
    return r


def _path(repo: Path, name: str = _NAME) -> Path:
    return Path(record_homes.record_path(str(repo), "sizings", name))


def _rel(repo: Path, name: str = _NAME) -> str:
    return os.path.relpath(_path(repo, name), repo)


def _seed(repo: Path, body: str | None = None, *, status: str = "routed") -> Path:
    p = _path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_sizing_body(status=status) if body is None else body, encoding="utf-8")
    return p


def _run(repo: Path, note: str = _NOTE, **over) -> dict:
    params = {"sizing_path": _rel(repo), "note": note}
    params.update(over)
    params = {k: v for k, v in params.items() if v is not None}
    return sup_mod._handler(params, repo_root=repo / ".git")


def test_routed_becomes_superseded(repo):
    p = _seed(repo)
    r = _run(repo)
    assert r["exit_code"] == 0 and r["applied"] is True, r
    text = p.read_text(encoding="utf-8")
    assert read_fm_field_unquoted(text, "status") == "superseded"
    assert yaml.safe_load(text)["superseded_note"] == _NOTE
    assert sup_mod._validate_sizing_fm(text) == []


def test_trailing_enum_comment_on_status_line(repo):
    p = _seed(repo, _sizing_body().replace("status: routed", f"status: routed{_COMMENT}"))
    r = _run(repo)
    assert r["exit_code"] == 0 and r["applied"] is True, r
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["status"] == "superseded"
    assert data["superseded_note"] == _NOTE
    assert sup_mod._validate_sizing_fm(p.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("second_note", [_NOTE, "a different note"])
def test_second_call_is_a_byte_identical_noop(repo, second_note):
    p = _seed(repo)
    _run(repo)
    before = p.read_bytes()
    r = _run(repo, note=second_note)
    assert r["exit_code"] == 0 and r["applied"] is False
    assert "already superseded" in r["message"]
    assert p.read_bytes() == before
    assert yaml.safe_load(before)["superseded_note"] == _NOTE


def test_superseded_without_note_is_not_backfilled(repo):
    p = _seed(repo, status="superseded")
    before = p.read_bytes()
    r = _run(repo)
    assert r["exit_code"] == 0 and r["applied"] is False
    assert p.read_bytes() == before


def test_applied_call_changes_exactly_two_keys(repo):
    p = _seed(repo)
    before = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert _run(repo)["applied"] is True
    after = yaml.safe_load(p.read_text(encoding="utf-8"))
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    assert changed == {"status", "superseded_note"}


@pytest.mark.parametrize("status", ["draft", "sized", "shipped", "declined"])
def test_refuses_non_routed_status(repo, status):
    p = _seed(repo, status=status)
    before = p.read_bytes()
    r = _run(repo)
    assert r["exit_code"] == 1 and status in r["error"]
    assert p.read_bytes() == before


def test_refuses_missing_status(repo):
    body = "".join(
        ln for ln in _sizing_body().splitlines(keepends=True) if not ln.startswith("status:")
    )
    p = _seed(repo, body)
    r = _run(repo)
    assert r["exit_code"] == 1
    assert p.read_text(encoding="utf-8") == body


def test_schema_invalid_result_is_refused(repo):
    body = "".join(
        ln for ln in _sizing_body().splitlines(keepends=True) if not ln.startswith("intent:")
    )
    p = _seed(repo, body)
    before = p.read_bytes()
    r = _run(repo)
    assert r["exit_code"] == 1 and r.get("applied") is not True
    assert p.read_bytes() == before


def test_lock_timeout_is_refused(repo, monkeypatch):
    p = _seed(repo)
    before = p.read_bytes()

    def boom(*a, **kw):
        raise LockTimeout("held")

    monkeypatch.setattr(sup_mod, "locked_rmw", boom)
    r = _run(repo)
    assert r["exit_code"] == 1 and r.get("applied", False) is False
    assert p.read_bytes() == before


@pytest.mark.parametrize(
    "over",
    [
        {"sizing_path": ""},
        {"note": ""},
        {"note": "   \n"},
    ],
)
def test_bad_params_write_nothing(repo, over):
    p = _seed(repo)
    before = p.read_bytes()
    r = _run(repo, **over)
    assert r["exit_code"] == 1
    assert p.read_bytes() == before


def test_missing_params_keys_write_nothing(repo):
    p = _seed(repo)
    before = p.read_bytes()
    assert sup_mod._handler({"note": _NOTE}, repo_root=repo / ".git")["exit_code"] == 1
    assert sup_mod._handler({"sizing_path": _rel(repo)}, repo_root=repo / ".git")["exit_code"] == 1
    assert p.read_bytes() == before


def test_missing_repo_root(repo):
    p = _seed(repo)
    before = p.read_bytes()
    r = sup_mod._handler({"sizing_path": _rel(repo), "note": _NOTE})
    assert r["exit_code"] == 1
    assert p.read_bytes() == before


def test_path_outside_sizings_home_refused(repo):
    outside = repo / "README.md"
    before = outside.read_bytes()
    r = _run(repo, sizing_path="README.md")
    assert r["exit_code"] == 1
    assert outside.read_bytes() == before


def test_nonexistent_path_refused(repo):
    r = _run(repo, sizing_path=_rel(repo, "20261013-absent.yaml"))
    assert r["exit_code"] == 1
    assert not _path(repo, "20261013-absent.yaml").exists()


def test_absolute_path_accepted(repo):
    p = _seed(repo)
    r = _run(repo, sizing_path=str(p))
    assert r["exit_code"] == 0 and r["applied"] is True, r


def test_awkward_note_round_trips(repo):
    note = 'moved: to "plan B" — naïve café ✓'
    p = _seed(repo)
    assert _run(repo, note=note)["applied"] is True
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["superseded_note"] == note


def test_registered_in_all_quad_surfaces():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.op_scopes import _OP_KEY_SCOPE
    from coordinator_core.ops import _EAGER_OP_MODULES
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_CLASSIFICATION["sizing.supersede"] is OpClass.MUTATING
    assert _OP_KEY_SCOPE.get("sizing.supersede") == "common_dir"
    assert OP_MODULE_MAP.get("sizing.supersede") == "coordinator_core.ops.sizing_supersede"
    assert "coordinator_core.ops.sizing_supersede" in {m for m, _ in _EAGER_OP_MODULES}


def test_applied_call_spawn_count_matches_decline(tmp_path, monkeypatch):
    dr_rel = "docs/decisions/DR-999-test-decline.md"

    def fixture(name: str) -> Path:
        r = tmp_path / name
        _init_repo(r)
        _seed(r)
        return r

    decline_repo = fixture("decline")
    dr = decline_repo / dr_rel
    dr.parent.mkdir(parents=True, exist_ok=True)
    dr.write_text("---\ntitle: test\nstatus: accepted\nid: DR-999\n---\n\n# test\n", encoding="utf-8")
    sup_repo = fixture("supersede")

    with _count_spawns_attributed(monkeypatch) as decline_spawns:
        d = decline_mod._handler(
            {"sizing_path": _rel(decline_repo), "decision_record": dr_rel},
            repo_root=decline_repo / ".git",
        )
    assert d["exit_code"] == 0 and d["applied"] is True, d

    with _count_spawns_attributed(monkeypatch) as supersede_spawns:
        s = _run(sup_repo)
    assert s["exit_code"] == 0 and s["applied"] is True, s

    assert len(supersede_spawns) == len(decline_spawns), (
        [x.argv for x in supersede_spawns],
        [x.argv for x in decline_spawns],
    )
    assert len(supersede_spawns) == _APPLIED_SPAWN_COUNT, [x.argv for x in supersede_spawns]
