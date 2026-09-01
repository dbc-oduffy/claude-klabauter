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
    """Point `discovery_path` at a scratch dir, so nothing here reads or writes
    the real per-clone svc dir this box's live listener is using."""
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
        """The distinction the incident needed: a half-written record and no
        record at all are the same `None` to every existing caller, and mean
        opposite things -- one says a writer is mid-rotation, the other says no
        listener has ever published here."""
        path = _svc(monkeypatch, tmp_path)
        path.write_text('{"port": 12', encoding="utf-8")
        record, cause = supervisor.read_discovery_with_cause()
        assert record is None
        assert cause == supervisor.CAUSE_RECORD_UNPARSEABLE

    def test_valid_json_that_is_not_an_object_is_malformed_not_torn(
        self, monkeypatch, tmp_path
    ):
        """Retrying cannot help here -- this is what the writer actually wrote --
        so it must not be reported as the retryable torn-read cause."""
        path = _svc(monkeypatch, tmp_path)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        record, cause = supervisor.read_discovery_with_cause()
        assert record is None
        assert cause == supervisor.CAUSE_RECORD_MALFORMED

    def test_an_unreadable_record_is_not_reported_as_absent(self, monkeypatch, tmp_path):
        """The Windows sharing-violation window `read_discovery`'s own docstring
        was hardened for: a rename beating the open. Budget spends, answer is
        `unreadable`, never `absent`."""
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
    """`read_discovery` is now a projection of the cause-carrying reader. Its
    contract -- a dict or None, never a raise -- must be exactly what it was, or
    this refactor has moved a hot path's behaviour while claiming not to."""

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
        # The caller gets port/pid without a second read, so it can say "record
        # fine, connect failed" -- its own arm -- rather than "no backend".
        assert out["record"]["port"] == 4321

    def test_it_always_names_the_file_it_consulted(self, monkeypatch, tmp_path):
        path = _svc(monkeypatch, tmp_path)
        out = supervisor.diagnose_no_backend()
        assert out["discovery_path"] == str(path)
        assert out["svc_dir"] == str(path.parent)

    def test_two_engine_roots_resolve_two_records(self, tmp_path):
        """THE 38-MINUTE SHAPE. The record is per-clone, so a caller resolving a
        different engine root than the running listener consults a different
        file entirely -- reporting a stable, self-consistent absence while the
        listener is healthy. Asserted against the real resolver, not a patched
        one, because the point is that this divergence needs no bug to occur.
        """
        a = supervisor.diagnose_no_backend(tmp_path / "root-a")
        b = supervisor.diagnose_no_backend(tmp_path / "root-b")
        assert a["svc_dir"] != b["svc_dir"]
        assert a["engine_root"] != b["engine_root"]

    def test_it_reports_rather_than_raises_when_the_root_will_not_resolve(self):
        """An instrument for explaining a degraded state may not add a second
        failure to it. Exercised through the resolver, which is where root
        resolution actually happens -- a caller that cannot resolve an engine
        root was never going to find a record, and saying so beats reporting
        `record_absent` from a path that was never computed."""

        def _boom(*_args, **_kwargs):
            raise RuntimeError("no root")

        out = supervisor.diagnose_no_backend(path_resolver=_boom)
        assert out["cause"] == "engine_root_unresolvable"
        assert "no root" in out["detail"]

    def test_the_result_is_json_serialisable(self, monkeypatch, tmp_path):
        """The consumer writes this into a degrade row on a stdlib-only path.
        A Path leaking into the dict would fail there and nowhere else."""
        path = _svc(monkeypatch, tmp_path)
        path.write_text(json.dumps({"port": 1}), encoding="utf-8")
        json.dumps(supervisor.diagnose_no_backend())


class TestOneReaderServesBothDoors:
    """`supervisor` and `front_door` carried byte-identical copies of this
    reader, differing only in which `discovery_path` they called. They now
    share `breadcrumb.read_record_with_cause`.

    Pinned because the duplication is what let the collapse exist in two
    places at once, and a future edit that "fixes" one copy would silently
    leave the other -- which is the exact shape of the bug this whole seam is
    about. Distinct records justify distinct paths, never distinct algorithms.
    """

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
        # Same reader, two different records -- the paths must differ, or the
        # two doors would be reading each other's discovery file.
        assert seen[0] != seen[1]

    def test_the_two_doors_read_different_files(self, tmp_path):
        from coordinator_core.warm import front_door

        assert supervisor.discovery_path(tmp_path) != front_door.discovery_path(tmp_path)

    def test_front_door_read_discovery_still_returns_a_bare_record(
        self, monkeypatch, tmp_path
    ):
        """Its public contract is unchanged by becoming a projection."""
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
        """The succession contract lets a front door take the 47623 seat. When
        it does, it inherits this diagnosis via the resolver parameter rather
        than re-growing the collapse under a new owner."""
        from coordinator_core.warm import front_door

        path = tmp_path / "warm-front-door.json"
        monkeypatch.setattr(front_door, "discovery_path", lambda engine_root=None: path)
        path.write_text(json.dumps({"port": 47623}), encoding="utf-8")

        out = supervisor.diagnose_no_backend(path_resolver=front_door.discovery_path)

        assert out["cause"] == supervisor.CAUSE_RECORD_PRESENT
        assert out["discovery_path"] == str(path)
        assert out["record"]["port"] == 47623

    def test_the_cause_vocabulary_has_exactly_one_definition(self):
        """Re-exported, never redefined: a second closed set is a second thing
        to drift, and DoE attributes against these strings by value."""
        from coordinator_core.warm import breadcrumb

        assert supervisor.READ_CAUSES is breadcrumb.READ_CAUSES
