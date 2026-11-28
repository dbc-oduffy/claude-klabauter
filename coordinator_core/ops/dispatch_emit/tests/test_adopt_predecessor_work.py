"""dispatch.adopt_predecessor_work: credits a halted predecessor's kept prints into a fresh run, explicitly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import adopt_predecessor_work as apw
from coordinator_core.ops.dispatch_emit import sizing_fire
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha, fingerprint
from coordinator_core.ops.dispatch_emit.write_baseline import common_dir_for, persist_raw

pytestmark = pytest.mark.complete

SIZING = "state/sizings/2026-11-18-adopt-fixture.yaml"
FRESH = "warp-2026-11-18-adopt-fixture-20261118T020202-def456"
PRED = "warp-2026-11-18-adopt-fixture-20261118T010101-abc123"
KEPT, OTHER = "a/kept.py", "a/other.py"


def _repo(root: Path) -> None:
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def _file(root: Path, rel: str, text: str) -> str:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(text.encode("utf-8"))
    return fingerprint(root, rel)


def _persist(root: Path, text: str) -> str:
    persist_raw(common_dir_for(root), text.encode("utf-8"))
    return blob_sha(text.encode("utf-8"))


def _fresh(root: Path, sizing: str = SIZING, baseline: dict | None = None) -> Path:
    home = root / RECORD_DIR_ROOT / FRESH
    home.mkdir(parents=True, exist_ok=True)
    (home / "run.json").write_text(json.dumps({"sizing_rel": sizing}) + "\n", encoding="utf-8")
    base = {KEPT: "f" * 40, OTHER: "e" * 40} if baseline is None else baseline
    (home / "manifest.json").write_text(json.dumps({"rows": [], "write_baseline": base}), encoding="utf-8")
    return home


def _pred(root: Path, kept: dict, *, status: str | None = "halted", sizing: str = SIZING, stage: str = "halted") -> Path:
    home = root / RECORD_DIR_ROOT / PRED
    home.mkdir(parents=True, exist_ok=True)
    (home / "run.json").write_text(json.dumps({"sizing_rel": sizing}) + "\n", encoding="utf-8")
    (home / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")
    if status:
        (home / "halted.json").write_text(json.dumps({"status": status, "sizing_rel": sizing}), encoding="utf-8")
    (home / "kept.json").write_text(
        json.dumps({"chunks": {"C1": {"paths": kept}}, "plan_gate": None, "post_checkpoint": {}}), encoding="utf-8"
    )
    return home


def _baseline(home: Path) -> dict:
    return json.loads((home / "manifest.json").read_text(encoding="utf-8"))["write_baseline"]


def _run_json(home: Path) -> dict:
    return json.loads((home / "run.json").read_text(encoding="utf-8"))


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    _repo(tmp_path)

    def no_spawn(*args, **kwargs):
        raise AssertionError("adoption spawned a git process")

    monkeypatch.setattr("coordinator_core.git.run.run_git", no_spawn)
    return tmp_path


def test_a_kept_path_carrying_its_print_is_credited(root):
    fp = _file(root, KEPT, "x = 1\n")
    fresh, _ = _fresh(root), _pred(root, {KEPT: fp})
    out = apw.adopt_predecessor_work(root, FRESH, PRED)
    assert out["ok"] and out["applied"] is True and out["adopted"] == [KEPT] and out["refused"] == {}
    assert _baseline(fresh)[KEPT] is None and _baseline(fresh)[OTHER] == "e" * 40
    assert _run_json(fresh)["adopted_from"] == [PRED]
    assert _run_json(fresh)["sizing_rel"] == SIZING


def test_a_peer_hunk_beside_the_print_keeps_the_credit(root):
    print_ = _persist(root, "x = 1\n")
    _file(root, KEPT, "x = 1\ny = 2\n")
    fresh, _ = _fresh(root), _pred(root, {KEPT: print_})
    assert apw.adopt_predecessor_work(root, FRESH, PRED)["adopted"] == [KEPT]


def test_a_foreign_rewrite_is_refused_by_name_and_the_rest_credited(root):
    print_ = _persist(root, "x = 1\n")
    _file(root, KEPT, "totally = 'other'\n")
    other = _file(root, OTHER, "ok = 1\n")
    fresh, _ = _fresh(root), _pred(root, {KEPT: print_, OTHER: other})
    out = apw.adopt_predecessor_work(root, FRESH, PRED)
    assert out["adopted"] == [OTHER] and list(out["refused"]) == [KEPT]
    assert _baseline(fresh)[KEPT] == "f" * 40 and _baseline(fresh)[OTHER] is None


def test_an_absent_kept_path_is_refused_by_name(root):
    fresh, _ = _fresh(root), _pred(root, {KEPT: "a" * 40})
    out = apw.adopt_predecessor_work(root, FRESH, PRED)
    assert out["applied"] is False and list(out["refused"]) == [KEPT]
    assert "adopted_from" not in _run_json(fresh) and _baseline(fresh)[KEPT] == "f" * 40


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"sizing": "state/sizings/other.yaml"}, "not " + SIZING),
        ({"status": None, "stage": "execute"}, "still running"),
        ({"status": None, "stage": "landed"}, "landed"),
        ({"status": "resumed"}, "only a halted or superseded"),
        ({"status": "landed"}, "only a halted or superseded"),
    ],
)
def test_an_unadoptable_predecessor_is_refused_and_nothing_written(root, kwargs, fragment):
    fp = _file(root, KEPT, "x = 1\n")
    fresh, _ = _fresh(root), _pred(root, {KEPT: fp}, **kwargs)
    before = (fresh / "manifest.json").read_bytes(), (fresh / "run.json").read_bytes()
    out = apw.adopt_predecessor_work(root, FRESH, PRED)
    assert out["ok"] is False and fragment in out["error"]
    assert ((fresh / "manifest.json").read_bytes(), (fresh / "run.json").read_bytes()) == before


def test_a_superseded_predecessor_is_adoptable(root):
    fp = _file(root, KEPT, "x = 1\n")
    _fresh(root), _pred(root, {KEPT: fp}, status="superseded")
    assert apw.adopt_predecessor_work(root, FRESH, PRED)["adopted"] == [KEPT]


def test_a_second_adoption_replies_applied_false_and_writes_nothing(root):
    fp = _file(root, KEPT, "x = 1\n")
    fresh, _ = _fresh(root), _pred(root, {KEPT: fp})
    assert apw.adopt_predecessor_work(root, FRESH, PRED)["applied"] is True
    after = (fresh / "manifest.json").read_bytes(), (fresh / "run.json").read_bytes()
    again = apw.adopt_predecessor_work(root, FRESH, PRED)
    assert again["ok"] is True and again["applied"] is False and again["adopted"] == []
    assert ((fresh / "manifest.json").read_bytes(), (fresh / "run.json").read_bytes()) == after


def test_a_run_never_adopts_itself(root):
    _fresh(root)
    assert apw.adopt_predecessor_work(root, FRESH, FRESH)["ok"] is False


def test_adoption_is_explicit_a_fresh_mint_credits_nothing_until_called(root):
    fp = _file(root, KEPT, "x = 1\n")
    fresh, _ = _fresh(root), _pred(root, {KEPT: fp})
    assert sizing_fire._kept_rows(root, SIZING, ["C1"]) == ((), None)
    assert _baseline(fresh)[KEPT] == "f" * 40


def test_the_registered_handler_replies_like_the_function(root):
    fp = _file(root, KEPT, "x = 1\n")
    _fresh(root), _pred(root, {KEPT: fp})
    assert apw._handler({"predecessor": PRED}, repo_root=root) == {"error": f"{apw.OP_NAME} requires run_id"}
    assert apw._handler({"run_id": FRESH}, repo_root=root) == {"error": f"{apw.OP_NAME} requires predecessor"}
    assert apw._handler({"run_id": FRESH, "predecessor": PRED}, repo_root=root)["adopted"] == [KEPT]
