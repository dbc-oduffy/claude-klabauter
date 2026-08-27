"""
coordinator_core.roadmap.tests.test_number_stubs — independent parity regression
net for coordinator_core.roadmap.number_stubs, re-derived from the CLI-smoke
acceptance criteria (AC9) in the oracle's own test file
(coordinator/bin/tests/test-roadmap-graph.js, DoE-claude) plus a direct oracle run
(``node coordinator/bin/roadmap-number-stubs.js <fixture>``) whose stdout was
captured and hardcoded below as the expected byte-parity output — not derived by
re-reading this port's own source.

Spec backlink: docs/plans/2026-06-28-roadmap-stub-numbering-dependency-order.md § C2
Port backlink: BIG_PORT Wave B, item roadmap-pair.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.roadmap.number_stubs import (
    derive_nodes,
    main,
    parse_edges_file,
    run_check_mode,
    run_state_mode,
)

# ---------------------------------------------------------------------------
# parse_edges_file / derive_nodes
# ---------------------------------------------------------------------------


def test_parse_edges_file_line_form_with_comments_and_isolated_node():
    content = "# comment\nWIDGET <- BASE\nGADGET <- WIDGET\nLONER\n"
    parsed = parse_edges_file(content)
    assert parsed["edges"] == [
        {"from": "WIDGET", "to": "BASE"},
        {"from": "GADGET", "to": "WIDGET"},
    ]
    assert parsed["isolatedNodes"] == ["LONER"]


def test_parse_edges_file_malformed_line_is_a_hard_error(capsys):
    """Was ``test_parse_edges_file_malformed_line_skipped``. A line containing
    ``<-`` with an empty side is an *attempted* edge; the old warn-and-skip
    silently changed the DAG and the WARNING went unread in a
    transcribe-by-hand authoring flow.
    """
    content = "<- BASE\nWIDGET <- BASE\n"
    with pytest.raises(SystemExit) as excinfo:
        parse_edges_file(content)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "ERROR: malformed edge line 1" in err
    assert "A <- B" in err
    assert "WARNING" not in err


def test_parse_edges_file_malformed_right_side_is_a_hard_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        parse_edges_file("WIDGET <-\nGADGET <- BASE\n")
    assert excinfo.value.code == 2
    assert "ERROR: malformed edge line 1" in capsys.readouterr().err


def test_parse_edges_file_multiline_zero_edge_file_is_refused(capsys):
    """A whole file in the wrong notation used to parse as N isolated nodes and
    zero edges, then print a confident, wrong numbering at exit 0.
    """
    content = "WIDGET blocked_by BASE\nGADGET blocked_by WIDGET\n"
    with pytest.raises(SystemExit) as excinfo:
        parse_edges_file(content)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "zero edges" in err
    assert 'first offending line 1: "WIDGET blocked_by BASE"' in err
    assert "A <- B" in err
    assert "edge-free roadmap needs no numbering op" in err


def test_parse_edges_file_zero_edge_refusal_ignores_comments_and_blanks(capsys):
    """The >1-line floor counts non-comment, non-blank lines only — a single
    isolated node buried in comments stays legal.
    """
    parsed = parse_edges_file("# header\n\nLONER\n\n# trailer\n")
    assert parsed["edges"] == []
    assert parsed["isolatedNodes"] == ["LONER"]

    with pytest.raises(SystemExit) as excinfo:
        parse_edges_file("# header\nA -> B\nB -> C\n")
    assert excinfo.value.code == 2
    assert 'first offending line 2: "A -> B"' in capsys.readouterr().err


def test_parse_edges_file_single_isolated_node_still_legal():
    parsed = parse_edges_file("LONER\n")
    assert parsed["edges"] == []
    assert parsed["isolatedNodes"] == ["LONER"]


def test_parse_edges_file_isolated_node_alongside_an_edge_still_legal():
    """The refusal is scoped to *zero* edges — an isolated node in a file that
    also carries real edges is a legitimate authoring case.
    """
    parsed = parse_edges_file("WIDGET <- BASE\nLONER\n")
    assert parsed["edges"] == [{"from": "WIDGET", "to": "BASE"}]
    assert parsed["isolatedNodes"] == ["LONER"]


def test_parse_edges_file_json_form():
    content = json.dumps([{"from": "BETA", "to": "ALPHA"}])
    parsed = parse_edges_file(content)
    assert parsed["edges"] == [{"from": "BETA", "to": "ALPHA"}]
    assert parsed["isolatedNodes"] == []


def test_parse_edges_file_json_malformed_entry_exits_1():
    content = json.dumps([{"from": "BETA"}])
    with pytest.raises(SystemExit) as excinfo:
        parse_edges_file(content)
    assert excinfo.value.code == 1


def test_parse_edges_file_json_form_behaviour_unchanged_by_line_form_refusals(capsys):
    """The line-form refusals are scoped to the line form. The JSON branch
    already failed loud (exit 1, "looks like JSON but failed to parse") and
    keeps that exit code and message shape — a caller may be reading it.
    """
    with pytest.raises(SystemExit) as excinfo:
        parse_edges_file('[{"from": "BETA", "to": ')
    assert excinfo.value.code == 1
    assert "ERROR: file looks like JSON but failed to parse" in capsys.readouterr().err

    # An empty JSON array yields zero edges and does NOT trip the line-form
    # zero-edge refusal — the JSON branch returns before that check.
    assert parse_edges_file("[]") == {"edges": [], "isolatedNodes": [], "sprints": {}}


def test_default_mode_refuses_wrong_notation_file(tmp_path, capsys):
    edges_file = tmp_path / "edges.txt"
    edges_file.write_text("WIDGET blocked_by BASE\nGADGET blocked_by WIDGET\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main([str(edges_file)])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "zero edges" in captured.err
    assert captured.out == ""


def test_derive_nodes_preserves_first_seen_order_dependency_before_dependent():
    edges = [{"from": "WIDGET", "to": "BASE"}, {"from": "GADGET", "to": "WIDGET"}]
    nodes = derive_nodes(edges, [])
    assert nodes == ["BASE", "WIDGET", "GADGET"]


def test_derive_nodes_includes_isolated_nodes():
    nodes = derive_nodes([], ["LONER"])
    assert nodes == ["LONER"]


# ---------------------------------------------------------------------------
# Default-mode CLI byte-parity (against a captured oracle run)
# ---------------------------------------------------------------------------

# Captured via: node coordinator/bin/roadmap-number-stubs.js <line-form fixture>
_ORACLE_DEFAULT_MODE_OUTPUT = (
    "# Roadmap stub linearization\n"
    "# Transcribe these values into stub frontmatter (number, sprint, wave).\n"
    "# Dependencies appear before their dependents.\n"
    "# Stub numbers are zero-padded to ≥2 digits — use the padded form in "
    "frontmatter and filenames.\n"
    "#\n"
    "# label                    stub#  sprint  wave\n"
    "# " + "-" * 52 + "\n"
    "BASE                        01      1       1\n"
    "WIDGET                      02      1       2\n"
    "GADGET                      03      1       3\n"
)


def test_default_mode_cli_byte_parity_with_oracle(tmp_path, capsys):
    edges_file = tmp_path / "edges.txt"
    edges_file.write_text("# test fixture\nWIDGET <- BASE\nGADGET <- WIDGET\n", encoding="utf-8")

    rc = main([str(edges_file)])

    out = capsys.readouterr().out
    assert rc == 0
    assert out == _ORACLE_DEFAULT_MODE_OUTPUT


def test_default_mode_dependency_at_lower_stub_number(tmp_path, capsys):
    edges_file = tmp_path / "edges.txt"
    edges_file.write_text("WIDGET <- BASE\nGADGET <- WIDGET\n", encoding="utf-8")
    main([str(edges_file)])
    out = capsys.readouterr().out
    lines = {ln.split()[0]: int(ln.split()[1]) for ln in out.splitlines() if ln and not ln.startswith("#")}
    assert lines["BASE"] < lines["WIDGET"] < lines["GADGET"]


def test_default_mode_exits_1_on_cyclic_edges_file(tmp_path, capsys):
    edges_file = tmp_path / "cyclic.txt"
    edges_file.write_text("P <- Q\nQ <- P\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main([str(edges_file)])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "dependency cycle detected" in err


def test_default_mode_accepts_json_form(tmp_path, capsys):
    edges_file = tmp_path / "edges.json"
    edges_file.write_text(json.dumps([{"from": "BETA", "to": "ALPHA"}]), encoding="utf-8")

    rc = main([str(edges_file)])
    out = capsys.readouterr().out
    assert rc == 0
    lines = {ln.split()[0]: int(ln.split()[1]) for ln in out.splitlines() if ln and not ln.startswith("#")}
    assert lines["ALPHA"] < lines["BETA"]


def test_default_mode_sprint_1_and_increasing_wave(tmp_path, capsys):
    edges_file = tmp_path / "chain.txt"
    edges_file.write_text("B <- A\nC <- B\n", encoding="utf-8")

    main([str(edges_file)])
    out = capsys.readouterr().out
    rows = {}
    for ln in out.splitlines():
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split()
        rows[parts[0]] = {"num": int(parts[1]), "sprint": int(parts[2]), "wave": int(parts[3])}

    assert all(r["sprint"] == 1 for r in rows.values())
    assert rows["A"]["wave"] < rows["B"]["wave"] < rows["C"]["wave"]


# ---------------------------------------------------------------------------
# CLI usage-error / exit-code contract
# ---------------------------------------------------------------------------


def test_main_no_args_exits_2(capsys):
    rc = main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Usage:" in err


def test_main_unreadable_edges_file_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "does-not-exist.txt")])
    assert excinfo.value.code == 2


def test_main_unknown_flag_exits_2(capsys):
    rc = main(["--bogus"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown flag" in err


def test_main_check_missing_run_id_exits_2(capsys):
    rc = main(["--check"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a <run-id>" in err


def test_main_check_malformed_run_id_exits_2(capsys):
    rc = main(["--check", "Bad_ID!"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "must match" in err


# ---------------------------------------------------------------------------
# --check mode against on-disk stub fixtures
# ---------------------------------------------------------------------------


def _write_stub(
    path: Path,
    roadmap_id: str,
    stub_id: str,
    number: int,
    sprint,
    wave,
    blocked_by=None,
    kind: str = "spinoff-roadmap",
) -> None:
    # Review: code-reviewer (P1, Finding 1) — `kind` defaults to the retired
    # spelling for byte-parity with existing callers; new tests below pass
    # `kind="roadmap-baton"` to prove `run_check_mode` actually finds
    # canonical-spelling stubs (the live defect the `where=` fix closed).
    lines = [
        "---",
        f'title: "Test stub {stub_id}"',
        "created: 2026-07-17",
        "status: open",
        "deployment_state: in_flight",
        f"kind: {kind}",
        f"roadmap_id: {roadmap_id}",
        f"stub_id: {stub_id}",
        f"number: {number}",
    ]
    if sprint is not None:
        lines.append(f"sprint: {sprint}")
    if wave is not None:
        lines.append(f"wave: {wave}")
    if blocked_by:
        lines.append("blocked_by:")
        for dep in blocked_by:
            lines.append(f"  - {dep}")
    else:
        lines.append("blocked_by: []")
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_check_mode_no_stubs_found_exits_0(tmp_path, capsys):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    rc = run_check_mode("nonexistent-roadmap")
    out = capsys.readouterr().out
    assert rc == 0
    assert "No roadmap-baton (spinoff-roadmap) stubs found" in out


def test_check_mode_clean_stub_set_exits_0(tmp_path, monkeypatch, capsys):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(handoffs / "stub-a-1.md", "rm-check", "stub-a-1", 1, 1, 1)
    _write_stub(
        handoffs / "stub-b-2.md", "rm-check", "stub-b-2", 2, 1, 2, blocked_by=["stub-a-1"]
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-check")
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: dependency order is valid" in out


def test_check_mode_violation_exits_1(tmp_path, monkeypatch, capsys):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    # b-2 blocked_by a-1 but shares the same (sprint, wave) slot — violation.
    _write_stub(handoffs / "stub-a-1.md", "rm-check2", "stub-a-1", 1, 1, 1)
    _write_stub(
        handoffs / "stub-b-2.md", "rm-check2", "stub-b-2", 2, 1, 1, blocked_by=["stub-a-1"]
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-check2")
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [same-or-inverted-slot]" in err


def test_main_check_dispatches_to_run_check_mode(tmp_path, monkeypatch, capsys):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = main(["--check", "rm-check3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No roadmap-baton (spinoff-roadmap) stubs found for roadmap_id=rm-check3" in out


# ---------------------------------------------------------------------------
# Canonical `kind: roadmap-baton` spelling — Review: code-reviewer (P1,
# Finding 1). Every fixture above seeds the RETIRED `kind: spinoff-roadmap`
# spelling, which already matched the pre-fix hardcoded `kind=spinoff-roadmap`
# literal — so none exercise `_ROADMAP_BATON_KIND_WHERE`'s `kind in (...)`
# term against the canonical spelling the live defect was about.
# ---------------------------------------------------------------------------


def test_check_mode_canonical_kind_roadmap_baton_is_found(tmp_path, monkeypatch, capsys):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(
        handoffs / "stub-a-1.md", "rm-check-canon", "stub-a-1", 1, 1, 1,
        kind="roadmap-baton",
    )
    _write_stub(
        handoffs / "stub-b-2.md", "rm-check-canon", "stub-b-2", 2, 1, 2,
        blocked_by=["stub-a-1"], kind="roadmap-baton",
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-check-canon")
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: dependency order is valid" in out
    assert "2 stub(s) checked" in out


def test_check_mode_dual_spelling_both_legacy_and_canonical_found(
    tmp_path, monkeypatch, capsys
):
    # Dual-acceptance is the contract: a mid-migration roadmap can carry BOTH
    # spellings, and run_check_mode must find both.
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(
        handoffs / "stub-a-1.md", "rm-check-dual", "stub-a-1", 1, 1, 1,
        kind="spinoff-roadmap",
    )
    _write_stub(
        handoffs / "stub-b-2.md", "rm-check-dual", "stub-b-2", 2, 1, 2,
        blocked_by=["stub-a-1"], kind="roadmap-baton",
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-check-dual")
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 stub(s) checked" in out


# ---------------------------------------------------------------------------
# --state mode — awaiting_gate/ready_to_fire/gate_dependency readiness resolver
# ---------------------------------------------------------------------------


def _write_state_stub(
    path: Path,
    roadmap_id: str,
    stub_id: str,
    deployment_state: str,
    sprint,
    wave,
    gate_dependency=None,
    kind: str = "spinoff-roadmap",
) -> None:
    lines = [
        "---",
        f'title: "Test stub {stub_id}"',
        "created: 2026-07-17",
        "status: open",
        f"deployment_state: {deployment_state}",
        f"kind: {kind}",
        f"roadmap_id: {roadmap_id}",
        f"stub_id: {stub_id}",
    ]
    if sprint is not None:
        lines.append(f"sprint: {sprint}")
    if wave is not None:
        lines.append(f"wave: {wave}")
    if gate_dependency:
        lines.append(f'gate_dependency: "{gate_dependency}"')
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_state_mode_no_stubs_found_exits_0(tmp_path, monkeypatch, capsys):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_state_mode("nonexistent-roadmap")
    out = capsys.readouterr().out
    assert rc == 0
    assert "No roadmap-baton (spinoff-roadmap) stubs found" in out


def test_state_mode_prints_readiness_and_gate_dependency(tmp_path, monkeypatch, capsys):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_state_stub(
        handoffs / "stub-a-1.md", "rm-state", "stub-a-1", "ready_to_fire", 1, 1
    )
    _write_state_stub(
        handoffs / "stub-b-2.md",
        "rm-state",
        "stub-b-2",
        "awaiting_gate",
        1,
        2,
        gate_dependency="peer-team-ask:foo",
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_state_mode("rm-state")
    out = capsys.readouterr().out
    assert rc == 0
    assert "stub-a-1" in out
    assert "ready_to_fire" in out
    assert "stub-b-2" in out
    assert "awaiting_gate" in out
    assert "peer-team-ask:foo" in out
    assert "1 ready_to_fire, 1 awaiting_gate" in out


def test_state_mode_canonical_kind_roadmap_baton_is_found(tmp_path, monkeypatch, capsys):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_state_stub(
        handoffs / "stub-a-1.md", "rm-state-canon", "stub-a-1", "ready_to_fire", 1, 1,
        kind="roadmap-baton",
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_state_mode("rm-state-canon")
    out = capsys.readouterr().out
    assert rc == 0
    assert "stub-a-1" in out
    assert "1 ready_to_fire, 0 awaiting_gate" in out


def test_main_state_dispatches_to_run_state_mode(tmp_path, monkeypatch, capsys):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = main(["--state", "rm-state2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No roadmap-baton (spinoff-roadmap) stubs found for roadmap_id=rm-state2" in out


def test_main_state_missing_run_id_exits_2(capsys):
    rc = main(["--state"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a <run-id>" in err


def test_main_state_malformed_run_id_exits_2(capsys):
    rc = main(["--state", "Bad_ID!"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "must match" in err


# ---------------------------------------------------------------------------
# C1/C2/C7 — edges.txt reconciliation (AC1/AC2/AC4/AC10) and query-failure-
# vs-clean-result exit-code discipline (AC3/AC11).
# Spec backlink: pln-roadmap-dependency-graph-close-6192fb § C1/C2/C6/C7.
# ---------------------------------------------------------------------------


def _write_edges_file(directory: Path, filename: str, lines) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_check_mode_ac10_mid_flight_edges_txt_only_dependency_now_fails(
    tmp_path, monkeypatch, capsys
):
    """AC10 headline regression: reproduces the inbound memo's exact scenario —
    a mid-flight stub whose dependency exists only in edges.txt, never in stub
    frontmatter blocked_by. Before C1, --check reported OK (0) here because
    edges.txt was never read. It must now fail loud (non-zero).
    """
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(handoffs / "stub-a-1.md", "rm-ac10", "stub-a-1", 1, 1, 1)
    # stub-b-2 carries NO blocked_by in frontmatter — the dependency on
    # stub-a-1 is only ever declared in edges.txt below.
    _write_stub(handoffs / "stub-b-2.md", "rm-ac10", "stub-b-2", 2, 1, 1)

    _write_edges_file(
        tmp_path / "state" / "roadmap" / "rm-ac10",
        "2026-08-05-rm-ac10-wave-assignment-edges.txt",
        ["stub-b-2 <- stub-a-1"],
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    # Pre-C1 baseline sanity: frontmatter alone carries zero edges, and with
    # no edges check_dependency_order trivially reports ok — this is exactly
    # the "OK: dependency order is valid" the memo's reproduction hit.
    rc = run_check_mode("rm-ac10")
    err = capsys.readouterr().err

    assert rc != 0
    assert "FAIL [edge-source-divergence]" in err
    assert "edges.txt declares 1 edge(s)" in err
    assert "frontmatter carries only 0" in err
    assert "roadmap.link_stubs" in err


def test_check_mode_c1_carried_greater_than_declared_is_not_a_failure(
    tmp_path, monkeypatch, capsys
):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(handoffs / "stub-a-1.md", "rm-c1-carry", "stub-a-1", 1, 1, 1)
    _write_stub(
        handoffs / "stub-b-2.md", "rm-c1-carry", "stub-b-2", 2, 1, 2,
        blocked_by=["stub-a-1"],
    )
    _write_stub(
        handoffs / "stub-c-3.md", "rm-c1-carry", "stub-c-3", 3, 1, 3,
        blocked_by=["stub-a-1", "stub-b-2"],
    )

    # edges.txt declares only ONE of the three frontmatter-carried edges.
    # Frontmatter carrying more than edges.txt declares must not be a failure.
    _write_edges_file(
        tmp_path / "state" / "roadmap" / "rm-c1-carry",
        "edges.txt",
        ["stub-b-2 <- stub-a-1"],
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-c1-carry")
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK: dependency order is valid" in out


def test_check_mode_c1_ac4_ok_line_states_edge_count_and_frontmatter_source(
    tmp_path, monkeypatch, capsys
):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(handoffs / "stub-a-1.md", "rm-c1-ac4", "stub-a-1", 1, 1, 1)
    _write_stub(
        handoffs / "stub-b-2.md", "rm-c1-ac4", "stub-b-2", 2, 1, 2,
        blocked_by=["stub-a-1"],
    )

    _write_edges_file(
        tmp_path / "state" / "roadmap" / "rm-c1-ac4",
        "edges.txt",
        ["stub-b-2 <- stub-a-1"],
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-c1-ac4")
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 edge(s) read from edges.txt for reconciliation only" in out
    assert "stub frontmatter blocked_by is the ONLY edge source this check enforces" in out


def test_check_mode_c1_multi_match_glob_skips_reconciliation_visibly(
    tmp_path, monkeypatch, capsys
):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(handoffs / "stub-a-1.md", "rm-c1-multi", "stub-a-1", 1, 1, 1)
    _write_stub(
        handoffs / "stub-b-2.md", "rm-c1-multi", "stub-b-2", 2, 1, 2,
        blocked_by=["stub-a-1"],
    )

    edges_dir = tmp_path / "state" / "roadmap" / "rm-c1-multi"
    edges_dir.mkdir(parents=True)
    (edges_dir / "2026-08-01-wave1-edges.txt").write_text(
        "stub-b-2 <- stub-a-1\n", encoding="utf-8"
    )
    (edges_dir / "2026-08-02-wave2-edges.txt").write_text(
        "stub-b-2 <- stub-a-1\n", encoding="utf-8"
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-c1-multi")
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 candidate edges files found" in out
    assert "cannot establish which is authoritative" in out
    assert "skipping edges.txt reconciliation" in out
    # The reconciliation is skipped, not counted — the OK line must not carry
    # an edges.txt read-count when it was never established.
    assert "edge(s) read from edges.txt" not in out
    assert "OK: dependency order is valid" in out


def test_check_mode_c1_unmaterialized_label_does_not_manufacture_divergence(
    tmp_path, monkeypatch, capsys
):
    """An edges.txt row whose endpoints never resolved to on-disk stubs (a
    clusters.md-authored planning label that was later merged/abandoned) must
    not be counted toward the declared-edge total, and so must not manufacture
    a divergence FAIL.
    """
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(handoffs / "stub-a-1.md", "rm-c1-ghost", "stub-a-1", 1, 1, 1)
    _write_stub(
        handoffs / "stub-b-2.md", "rm-c1-ghost", "stub-b-2", 2, 1, 2,
        blocked_by=["stub-a-1"],
    )

    _write_edges_file(
        tmp_path / "state" / "roadmap" / "rm-c1-ghost",
        "edges.txt",
        ["ghost-label-a <- ghost-label-b"],
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-c1-ghost")
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert rc == 0
    assert "FAIL" not in err
    assert "OK: dependency order is valid" in out
    assert "0 edge(s) read from edges.txt" in out


def test_check_mode_c1_ac2_declared_only_edge_does_not_reach_check_dependency_order(
    tmp_path, monkeypatch, capsys
):
    """AC2 negative assertion: a declared-only edges.txt edge must never enter
    the graph handed to check_dependency_order. Constructed so that IF the
    edges.txt-only edge (stub-a-1 blocked_by stub-a-3, i.e. reversed order)
    were merged into the check graph it would trip a number-order violation —
    asserting on behaviour (no such violation appears), not just a count.
    """
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_stub(handoffs / "stub-a-1.md", "rm-c1-ac2", "stub-a-1", 1, 1, 1)
    _write_stub(
        handoffs / "stub-a-2.md", "rm-c1-ac2", "stub-a-2", 2, 1, 2,
        blocked_by=["stub-a-1"],
    )
    _write_stub(
        handoffs / "stub-a-3.md", "rm-c1-ac2", "stub-a-3", 3, 1, 3,
        blocked_by=["stub-a-1", "stub-a-2"],
    )

    # Declares a reversed edge never present in frontmatter: stub-a-1
    # blocked_by stub-a-3. If merged into the check graph this would require
    # number(stub-a-3) < number(stub-a-1) -- 3 < 1 -- a number-order
    # violation. carried (3) >= declared (1) so no reconciliation FAIL either.
    _write_edges_file(
        tmp_path / "state" / "roadmap" / "rm-c1-ac2",
        "edges.txt",
        ["stub-a-1 <- stub-a-3"],
    )

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    rc = run_check_mode("rm-c1-ac2")
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert rc == 0
    assert "FAIL [number-order]" not in err
    assert "FAIL [edge-source-divergence]" not in err
    assert "OK: dependency order is valid" in out


def test_check_mode_c2_both_queries_fail_returns_2(tmp_path, monkeypatch, capsys):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    def _raise(record_type, root, where=None):
        raise RuntimeError(f"boom: {record_type}")

    monkeypatch.setattr(mod, "query_records", _raise)

    rc = run_check_mode("rm-c2-both")
    err = capsys.readouterr().err
    assert rc == 2
    assert "ERROR: could not establish roadmap state" in err
    assert "both the live and archived roadmap-baton queries raised" in err


def test_check_mode_c2_one_query_fails_plus_empty_result_returns_2(
    tmp_path, monkeypatch, capsys
):
    """AC3 / F3 hole: a raised live query plus an empty (successful) archived
    result must be distinguished from the both-raised case AND from a genuine
    clean empty result — both must return 2, but with different stderr text.
    """
    (tmp_path / "state" / "handoffs").mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    def _one_raises(record_type, root, where=None):
        if record_type == "handoff":
            raise RuntimeError("live query boom")
        return []

    monkeypatch.setattr(mod, "query_records", _one_raises)

    rc = run_check_mode("rm-c2-one")
    err = capsys.readouterr().err
    assert rc == 2
    assert "ERROR: could not establish roadmap state" in err
    assert "the live roadmap-baton query raised and the other corpus returned no results" in err
    # Distinct from the both-raised case's message.
    assert "both the live and archived roadmap-baton queries raised" not in err


def test_state_mode_c7_both_queries_fail_returns_2(tmp_path, monkeypatch, capsys):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    def _raise(record_type, root, where=None):
        raise RuntimeError(f"boom: {record_type}")

    monkeypatch.setattr(mod, "query_records", _raise)

    rc = run_state_mode("rm-c7-both")
    err = capsys.readouterr().err
    assert rc == 2
    assert "ERROR: could not establish roadmap state" in err
    assert "both the live and archived roadmap-baton queries raised" in err


def test_state_mode_c7_one_query_fails_plus_empty_result_returns_2(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    def _one_raises(record_type, root, where=None):
        if record_type == "handoff":
            raise RuntimeError("live query boom")
        return []

    monkeypatch.setattr(mod, "query_records", _one_raises)

    rc = run_state_mode("rm-c7-one")
    err = capsys.readouterr().err
    assert rc == 2
    assert "ERROR: could not establish roadmap state" in err
    assert "the live roadmap-baton query raised and the other corpus returned no results" in err
    assert "both the live and archived roadmap-baton queries raised" not in err


# ---------------------------------------------------------------------------
# Review: code-reviewer, Finding 3 — third distinct branch of the C2/C7
# exit-code change: exactly one corpus raises but the OTHER corpus returns
# real (non-empty) results. `_roadmap_state_error_exit` must NOT exit(2) here
# — the surviving corpus's results are usable — but the run must still WARN
# that results are partial, and (check-mode only) skip edges.txt
# reconciliation against an admittedly-incomplete stub_id set rather than
# print a misleadingly-confident OK/FAIL against it.
# ---------------------------------------------------------------------------

_ONE_RAISES_NONEMPTY_FIXTURE = [
    {
        "path": "state/handoffs/stub-x-1.md",
        "frontmatter": {
            "stub_id": "stub-x-1",
            "number": 1,
            "sprint": 1,
            "wave": 1,
            "blocked_by": [],
        },
    }
]


def test_check_mode_one_query_fails_but_other_returns_results_proceeds_with_warning(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    def _one_raises_other_nonempty(record_type, root, where=None):
        if record_type == "handoff":
            raise RuntimeError("live query boom")
        return list(_ONE_RAISES_NONEMPTY_FIXTURE)

    monkeypatch.setattr(mod, "query_records", _one_raises_other_nonempty)

    rc = run_check_mode("rm-c3-partial")
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    assert rc == 0
    assert (
        "WARNING: results are partial — the live roadmap-baton query raised; "
        "this check only covers the corpus that succeeded" in err
    )
    assert "ERROR: could not establish roadmap state" not in err
    assert "NOTE: skipping edges.txt reconciliation — results are partial" in out
    assert "OK: dependency order is valid" in out


def test_state_mode_one_query_fails_but_other_returns_results_proceeds_with_warning(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)

    import coordinator_core.roadmap.number_stubs as mod

    monkeypatch.setattr(mod, "resolve_root", lambda: str(tmp_path))

    def _one_raises_other_nonempty(record_type, root, where=None):
        if record_type == "handoff":
            raise RuntimeError("live query boom")
        return list(_ONE_RAISES_NONEMPTY_FIXTURE)

    monkeypatch.setattr(mod, "query_records", _one_raises_other_nonempty)

    rc = run_state_mode("rm-c3-partial")
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    assert rc == 0
    assert (
        "WARNING: results are partial — the live roadmap-baton query raised; "
        "this state resolution only covers the corpus that succeeded" in err
    )
    assert "ERROR: could not establish roadmap state" not in err
    assert "stub-x-1" in out
    assert "1 stub(s): 0 ready_to_fire, 0 awaiting_gate." in out
