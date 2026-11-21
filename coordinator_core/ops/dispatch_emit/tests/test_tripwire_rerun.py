"""The post-land re-run of the tripwires a test verdict recorded NOT RUN: one detached `test_leg_run` at the landed sha."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register, test_leg_run
from coordinator_core.ops.dispatch_emit import ask_contract, halt_split_tree, halted_run, requirements_ledger, tripwire_rerun
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

SELF = "5e1f0000-0000-4000-8000-0000000000d1"
RUN = "run1"
TRIPWIRES = ["t/test_a.py", "t/test_b.py"]


@pytest.fixture
def spawns(monkeypatch):
    calls: list = []

    def fake(root, script, args=None, **_kw):
        calls.append((str(root), script, list(args or [])))
        return True

    monkeypatch.setattr(tripwire_rerun, "spawn_detached", fake)
    return calls


def test_launch_starts_one_detached_leg_over_exactly_the_tripwires(spawns, tmp_path):
    run_dir = tmp_path / "run"
    got = tripwire_rerun.launch(tmp_path, run_dir, "abc123", {"tripwires_not_run": [*TRIPWIRES, TRIPWIRES[0]]})

    assert len(spawns) == 1
    root, script, args = spawns[0]
    assert root == str(tmp_path) and script == "coordinator_core/ops/dispatch_emit/tripwire_rerun.py"
    assert args == [
        "--run-id", "run", "--head-sha", "abc123", "--touched", TRIPWIRES[0], "--touched", TRIPWIRES[1],
        "--out", str(run_dir / "tripwire-rerun.json"), "--", *TRIPWIRES,
    ]
    assert got == args


@pytest.mark.parametrize(
    "verdict",
    [None, {}, {"status": "pass"}, {"tripwires_not_run": []}, {"tripwires_not_run": "t/a.py"}, {"tripwires_not_run": [1, ""]}],
)
def test_launch_starts_nothing_without_named_tripwires(spawns, tmp_path, verdict):
    assert tripwire_rerun.launch(tmp_path, tmp_path, "abc", verdict) is None
    assert spawns == []


def test_launch_starts_nothing_without_a_run_dir_or_sha_and_survives_a_failed_spawn(spawns, tmp_path, monkeypatch):
    verdict = {"tripwires_not_run": TRIPWIRES}
    assert tripwire_rerun.launch(tmp_path, None, "abc", verdict) is None
    assert tripwire_rerun.launch(tmp_path, tmp_path, "", verdict) is None
    assert spawns == []
    monkeypatch.setattr(tripwire_rerun, "spawn_detached", lambda *a, **k: False)
    assert tripwire_rerun.launch(tmp_path, tmp_path, "abc", verdict) is None

    def boom(*a, **k):
        raise OSError("no spawn")

    monkeypatch.setattr(tripwire_rerun, "spawn_detached", boom)
    assert tripwire_rerun.launch(tmp_path, tmp_path, "abc", verdict) is None


LEDGER_RUN = "ask-20261001T000000-bbbbbb"
SIZING_REL = "state/sizings/2026-10-01-rerun.yaml"
OWNER = "0w4e0000-0000-4000-8000-0000000000aa"


@pytest.fixture
def ledger_repo(tmp_path, monkeypatch):
    """A repo root with a bound requirements ledger, a recorded owner and a live G-EM; messages are captured."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    doc = {
        "schema": "sizing-object", "name": "rerun", "intent": "exercise the re-run reaction",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-rerun-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make it so"}],
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    requirements_ledger.start(tmp_path, LEDGER_RUN)
    requirements_ledger.bind(tmp_path, LEDGER_RUN, SIZING_REL, ask_text="the ask")
    sent: dict = {"owner": [], "gem": []}
    monkeypatch.setattr(halted_run, "recorded_emitter", lambda root, run_id: OWNER)
    monkeypatch.setattr(tripwire_rerun, "_OWNER_SENDER", lambda owner, text: sent["owner"].append((owner, text)))
    monkeypatch.setattr(halt_split_tree, "_ALERT_SENDER", lambda name, text: sent["gem"].append((name, text)))
    monkeypatch.setattr(
        "coordinator_core.group_em.nomination.identify", lambda root: {"state": "live", "holder": {"name": "gem-1"}}
    )
    return tmp_path, sent


def _fake_leg(monkeypatch, record):
    """`test_leg_run.main` replaced: writes `record` to its `--out` (nothing when None) and returns exit 0."""

    def fake(argv):
        if record is not None:
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(record) + "\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(test_leg_run, "main", fake)


def _argv(root: Path) -> list:
    out = root / RUN_DIR_ROOT / LEDGER_RUN / tripwire_rerun.RERUN_RECORD
    out.parent.mkdir(parents=True, exist_ok=True)
    return ["--run-id", LEDGER_RUN, "--head-sha", "a" * 40, "--out", str(out), "--", *TRIPWIRES]


def _marker(root: Path) -> dict:
    return json.loads((ask_contract.record_dir(root, LEDGER_RUN) / halted_run.HALTED_FILE).read_text(encoding="utf-8"))


