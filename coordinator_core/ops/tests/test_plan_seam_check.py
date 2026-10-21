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


def test_ui_consumer_in_the_certified_set_counts_as_in_set_at_prep_and_wave_boundary(tmp_path, git):
    root = _root(tmp_path)
    ui = _plan(root, "ui", _row("C4", "[ui.tsx]"))
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=_cap(f"    ui_consumer: {{plan: {ui}, chunk: C4}}\n"))
    assert _check(root, [a])["verdict"] == "REFUSED"
    assert _check(root, [a], certified_plans=[ui, a])["verdict"] == "CLEAN"
    wb = _check(root, [a], "wave-boundary", True, landed_range=RANGE, landed_rows=[], wave=1,
                certified_plans=[ui])
    assert wb["verdict"] == "CLEAN"
    other = _check(root, [a], certified_plans=["docs/plans/elsewhere.md"])
    assert other["verdict"] == "REFUSED"


def test_certified_plans_must_stay_inside_the_worktree(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    with pytest.raises(ValueError, match="certified plan escapes"):
        _check(root, [a], certified_plans=["../outside.md"])


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


def test_a_repo_declared_hub_does_not_collide_without_row_annotations(tmp_path, git):
    root = _root(tmp_path)
    (root / "coordinator.local.md").write_text(
        '---\nappend_only_paths: "s/hub.ts, ./s/other-hub.ts"\n---\n', encoding="utf-8"
    )
    a = _plan(root, "a", _row("R1", "[s/hub.ts, s/x.py]"))
    b = _plan(root, "b", _row("R2", "[s/hub.ts, s/x.py]"))
    r = _check(root, [a, b])
    assert r["verdict"] == "REFUSED" and {f["path"] for f in r["findings"]} == {"s/x.py"}


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


def test_a_pair_ordered_through_a_plan_outside_the_set_is_clean(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[s/x.py]"))
    mid = _plan(root, "m", _row("R3", "[s/m.py]"),
                fm=f"capabilities: []\ndepends_on_plan:\n  - {{plan: {a}, status: approved, gate_kind: k}}\n")
    b = _plan(root, "b", _row("R2", "[s/x.py]"),
              fm=f"capabilities: []\ndepends_on_plan:\n  - {{plan: {mid}, status: approved, gate_kind: k}}\n")
    assert _check(root, [a, mid, b])["verdict"] == "CLEAN"
    r = _check(root, [a, b])
    assert not any(f["class"] == "writes-collision" and f["path"] for f in r["findings"])


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


def test_a_landed_row_reported_by_its_minted_inventory_id_still_counts_as_landed(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[x/a.py]") + _row("A2", "[x/b.py]"))
    b = _plan(root, "b", _row("B1", "[b.py]", "[x/a.py]"))
    git.touched = ["x/a.py"]
    git.landed.add("x/a.py")
    r = _wb(root, [a, b], [(a, "chunk-a.A1")])
    assert r["verdict"] == "CLEAN", r["findings"]
    assert not [f for f in r["findings"] if "no landed row declared" in f["detail"]]


def test_an_unlanded_row_hitting_a_touched_path_is_still_drift(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[x/a.py]"))
    b = _plan(root, "b", _row("B1", "[x/z.py]"))
    git.touched = ["x/z.py"]
    r = _wb(root, [a, b], [(a, "chunk-a.A1")])
    assert r["verdict"] == "DRIFT"


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
    d = yaml.safe_load((root / "docs/plans/b.wave-3.seam.yaml").read_text())
    _validate(d)
    assert d["verdict"] == "DRIFT" and d["wave"] == 3 and r["verdict"] == "DRIFT"
    assert "docs/plans/b.wave-3.seam.yaml" in r["sidecars"]
    assert not (root / "docs/plans/b.seam.yaml").exists()


def test_wave_boundary_record_leaves_prep_sidecar_byte_identical(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("A1", "[lib.py]"))
    b = _plan(root, "b", _row("B1", "[b.py]", "[lib.py]"))
    op._record_handler({"plans": [a, b], "phase": "prep", "named_set": True}, repo_root=root)
    prep = root / "docs/plans/b.seam.yaml"
    before = prep.read_bytes()
    r = op._record_handler({"plans": [b], "phase": "wave-boundary", "named_set": True, "landed_range": RANGE,
                            "landed_rows": [{"plan": a, "row": "A1"}], "wave": 1}, repo_root=root)
    assert prep.read_bytes() == before
    assert r["sidecars"] == ["docs/plans/b.wave-1.seam.yaml"]
    assert (root / "docs/plans/b.wave-1.seam.yaml").is_file()
    assert yaml.safe_load(before.decode())["phase"] == "prep"


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
    nrec = [f for f in named["findings"] if f["class"] == "writes-collision"]
    assert len(nrec) == 3 and all(f["blocking"] for f in nrec)
    r = _check(root, plans, named=False)
    records = [f for f in r["findings"] if f["class"] == "writes-collision"]
    assert r["verdict"] == "CLEAN"
    assert sorted(f["plan"] for f in records) == sorted(plans)
    for f in records:
        assert f["blocking"] is False and f["path"] == "s/x.py"
        assert f["counterpart_plan"] in plans and f["counterpart_plan"] != f["plan"]
        assert f["detail"].startswith("3 plans declare writes to s/x.py")


def test_three_named_writers_of_one_path_refuse_every_plan(tmp_path, git):
    root = _root(tmp_path)
    plans = [_plan(root, n, _row("R1", "[s/x.py]")) for n in ("a", "b", "c")]
    r = _check(root, plans, named=True)
    records = [f for f in r["findings"] if f["class"] == "writes-collision"]
    assert len(records) == 3 and r["verdict"] == "REFUSED"
    assert r["per_plan"] == {p: "REFUSED" for p in plans}


def test_forty_named_writers_give_forty_records_not_pairs(tmp_path, git):
    root = _root(tmp_path)
    plans = [_plan(root, f"p{i:02d}", _row("R1", "[s/x.py]")) for i in range(40)]
    r = _check(root, plans, named=True)
    records = [f for f in r["findings"] if f["class"] == "writes-collision"]
    assert len(records) == 40 and all(f["blocking"] for f in records)


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
    r = _check(root, [a], universe=True)
    [f] = r["findings"]
    assert f["class"] == "missing-seam" and f["blocking"] is False and f["counterpart_plan"] == b
    assert "written by coded row W1 but absent at HEAD" in f["detail"] and r["verdict"] == "CLEAN"
    git.tracked.add("lib/y.py")
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


def test_universe_findings_persist_in_a_schema_valid_sidecar(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"))
    _plan(root, "b", _row("R1", "[x.py]"))
    r = op._record_handler({"plans": [a], "phase": "prep", "named_set": True, "universe": True}, repo_root=root)
    assert len(_seams(r)) == 1
    doc = yaml.safe_load((root / "docs/plans/a.seam.yaml").read_text(encoding="utf-8"))
    assert [f["class"] for f in doc["findings"]] == ["missing-seam"] and doc["schema_version"] == "1.1.0"
    assert doc["findings"][0]["blocking"] is False
    _validate(doc)


def test_universe_capped_rollup_and_consume_seam_sidecar_validate(tmp_path, git):
    root = _root(tmp_path)
    paths = ", ".join(f"f{i:03d}.py" for i in range(60))
    a = _plan(root, "a", _row("R1", f"[{paths}]", consumes="[lib/y.py]"))
    _plan(root, "b", _row("R1", f"[{paths}, lib/y.py]"))
    op._record_handler({"plans": [a], "phase": "prep", "named_set": True, "universe": True}, repo_root=root)
    doc = yaml.safe_load((root / "docs/plans/a.seam.yaml").read_text(encoding="utf-8"))
    assert len(doc["findings"]) == op.MISSING_SEAM_CAP + 1
    _validate(doc)


def test_consume_of_an_open_outside_writers_path_is_one_non_blocking_missing_seam(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]", consumes="[lib/y.py]"))
    c = _plan(root, "c", _row("W1", "[lib/y.py]"))
    d = _plan(root, "d", _row("W1", "[lib/y.py]", extra="  appends: [lib/y.py]\n"))
    r = _check(root, [a], universe=True)
    assert r["verdict"] == "CLEAN" and _classes(r, blocking=True) == []
    assert [f["class"] for f in r["findings"]] == ["missing-seam"]
    [f] = r["findings"]
    assert f["counterpart_plan"] == c and d in f["detail"] and f["blocking"] is False
    off = _check(root, [a])
    assert off["verdict"] != "CLEAN" and [x["class"] for x in off["findings"]] == ["unpromised-export"]


def test_an_appending_outside_plan_counts_as_the_writer(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]", consumes="[log.md]"))
    c = _plan(root, "c", _row("W1", "[log.md]", extra="  appends: [log.md]\n"))
    r = _check(root, [a], universe=True)
    assert [(f["class"], f["counterpart_plan"], f["blocking"]) for f in r["findings"]] == [("missing-seam", c, False)]


def test_consume_nobody_writes_stays_a_blocking_export_with_universe(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]", consumes="[lib/z.py]"))
    _plan(root, "c", _row("W1", "[other.py]"))
    r = _check(root, [a], universe=True)
    assert r["verdict"] != "CLEAN"
    assert [f["class"] for f in r["findings"] if f["blocking"]] == ["unpromised-export"]


_HUB = "  appends: [hub.md]\n"
_CAP_NO_CONSUMER = "capabilities:\n  - id: cap1\n    statement: s\n    role: admin\n    click_path: A > B\n"


def _golden_set(root: Path) -> list:
    """Collisions (exact, prefix, append pair, ordered), an export, a capability finding,
    an undetermined row and an unreadable spine, in one set."""
    return [
        _plan(root, "a", _row("R1", "[s/x.py]") + _row("R2", "[lib/a.py]", consumes="[lib/missing.py]")),
        _plan(root, "b", _row("R1", "[s/x.py]") + _row("R2", "[x.py]", extra="  writes_under: [pkg/]\n")),
        _plan(root, "c", _row("R1", "[s/x.py]") + _row("R2", "[hub.md]", extra=_HUB)),
        _plan(root, "d", _row("R1", "[pkg/inner.py]") + _row("R2", "[hub.md]", extra=_HUB)),
        _plan(root, "e", _row("R1", "[s/x.py]"),
              fm="capabilities: []\ndepends_on_plan:\n  - plan: docs/plans/a.md\n    status: approved\n"),
        _plan(root, "f", _row("R1", "[f.py]"), fm=_CAP_NO_CONSUMER),
        _plan(root, "g", _row("R1")),
        _plan(root, "h", _row("R1", "[h.py]") + _row("R1", "[h2.py]")),
    ]


def _golden_replies(root: Path) -> dict:
    plans = _golden_set(root)
    return {"named": _check(root, plans, named=True), "default": _check(root, plans, named=False)}


_FINDING_KEYS = ("plan", "class", "blocking", "counterpart_plan", "capability", "missing_consumer",
                 "path", "row", "detail")
_EXPORT_DETAIL = ("row R2 consumes lib/missing.py, which no row of its plan or of another plan in the set "
                  "writes and which is not tracked at HEAD")
_CAP_MISSING = "A > B (role admin): no ui_consumer or ui_carve_out"
_CAP_DETAIL = "capability declares neither ui_consumer nor ui_carve_out"
_WRITES_X = "plans declare writes to s/x.py with no depends_on_plan edge between them"
_WRITES_PKG = "2 plans declare writes to pkg/inner.py with no depends_on_plan edge between them"
_UNREADABLE = "undetermined: spine unreadable: duplicate row id 'R1' in spine"
_PLANS_SHORT = "abcdefgh"


def _golden_findings(blocking: bool) -> list:
    """The golden findings, in order, with plans as `@<name>.md` and `blocking` for a collision."""
    c = blocking
    return [
        ("@a.md", "unpromised-export", True, None, None, None, "lib/missing.py", "R2", _EXPORT_DETAIL),
        ("@a.md", "writes-collision", c, "@b.md", None, None, "s/x.py", None, f"3 {_WRITES_X}; also @c.md"),
        ("@b.md", "writes-collision", c, "@d.md", None, None, "pkg/inner.py", None, _WRITES_PKG),
        ("@b.md", "writes-collision", c, "@a.md", None, None, "s/x.py", None, f"4 {_WRITES_X}; also @c.md, @e.md"),
        ("@c.md", "writes-collision", c, "@a.md", None, None, "s/x.py", None, f"4 {_WRITES_X}; also @b.md, @e.md"),
        ("@d.md", "writes-collision", c, "@b.md", None, None, "pkg/inner.py", None, _WRITES_PKG),
        ("@e.md", "writes-collision", c, "@b.md", None, None, "s/x.py", None, f"3 {_WRITES_X}; also @c.md"),
        ("@f.md", "capability-without-ui-consumer", True, None, "cap1", _CAP_MISSING, None, None, _CAP_DETAIL),
        ("@g.md", "writes-collision", False, None, None, None, None, "R1", "undetermined"),
        ("@h.md", "writes-collision", c, None, None, None, None, None, _UNREADABLE),
    ]


def _short(reply: dict) -> tuple:
    """(verdict, per_plan, findings) with `docs/plans/` shortened to `@`; every finding carries exactly the contract keys."""
    def at(v):
        return v.replace("docs/plans/", "@") if isinstance(v, str) else v

    assert all(tuple(f) == _FINDING_KEYS for f in reply["findings"])
    return (reply["verdict"], {at(k): v for k, v in reply["per_plan"].items()},
            [tuple(at(f[k]) for k in _FINDING_KEYS) for f in reply["findings"]])


def test_reply_over_a_multi_plan_set_is_pinned(tmp_path, git):
    """The reply is the contract: the values below are what the pre-rebuild evaluation returned."""
    root = _root(tmp_path)
    replies = _golden_replies(root)
    assert replies["named"]["set"] == [f"docs/plans/{n}.md" for n in _PLANS_SHORT]
    assert replies["named"]["checked_at_sha"] == HEAD and replies["named"]["phase"] == "prep"
    assert _short(replies["named"]) == (
        "REFUSED",
        {"@a.md": "REFUSED", "@b.md": "REFUSED", "@c.md": "REFUSED", "@d.md": "REFUSED",
         "@e.md": "REFUSED", "@f.md": "REFUSED", "@g.md": "CLEAN", "@h.md": "REFUSED"},
        _golden_findings(True))
    assert _short(replies["default"]) == (
        "REFUSED",
        {"@a.md": "REFUSED", "@b.md": "CLEAN", "@c.md": "CLEAN", "@d.md": "CLEAN",
         "@e.md": "CLEAN", "@f.md": "REFUSED", "@g.md": "CLEAN", "@h.md": "CLEAN"},
        _golden_findings(False))
