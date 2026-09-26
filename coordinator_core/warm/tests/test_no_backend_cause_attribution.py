"""coordinator_core.warm.tests.test_no_backend_cause_attribution

`read_discovery` answers None for five different reasons and says which only
through `read_discovery_with_cause`; `diagnose_no_backend` is the caller-facing
projection of that, plus the resolved path the record was looked for in.

Why these assertions and not others: the collapse being fixed is not a missing
feature, it is information the reader ALREADY COMPUTES and discards one frame
below. So each test here pins a distinction the control flow makes -- absent vs
unreadable vs torn vs not-an-object -- rather than a new behaviour. The pin that
matters most operationally is `test_two_engine_roots_resolve_two_records`: a
caller resolving a different root than the running listener reads a different
file and sees a permanent, self-consistent "absent" against a healthy listener,
which no counter can distinguish from a dead engine.

Deliberately NOT asserted: liveness. `diagnose_no_backend`'s negative spec is
that it never connects, probes, or stats a pid, so a test demanding it detect a
dead listener would be pinning the opposite of its contract.
"""

from __future__ import annotations

import json

from coordinator_core.warm import supervisor


def _svc(monkeypatch, tmp_path):
    monkeypatch.setattr(
        supervisor, "discovery_path", lambda engine_root=None: tmp_path / "warm-http.json"
    )
    return tmp_path / "warm-http.json"


class TestTheFiveAnswersAreDistinguishable:
    def test_absent_is_its_own_cause(self, monkeypatch, tmp_path):
        _svc(monkeypatch, tmp_path)
        record, cause = supervisor.read_discovery_with_cause()
        assert record is None
        assert cause == supervisor.CAUSE_RECORD_ABSENT

    def test_a_well_formed_record_reports_present_and_returns_itself(
        self, monkeypatch, tmp_path
    ):
        path = _svc(monkeypatch, tmp_path)
        path.write_text(json.dumps({"port": 1234, "pid": 9}), encoding="utf-8")
        record, cause = supervisor.read_discovery_with_cause()
        assert cause == supervisor.CAUSE_RECORD_PRESENT
        assert record == {"port": 1234, "pid": 9}

    def test_a_torn_parse_is_not_reported_as_absent(self, monkeypatch, tmp_path):
        path = _svc(monkeypatch, tmp_path)
        path.write_text('{"port": 12', encoding="utf-8")
        record, cause = supervisor.read_discovery_with_cause()
        assert record is None
        assert cause == supervisor.CAUSE_RECORD_UNPARSEABLE

    def test_valid_json_that_is_not_an_object_is_malformed_not_torn(
        self, monkeypatch, tmp_path
    ):
        path = _svc(monkeypatch, tmp_path)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        record, cause = supervisor.read_discovery_with_cause()
        assert record is None
        assert cause == supervisor.CAUSE_RECORD_MALFORMED

    def test_an_unreadable_record_is_not_reported_as_absent(self, monkeypatch, tmp_path):
        path = _svc(monkeypatch, tmp_path)
        path.write_text("{}", encoding="utf-8")

        def _boom(*_args, **_kwargs):
            raise PermissionError("sharing violation")

        monkeypatch.setattr(type(path), "read_text", _boom)
        record, cause = supervisor.read_discovery_with_cause()
        assert record is None
        assert cause == supervisor.CAUSE_RECORD_UNREADABLE

    def test_every_cause_returned_is_a_declared_one(self, monkeypatch, tmp_path):
        """No cause string reaches a caller that `READ_CAUSES` does not name --
        the same closed-set discipline `telemetry.record_degrade` holds for its
        own kinds, so nothing downstream has to attribute an unknown token."""
        path = _svc(monkeypatch, tmp_path)
        for content in [None, "{}", '{"a": 1}', "[1]", "{oops"]:
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_text(content, encoding="utf-8")
            _, cause = supervisor.read_discovery_with_cause()
            assert cause in supervisor.READ_CAUSES


