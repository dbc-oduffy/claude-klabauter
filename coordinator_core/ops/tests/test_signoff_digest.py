"""Pins signoff.digest: one item per surface per bucket, legacy rows unrecorded, date window."""
import textwrap

from coordinator_core.ops.signoff_digest import _handler

PM = {"source": "pm", "pm_quote": "yes", "on": "2026-10-09"}
APM = {"source": "apm", "apm_ruling": "fine", "ruling_ref": "docs/decisions/x.md", "on": "2026-10-09"}


def _plan(root, name, front, rows):
    spine = "```yaml plan-tasks\n" + rows + "\n```\n"
    p = root / "docs/plans" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{front}\n---\n\n## Tasks\n\n{spine}", encoding="utf-8")


def _sizing(root, name, accepted):
    p = root / "state/sizings" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"exit_criterion:\n  statement: s\n  accepted: {accepted}\n", encoding="utf-8")


def test_each_surface_each_bucket(tmp_path):
    front = textwrap.dedent("""\
        plan_id: pln-1
        execution_authorized_by: PM
        execution_authorized_note: go
        execution_authorized_at: 2026-10-09
        grouping_approvals:
          do:
            status: approved
            signoff: {source: apm, apm_ruling: fine, ruling_ref: r.md, on: 2026-10-09}
          defer:
            status: approved""")
    rows = ("- {id: A, pm_approved: true, signoff: {source: pm, pm_quote: yes, on: 2026-10-09}}\n"
            "- {id: B, pm_approved: true, signoff: {source: apm, apm_ruling: fine, ruling_ref: r.md, on: 2026-10-09}}\n"
            "- {id: C, pm_approved: true}\n"
            "- {id: D, pm_approved: false}")
    _plan(tmp_path, "2026-10-09-p.md", front, rows)
    _sizing(tmp_path, "2026-10-09-pm.yaml", "{pm_quote: ok, on: '2026-10-09'}")
    _sizing(tmp_path, "2026-10-09-apm.yaml", "{source: apm, apm_ruling: ok, ruling_ref: r.md, on: '2026-10-09'}")
    _sizing(tmp_path, "2026-10-09-none.yaml", "{source: pm, on: '2026-10-09'}")
    r = _handler({"since": "2026-10-01"}, tmp_path)
    got = {b: {(i["surface"], i["target"]) for i in r[b]} for b in ("pm_verified", "delegated", "unrecorded")}
    assert got["pm_verified"] == {("exec-auth", "pln-1"), ("row", "row:A"), ("sizing", "2026-10-09-pm")}
    assert got["delegated"] == {("grouping", "grouping:do"), ("row", "row:B"), ("sizing", "2026-10-09-apm")}
    assert got["unrecorded"] == {("grouping", "grouping:defer"), ("row", "row:C"), ("sizing", "2026-10-09-none")}
    assert r["summary"]["pm_verified"].startswith("pm_verified: 3")


def test_legacy_pm_approved_is_unrecorded_and_window_excludes_old(tmp_path):
    _plan(tmp_path, "2026-10-09-new.md", "plan_id: n", "- {id: A, pm_approved: true}")
    _plan(tmp_path, "2026-01-01-old.md", "plan_id: o", "- {id: A, pm_approved: true}")
    r = _handler({"since": "2026-10-01"}, tmp_path)
    assert [i["path"] for i in r["unrecorded"]] == ["docs/plans/2026-10-09-new.md"]
    assert r["pm_verified"] == [] and r["delegated"] == []


def test_bad_since_and_missing_root(tmp_path):
    assert _handler({"since": "nope"}, tmp_path)["exit_code"] == 1
    assert _handler({}, None)["exit_code"] == 1