def _texts(root: Path) -> list:
    return [r["source_text"] for r in requirement_register.read_register((root / SIZING_REL).read_text(encoding="utf-8")).rows]


def test_red_rerun_reopens_the_run_writes_a_ledger_row_and_messages_owner_and_gem(ledger_repo, monkeypatch):
    root, sent = ledger_repo
    _fake_leg(monkeypatch, {"status": "fail", "new_red": ["t/test_a.py::test_x"], "over_budget": False})

    assert tripwire_rerun.main(_argv(root)) == 0

    marker = _marker(root)
    assert marker["status"] == "halted" and marker["halted"] == halted_run.HALT_POST_LAND_TRIPWIRE_RED
    assert marker["reds"] == ["t/test_a.py::test_x"] and marker["sha"] == "a" * 40
    assert any("t/test_a.py::test_x" in t and "a" * 12 in t for t in _texts(root))
    assert [o for o, _ in sent["owner"]] == [OWNER] and "t/test_a.py::test_x" in sent["owner"][0][1]
    assert [n for n, _ in sent["gem"]] == ["gem-1"]


def test_green_rerun_changes_nothing_and_sends_nothing(ledger_repo, monkeypatch):
    root, sent = ledger_repo
    ledger_file = requirements_ledger.run_dir(root, LEDGER_RUN) / "requirements.md"
    before = (ledger_file.read_bytes(), (root / SIZING_REL).read_bytes())
    _fake_leg(monkeypatch, {"status": "pass", "new_red": [], "over_budget": False})

    assert tripwire_rerun.main(_argv(root)) == 0

    assert (ledger_file.read_bytes(), (root / SIZING_REL).read_bytes()) == before
    assert not (ask_contract.record_dir(root, LEDGER_RUN) / halted_run.HALTED_FILE).exists()
    assert sent == {"owner": [], "gem": []}


def test_a_leg_that_raises_and_writes_no_record_reads_as_red(ledger_repo, monkeypatch):
    root, sent = ledger_repo

    def boom(argv):
        raise RuntimeError("leg died")

    monkeypatch.setattr(test_leg_run, "main", boom)

    assert tripwire_rerun.main(_argv(root)) == 1

    assert _marker(root)["halted"] == halted_run.HALT_POST_LAND_TRIPWIRE_RED
    assert len(sent["owner"]) == 1 and len(sent["gem"]) == 1


def test_a_stale_record_does_not_read_as_a_fresh_green(ledger_repo, monkeypatch):
    root, sent = ledger_repo
    argv = _argv(root)
    Path(argv[argv.index("--out") + 1]).write_text(json.dumps({"status": "pass", "new_red": []}), encoding="utf-8")
    monkeypatch.setattr(test_leg_run, "main", lambda a: 0)

    tripwire_rerun.main(argv)

    assert _marker(root)["halted"] == halted_run.HALT_POST_LAND_TRIPWIRE_RED and len(sent["gem"]) == 1


@pytest.mark.parametrize(
    "record",
    [{"status": "pass", "new_red": [], "over_budget": True}, {"status": "error", "new_red": [], "over_budget": False}],
)
def test_an_over_budget_or_errored_rerun_is_red(ledger_repo, record):
    root, sent = ledger_repo

    out = tripwire_rerun.react(root, LEDGER_RUN, "b" * 40, record)

    assert out["red"] is True and out["reopened"] and len(sent["owner"]) == 1 and len(sent["gem"]) == 1


def test_an_unbound_run_still_gets_the_marker_and_the_messages(ledger_repo):
    root, sent = ledger_repo

    out = tripwire_rerun.react(root, "ask-20261001T000000-cccccc", "b" * 40, {"status": "fail", "new_red": ["t/x.py::t"]})

    assert out["ledger_row"] is False and out["reopened"] is True
    assert len(sent["owner"]) == 1 and len(sent["gem"]) == 1


def test_reds_beyond_the_shown_cap_are_summarised_in_the_messages(ledger_repo):
    root, sent = ledger_repo
    reds = [f"t/x.py::t{i}" for i in range(tripwire_rerun._REDS_SHOWN + 3)]

    out = tripwire_rerun.react(root, LEDGER_RUN, "b" * 40, {"status": "fail", "new_red": reds})

    assert out["reds"] == reds
    text = sent["owner"][0][1]
    assert "(+3 more)" in text and reds[tripwire_rerun._REDS_SHOWN - 1] in text and reds[-1] not in text


def test_react_sends_no_owner_message_under_pytest_without_the_seam(ledger_repo, monkeypatch):
    root, sent = ledger_repo
    monkeypatch.setattr(tripwire_rerun, "_OWNER_SENDER", None)

    out = tripwire_rerun.react(root, LEDGER_RUN, "b" * 40, {"status": "fail", "new_red": ["t/x.py::t"]})

    assert out["owner_sent"] is False and sent["owner"] == [] and len(sent["gem"]) == 1