class TestReadDiscoveryIsUnchangedByTheSplit:

    def test_it_still_returns_the_bare_record(self, monkeypatch, tmp_path):
        path = _svc(monkeypatch, tmp_path)
        path.write_text(json.dumps({"port": 7}), encoding="utf-8")
        assert supervisor.read_discovery() == {"port": 7}

    def test_it_still_returns_none_for_every_failure_shape(self, monkeypatch, tmp_path):
        path = _svc(monkeypatch, tmp_path)
        for content in ["{oops", "[1]", '"a string"']:
            path.write_text(content, encoding="utf-8")
            assert supervisor.read_discovery() is None
        path.unlink()
        assert supervisor.read_discovery() is None


class TestDiagnoseNamesWhereItLooked:
    def test_present_says_the_engine_side_is_not_the_problem(self, monkeypatch, tmp_path):
        path = _svc(monkeypatch, tmp_path)
        path.write_text(json.dumps({"port": 4321, "pid": 5}), encoding="utf-8")
        out = supervisor.diagnose_no_backend()
        assert out["cause"] == supervisor.CAUSE_RECORD_PRESENT
        assert out["record"]["port"] == 4321

    def test_it_always_names_the_file_it_consulted(self, monkeypatch, tmp_path):
        path = _svc(monkeypatch, tmp_path)
        out = supervisor.diagnose_no_backend()
        assert out["discovery_path"] == str(path)
        assert out["svc_dir"] == str(path.parent)

    def test_two_engine_roots_resolve_two_records(self, tmp_path):
        a = supervisor.diagnose_no_backend(tmp_path / "root-a")
        b = supervisor.diagnose_no_backend(tmp_path / "root-b")
        assert a["svc_dir"] != b["svc_dir"]
        assert a["engine_root"] != b["engine_root"]

    def test_it_reports_rather_than_raises_when_the_root_will_not_resolve(self):

        def _boom(*_args, **_kwargs):
            raise RuntimeError("no root")

        out = supervisor.diagnose_no_backend(path_resolver=_boom)
        assert out["cause"] == "engine_root_unresolvable"
        assert "no root" in out["detail"]

    def test_the_result_is_json_serialisable(self, monkeypatch, tmp_path):
        path = _svc(monkeypatch, tmp_path)
        path.write_text(json.dumps({"port": 1}), encoding="utf-8")
        json.dumps(supervisor.diagnose_no_backend())


class TestOneReaderServesBothDoors:

    def test_both_doors_delegate_to_the_one_reader(self, monkeypatch, tmp_path):
        from coordinator_core.warm import breadcrumb, front_door

        seen = []

        def _spy(path):
            seen.append(path)
            return {"port": 1}, breadcrumb.CAUSE_RECORD_PRESENT

        monkeypatch.setattr(breadcrumb, "read_record_with_cause", _spy)
        supervisor.read_discovery_with_cause(tmp_path)
        front_door.read_discovery_with_cause(tmp_path)

        assert len(seen) == 2
        assert seen[0] != seen[1]

    def test_the_two_doors_read_different_files(self, tmp_path):
        from coordinator_core.warm import front_door

        assert supervisor.discovery_path(tmp_path) != front_door.discovery_path(tmp_path)

    def test_front_door_read_discovery_still_returns_a_bare_record(
        self, monkeypatch, tmp_path
    ):
        from coordinator_core.warm import front_door

        path = tmp_path / "warm-front-door.json"
        monkeypatch.setattr(front_door, "discovery_path", lambda engine_root=None: path)
        path.write_text(json.dumps({"port": 99}), encoding="utf-8")
        assert front_door.read_discovery() == {"port": 99}
        path.write_text("{torn", encoding="utf-8")
        assert front_door.read_discovery() is None

    def test_diagnose_can_explain_the_front_door_without_a_second_copy(
        self, monkeypatch, tmp_path
    ):
        from coordinator_core.warm import front_door

        path = tmp_path / "warm-front-door.json"
        monkeypatch.setattr(front_door, "discovery_path", lambda engine_root=None: path)
        path.write_text(json.dumps({"port": 47623}), encoding="utf-8")

        out = supervisor.diagnose_no_backend(path_resolver=front_door.discovery_path)

        assert out["cause"] == supervisor.CAUSE_RECORD_PRESENT
        assert out["discovery_path"] == str(path)
        assert out["record"]["port"] == 47623

    def test_the_cause_vocabulary_has_exactly_one_definition(self):
        from coordinator_core.warm import breadcrumb

        assert supervisor.READ_CAUSES is breadcrumb.READ_CAUSES
