"""plan.seam_check / plan.seam_record: finding classes, verdict rules, named_set splits, sidecar shape.

Git is faked at the module's ``run_git`` seam; no process is spawned.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml

from coordinator_core.ops import plan_seam_check as op

HEAD = "a" * 40
RANGE = "b" * 40 + ".." + "c" * 40

_SCHEMA = json.loads(
    (Path(op.__file__).resolve().parents[1] / "frontmatter" / "schemas" / "seam-check.schema.json")
    .read_text(encoding="utf-8")
)


def _validate(doc: dict) -> None:
    """The sidecar against the vendored seam-check schema."""
    jsonschema.validate(instance=doc, schema=_SCHEMA)


class FakeGit:
    def __init__(self):
        self.tracked: set = set()
        self.landed: set = set()
        self.touched: list = []
        self.calls: list = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if "cat-file" in args:
            out = []
            for line in kw["input"].decode().split("\n"):
                if not line:
                    continue
                if line == "HEAD":
                    out.append(f"{HEAD} commit 10")
                elif line.split(":", 1)[1] in (self.tracked if line.startswith("HEAD:") else self.landed):
                    out.append(f"{'d' * 40} blob 1")
                else:
                    out.append(f"{line} missing")
            return SimpleNamespace(ok=True, stdout="\n".join(out) + "\n")
        return SimpleNamespace(ok=True, stdout="\0".join(self.touched) + "\0")


@pytest.fixture
def git(monkeypatch):
    fake = FakeGit()
    monkeypatch.setattr(op, "run_git", fake)
    return fake


def _row(rid, writes=None, consumes=None, extra=""):
    r = f"- id: {rid}\n  title: t\n  surface: s\n  change_kind: code-edit\n"
    if writes is not None:
        r += f"  writes: {writes}\n"
    if consumes is not None:
        r += f"  consumes: {consumes}\n"
    return r + extra


def _plan(root: Path, name: str, rows: str, fm: str = "capabilities: []\n") -> str:
    rel = f"docs/plans/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: t\nstatus: approved\n{fm}---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
                 encoding="utf-8")
    return rel


def _root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _check(root, plans, phase="prep", named=True, **extra):
    return op._check_handler({"plans": plans, "phase": phase, "named_set": named, **extra}, repo_root=root)


def _classes(reply, blocking=None):
    return sorted(f["class"] for f in reply["findings"] if blocking is None or f["blocking"] is blocking)


def test_clean_single_plan(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    r = _check(root, [a])
    assert r["verdict"] == "CLEAN" and r["per_plan"] == {a: "CLEAN"} and r["set"] == [a]


def test_capabilities_undeclared_split(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"), fm="")
    r = _check(root, [a], "prep", True)
    assert r["verdict"] == "REFUSED" and r["findings"][0]["blocking"] is True
    r = _check(root, [a], "prep", False)
    assert r["verdict"] == "CLEAN" and r["findings"][0]["blocking"] is False
    assert _check(root, [a], "fire", False)["verdict"] == "REFUSED"
    wb = _check(root, [a], "wave-boundary", True, landed_range=RANGE, landed_rows=[], wave=1)
    assert wb["verdict"] == "CLEAN" and wb["findings"][0]["blocking"] is False


def _cap(extra):
    return "capabilities:\n  - id: cap1\n    statement: s\n    role: admin\n    click_path: A > B\n" + extra


def test_ui_consumer_in_set_resolves_and_in_other_plan_must_be_coded(tmp_path, git):
    root = _root(tmp_path)
    ui = _plan(root, "ui", _row("C4", "[ui.tsx]"))
    a = _plan(root, "a", _row("R1", "[x.py]"),
              fm=_cap(f"    ui_consumer: {{plan: {ui}, chunk: C4}}\n"))
    assert _check(root, [a, ui])["verdict"] == "CLEAN"
    r = _check(root, [a])
    assert r["verdict"] == "REFUSED"
    f = r["findings"][0]
    assert f["class"] == "capability-without-ui-consumer" and f["capability"] == "cap1"
    assert "A > B" in f["missing_consumer"] and "admin" in f["missing_consumer"] and "C4" in f["missing_consumer"]
    _plan(root, "ui", _row("C4", "[ui.tsx]", extra="  disposition: coded\n"))
    assert _check(root, [a])["verdict"] == "CLEAN"


@pytest.mark.parametrize("row,why", [
    (_row("C4", "[ui.tsx]", extra="  disposition: wont_do\n"), "wont_do"),
    (_row("C4", "[ui.tsx]", extra="  disposition: voided\n"), "voided"),
    (_row("C4"), "declares no write"),
    (_row("C9", "[ui.tsx]"), "no row"),
])
def test_ui_consumer_unresolved(tmp_path, git, row, why):
    root = _root(tmp_path)
    ui = _plan(root, "ui", row)
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=_cap(f"    ui_consumer: {{plan: {ui}, chunk: C4}}\n"))
    r = _check(root, [a, ui])
    assert r["per_plan"] == {a: "REFUSED", ui: "CLEAN"}
    assert why in r["findings"][0]["detail"]


def test_own_plan_chunk_and_shipped_path(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("C4", "[ui.tsx]"), fm=_cap("    ui_consumer: {chunk: C4}\n"))
    assert _check(root, [a])["verdict"] == "CLEAN"
    b = _plan(root, "b", _row("R1", "[x.py]"), fm=_cap("    ui_consumer: {shipped: web/p.tsx}\n"))
    r = _check(root, [b])
    assert r["verdict"] == "REFUSED" and "not tracked at HEAD" in r["findings"][0]["detail"]
    git.tracked.add("web/p.tsx")
    assert _check(root, [b])["verdict"] == "CLEAN"


def test_empty_role_or_click_path_blocks(tmp_path, git):
    root = _root(tmp_path)
    fm = "capabilities:\n  - id: c\n    statement: s\n    role: ''\n    click_path: A\n    ui_consumer: {chunk: R1}\n"
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=fm)
    assert _check(root, [a])["verdict"] == "REFUSED"


def test_ui_carve_out_passes_and_is_recorded(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=_cap("    ui_carve_out: 'no UI, the PM said so'\n"))
    r = _check(root, [a])
    f = r["findings"][0]
    assert r["verdict"] == "CLEAN" and f["blocking"] is False
    assert f["class"] == "capability-without-ui-consumer" and "no UI, the PM said so" in f["detail"]


def test_unpromised_export_and_its_exemptions(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]", "[gone.py]"))
    r = _check(root, [a])
    assert r["verdict"] == "REFUSED"
    f = r["findings"][0]
    assert f["class"] == "unpromised-export" and f["path"] == "gone.py" and f["row"] == "R1"
    git.tracked.add("gone.py")
    assert _check(root, [a])["verdict"] == "CLEAN"
    git.tracked.clear()
    own = _plan(root, "own", _row("W", "[gone.py]") + _row("R1", "[y.py]", "[gone.py]"))
    assert _check(root, [own])["verdict"] == "CLEAN"
    writer = _plan(root, "writer", _row("W", "[gone.py]"))
    assert _check(root, [a, writer])["verdict"] == "CLEAN"
    r = _check(root, [a])
    assert r["per_plan"] == {a: "REFUSED"}


def test_depends_on_plan_promised_path_is_not_unpromised(tmp_path, git):
    root = _root(tmp_path)
    writer = _plan(root, "writer", _row("W", "[gone.py]"), fm="capabilities: []\nstatus_x: 1\n")
    pred = f"capabilities: []\ndepends_on_plan:\n  - {{plan: {writer}, status: approved, gate_kind: k}}\n"
    a = _plan(root, "a", _row("R1", "[x.py]", "[gone.py]"), fm=pred)
    r = _check(root, [a])
    assert "unpromised-export" not in _classes(r)


def test_writes_collision_named_and_default(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[s/x.py]"))
    b = _plan(root, "b", _row("R2", "[s/x.py]"))
    r = _check(root, [a, b], named=True)
    assert r["verdict"] == "REFUSED" and r["per_plan"] == {a: "REFUSED", b: "REFUSED"}
    by_plan = {f["plan"]: f for f in r["findings"]}
    assert by_plan[a]["counterpart_plan"] == b and by_plan[b]["counterpart_plan"] == a
    assert by_plan[a]["path"] == "s/x.py"
    r = _check(root, [a, b], named=False)
    assert r["verdict"] == "CLEAN" and all(f["blocking"] is False for f in r["findings"])
    assert len(r["findings"]) == 2


def test_writes_under_collision(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[s/x.py]"))
    b = _plan(root, "b", _row("R2", "[other.py]", extra="  writes_under: [s/]\n"))
    assert _check(root, [a, b])["verdict"] == "REFUSED"


def test_two_appends_to_one_path_do_not_collide(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[s/hub.ts, s/a.ts]", extra="  appends: [s/hub.ts]\n"))
    b = _plan(root, "b", _row("R2", "[s/hub.ts]", extra="  appends: [s/hub.ts]\n"))
    assert _check(root, [a, b])["verdict"] == "CLEAN"


def test_an_append_against_an_in_place_write_collides(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[s/hub.ts]", extra="  appends: [s/hub.ts]\n"))
    b = _plan(root, "b", _row("R2", "[s/hub.ts]"))
    r = _check(root, [a, b])
    assert r["verdict"] == "REFUSED" and {f["path"] for f in r["findings"]} == {"s/hub.ts"}


def test_one_in_place_row_makes_the_plan_an_in_place_writer(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[s/hub.ts]", extra="  appends: [s/hub.ts]\n") + _row("R3", "[s/hub.ts]"))
    b = _plan(root, "b", _row("R2", "[s/hub.ts]", extra="  appends: [s/hub.ts]\n"))
    assert _check(root, [a, b])["verdict"] == "REFUSED"


def test_ordered_pair_is_clean(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[s/x.py]"))
    edge = f"capabilities: []\ndepends_on_plan:\n  - {{plan: {a}, status: approved, gate_kind: k}}\n"
    b = _plan(root, "b", _row("R2", "[s/x.py]"), fm=edge)
    r = _check(root, [a, b])
    assert r["verdict"] == "CLEAN" and not any(f["class"] == "writes-collision" for f in r["findings"])


def test_undeclared_writes_recorded_as_undetermined(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1"))
    r = _check(root, [a])
    f = r["findings"][0]
    assert r["verdict"] == "CLEAN" and f["class"] == "writes-collision"
    assert f["blocking"] is False and f["detail"] == "undetermined" and f["row"] == "R1"


def test_unimplicated_set_mate_stays_clean(tmp_path, git):
    root = _root(tmp_path)
    bad = _plan(root, "bad", _row("R1", "[x.py]", "[gone.py]"))
    ok = _plan(root, "ok", _row("R2", "[y.py]"))
    r = _check(root, [bad, ok])
    assert r["verdict"] == "REFUSED" and r["per_plan"] == {bad: "REFUSED", ok: "CLEAN"}


def _wb(root, plans, landed, **kw):
    return _check(root, plans, "wave-boundary", True, landed_range=RANGE,
                  landed_rows=[{"plan": p, "row": r} for p, r in landed], wave=2, **kw)


def test_drift_promised_path_absent(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[lib.py]"))
    b = _plan(root, "b", _row("B1", "[b.py]", "[lib.py]"))
    r = _wb(root, [a, b], [(a, "A1")])
    assert r["verdict"] == "DRIFT" and r["per_plan"] == {a: "CLEAN", b: "DRIFT"}
    f = r["findings"][0]
    assert f["class"] == "drifted-contract" and f["blocking"] and f["counterpart_plan"] == a
    assert f["path"] == "lib.py" and f["row"] == "B1"
    git.landed.add("lib.py")
    assert _wb(root, [a, b], [(a, "A1")])["verdict"] == "CLEAN"


def test_drift_asks_the_landed_head_not_head(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[lib.py]"))
    b = _plan(root, "b", _row("B1", "[b.py]", "[lib.py]"))
    git.tracked.add("lib.py")
    assert _wb(root, [a, b], [(a, "A1")])["verdict"] == "DRIFT"
    git.tracked.clear()
    git.landed.add("lib.py")
    assert _wb(root, [a, b], [(a, "A1")])["verdict"] == "CLEAN"


def test_export_lens_asks_head_even_at_wave_boundary(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[x.py]", "[gone.py]"))
    git.landed.add("gone.py")
    r = _wb(root, [a], [])
    assert "unpromised-export" in _classes(r, True)
    git.tracked.add("gone.py")
    assert "unpromised-export" not in _classes(_wb(root, [a], []))


def _archive(root, rel, rows, fm="capabilities: []\n"):
    live = _plan(root, Path(rel).stem, rows, fm)
    moved = root / "archive" / "specs" / "2026-09" / Path(rel).name
    moved.parent.mkdir(parents=True, exist_ok=True)
    (root / live).rename(moved)


def test_archived_dependency_that_writes_the_consumed_path_promises_it(tmp_path, git):
    root = _root(tmp_path)
    _archive(root, "docs/plans/dep.md", _row("W", "[lib/x.py]"))
    edge = "capabilities: []\ndepends_on_plan:\n  - {plan: docs/plans/dep.md, status: approved, gate_kind: k}\n"
    a = _plan(root, "a", _row("R1", "[y.py]", "[lib/x.py]"), fm=edge)
    r = _check(root, [a])
    assert "unpromised-export" not in _classes(r) and r["verdict"] == "CLEAN"


def test_archived_dependency_that_does_not_write_it_leaves_the_export_unpromised(tmp_path, git):
    root = _root(tmp_path)
    _archive(root, "docs/plans/dep.md", _row("W", "[other.py]"))
    edge = "capabilities: []\ndepends_on_plan:\n  - {plan: docs/plans/dep.md, status: approved, gate_kind: k}\n"
    a = _plan(root, "a", _row("R1", "[y.py]", "[lib/x.py]"), fm=edge)
    r = _check(root, [a])
    assert [f["path"] for f in r["findings"] if f["class"] == "unpromised-export"] == ["lib/x.py"]


def test_drift_clause_two_still_checks_a_plan_that_has_landed_a_row(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[a.py]"))
    b = _plan(root, "b", _row("B1", "[b.py]") + _row("B2", "[hot.py]"))
    git.touched = ["hot.py"]
    r = _wb(root, [a, b], [(a, "A1"), (b, "B1")])
    hits = [f for f in r["findings"] if f["class"] == "drifted-contract"]
    assert [(f["plan"], f["row"], f["path"]) for f in hits] == [(b, "B2", "hot.py")]


def test_a_landed_prefix_is_not_a_promise_of_every_file_under_it(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[a.py]", extra="  writes_under: [src/]\n"))
    b = _plan(root, "b", _row("B2", "[b.py]", "[src/gen.py]"))
    r = _wb(root, [a, b], [(a, "A1")])
    assert not [f for f in r["findings"] if "was promised by a landed row" in f["detail"]]


def test_landed_rows_plan_is_normalised_and_escapes_are_refused(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[lib.py]"))
    b = _plan(root, "b", _row("B1", "[b.py]", "[lib.py]"))
    r = _wb(root, [a, b], [(f"./{a}", "A1")])
    assert r["per_plan"] == {a: "CLEAN", b: "DRIFT"}
    assert r["findings"][0]["counterpart_plan"] == a
    with pytest.raises(ValueError, match="escapes"):
        _wb(root, [a], [("../outside.md", "A1")])


def test_depends_on_plan_escape_is_dropped_before_any_read(tmp_path, git):
    root = _root(tmp_path)
    edge = ("capabilities: []\ndepends_on_plan:\n  - {plan: ../evil.md, status: approved, gate_kind: k}\n"
            "  - {plan: /etc/hosts, status: approved, gate_kind: k}\n"
            "  - {plan: ./docs/plans/ok.md, status: approved, gate_kind: k}\n")
    a = _plan(root, "a", _row("R1", "[y.py]"), fm=edge)
    assert op._Plan(a, root).edge_plans() == {"docs/plans/ok.md"}


def test_a_path_only_in_legacy_reads_is_a_non_blocking_export(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]", extra="  reads: [old.py]\n"))
    r = _check(root, [a])
    f = [x for x in r["findings"] if x["class"] == "unpromised-export"]
    assert r["verdict"] == "CLEAN" and len(f) == 1 and f[0]["blocking"] is False
    assert "reads:" in f[0]["detail"] and f[0]["path"] == "old.py"


def test_collision_ordering_follows_transitive_edges(tmp_path, git):
    root = _root(tmp_path)
    c = _plan(root, "c", _row("R3", "[s/x.py]"))
    b = _plan(root, "b", _row("R2", "[b.py]"),
              fm=f"capabilities: []\ndepends_on_plan:\n  - {{plan: {c}, status: approved, gate_kind: k}}\n")
    a = _plan(root, "a", _row("R1", "[s/x.py]"),
              fm=f"capabilities: []\ndepends_on_plan:\n  - {{plan: {b}, status: approved, gate_kind: k}}\n")
    r = _check(root, [a, b, c])
    assert not any(f["class"] == "writes-collision" for f in r["findings"])


def test_record_refuses_plans_outside_docs_plans_but_check_accepts_them(tmp_path, git):
    root = _root(tmp_path)
    inv = root / "state" / "mise-inventory"
    inv.mkdir(parents=True)
    _plan(root, "x", _row("R1", "[x.py]"))
    rel = "state/mise-inventory/x.spine.md"
    (inv / "x.spine.md").write_text((root / "docs/plans/x.md").read_text(), encoding="utf-8")
    assert _check(root, [rel])["verdict"] == "CLEAN"
    with pytest.raises(ValueError, match="docs/plans"):
        op._record_handler({"plans": [rel], "phase": "prep", "named_set": True}, repo_root=root)
    assert not list(inv.glob("*.seam.yaml"))


def test_drift_undeclared_touch_on_remaining_write_and_reads_at_head(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[a.py]"))
    b = _plan(root, "b", _row("B1", "[hot.py]"))
    git.touched = ["hot.py", "a.py"]
    r = _wb(root, [a, b], [(a, "A1")])
    assert r["verdict"] == "DRIFT" and r["per_plan"] == {a: "CLEAN", b: "DRIFT"}
    assert [f["path"] for f in r["findings"] if f["class"] == "drifted-contract"] == ["hot.py"]
    c = _plan(root, "c", _row("C1", "[c.py]", extra="  reads_at_head: [hot.py]\n"))
    r = _wb(root, [a, c], [(a, "A1")])
    f = [x for x in r["findings"] if x["class"] == "drifted-contract"]
    assert r["verdict"] == "CLEAN" and len(f) == 1 and f[0]["blocking"] is False
    assert any("--no-renames" in c for c in git.calls)


def test_wave_params_required_and_refused(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    with pytest.raises(ValueError):
        _check(root, [a], "wave-boundary", True)
    with pytest.raises(ValueError):
        _check(root, [a], "prep", True, wave=1)
    with pytest.raises(ValueError):
        op._check_handler({"plans": [a], "phase": "prep"}, repo_root=root)


def test_git_batched_one_call_for_tracked_questions(tmp_path, git):
    root = _root(tmp_path)
    plans = [_plan(root, f"p{i}", _row("R", f"[w{i}.py]", f"[gone{i}.py]")) for i in range(6)]
    _check(root, plans)
    assert len(git.calls) == 1 and "cat-file" in git.calls[0]


def test_record_writes_one_valid_sidecar_per_plan_and_check_writes_none(tmp_path, git):
    root = _root(tmp_path)
    bad = _plan(root, "bad", _row("R1", "[x.py]", "[gone.py]"))
    ok = _plan(root, "ok", _row("R2", "[y.py]"))
    params = {"plans": [bad, ok], "phase": "prep", "named_set": True}
    _check(root, [bad, ok])
    assert not list((root / "docs/plans").glob("*.seam.yaml"))
    r = op._record_handler(params, repo_root=root)
    assert sorted(r["sidecars"]) == ["docs/plans/bad.seam.yaml", "docs/plans/ok.seam.yaml"]
    docs = {p: yaml.safe_load((root / p).read_text()) for p in r["sidecars"]}
    for d in docs.values():
        _validate(d)
    assert docs["docs/plans/bad.seam.yaml"]["verdict"] == "REFUSED"
    assert docs["docs/plans/bad.seam.yaml"]["findings"][0]["class"] == "unpromised-export"
    assert docs["docs/plans/ok.seam.yaml"]["verdict"] == "CLEAN" and docs["docs/plans/ok.seam.yaml"]["findings"] == []
    assert docs["docs/plans/ok.seam.yaml"]["wave"] is None
    assert docs["docs/plans/ok.seam.yaml"]["checked_at_sha"] == HEAD
    assert not any(p.name.startswith(".atomic-write") for p in (root / "docs/plans").iterdir())


def test_record_wave_boundary_sidecar_validates(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[lib.py]"))
    b = _plan(root, "b", _row("B1", "[b.py]", "[lib.py]"))
    r = op._record_handler({"plans": [a, b], "phase": "wave-boundary", "named_set": True, "landed_range": RANGE,
                            "landed_rows": [{"plan": a, "row": "A1"}], "wave": 3}, repo_root=root)
    d = yaml.safe_load((root / "docs/plans/b.seam.yaml").read_text())
    _validate(d)
    assert d["verdict"] == "DRIFT" and d["wave"] == 3 and r["verdict"] == "DRIFT"


def test_seam_sidecar_travels_with_its_plan_on_archive(tmp_path):
    from coordinator_core.ops.fleet.archive_plans import collect_live_plan_paths

    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    for name in ("2026-10-08-x.md", "2026-10-08-x.seam.yaml"):
        (plans / name).write_text("x\n", encoding="utf-8")
    names = {p.name for p in collect_live_plan_paths(tmp_path)}
    assert "2026-10-08-x.seam.yaml" in names


def test_default_set_collisions_are_one_record_per_plan_and_path(tmp_path, git):
    root = _root(tmp_path)
    plans = [_plan(root, n, _row("R1", "[s/x.py]")) for n in ("a", "b", "c")]
    named = _check(root, plans, named=True)
    assert len([f for f in named["findings"] if f["class"] == "writes-collision"]) == 6
    r = _check(root, plans, named=False)
    records = [f for f in r["findings"] if f["class"] == "writes-collision"]
    assert r["verdict"] == "CLEAN"
    assert sorted(f["plan"] for f in records) == sorted(plans)
    for f in records:
        assert f["blocking"] is False and f["path"] == "s/x.py"
        assert f["counterpart_plan"] in plans and f["counterpart_plan"] != f["plan"]
        assert f["detail"].startswith("3 plans declare writes to s/x.py")


def test_unreadable_spine_implicates_only_its_plan_in_a_named_set(tmp_path, git):
    root = _root(tmp_path)
    bad = _plan(root, "bad", _row("R1", "[x.py]") + _row("R1", "[y.py]"))
    good = _plan(root, "good", _row("R1", "[z.py]"))
    r = _check(root, [bad, good], named=True)
    assert r["per_plan"] == {bad: "REFUSED", good: "CLEAN"}
    f = next(f for f in r["findings"] if f["plan"] == bad)
    assert f["class"] == "writes-collision" and f["detail"].startswith("undetermined: spine unreadable")
    r = _check(root, [bad, good], named=False)
    assert r["verdict"] == "CLEAN"
    assert any(f["plan"] == bad and f["blocking"] is False for f in r["findings"])


def test_a_sibling_repo_consume_is_not_an_unpromised_export(tmp_path, git, monkeypatch):
    from coordinator_core.roadmap import prep_gate

    monkeypatch.setattr(prep_gate, "fleet_siblings", lambda _root: ("coordinator-content-repo",))
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]", "[coordinator-content-repo/coordinator/hooks/hooks.json, gone.py]"))
    r = _check(root, [a])
    assert [f["path"] for f in r["findings"] if f["class"] == "unpromised-export"] == ["gone.py"]


@pytest.mark.parametrize("which", ["cat-file", "diff"])
def test_a_git_timeout_is_named_as_an_engine_failure(tmp_path, monkeypatch, which):
    fake = FakeGit()

    def timing_out(args, **kw):
        if which in args:
            return SimpleNamespace(ok=False, stdout="", timed_out=True)
        return fake(args, **kw)

    monkeypatch.setattr(op, "run_git", timing_out)
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    with pytest.raises(ValueError, match=rf"engine failure: git {which}.* timed out"):
        _check(root, [a], "wave-boundary", landed_range=RANGE, landed_rows=[], wave=1)


def test_git_reads_carry_no_seam_specific_timeout(git, tmp_path, monkeypatch):
    seen = []

    def recording(args, **kw):
        seen.append(kw.get("timeout"))
        return git(args, **kw)

    monkeypatch.setattr(op, "run_git", recording)
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    _check(root, [a], "wave-boundary", landed_range=RANGE, landed_rows=[], wave=1)
    assert seen and all(t is None for t in seen)


# --- universe: missing-seam findings against open plans outside the set -------------------------


def _seams(reply):
    return [f for f in reply["findings"] if f["class"] == "missing-seam"]


def _status(root, rel, status):
    p = root / rel
    p.write_text(p.read_text(encoding="utf-8").replace("status: approved", f"status: {status}"), encoding="utf-8")


def test_universe_off_is_unchanged(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    _plan(root, "b", _row("R1", "[x.py]"))
    base = _check(root, [a])
    assert _check(root, [a], universe=False) == base == _check(root, [a], universe=None)
    assert _seams(base) == []


def test_universe_must_be_a_bool(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    with pytest.raises(ValueError, match="universe must be a bool"):
        _check(root, [a], universe="yes")


def test_universe_write_overlap_is_non_blocking_and_leaves_the_verdict(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    b = _plan(root, "b", _row("R1", "[x.py]"))
    off = _check(root, [a])
    r = _check(root, [a], universe=True)
    assert r["verdict"] == off["verdict"] == "CLEAN" and r["per_plan"] == off["per_plan"]
    [f] = _seams(r)
    assert f["plan"] == a and f["blocking"] is False and f["counterpart_plan"] == b and f["path"] == "x.py"
    assert [x for x in r["findings"] if x["class"] != "missing-seam"] == off["findings"]


def test_universe_overlap_ignores_edged_terminal_and_in_set_plans(tmp_path, git):
    root = _root(tmp_path)
    b = _plan(root, "b", _row("R1", "[x.py]"))
    edged = _plan(root, "edged", _row("R1", "[x.py]"), fm=f"capabilities: []\ndepends_on_plan:\n  - plan: {b}\n")
    assert _seams(_check(root, [edged], universe=True)) == []
    a = _plan(root, "a", _row("R1", "[x.py]"))
    assert {f["counterpart_plan"] for f in _seams(_check(root, [a], universe=True))} == {b, edged}
    _status(root, b, "implemented")
    _status(root, edged, "abandoned")
    assert _seams(_check(root, [a], universe=True)) == []
    _status(root, b, "approved")
    assert _seams(_check(root, [a, b], universe=True)) == []


def test_universe_overlap_honours_appends(tmp_path, git):
    root = _root(tmp_path)
    app = "  appends: [log.md]\n"
    a = _plan(root, "a", _row("R1", "[log.md]", extra=app))
    b = _plan(root, "b", _row("R1", "[log.md]", extra=app))
    assert _seams(_check(root, [a], universe=True)) == []
    _plan(root, "b", _row("R1", "[log.md]"))
    assert len(_seams(_check(root, [a], universe=True))) == 1


def test_universe_consumed_path_with_only_an_uncoded_outside_writer(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]", consumes="[lib/y.py]"))
    b = _plan(root, "b", _row("W1", "[lib/y.py]"))
    r = _check(root, [a], universe=True)
    [f] = _seams(r)
    assert f["plan"] == a and f["blocking"] is False and f["counterpart_plan"] == b
    assert f["path"] == "lib/y.py" and f["row"] == "R1"
    _plan(root, "b", _row("W1", "[lib/y.py]", extra="  disposition: coded\n"))
    assert _seams(_check(root, [a], universe=True)) == []


def test_universe_consumed_path_nobody_ships(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]", consumes="[lib/z.py]"))
    r = _check(root, [a], universe=True)
    [f] = _seams(r)
    assert f["blocking"] is False and f["path"] == "lib/z.py" and f["counterpart_plan"] is None
    assert r["verdict"] == _check(root, [a])["verdict"]
    git.tracked.add("lib/z.py")
    assert _seams(_check(root, [a], universe=True)) == []


def test_universe_caps_findings_and_names_the_overflow(tmp_path, git):
    root = _root(tmp_path)
    paths = ", ".join(f"f{i:03d}.py" for i in range(60))
    a = _plan(root, "a", _row("R1", f"[{paths}]"))
    _plan(root, "b", _row("R1", f"[{paths}]"))
    seams = _seams(_check(root, [a], universe=True))
    assert len(seams) == op.MISSING_SEAM_CAP + 1
    [summary] = [f for f in seams if "further" in f["detail"]]
    assert "10 further missing-seam findings omitted" in summary["detail"] and summary["blocking"] is False


def test_universe_findings_stay_out_of_the_sidecar(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    _plan(root, "b", _row("R1", "[x.py]"))
    r = op._record_handler({"plans": [a], "phase": "prep", "named_set": True, "universe": True}, repo_root=root)
    assert len(_seams(r)) == 1
    doc = yaml.safe_load((root / "docs/plans/a.seam.yaml").read_text(encoding="utf-8"))
    assert doc["findings"] == []
    _validate(doc)
