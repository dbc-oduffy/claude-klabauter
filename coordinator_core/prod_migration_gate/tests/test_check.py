"""Tests for the pure prod-migration checker: each rule's breach and its passing counterpart."""

import hashlib
import json

from coordinator_core.prod_migration_gate.check import Change, check_prod_migrations

D = "packages/ddct"
MIG = f"{D}/migrations"
MANIFEST = f"{MIG}/applied-in-production.json"
OLD = f"{MIG}/1006_old.sql"
OLD_BYTES = b"select 1;"


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _manifest():
    return json.dumps({"1006_old.sql": _sha(OLD_BYTES)}).encode()


def _run(changes, landed=None, head=None, started=frozenset({"1011"})):
    landed = {} if landed is None else landed
    head = {MANIFEST: _manifest()} if head is None else head
    calls = []

    def st():
        calls.append(1)
        if isinstance(started, Exception):
            raise started
        return started

    out = check_prod_migrations(changes, landed.get, head.get, st)
    return out, calls


def _landed(**extra):
    base = {MANIFEST: _manifest(), OLD: OLD_BYTES}
    base.update(extra)
    return base


def test_added_without_down_refused():
    out, _ = _run([Change("A", f"{MIG}/1011_new.sql")], _landed(**{f"{MIG}/1011_new.sql": b"x"}))
    assert [r.rule for r in out] == ["D"]


def test_added_with_down_and_started_passes():
    landed = _landed(**{f"{MIG}/1011_new.sql": b"x", f"{D}/migrations-down/1011_new.down.sql": b"y"})
    out, _ = _run([Change("A", f"{MIG}/1011_new.sql")], landed)
    assert out == []


def test_modified_applied_refused_unchanged_bytes_pass():
    out, _ = _run([Change("M", OLD)], _landed(**{OLD: b"select 2;"}))
    assert [r.rule for r in out] == ["I"]
    assert _run([Change("M", OLD)], _landed())[0] == []


def test_deleted_and_renamed_away_applied_refused():
    assert [r.rule for r in _run([Change("D", OLD)], {MANIFEST: _manifest()})[0]] == ["I"]
    out, _ = _run([Change("R", f"{MIG}/1006_renamed.sql", OLD)], _landed())
    assert "I" in [r.rule for r in out]


def test_unlisted_migration_edit_passes():
    other = f"{MIG}/1007_other.sql"
    assert _run([Change("M", other)], _landed(**{other: b"z"}))[0] == []


def test_repo_without_manifest_passes_and_started_not_called():
    p = "app/migrations/1_a.sql"
    out, calls = _run([Change("A", p)], {p: b"x"}, head={})
    assert out == [] and calls == []


def test_manifest_only_in_landed_tree_scopes_the_repo():
    landed = {MANIFEST: b"{}", f"{MIG}/1011_new.sql": b"x"}
    out, _ = _run([Change("A", f"{MIG}/1011_new.sql")], landed, head={})
    assert [r.rule for r in out] == ["D"]


def test_number_absent_from_started_refused_present_passes():
    landed = _landed(**{f"{MIG}/1011_new.sql": b"x", f"{D}/migrations-down/1011_new.down.sql": b"y"})
    out, calls = _run([Change("A", f"{MIG}/1011_new.sql")], landed, started=frozenset({"1012"}))
    assert [r.rule for r in out] == ["S"] and calls == [1]
    assert _run([Change("A", f"{MIG}/1011_new.sql")], landed, started=frozenset({"1011"}))[0] == []


def test_started_lazy_only_when_added_exists():
    _, calls = _run([Change("M", OLD)], _landed())
    assert calls == []


def test_started_failure_is_a_refusal_not_a_raise():
    landed = _landed(**{f"{MIG}/1011_new.sql": b"x", f"{D}/migrations-down/1011_new.down.sql": b"y"})
    out, _ = _run([Change("A", f"{MIG}/1011_new.sql")], landed, started=RuntimeError("no store"))
    assert [r.rule for r in out] == ["S"] and "no store" in out[0].message


def test_variable_width_number_and_windows_separators():
    p = "packages\\ddct\\migrations\\100_id.sql"
    landed = {MANIFEST: b"{}", "packages/ddct/migrations/100_id.sql": b"x",
              "packages/ddct/migrations-down/100_id.down.sql": b"y"}
    out, _ = _run([Change("A", p)], landed, started=frozenset({"100"}))
    assert out == []


def test_non_migration_paths_ignored():
    assert _run([Change("A", "README.md"), Change("M", MANIFEST)], _landed())[0] == []
