"""Work-state family of `orient_brief`: one test per R0 requirement row (REQ-W1..REQ-W9).

Every test builds a `tmp_path` fixture repo. Where the shipped op answers the same question
(`records.query`, `plan.list_orphaned`, `plugin_health.scan`, the memo surfacer), the test
pins the family's output against it on the same fixture.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from coordinator_core.orient_brief import _work

_DAY = 86400


def _repo(tmp_path: Path) -> Path:
    git = tmp_path / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


def _handoff(
    root: Path,
    name: str,
    *,
    title: str = "A handoff",
    status: str = "open",
    state: str = "ready_to_fire",
    created: str = "2026-10-01",
    extra: str = "",
    body: str = "body\n",
) -> Path:
    path = root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\nstatus: {status}\ndeployment_state: {state}\n"
        f"created: {created}\n{extra}---\n{body}",
        encoding="utf-8",
    )
    return path


def _plan(root: Path, name: str, *, status: str = "approved", created: str = "2026-09-01", extra: str = "") -> Path:
    path = root / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {name}\nstatus: {status}\ncreated: {created}\n{extra}---\nbody\n", encoding="utf-8")
    return path


def _age(path: Path, days: int) -> None:
    stamp = time.time() - days * _DAY
    os.utime(path, (stamp, stamp))


def _by_id(entries: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in entries}


def _collect(root: Path, cadence: str = "day"):
    return _work.collect(cadence, repo_root=root)


@pytest.fixture(autouse=True)
def _quiet_environment(monkeypatch, tmp_path_factory):
    """No drift points, no addon sentinels, no RAG directive, no memo feature unless a test asks."""
    empty = tmp_path_factory.mktemp("harness-env")
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("USERPROFILE", str(empty))
    monkeypatch.setenv("CLAUDE_HOME", str(empty))
    monkeypatch.setenv("COORDINATOR_PLUGINS_ROOT", str(empty / "plugins"))
    monkeypatch.setenv("COORDINATOR_CONSUMER_HEALTH_ROOT", str(empty / "consumer"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(empty))
    (empty / ".claude").mkdir()
    (empty / ".claude" / "settings.json").write_text(json.dumps({"effortLevel": "medium"}))
    monkeypatch.setattr("coordinator_core.ops.check_rag_state.check_rag_state", lambda: ("fresh", 0))
    monkeypatch.setattr("coordinator_core.machine_profile.feature_enabled", lambda name: False)


def test_clean_fixture_emits_nothing(tmp_path):
    result = _collect(_repo(tmp_path))
    assert result.directives == [] and result.judgment_points == []


def test_every_requirement_row_is_cited_in_the_module():
    source = Path(_work.__file__).read_text(encoding="utf-8")
    for n in range(1, 10):
        assert f"REQ-W{n}" in source


def test_status_vocabulary_tracks_the_shipped_plan_census():
    from coordinator_core.ops import draft_plan_aging as dpa

    assert _work._KNOWN_NON_TERMINAL_PLAN_STATUSES == dpa._KNOWN_NON_TERMINAL_PLAN_STATUSES
    assert _work._CARRY_OBSERVABILITY_FIX_LANDED_ON == dpa._CARRY_OBSERVABILITY_FIX_LANDED_ON
    assert _work._ORPHAN_AGE_DAYS == dpa.AGING_THRESHOLD_DAYS
    assert _work._PLAN_SIDECAR_SUFFIXES == dpa.SIDECAR_SUFFIXES


def test_a_failing_probe_loses_only_its_own_entries(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    _handoff(root, "a.md")

    def boom(_root):
        raise OSError("disk")

    monkeypatch.setattr(_work, "_scan_handoffs", boom)
    monkeypatch.setattr("coordinator_core.ops.check_rag_state.check_rag_state", lambda: ("stale", 1))
    result = _collect(root)
    assert "d-rag-staleness-regen" in _by_id(result.directives)
    assert "work/handoffs: OSError" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# REQ-W1: stale executing plans
# ---------------------------------------------------------------------------


def test_req_w1_stale_executing_plans(tmp_path):
    root = _repo(tmp_path)
    old = _plan(root, "2026-09-01-old.md", status="executing")
    _age(old, 10)
    fresh = _plan(root, "2026-09-02-fresh.md", status="executing")
    _age(fresh, 1)
    done = _plan(root, "2026-09-03-approved.md", status="approved")
    _age(done, 30)
    prefix = _plan(root, "2026-09-04-prefix.md", status="executing-foo")
    _age(prefix, 5)

    directive = _by_id(_collect(root).directives)["d-handoff-triage-stale-plans"]
    assert directive["cli"] == "workday-start-handoff-triage" and directive["args"] == ["stale-plans"]
    lines = directive["detail"].splitlines()
    assert lines == [
        f"  - {old} (status: executing, untouched 10d)",
        f"  - {prefix} (status: executing, untouched 5d)",
    ]


def test_req_w1_absent_when_every_executing_plan_is_fresh(tmp_path):
    root = _repo(tmp_path)
    _plan(root, "2026-09-01-live.md", status="executing")
    assert "d-handoff-triage-stale-plans" not in _by_id(_collect(root).directives)


# ---------------------------------------------------------------------------
# REQ-W2 / REQ-W3: handoff triage, pinned against records.query
# ---------------------------------------------------------------------------


def _records_listing(root: Path, where: str, **extra) -> list[str]:
    from coordinator_core.ops import records_query as rq

    params = {"type": "handoff", "where": where, "format": "markdown-list", "limit": 0, **extra}
    return rq._handler(params, root)["records"].splitlines()


_TITLES = [
    "plain title",
    '"double quoted"',
    "'single quoted'",
    '"with \\"escapes\\""',
    "has # a comment",
    "colon: inside",
    "> \n  folded across\n  two lines",
]


def _ready_fixture(root: Path, count: int) -> None:
    for i in range(count):
        _handoff(
            root,
            f"2026-09-{i + 1:02d}-h{i}.md",
            title=_TITLES[i % len(_TITLES)],
            created=f"2026-09-{i + 1:02d}",
        )


def test_req_w2_ready_listing_matches_records_query(tmp_path):
    root = _repo(tmp_path)
    _ready_fixture(root, 10)
    expected = _records_listing(
        root, "deployment_state=ready_to_fire AND status=open", sort="-created"
    )
    detail = _by_id(_collect(root).directives)["d-handoff-triage-ready"]["detail"]
    assert detail.splitlines() == expected


def test_req_w2_ready_listing_caps_at_fifteen_lines(tmp_path):
    root = _repo(tmp_path)
    _ready_fixture(root, 20)
    expected = _records_listing(
        root, "deployment_state=ready_to_fire AND status=open", sort="-created"
    )
    detail = _by_id(_collect(root).directives)["d-handoff-triage-ready"]["detail"].splitlines()
    assert detail[:15] == expected[:15]
    assert detail[15] == ""
    assert detail[16].endswith("more — run `workday-start-handoff-triage ready` to see them all")
    assert detail[16].startswith(f"+{len(expected) - 15 + 1} more")


def test_req_w2_excludes_consumed_marker_and_other_states(tmp_path):
    root = _repo(tmp_path)
    _handoff(root, "a-live.md", title="live")
    _handoff(root, "b-consumed.md", title="consumed", body="text\n<!-- consumed: 2026-10-01 by s -->\n")
    _handoff(root, "c-claimed.md", title="claimed", status="claimed")
    _handoff(root, "d-gated.md", title="gated", state="awaiting_gate")
    expected = _records_listing(root, "deployment_state=ready_to_fire AND status=open", sort="-created")
    detail = _by_id(_collect(root).directives)["d-handoff-triage-ready"]["detail"]
    assert detail.splitlines() == expected
    assert "consumed" not in detail and "[claimed]" not in detail


def test_req_w2_live_ledger_claim_suppresses_and_dead_one_does_not(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _handoff(root, "a-held.md", title="held", created="2026-10-02")
    _handoff(root, "b-dead.md", title="dead holder", created="2026-10-01")
    claims = root / ".git" / "coordinator-sessions" / "handoff-claims"
    for name, session in (("a-held.md", "live-session"), ("b-dead.md", "dead-session")):
        (claims / name).mkdir(parents=True)
        (claims / name / "session_id").write_text(session)
    monkeypatch.setattr(
        "coordinator_core.claim_state.cs_claim_holder_live",
        lambda claim_dir: Path(claim_dir).name == "a-held.md",
    )
    detail = _by_id(_collect(root).directives)["d-handoff-triage-ready"]["detail"]
    assert "dead holder" in detail and "held](" not in detail


def test_req_w2_absent_when_nothing_is_ready(tmp_path):
    root = _repo(tmp_path)
    _handoff(root, "a.md", state="shipped", status="closed")
    assert "d-handoff-triage-ready" not in _by_id(_collect(root).directives)


def test_req_w3_awaiting_gate_listing_and_stale_subset(tmp_path):
    root = _repo(tmp_path)
    recent = (date.today() - timedelta(days=1)).isoformat()
    for i in range(3):
        _handoff(root, f"g{i}.md", title=f"gated {i}", state="awaiting_gate", created=f"2026-08-0{i + 1}")
    _handoff(root, "g-recent.md", title="recent", state="awaiting_gate", created=recent)
    where = "deployment_state=awaiting_gate AND status=open"
    full = _records_listing(root, where, sort="-created")
    stale = _records_listing(root, where, older_than="6d")
    detail = _by_id(_collect(root).directives)["d-handoff-triage-awaiting-gate"]
    assert detail["args"] == ["awaiting-gate"]
    assert detail["detail"].splitlines() == full + ["--- awaiting_gate, older than 6d ---"] + stale


def test_req_w3_each_section_caps_on_its_own(tmp_path):
    root = _repo(tmp_path)
    for i in range(20):
        _handoff(root, f"g{i:02d}.md", title=f"gated {i}", state="awaiting_gate", created=f"2026-08-{i + 1:02d}")
    lines = _by_id(_collect(root).directives)["d-handoff-triage-awaiting-gate"]["detail"].splitlines()
    separator = lines.index("--- awaiting_gate, older than 6d ---")
    first, second = lines[:separator], lines[separator + 1:]
    assert len(first) == 17 and first[-1].startswith("+5 more")
    assert len(second) == 17 and second[-1].startswith("+6 more")


def test_req_w3_absent_when_nothing_awaits_a_gate(tmp_path):
    root = _repo(tmp_path)
    _handoff(root, "a.md")
    assert "d-handoff-triage-awaiting-gate" not in _by_id(_collect(root).directives)


# ---------------------------------------------------------------------------
# REQ-W4: orphaned-plan tiers, pinned against plan.list_orphaned
# ---------------------------------------------------------------------------


def _orphan_fixture(root: Path) -> None:
    _plan(root, "2026-09-01-authorized.md", extra="deliverable_id: dlv-a\nexecution_authorized_by: PM\n")
    _plan(root, "2026-09-01-parked.md", created="2026-08-01", extra="deliverable_id: dlv-b\n")
    _plan(root, "2026-08-01-also-parked.md", created="2026-08-01")
    _plan(root, "2026-10-03-recent.md", created=date.today().isoformat())
    _plan(root, "2026-07-01-legacy.md", created="2026-07-01", extra="deliverable_id: dlv-c\n")
    _plan(root, "2026-09-02-owned.md", extra="deliverable_id: dlv-owned\n")
    _plan(root, "2026-09-03-mystery.md", status="mystery")
    _plan(root, "2026-09-04-done.md", status="implemented")
    _plan(root, "2026-09-01-parked.review.md", status="approved")
    (root / "docs" / "plans" / "INDEX.md").write_text("no frontmatter\n")
    _handoff(root, "owner.md", extra="deliverable_id: dlv-owned\n")


def test_req_w4_tiers_match_the_shipped_census(tmp_path):
    from coordinator_core.ops.draft_plan_aging import list_orphaned

    root = _repo(tmp_path)
    _orphan_fixture(root)
    census = list_orphaned(root, 14)
    lines = _by_id(_collect(root).directives)["d-plan-orphan-tiers"]["detail"].splitlines()
    assert lines[0] == (
        f"P1 authorized_orphan: {census['authorized_orphan'][0]['path']} (execution_authorized_by=PM)"
    )
    assert f"P3 parked: {census['parked_count']} unowned plan(s), no authorization, age >= 14d" in lines
    assert any(l.startswith(f"legacy_unjoinable: {census['legacy_unjoinable_count']} plan(s)") for l in lines)
    assert any("unrecognized_status: docs/plans/2026-09-03-mystery.md (status='mystery')" == l for l in lines)
    assert census["owned_count"] == 1 and "2026-09-02-owned" not in "\n".join(lines)


def test_req_w4_unrecognized_status_lists_ten_then_counts_the_rest(tmp_path):
    root = _repo(tmp_path)
    for i in range(13):
        _plan(root, f"2026-09-{i + 1:02d}-odd.md", status="odd", extra="execution_authorized_by: PM\n")
    lines = _by_id(_collect(root).directives)["d-plan-orphan-tiers"]["detail"].splitlines()
    unrecognized = [l for l in lines if l.startswith("unrecognized_status")]
    assert len(unrecognized) == 11 and unrecognized[-1] == "unrecognized_status: +3 more"


def test_req_w4_reads_authorization_past_a_long_header(tmp_path):
    root = _repo(tmp_path)
    header = "".join(f"# filler line {i} padding padding padding padding\n" for i in range(400))
    _plan(root, "2026-09-01-long.md", extra=header + "execution_authorized_by: PM\n")
    detail = _by_id(_collect(root).directives)["d-plan-orphan-tiers"]["detail"]
    assert "P1 authorized_orphan: docs/plans/2026-09-01-long.md (execution_authorized_by=PM)" in detail


def test_req_w4_truncates_external_text(tmp_path):
    root = _repo(tmp_path)
    _plan(root, "2026-09-01-wordy.md", extra="execution_authorized_by: " + "x" * 400 + "\n")
    detail = _by_id(_collect(root).directives)["d-plan-orphan-tiers"]["detail"]
    assert "x" * 200 + "…" in detail and "x" * 201 not in detail


def test_req_w4_absent_when_every_plan_is_owned_or_terminal(tmp_path):
    root = _repo(tmp_path)
    _plan(root, "2026-09-01-done.md", status="implemented")
    assert "d-plan-orphan-tiers" not in _by_id(_collect(root).directives)


# ---------------------------------------------------------------------------
# REQ-W5: effort and model drift
# ---------------------------------------------------------------------------


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"), **extra}


def _settings(tmp_path: Path, effort: str | None) -> None:
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({} if effort is None else {"effortLevel": effort}))


def _transcript(tmp_path: Path, session: str, model: str) -> None:
    folder = tmp_path / ".claude" / "projects" / "some-project"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session}.jsonl").write_text(
        '{"model": "claude-opus-5"}\n' + json.dumps({"message": {"model": model}}) + "\n"
    )


def test_req_w5_clean_environment_emits_nothing(tmp_path):
    _settings(tmp_path, "medium")
    _transcript(tmp_path, "sid", "claude-opus-5-1")
    assert _work._em_environment_points(_env(tmp_path, CLAUDE_CODE_SESSION_ID="sid")) == []


def test_req_w5_effort_drift_and_model_drift_are_reportable_points(tmp_path):
    _settings(tmp_path, "high")
    _transcript(tmp_path, "sid", "claude-sonnet-5")
    points = _by_id(_work._em_environment_points(_env(tmp_path, CLAUDE_CODE_SESSION_ID="sid")))
    assert set(points) == {"j-em-env-effort", "j-em-env-model"}
    for point in points.values():
        assert point["reportable"] is True and point["reason"] == "recommendation-forbidden"
        assert [d["value"] for d in point["dispositions"]] == [point["dispositions"][0]["value"], "leave"]
    assert [d["value"] for d in points["j-em-env-effort"]["dispositions"]] == ["pin", "leave"]
    assert [d["value"] for d in points["j-em-env-model"]["dispositions"]] == ["switch", "leave"]


def test_req_w5_unpinned_effort_is_a_point(tmp_path):
    _settings(tmp_path, None)
    assert [p["id"] for p in _work._em_environment_points(_env(tmp_path))] == ["j-em-env-effort"]


def test_req_w5_unresolvable_transcript_degrades_to_silent(tmp_path):
    _settings(tmp_path, "medium")
    assert _work._em_environment_points(_env(tmp_path, CLAUDE_CODE_SESSION_ID="missing")) == []


def test_req_w5_project_settings_win_over_user_settings(tmp_path):
    _settings(tmp_path, "high")
    proj = tmp_path / "proj" / ".claude"
    proj.mkdir(parents=True)
    (proj / "settings.local.json").write_text(json.dumps({"effortLevel": "medium"}))
    assert _work._em_environment_points(_env(tmp_path)) == []


# ---------------------------------------------------------------------------
# REQ-W6: addon health, pinned against plugin_health.scan
# ---------------------------------------------------------------------------


def _sentinel(base: Path, plugin: str, **fields) -> None:
    folder = base / plugin / "data"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "doctor-last-run.json").write_text(json.dumps(fields))


def _addon_fixture(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    plugins, consumer = tmp_path / "plugins", tmp_path / "consumer"
    monkeypatch.setenv("COORDINATOR_PLUGINS_ROOT", str(plugins))
    monkeypatch.setenv("COORDINATOR_CONSUMER_HEALTH_ROOT", str(consumer))
    ran = "2026-01-01T00:00:00Z"
    _sentinel(plugins, "red-one", verdict="RED", ran_at=ran, red_probes=["p1", "p2"], hint="fix it")
    _sentinel(plugins, "amber-one", verdict="AMBER", ran_at=ran)
    _sentinel(plugins, "green-old", verdict="GREEN", ran_at=ran)
    _sentinel(plugins, "weird", verdict="PURPLE", ran_at=ran)
    _sentinel(consumer, "consumer-red", verdict="RED", ran_at=ran)
    (plugins / "_pre-refresh-snapshots" / "x" / "data").mkdir(parents=True)
    (plugins / "_pre-refresh-snapshots" / "x" / "data" / "doctor-last-run.json").write_text("{}")
    (plugins / "never-run" / "commands").mkdir(parents=True)
    (plugins / "never-run" / "commands" / "doctor.md").write_text("doctor")
    (plugins / "nested" / "plugin" / "commands").mkdir(parents=True)
    (plugins / "nested" / "plugin" / "commands" / "doctor.md").write_text("doctor")
    (plugins / "no-doctor" / "commands").mkdir(parents=True)
    hooks = plugins / "hooked" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": "run ${CLAUDE_PLUGIN_ROOT}/scripts/gone.sh"}]}]}})
    )
    return plugins, consumer


@pytest.mark.parametrize("cadence,mode", [("day", "--red-and-stale"), ("session", "--red-only"), ("week", "--red-only")])
def test_req_w6_addon_health_matches_the_shipped_scan(monkeypatch, tmp_path, cadence, mode):
    from coordinator_core.plugin_health import scan

    _addon_fixture(monkeypatch, tmp_path)
    expected, _ = scan._run(mode)
    directives = _work._addon_health_directives(cadence, time.time())
    assert [d["detail"] for d in directives] == expected
    assert [d["id"] for d in directives] == [f"d-addon-health-{i}" for i in range(1, len(expected) + 1)]
    assert all(d["cli"] == "scan-addon-health" and d["args"] == [mode] for d in directives)
    assert (len(expected) > 5) == (cadence == "day")


def test_req_w6_unreadable_sentinel_is_a_finding_at_day_only(monkeypatch, tmp_path):
    plugins = tmp_path / "plugins"
    monkeypatch.setenv("COORDINATOR_PLUGINS_ROOT", str(plugins))
    (plugins / "broken" / "data").mkdir(parents=True)
    (plugins / "broken" / "data" / "doctor-last-run.json").write_text("{not json")
    details = [d["detail"] for d in _work._addon_health_directives("day", time.time())]
    assert len(details) == 1 and details[0].startswith("[health] broken: sentinel unreadable at ")
    assert details[0].endswith("(malformed JSON?). Run /broken:doctor.")
    assert _work._addon_health_directives("session", time.time()) == []


# ---------------------------------------------------------------------------
# REQ-W7: inbound cross-repo memos
# ---------------------------------------------------------------------------


def _memo(root: Path, name: str, **fields: str) -> None:
    inbox = root / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}: {v}\n" for k, v in {"status": "open", "kind": "ask", "from": "peer", **fields}.items())
    (inbox / name).write_text(f"---\n{body}---\nbody\n", encoding="utf-8")


@pytest.fixture
def memos_on(monkeypatch):
    monkeypatch.setattr("coordinator_core.machine_profile.feature_enabled", lambda name: True)


def test_req_w7_memo_points_order_and_shape(tmp_path, memos_on):
    root = _repo(tmp_path)
    _memo(root, "a.md", created="2026-10-01", title="older ask")
    _memo(root, "b.md", created="2026-10-02", title="newer ask")
    _memo(root, "c.md", created="2026-10-03", title="a note", kind="fyi")
    _memo(root, "d.md", created="2026-10-03", title="in flight", status="in_progress", picked_up_by="s|1")
    _memo(root, "e.md", created="2026-05-01", title="grandfathered")
    _memo(root, "f.md", created="2026-10-03", title="done", status="actioned")
    points = _collect(root).judgment_points
    assert [p["id"] for p in points] == ["j-memo-1", "j-memo-2", "j-memo-3", "j-memo-4"]
    assert [p["question"].split("|")[3] for p in points] == [
        "in flight [CLAIMED by s–1]",
        "newer ask",
        "older ask",
        "a note",
    ]
    for point in points:
        assert point["reason"] == "recommendation-forbidden" and point["recommendation"] is None
        assert [d["value"] for d in point["dispositions"]] == ["accept", "decline", "surface_to_pm"]


def test_req_w7_matches_the_shipped_surfacer_lines(tmp_path, memos_on):
    from coordinator_core.ops import workday_start_cross_repo_memo_surface as surface

    root = _repo(tmp_path)
    for i in range(5):
        _memo(root, f"m{i}.md", created=f"2026-10-0{i + 1}", title=f"memo {i}", kind="fyi" if i % 2 else "ask")
    expected = sorted(surface._list_qualifying_lines(str(root / "state" / "cross-repo" / "inbox")))
    questions = [p["question"].split(": ", 1)[1] for p in _collect(root).judgment_points]
    assert sorted(questions) == expected


def test_req_w7_caps_at_fifteen_with_one_overflow_point(tmp_path, memos_on):
    root = _repo(tmp_path)
    for i in range(18):
        _memo(root, f"m{i:02d}.md", created="2026-10-01", title=f"memo {i}")
    points = _collect(root).judgment_points
    assert len(points) == 16 and points[-1]["id"] == "j-overflow-memo"
    assert "workday-start-cross-repo-memo-surface" in points[-1]["evidence"]
    assert [d["value"] for d in points[-1]["dispositions"]] == ["run_list_command", "leave_for_now"]


def test_req_w7_suppressed_at_session(tmp_path, memos_on):
    root = _repo(tmp_path)
    _memo(root, "a.md", created="2026-10-01", title="ask")
    assert _collect(root, "session").judgment_points == []
    assert len(_collect(root, "week").judgment_points) == 1


def test_req_w7_absent_when_the_feature_is_off_or_the_inbox_is_missing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _memo(root, "a.md", created="2026-10-01", title="ask")
    assert _collect(root).judgment_points == []
    monkeypatch.setattr("coordinator_core.machine_profile.feature_enabled", lambda name: True)
    assert _collect(_repo(tmp_path / "empty")).judgment_points == [] if (tmp_path / "empty").mkdir() is None else True


# ---------------------------------------------------------------------------
# REQ-W8: example-retrieval-repo state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer,emits",
    [(("stale", 0), True), (("unknown", 1), True), (("fresh", 0), False), (("absent", 0), False), (("", 1), False)],
)
def test_req_w8_rag_staleness(monkeypatch, answer, emits):
    monkeypatch.setattr("coordinator_core.ops.check_rag_state.check_rag_state", lambda: answer)
    directives = _work._rag_directive()
    assert bool(directives) is emits
    if emits:
        assert directives[0]["id"] == "d-rag-staleness-regen" and directives[0]["cli"] == "generate-repomap"
        assert directives[0]["detail"].startswith(f"example-retrieval-repo state={answer[0]!r} — regenerate repomap.")


# ---------------------------------------------------------------------------
# REQ-W9: agent worktrees
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def real_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "f.txt").write_text("x")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-m", "init")
    return root


def test_req_w9_agent_worktree_states(real_repo):
    clean = real_repo / ".claude" / "worktrees" / "agent-clean"
    dirty = real_repo / ".claude" / "worktrees" / "agent-dirty"
    benign = real_repo / ".claude" / "worktrees" / "agent-benign"
    ahead = real_repo / ".claude" / "worktrees" / "agent-ahead"
    for path, branch in ((clean, "b1"), (dirty, "b2"), (benign, "b3"), (ahead, "b4")):
        _git(real_repo, "worktree", "add", "-b", branch, str(path))
    (dirty / "scratch.txt").write_text("uncommitted")
    (benign / ".last-cleanup").write_text("x")
    (ahead / "g.txt").write_text("y")
    _git(ahead, "add", "g.txt")
    _git(ahead, "commit", "-m", "ahead")

    result = _work.collect("day", repo_root=real_repo)
    directives = _by_id(result.directives)
    assert [d["id"] for d in result.directives if d["id"].startswith("d-worktree")] == [
        "d-worktree-reap-1",
        "d-worktree-reap-2",
        "d-worktree-reap-3",
    ]
    assert all(d["cli"] == "agent-worktree-sweep" and d["args"] == ["--reap"] for d in directives.values() if d["id"].startswith("d-worktree"))
    details = " ".join(d["detail"] for d in result.directives)
    assert "empty-clean" in details and "dirty-benign" in details and "commits-clean" in details
    (point,) = [p for p in result.judgment_points if p["id"].startswith("j-worktree-dirty")]
    assert point["id"] == "j-worktree-dirty-1" and "agent-dirty" in point["question"]
    assert [d["value"] for d in point["dispositions"]] == ["pm_reviews_manually", "leave_for_now"]


def test_req_w9_absent_without_agent_worktrees_or_an_active_branch(real_repo, tmp_path):
    assert [d for d in _work.collect("day", repo_root=real_repo).directives if d["id"].startswith("d-worktree")] == []
    _git(real_repo, "worktree", "add", "-b", "b9", str(real_repo / ".claude" / "worktrees" / "agent-x"))
    (real_repo / ".git" / "HEAD").write_text("0" * 40 + "\n")
    assert _work._worktree_entries(real_repo) == ([], [])


def test_family_spawns_nothing_without_an_agent_worktree(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _handoff(root, "a.md")
    _plan(root, "2026-09-01-p.md", extra="execution_authorized_by: PM\n")

    def forbidden(*args, **kwargs):
        raise AssertionError(f"spawned: {args}")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert _collect(root).directives
