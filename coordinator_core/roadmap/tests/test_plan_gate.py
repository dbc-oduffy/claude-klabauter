"""
coordinator_core/roadmap/tests/test_plan_gate.py — baton and plan record readers.

Subject: `coordinator_core.roadmap.plan_gate`, the shared disk-read layer: the
narrow frontmatter scanner, the plan index and link resolution, the baton scan
and the sizing-route reader.

Zero spawns; every case builds its corpus in `tmp_path`. The exception is
`test_narrow_scan_agrees_with_the_general_parser`, which reads this repo's own
records — a parity oracle needs a corpus it did not author, and a hand-built
fixture would only prove the scanner agrees with the shapes its author thought
of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap import plan_gate as pg


def _write(path: Path, frontmatter: str, body: str = "body\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter.strip()}\n---\n\n{body}", encoding="utf-8")
    return path


def _baton(root: Path, stub_id: str, **fields) -> Path:
    lines = [
        "kind: roadmap-baton",
        f"title: {fields.pop('title', stub_id)}",
        f"stub_id: {stub_id}",
        f"status: {fields.pop('status', 'open')}",
        f"deployment_state: {fields.pop('deployment_state', 'ready_to_fire')}",
        "baton_role: work",
    ]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    return _write(root / "state" / "handoffs" / f"{stub_id}.md", "\n".join(lines))


def _normalise(value):
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return [str(item) for item in value]
    return value


@pytest.mark.parametrize("subtree", [("state", "handoffs"), ("archive", "handoffs")])
def test_narrow_scan_agrees_with_the_general_parser(subtree):
    root = Path(__file__).resolve().parents[3]
    paths = pg._iter_record_paths(root, subtree, recursive=(subtree[0] == "archive"))
    if not paths:
        pytest.skip(f"{'/'.join(subtree)} is empty in this checkout")

    corrupted_by_the_general_parser = {"shipped_in"}

    disagreements = []
    for path in paths:
        narrow = pg._read_baton_fields(path)
        general = pg._read_frontmatter_head(path)
        for field in pg._BATON_FIELDS - corrupted_by_the_general_parser:
            got, want = _normalise(narrow.get(field)), _normalise(general.get(field))
            if got != want:
                disagreements.append((path.name, field, got, want))

    assert not disagreements, (
        f"{len(disagreements)} field(s) read differently by the narrow scanner "
        f"and the general parser; first 5: {disagreements[:5]}"
    )


#: Every block-scalar shape `_parse_yaml_mapping_block` and `_scan_fields`
#: must agree on: folded and literal, with each chomping indicator and with
#: none (`_consume_block_scalar` does not distinguish `-`/`+`/bare in its
#: RETURNED text — trailing blank body lines are always dropped — so this
#: list exists to pin that non-distinction on both readers, not to expect a
#: different result per form).
_BLOCK_SCALAR_FORMS = ["|", "|-", "|+", ">", ">-", ">+"]


def test_narrow_scan_agrees_on_a_block_scalar_shape_per_scanned_key(tmp_path):
    preset = {"kind", "title", "stub_id", "status", "deployment_state", "baton_role"}
    lines = [
        "kind: roadmap-baton",
        "title: block-scalar-fixture",
        "stub_id: block-scalar-fixture",
        "status: open",
        "deployment_state: ready_to_fire",
        "baton_role: work",
    ]
    scanned = sorted(pg._BATON_FIELDS - preset)
    for index, key in enumerate(scanned):
        form = _BLOCK_SCALAR_FORMS[index % len(_BLOCK_SCALAR_FORMS)]
        lines.append(f"{key}: {form}")
        lines.append(f"  line one of {key}")
        lines.append(f"  line two of {key}")
    path = _write(tmp_path / "state" / "handoffs" / "block-scalar-fixture.md", "\n".join(lines))

    narrow = pg._read_baton_fields(path)
    general = pg._read_frontmatter_head(path)

    disagreements = [
        (key, _normalise(narrow.get(key)), _normalise(general.get(key)))
        for key in scanned
        if _normalise(narrow.get(key)) != _normalise(general.get(key))
    ]
    assert not disagreements, (
        f"{len(disagreements)} field(s) read differently on a block scalar; "
        f"first 5: {disagreements[:5]}"
    )


def test_kind_plan_is_admitted_because_the_template_emits_it():
    assert pg.is_plan_record({"kind": "plan"}) is True
    assert pg.is_plan_record({}) is True
    assert pg.is_plan_record({"kind": "staff-eng-review"}) is False
    assert pg.is_plan_record({"plan": "docs/plans/x.md"}) is False
    assert pg.is_plan_record({"kind": "plan", "plan": "docs/plans/x.md"}) is False


def test_scan_batons_indexes_a_baton_by_stub_id_and_filename_stem(tmp_path):
    _baton(tmp_path, "live-1")

    by_id, records = pg.scan_batons(tmp_path)

    assert [r["id"] for r in records] == ["live-1"]
    assert by_id["live-1"] is records[0]
    assert records[0]["candidate"] is True


def test_scan_batons_resolves_an_archived_blocker_only_when_a_live_edge_names_it(tmp_path):
    _baton(tmp_path, "dependent-1", blocked_by=["old-1"])
    _write(
        tmp_path / "archive" / "handoffs" / "2026-09" / "old-1.md",
        "kind: roadmap-baton\nstub_id: old-1\nstatus: closed\n"
        "deployment_state: shipped\nbaton_role: work",
    )
    _write(
        tmp_path / "archive" / "handoffs" / "2026-09" / "unnamed-1.md",
        "kind: roadmap-baton\nstub_id: unnamed-1\nstatus: closed\n"
        "deployment_state: shipped\nbaton_role: work",
    )

    by_id, records = pg.scan_batons(tmp_path)

    assert by_id["old-1"]["live"] is False
    assert "unnamed-1" not in by_id
    assert [r["id"] for r in records] == ["dependent-1", "old-1"]

    by_id, _ = pg.scan_batons(tmp_path, include_archived=False)
    assert "old-1" not in by_id


def test_a_live_baton_wins_an_id_collision_with_an_archived_namesake(tmp_path):
    _baton(tmp_path, "twin-1")
    _baton(tmp_path, "dependent-1", blocked_by=["twin-1"])
    _write(
        tmp_path / "archive" / "handoffs" / "twin-1.md",
        "kind: roadmap-baton\nstub_id: twin-1\nstatus: closed\n"
        "deployment_state: shipped\nbaton_role: work",
    )

    by_id, _ = pg.scan_batons(tmp_path)

    assert by_id["twin-1"]["live"] is True


def _xl_sizing(root: Path, slug: str, *, xl_exit: str) -> str:
    rel = f"state/sizings/{slug}.yaml"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema: sizing-object\n"
        "route: pm-decision  # dispatch | spec-dispatch | shape | plan | roadmap\n"
        "detents: []\n"
        "fork: null\n"
        f"xl_exit: {xl_exit}\n"
        "status: routed\n",
        encoding="utf-8",
    )
    return rel


def test_an_accepted_xl_exit_resolves_pm_decision_to_plan(tmp_path):
    """example-market-data-repo, 2026-09-11: a sizing resolved on 2026-08-05 —
    appetite raised, `xl_exit: accept_multi_session` assented, the EM's split
    DECLINED — still routed pm-decision in every wave since, so each wave
    re-asked a question answered in the file it had just read. The PM called
    that class of escalation hedging."""
    rel = _xl_sizing(tmp_path, "resolved", xl_exit="accept_multi_session")
    assert pg._sizing_route(tmp_path, [rel]) == "plan"


@pytest.mark.parametrize("exit_value", ["split", "shape", "roadmap"])
def test_the_other_xl_exits_stay_at_the_gate(tmp_path, exit_value):
    rel = _xl_sizing(tmp_path, f"exit-{exit_value}", xl_exit=exit_value)
    assert pg._sizing_route(tmp_path, [rel]) == "pm-decision"


def test_an_unset_xl_exit_is_not_an_acceptance(tmp_path):
    rel = _xl_sizing(tmp_path, "unset", xl_exit="null")
    assert pg._sizing_route(tmp_path, [rel]) == "pm-decision"


def test_a_nested_route_key_does_not_shadow_the_top_level_one(tmp_path):
    rel = "state/sizings/nested.yaml"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema: sizing-object\n"
        "em_analysis:\n"
        "  route: dispatch\n"
        "route: plan  # dispatch | spec-dispatch | shape | plan | roadmap\n",
        encoding="utf-8",
    )
    assert pg._sizing_route(tmp_path, [rel]) == "plan"


def test_a_comment_after_a_quoted_scalar_is_still_a_comment():
    assert pg._unquote('""  # both gates discharged 2026-08-29') == ""
    assert pg._unquote('"a real gate"  # deprecated') == "a real gate"
    assert pg._unquote("'it''s gated'  # note") == "it's gated"
    assert pg._unquote('"a # inside the quotes"') == "a # inside the quotes"
    assert pg._unquote('"unterminated  # not ours to truncate') == (
        '"unterminated  # not ours to truncate'
    )
