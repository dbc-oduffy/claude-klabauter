"""Executable form of `session_state_trigger`'s four-rule negative spec.

The module docstring is authoritative; if this file and that prose drift, the
prose wins and this file is updated to match it -- never the reverse.

Every constant asserted here was measured against shipped bundle 2.1.257 and is
cited in `docs/reference/harness-session-state-surface.md`. A test that changes
one of them without a fresh measurement is asserting a memory, not a fact.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.group_em import session_state_trigger as sst
from coordinator_core.session.harness_registry import RegistryRecord


def _rec(status, waiting_for=None, name="peer", cwd="/fake/repo-root"):
    return RegistryRecord(
        pid=1234,
        start_epoch=1_700_000_000.0,
        cwd=cwd,
        name=name,
        status=status,
        waiting_for=waiting_for,
    )


class TestClassification:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("idle", sst.PARKED_CANDIDATE),
            ("shell", sst.PARKED_CANDIDATE),
            ("busy", sst.WORKING),
        ],
    )
    def test_reachable_states_classify(self, status, expected):
        assert sst.classify("s1", _rec(status)).trigger == expected

    def test_shell_is_a_subtype_of_idle_not_a_third_state(self):
        """The harness's own notifier folds `shell` into idle; we match it.

        Measured: `status === "idle" || status === "shell"` drives the
        harness's peer-idle notifier. A downstream reader treating `shell` as
        distinct diverges from the harness's own semantics.
        """
        assert sst.classify("a", _rec("idle")).trigger == sst.classify(
            "b", _rec("shell")
        ).trigger

    def test_busy_is_not_excluded_only_not_pulled_forward(self):
        """Rule 1: a trigger widens. `busy` peers stay in the returned set.

        `busy` is forced by any live delegated task, so it is a narrower fact
        than "the model is generating" -- never grounds for dropping a peer.
        """
        scan = sst.TriggerScan(signals=[sst.classify("busy-peer", _rec("busy"))])
        assert len(scan.signals) == 1
        assert scan.parked_candidates() == []

    def test_unknown_status_is_returned_not_dropped(self):
        """An unrecognised value must survive into the caller's iteration.

        A future harness value this module has never seen must not silently
        vanish from the fleet -- that would be a narrowing, which rule 1
        forbids outright.
        """
        signal = sst.classify("s1", _rec("some-future-state"))
        assert signal.trigger == sst.UNCLASSIFIED
        assert sst.TriggerScan(signals=[signal]).unclassified() == [signal]


class TestWaitingIsNotAKindOfIdle:
    """`waiting` means a human is blocking -- the opposite of parked."""

    @pytest.mark.parametrize(
        "reason", ["input needed", "permission prompt", "dialog open", "goal proposal"]
    )
    def test_human_waits_are_blocked_on_human(self, reason):
        signal = sst.classify("s1", _rec("waiting", reason))
        assert signal.trigger == sst.BLOCKED_ON_HUMAN
        assert signal.needs_a_human

    @pytest.mark.parametrize("reason", ["sandbox request", "worker request"])
    def test_machine_waits_are_not_a_human_block(self, reason):
        """These clear themselves when the request completes; nobody is needed."""
        assert sst.classify("s1", _rec("waiting", reason)).trigger == sst.WORKING

    def test_waiting_never_reads_as_parked(self):
        signal = sst.classify("s1", _rec("waiting", "permission prompt"))
        assert signal.trigger != sst.PARKED_CANDIDATE

    def test_unknown_wait_reason_still_blocks_on_a_human(self):
        """Fail toward "someone is blocked", not toward silence.

        A `waitingFor` value we do not recognise is far likelier to be a new
        human-blocking dialog than a new machine request -- and the cost of
        being wrong is asymmetric: a surfaced peer that did not need one costs
        a glance, a missed one costs a stalled session nobody finds.
        """
        assert sst.classify("s1", _rec("waiting", "some-new-dialog")).trigger == (
            sst.BLOCKED_ON_HUMAN
        )

    def test_a_question_is_routable_and_a_permission_prompt_is_not(self):
        """Only `input needed` names a block a caller has an alternative for.

        A question can be put to an adversarial reviewer instead of a human.
        A permission decision cannot be delegated -- only the human holds it.
        """
        question = sst.classify("q", _rec("waiting", "input needed"))
        prompt = sst.classify("p", _rec("waiting", "permission prompt"))
        scan = sst.TriggerScan(signals=[question, prompt])

        assert scan.questions() == [question]
        assert len(scan.blocked_on_human()) == 2

    def test_zero_waiting_records_is_not_evidence_the_path_is_dead(self):
        """The config-vs-capability guard, as an executable reminder.

        On a bypass-mode fleet no dialog is ever opened, so `waiting` is
        structurally unreachable and a census returns zero. That is a fact
        about the fleet's configuration, not about the harness. This test
        exists so a reader who measures zero hits finds a red test rather than
        an empty code path that looks safe to delete.
        """
        assert sst.WAIT_REASONS_HUMAN, "the human-wait vocabulary must not be emptied"
        assert sst.classify("s", _rec("waiting", "input needed")).question_shaped


class TestNullVersusZero:
    def test_unreadable_registry_is_not_an_empty_fleet(self, monkeypatch):
        monkeypatch.setattr(sst.harness_registry, "registry_dir", lambda: None)
        monkeypatch.setattr(sst.harness_registry, "snapshot", dict)

        scan = sst.scan()
        assert scan.registry_readable is False
        assert scan.signals == []

    def test_readable_but_empty_is_distinguishable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sst.harness_registry, "registry_dir", lambda: tmp_path)
        monkeypatch.setattr(sst.harness_registry, "snapshot", dict)

        scan = sst.scan()
        assert scan.registry_readable is True
        assert scan.signals == []

    def test_scan_never_raises_when_the_snapshot_explodes(self, monkeypatch):
        def boom():
            raise RuntimeError("registry on fire")

        monkeypatch.setattr(sst.harness_registry, "snapshot", boom)
        scan = sst.scan()
        assert scan.registry_readable is False


class TestSettleLedger:
    """Rule 3: the settle is keyed on our own clock, never `statusUpdatedAt`."""

    def test_first_sighting_is_unknown_not_zero(self, tmp_path):
        ledger = sst.SettleLedger(tmp_path / "ledger.json")
        signal = sst.classify("s1", _rec("idle"))

        assert ledger.held_for(signal, now=1000.0) is None
        assert ledger.settled(signal, now=1000.0) is False

    def test_a_held_state_accumulates_and_settles(self, tmp_path):
        ledger = sst.SettleLedger(tmp_path / "ledger.json")
        signal = sst.classify("s1", _rec("idle"))

        ledger.observe([signal], now=1000.0)
        assert ledger.settled(signal, settle_seconds=120.0, now=1060.0) is False
        assert ledger.settled(signal, settle_seconds=120.0, now=1121.0) is True

    def test_a_changed_trigger_restarts_the_clock(self, tmp_path):
        """A peer that went busy and came back is a NEW stop, not a continuation."""
        ledger = sst.SettleLedger(tmp_path / "ledger.json")
        parked = sst.classify("s1", _rec("idle"))

        ledger.observe([parked], now=1000.0)
        ledger.observe([sst.classify("s1", _rec("busy"))], now=1100.0)
        ledger.observe([parked], now=1200.0)

        assert ledger.held_for(parked, now=1260.0) == pytest.approx(60.0)
        assert ledger.settled(parked, settle_seconds=120.0, now=1260.0) is False

    def test_a_changed_wait_reason_restarts_the_clock(self, tmp_path):
        """Blocked on a prompt then on a question is two blocks, not one."""
        ledger = sst.SettleLedger(tmp_path / "ledger.json")
        prompt = sst.classify("s1", _rec("waiting", "permission prompt"))
        question = sst.classify("s1", _rec("waiting", "input needed"))

        ledger.observe([prompt], now=1000.0)
        ledger.observe([question], now=1100.0)

        assert ledger.held_for(question, now=1150.0) == pytest.approx(50.0)

    def test_an_absent_peer_is_forgotten_and_earns_no_credit(self, tmp_path):
        """A returning peer must not be credited with time it spent away."""
        ledger = sst.SettleLedger(tmp_path / "ledger.json")
        signal = sst.classify("s1", _rec("idle"))

        ledger.observe([signal], now=1000.0)
        ledger.observe([], now=1100.0)
        ledger.observe([signal], now=1200.0)

        assert ledger.held_for(signal, now=1260.0) == pytest.approx(60.0)

    def test_the_ledger_survives_a_round_trip(self, tmp_path):
        path = tmp_path / "ledger.json"
        signal = sst.classify("s1", _rec("idle"))

        writer = sst.SettleLedger(path)
        writer.observe([signal], now=1000.0)
        writer.save()

        reader = sst.SettleLedger(path)
        reader.load()
        assert reader.settled(signal, settle_seconds=120.0, now=1121.0) is True

    def test_a_corrupt_ledger_degrades_to_unknown_not_to_settled(self, tmp_path):
        """Fail toward withholding: a premature nudge costs more than a late one."""
        path = tmp_path / "ledger.json"
        path.write_text("{not json at all", encoding="utf-8")

        ledger = sst.SettleLedger(path)
        ledger.load()
        assert ledger.settled(sst.classify("s1", _rec("idle")), now=9e9) is False


class TestTheBannedFieldIsAbsent:
    def test_the_banned_timestamp_is_never_READ_only_explained(self):
        """Rule 3, enforced against CODE rather than against prose.

        `coordinator_core/session/tests/test_status_ban_enforcement.py` holds
        the same line for `coordinator_core/session/`. This module sits outside
        that package, so the ban needs its own guard here or a future edit
        reintroduces the arithmetic where nothing is watching.

        Deliberately scoped to executable tokens: docstrings and comments MUST
        stay free to name the field, because explaining why it is banned is
        how the ban survives contact with the next reader. A guard that
        forbade the word outright would pressure its own rationale out of the
        file -- which is how a ban quietly becomes folklore and then gets
        narrowed by someone who never learned the reason.
        """
        import io as _io
        import tokenize
        from pathlib import Path

        source = Path(sst.__file__).read_text(encoding="utf-8")
        executable = []
        for tok in tokenize.generate_tokens(_io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            executable.append(tok.string)
        code = " ".join(executable)

        for token in ("updatedAt", "statusUpdatedAt"):
            assert token not in code, (
                f"{token!r} is READ in executable code -- this is the standing "
                f"2026-08-14 status ban. Nothing re-stamps a registry record, "
                f"so its age is time-since-transition, and a settle built on "
                f"it is the convenience that narrows the ban."
            )

    def test_that_guard_would_catch_a_real_reintroduction(self):
        """The guard above is worthless if it cannot fail; prove it can.

        A negative-spec test that has never been shown to go red is an
        assertion about its author's intent, not about the code.
        """
        import io as _io
        import tokenize

        offending = "age = record.statusUpdatedAt - now\n"
        executable = [
            tok.string
            for tok in tokenize.generate_tokens(_io.StringIO(offending).readline)
            if tok.type not in (tokenize.COMMENT, tokenize.STRING)
        ]
        assert "statusUpdatedAt" in " ".join(executable)

        benign = '"""a docstring naming statusUpdatedAt to explain it"""\n'
        executable = [
            tok.string
            for tok in tokenize.generate_tokens(_io.StringIO(benign).readline)
            if tok.type not in (tokenize.COMMENT, tokenize.STRING)
        ]
        assert "statusUpdatedAt" not in " ".join(executable)


class TestOracleIsDiagnosticOnly:
    def test_an_unavailable_oracle_is_not_agreement(self, monkeypatch):
        """`available: False` must never be read as "the two sources agree"."""

        def boom(*a, **k):
            raise FileNotFoundError("claude not on PATH")

        monkeypatch.setattr(sst.subprocess, "run", boom)
        report = sst.oracle_disagreements()

        assert report["available"] is False
        assert report["disagreements"] == []

    def test_the_oracle_folds_shell_into_idle_as_the_cli_does(self, monkeypatch):
        """The CLI reports `idle` where the registry says `shell`; not a drift."""

        class _Proc:
            stdout = json.dumps([{"sessionId": "s1", "status": "idle"}])

        monkeypatch.setattr(sst.subprocess, "run", lambda *a, **k: _Proc())
        monkeypatch.setattr(
            sst.harness_registry, "snapshot", lambda: {"s1": _rec("shell")}
        )

        assert sst.oracle_disagreements()["disagreements"] == []

    def test_a_real_status_mismatch_is_reported(self, monkeypatch):
        class _Proc:
            stdout = json.dumps([{"sessionId": "s1", "status": "idle"}])

        monkeypatch.setattr(sst.subprocess, "run", lambda *a, **k: _Proc())
        monkeypatch.setattr(
            sst.harness_registry, "snapshot", lambda: {"s1": _rec("busy")}
        )

        disagreements = sst.oracle_disagreements()["disagreements"]
        assert len(disagreements) == 1
        assert disagreements[0]["kind"] == "status-mismatch"

    def test_presence_in_one_source_only_is_reported_both_ways(self, monkeypatch):
        class _Proc:
            stdout = json.dumps([{"sessionId": "only-cli", "status": "idle"}])

        monkeypatch.setattr(sst.subprocess, "run", lambda *a, **k: _Proc())
        monkeypatch.setattr(
            sst.harness_registry, "snapshot", lambda: {"only-ours": _rec("idle")}
        )

        kinds = {d["kind"] for d in sst.oracle_disagreements()["disagreements"]}
        assert kinds == {"absent-from-registry", "absent-from-cli"}
